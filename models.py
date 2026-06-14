"""
models.py
─────────
SQLAlchemy ORM models for BandBoard.

Changes from v1 (raw sqlite3):
 • Passwords stored as bcrypt hashes (was plain SHA-256)
 • Flask-Login mixin on User
 • Location fields on User + Audition (for geo-filtering)
 • MediaFile model for S3/GCS uploads
 • Proper foreign-key relationships with back-populates
 • server_default timestamps so SQLite & Postgres both work
"""

from datetime import datetime
from flask_login import UserMixin
import bcrypt

from extensions import db


# ── User ──────────────────────────────────────────────────────────────────────

class User(UserMixin, db.Model):
    __tablename__ = "users"

    id         = db.Column(db.Integer, primary_key=True)
    username   = db.Column(db.String(80), unique=True, nullable=False)
    email      = db.Column(db.String(120), unique=True, nullable=True)
    _password  = db.Column("password", db.String(128), nullable=False)
    role       = db.Column(db.String(10), nullable=False)   # 'band' | 'musician'
    bio        = db.Column(db.Text, default="")

    # Geolocation (optional – populated via geocoding API)
    city       = db.Column(db.String(100), default="")
    country    = db.Column(db.String(100), default="")
    lat        = db.Column(db.Float, nullable=True)
    lng        = db.Column(db.Float, nullable=True)

    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    # Relationships
    auditions    = db.relationship("Audition",    back_populates="band",     lazy="dynamic")
    applications = db.relationship("Application", back_populates="musician", lazy="dynamic")

    # ── Password helpers (bcrypt) ──────────────────────────────────────────
    @property
    def password(self):
        raise AttributeError("password is write-only")

    @password.setter
    def password(self, raw: str):
        self._password = bcrypt.hashpw(
            raw.encode(), bcrypt.gensalt(rounds=12)
        ).decode()

    def check_password(self, raw: str) -> bool:
        return bcrypt.checkpw(raw.encode(), self._password.encode())

    def __repr__(self):
        return f"<User {self.username} ({self.role})>"


# ── Audition ──────────────────────────────────────────────────────────────────

class Audition(db.Model):
    __tablename__ = "auditions"

    id           = db.Column(db.Integer, primary_key=True)
    band_id      = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False)

    title        = db.Column(db.String(200), nullable=False)
    instrument   = db.Column(db.String(80),  nullable=False)
    description  = db.Column(db.Text,        nullable=False)
    piece_name   = db.Column(db.String(200), nullable=False)
    piece_details= db.Column(db.Text,        nullable=False)
    genre        = db.Column(db.String(80),  default="")
    deadline     = db.Column(db.Date,        nullable=True)
    status       = db.Column(db.String(10),  default="open")   # open | closed

    # Geolocation (inherited from band at post time for fast filtering)
    city         = db.Column(db.String(100), default="")
    country      = db.Column(db.String(100), default="")
    lat          = db.Column(db.Float, nullable=True)
    lng          = db.Column(db.Float, nullable=True)

    created_at   = db.Column(db.DateTime, default=datetime.utcnow)

    # Relationships
    band         = db.relationship("User",        back_populates="auditions")
    applications = db.relationship("Application", back_populates="audition",
                                   cascade="all, delete-orphan", lazy="dynamic")
    media_files  = db.relationship("MediaFile",   back_populates="audition",
                                   cascade="all, delete-orphan")

    @property
    def app_count(self):
        return self.applications.count()

    def to_search_dict(self):
        """Serialise for Elasticsearch indexing."""
        return {
            "id":           self.id,
            "title":        self.title,
            "instrument":   self.instrument,
            "description":  self.description,
            "piece_name":   self.piece_name,
            "genre":        self.genre or "",
            "band_name":    self.band.username if self.band else "",
            "city":         self.city or "",
            "country":      self.country or "",
            "status":       self.status,
            "created_at":   self.created_at.isoformat() if self.created_at else "",
        }

    def __repr__(self):
        return f"<Audition {self.id}: {self.title}>"


# ── Application ───────────────────────────────────────────────────────────────

class Application(db.Model):
    __tablename__ = "applications"

    id          = db.Column(db.Integer, primary_key=True)
    audition_id = db.Column(db.Integer, db.ForeignKey("auditions.id"), nullable=False)
    musician_id = db.Column(db.Integer, db.ForeignKey("users.id"),    nullable=False)

    message     = db.Column(db.Text,        nullable=False)
    video_link  = db.Column(db.String(500), default="")
    status      = db.Column(db.String(10),  default="pending")  # pending|accepted|rejected

    created_at  = db.Column(db.DateTime, default=datetime.utcnow)

    # Relationships
    audition = db.relationship("Audition", back_populates="applications")
    musician = db.relationship("User",     back_populates="applications")
    media_files = db.relationship("MediaFile", back_populates="application",
                                  cascade="all, delete-orphan")

    __table_args__ = (
        db.UniqueConstraint("audition_id", "musician_id", name="uq_one_application"),
    )

    def __repr__(self):
        return f"<Application {self.id} status={self.status}>"


# ── MediaFile ─────────────────────────────────────────────────────────────────

class MediaFile(db.Model):
    """
    Tracks files uploaded to cloud storage (S3/GCS).
    Can be attached to an Audition (e.g. sheet music PDF) or an Application
    (e.g. musician's demo reel).
    """
    __tablename__ = "media_files"

    id             = db.Column(db.Integer, primary_key=True)
    audition_id    = db.Column(db.Integer, db.ForeignKey("auditions.id"),    nullable=True)
    application_id = db.Column(db.Integer, db.ForeignKey("applications.id"), nullable=True)
    uploader_id    = db.Column(db.Integer, db.ForeignKey("users.id"),        nullable=False)

    filename       = db.Column(db.String(300), nullable=False)   # original filename
    storage_key    = db.Column(db.String(500), nullable=False)   # S3/GCS object key
    storage_url    = db.Column(db.String(500), nullable=False)   # public / signed URL
    mime_type      = db.Column(db.String(100), default="application/octet-stream")
    file_size      = db.Column(db.Integer, default=0)            # bytes
    duration_secs  = db.Column(db.Integer, nullable=True)        # for video/audio

    created_at     = db.Column(db.DateTime, default=datetime.utcnow)

    # Relationships
    audition    = db.relationship("Audition",    back_populates="media_files")
    application = db.relationship("Application", back_populates="media_files")
    uploader    = db.relationship("User")

    @property
    def size_human(self):
        """Human-readable file size (e.g. '3.2 MB')."""
        if self.file_size < 1024:
            return f"{self.file_size} B"
        elif self.file_size < 1024 ** 2:
            return f"{self.file_size/1024:.1f} KB"
        elif self.file_size < 1024 ** 3:
            return f"{self.file_size/1024**2:.1f} MB"
        return f"{self.file_size/1024**3:.1f} GB"

    def __repr__(self):
        return f"<MediaFile {self.filename} ({self.size_human})>"
