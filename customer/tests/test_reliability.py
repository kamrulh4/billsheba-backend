"""Money/uptime safety nets: overdue gating, router retry, reminders, bKash reconciliation."""

from datetime import timedelta
from decimal import Decimal
from unittest import mock

from django.core.cache import cache
from django.core.management import call_command
from django.utils import timezone
from rest_framework.test import APITestCase

from common.bd import current_period
from core.models import Organization, User
from customer.models import Customer, OnlinePayment, Payment, SMSLog
from customer.services import billing
from customer.services.online import reconcile_online_payments
from customer.tests.test_bkash import FakeBkash

API = "/api/v1"
PW = "Str0ng!Pass-2026"


class Base(APITestCase):
    @classmethod
    def setUpTestData(cls):
        call_command("seed_plans", verbosity=0)

    def setUp(self):
        cache.clear()
        res = self.client.post(
            f"{API}/auth/signup",
            {"organization_name": "Net Zone", "first_name": "R", "phone": "01711000001",
             "email": "o@example.com", "password": PW, "confirm_password": PW},
            format="json",
        )
        self.admin_tokens = res.data
        from core.models import Subscription

        Subscription.objects.update(max_users=20)
        self.as_admin()
        pkg = self.client.post(f"{API}/packages", {"name": "P", "speed_mbps": 10, "price": 600}, format="json").data
        self.client.post(
            f"{API}/customers",
            {"name": "Karim", "phone": "01811000001", "username": "karim", "password": "pw123456", "package_id": pkg["id"]},
            format="json",
        )
        self.org = Organization.objects.get()
        self.customer = Customer.objects.get()

    def as_admin(self):
        self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {self.admin_tokens['access_token']}")

    def login(self, phone):
        res = self.client.post(f"{API}/users/login", {"phone": phone, "password": PW}, format="json")
        self.assertEqual(res.status_code, 200, res.content)
        self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {res.data['access_token']}")

    def new_user(self, kind, phone):
        body = {"first_name": kind, "last_name": "U", "phone": phone, "email": f"{phone}@e.com",
                "kind": kind, "password": PW, "confirm_password": PW}
        return self.client.post(f"{API}/users", body, format="json")


class RoleHierarchyTests(Base):
    def test_roles_cannot_escalate(self):
        self.assertEqual(self.new_user("MANAGER", "01911000001").status_code, 201)
        self.assertEqual(self.new_user("STAFF", "01911000002").status_code, 201)

        self.login("01911000001")  # manager
        self.assertEqual(self.new_user("ADMIN", "01911000003").status_code, 400)
        self.assertEqual(self.new_user("MANAGER", "01911000004").status_code, 400)
        self.assertEqual(self.new_user("STAFF", "01911000005").status_code, 201)
        admin = User.objects.get(phone="01711000001")
        res = self.client.patch(f"{API}/users/{admin.uid}", {"first_name": "Hacked"}, format="json")
        self.assertEqual(res.status_code, 400)
        self.assertNotEqual(User.objects.get(pk=admin.pk).first_name, "Hacked")

        self.login("01911000002")  # staff
        self.assertEqual(self.new_user("STAFF", "01911000006").status_code, 403)

    def test_cannot_change_own_role(self):
        self.new_user("MANAGER", "01911000001")
        self.login("01911000001")
        me = User.objects.get(phone="01911000001")
        res = self.client.patch(f"{API}/users/{me.uid}", {"kind": "STAFF"}, format="json")
        self.assertEqual(res.status_code, 400)


class OverdueGatingTests(Base):
    def test_paying_this_month_does_not_enable_while_older_bill_is_unpaid(self):
        month, year = current_period()
        prev_month = "JANUARY" if month != "JANUARY" else "FEBRUARY"
        Payment.objects.create(
            organization=self.org, customer=self.customer, bill_amount=600, amount=0,
            billing_month=prev_month, billing_year=year - 1, paid=False,
        )
        Customer.objects.update(is_active=False)
        res = self.client.post(
            f"{API}/payments",
            {"customer_id": self.customer.id, "amount": "600", "payment_method": "CASH"},
            format="json",
        )
        self.assertEqual(res.status_code, 201, res.content)
        self.assertTrue(res.data["paid"])
        self.assertFalse(Customer.objects.get().is_active)  # old bill still owed

        # clearing the old bill switches the customer back on
        old = Payment.objects.get(billing_year=year - 1)
        self.client.patch(f"{API}/payments/{old.uid}", {"amount": "600", "paid": True}, format="json")
        self.assertTrue(Customer.objects.get().is_active)


class RouterRetryTests(Base):
    def setUp(self):
        super().setUp()
        self.org.router_ip, self.org.router_username, self.org.router_password = "https://10.0.0.1", "a", "b"
        self.org.save()

    def test_failed_router_call_is_remembered_and_retried(self):
        customer = Customer.objects.get()
        with mock.patch("customer.models.Mikrotik.toggle_ppp_user", return_value=(False, "down")):
            customer.is_active = False
            customer.save(update_fields=["is_active"])
        self.assertTrue(Customer.objects.get().router_sync_pending)

        with mock.patch("customer.services.billing.Mikrotik.toggle_ppp_user", return_value=(True, "ok")) as call:
            result = billing.retry_router_sync(Organization.objects.get())
        self.assertEqual(result, {"retried": 1, "fixed": 1})
        self.assertEqual(call.call_args.args[:2], ("karim", True))  # still inactive => disable
        self.assertFalse(Customer.objects.get().router_sync_pending)

    def test_successful_call_clears_flag(self):
        Customer.objects.update(router_sync_pending=True)
        customer = Customer.objects.get()
        with mock.patch("customer.models.Mikrotik.toggle_ppp_user", return_value=(True, "ok")):
            customer.is_active = False
            customer.save(update_fields=["is_active"])
        self.assertFalse(Customer.objects.get().router_sync_pending)


class ReminderTests(Base):
    def test_reminder_sent_once_per_day_on_due_day(self):
        billing.generate_bills(self.org)
        Organization.objects.update(sms_feature=True, sms_quota_override=1000, due_day=timezone.localdate().day)
        org = Organization.objects.get()
        with mock.patch("customer.services.sms.SMS.send_single_sms", return_value=True) as sms:
            self.assertEqual(billing.send_due_reminders(org), {"sent": 1})
            self.assertEqual(billing.send_due_reminders(org), {"sent": 0})  # idempotent
        self.assertEqual(sms.call_count, 1)
        self.assertEqual(SMSLog.objects.count(), 1)
        self.assertIn("600", SMSLog.objects.get().message)

    def test_two_day_early_reminder_is_off_by_default_but_can_be_enabled(self):
        from django.test import override_settings

        billing.generate_bills(self.org)
        Organization.objects.update(sms_feature=True, sms_quota_override=1000, due_day=timezone.localdate().day + 2 if timezone.localdate().day <= 26 else 28)
        org = Organization.objects.get()
        if org.due_day - timezone.localdate().day != 2:
            self.skipTest("needs a day of month <= 26 to place the due day two days ahead")
        with mock.patch("customer.services.sms.SMS.send_single_sms", return_value=True):
            self.assertEqual(billing.send_due_reminders(org)["sent"], 0)  # default: off
            with override_settings(REMINDER_DAYS_BEFORE_DUE=2):
                self.assertEqual(billing.send_due_reminders(org)["sent"], 1)

    def test_no_reminder_on_other_days(self):
        billing.generate_bills(self.org)
        day = timezone.localdate().day
        Organization.objects.update(sms_feature=True, sms_quota_override=1000, due_day=28 if day < 20 else 5)
        with mock.patch("customer.services.sms.SMS.send_single_sms", return_value=True) as sms:
            self.assertEqual(billing.send_due_reminders(Organization.objects.get())["sent"], 0)
        sms.assert_not_called()


class ReconciliationTests(Base):
    def make_op(self, age_minutes):
        self.org.bkash_app_key = self.org.bkash_app_secret = self.org.bkash_username = self.org.bkash_password = "x"
        self.org.save()
        billing.generate_bills(self.org)
        op = OnlinePayment.objects.create(
            organization=self.org, customer=self.customer, amount=Decimal("600"),
            invoice_number=f"INV{age_minutes}", gateway_payment_id=f"PAY{age_minutes}",
        )
        OnlinePayment.objects.filter(pk=op.pk).update(created_at=timezone.now() - timedelta(minutes=age_minutes))
        return op

    def run_reconcile(self, **fake_kwargs):
        fake = FakeBkash(**fake_kwargs)
        fake.created_amount = "600.00"
        status = mock.Mock(status_code=200)
        status.json.return_value = {"transactionStatus": fake.execute_status, "trxID": "TRXQ1", "amount": "600.00"}
        token = mock.Mock(status_code=200)
        token.json.return_value = {"id_token": "t"}
        post = lambda url, **kw: token if url.endswith("/token/grant") else status  # noqa: E731
        with mock.patch("common.bkash.requests.post", side_effect=post):
            return reconcile_online_payments()

    def test_lost_callback_is_recovered_from_bkash(self):
        op = self.make_op(15)
        stats = self.run_reconcile()
        self.assertEqual(stats["completed"], 1)
        op.refresh_from_db()
        self.assertEqual((op.online_status, op.trx_id), ("COMPLETED", "TRXQ1"))
        self.assertTrue(Payment.objects.get().paid)
        # idempotent
        self.assertEqual(self.run_reconcile(), {"completed": 0, "failed": 0, "waiting": 0})

    def test_fresh_checkout_is_left_alone(self):
        self.make_op(3)
        self.assertEqual(self.run_reconcile(), {"completed": 0, "failed": 0, "waiting": 0})

    def test_unfinished_old_checkout_expires(self):
        op = self.make_op(45)
        stats = self.run_reconcile(execute_status="Initiated")
        self.assertEqual(stats["failed"], 1)
        op.refresh_from_db()
        self.assertEqual(op.online_status, "FAILED")
        self.assertEqual(Payment.objects.get().amount, Decimal("0.00"))
