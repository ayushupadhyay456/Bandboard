"""
tasks/celery_app.py
────────────────────
Celery application factory + all background task definitions.

Tasks
─────
  send_application_notification   – notify band when a musician applies
  send_status_update_notification – notify musician when status changes
  reindex_audition_task           – async ES reindex after audition is saved
  cleanup_expired_auditions        – periodic: auto-close past-deadline auditions

Running workers
───────────────
  # Development (single process, no prefork)
  celery -A tasks.celery_app worker --loglevel=info --pool=solo

  # Production
  celery -A tasks.celery_app worker --loglevel=info -c 4

  # Beat scheduler (periodic tasks)
  celery -A tasks.celery_app beat --loglevel=info

  # Monitoring UI (Flower)
  celery -A tasks.celery_app flower
"""

import logging
import os
import smtplib
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

from celery import Celery
from celery.schedules import crontab

log = logging.getLogger(__name__)

# ── Celery factory ────────────────────────────────────────────────────────────

def make_celery(app=None):
    broker  = os.environ.get("CELERY_BROKER_URL",     "redis://localhost:6379/0")
    backend = os.environ.get("CELERY_RESULT_BACKEND", "redis://localhost:6379/0")

    celery = Celery(
        "bandboard",
        broker=broker,
        backend=backend,
        include=["tasks.celery_app"],
    )

    celery.conf.update(
        task_serializer      = "json",
        result_serializer    = "json",
        accept_content       = ["json"],
        timezone             = "UTC",
        enable_utc           = True,
        task_track_started   = True,
        # Periodic task schedule
        beat_schedule        = {
            "cleanup-expired-auditions": {
                "task":     "tasks.celery_app.cleanup_expired_auditions",
                "schedule": crontab(hour=3, minute=0),   # daily at 03:00 UTC
            },
        },
    )

    if app is not None:
        # Flask app context for tasks that need DB access
        class ContextTask(celery.Task):
            def __call__(self, *args, **kwargs):
                with app.app_context():
                    return self.run(*args, **kwargs)
        celery.Task = ContextTask

    return celery


celery = make_celery()


# ── Email helper ──────────────────────────────────────────────────────────────

def _send_email(to: str, subject: str, html_body: str):
    """Send a plain SMTP email. Logs and swallows errors so tasks don't crash."""
    mail_server = os.environ.get("MAIL_SERVER")
    if not mail_server:
        log.info("MAIL_SERVER not configured – skipping email to %s", to)
        return

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"]    = os.environ.get("MAIL_DEFAULT_SENDER", "noreply@bandboard.io")
    msg["To"]      = to
    msg.attach(MIMEText(html_body, "html"))

    try:
        with smtplib.SMTP(mail_server, int(os.environ.get("MAIL_PORT", 587))) as srv:
            if os.environ.get("MAIL_USE_TLS", "true").lower() == "true":
                srv.starttls()
            username = os.environ.get("MAIL_USERNAME")
            password = os.environ.get("MAIL_PASSWORD")
            if username and password:
                srv.login(username, password)
            srv.sendmail(msg["From"], [to], msg.as_string())
        log.info("Email sent to %s: %s", to, subject)
    except Exception as exc:
        log.error("Failed to send email to %s: %s", to, exc)


# ── Tasks ─────────────────────────────────────────────────────────────────────

@celery.task(name="tasks.celery_app.send_application_notification",
             bind=True, max_retries=3, default_retry_delay=60)
def send_application_notification(self, band_email: str, band_name: str,
                                   musician_name: str, audition_title: str,
                                   audition_id: int):
    """
    Notify a band by email when a musician submits an application.
    Retried up to 3× on SMTP failure.
    """
    subject = f'New application for "{audition_title}" - BandBoard'
    html = f"""
    <div style="font-family:sans-serif;max-width:480px;margin:0 auto;">
      <h2 style="color:#e8ff47;background:#0a0a0b;padding:1rem;border-radius:6px;">
        🎸 New Application
      </h2>
      <p>Hi <strong>{band_name}</strong>,</p>
      <p>
        <strong>{musician_name}</strong> has applied to your audition
        <em>"{audition_title}"</em>.
      </p>
      <p style="margin-top:1.5rem;">
        <a href="http://localhost:5000/audition/{audition_id}"
           style="background:#e8ff47;color:#000;padding:0.7rem 1.5rem;
                  border-radius:4px;font-weight:700;text-decoration:none;">
          Review Application →
        </a>
      </p>
      <hr style="border:none;border-top:1px solid #2e2e38;margin:2rem 0;">
      <p style="font-size:0.8rem;color:#5a5a6a;">BandBoard · The Audition Board for Serious Musicians</p>
    </div>
    """
    try:
        _send_email(band_email, subject, html)
    except Exception as exc:
        raise self.retry(exc=exc)


@celery.task(name="tasks.celery_app.send_status_update_notification",
             bind=True, max_retries=3, default_retry_delay=60)
def send_status_update_notification(self, musician_email: str, musician_name: str,
                                     band_name: str, audition_title: str,
                                     new_status: str, audition_id: int):
    """
    Notify a musician when their application status changes.
    """
    emoji_map = {"accepted": "🎉", "rejected": "😔", "pending": "⏳"}
    emoji = emoji_map.get(new_status, "📬")

    subject = f'{emoji} Your application to "{audition_title}" - {new_status.upper()}'
    color   = {"accepted": "#2ecc71", "rejected": "#ff4757"}.get(new_status, "#f39c12")

    html = f"""
    <div style="font-family:sans-serif;max-width:480px;margin:0 auto;">
      <h2 style="color:{color};background:#0a0a0b;padding:1rem;border-radius:6px;">
        {emoji} Application {new_status.capitalize()}
      </h2>
      <p>Hi <strong>{musician_name}</strong>,</p>
      <p>
        <strong>{band_name}</strong> has updated your application for
        <em>"{audition_title}"</em>.
      </p>
      <p>
        Your application status is now:
        <strong style="color:{color};">{new_status.upper()}</strong>
      </p>
      <p style="margin-top:1.5rem;">
        <a href="http://localhost:5000/audition/{audition_id}"
           style="background:#e8ff47;color:#000;padding:0.7rem 1.5rem;
                  border-radius:4px;font-weight:700;text-decoration:none;">
          View Audition →
        </a>
      </p>
      <hr style="border:none;border-top:1px solid #2e2e38;margin:2rem 0;">
      <p style="font-size:0.8rem;color:#5a5a6a;">BandBoard · The Audition Board for Serious Musicians</p>
    </div>
    """
    try:
        _send_email(musician_email, subject, html)
    except Exception as exc:
        raise self.retry(exc=exc)


@celery.task(name="tasks.celery_app.reindex_audition_task")
def reindex_audition_task(audition_dict: dict):
    """Asynchronously index / reindex a single audition in Elasticsearch."""
    from utils.search import index_audition
    index_audition(audition_dict)


@celery.task(name="tasks.celery_app.cleanup_expired_auditions")
def cleanup_expired_auditions():
    """
    Periodic task (daily).  Auto-closes auditions whose deadline has passed.
    Requires the Flask app context to be bound (see make_celery).
    """
    from datetime import date
    try:
        from extensions import db
        from models import Audition

        today = date.today()
        expired = (
            Audition.query
            .filter(Audition.status == "open",
                    Audition.deadline != None,
                    Audition.deadline < today)
            .all()
        )
        for aud in expired:
            aud.status = "closed"
            log.info("Auto-closed expired audition id=%d ('%s')", aud.id, aud.title)

        db.session.commit()
        log.info("cleanup_expired_auditions: closed %d auditions", len(expired))
    except Exception as exc:
        log.error("cleanup_expired_auditions failed: %s", exc)
