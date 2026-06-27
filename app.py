"""
app.py  –  BandBoard v2
────────────────────────
Enhanced architecture:

  ✓  Flask-SQLAlchemy ORM  (replaces raw sqlite3)
  ✓  Flask-Migrate         (schema migrations via Alembic)
  ✓  Flask-Login           (session management, @login_required)
  ✓  Flask-SocketIO        (real-time notifications over WebSocket)
  ✓  bcrypt                (secure password hashing)
  ✓  Redis cache           (browse/index pages, TTL-based invalidation)
  ✓  Celery + Redis        (async email notifications)
  ✓  Local file storage    (uploads saved to /app/uploads/)
  ✓  Geolocation fields    (city/country on users & auditions)
  ✓  Flask-Migrate CLI     (`flask db init / migrate / upgrade`)
"""

import os
from datetime import datetime

from dotenv import load_dotenv
from flask import (Flask, flash, redirect, render_template,
                   request, url_for, jsonify)
from flask_login import (current_user, login_required,
                          login_user, logout_user)
from flask_socketio import join_room

load_dotenv()

from extensions import db, login_manager, migrate, socketio
from models import Application, Audition, User
from utils.cache import (TTL_MEDIUM, TTL_LONG, TTL_SHORT,
                          cache_get, cache_set,
                          invalidate_audition_caches, invalidate_user_caches)


# ── Application factory ───────────────────────────────────────────────────────

def create_app(config_override=None):
    app = Flask(__name__)

    app.config["SECRET_KEY"]                  = os.environ.get("SECRET_KEY", "dev-only-secret")
    app.config["SQLALCHEMY_DATABASE_URI"]     = os.environ.get("DATABASE_URL", "sqlite:///bandboard.db")
    app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False
    app.config["MAX_CONTENT_LENGTH"]          = int(os.environ.get("MAX_UPLOAD_SIZE", 52_428_800))

    if config_override:
        app.config.update(config_override)

    db.init_app(app)
    migrate.init_app(app, db)
    login_manager.init_app(app)
    socketio.init_app(
        app,
        message_queue=os.environ.get("REDIS_URL", "redis://localhost:6379/0"),
    )

    from tasks.celery_app import make_celery
    app.celery = make_celery(app)

    @login_manager.user_loader
    def load_user(user_id):
        return db.session.get(User, int(user_id))

    _register_routes(app)
    _register_socket_events()

    return app


# ── Routes ────────────────────────────────────────────────────────────────────

def _register_routes(app):

    # ── Index ─────────────────────────────────────────────────────────────────
    @app.route("/")
    def index():
        CACHE_KEY = "auditions:index"
        auditions = cache_get(CACHE_KEY)
        if not auditions:
            rows = (
                db.session.query(Audition, User.username.label("band_name"))
                .join(User, Audition.band_id == User.id)
                .filter(Audition.status == "open")
                .order_by(Audition.created_at.desc())
                .limit(12).all()
            )
            auditions = rows
            cache_set(CACHE_KEY, [
                {**{c.key: getattr(a, c.key) for c in Audition.__table__.columns},
                 "band_name": bn, "created_at": str(a.created_at)}
                for a, bn in rows
            ], TTL_MEDIUM)
            auditions = rows
        else:
            class _Row:
                def __init__(self, d):
                    self.__dict__.update(d)
            auditions = [_Row(d) for d in auditions]

        return render_template("index.html", auditions=auditions)

    # ── Register ──────────────────────────────────────────────────────────────
    @app.route("/register", methods=["GET", "POST"])
    def register():
        if current_user.is_authenticated:
            return redirect(url_for("index"))
        if request.method == "POST":
            username = request.form["username"].strip()
            password = request.form["password"]
            role     = request.form["role"]
            email    = request.form.get("email", "").strip() or None
            if len(password) < 6:
                flash("Password must be at least 6 characters.", "error")
                return redirect(url_for("register"))
            user = User(username=username, role=role, email=email)
            user.password = password
            db.session.add(user)
            try:
                db.session.commit()
                flash("Account created! Please log in.", "success")
                return redirect(url_for("login"))
            except Exception:
                db.session.rollback()
                flash("Username or email already taken.", "error")
        return render_template("register.html")

    # ── Login ─────────────────────────────────────────────────────────────────
    @app.route("/login", methods=["GET", "POST"])
    def login():
        if current_user.is_authenticated:
            return redirect(url_for("index"))
        if request.method == "POST":
            username = request.form["username"].strip()
            password = request.form["password"]
            user = User.query.filter_by(username=username).first()
            if user and user.check_password(password):
                login_user(user, remember=request.form.get("remember") == "on")
                flash(f"Welcome back, {user.username}!", "success")
                return redirect(request.args.get("next") or url_for("index"))
            flash("Invalid credentials.", "error")
        return render_template("login.html")

    # ── Logout ────────────────────────────────────────────────────────────────
    @app.route("/logout")
    @login_required
    def logout():
        logout_user()
        return redirect(url_for("index"))

    # ── Browse auditions ──────────────────────────────────────────────────────
    @app.route("/auditions")
    def auditions():
        instrument = request.args.get("instrument", "")
        genre      = request.args.get("genre", "")
        q          = request.args.get("q", "")
        city       = request.args.get("city", "")
        page       = max(1, request.args.get("page", 1, type=int))
        PER_PAGE   = 20

        # SQL LIKE search (Elasticsearch removed)
        query = (
            db.session.query(Audition, User.username.label("band_name"))
            .join(User, Audition.band_id == User.id)
            .filter(Audition.status == "open")
        )
        if instrument: query = query.filter(Audition.instrument == instrument)
        if genre:      query = query.filter(Audition.genre == genre)
        if city:       query = query.filter(Audition.city.ilike(f"%{city}%"))
        if q:
            like = f"%{q}%"
            query = query.filter(
                db.or_(
                    Audition.title.ilike(like),
                    Audition.description.ilike(like),
                    Audition.piece_name.ilike(like),
                )
            )
        rows = query.order_by(Audition.created_at.desc()).offset((page - 1) * PER_PAGE).limit(PER_PAGE).all()
        return render_template("auditions.html", auditions=rows,
                               instrument=instrument, genre=genre, q=q,
                               city=city, page=page)

    # ── Audition detail ───────────────────────────────────────────────────────
    @app.route("/audition/<int:aid>")
    def audition_detail(aid):
        CACHE_KEY = f"auditions:detail:{aid}"
        aud = cache_get(CACHE_KEY)
        if not aud:
            aud = db.session.get(Audition, aid)
            if not aud:
                flash("Audition not found.", "error")
                return redirect(url_for("auditions"))
            cache_set(CACHE_KEY, aud, TTL_SHORT)
        apps = []
        if current_user.is_authenticated and current_user.role == "band" \
                and aud.band_id == current_user.id:
            apps = (
                db.session.query(Application, User.username.label("musician_name"))
                .join(User, Application.musician_id == User.id)
                .filter(Application.audition_id == aid).all()
            )
        user_applied = False
        if current_user.is_authenticated and current_user.role == "musician":
            user_applied = Application.query.filter_by(
                audition_id=aid, musician_id=current_user.id).first() is not None
        return render_template("audition_detail.html", audition=aud,
                               applications=apps, user_applied=user_applied)

    # ── Post audition ─────────────────────────────────────────────────────────
    @app.route("/post-audition", methods=["GET", "POST"])
    @login_required
    def post_audition():
        if current_user.role != "band":
            flash("Only bands can post auditions.", "error")
            return redirect(url_for("index"))
        if request.method == "POST":
            deadline_str = request.form.get("deadline", "").strip()
            deadline = None
            if deadline_str:
                try:
                    deadline = datetime.strptime(deadline_str, "%Y-%m-%d").date()
                except ValueError:
                    flash("Invalid deadline format.", "error")
                    return redirect(url_for("post_audition"))

            aud = Audition(
                band_id     = current_user.id,
                title       = request.form["title"].strip(),
                instrument  = request.form["instrument"].strip(),
                description = request.form["description"].strip(),
                piece_name  = request.form["piece_name"].strip(),
                piece_details = request.form["piece_details"].strip(),
                genre       = request.form.get("genre", "").strip(),
                deadline    = deadline,
                city        = current_user.city,
                country     = current_user.country,
                lat         = current_user.lat,
                lng         = current_user.lng,
            )
            db.session.add(aud)
            db.session.commit()

            f = request.files.get("sheet_music")
            if f and f.filename:
                import werkzeug.utils, pathlib
                upload_dir = pathlib.Path("uploads") / "auditions" / str(aud.id)
                upload_dir.mkdir(parents=True, exist_ok=True)
                safe_name = werkzeug.utils.secure_filename(f.filename)
                f.save(upload_dir / safe_name)

            invalidate_audition_caches(aud.id)
            invalidate_user_caches(current_user.id)
            flash("Audition posted!", "success")
            return redirect(url_for("audition_detail", aid=aud.id))
        return render_template("post_audition.html")

    # ── Apply to audition ─────────────────────────────────────────────────────
    @app.route("/audition/<int:aid>/apply", methods=["POST"])
    @login_required
    def apply(aid):
        if current_user.role != "musician":
            flash("Only musicians can apply.", "error")
            return redirect(url_for("login"))
        aud = db.session.get(Audition, aid)
        if not aud or aud.status != "open":
            flash("This audition is no longer accepting applications.", "error")
            return redirect(url_for("auditions"))
        if Application.query.filter_by(audition_id=aid,
                                        musician_id=current_user.id).first():
            flash("You already applied to this audition.", "error")
            return redirect(url_for("audition_detail", aid=aid))

        app_obj = Application(
            audition_id = aid,
            musician_id = current_user.id,
            message     = request.form["message"].strip(),
            video_link  = request.form.get("video_link", "").strip(),
        )
        db.session.add(app_obj)
        db.session.commit()

        f = request.files.get("demo_file")
        if f and f.filename:
            import werkzeug.utils, pathlib
            upload_dir = pathlib.Path("uploads") / "applications" / str(app_obj.id)
            upload_dir.mkdir(parents=True, exist_ok=True)
            safe_name = werkzeug.utils.secure_filename(f.filename)
            f.save(upload_dir / safe_name)
            app_obj.demo_file_path = str(upload_dir / safe_name)
            db.session.commit()

        if aud.band and aud.band.email:
            from tasks.celery_app import send_application_notification
            send_application_notification.delay(
                band_email=aud.band.email, band_name=aud.band.username,
                musician_name=current_user.username, audition_title=aud.title,
                audition_id=aid,
            )

        socketio.emit("new_application",
                      {"musician": current_user.username,
                       "audition": aud.title, "audition_id": aid},
                      room=f"band_{aud.band_id}")

        invalidate_audition_caches(aid)
        invalidate_user_caches(current_user.id)
        flash("Application submitted! Good luck 🎸", "success")
        return redirect(url_for("audition_detail", aid=aid))

    # ── Update application status ──────────────────────────────────────────────
    @app.route("/application/<int:app_id>/update", methods=["POST"])
    @login_required
    def update_application(app_id):
        app_obj = db.session.get(Application, app_id)
        if not app_obj or app_obj.audition.band_id != current_user.id:
            flash("Not authorized.", "error")
            return redirect(url_for("dashboard"))
        new_status = request.form["status"]
        if new_status not in ("pending", "accepted", "rejected"):
            flash("Invalid status.", "error")
            return redirect(url_for("audition_detail", aid=app_obj.audition_id))

        app_obj.status = new_status
        db.session.commit()

        musician = db.session.get(User, app_obj.musician_id)
        if musician and musician.email:
            from tasks.celery_app import send_status_update_notification
            send_status_update_notification.delay(
                musician_email=musician.email, musician_name=musician.username,
                band_name=current_user.username,
                audition_title=app_obj.audition.title,
                new_status=new_status, audition_id=app_obj.audition_id,
            )

        socketio.emit("application_status_update",
                      {"status": new_status,
                       "audition": app_obj.audition.title,
                       "audition_id": app_obj.audition_id},
                      room=f"musician_{musician.id}")

        invalidate_audition_caches(app_obj.audition_id)
        flash(f"Application marked as {new_status}.", "success")
        return redirect(url_for("audition_detail", aid=app_obj.audition_id))

    # ── Close audition ─────────────────────────────────────────────────────────
    @app.route("/audition/<int:aid>/close", methods=["POST"])
    @login_required
    def close_audition(aid):
        aud = db.session.get(Audition, aid)
        if aud and aud.band_id == current_user.id:
            aud.status = "closed"
            db.session.commit()
            invalidate_audition_caches(aid)
            invalidate_user_caches(current_user.id)
            flash("Audition closed.", "success")
        return redirect(url_for("dashboard"))

    # ── Dashboard ──────────────────────────────────────────────────────────────
    @app.route("/dashboard")
    @login_required
    def dashboard():
        if current_user.role == "band":
            raw = (
                db.session.query(Audition,
                                 db.func.count(Application.id).label("app_count"))
                .outerjoin(Application, Application.audition_id == Audition.id)
                .filter(Audition.band_id == current_user.id)
                .group_by(Audition.id)
                .order_by(Audition.created_at.desc()).all()
            )
            items = []
            for aud, count in raw:
                aud.app_count = count
                items.append(aud)
            return render_template("dashboard_band.html", auditions=items)
        else:
            raw = (
                db.session.query(Application,
                                 Audition.title, Audition.instrument,
                                 Audition.piece_name, User.username.label("band_name"))
                .join(Audition, Application.audition_id == Audition.id)
                .join(User, Audition.band_id == User.id)
                .filter(Application.musician_id == current_user.id)
                .order_by(Application.created_at.desc()).all()
            )
            items = []
            for app_obj, title, instrument, piece_name, band_name in raw:
                app_obj.title      = title
                app_obj.instrument = instrument
                app_obj.piece_name = piece_name
                app_obj.band_name  = band_name
                items.append(app_obj)
            return render_template("dashboard_musician.html", applications=items)

    # ── Location update API ────────────────────────────────────────────────────
    @app.route("/api/profile/location", methods=["POST"])
    @login_required
    def update_location():
        data = request.get_json(silent=True) or {}
        current_user.city    = data.get("city",    current_user.city)
        current_user.country = data.get("country", current_user.country)
        current_user.lat     = data.get("lat",     current_user.lat)
        current_user.lng     = data.get("lng",     current_user.lng)
        db.session.commit()
        if current_user.role == "band":
            for aud in current_user.auditions.filter_by(status="open"):
                aud.city    = current_user.city
                aud.country = current_user.country
                aud.lat     = current_user.lat
                aud.lng     = current_user.lng
            db.session.commit()
        invalidate_user_caches(current_user.id)
        return jsonify({"ok": True})


# ── SocketIO events ───────────────────────────────────────────────────────────

def _register_socket_events():

    @socketio.on("connect")
    def on_connect():
        if current_user.is_authenticated:
            join_room(f"{current_user.role}_{current_user.id}")

    @socketio.on("disconnect")
    def on_disconnect():
        pass


# ── Entry point ───────────────────────────────────────────────────────────────

app = create_app()

if __name__ == "__main__":
    with app.app_context():
        db.create_all()
    debug = os.environ.get("FLASK_ENV", "production") == "development"
    socketio.run(app, host="0.0.0.0", port=5000, debug=debug)