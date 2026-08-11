"""
Candidate profile model.

Stores reusable application information.
Sensitive fields (salary expectations, etc.) are encrypted at rest.
"""

from __future__ import annotations

from datetime import datetime
from typing import Optional

from sqlalchemy import Boolean, DateTime, Integer, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from jobhunter.database import Base


class CandidateProfile(Base):
    """Reusable candidate data for application auto-fill."""

    __tablename__ = "candidate_profiles"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)

    # ── Identity ──────────────────────────────
    full_name: Mapped[Optional[str]] = mapped_column(String(256))
    preferred_name: Mapped[Optional[str]] = mapped_column(String(128))
    email: Mapped[Optional[str]] = mapped_column(String(256))
    phone: Mapped[Optional[str]] = mapped_column(String(64))
    city: Mapped[Optional[str]] = mapped_column(String(128))
    country: Mapped[Optional[str]] = mapped_column(String(128))

    # ── Education ─────────────────────────────
    university: Mapped[Optional[str]] = mapped_column(String(256))
    degree: Mapped[Optional[str]] = mapped_column(String(256))
    current_year: Mapped[Optional[int]] = mapped_column(Integer)
    graduation_expected: Mapped[Optional[str]] = mapped_column(String(32))

    # ── Work authorization ────────────────────
    work_authorization: Mapped[Optional[str]] = mapped_column(String(128))
    residence_permit_expiry: Mapped[Optional[str]] = mapped_column(String(32))
    requires_sponsorship: Mapped[bool] = mapped_column(Boolean, default=False)

    # ── Links ─────────────────────────────────
    linkedin_url: Mapped[Optional[str]] = mapped_column(Text)
    github_url: Mapped[Optional[str]] = mapped_column(Text)
    portfolio_url: Mapped[Optional[str]] = mapped_column(Text)

    # ── Preferences ───────────────────────────
    preferred_locations: Mapped[Optional[str]] = mapped_column(Text)  # JSON list
    availability: Mapped[Optional[str]] = mapped_column(String(256))
    relocation_willing: Mapped[bool] = mapped_column(Boolean, default=True)

    # ── Sensitive (stored encrypted) ──────────
    # Prefix "enc_" signals these need Fernet decryption before use
    enc_salary_expectation_huf: Mapped[Optional[str]] = mapped_column(Text)

    # ── Skills ────────────────────────────────
    skills: Mapped[Optional[str]] = mapped_column(Text)  # JSON list
    languages: Mapped[Optional[str]] = mapped_column(Text)  # JSON list

    # ── Meta ──────────────────────────────────
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    def __repr__(self) -> str:
        return f"<CandidateProfile {self.full_name!r}>"


class Document(Base):
    """
    Tracks uploaded documents (CVs, cover letters, transcripts).

    Actual files live in data/documents/ — never in the database.
    """

    __tablename__ = "documents"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(256), nullable=False)
    doc_type: Mapped[str] = mapped_column(String(64), nullable=False)
    # e.g. cv_general, cv_robotics, cv_automation, cover_letter, transcript, enrollment
    variant: Mapped[Optional[str]] = mapped_column(String(64))
    file_path: Mapped[str] = mapped_column(Text, nullable=False)
    file_hash: Mapped[Optional[str]] = mapped_column(String(64))
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    def __repr__(self) -> str:
        return f"<Document {self.doc_type!r} {self.name!r}>"
