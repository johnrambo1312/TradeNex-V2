import os
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal
from urllib.parse import urlparse


if os.environ.get("RUN_PRODUCTION_INTEGRATION") != "1":
    raise unittest.SkipTest("Production integration tests require disposable services.")

database_url = os.environ.get("DATABASE_URL", "")
if urlparse(database_url).path != "/tradenex_test":
    raise RuntimeError("Integration tests require the disposable tradenex_test database.")

from app import app, db  # noqa: E402
from models import RegistrationRequest, User, WithdrawalRequest  # noqa: E402


class ProductionIntegrationTests(unittest.TestCase):
    def test_postgres_decimal_and_concurrent_withdrawals(self):
        app.config.update(TESTING=True, WTF_CSRF_ENABLED=False)
        with app.app_context():
            registration = RegistrationRequest(
                request_code="integration-test",
                full_name="Integration Test",
                email="integration@example.test",
                phone="0123456789",
                bank_name="Test Bank",
                bank_account_number="123",
                plan_name="Silver",
                plan_price=508,
            )
            user = User(
                nova_id="integration-test",
                full_name="Integration Test",
                balance=Decimal("100.00"),
                is_matured=True,
                registration_request=registration,
            )
            user.set_password("integration-password")
            db.session.add(user)
            db.session.commit()
            user_id = user.id

        barrier = threading.Barrier(2)

        def withdraw(index):
            client = app.test_client()
            with client.session_transaction(base_url="https://localhost") as session:
                session["user_id"] = user_id
            barrier.wait(timeout=10)
            return client.post(
                "/withdrawal/request",
                data={"amount": "80.00"},
                base_url="https://localhost",
                environ_base={"REMOTE_ADDR": f"127.0.0.{index + 1}"},
            ).status_code

        with ThreadPoolExecutor(max_workers=2) as executor:
            responses = list(executor.map(withdraw, range(2)))
        self.assertEqual(responses, [302, 302])
        with app.app_context():
            user = db.session.get(User, user_id)
            self.assertEqual(user.withdrawable_amount, Decimal("20.00"))
            self.assertEqual(
                WithdrawalRequest.query.filter_by(user_id=user_id).count(), 1
            )

    def test_shared_rate_limit(self):
        app.config.update(TESTING=True, WTF_CSRF_ENABLED=False)
        clients = [app.test_client(), app.test_client()]
        responses = [
            clients[index % 2].post(
                "/admin/api/login",
                json={"username": "does-not-exist", "password": "invalid"},
                base_url="https://localhost",
                environ_base={"REMOTE_ADDR": "127.0.0.100"},
            ).status_code
            for index in range(6)
        ]
        self.assertEqual(responses, [401, 401, 401, 401, 401, 429])
