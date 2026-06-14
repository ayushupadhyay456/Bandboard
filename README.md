# BandBoard v2

> The Audition Board for Serious Musicians — now with a production-grade backend stack.

---

## What's New in v2

| Feature | Implementation | File(s) |
|---|---|---|
| Secure password hashing | bcrypt (12 rounds, salted) | `models.py` |
| Session management | Flask-Login | `extensions.py`, `app.py` |
| ORM + migrations | Flask-SQLAlchemy + Flask-Migrate | `models.py`, `migrations/` |
| Redis caching | `redis-py`, TTL-based invalidation | `utils/cache.py` |
| Async email notifications | Celery + Redis broker | `tasks/celery_app.py` |
| Real-time push notifications | Flask-SocketIO + WebSocket | `extensions.py`, `base.html` |
| Full-text search | Elasticsearch (SQL LIKE fallback) | `utils/search.py` |
| Cloud file uploads | AWS S3 via boto3 | `utils/storage.py` |
| Geolocation filters | City/country fields + geocoding API | `models.py`, `/api/profile/location` |
| Production DB | PostgreSQL (SQLite for dev) | `docker-compose.yml` |
| Email field | Added to User model + register form | `models.py`, `register.html` |
| Pagination | 20 results/page on browse | `auditions.html` |

---

## Quick Start

### Development (SQLite, no Docker)

```bash
# 1. Clone and install
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

# 2. Configure
cp .env.example .env
# Edit .env — at minimum set SECRET_KEY

# 3. Run (auto-creates SQLite DB)
FLASK_ENV=development python app.py
```

Visit http://localhost:5000

> **Note:** Without Redis, ES, or S3 configured, the app degrades gracefully:
> caching is skipped, search falls back to SQL LIKE, file uploads are disabled,
> and email tasks are logged but not sent.

---

### Full Stack (Docker Compose)

```bash
# Start everything (web, celery, redis, postgres, elasticsearch)
docker compose up --build

# First run: apply DB migrations
docker compose exec web flask db upgrade

# Reindex Elasticsearch (after seeding data)
# Visit: http://localhost:5000/admin/reindex
```

| Service | URL |
|---|---|
| Web app | http://localhost:5000 |
| Flower (Celery UI) | http://localhost:5555 |
| Elasticsearch | http://localhost:9200 |

---

## Architecture Overview

```
┌─────────────────────────────────────────────────────────────┐
│                         Browser                             │
│    HTTP requests          WebSocket (Socket.IO)             │
└────────────┬─────────────────────┬───────────────────────────┘
             │                     │
    ┌────────▼─────────────────────▼────────┐
    │         Flask + Flask-SocketIO        │
    │         (eventlet async mode)         │
    └──┬──────────┬──────────┬──────────────┘
       │          │          │
  ┌────▼───┐  ┌───▼───┐  ┌──▼──────────┐
  │ SQLAlch│  │ Redis │  │ Elasticsearch│
  │  ORM   │  │ Cache │  │  (FTS index) │
  └────┬───┘  └───────┘  └─────────────┘
       │
  ┌────▼────────────────────────┐
  │  PostgreSQL / SQLite        │
  └─────────────────────────────┘

  Background jobs (Celery):
  ┌──────────────────────────────────────────┐
  │  Redis (broker) → Celery worker          │
  │  Tasks: send_email, reindex_es, cleanup  │
  └──────────────────────────────────────────┘

  File storage:
  ┌──────────────────────────────────────────┐
  │  Flask → boto3 → AWS S3 bucket           │
  │  MediaFile model tracks metadata         │
  └──────────────────────────────────────────┘
```

---

## Project Structure

```
bandboard/
├── app.py                  # Flask app factory + all routes
├── extensions.py           # Extension instances (db, login, socketio, redis)
├── models.py               # SQLAlchemy ORM models (User, Audition, Application, MediaFile)
├── requirements.txt        # Python dependencies
├── .env.example            # Environment variable template
├── Dockerfile
├── docker-compose.yml      # Full stack (web, celery, redis, postgres, ES)
│
├── tasks/
│   └── celery_app.py       # Celery factory + task definitions
│
├── utils/
│   ├── cache.py            # Redis cache helpers + invalidation
│   ├── search.py           # Elasticsearch integration + SQL fallback
│   └── storage.py          # AWS S3 upload/delete helpers
│
├── migrations/
│   └── env.py              # Alembic environment (Flask-Migrate)
│
└── templates/
    ├── base.html           # Layout + SocketIO client + notification bell
    ├── index.html
    ├── auditions.html      # + city filter, pagination
    ├── audition_detail.html # + file attachments, demo uploads
    ├── post_audition.html  # + file upload input
    ├── register.html       # + email field
    ├── login.html
    ├── dashboard_band.html  # + live indicator
    └── dashboard_musician.html # + live indicator
```

---

## Key Implementation Details

### 1. Security — bcrypt Password Hashing

`models.py` uses a Python `@property` setter so the hash is always applied:

```python
user = User(username="alice", role="musician")
user.password = "mysecretpassword"   # bcrypt hashes automatically
user.check_password("mysecretpassword")  # → True
```

### 2. Redis Caching

Cache keys follow a hierarchical convention. Cache is **always optional** — if
Redis is down, the app falls through to the database.

```python
from utils.cache import cache_get, cache_set, TTL_MEDIUM

data = cache_get("auditions:list")
if data is None:
    data = db_query()
    cache_set("auditions:list", data, TTL_MEDIUM)   # 120s TTL
```

After any write, call the appropriate invalidation helper:
```python
invalidate_audition_caches(audition_id)   # clears list + detail caches
invalidate_user_caches(user_id)           # clears dashboard caches
```

### 3. Celery Background Tasks

Tasks are defined in `tasks/celery_app.py`. They retry on SMTP failure (up to 3×):

```python
# Triggered in app.py after an application is submitted:
send_application_notification.delay(
    band_email="band@example.com",
    band_name="The Riffs",
    musician_name="Alice",
    audition_title="Lead Guitarist Needed",
    audition_id=42,
)
```

Run the worker:
```bash
celery -A tasks.celery_app worker --loglevel=info --pool=solo
```

### 4. Real-Time Notifications (SocketIO)

Each authenticated user joins a personal room on connect:
- Bands join `band_{id}` — receive `new_application` events
- Musicians join `musician_{id}` — receive `application_status_update` events

The notification bell in the navbar persists notifications in `localStorage`.

### 5. Elasticsearch

The app tries ES first, falls back to SQL on any failure:

```python
# utils/search.py
ids = search_auditions(q="guitar", city="London")
if ids is None:
    # ES unavailable — do SQL LIKE query
```

After saving a new audition, a Celery task reindexes it asynchronously.

Manual full reindex: `GET /admin/reindex`

### 6. File Uploads (S3)

`utils/storage.py` validates MIME type and size before uploading:

```python
result = upload_file(file_obj, filename, folder="auditions/42")
# returns: {storage_key, storage_url, mime_type, file_size}
```

A `MediaFile` row is created to track metadata (size, MIME, duration).

### 7. Database Migrations

```bash
# After changing models.py, generate a migration:
flask db migrate -m "add bio field to user"

# Apply it:
flask db upgrade

# Roll back:
flask db downgrade
```

### 8. Geolocation

Users can set their city/country via `POST /api/profile/location`. Bands'
open auditions are synced to the same location so musicians can filter locally.

---

## Environment Variables

See `.env.example` for all variables. The most important ones:

| Variable | Default | Description |
|---|---|---|
| `SECRET_KEY` | (required) | Flask session signing key |
| `DATABASE_URL` | `sqlite:///bandboard.db` | SQLAlchemy DB URI |
| `REDIS_URL` | `redis://localhost:6379/0` | Task broker |
| `REDIS_CACHE_URL` | `redis://localhost:6379/1` | Cache DB |
| `ELASTICSEARCH_URL` | `http://localhost:9200` | ES endpoint |
| `ELASTICSEARCH_ENABLED` | `true` | Set `false` to disable |
| `AWS_S3_BUCKET` | `bandboard-media` | S3 bucket name |
| `MAIL_SERVER` | (optional) | SMTP server for emails |

---

## Running Celery in Production

```bash
# Worker (4 concurrent processes)
celery -A tasks.celery_app worker --loglevel=info -c 4

# Beat scheduler (periodic tasks — run only ONE instance)
celery -A tasks.celery_app beat --loglevel=info

# Monitoring UI
celery -A tasks.celery_app flower
```

---

## Upgrading from v1

v1 used raw `sqlite3` and `hashlib.sha256` for passwords. To migrate:

1. Export existing users with a one-time script
2. Run `flask db upgrade` to create the new schema
3. Re-hash passwords: prompt users to reset on next login (recommended)
   or run a migration script that wraps old SHA-256 hashes with bcrypt

The DB schema is otherwise backward-compatible — all table names are the same.
