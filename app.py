import calendar
import math
import os
import re
import secrets
from datetime import datetime, timezone, timedelta
from functools import wraps

from flask import Flask, render_template, request, redirect, url_for, session, flash, jsonify
from flask_wtf.csrf import CSRFProtect
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address
from dotenv import load_dotenv
from sqlalchemy import inspect as sqlalchemy_inspect, text, update
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from werkzeug.middleware.proxy_fix import ProxyFix

from models import (
    db,
    User,
    AdminUser,
    RegistrationRequest,
    TradeEntry,
    UserLiveBalance,
    WithdrawalRequest,
    ReferralCommission,
)

load_dotenv()

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
app = Flask(
    __name__,
    template_folder=os.path.join(BASE_DIR, "templates"),
    static_folder=os.path.join(BASE_DIR, "static"),
)
app.wsgi_app = ProxyFix(app.wsgi_app, x_proto=1, x_host=1, x_port=1)

APP_ENV = os.environ.get("APP_ENV", "").strip().lower()
IS_PRODUCTION = APP_ENV == "production" or bool(os.environ.get("RENDER"))
secret_key = os.environ.get("SECRET_KEY", "").strip()
if IS_PRODUCTION and not secret_key:
    raise RuntimeError("SECRET_KEY mesti ditetapkan untuk production.")
app.config["SECRET_KEY"] = secret_key or "ubah-rahsia-ini-dalam-env"
app.config["PERMANENT_SESSION_LIFETIME"] = timedelta(minutes=30)
app.config["SESSION_REFRESH_EACH_REQUEST"] = True
app.config["SESSION_COOKIE_HTTPONLY"] = True
app.config["SESSION_COOKIE_SAMESITE"] = "Lax"
app.config["SESSION_COOKIE_SECURE"] = IS_PRODUCTION or os.environ.get("FLASK_DEBUG", "0") != "1"

csrf = CSRFProtect(app)
limiter = Limiter(app=app, key_func=get_remote_address, default_limits=[])

# SQLite hanya digunakan untuk development setempat.
db_url = (os.environ.get("DATABASE_URL") or "").strip()
if not db_url:
    if IS_PRODUCTION:
        raise RuntimeError("DATABASE_URL PostgreSQL mesti ditetapkan untuk production.")
    db_url = "sqlite:///tradenex_demo.db"
if db_url.startswith("postgres://"):
    db_url = db_url.replace("postgres://", "postgresql://", 1)
if IS_PRODUCTION and not db_url.startswith(("postgresql://", "postgresql+")):
    raise RuntimeError("DATABASE_URL production mesti menggunakan PostgreSQL, bukan SQLite.")
app.config["SQLALCHEMY_DATABASE_URI"] = db_url
app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False
db.init_app(app)

@app.context_processor
def inject_contact_settings():
    whatsapp_number = "".join(
        character for character in os.environ.get("WHATSAPP_NUMBER", "60312345678")
        if character.isdigit()
    )
    return {"whatsapp_number": whatsapp_number}

# MYT = UTC+8 — semua masa dalam DB disimpan sebagai naive MYT
MYT = timezone(timedelta(hours=8))

REGISTRATION_PLANS = {
    "basic": ("Basic", 488),
    "advance": ("Advance", 1888),
    "gold": ("Gold", 3888),
    "platinum": ("Platinum", 6888),
}


def now_myt():
    """Masa sekarang dalam MYT (betul untuk Railway yang run UTC)."""
    return datetime.now(MYT).replace(tzinfo=None)


def registration_for_user(user):
    return user.registration_request


def next_referral_commission_payable_at(now=None):
    now = now or now_myt()

    def payout_date(year, month):
        return datetime(year, month, min(30, calendar.monthrange(year, month)[1]), 0, 5)

    payable_at = payout_date(now.year, now.month)
    if now < payable_at:
        return payable_at

    next_month = 1 if now.month == 12 else now.month + 1
    next_year = now.year + 1 if now.month == 12 else now.year
    return payout_date(next_year, next_month)


def schedule_referral_commissions(user, now=None):
    if not user.referred_by:
        return

    payable_at = next_referral_commission_payable_at(now)
    beneficiaries = (
        (1, user.referred_by, 30.0),
        (2, user.referred_by.referred_by, 15.0),
    )
    scheduled_beneficiary_ids = set()
    for level, beneficiary, amount in beneficiaries:
        if not beneficiary or beneficiary.id in scheduled_beneficiary_ids:
            continue
        scheduled_beneficiary_ids.add(beneficiary.id)
        db.session.add(ReferralCommission(
            beneficiary=beneficiary,
            referred_user=user,
            level=level,
            amount=amount,
            payable_at=payable_at,
        ))


def process_due_referral_commissions(now=None):
    now = now or now_myt()
    commissions = ReferralCommission.query.filter(
        ReferralCommission.status == "pending",
        ReferralCommission.payable_at <= now,
    ).with_for_update().all()
    paid_count = 0
    for commission in commissions:
        beneficiary = db.session.get(User, commission.beneficiary_user_id)
        referred_user = db.session.get(User, commission.referred_user_id)
        if not beneficiary or not beneficiary.is_active or not referred_user or not referred_user.is_active:
            continue
        result = db.session.execute(
            update(ReferralCommission)
            .where(
                ReferralCommission.id == commission.id,
                ReferralCommission.status == "pending",
                ReferralCommission.payable_at <= now,
            )
            .values(status="paid", paid_at=now)
            .execution_options(synchronize_session=False)
        )
        paid_count += result.rowcount

    if paid_count:
        db.session.commit()
    else:
        db.session.rollback()
    return paid_count


def ensure_database_ready():
    """Pastikan jadual dan akaun admin tersedia sebelum aplikasi menerima trafik."""
    db.create_all()
    ensure_registration_request_schema()
    ensure_user_schema()
    ensure_withdrawal_request_schema()

    if not AdminUser.query.filter_by(username="admin").first():
        admin_password = os.environ.get("DEFAULT_ADMIN_PASSWORD", "").strip()
        if not admin_password:
            if IS_PRODUCTION:
                raise RuntimeError(
                    "Tetapkan DEFAULT_ADMIN_PASSWORD untuk mencipta admin pertama di production."
                )
            admin_password = "admin123"
        elif IS_PRODUCTION and len(admin_password) < 12:
            raise RuntimeError(
                "DEFAULT_ADMIN_PASSWORD mesti sekurang-kurangnya 12 aksara untuk admin pertama di production."
            )
        default_admin = AdminUser(username="admin")
        default_admin.set_password(admin_password)
        db.session.add(default_admin)
        db.session.commit()


def ensure_user_schema():
    """Tambah medan baki Wallet pada pangkalan data user sedia ada."""
    inspector = sqlalchemy_inspect(db.engine)
    if "users" not in inspector.get_table_names():
        return

    existing_columns = {column["name"] for column in inspector.get_columns("users")}
    missing_columns = {
        "withdrawn_total": "FLOAT NOT NULL DEFAULT 0",
        "realized_profit_loss": "FLOAT NOT NULL DEFAULT 0",
        "is_matured": "BOOLEAN NOT NULL DEFAULT FALSE",
        "referral_code": "VARCHAR(9)",
        "referred_by_id": "INTEGER REFERENCES users(id) ON DELETE SET NULL",
        "registration_request_id": (
            "INTEGER REFERENCES registration_requests(id) ON DELETE RESTRICT"
        ),
    }
    with db.engine.begin() as connection:
        for column_name, column_definition in missing_columns.items():
            if column_name not in existing_columns:
                connection.exec_driver_sql(
                    f"ALTER TABLE users ADD COLUMN {column_name} {column_definition}"
                )
        referral_codes = {
            row[0]
            for row in connection.exec_driver_sql(
                "SELECT referral_code FROM users WHERE referral_code IS NOT NULL"
            ).fetchall()
        }
        rows_without_referral_code = connection.exec_driver_sql(
            "SELECT id FROM users WHERE referral_code IS NULL OR referral_code = ''"
        ).fetchall()
        for row in rows_without_referral_code:
            for attempt in range(100):
                referral_code = f"E-{secrets.randbelow(10_000_000):07d}"
                if referral_code not in referral_codes:
                    referral_codes.add(referral_code)
                    break
            else:
                raise RuntimeError(
                    "Referral code unik tidak dapat dijana untuk akaun sedia ada."
                )
            connection.execute(
                text("UPDATE users SET referral_code = :code WHERE id = :id"),
                {"code": referral_code, "id": row[0]},
            )
        connection.exec_driver_sql(
            "CREATE UNIQUE INDEX IF NOT EXISTS uq_users_registration_request_id "
            "ON users (registration_request_id)"
        )
        connection.exec_driver_sql(
            "CREATE UNIQUE INDEX IF NOT EXISTS uq_users_referral_code "
            "ON users (referral_code)"
        )


def ensure_withdrawal_request_schema():
    """Tambah snapshot maklumat bank pada permohonan pengeluaran sedia ada."""
    inspector = sqlalchemy_inspect(db.engine)
    if "withdrawal_requests" not in inspector.get_table_names():
        return

    existing_columns = {
        column["name"] for column in inspector.get_columns("withdrawal_requests")
    }
    missing_columns = {
        "account_holder_name": "VARCHAR(120)",
        "bank_name": "VARCHAR(100)",
        "bank_account_number": "VARCHAR(34)",
        "payment_reference": "VARCHAR(120)",
        "registration_request_id": (
            "INTEGER REFERENCES registration_requests(id) ON DELETE RESTRICT"
        ),
    }
    with db.engine.begin() as connection:
        for column_name, column_definition in missing_columns.items():
            if column_name not in existing_columns:
                connection.exec_driver_sql(
                    f"ALTER TABLE withdrawal_requests ADD COLUMN {column_name} {column_definition}"
                )


def ensure_registration_request_schema():
    """Tambah medan permohonan baharu ke pangkalan data sedia ada dengan selamat."""
    inspector = sqlalchemy_inspect(db.engine)
    if "registration_requests" not in inspector.get_table_names():
        return

    existing_columns = {column["name"] for column in inspector.get_columns("registration_requests")}
    with db.engine.begin() as connection:
        if "request_code" not in existing_columns:
            connection.exec_driver_sql(
                "ALTER TABLE registration_requests ADD COLUMN request_code VARCHAR(16)"
            )
        if "plan_name" not in existing_columns:
            connection.exec_driver_sql(
                "ALTER TABLE registration_requests ADD COLUMN plan_name VARCHAR(20) NOT NULL DEFAULT 'basic'"
            )
        if "plan_price" not in existing_columns:
            connection.exec_driver_sql(
                "ALTER TABLE registration_requests ADD COLUMN plan_price INTEGER NOT NULL DEFAULT 488"
            )
        if "referral_code" not in existing_columns:
            connection.exec_driver_sql(
                "ALTER TABLE registration_requests ADD COLUMN referral_code VARCHAR(9)"
            )

        rows_without_code = connection.exec_driver_sql(
            "SELECT id FROM registration_requests WHERE request_code IS NULL"
        ).fetchall()
        for row in rows_without_code:
            request_code = f"TN-{secrets.token_hex(5).upper()}"
            connection.execute(
                text("UPDATE registration_requests SET request_code = :code WHERE id = :id"),
                {"code": request_code, "id": row[0]},
            )

        connection.exec_driver_sql(
            "CREATE UNIQUE INDEX IF NOT EXISTS uq_registration_requests_request_code "
            "ON registration_requests (request_code)"
        )


with app.app_context():
    ensure_database_ready()


# ---------------------------------------------------------------------------
# Helpers / Decorators
# ---------------------------------------------------------------------------

def login_required(f):
    @wraps(f)
    def wrapper(*args, **kwargs):
        user_id = session.get("user_id")
        if user_id is None:
            return redirect(url_for("login_page"))
        user = db.session.get(User, user_id)
        if not user or not user.is_active:
            session.pop("user_id", None)
            flash("Akaun anda telah dinyahaktifkan. Sila hubungi admin.", "error")
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


def generate_referral_code():
    """Jana referral code unik dengan format E- diikuti tujuh digit."""
    for _ in range(100):
        referral_code = f"E-{secrets.randbelow(10_000_000):07d}"
        if not User.query.filter_by(referral_code=referral_code).first():
            return referral_code
    raise RuntimeError("Referral code unik tidak dapat dijana selepas 100 percubaan.")


# ---------------------------------------------------------------------------
# Laman utama (index.html sedia ada)
# ---------------------------------------------------------------------------

@app.route("/")
def home():
    return render_template("index.html")


# ---------------------------------------------------------------------------
# USER: Login & Dashboard
# ---------------------------------------------------------------------------

@app.route("/login", methods=["GET"])
def login_page():
    return render_template("login.html")


@app.route("/api/registration", methods=["POST"])
@limiter.limit("10 per minute")
def api_registration():
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        return jsonify({"ok": False, "message": "Format permohonan tidak sah."}), 400

    fields = {
        "full_name": data.get("full_name"),
        "email": data.get("email"),
        "phone": data.get("phone"),
        "bank_name": data.get("bank_name"),
        "bank_account_number": data.get("bank_account_number"),
        "plan": data.get("plan"),
    }
    if not all(isinstance(value, str) for value in fields.values()):
        return jsonify({"ok": False, "message": "Sila lengkapkan semua maklumat dengan format yang betul."}), 400
    referral_code = data.get("referral_code", "")
    if not isinstance(referral_code, str):
        return jsonify({"ok": False, "message": "Kod referral tidak sah."}), 400
    referral_code = referral_code.strip().upper()
    if referral_code and not re.fullmatch(r"E-\d{7}", referral_code):
        return jsonify({"ok": False, "message": "Kod referral mesti menggunakan format E- diikuti 7 digit."}), 400
    if referral_code:
        if not User.query.filter_by(referral_code=referral_code, is_active=True).first():
            return jsonify({"ok": False, "message": "Kod referral tidak dijumpai atau tidak aktif."}), 400

    full_name = fields["full_name"].strip()
    email = fields["email"].strip().lower()
    phone = fields["phone"].strip()
    bank_name = fields["bank_name"].strip()
    bank_account_number = fields["bank_account_number"].strip()
    plan = fields["plan"].strip()
    phone_digits = re.sub(r"\D", "", phone)
    account_compact = re.sub(r"[\s-]", "", bank_account_number)

    if not all((full_name, email, phone, bank_name, bank_account_number)):
        return jsonify({"ok": False, "message": "Sila lengkapkan semua maklumat."}), 400
    if data.get("consent") is not True:
        return jsonify({"ok": False, "message": "Persetujuan penyimpanan maklumat diperlukan."}), 400
    if len(full_name) > 120 or len(email) > 254 or not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", email):
        return jsonify({"ok": False, "message": "Nama atau alamat e-mel tidak sah."}), 400
    if len(phone) > 30 or not 7 <= len(phone_digits) <= 15:
        return jsonify({"ok": False, "message": "Nombor telefon tidak sah."}), 400
    invalid_account = (
        len(bank_account_number) > 34
        or not 5 <= len(account_compact) <= 34
        or not re.fullmatch(r"[A-Za-z0-9]+", account_compact)
    )
    if len(bank_name) > 100 or invalid_account:
        return jsonify({"ok": False, "message": "Maklumat bank atau nombor akaun tidak sah."}), 400
    if plan not in REGISTRATION_PLANS:
        return jsonify({"ok": False, "message": "Sila pilih pakej akaun yang sah."}), 400

    plan_name, plan_price = REGISTRATION_PLANS[plan]
    registration = RegistrationRequest(
        full_name=full_name,
        email=email,
        phone=phone,
        bank_name=bank_name,
        bank_account_number=bank_account_number,
        plan_name=plan_name,
        plan_price=plan_price,
        referral_code=referral_code or None,
    )
    registration_saved = False
    for attempt in range(100):
        request_code = f"TN{secrets.randbelow(10000):04d}"
        if User.query.filter_by(nova_id=request_code).first():
            continue
        registration.request_code = request_code
        db.session.add(registration)
        try:
            db.session.commit()
            registration_saved = True
            break
        except IntegrityError:
            db.session.rollback()
            if attempt == 99:
                app.logger.exception("Failed to generate a unique registration request ID")
                return jsonify({"ok": False, "message": "ID empat digit yang unik tidak dapat dijana. Sila cuba lagi."}), 500
        except SQLAlchemyError:
            db.session.rollback()
            app.logger.exception("Failed to save registration request")
            return jsonify({"ok": False, "message": "Permohonan tidak dapat disimpan. Sila cuba lagi."}), 500

    if not registration_saved:
        app.logger.error("Failed to generate an unused registration NOVA ID after 100 attempts")
        return jsonify({"ok": False, "message": "NOVA ID yang unik tidak dapat dijana. Sila cuba lagi."}), 500

    return jsonify({
        "ok": True,
        "message": "Permohonan berjaya dihantar.",
        "request_code": registration.request_code,
        "plan_name": registration.plan_name,
        "plan_price": registration.plan_price,
    }), 201


@app.route("/api/login", methods=["POST"])
@limiter.limit("10 per minute")
def api_login():
    data = request.get_json(silent=True) if request.is_json else request.form
    if request.is_json and not isinstance(data, dict):
        return jsonify({"ok": False, "message": "Format permintaan log masuk tidak sah."}), 400
    nova_id = data.get("nova_id", "")
    password = data.get("password", "")
    if not isinstance(nova_id, str) or not isinstance(password, str):
        return jsonify({"ok": False, "message": "ID atau kata laluan tidak sah."}), 400
    nova_id = nova_id.strip()

    user = User.query.filter_by(nova_id=nova_id).first()

    if not user or not user.check_password(password):
        return jsonify({"ok": False, "message": "ID atau kata laluan tidak sah."}), 401

    if not user.is_active:
        return jsonify({"ok": False, "message": "Akaun anda telah dinyahaktifkan. Sila hubungi admin."}), 403

    session["user_id"] = user.id
    return jsonify({"ok": True, "redirect": url_for("dashboard")})


@app.route("/logout", methods=["POST"])
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
    withdrawal_requests = WithdrawalRequest.query.filter_by(user_id=user.id).order_by(
        WithdrawalRequest.created_at.desc()
    ).all()
    bank_registration = registration_for_user(user)
    level_one_referrals = User.query.filter_by(referred_by_id=user.id).order_by(
        User.created_at.asc()
    ).all()
    level_two_referrals = []
    if level_one_referrals:
        level_two_referrals = User.query.filter(
            User.referred_by_id.in_([referral.id for referral in level_one_referrals])
        ).order_by(User.created_at.asc()).all()
    user_commissions = ReferralCommission.query.filter_by(
        beneficiary_user_id=user.id
    ).order_by(ReferralCommission.paid_at.desc(), ReferralCommission.payable_at.desc()).all()
    commission_by_referral_level = {
        (commission.referred_user_id, commission.level): commission
        for commission in user_commissions
    }
    paid_commission_history = [
        commission for commission in user_commissions if commission.status == "paid"
    ]

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
        wallet_balance_before_session=max(
            0.0,
            float(user.balance or 0)
            - float(user.withdrawn_total or 0)
            + float(user.realized_profit_loss or 0)
            + user.paid_referral_commission,
        ),
        chart_labels=chart_labels,
        chart_values=chart_values,
        chart_timestamps=chart_timestamps,
        show_chart=show_chart,
        withdrawal_requests=withdrawal_requests,
        bank_registration=bank_registration,
        level_one_referrals=level_one_referrals,
        level_two_referrals=level_two_referrals,
        commission_by_referral_level=commission_by_referral_level,
        paid_commission_history=paid_commission_history,
        now_myt=now_myt(),
    )


# ---------------------------------------------------------------------------
# ADMIN: Login
# ---------------------------------------------------------------------------

@app.route("/admin/login", methods=["GET"])
def admin_login_page():
    return render_template("admin_login.html")


@app.route("/admin/api/login", methods=["POST"])
@limiter.limit("5 per minute")
def admin_api_login():
    data = request.get_json(silent=True) if request.is_json else request.form
    if request.is_json and not isinstance(data, dict):
        return jsonify({"ok": False, "message": "Format permintaan log masuk tidak sah."}), 400
    username = data.get("username", "")
    password = data.get("password", "")
    if not isinstance(username, str) or not isinstance(password, str):
        return jsonify({"ok": False, "message": "Username atau password admin tidak sah."}), 400
    username = username.strip()

    admin = AdminUser.query.filter_by(username=username).first()
    if not admin or not admin.check_password(password):
        return jsonify({"ok": False, "message": "Username atau password admin tidak sah."}), 401

    session["admin_id"] = admin.id
    if admin.check_password("admin123"):
        flash("Password admin masih menggunakan password lalai. Sila tukar segera.", "error")
    return jsonify({"ok": True, "redirect": url_for("admin_dashboard")})


@app.route("/admin/change-password", methods=["POST"])
@admin_required
def admin_change_password():
    admin = db.session.get(AdminUser, session["admin_id"])
    if not admin:
        session.pop("admin_id", None)
        return redirect(url_for("admin_login_page"))

    current_password = request.form.get("current_password", "")
    new_password = request.form.get("new_password", "")
    confirm_password = request.form.get("confirm_password", "")
    if not admin.check_password(current_password):
        flash("Password semasa tidak betul.", "error")
    elif len(new_password) < 12:
        flash("Password baharu mesti sekurang-kurangnya 12 aksara.", "error")
    elif new_password != confirm_password:
        flash("Pengesahan password baharu tidak sepadan.", "error")
    else:
        admin.set_password(new_password)
        db.session.commit()
        flash("Password admin berjaya ditukar.", "success")
    return redirect(url_for("admin_dashboard"))


@app.route("/admin/logout", methods=["POST"])
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
    registration_requests = RegistrationRequest.query.order_by(RegistrationRequest.created_at.desc()).all()
    withdrawal_requests = WithdrawalRequest.query.order_by(
        WithdrawalRequest.created_at.desc()
    ).all()
    linked_registration_ids = {
        user.registration_request_id for user in users if user.registration_request_id
    }
    protected_registration_ids = linked_registration_ids | {
        withdrawal.registration_request_id
        for withdrawal in withdrawal_requests
        if withdrawal.registration_request_id
    }
    referrals_by_user = {}
    for user in users:
        level_one = [referral for referral in users if referral.referred_by_id == user.id]
        level_one_ids = {referral.id for referral in level_one}
        level_two = [
            referral for referral in users if referral.referred_by_id in level_one_ids
        ]
        referrals_by_user[user.id] = (level_one, level_two)
    commissions_by_user = {user.id: [] for user in users}
    commission_totals_by_user = {
        user.id: {"paid": 0.0, "pending": 0.0} for user in users
    }
    commission_by_user_referral_level = {}
    for commission in ReferralCommission.query.order_by(
        ReferralCommission.created_at.desc()
    ).all():
        commissions_by_user.setdefault(commission.beneficiary_user_id, []).append(commission)
        commission_by_user_referral_level[
            (commission.beneficiary_user_id, commission.referred_user_id, commission.level)
        ] = commission
        total_key = "paid" if commission.status == "paid" else "pending"
        commission_totals_by_user.setdefault(
            commission.beneficiary_user_id, {"paid": 0.0, "pending": 0.0}
        )[total_key] += float(commission.amount or 0)

    return render_template(
        "admin_dashboard.html",
        users=users,
        registration_requests=registration_requests,
        withdrawal_requests=withdrawal_requests,
        protected_registration_ids=protected_registration_ids,
        referrals_by_user=referrals_by_user,
        commissions_by_user=commissions_by_user,
        commission_totals_by_user=commission_totals_by_user,
        commission_by_user_referral_level=commission_by_user_referral_level,
        now_myt=now_myt(),
    )


@app.route("/admin/registration/<int:registration_id>/delete", methods=["POST"])
@admin_required
def admin_delete_registration(registration_id):
    registration = db.get_or_404(RegistrationRequest, registration_id)
    if registration.user or WithdrawalRequest.query.filter_by(
        registration_request_id=registration.id
    ).first():
        flash("Rekod pendaftaran yang mempunyai pautan akaun atau sejarah pengeluaran tidak boleh dipadam.", "error")
        return redirect(url_for("admin_dashboard"))
    db.session.delete(registration)
    db.session.commit()
    flash("Permohonan pendaftaran berjaya dipadam.", "success")
    return redirect(url_for("admin_dashboard"))


@app.route("/admin/registration/<int:registration_id>/accept", methods=["POST"])
@admin_required
def admin_accept_registration(registration_id):
    registration = RegistrationRequest.query.filter_by(id=registration_id).with_for_update().first_or_404()
    if registration.user:
        flash(f"Permohonan ini sudah diterima. NOVA ID: {registration.user.nova_id}.", "error")
        return redirect(url_for("admin_dashboard"))
    if not registration.request_code:
        flash("Permohonan ini tiada NOVA ID yang sah dan tidak dapat diterima. Sila hubungi sokongan teknikal.", "error")
        return redirect(url_for("admin_dashboard"))
    if User.query.filter_by(nova_id=registration.request_code).first():
        flash(
            "NOVA ID permohonan ini sudah digunakan oleh akaun lain. "
            "Akaun tidak dicipta untuk mengelakkan ID login bertindih.",
            "error",
        )
        return redirect(url_for("admin_dashboard"))

    password = request.form.get("password", "")
    confirm_password = request.form.get("confirm_password", "")
    if len(password) < 12:
        flash("Password user mesti sekurang-kurangnya 12 aksara.", "error")
        return redirect(url_for("admin_dashboard"))
    if len(password) > 128:
        flash("Password user tidak boleh melebihi 128 aksara.", "error")
        return redirect(url_for("admin_dashboard"))
    if password != confirm_password:
        flash("Pengesahan password user tidak sepadan.", "error")
        return redirect(url_for("admin_dashboard"))

    user = User(
        nova_id=registration.request_code,
        referral_code=generate_referral_code(),
        referred_by=User.query.filter_by(referral_code=registration.referral_code).first()
        if registration.referral_code else None,
        full_name=registration.full_name,
        balance=registration.plan_price,
        registration_request=registration,
    )
    user.set_password(password)
    db.session.add(user)
    schedule_referral_commissions(user)
    try:
        db.session.commit()
    except IntegrityError:
        db.session.rollback()
        registration = db.session.get(RegistrationRequest, registration_id)
        if registration and registration.user:
            flash(f"Permohonan ini sudah diterima. NOVA ID: {registration.user.nova_id}.", "error")
        elif registration and User.query.filter_by(nova_id=registration.request_code).first():
            flash(
                "NOVA ID permohonan ini sudah digunakan oleh akaun lain. "
                "Akaun tidak dicipta untuk mengelakkan ID login bertindih.",
                "error",
            )
        else:
            app.logger.exception("Failed to create a user account from registration request %s", registration_id)
            flash("Akaun tidak dapat dicipta kerana ID bertindih. Sila cuba terima permohonan sekali lagi.", "error")
        return redirect(url_for("admin_dashboard"))
    except SQLAlchemyError:
        db.session.rollback()
        app.logger.exception("Failed to accept registration request %s", registration_id)
        flash("Akaun tidak dapat dicipta kerana ralat pangkalan data. Sila cuba lagi.", "error")
        return redirect(url_for("admin_dashboard"))

    flash(
        f"Permohonan diterima. Akaun {user.full_name} dicipta dengan NOVA ID {user.nova_id}. "
        f"Modal akaun ditetapkan mengikut pakej {registration.plan_name} (RM{registration.plan_price:,.2f}). "
        "Sampaikan NOVA ID serta password kepada user dengan selamat.",
        "success",
    )
    return redirect(url_for("admin_dashboard"))


@app.route("/admin/user/create", methods=["POST"])
@admin_required
def admin_create_user():
    password = request.form.get("password", "")
    full_name = request.form.get("full_name", "").strip()
    balance = request.form.get("balance", "0") or "0"
    registration_id = request.form.get("registration_request_id", "").strip()

    if not password or not full_name:
        flash("Sila isi Nama dan Password.", "error")
        return redirect(url_for("admin_dashboard"))

    try:
        balance_float = float(balance)
        if not math.isfinite(balance_float):
            flash("Modal awal mesti nombor yang sah.", "error")
            return redirect(url_for("admin_dashboard"))
        if balance_float < 0:
            flash("Modal awal tidak boleh negatif.", "error")
            return redirect(url_for("admin_dashboard"))
    except ValueError:
        flash("Format baki tidak sah (mesti angka).", "error")
        return redirect(url_for("admin_dashboard"))

    registration = None
    if registration_id:
        try:
            registration = db.session.get(RegistrationRequest, int(registration_id))
        except ValueError:
            registration = None
        if not registration:
            flash("Rekod pendaftaran yang dipilih tidak sah.", "error")
            return redirect(url_for("admin_dashboard"))
        if registration.user:
            flash("Rekod pendaftaran itu sudah dipautkan kepada akaun lain.", "error")
            return redirect(url_for("admin_dashboard"))
        full_name = registration.full_name

    nova_id = generate_nova_id()

    user = User(
        nova_id=nova_id,
        referral_code=generate_referral_code(),
        referred_by=User.query.filter_by(referral_code=registration.referral_code).first()
        if registration and registration.referral_code else None,
        full_name=full_name,
        balance=balance_float,
        registration_request=registration,
    )
    user.set_password(password)
    db.session.add(user)
    schedule_referral_commissions(user)
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
    is_matured = request.form.get("is_matured") == "on"
    registration_id = request.form.get("registration_request_id", "").strip()

    if full_name:
        user.full_name = full_name
    
    # Validasi balance (float) kalau ada input
    if balance not in (None, ""):
        try:
            balance_float = float(balance)
            if not math.isfinite(balance_float):
                flash("Modal mesti nombor yang sah.", "error")
                return redirect(url_for("admin_dashboard"))
            if balance_float < 0:
                flash("Modal tidak boleh negatif.", "error")
                return redirect(url_for("admin_dashboard"))
            user.balance = balance_float
        except ValueError:
            flash("Format baki tidak sah (mesti angka).", "error")
            return redirect(url_for("admin_dashboard"))
    
    if new_password:
        user.set_password(new_password)
    if registration_id:
        try:
            registration = db.session.get(RegistrationRequest, int(registration_id))
        except ValueError:
            registration = None
        if not registration:
            flash("Rekod pendaftaran yang dipilih tidak sah.", "error")
            return redirect(url_for("admin_dashboard"))
        if registration.user and registration.user.id != user.id:
            flash("Rekod pendaftaran itu sudah dipautkan kepada akaun lain.", "error")
            return redirect(url_for("admin_dashboard"))
        user.registration_request = registration
        user.full_name = registration.full_name
    else:
        user.registration_request = None
    user.is_active = is_active
    user.is_matured = is_matured

    db.session.commit()
    flash(f"Akaun '{user.nova_id}' berjaya dikemaskini.", "success")
    return redirect(url_for("admin_dashboard"))


@app.route("/withdrawal/request", methods=["POST"])
@login_required
@limiter.limit("5 per hour")
def user_withdrawal_request():
    user = User.query.filter_by(id=session["user_id"]).with_for_update().first()
    if not user:
        session.pop("user_id", None)
        return redirect(url_for("login_page"))

    if not user.is_trade_closed:
        flash("Permohonan pengeluaran hanya boleh dibuat selepas sesi dagangan ditutup.", "error")
        return redirect(url_for("dashboard"))

    try:
        amount = round(float(request.form.get("amount", "")), 2)
    except (TypeError, ValueError):
        flash("Sila masukkan jumlah pengeluaran yang sah.", "error")
        return redirect(url_for("dashboard"))

    if not math.isfinite(amount) or amount <= 0:
        flash("Jumlah pengeluaran mesti melebihi RM 0.00.", "error")
        return redirect(url_for("dashboard"))

    available = round(user.withdrawable_amount, 2)
    if amount > available:
        if user.is_matured:
            message = f"Jumlah melebihi baki yang boleh dikeluarkan (RM {available:,.2f})."
        else:
            message = f"Sebelum akaun matang, pengeluaran terhad kepada keuntungan (RM {available:,.2f})."
        flash(message, "error")
        return redirect(url_for("dashboard"))

    bank_registration = registration_for_user(user)
    if not bank_registration:
        flash("Akaun belum dipautkan kepada rekod pendaftaran yang tepat. Sila hubungi admin untuk semakan.", "error")
        return redirect(url_for("dashboard"))

    user.withdrawn_total = round(float(user.withdrawn_total or 0) + amount, 2)
    user.realized_profit_loss = round(
        float(user.realized_profit_loss or 0) + user.current_profit_loss,
        2,
    )
    db.session.add(WithdrawalRequest(
        user_id=user.id,
        registration_request_id=bank_registration.id,
        amount=amount,
        account_holder_name=bank_registration.full_name,
        bank_name=bank_registration.bank_name,
        bank_account_number=bank_registration.bank_account_number,
    ))
    TradeEntry.query.filter_by(user_id=user.id).delete()
    if user.live_balance_snapshot:
        db.session.delete(user.live_balance_snapshot)

    db.session.commit()
    flash("Permohonan pengeluaran berjaya dihantar kepada admin untuk semakan.", "success")
    return redirect(url_for("dashboard"))


@app.route("/admin/withdrawal/<int:withdrawal_id>/<action>", methods=["POST"])
@admin_required
def admin_update_withdrawal(withdrawal_id, action):
    withdrawal = WithdrawalRequest.query.filter_by(id=withdrawal_id).with_for_update().first_or_404()
    if withdrawal.status != "pending":
        flash("Permohonan ini telah diproses sebelum ini.", "error")
        return redirect(url_for("admin_dashboard"))

    if action not in {"processed", "rejected"}:
        return "Tindakan tidak sah.", 400

    user = User.query.filter_by(id=withdrawal.user_id).with_for_update().first()
    if not user:
        flash("Akaun user untuk permohonan ini tidak ditemui.", "error")
        return redirect(url_for("admin_dashboard"))

    if action == "rejected":
        user.withdrawn_total = max(
            0.0,
            round(float(user.withdrawn_total or 0) - withdrawal.amount, 2),
        )
        withdrawal.status = "rejected"
        flash(f"Permohonan {user.nova_id} ditolak dan RM {withdrawal.amount:,.2f} dipulangkan ke baki.", "success")
    else:
        payment_reference = request.form.get("payment_reference", "").strip()
        if not payment_reference or len(payment_reference) > 120:
            flash("Masukkan nombor rujukan transaksi bayaran (maksimum 120 aksara).", "error")
            return redirect(url_for("admin_dashboard"))
        withdrawal.payment_reference = payment_reference
        withdrawal.status = "processed"
        flash(f"Permohonan {user.nova_id} ditandakan selesai. Rujukan bayaran: {payment_reference}.", "success")

    withdrawal.processed_at = datetime.now(timezone.utc).replace(tzinfo=None)
    db.session.commit()
    return redirect(url_for("admin_dashboard"))


@app.route("/admin/user/<int:user_id>/archive", methods=["POST"])
@admin_required
def admin_archive_user(user_id):
    user = db.get_or_404(User, user_id)
    user.is_active = False
    db.session.commit()
    flash(f"Akaun '{user.nova_id}' dinyahaktifkan. Rekod akaun dan sejarah transaksi dikekalkan.", "success")
    return redirect(url_for("admin_dashboard"))


@app.route("/admin/user/<int:user_id>/add_entry", methods=["POST"])
@admin_required
def admin_add_entry(user_id):
    user = db.get_or_404(User, user_id)
    value = request.form.get("value")
    note = request.form.get("note", "").strip()
    entry_time_str = request.form.get("entry_time", "").strip()  # format: YYYY-MM-DDTHH:MM

    entry_type = request.form.get("entry_type", "entry").strip()  # "entry" atau "close"

    if entry_type not in {"entry", "close"}:
        flash("Jenis entry tidak sah.", "error")
        return redirect(url_for("admin_dashboard"))

    if not value:
        flash("Sila masukkan nilai untuk entry graf.", "error")
        return redirect(url_for("admin_dashboard"))

    # Validasi & parse value (float)
    try:
        value_float = float(value)
        if not math.isfinite(value_float):
            flash("Nilai entry mesti nombor yang sah.", "error")
            return redirect(url_for("admin_dashboard"))
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

    if entry_type == "close":
        if user.live_balance_snapshot:
            db.session.delete(user.live_balance_snapshot)

    db.session.commit()

    label = "Close" if entry_type == "close" else "Entry"
    flash(f"{label} graf ditambah untuk '{user.nova_id}' — RM {value_float:,.2f}.", "success")
    return redirect(url_for("admin_dashboard"))


@app.route("/admin/entry/<int:entry_id>/delete", methods=["POST"])
@admin_required
def admin_delete_entry(entry_id):
    entry = db.get_or_404(TradeEntry, entry_id)
    user = db.get_or_404(User, entry.user_id)
    profit_loss_before = user.current_profit_loss
    user.entries.remove(entry)
    db.session.flush()
    user.realized_profit_loss = round(
        float(user.realized_profit_loss or 0)
        + profit_loss_before
        - user.current_profit_loss,
        2,
    )
    if user.live_balance_snapshot:
        db.session.delete(user.live_balance_snapshot)
    db.session.commit()
    flash("Entry dipadam. Pelarasan dibuat supaya baki Wallet kekal konsisten.", "success")
    return redirect(url_for("admin_dashboard"))


@app.route("/admin/user/<int:user_id>/clear_history", methods=["POST"])
@admin_required
def admin_clear_history(user_id):
    user = db.get_or_404(User, user_id)
    user.realized_profit_loss = round(
        float(user.realized_profit_loss or 0) + user.current_profit_loss,
        2,
    )
    TradeEntry.query.filter_by(user_id=user.id).delete()
    if user.live_balance_snapshot:
        db.session.delete(user.live_balance_snapshot)
    db.session.commit()
    flash(f"History entry untuk '{user.nova_id}' berjaya dipadam.", "success")
    return redirect(url_for("admin_dashboard"))


@app.route("/admin/user/<int:user_id>/mature", methods=["POST"])
@admin_required
def admin_mark_user_mature(user_id):
    user = db.get_or_404(User, user_id)
    if user.is_matured:
        flash(f"Akaun '{user.nova_id}' sudah ditandakan matang.", "error")
    else:
        user.is_matured = True
        db.session.commit()
        flash(f"Akaun '{user.nova_id}' kini matang. Pengeluaran modal telah dibenarkan.", "success")
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


@app.cli.command("process-referral-commissions")
def process_referral_commissions_command():
    """Kredit komisen yang sudah melepasi tarikh bayaran berjadual."""
    paid_count = process_due_referral_commissions()
    if paid_count:
        print(f"{paid_count} komisen referral berjaya dikreditkan ke Wallet.")
    else:
        print("Tiada komisen referral tertunggak yang layak dibayar.")


if __name__ == "__main__":
    with app.app_context():
        db.create_all()
    app.run(debug=os.environ.get("FLASK_DEBUG", "0") == "1", host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))
