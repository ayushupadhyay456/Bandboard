"""
extensions.py
─────────────
Initialises all Flask extensions in one place so they can be imported
anywhere without causing circular-import issues.  Each extension is
created here (un-bound) and then registered on the app in create_app().
"""

from flask_sqlalchemy import SQLAlchemy
from flask_migrate import Migrate
from flask_login import LoginManager
from flask_socketio import SocketIO
import redis
import os

# ── SQLAlchemy ORM ────────────────────────────────────────────────────────────
db = SQLAlchemy()

# ── Schema migrations ─────────────────────────────────────────────────────────
migrate = Migrate()

# ── Session / auth management ─────────────────────────────────────────────────
login_manager = LoginManager()
login_manager.login_view = "auth.login"
login_manager.login_message = "Please log in to access this page."
login_manager.login_message_category = "error"

# ── WebSocket real-time layer ─────────────────────────────────────────────────
socketio = SocketIO(cors_allowed_origins="*", async_mode="eventlet")

# ── Redis client (caching + pub/sub) ─────────────────────────────────────────
def get_redis():
    """Return a Redis client connected to the cache DB (db=1)."""
    return redis.from_url(
        os.environ.get("REDIS_CACHE_URL", "redis://localhost:6379/1"),
        decode_responses=True,
    )
