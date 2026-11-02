import os
import time
import unittest
from datetime import datetime

os.environ["APP_ENV"] = "test"
os.environ["SECRET_KEY"] = "test-only-secret"
os.environ["DEFAULT_ADMIN_PASSWORD"] = "test-admin-password"
os.environ["DATABASE_URL"] = "sqlite:///:memory:"

from app import (  # noqa: E402
    app,
    db,
    next_referral_commission_payable_at,
    now_myt,
    process_due_referral_commissions,
    schedule_referral_commissions,
)
from models import (  # noqa: E402
    AdminUser,
    ReferralCommission,
    RegistrationRequest,
    User,
    WithdrawalRequest,
)


class FinancialFlowTests(unittest.TestCase):
    def setUp(self):
        app.config.update(TESTING=True, WTF_CSRF_ENABLED=False, RATELIMIT_ENABLED=False)
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
