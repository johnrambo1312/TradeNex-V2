from flask_sqlalchemy import SQLAlchemy
from werkzeug.security import generate_password_hash, check_password_hash
from datetime import datetime, timezone, timedelta

db = SQLAlchemy()

def utcnow():
    return datetime.now(timezone.utc).replace(tzinfo=None)  # simpan sebagai naive UTC, konsisten

# MYT = UTC+8 — entry_date trade disimpan sebagai naive MYT (lihat app.py: now_myt()).
# Helper berasingan di sini (bukan import dari app.py) untuk elak circular import.
_MYT = timezone(timedelta(hours=8))

def _now_myt():
    return datetime.now(_MYT).replace(tzinfo=None)


class AdminUser(db.Model):
    """Akaun admin - boleh log masuk ke /admin untuk urus user."""
    __tablename__ = "admin_users"

    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(50), unique=True, nullable=False)
    password_hash = db.Column(db.String(255), nullable=False)
    created_at = db.Column(db.DateTime, default=utcnow)

    def set_password(self, raw_password):
        self.password_hash = generate_password_hash(raw_password)

    def check_password(self, raw_password):
        return check_password_hash(self.password_hash, raw_password)


class User(db.Model):
    """Akaun user (client demo). Dicipta oleh admin sahaja - tiada self-register."""
    __tablename__ = "users"

    id = db.Column(db.Integer, primary_key=True)
    nova_id = db.Column(db.String(50), unique=True, nullable=False)  # ID login
    password_hash = db.Column(db.String(255), nullable=False)
    full_name = db.Column(db.String(120), nullable=False)

    # Nilai akaun ringkas (admin set terus)
    balance = db.Column(db.Float, default=0.0)
    is_active = db.Column(db.Boolean, default=True)
    created_at = db.Column(db.DateTime, default=utcnow)

    entries = db.relationship(
        "TradeEntry", backref="user", lazy=True,
        order_by="TradeEntry.entry_date", cascade="all, delete-orphan"
    )
    live_balance_snapshot = db.relationship(
        "UserLiveBalance", backref="user", uselist=False, cascade="all, delete-orphan"
    )

    @property
    def current_balance(self):
        """Baki live semasa — berdasarkan entry sahaja, tidak melibatkan modal."""
        if not self.entries:
            return 0.0

        sorted_entries = sorted(self.entries, key=lambda e: e.entry_date)
        now = _now_myt()

        past = [e for e in sorted_entries if e.entry_date <= now]
        future = [e for e in sorted_entries if e.entry_date > now]

        if not past:
            return 0.0  # sesi pertama belum bermula

        anchor_past = past[-1]

        if not future:
            return anchor_past.value  # kekal pada nilai entry terakhir

        anchor_future = future[0]

        if anchor_past.entry_date.date() != anchor_future.entry_date.date():
            return anchor_past.value  # gap merentas hari, kekal flat

        total_seconds = (anchor_future.entry_date - anchor_past.entry_date).total_seconds()
        if total_seconds <= 0:
            return anchor_future.value

        elapsed = (now - anchor_past.entry_date).total_seconds()
        frac = max(0.0, min(1.0, elapsed / total_seconds))
        return anchor_past.value + (anchor_future.value - anchor_past.value) * frac

    @property
    def withdraw_live_balance(self):
        """Nilai withdraw diambil terus dari candle terakhir yang disimpan dari dashboard user."""
        if self.live_balance_snapshot:
            return float(self.live_balance_snapshot.value)
        return self.current_balance

    @property
    def is_trade_closed(self):
        """True bila sesi benar-benar tamat: ada Entry, last ialah Close, dan masa Close dah lepas."""
        if not self.entries:
            return False
        sorted_entries = sorted(self.entries, key=lambda e: e.entry_date)
        has_entry = any((e.note or "").startswith("[Entry]") for e in sorted_entries)
        if not has_entry:
            return False
        last = sorted_entries[-1]
        if not bool((last.note or "").startswith("[Close]")):
            return False

        # Status "Selesai" hanya selepas masa Close berlalu.
        return _now_myt() >= last.entry_date

    def set_password(self, raw_password):
        self.password_hash = generate_password_hash(raw_password)

    def check_password(self, raw_password):
        return check_password_hash(self.password_hash, raw_password)


class TradeEntry(db.Model):
    """
    Satu titik data dalam graf trading user.
    Admin tambah entry dari semasa ke semasa -> graf plot ikut entry_date & value.
    
    PENTING: entry_date MESTI disimpan sebagai naive MYT (UTC+8).
    Kalau admin tak specify waktu, auto jadi MYT sekarang (railway-safe).
    """
    __tablename__ = "trade_entries"

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False)

    entry_date = db.Column(db.DateTime, default=_now_myt)
    value = db.Column(db.Float, nullable=False)   # nilai portfolio pada titik masa ini
    note = db.Column(db.String(255))               # catatan pilihan, cth "Beli FCPO"


class UserLiveBalance(db.Model):
    """Snapshot nilai candle terakhir yang user nampak di dashboard."""
    __tablename__ = "user_live_balances"

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id"), unique=True, nullable=False)
    value = db.Column(db.Float, nullable=False, default=0.0)
    updated_at = db.Column(db.DateTime, default=utcnow, onupdate=utcnow)
