from flask_sqlalchemy import SQLAlchemy
from werkzeug.security import generate_password_hash, check_password_hash
from datetime import datetime, timezone, timedelta

from candle_engine import build_candles, final_candle_close

db = SQLAlchemy()

def utcnow():
    return datetime.now(timezone.utc).replace(tzinfo=None)  # simpan sebagai naive UTC, konsisten

# MYT = UTC+8 — entry_date trade disimpan sebagai naive MYT (lihat app.py: now_myt()).
# Helper berasingan di sini (bukan import dari app.py) untuk elak circular import.
_MYT = timezone(timedelta(hours=8))

def _now_myt():
    return datetime.now(_MYT).replace(tzinfo=None)


def _entry_timestamp(value):
    """Timestamp UTC (saat) untuk entry_date naive MYT, sama seperti carta."""
    return int(value.replace(tzinfo=_MYT).timestamp())


def _as_utc_naive(value, naive_timezone):
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=naive_timezone)
    return value.astimezone(timezone.utc).replace(tzinfo=None)


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
    __table_args__ = (
        db.Index("uq_users_registration_request_id", "registration_request_id", unique=True),
    )

    id = db.Column(db.Integer, primary_key=True)
    nova_id = db.Column(db.String(50), unique=True, nullable=False)  # ID login
    referral_code = db.Column(db.String(9), unique=True, nullable=True)
    referred_by_id = db.Column(db.Integer, db.ForeignKey("users.id", ondelete="SET NULL"))
    password_hash = db.Column(db.String(255), nullable=False)
    full_name = db.Column(db.String(120), nullable=False)

    # Modal yang ditetapkan admin; sistem tidak mengubahnya semasa trade atau withdraw.
    balance = db.Column(db.Float, default=0.0)
    current_rank = db.Column(db.Integer, default=0, nullable=False, server_default="0")
    withdrawn_total = db.Column(db.Float, default=0.0, nullable=False, server_default="0")
    realized_profit_loss = db.Column(db.Float, default=0.0, nullable=False, server_default="0")
    is_matured = db.Column(db.Boolean, default=False, nullable=False, server_default="false")
    registration_request_id = db.Column(
        db.Integer,
        db.ForeignKey("registration_requests.id", ondelete="RESTRICT"),
    )
    is_active = db.Column(db.Boolean, default=True)
    created_at = db.Column(db.DateTime, default=utcnow)

    registration_request = db.relationship("RegistrationRequest", back_populates="user", uselist=False)
    referred_by = db.relationship(
        "User", remote_side=[id], foreign_keys=[referred_by_id], backref="referred_users"
    )
    entries = db.relationship(
        "TradeEntry", backref="user", lazy=True,
        order_by="TradeEntry.entry_date", cascade="all, delete-orphan"
    )
    live_balance_snapshot = db.relationship(
        "UserLiveBalance", backref="user", uselist=False, cascade="all, delete-orphan"
    )
    withdrawal_requests = db.relationship(
        "WithdrawalRequest", backref="user", lazy=True,
        order_by="WithdrawalRequest.created_at.desc()"
    )
    referral_commissions = db.relationship(
        "ReferralCommission",
        foreign_keys="ReferralCommission.beneficiary_user_id",
        back_populates="beneficiary",
        lazy=True,
    )

    @property
    def paid_referral_commission(self):
        return sum(
            float(commission.amount or 0)
            for commission in self.referral_commissions
            if commission.status == "paid"
        )

    def _withdrawal_commission_allocations(self):
        paid_commissions = sorted(
            (
                (_as_utc_naive(commission.paid_at, _MYT), float(commission.amount or 0))
                for commission in self.referral_commissions
                if commission.status == "paid" and commission.paid_at
            ),
            key=lambda item: item[0],
        )
        withdrawals = sorted(
            self.withdrawal_requests,
            key=lambda withdrawal: (
                _as_utc_naive(withdrawal.created_at, timezone.utc) or datetime.min,
                withdrawal.id or 0,
            ),
        )
        allocations = {}
        allocated_commission = 0.0
        eligible_commission = 0.0
        paid_index = 0
        for withdrawal in withdrawals:
            if withdrawal.status == "rejected":
                allocations[withdrawal] = 0.0
                continue

            created_at = _as_utc_naive(withdrawal.created_at, timezone.utc)
            while (
                created_at is not None
                and paid_index < len(paid_commissions)
                and paid_commissions[paid_index][0] <= created_at
            ):
                eligible_commission += paid_commissions[paid_index][1]
                paid_index += 1

            amount = max(0.0, float(withdrawal.amount or 0))
            recorded_commission = withdrawal.commission_amount
            recorded_account = withdrawal.account_amount
            is_legacy = (
                float(recorded_commission or 0) == 0
                and float(recorded_account or 0) == 0
            )
            if is_legacy:
                # Legacy requests had no source split; reserve eligible paid commissions first.
                commission_amount = min(
                    amount,
                    max(0.0, eligible_commission - allocated_commission),
                )
            else:
                commission_amount = min(
                    amount,
                    max(0.0, float(recorded_commission or 0)),
                )

            allocations[withdrawal] = commission_amount
            allocated_commission += commission_amount
        return allocations

    @property
    def withdrawn_commission(self):
        return sum(self._withdrawal_commission_allocations().values())

    @property
    def withdrawable_commission(self):
        return round(max(0.0, self.paid_referral_commission - self.withdrawn_commission), 2)

    @property
    def withdrawn_account_amount(self):
        commission_allocations = self._withdrawal_commission_allocations()
        return sum(
            max(
                0.0,
                float(withdrawal.amount or 0)
                - commission_allocations.get(withdrawal, 0.0),
            )
            for withdrawal in self.withdrawal_requests
            if withdrawal.status != "rejected"
        )

    @property
    def wallet_balance_before_session(self):
        return round(
            max(
                0.0,
                float(self.balance or 0)
                + float(self.realized_profit_loss or 0)
                - self.withdrawn_account_amount,
            )
            + self.withdrawable_commission,
            2,
        )

    @property
    def wallet_account_balance(self):
        return round(
            max(
                0.0,
                float(self.balance or 0)
                + float(self.realized_profit_loss or 0)
                + self.current_profit_loss
                - self.withdrawn_account_amount,
            ),
            2,
        )

    @property
    def withdrawable_account_amount(self):
        account_balance = (
            float(self.balance or 0)
            if self.is_matured
            else 0.0
        )
        return round(
            max(
                0.0,
                account_balance
                + float(self.realized_profit_loss or 0)
                + self.current_profit_loss
                - self.withdrawn_account_amount,
            ),
            2,
        )

    @property
    def pending_referral_commission(self):
        return sum(
            float(commission.amount or 0)
            for commission in self.referral_commissions
            if commission.status == "pending"
        )

    def chart_candles(self, now_ts=None):
        """Candle lengkap untuk carta; kosong jika sesi (Entry -> Close) belum lengkap."""
        ordered = sorted(self.entries, key=lambda e: e.entry_date)
        if len(ordered) < 2:
            return []
        if not (ordered[0].note or "").startswith("[Entry]"):
            return []
        if not (ordered[-1].note or "").startswith("[Close]"):
            return []
        if now_ts is None:
            now_ts = datetime.now(timezone.utc).timestamp()
        return build_candles(
            self.id,
            ordered[0].value,
            ordered[-1].value,
            _entry_timestamp(ordered[0].entry_date),
            _entry_timestamp(ordered[-1].entry_date),
            now_ts,
        )

    @property
    def current_balance(self):
        """Baki live semasa — berdasarkan entry sahaja, tidak melibatkan modal."""
        if not self.entries:
            return 0.0

        sorted_entries = sorted(self.entries, key=lambda e: e.entry_date)
        now = _now_myt()

        if self.is_trade_closed:
            # Sesi tamat: nilai penutup candle terakhir di carta yang menentukan baki.
            closing_value = final_candle_close(
                self.id,
                sorted_entries[0].value,
                sorted_entries[-1].value,
                _entry_timestamp(sorted_entries[0].entry_date),
                _entry_timestamp(sorted_entries[-1].entry_date),
            )
            if closing_value is not None:
                return closing_value

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
    def current_profit_loss(self):
        if not self.entries:
            return 0.0

        sorted_entries = sorted(self.entries, key=lambda e: e.entry_date)
        if sorted_entries[0].entry_date > _now_myt():
            return 0.0
        starting_value = sorted_entries[0].value
        return self.current_balance - starting_value

    @property
    def withdraw_live_balance(self):
        """Baki Wallet: baki akaun ditambah komisen yang masih tersedia."""
        return self.wallet_account_balance + self.withdrawable_commission

    @property
    def wallet_display_balance(self):
        """Baki Wallet yang dipaparkan: dibekukan semasa sesi berjalan, dikemas kini bila sesi tamat."""
        entries = sorted(self.entries, key=lambda e: e.entry_date)
        session_ready = (
            len(entries) >= 2
            and any((e.note or "").startswith("[Entry]") for e in entries)
            and (entries[-1].note or "").startswith("[Close]")
        )
        if session_ready and not self.is_trade_closed:
            return self.wallet_balance_before_session
        return self.withdraw_live_balance

    @property
    def next_commission_payable_at(self):
        """Tarikh bayaran komisen menunggu yang paling awal, atau None."""
        dates = [
            commission.payable_at
            for commission in self.referral_commissions
            if commission.status == "pending" and commission.payable_at
        ]
        return min(dates) if dates else None

    @property
    def withdrawable_amount(self):
        """Jumlah pengeluaran tersedia; komisen diasingkan daripada baki akaun."""
        return self.withdrawable_account_amount + self.withdrawable_commission

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


class RegistrationRequest(db.Model):
    """Maklumat permohonan akaun yang hanya boleh diakses oleh admin."""
    __tablename__ = "registration_requests"

    id = db.Column(db.Integer, primary_key=True)
    request_code = db.Column(db.String(16), unique=True, nullable=True)
    full_name = db.Column(db.String(120), nullable=False)
    email = db.Column(db.String(254), nullable=False)
    phone = db.Column(db.String(30), nullable=False)
    bank_name = db.Column(db.String(100), nullable=False)
    bank_account_number = db.Column(db.String(34), nullable=False)
    plan_name = db.Column(db.String(20), nullable=False, default="basic", server_default="basic")
    plan_price = db.Column(db.Integer, nullable=False, default=488, server_default="488")
    referral_code = db.Column(db.String(9))
    created_at = db.Column(db.DateTime, default=utcnow, nullable=False)
    user = db.relationship("User", back_populates="registration_request", uselist=False)


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


class WithdrawalRequest(db.Model):
    """Permohonan pengeluaran untuk semakan dan pemprosesan admin."""
    __tablename__ = "withdrawal_requests"

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False, index=True)
    registration_request_id = db.Column(
        db.Integer,
        db.ForeignKey("registration_requests.id", ondelete="RESTRICT"),
    )
    amount = db.Column(db.Float, nullable=False)
    commission_amount = db.Column(db.Float, nullable=False, default=0.0, server_default="0")
    account_amount = db.Column(db.Float, nullable=False, default=0.0, server_default="0")
    account_holder_name = db.Column(db.String(120))
    bank_name = db.Column(db.String(100))
    bank_account_number = db.Column(db.String(34))
    payment_reference = db.Column(db.String(120))
    status = db.Column(db.String(20), nullable=False, default="pending", server_default="pending")
    created_at = db.Column(db.DateTime, default=utcnow, nullable=False)
    processed_at = db.Column(db.DateTime)


class ReferralCommission(db.Model):
    """Komisen referral sekali bayar bagi setiap akaun referral dan tahap."""
    __tablename__ = "referral_commissions"
    __table_args__ = (
        db.UniqueConstraint(
            "referred_user_id", "level", name="uq_referral_commission_user_level"
        ),
    )

    id = db.Column(db.Integer, primary_key=True)
    beneficiary_user_id = db.Column(
        db.Integer, db.ForeignKey("users.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    referred_user_id = db.Column(
        db.Integer, db.ForeignKey("users.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    level = db.Column(db.Integer, nullable=False)
    amount = db.Column(db.Float, nullable=False)
    status = db.Column(db.String(20), nullable=False, default="pending", server_default="pending")
    payable_at = db.Column(db.DateTime, nullable=False, index=True)
    created_at = db.Column(db.DateTime, default=utcnow, nullable=False)
    paid_at = db.Column(db.DateTime)

    beneficiary = db.relationship(
        "User", foreign_keys=[beneficiary_user_id], back_populates="referral_commissions"
    )
    referred_user = db.relationship("User", foreign_keys=[referred_user_id])
