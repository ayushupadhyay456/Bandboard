# ── Base image ────────────────────────────────────────────────────────────────
FROM python:3.12-slim

# Prevents .pyc files; forces stdout/stderr flush
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    FLASK_APP=app.py \
    FLASK_ENV=production \
    DATABASE_PATH=/app/data/bandboard.db

# Create non-root user for security
RUN addgroup --system appgroup && adduser --system --ingroup appgroup appuser

WORKDIR /app

# Install dependencies (cached layer — only re-runs if requirements change)
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy application source
COPY . .

# Create persistent data directory and fix ownership
RUN mkdir -p /app/data && chown -R appuser:appgroup /app

USER appuser

EXPOSE 5000

# Production: gunicorn with 2 workers
# The entrypoint also calls init_db() via a small wrapper
CMD ["sh", "-c", "python -c 'from app import init_db; init_db()' && gunicorn --bind 0.0.0.0:5000 --workers 2 --timeout 60 app:app"]
