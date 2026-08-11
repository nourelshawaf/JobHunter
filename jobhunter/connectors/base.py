"""
BaseConnector — abstract interface that every job source adapter must implement.

Each connector is isolated: a failure in one never stops the others.
All HTTP requests must go through the provided httpx client which
enforces rate limiting and respects robots.txt restrictions.
"""

from __future__ import annotations

import abc
import asyncio
import random
import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Optional

import httpx
import structlog

from jobhunter.config import Settings, get_settings

logger = structlog.get_logger(__name__)


@dataclass
class RawJob:
    """
    Minimal job data as returned by a connector.

    Connectors populate only the fields they can extract.
    The normaliser fills gaps and converts types.
    """

    source: str
    source_job_id: Optional[str]
    title: str
    company: str
    location: Optional[str] = None
    description: Optional[str] = None
    requirements: Optional[str] = None
    application_url: Optional[str] = None
    source_url: Optional[str] = None
    posted_at: Optional[datetime] = None
    deadline: Optional[datetime] = None
    salary_raw: Optional[str] = None
    work_mode_raw: Optional[str] = None  # "remote" | "hybrid" | "on-site" | None
    job_type_raw: Optional[str] = None
    language_requirements: Optional[str] = None
    extra: dict[str, Any] = field(default_factory=dict)  # source-specific extras


@dataclass
class ConnectorResult:
    """Return value of a connector run."""

    connector_name: str
    jobs: list[RawJob]
    errors: list[str]
    started_at: datetime
    finished_at: datetime
    success: bool

    @property
    def duration_seconds(self) -> float:
        delta = self.finished_at - self.started_at
        return delta.total_seconds()


class BaseConnector(abc.ABC):
    """
    Abstract base class for all job source connectors.

    Subclasses implement ``_fetch_jobs`` and optionally override
    ``_is_healthy``. Everything else (rate limiting, retries,
    error isolation) is handled here.
    """

    #: Unique name used in config, logs, and the database source field.
    name: str = ""

    #: Human-readable description shown in the dashboard.
    description: str = ""

    #: Whether this connector requires a browser (Playwright).
    requires_browser: bool = False

    def __init__(self, settings: Optional[Settings] = None) -> None:
        self.settings = settings or get_settings()
        self._last_request_at: float = 0.0
        self._consecutive_errors: int = 0
        self._http_client: Optional[httpx.AsyncClient] = None

    # ── Public API ────────────────────────────

    async def run(self) -> ConnectorResult:
        """
        Execute the connector and return a ``ConnectorResult``.

        Never raises — all exceptions are caught and logged.
        """
        started = datetime.utcnow()
        errors: list[str] = []
        jobs: list[RawJob] = []

        logger.info("connector.start", connector=self.name)

        try:
            async with self._build_http_client() as client:
                self._http_client = client
                jobs = await self._fetch_jobs()
                self._consecutive_errors = 0
                logger.info(
                    "connector.done",
                    connector=self.name,
                    jobs_found=len(jobs),
                )
        except Exception as exc:
            self._consecutive_errors += 1
            error_msg = f"{type(exc).__name__}: {exc}"
            errors.append(error_msg)
            logger.error(
                "connector.error",
                connector=self.name,
                error=error_msg,
                consecutive_errors=self._consecutive_errors,
            )
        finally:
            self._http_client = None

        return ConnectorResult(
            connector_name=self.name,
            jobs=jobs,
            errors=errors,
            started_at=started,
            finished_at=datetime.utcnow(),
            success=len(errors) == 0,
        )

    async def is_healthy(self) -> bool:
        """Quick health check — override for source-specific ping."""
        try:
            return await self._is_healthy()
        except Exception:
            return False

    # ── Abstract ──────────────────────────────

    @abc.abstractmethod
    async def _fetch_jobs(self) -> list[RawJob]:
        """
        Fetch and return raw jobs from the source.

        Must use ``self._get`` / ``self._post`` for HTTP requests
        so rate limiting is applied automatically.
        """
        ...

    # ── Overridable ───────────────────────────

    async def _is_healthy(self) -> bool:
        """Return True if the source is reachable. Override per-connector."""
        return True

    # ── HTTP helpers ─────────────────────────

    def _build_http_client(self) -> httpx.AsyncClient:
        """Build an httpx client with reasonable defaults."""
        return httpx.AsyncClient(
            timeout=httpx.Timeout(30.0, connect=10.0),
            headers={
                "User-Agent": (
                    "JobHunter/0.1 (personal job search tool; "
                    "respectful automated access; contact: see README)"
                ),
                "Accept-Language": "en-US,en;q=0.9,hu;q=0.8",
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            },
            follow_redirects=True,
        )

    async def _get(self, url: str, **kwargs: Any) -> httpx.Response:
        """Rate-limited GET request with retry on transient errors."""
        await self._throttle()
        return await self._request_with_retry("GET", url, **kwargs)

    async def _post(self, url: str, **kwargs: Any) -> httpx.Response:
        """Rate-limited POST request with retry on transient errors."""
        await self._throttle()
        return await self._request_with_retry("POST", url, **kwargs)

    async def _request_with_retry(
        self,
        method: str,
        url: str,
        max_retries: int = 3,
        **kwargs: Any,
    ) -> httpx.Response:
        """Retry on 429 / 5xx with exponential backoff."""
        assert self._http_client is not None, "_http_client not set — call inside run()"

        last_exc: Optional[Exception] = None
        for attempt in range(max_retries):
            try:
                response = await self._http_client.request(method, url, **kwargs)

                if response.status_code == 429:
                    retry_after = int(response.headers.get("Retry-After", 60))
                    logger.warning(
                        "connector.rate_limited",
                        connector=self.name,
                        retry_after=retry_after,
                    )
                    await asyncio.sleep(retry_after)
                    continue

                if response.status_code >= 500:
                    wait = (2**attempt) * 5
                    logger.warning(
                        "connector.server_error",
                        connector=self.name,
                        status=response.status_code,
                        retry_in=wait,
                    )
                    await asyncio.sleep(wait)
                    continue

                response.raise_for_status()
                self._last_request_at = time.monotonic()
                return response

            except httpx.TransportError as exc:
                last_exc = exc
                wait = (2**attempt) * 3
                logger.warning(
                    "connector.transport_error",
                    connector=self.name,
                    error=str(exc),
                    retry_in=wait,
                )
                await asyncio.sleep(wait)

        raise RuntimeError(
            f"[{self.name}] Failed after {max_retries} attempts on {url}: {last_exc}"
        )

    async def _throttle(self) -> None:
        """Ensure minimum delay between requests."""
        now = time.monotonic()
        elapsed = now - self._last_request_at
        min_delay = self.settings.min_request_delay_seconds
        max_delay = self.settings.max_request_delay_seconds
        wait = random.uniform(min_delay, max_delay) - elapsed
        if wait > 0:
            await asyncio.sleep(wait)
