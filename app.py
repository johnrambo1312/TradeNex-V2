import os
from datetime import datetime, timezone, timedelta
from functools import wraps

from flask import Flask, render_template, request, redirect, url_for, session, flash, jsonify
from flask_wtf.csrf import CSRFProtect
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address
from dotenv import load_dotenv
from werkzeug.middleware.proxy_fix import ProxyFix

from models import db, User, AdminUser, TradeEntry, UserLiveBalance

load_dotenv()

app = Flask(__name__)
app.wsgi_app = ProxyFix(app.wsgi_app, x_proto=1, x_host=1, x_port=1)

app.config["SECRET_KEY"] = os.environ.get("SECRET_KEY", "ubah-rahsia-ini-dalam-env")
app.config["PERMANENT_SESSION_LIFETIME"] = timedelta(minutes=30)
app.config["SESSION_REFRESH_EACH_REQUEST"] = True
app.config["SESSION_COOKIE_HTTPONLY"] = True
app.config["SESSION_COOKIE_SAMESITE"] = "Lax"
app.config["SESSION_COOKIE_SECURE"] = os.environ.get("FLASK_DEBUG", "0") != "1"

csrf = CSRFProtect(app)
limiter = Limiter(app=app, key_func=get_remote_address, default_limits=[])

# Railway bagi DATABASE_URL untuk Postgres; kalau tiada, fallback ke SQLite local (untuk dev/demo)
db_url = os.environ.get("DATABASE_URL", "sqlite:///tradenex_demo.db")
if db_url.startswith("postgres://"):
    # SQLAlchemy versi baru perlukan "postgresql://" bukan "postgres://"
    db_url = db_url.replace("postgres://", "postgresql://", 1)
app.config["SQLALCHEMY_DATABASE_URI"] = db_url
app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False

db.init_app(app)

# MYT = UTC+8 — semua masa dalam DB disimpan sebagai naive MYT
MYT = timezone(timedelta(hours=8))

def now_myt():
    """Masa sekarang dalam MYT (betul untuk Railway yang run UTC)."""
    return datetime.now(MYT).replace(tzinfo=None)


def ensure_database_ready():
    """Pastikan jadual asas wujud bila app start di Railway/gunicorn."""
    try:
        db.create_all()

        if not AdminUser.query.filter_by(username="admin").first():
            default_admin = AdminUser(username="admin")
            default_admin.set_password(os.environ.get("DEFAULT_ADMIN_PASSWORD", "admin123"))
            db.session.add(default_admin)
            db.session.commit()
    except Exception as e:
        # Database tidak available semasa build — skip, akan run di startup nanti
        print(f"[Warning] Database initialization skipped during build: {type(e).__name__}")


with app.app_context():
    try:
        ensure_database_ready()
    except Exception:
        # Biarkan je kalau gagal semasa build, akan try lagi semasa runtime
        pass


# ---------------------------------------------------------------------------
# Helpers / Decorators
# ---------------------------------------------------------------------------

def login_required(f):
    @wraps(f)
    def wrapper(*args, **kwargs):
        if "user_id" not in session:
            return redirect(url_for("login_page"))
        return f(*args, **kwargs)
    return wrapper


def admin_required(f):
    @wraps(f)
    def wrapper(*args, **kwargs):
        if "admin_id" not in session:
            return redirect(url_for("admin_login_page"))
        return f(*args, **kwargs)
    return wrapper


def generate_nova_id():
    """Jana NOVA ID unik random format TN#### (0000-9999), cek tak duplicate."""
    import random
    max_attempts = 100
    for _ in range(max_attempts):
        rand_num = random.randint(0, 9999)
        nova_id = f"TN{rand_num:04d}"
        if not User.query.filter_by(nova_id=nova_id).first():
            return nova_id
    # Fallback (sangat jarang) — guna timestamp
    return f"TN{int(datetime.now().timestamp()) % 10000:04d}"


# ---------------------------------------------------------------------------
# Laman utama (index.html sedia ada)
# ---------------------------------------------------------------------------

_db_initialized = False

@app.before_request
def init_database_on_first_request():
    """Ensure database is ready on first request."""
    global _db_initialized
    if not _db_initialized:
        with app.app_context():
            try:
                ensure_database_ready()
                _db_initialized = True
            except Exception:
                pass  # Will retry on next request

@app.route("/")
def home():
    return render_template("index.html")


# ---------------------------------------------------------------------------
# USER: Login & Dashboard
# ---------------------------------------------------------------------------

@app.route("/login", methods=["GET"])
def login_page():
    return render_template("login.html")


@app.route("/api/login", methods=["POST"])
@limiter.limit("10 per minute")
def api_login():
    data = request.get_json(silent=True) or request.form
    nova_id = (data.get("nova_id") or "").strip()
    password = data.get("password") or ""

    user = User.query.filter_by(nova_id=nova_id).first()

    if not user or not user.check_password(password):
        return jsonify({"ok": False, "message": "ID atau kata laluan tidak sah."}), 401

    if not user.is_active:
        return jsonify({"ok": False, "message": "Akaun anda telah dinyahaktifkan. Sila hubungi admin."}), 403

    session["user_id"] = user.id
    return jsonify({"ok": True, "redirect": url_for("dashboard")})


@app.route("/logout")
def logout():
    session.pop("user_id", None)
    return redirect(url_for("login_page"))


@app.route("/dashboard")
@login_required
def dashboard():
    user = db.session.get(User, session["user_id"])
    if not user:
        session.pop("user_id", None)
        return redirect(url_for("login_page"))

    entries = TradeEntry.query.filter_by(user_id=user.id).order_by(TradeEntry.entry_date).all()

    def to_utc_ts(dt):
        # Semua datetime dalam DB adalah naive MYT (UTC+8).
        # Tukar ke UTC timestamp yang JS Date.now() faham:
        # MYT naive → label sebagai MYT → convert ke UTC → ambil timestamp
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=MYT)
        return int(dt.astimezone(timezone.utc).timestamp())

    has_entry = any((e.note or "").startswith("[Entry]") for e in entries)
    last_is_close = bool(entries and (entries[-1].note or "").startswith("[Close]"))
    # Chart boleh mula sebaik sahaja sequence Entry -> Close lengkap,
    # walaupun masa Close mungkin belum berlalu.
    session_ready = has_entry and last_is_close and len(entries) >= 2

    if session_ready:
        chart_labels = [e.entry_date.strftime("%H:%M") for e in entries]
        chart_values = [e.value for e in entries]
        chart_timestamps = [to_utc_ts(e.entry_date) for e in entries]
        show_chart = True
    else:
        # Chart hanya jalan bila sesi lengkap (Entry + Close).
        chart_labels = []
        chart_values = []
        chart_timestamps = []
        show_chart = False

    return render_template(
        "dashboard.html",
        user=user,
        chart_labels=chart_labels,
        chart_values=chart_values,
        chart_timestamps=chart_timestamps,
        show_chart=show_chart,
    )


@app.route("/api/user/snapshot_balance", methods=["POST"])
@csrf.exempt
@login_required
def api_user_snapshot_balance():
    """Simpan nilai candle semasa yang dipaparkan di dashboard user (tanpa kiraan backend)."""
    user = db.session.get(User, session["user_id"])
    if not user:
        return jsonify({"ok": False, "message": "User tidak ditemui."}), 404

    data = request.get_json(silent=True) or {}
    raw_value = data.get("value")

    try:
        value = round(float(raw_value), 2)
    except (TypeError, ValueError):
        return jsonify({"ok": False, "message": "Nilai tidak sah."}), 400

    if value < 0:
        value = 0.0

    snap = user.live_balance_snapshot
    if not snap:
        snap = UserLiveBalance(user_id=user.id, value=value)
        db.session.add(snap)
    else:
        snap.value = value

    db.session.commit()
    return jsonify({"ok": True})


# ---------------------------------------------------------------------------
# ADMIN: Login
# ---------------------------------------------------------------------------

@app.route("/admin/login", methods=["GET"])
def admin_login_page():
    return render_template("admin_login.html")


@app.route("/admin/api/login", methods=["POST"])
@limiter.limit("5 per minute")
def admin_api_login():
    data = request.get_json(silent=True) or request.form
    username = (data.get("username") or "").strip()
    password = data.get("password") or ""

    admin = AdminUser.query.filter_by(username=username).first()
    if not admin or not admin.check_password(password):
        return jsonify({"ok": False, "message": "Username atau password admin tidak sah."}), 401

    session["admin_id"] = admin.id
    return jsonify({"ok": True, "redirect": url_for("admin_dashboard")})


@app.route("/admin/logout")
def admin_logout():
    session.pop("admin_id", None)
    return redirect(url_for("admin_login_page"))


# ---------------------------------------------------------------------------
# ADMIN: Dashboard - senarai & urus user
# ---------------------------------------------------------------------------

@app.route("/admin")
@admin_required
def admin_dashboard():
    users = User.query.order_by(User.created_at.asc()).all()
    return render_template("admin_dashboard.html", users=users)


@app.route("/admin/user/create", methods=["POST"])
@admin_required
def admin_create_user():
    password = request.form.get("password", "")
    full_name = request.form.get("full_name", "").strip()
    balance = request.form.get("balance", "0") or "0"

    if not password or not full_name:
        flash("Sila isi Nama dan Password.", "error")
        return redirect(url_for("admin_dashboard"))

    try:
        balance_float = float(balance)
        if balance_float < 0:
            flash("Baki awal tidak boleh negatif.", "error")
            return redirect(url_for("admin_dashboard"))
    except ValueError:
        flash("Format baki tidak sah (mesti angka).", "error")
        return redirect(url_for("admin_dashboard"))

    nova_id = generate_nova_id()

    user = User(
        nova_id=nova_id,
        full_name=full_name,
        balance=balance_float,
    )
    user.set_password(password)
    db.session.add(user)
    db.session.commit()
    flash(f"User berjaya dicipta — NOVA ID: {nova_id}", "success")
    return redirect(url_for("admin_dashboard"))


@app.route("/admin/user/<int:user_id>/update", methods=["POST"])
@admin_required
def admin_update_user(user_id):
    user = db.get_or_404(User, user_id)

    full_name = request.form.get("full_name", "").strip()
    balance = request.form.get("balance")
    new_password = request.form.get("new_password", "").strip()
    is_active = request.form.get("is_active") == "on"

    if full_name:
        user.full_name = full_name
    
    # Validasi balance (float) kalau ada input
    if balance not in (None, ""):
        try:
            balance_float = float(balance)
            if balance_float < 0:
                flash("Baki tidak boleh negatif.", "error")
                return redirect(url_for("admin_dashboard"))
            user.balance = balance_float
        except ValueError:
            flash("Format baki tidak sah (mesti angka).", "error")
            return redirect(url_for("admin_dashboard"))
    
    if new_password:
        user.set_password(new_password)
    user.is_active = is_active

    db.session.commit()
    flash(f"Akaun '{user.nova_id}' berjaya dikemaskini.", "success")
    return redirect(url_for("admin_dashboard"))


@app.route("/admin/user/<int:user_id>/delete", methods=["POST"])
@admin_required
def admin_delete_user(user_id):
    user = db.get_or_404(User, user_id)
    db.session.delete(user)
    db.session.commit()
    flash(f"Akaun '{user.nova_id}' telah dipadam.", "success")
    return redirect(url_for("admin_dashboard"))


@app.route("/admin/user/<int:user_id>/add_entry", methods=["POST"])
@admin_required
def admin_add_entry(user_id):
    user = db.get_or_404(User, user_id)
    value = request.form.get("value")
    note = request.form.get("note", "").strip()
    entry_time_str = request.form.get("entry_time", "").strip()  # format: YYYY-MM-DDTHH:MM

    entry_type = request.form.get("entry_type", "entry").strip()  # "entry" atau "close"

    if not value:
        flash("Sila masukkan nilai untuk entry graf.", "error")
        return redirect(url_for("admin_dashboard"))

    # Validasi & parse value (float)
    try:
        value_float = float(value)
        if value_float < 0:
            flash("Nilai entry tidak boleh negatif.", "error")
            return redirect(url_for("admin_dashboard"))
    except ValueError:
        flash("Format nilai tidak sah (mesti angka).", "error")
        return redirect(url_for("admin_dashboard"))

    # Parse entry_time (MYT naive datetime)
    if entry_time_str:
        try:
            entry_date = datetime.strptime(entry_time_str, "%Y-%m-%dT%H:%M")
            # Assume input sudah dalam MYT (Railway safe: DB always MYT naive)
        except ValueError:
            flash("Format waktu tidak sah. Guna format: YYYY-MM-DDTHH:MM", "error")
            return redirect(url_for("admin_dashboard"))
    else:
        # Kalau admin tak specify, auto guna MYT sekarang
        entry_date = now_myt()

    # Validasi: entry_date mesti tidak mundur dari entry terakhir
    entries = TradeEntry.query.filter_by(user_id=user.id).order_by(TradeEntry.entry_date.desc()).all()
    if entries:
        last_entry = entries[0]
        if entry_date < last_entry.entry_date:
            flash("⚠ Waktu entry mesti selepas entry terakhir! Masukkan waktu yang betul.", "error")
            return redirect(url_for("admin_dashboard"))

    # Close tidak dibenarkan jika belum ada sebarang Entry.
    if entry_type == "close":
        has_entry = any((e.note or "").startswith("[Entry]") for e in entries)
        if not has_entry:
            flash("Sila tambah sekurang-kurangnya satu Entry sebelum set Close.", "error")
            return redirect(url_for("admin_dashboard"))

    if entry_type == "close":
        auto_note = f"[Close] {note}" if note else "[Close]"
    else:
        auto_note = f"[Entry] {note}" if note else "[Entry]"

    entry = TradeEntry(
        user_id=user.id,
        value=value_float,
        note=auto_note,
        entry_date=entry_date,
    )
    db.session.add(entry)
    db.session.commit()

    label = "Close" if entry_type == "close" else "Entry"
    flash(f"{label} graf ditambah untuk '{user.nova_id}' — RM {value_float:,.2f}.", "success")
    return redirect(url_for("admin_dashboard"))


@app.route("/admin/entry/<int:entry_id>/delete", methods=["POST"])
@admin_required
def admin_delete_entry(entry_id):
    entry = db.get_or_404(TradeEntry, entry_id)
    db.session.delete(entry)
    db.session.commit()
    flash("Entry graf dipadam.", "success")
    return redirect(url_for("admin_dashboard"))


@app.route("/admin/user/<int:user_id>/clear_history", methods=["POST"])
@admin_required
def admin_clear_history(user_id):
    user = db.get_or_404(User, user_id)
    TradeEntry.query.filter_by(user_id=user.id).delete()
    db.session.commit()
    flash(f"History entry untuk '{user.nova_id}' berjaya dipadam.", "success")
    return redirect(url_for("admin_dashboard"))


@app.route("/admin/user/<int:user_id>/withdraw", methods=["POST"])
@admin_required
def admin_withdraw(user_id):
    user = db.get_or_404(User, user_id)

    if not user.is_trade_closed:
        flash("Withdraw hanya boleh dilakukan bila sesi trade Selesai.", "error")
        return redirect(url_for("admin_dashboard"))

    try:
        amount = round(float(request.form.get("withdraw_amount", 0)), 2)
    except (ValueError, TypeError):
        flash("Jumlah withdraw tidak sah.", "error")
        return redirect(url_for("admin_dashboard"))

    if amount <= 0:
        flash("Jumlah withdraw mesti lebih dari RM 0.", "error")
        return redirect(url_for("admin_dashboard"))

    current = user.withdraw_live_balance
    if amount > current:
        flash(f"Jumlah melebihi baki semasa (RM {current:,.2f}).", "error")
        return redirect(url_for("admin_dashboard"))

    new_balance = round(current - amount, 2)
    user.balance = new_balance

    # Auto buang semua history entry selepas withdraw
    TradeEntry.query.filter_by(user_id=user.id).delete()
    db.session.commit()

    flash(f"Withdraw RM {amount:,.2f} berjaya untuk '{user.nova_id}'. Baki baru: RM {new_balance:,.2f}.", "success")
    return redirect(url_for("admin_dashboard"))


# ---------------------------------------------------------------------------
# CLI command untuk setup pertama kali: buat tables + admin pertama
# Guna: flask init-db
# ---------------------------------------------------------------------------

@app.cli.command("init-db")
def init_db():
    """Cipta semua jadual DB dan satu akaun admin lalai jika belum wujud."""
    admin_exists = AdminUser.query.filter_by(username="admin").first()
    ensure_database_ready()

    if not admin_exists:
        print("Admin lalai dicipta -> username: admin | password: (lihat DEFAULT_ADMIN_PASSWORD di .env, default 'admin123')")
        print("PENTING: tukar password ini selepas log masuk pertama kali!")
    else:
        print("Admin 'admin' sudah wujud, skip.")

    print("Database sedia digunakan.")


if __name__ == "__main__":
    with app.app_context():
        db.create_all()
    app.run(debug=os.environ.get("FLASK_DEBUG", "0") == "1", host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))
