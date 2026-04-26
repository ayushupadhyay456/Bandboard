from flask import Flask, render_template, request, redirect, url_for, flash, session
from datetime import datetime
import sqlite3, os, hashlib

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", "bandboard-secret-dev-only")

# Support Docker volume path via env var, default to local file for dev
DB = os.environ.get("DATABASE_PATH", "bandboard.db")

# Ensure the data directory exists (needed when using Docker volumes)
os.makedirs(os.path.dirname(DB), exist_ok=True) if os.path.dirname(DB) else None

# ---------- DB helpers ----------

def get_db():
    conn = sqlite3.connect(DB)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    with get_db() as db:
        db.executescript("""
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT UNIQUE NOT NULL,
            password TEXT NOT NULL,
            role TEXT NOT NULL CHECK(role IN ('band','musician')),
            bio TEXT DEFAULT '',
            created_at TEXT DEFAULT (datetime('now'))
        );

        CREATE TABLE IF NOT EXISTS auditions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            band_id INTEGER NOT NULL,
            title TEXT NOT NULL,
            instrument TEXT NOT NULL,
            description TEXT NOT NULL,
            piece_name TEXT NOT NULL,
            piece_details TEXT NOT NULL,
            genre TEXT DEFAULT '',
            deadline TEXT,
            status TEXT DEFAULT 'open',
            created_at TEXT DEFAULT (datetime('now')),
            FOREIGN KEY(band_id) REFERENCES users(id)
        );

        CREATE TABLE IF NOT EXISTS applications (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            audition_id INTEGER NOT NULL,
            musician_id INTEGER NOT NULL,
            message TEXT NOT NULL,
            video_link TEXT DEFAULT '',
            status TEXT DEFAULT 'pending',
            created_at TEXT DEFAULT (datetime('now')),
            FOREIGN KEY(audition_id) REFERENCES auditions(id),
            FOREIGN KEY(musician_id) REFERENCES users(id)
        );
        """)

def hash_pw(pw):
    return hashlib.sha256(pw.encode()).hexdigest()

# ---------- Auth ----------

@app.route("/")
def index():
    db = get_db()
    auditions = db.execute("""
        SELECT a.*, u.username AS band_name
        FROM auditions a JOIN users u ON a.band_id = u.id
        WHERE a.status = 'open'
        ORDER BY a.created_at DESC LIMIT 12
    """).fetchall()
    return render_template("index.html", auditions=auditions)

@app.route("/register", methods=["GET","POST"])
def register():
    if request.method == "POST":
        username = request.form["username"].strip()
        password = request.form["password"]
        role = request.form["role"]
        if not username or not password:
            flash("All fields required.", "error")
            return redirect(url_for("register"))
        db = get_db()
        try:
            db.execute("INSERT INTO users(username,password,role) VALUES(?,?,?)",
                       (username, hash_pw(password), role))
            db.commit()
            flash("Account created! Please log in.", "success")
            return redirect(url_for("login"))
        except sqlite3.IntegrityError:
            flash("Username already taken.", "error")
    return render_template("register.html")

@app.route("/login", methods=["GET","POST"])
def login():
    if request.method == "POST":
        username = request.form["username"].strip()
        password = request.form["password"]
        db = get_db()
        user = db.execute("SELECT * FROM users WHERE username=? AND password=?",
                          (username, hash_pw(password))).fetchone()
        if user:
            session["user_id"] = user["id"]
            session["username"] = user["username"]
            session["role"] = user["role"]
            flash(f"Welcome back, {user['username']}!", "success")
            return redirect(url_for("index"))
        flash("Invalid credentials.", "error")
    return render_template("login.html")

@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("index"))

# ---------- Auditions ----------

@app.route("/auditions")
def auditions():
    db = get_db()
    instrument = request.args.get("instrument","")
    genre = request.args.get("genre","")
    q = request.args.get("q","")
    sql = """SELECT a.*, u.username AS band_name
             FROM auditions a JOIN users u ON a.band_id = u.id
             WHERE a.status='open'"""
    params = []
    if instrument:
        sql += " AND a.instrument=?"; params.append(instrument)
    if genre:
        sql += " AND a.genre=?"; params.append(genre)
    if q:
        sql += " AND (a.title LIKE ? OR a.piece_name LIKE ?)"; params += [f"%{q}%",f"%{q}%"]
    sql += " ORDER BY a.created_at DESC"
    rows = db.execute(sql, params).fetchall()
    instruments = db.execute("SELECT DISTINCT instrument FROM auditions").fetchall()
    genres = db.execute("SELECT DISTINCT genre FROM auditions WHERE genre!=''").fetchall()
    return render_template("auditions.html", auditions=rows,
                           instruments=instruments, genres=genres,
                           selected_inst=instrument, selected_genre=genre, q=q)

@app.route("/audition/<int:aid>")
def audition_detail(aid):
    db = get_db()
    aud = db.execute("""SELECT a.*, u.username AS band_name, u.bio AS band_bio
                        FROM auditions a JOIN users u ON a.band_id=u.id
                        WHERE a.id=?""", (aid,)).fetchone()
    if not aud:
        flash("Audition not found.", "error")
        return redirect(url_for("auditions"))
    apps = []
    user_applied = False
    if session.get("role") == "band" and session.get("user_id") == aud["band_id"]:
        apps = db.execute("""SELECT ap.*, u.username AS musician_name
                             FROM applications ap JOIN users u ON ap.musician_id=u.id
                             WHERE ap.audition_id=? ORDER BY ap.created_at DESC""", (aid,)).fetchall()
    elif session.get("role") == "musician":
        existing = db.execute("SELECT id FROM applications WHERE audition_id=? AND musician_id=?",
                              (aid, session["user_id"])).fetchone()
        user_applied = existing is not None
    return render_template("audition_detail.html", aud=aud, apps=apps, user_applied=user_applied)

@app.route("/post-audition", methods=["GET","POST"])
def post_audition():
    if session.get("role") != "band":
        flash("Only bands can post auditions.", "error")
        return redirect(url_for("login"))
    if request.method == "POST":
        db = get_db()
        db.execute("""INSERT INTO auditions(band_id,title,instrument,description,piece_name,piece_details,genre,deadline)
                      VALUES(?,?,?,?,?,?,?,?)""",
                   (session["user_id"],
                    request.form["title"].strip(),
                    request.form["instrument"].strip(),
                    request.form["description"].strip(),
                    request.form["piece_name"].strip(),
                    request.form["piece_details"].strip(),
                    request.form.get("genre","").strip(),
                    request.form.get("deadline","") or None))
        db.commit()
        flash("Audition posted successfully!", "success")
        return redirect(url_for("dashboard"))
    return render_template("post_audition.html")

@app.route("/apply/<int:aid>", methods=["POST"])
def apply(aid):
    if session.get("role") != "musician":
        flash("Only musicians can apply.", "error")
        return redirect(url_for("login"))
    db = get_db()
    existing = db.execute("SELECT id FROM applications WHERE audition_id=? AND musician_id=?",
                          (aid, session["user_id"])).fetchone()
    if existing:
        flash("You already applied to this audition.", "error")
        return redirect(url_for("audition_detail", aid=aid))
    db.execute("INSERT INTO applications(audition_id,musician_id,message,video_link) VALUES(?,?,?,?)",
               (aid, session["user_id"],
                request.form["message"].strip(),
                request.form.get("video_link","").strip()))
    db.commit()
    flash("Application submitted! Good luck 🎸", "success")
    return redirect(url_for("audition_detail", aid=aid))

@app.route("/application/<int:app_id>/update", methods=["POST"])
def update_application(app_id):
    db = get_db()
    app_row = db.execute("""SELECT ap.*, a.band_id FROM applications ap
                            JOIN auditions a ON ap.audition_id=a.id
                            WHERE ap.id=?""", (app_id,)).fetchone()
    if not app_row or app_row["band_id"] != session.get("user_id"):
        flash("Not authorized.", "error")
        return redirect(url_for("dashboard"))
    new_status = request.form["status"]
    db.execute("UPDATE applications SET status=? WHERE id=?", (new_status, app_id))
    db.commit()
    flash(f"Application marked as {new_status}.", "success")
    return redirect(url_for("audition_detail", aid=app_row["audition_id"]))

@app.route("/audition/<int:aid>/close", methods=["POST"])
def close_audition(aid):
    db = get_db()
    aud = db.execute("SELECT * FROM auditions WHERE id=?", (aid,)).fetchone()
    if aud and aud["band_id"] == session.get("user_id"):
        db.execute("UPDATE auditions SET status='closed' WHERE id=?", (aid,))
        db.commit()
        flash("Audition closed.", "success")
    return redirect(url_for("dashboard"))

@app.route("/dashboard")
def dashboard():
    if not session.get("user_id"):
        return redirect(url_for("login"))
    db = get_db()
    if session["role"] == "band":
        items = db.execute("""SELECT a.*, COUNT(ap.id) AS app_count
                              FROM auditions a LEFT JOIN applications ap ON a.id=ap.audition_id
                              WHERE a.band_id=? GROUP BY a.id ORDER BY a.created_at DESC""",
                           (session["user_id"],)).fetchall()
        return render_template("dashboard_band.html", auditions=items)
    else:
        items = db.execute("""SELECT ap.*, a.title, a.instrument, a.piece_name, u.username AS band_name
                              FROM applications ap
                              JOIN auditions a ON ap.audition_id=a.id
                              JOIN users u ON a.band_id=u.id
                              WHERE ap.musician_id=? ORDER BY ap.created_at DESC""",
                           (session["user_id"],)).fetchall()
        return render_template("dashboard_musician.html", applications=items)

if __name__ == "__main__":
    init_db()
    debug = os.environ.get("FLASK_ENV", "production") == "development"
    app.run(host="0.0.0.0", port=5000, debug=debug)
