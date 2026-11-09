import os
import hashlib
import json
import time
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from decimal import Decimal
from unittest.mock import patch
from flask import template_rendered
from limits.errors import StorageError
from redis.exceptions import ConnectionError as RedisConnectionError
from sqlalchemy import event

os.environ["APP_ENV"] = "test"
os.environ["SECRET_KEY"] = "test-only-secret"
os.environ["DEFAULT_ADMIN_PASSWORD"] = "test-admin-password"
os.environ["DATABASE_URL"] = "sqlite:///:memory:"

from app import (  # noqa: E402
    app,
    db,
    limiter,
    next_referral_commission_payable_at,
    now_myt,
    process_due_referral_commissions,
    schedule_referral_commissions,
)
from models import (  # noqa: E402
    AdminUser,
    ReferralCommission,
    RegistrationRequest,
    TradeEntry,
    User,
    WithdrawalRequest,
)


class FinancialFlowTests(unittest.TestCase):
    def setUp(self):
        app.config.update(TESTING=True, WTF_CSRF_ENABLED=False, RATELIMIT_ENABLED=False)
        self.previous_limiter_enabled = limiter.enabled
        limiter.enabled = False
        with app.app_context():
            db.session.remove()
            db.drop_all()
            db.create_all()
            admin = AdminUser(username="test-admin")
            admin.set_password("test-admin-password")
            db.session.add(admin)
            db.session.commit()
            self.admin_id = admin.id
        self.client = app.test_client()

    def tearDown(self):
        limiter.enabled = self.previous_limiter_enabled
        with app.app_context():
            db.session.remove()
            db.drop_all()

    def create_user(
        self,
        nova_id,
        *,
        balance=0,
        is_matured=False,
        current_rank=0,
        referred_by=None,
        is_active=True,
    ):
        user = User(
            nova_id=nova_id,
            full_name=nova_id,
            balance=balance,
            is_matured=is_matured,
            current_rank=current_rank,
            referred_by=referred_by,
            is_active=is_active,
        )
        user.set_password("test-user-password")
        db.session.add(user)
        db.session.flush()
        return user

    def link_bank_registration(self, user):
        registration = RegistrationRequest(
            request_code=f"TN{user.id:04d}",
            full_name=user.full_name,
            email=f"{user.nova_id.lower()}@example.test",
            phone=f"012345{user.id:04d}",
            bank_name="Test Bank",
            bank_account_number=f"123456{user.id:04d}",
            plan_name="Silver",
            plan_price=508,
        )
        db.session.add(registration)
        db.session.flush()
        user.registration_request = registration
        return registration

    def login_user(self, user_id):
        with self.client.session_transaction() as session:
            session["user_id"] = user_id

    def login_admin(self):
        with self.client.session_transaction() as session:
            session["admin_id"] = self.admin_id
            session["admin_seen"] = int(time.time())

    def test_admin_dashboard_shows_newest_users_first(self):
        with app.app_context():
            older_user = self.create_user("TN-OLDER")
            older_user.created_at = datetime(2025, 1, 1)
            newer_user = self.create_user("TN-NEWER")
            newer_user.created_at = datetime(2025, 2, 1)
            db.session.commit()

        self.login_admin()
        response = self.client.get("/admin")

        self.assertEqual(response.status_code, 200)
        html = response.get_data(as_text=True)
        self.assertLess(html.index("TN-NEWER"), html.index("TN-OLDER"))

    def test_dashboard_pages_and_filters_are_applied_in_database(self):
        with app.app_context():
            for index in range(60):
                db.session.add(User(
                    nova_id=f"PAGE{index:04d}", full_name=f"Page User {index:04d}",
                    password_hash="test", is_active=index % 2 == 0,
                    is_matured=index % 3 == 0,
                    created_at=datetime(2025, 1, 1) + timedelta(minutes=index),
                ))
            for index in range(45):
                db.session.add(RegistrationRequest(
                    request_code=f"REG{index:04d}", full_name=f"Applicant {index:04d}",
                    email=f"user{index}@example.test", phone=f"012{index:07d}",
                    bank_name="Test Bank", bank_account_number="123",
                    created_at=datetime(2025, 1, 1) + timedelta(minutes=index),
                ))
            db.session.commit()
            engine = db.engine
        self.login_admin()
        contexts = []
        queries = []

        def capture_template(sender, template, context, **extra):
            if template.name == "admin_dashboard.html":
                contexts.append(context)

        def capture_query(conn, cursor, statement, parameters, context, executemany):
            queries.append(statement.lower())

        template_rendered.connect(capture_template, app, weak=False)
        event.listen(engine, "before_cursor_execute", capture_query)
        try:
            response = self.client.get("/admin?u_page=2&r_page=2")
            self.assertEqual(response.status_code, 200)
            self.assertEqual(len(contexts[-1]["users"]), 25)
            self.assertEqual(len(contexts[-1]["registration_requests"]), 20)
            self.assertEqual(contexts[-1]["users"][0].nova_id, "PAGE0034")
            self.assertEqual(contexts[-1]["registration_requests"][0].request_code, "REG0024")
            self.assertTrue(any("limit" in query and "users" in query for query in queries))
            self.assertTrue(any("limit" in query and "registration_requests" in query for query in queries))
            response = self.client.get("/admin?u_status=inactive&u_mat=matured&r_q=Applicant%20001")
            self.assertEqual(response.status_code, 200)
            self.assertEqual(contexts[-1]["filtered_user_count"], 10)
            self.assertTrue(all(not user.is_active and user.is_matured for user in contexts[-1]["users"]))
            self.assertEqual(contexts[-1]["filtered_registration_count"], 10)
        finally:
            template_rendered.disconnect(capture_template, app)
            event.remove(engine, "before_cursor_execute", capture_query)

    def test_registration_option_search_paginates_and_protects_linked_records(self):
        with app.app_context():
            linked_user = self.create_user("PICKER-USER")
            linked_registration = self.link_bank_registration(linked_user)
            for index in range(31):
                db.session.add(RegistrationRequest(
                    request_code=f"OPT{index:04d}", full_name=f"Applicant {index:04d}",
                    email=f"option{index}@example.test", phone=f"013{index:07d}",
                    bank_name="Test Bank", bank_account_number="123",
                    created_at=datetime(2025, 1, 1) + timedelta(minutes=index),
                ))
            db.session.commit()
            user_id = linked_user.id
            registration_id = linked_registration.id
        self.login_admin()
        first = self.client.get("/admin/api/registration-options").get_json()
        second = self.client.get("/admin/api/registration-options?page=2").get_json()
        self.assertEqual((first["count"], first["pages"], len(first["items"])), (31, 2, 25))
        self.assertEqual(len(second["items"]), 6)
        self.assertNotIn(registration_id, [item["id"] for item in first["items"] + second["items"]])
        current = self.client.get(f"/admin/api/registration-options?user_id={user_id}").get_json()
        self.assertEqual(current["count"], 32)
        self.assertIn(registration_id, [item["id"] for item in current["items"]])
        search = self.client.get("/admin/api/registration-options?q=Applicant%200001").get_json()
        self.assertEqual(search["count"], 1)
        self.assertIn("Applicant 0001", search["items"][0]["label"])

    def test_dashboard_trade_status_filters_match_account_status(self):
        now = now_myt()
        with app.app_context():
            ready = self.create_user("STATUS-READY")
            active = self.create_user("STATUS-ACTIVE")
            closed = self.create_user("STATUS-CLOSED")
            inactive = self.create_user("STATUS-INACTIVE", is_active=False)
            db.session.add_all([
                TradeEntry(user=active, value=100, entry_date=now - timedelta(minutes=3), note="[Entry]"),
                TradeEntry(user=active, value=110, entry_date=now + timedelta(minutes=3), note="[Close]"),
                TradeEntry(user=closed, value=100, entry_date=now - timedelta(minutes=5), note="[Entry]"),
                TradeEntry(user=closed, value=110, entry_date=now - timedelta(minutes=1), note="[Close]"),
            ])
            db.session.commit()
        self.login_admin()
        contexts = []

        def capture_template(sender, template, context, **extra):
            if template.name == "admin_dashboard.html":
                contexts.append(context)

        template_rendered.connect(capture_template, app, weak=False)
        try:
            for status, expected in (
                ("ready", "STATUS-READY"), ("active", "STATUS-ACTIVE"),
                ("closed", "STATUS-CLOSED"), ("inactive", "STATUS-INACTIVE"),
            ):
                response = self.client.get(f"/admin?u_status={status}")
                self.assertEqual(response.status_code, 200)
                self.assertEqual([user.nova_id for user in contexts[-1]["users"]], [expected])
                self.assertEqual(contexts[-1]["filtered_user_count"], 1)
        finally:
            template_rendered.disconnect(capture_template, app)

    def test_registration_options_require_admin(self):
        response = self.client.get("/admin/api/registration-options")
        self.assertEqual(response.status_code, 302)
        self.assertIn("/admin/login", response.headers["Location"])

    def test_health_endpoint_checks_database_and_disables_caching(self):
        response = self.client.get("/health")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json(), {"status": "ok"})
        self.assertEqual(response.headers["Cache-Control"], "no-store")

    def test_unmatured_user_can_withdraw_only_paid_commission(self):
        with app.app_context():
            user = self.create_user("TN1001", balance=500)
            self.link_bank_registration(user)
            referred_user = self.create_user("TN1002")
            db.session.add(ReferralCommission(
                beneficiary=user,
                referred_user=referred_user,
                level=1,
                amount=30,
                status="paid",
                payable_at=now_myt(),
                paid_at=now_myt(),
            ))
            db.session.commit()
            user_id = user.id

        self.login_user(user_id)
        response = self.client.post("/withdrawal/request", data={"amount": "20"})
        self.assertEqual(response.status_code, 302)

        with app.app_context():
            user = db.session.get(User, user_id)
            withdrawal = WithdrawalRequest.query.filter_by(user_id=user_id).one()
            self.assertEqual(withdrawal.amount, 20)
            self.assertEqual(withdrawal.commission_amount, 20)
            self.assertEqual(withdrawal.account_amount, 0)
            self.assertEqual(user.withdrawable_commission, 10)

        response = self.client.post("/withdrawal/request", data={"amount": "11"})
        self.assertEqual(response.status_code, 302)
        with app.app_context():
            self.assertEqual(
                WithdrawalRequest.query.filter_by(user_id=user_id).count(),
                1,
            )

    def test_matured_user_can_withdraw_account_balance(self):
        with app.app_context():
            user = self.create_user("TN2001", balance=500, is_matured=True)
            self.link_bank_registration(user)
            db.session.commit()
            user_id = user.id

        self.login_user(user_id)
        response = self.client.post("/withdrawal/request", data={"amount": "125.50"})
        self.assertEqual(response.status_code, 302)

        with app.app_context():
            withdrawal = WithdrawalRequest.query.filter_by(user_id=user_id).one()
            self.assertEqual(withdrawal.amount, 125.5)
            self.assertEqual(withdrawal.account_amount, 125.5)
            self.assertEqual(withdrawal.commission_amount, 0)

    def test_withdrawal_requires_linked_bank_registration(self):
        with app.app_context():
            user = self.create_user("TN2501", balance=500, is_matured=True)
            db.session.commit()
            user_id = user.id

        self.login_user(user_id)
        response = self.client.post("/withdrawal/request", data={"amount": "125"})
        self.assertEqual(response.status_code, 302)

        with app.app_context():
            user = db.session.get(User, user_id)
            self.assertEqual(
                WithdrawalRequest.query.filter_by(user_id=user_id).count(),
                0,
            )
            self.assertEqual(user.withdrawn_total, 0)

    def test_login_normalizes_nova_id_and_persists_preselected_language(self):
        with app.app_context():
            user = self.create_user("TN2601")
            db.session.commit()
            user_id = user.id

        self.client.post(
            "/language",
            data={"language": "ms", "next": "/login"},
        )
        response = self.client.post(
            "/api/login",
            json={"nova_id": "tn2601", "password": "test-user-password"},
        )
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.get_json()["ok"])

        with self.client.session_transaction() as session:
            self.assertEqual(session["language"], "ms")
            self.assertEqual(session["user_id"], user_id)
        with app.app_context():
            self.assertEqual(db.session.get(User, user_id).preferred_language, "ms")

    def test_admin_entry_keeps_chart_manager_open_until_close(self):
        with app.app_context():
            user = self.create_user("TN2701", balance=500)
            db.session.commit()
            user_id = user.id

        self.login_admin()
        entry_response = self.client.post(
            f"/admin/user/{user_id}/add_entry",
            data={"entry_type": "entry", "value": "550"},
        )
        self.assertEqual(entry_response.status_code, 302)
        self.assertIn(f"manage_user={user_id}", entry_response.headers["Location"])
        self.assertEqual(
            self.client.get(entry_response.headers["Location"]).status_code,
            200,
        )

        close_response = self.client.post(
            f"/admin/user/{user_id}/add_entry",
            data={"entry_type": "close", "value": "575"},
        )
        self.assertEqual(close_response.status_code, 302)
        self.assertNotIn("manage_user=", close_response.headers["Location"])

        with app.app_context():
            entries = db.session.get(User, user_id).entries
            self.assertEqual([entry.note for entry in entries], ["[Entry]", "[Close]"])

    def test_chart_entry_rejects_extreme_value_and_oversized_note(self):
        with app.app_context():
            user = self.create_user("TN2708")
            db.session.commit()
            user_id = user.id
        self.login_admin()
        for data, message in (
            ({"entry_type": "entry", "value": "1e308"}, "Nilai entry mesti kurang"),
            ({"entry_type": "entry", "value": "500", "note": "x" * 248},
             "Catatan entry tidak boleh melebihi"),
        ):
            response = self.client.post(
                f"/admin/user/{user_id}/add_entry", data=data, follow_redirects=True
            )
            self.assertEqual(response.status_code, 200)
            self.assertIn(message, response.get_data(as_text=True))
        with app.app_context():
            self.assertEqual(TradeEntry.query.filter_by(user_id=user_id).count(), 0)

    def test_close_session_limit_accepts_30_days_but_rejects_longer(self):
        start = datetime(2026, 1, 1)
        with app.app_context():
            user = self.create_user("TN2702", balance=500)
            db.session.add(TradeEntry(user=user, value=500, entry_date=start, note="[Entry]"))
            db.session.commit()
            user_id = user.id

        self.login_admin()
        response = self.client.post(
            f"/admin/user/{user_id}/add_entry",
            data={"entry_type": "close", "value": "550", "entry_time": "2026-01-31T00:01"},
            follow_redirects=True,
        )
        self.assertIn("tidak boleh melebihi 30 hari", response.get_data(as_text=True))
        with app.app_context():
            self.assertEqual(TradeEntry.query.filter_by(user_id=user_id).count(), 1)

        self.client.post(
            f"/admin/user/{user_id}/add_entry",
            data={"entry_type": "close", "value": "550", "entry_time": "2026-01-31T00:00"},
        )
        with app.app_context():
            self.assertEqual(TradeEntry.query.filter_by(user_id=user_id).count(), 2)

    def test_candle_feed_suffix_and_final_close_match_full_feed(self):
        start = datetime(2026, 1, 1)
        end = start + timedelta(days=7)
        with app.app_context():
            user = self.create_user("TN2703", balance=500)
            db.session.add_all([
                TradeEntry(user=user, value=500, entry_date=start, note="[Entry]"),
                TradeEntry(user=user, value=550, entry_date=end, note="[Close]"),
            ])
            db.session.commit()
            user_id = user.id
        self.login_user(user_id)

        with patch("app.now_myt", return_value=end - timedelta(seconds=1)):
            before = self.client.get("/api/chart/candles").get_json()
        self.assertFalse(before["closed"])
        start_index = len(before["candles"]) - 1
        with patch("app.now_myt", return_value=end):
            full = self.client.get("/api/chart/candles").get_json()
            suffix = self.client.get(
                f"/api/chart/candles?start={start_index}"
            ).get_json()
        self.assertTrue(full["closed"])
        self.assertTrue(suffix["closed"])
        self.assertEqual(before["candles"][:start_index] + suffix["candles"], full["candles"])
        self.assertEqual(len(full["candles"]), 7 * 24 * 60)
        self.assertLessEqual(abs(full["candles"][-1]["c"] - 550), 10)
        with patch("app.now_myt", return_value=end), patch.object(
            User, "chart_candles", side_effect=AssertionError("Unchanged feed was regenerated")
        ):
            unchanged = self.client.get(
                f"/api/chart/candles?start={start_index}&closed=1&revision={full['revision']}"
            ).get_json()
        self.assertTrue(unchanged["unchanged"])

        with app.app_context():
            closing_entry = TradeEntry.query.filter_by(user_id=user_id, note="[Close]").one()
            closing_entry.value = 560
            db.session.commit()
        with patch("app.now_myt", return_value=end):
            revised = self.client.get(
                f"/api/chart/candles?start={start_index}&closed=1&revision={full['revision']}"
            ).get_json()
        self.assertEqual(revised["start"], 0)
        self.assertNotEqual(revised["revision"], full["revision"])
        self.assertEqual(len(revised["candles"]), 7 * 24 * 60)

    def test_candle_feed_rejects_invalid_suffix(self):
        with app.app_context():
            user = self.create_user("TN2704")
            db.session.commit()
            user_id = user.id
        self.login_user(user_id)
        self.assertEqual(self.client.get("/api/chart/candles?start=-1").status_code, 400)
        self.assertEqual(self.client.get("/api/chart/candles?start=1").status_code, 400)
        self.assertEqual(self.client.get("/api/chart/candles?start=abc").status_code, 400)

    def test_30_day_candles_keep_existing_final_close(self):
        from candle_engine import build_candles, final_candle_close

        start = 1_767_196_800
        end = start + 30 * 24 * 60 * 60
        candles = build_candles(123, 500.0, 550.0, start, end, end)
        self.assertEqual(len(candles), 43_200)
        serialized = json.dumps(candles, sort_keys=True, separators=(",", ":")).encode()
        self.assertEqual(
            hashlib.sha256(serialized).hexdigest(),
            "861d028bf9af135885cfaac16615d84332b84cbd6d6ea7d4bf6cdb017f0e44e8",
        )
        self.assertEqual(candles[-1]["c"], final_candle_close(123, 500.0, 550.0, start, end))
        self.assertLessEqual(abs(candles[-1]["c"] - 550.0), 10.0)

    def test_incremental_candles_match_uncached_across_end_and_time_reversal(self):
        from candle_engine import _build_candles, build_candles

        start = 1_767_196_800
        end = start + 7 * 24 * 60 * 60 + 37
        key = (27123, 500.0, 550.0, start, end)
        for timestamp in (
            start + 60, start + 1440 * 60, end - 1, end,
            end + 100, start + 60 * 30, end,
        ):
            expected, _ = _build_candles(*key, timestamp)
            self.assertEqual(build_candles(*key, timestamp), expected)

    def test_candle_cache_computes_only_new_tail(self):
        from candle_engine import _hash_seed, build_candles

        start = 1_767_196_800
        key = (27124, 500.0, 550.0, start, start + 30 * 24 * 60 * 60)
        build_candles(*key, start + 29 * 24 * 60 * 60)
        with patch("candle_engine._hash_seed", wraps=_hash_seed) as hash_seed:
            result = build_candles(*key, start + 29 * 24 * 60 * 60 + 60)
        self.assertEqual(len(result), 29 * 24 * 60 + 1)
        self.assertEqual(hash_seed.call_count, 8)

    def test_concurrent_candle_reads_preserve_exact_results(self):
        from candle_engine import _build_candles, build_candles

        start = 1_767_196_800
        end = start + 7 * 24 * 60 * 60
        key = (27125, 500.0, 550.0, start, end)
        timestamps = [end - 120, end - 60, end, end + 1] * 4
        expected = {timestamp: _build_candles(*key, timestamp)[0] for timestamp in set(timestamps)}
        with ThreadPoolExecutor(max_workers=8) as executor:
            results = list(executor.map(lambda timestamp: build_candles(*key, timestamp), timestamps))
        for timestamp, actual in zip(timestamps, results):
            self.assertEqual(actual, expected[timestamp])

    def test_candle_cache_evicts_before_exceeding_limit(self):
        import candle_engine

        start = 1_767_196_800
        end = start + 300
        with candle_engine._cache_lock:
            candle_engine._cache.clear()
        try:
            with patch.object(candle_engine, "_CACHE_CANDLE_LIMIT", 6):
                candle_engine.build_candles(99101, 500.0, 550.0, start, end, end)
                original_build = candle_engine._build_candles

                def check_size(*args, **kwargs):
                    self.assertLessEqual(
                        sum(len(item[0]) for item in candle_engine._cache.values()) + 5, 6
                    )
                    return original_build(*args, **kwargs)

                with patch.object(candle_engine, "_build_candles", side_effect=check_size):
                    candle_engine.build_candles(99102, 500.0, 550.0, start, end, end)
                self.assertEqual(len(candle_engine._cache), 1)
        finally:
            with candle_engine._cache_lock:
                candle_engine._cache.clear()

    def test_candle_cache_overload_returns_retryable_503(self):
        import candle_engine

        with app.app_context():
            user = self.create_user("TN2707")
            start = now_myt() - timedelta(minutes=5)
            db.session.add_all([
                TradeEntry(user=user, value=500, entry_date=start, note="[Entry]"),
                TradeEntry(user=user, value=550, entry_date=start + timedelta(hours=1), note="[Close]"),
            ])
            db.session.commit()
            user_id = user.id
        self.login_user(user_id)
        lock_held = threading.Event()
        release_lock = threading.Event()

        def hold_lock():
            with candle_engine._cache_lock:
                lock_held.set()
                release_lock.wait(timeout=5)

        thread = threading.Thread(target=hold_lock)
        thread.start()
        self.assertTrue(lock_held.wait(timeout=2))
        try:
            with patch.object(candle_engine, "_CACHE_LOCK_TIMEOUT", 0.05):
                response = self.client.get("/api/chart/candles")
            self.assertEqual(response.status_code, 503)
            self.assertEqual(response.headers["Retry-After"], "15")
            self.assertFalse(response.get_json()["ok"])
        finally:
            release_lock.set()
            thread.join(timeout=5)
        self.assertFalse(thread.is_alive())
        self.assertEqual(self.client.get("/api/chart/candles").status_code, 200)

    def test_rate_limit_storage_failure_is_explicit_and_retryable(self):
        for error in (RedisConnectionError("test outage"), StorageError("test outage")):
            with self.subTest(error=type(error).__name__):
                with patch.object(limiter, "_check_request_limit", side_effect=error):
                    response = self.client.post(
                        "/admin/api/login",
                        json={"username": "admin", "password": "invalid"},
                    )
                self.assertEqual(response.status_code, 503)
                self.assertEqual(response.headers["Retry-After"], "15")
                self.assertFalse(response.get_json()["ok"])

    def test_decimal_withdrawals_keep_exact_cent_balance(self):
        with app.app_context():
            user = self.create_user("TN2705", balance=Decimal("1.00"), is_matured=True)
            self.link_bank_registration(user)
            db.session.commit()
            user_id = user.id
        self.login_user(user_id)
        for amount in ("0.10", "0.20", "0.30"):
            self.client.post("/withdrawal/request", data={"amount": amount})
        with app.app_context():
            user = db.session.get(User, user_id)
            self.assertEqual(user.withdrawn_total, Decimal("0.60"))
            self.assertEqual(user.withdrawable_amount, Decimal("0.40"))
            self.assertIsInstance(user.withdrawable_amount, Decimal)

    def test_large_request_is_rejected(self):
        response = self.client.post(
            "/api/registration",
            data=b"x" * (1024 * 1024 + 1),
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 413)

    def test_authenticated_dashboard_is_not_cached(self):
        with app.app_context():
            user = self.create_user("TN2706")
            db.session.commit()
            user_id = user.id
        self.login_user(user_id)
        response = self.client.get("/dashboard")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers["Cache-Control"], "private, no-store")

    def test_login_rejects_missing_csrf_token(self):
        app.config["WTF_CSRF_ENABLED"] = True
        try:
            response = self.client.post(
                "/api/login",
                json={"nova_id": "TN0000", "password": "invalid"},
            )
            self.assertEqual(response.status_code, 400)
        finally:
            app.config["WTF_CSRF_ENABLED"] = False

    def test_rejected_withdrawal_restores_available_balance(self):
        with app.app_context():
            user = self.create_user("TN3001", balance=500, is_matured=True)
            self.link_bank_registration(user)
            db.session.commit()
            user_id = user.id

        self.login_user(user_id)
        self.client.post("/withdrawal/request", data={"amount": "125"})
        with app.app_context():
            withdrawal = WithdrawalRequest.query.filter_by(user_id=user_id).one()
            withdrawal_id = withdrawal.id
            self.assertEqual(db.session.get(User, user_id).withdrawable_amount, 375)

        self.login_admin()
        response = self.client.post(
            f"/admin/withdrawal/{withdrawal_id}/rejected"
        )
        self.assertEqual(response.status_code, 302)

        with app.app_context():
            user = db.session.get(User, user_id)
            withdrawal = db.session.get(WithdrawalRequest, withdrawal_id)
            self.assertEqual(withdrawal.status, "rejected")
            self.assertEqual(user.withdrawn_total, 0)
            self.assertEqual(user.withdrawable_amount, 500)

    def test_referral_commissions_use_ranked_rates_and_next_payout_date(self):
        payout_check = datetime(2026, 10, 1, 0, 0)
        with app.app_context():
            grandparent = self.create_user("TN4001", current_rank=1)
            parent = self.create_user(
                "TN4002",
                current_rank=2,
                referred_by=grandparent,
            )
            referred = self.create_user("TN4003", referred_by=parent)
            schedule_referral_commissions(referred, now=payout_check)
            db.session.commit()

            commissions = ReferralCommission.query.filter_by(
                referred_user_id=referred.id
            ).order_by(ReferralCommission.level).all()
            self.assertEqual(
                [(commission.level, commission.amount) for commission in commissions],
                [(1, 50), (2, 15)],
            )
            self.assertTrue(all(
                commission.payable_at
                == next_referral_commission_payable_at(payout_check)
                for commission in commissions
            ))
            self.assertEqual(
                next_referral_commission_payable_at(payout_check),
                datetime(2026, 10, 30, 0, 5),
            )

    def test_due_commission_is_credited_once_and_inactive_accounts_are_skipped(self):
        due_at = datetime(2026, 10, 30, 0, 5)
        with app.app_context():
            beneficiary = self.create_user("TN5001")
            active_referred = self.create_user("TN5002")
            inactive_beneficiary = self.create_user("TN5003", is_active=False)
            inactive_referred = self.create_user("TN5004", is_active=False)
            active_commission = ReferralCommission(
                beneficiary=beneficiary,
                referred_user=active_referred,
                level=1,
                amount=30,
                payable_at=due_at,
            )
            inactive_commission = ReferralCommission(
                beneficiary=beneficiary,
                referred_user=inactive_referred,
                level=2,
                amount=15,
                payable_at=due_at,
            )
            inactive_beneficiary_commission = ReferralCommission(
                beneficiary=inactive_beneficiary,
                referred_user=active_referred,
                level=2,
                amount=15,
                payable_at=due_at,
            )
            db.session.add_all([
                active_commission,
                inactive_commission,
                inactive_beneficiary_commission,
            ])
            db.session.commit()
            active_commission_id = active_commission.id
            inactive_commission_id = inactive_commission.id
            inactive_beneficiary_commission_id = inactive_beneficiary_commission.id

            self.assertEqual(
                process_due_referral_commissions(datetime(2026, 10, 29, 23, 59)),
                0,
            )
            self.assertEqual(process_due_referral_commissions(due_at), 1)
            self.assertEqual(process_due_referral_commissions(due_at), 0)

            active_commission = db.session.get(
                ReferralCommission, active_commission_id
            )
            inactive_commission = db.session.get(
                ReferralCommission, inactive_commission_id
            )
            inactive_beneficiary_commission = db.session.get(
                ReferralCommission, inactive_beneficiary_commission_id
            )
            self.assertEqual(active_commission.status, "paid")
            self.assertEqual(active_commission.paid_at, due_at)
            self.assertEqual(
                db.session.get(User, active_commission.beneficiary_user_id)
                .withdrawable_commission,
                30,
            )
            self.assertEqual(inactive_commission.status, "pending")
            self.assertEqual(inactive_beneficiary_commission.status, "pending")


if __name__ == "__main__":
    unittest.main()
