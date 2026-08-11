# ── Stage 1: build ────────────────────────────────────────────────────────
FROM python:3.12-slim AS builder

WORKDIR /app

# System dependencies for lxml, Playwright, etc.
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc \
    libxml2-dev \
    libxslt-dev \
    && rm -rf /var/lib/apt/lists/*

COPY pyproject.toml .
RUN pip install --no-cache-dir --upgrade pip && \
    pip install --no-cache-dir -e ".[dev]" --prefix=/install

# ── Stage 2: runtime ──────────────────────────────────────────────────────
FROM python:3.12-slim AS runtime

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
    libxml2 \
    libxslt1.1 \
    && rm -rf /var/lib/apt/lists/*

# Copy installed packages
COPY --from=builder /install /usr/local

# Copy source
COPY jobhunter/ ./jobhunter/
COPY migrations/ ./migrations/
COPY alembic.ini .
COPY config.example.yaml .

# Data and log volumes (gitignored, mounted at runtime)
RUN mkdir -p data logs data/screenshots data/documents

# Playwright browsers (optional — comment out if not needed)
# RUN playwright install chromium --with-deps

EXPOSE 8000 8501

# Default: run the dashboard
CMD ["streamlit", "run", "jobhunter/dashboard/app.py", \
     "--server.port", "8501", "--server.address", "0.0.0.0"]
