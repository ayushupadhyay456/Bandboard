##############################################################################
# Dockerfile  –  BandBoard v2
# Multi-stage build: deps layer cached separately from source code
##############################################################################

FROM python:3.12-slim AS base

# System deps (for bcrypt, psycopg2)
RUN apt-get update && apt-get install -y --no-install-recommends \
    libpq-dev \
    gcc \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# ── Dependency layer (invalidated only when requirements.txt changes) ─────────
FROM base AS deps
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# ── Final image ───────────────────────────────────────────────────────────────
FROM deps AS final
COPY . .

# Non-root user for security
RUN useradd -m bandboard && chown -R bandboard /app
USER bandboard

EXPOSE 5000
CMD ["python", "app.py"]
