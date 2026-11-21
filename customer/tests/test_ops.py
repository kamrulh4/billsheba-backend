"""Operational paths: suspension jobs, account recovery, user/org/package management."""

from datetime import timedelta
from decimal import Decimal
from unittest import mock

from django.utils import timezone

from common.bd import current_period
from core.choices import SubscriptionStatus
from core.models import OTP, Organization, Subscription, User
from customer import tasks
from customer.models import Customer, Package, Payment
from customer.services import billing
from customer.tests.test_reliability import API, PW, Base


class SuspensionJobTests(Base):
    def router(self):
        self.org.router_ip, self.org.router_username, self.org.router_password = "https://10.0.0.1", "a", "b"
        self.org.save()
        return Organization.objects.get()

    def test_unpaid_customer_is_suspended_and_router_told(self):
        billing.generate_bills(self.org)
        org = self.router()
        with mock.patch("customer.services.billing.Mikrotik.toggle_ppp_user", return_value=(True, "ok")) as toggle:
            self.assertEqual(billing.suspend_overdue(org), {"suspended": 1})
        self.assertFalse(Customer.objects.get().is_active)
        self.assertEqual(toggle.call_args.args[:2], ("karim", True))

    def test_router_failure_leaves_customer_active_for_retry(self):
        billing.generate_bills(self.org)
        org = self.router()
        with mock.patch("customer.services.billing.Mikrotik.toggle_ppp_user", return_value=(False, "down")):
            self.assertEqual(billing.suspend_overdue(org), {"suspended": 0})
        self.assertTrue(Customer.objects.get().is_active)

    def test_paid_and_free_customers_are_never_suspended(self):
        billing.generate_bills(self.org)
        Payment.objects.update(paid=True, amount=600)
        self.assertEqual(billing.suspend_overdue(self.org)["suspended"], 0)
        Payment.objects.update(paid=False, amount=0)
        Customer.objects.update(is_free=True)
        self.assertEqual(billing.suspend_overdue(self.org)["suspended"], 0)

    def test_without_router_suspension_still_updates_database(self):
        billing.generate_bills(self.org)
        self.assertEqual(billing.suspend_overdue(self.org)["suspended"], 1)
        self.assertFalse(Customer.objects.get().is_active)

    def test_rolling_cycle_suspends_after_subscription_end(self):
        Organization.objects.update(billing_cycle="DAYS_30")
        Customer.objects.update(subscription_end_date=timezone.localdate() - timedelta(days=2))
        self.assertEqual(billing.suspend_expired_subscriptions(Organization.objects.get())["suspended"], 1)
        self.assertFalse(Customer.objects.get().is_active)

    def test_monthly_org_ignores_rolling_suspension(self):
        self.assertEqual(billing.suspend_expired_subscriptions(self.org), {"suspended": 0})


class DailyCycleTests(Base):
    def test_daily_cycle_bills_then_suspends_after_due_day(self):
        Organization.objects.update(due_day=0)  # "overdue" from day 1 of any month
        tasks.daily_billing_cycle()
        self.assertEqual(Payment.objects.count(), 1)
        self.assertFalse(Customer.objects.get().is_active)
        before = Payment.objects.count()
        tasks.daily_billing_cycle()  # idempotent
        self.assertEqual(Payment.objects.count(), before)

    def test_lapsed_trials_become_expired(self):
        Organization.objects.update(subscription_end_date=timezone.localdate() - timedelta(days=1))
        tasks.expire_lapsed_organizations()
        self.assertEqual(Organization.objects.get().subscription_status, SubscriptionStatus.EXPIRED)

    def test_per_org_task_wrappers(self):
        self.assertEqual(tasks.generate_customer_bills(self.org.id)["created"], 1)
        tasks.deactivate_due_payment_customers(self.org.id)
        tasks.generate_organizations_bills()
        tasks.deactivate_organizations_due_payment_customers()
        tasks.deactivate_all_organizations_expired_subscription_customers()
        self.assertIsNone(tasks.generate_customer_bills(999999))

    def test_task_endpoints_scope(self):
        self.assertEqual(self.client.get(f"{API}/tasks/generate-bills").status_code, 200)
        self.assertEqual(self.client.get(f"{API}/tasks/deactive-due-customer").status_code, 400)  # no router
        self.assertEqual(self.client.get(f"{API}/tasks/deactive-expired-subscription-customer").status_code, 200)
        for platform_only in ("generate-organizations-bills", "deactive-organizations-due-customer", "daily-cycle"):
            self.assertEqual(self.client.get(f"{API}/tasks/{platform_only}").status_code, 403)


class AccountRecoveryTests(Base):
    def test_forgot_password_full_flow(self):
        self.client.credentials()
        res = self.client.post(f"{API}/users/forget-password", {"phone": "01711000001"}, format="json")
        self.assertEqual(res.status_code, 200, res.content)
        otp = OTP.objects.get()
        again = self.client.post(f"{API}/users/forget-password", {"phone": "01711000001"}, format="json")
        self.assertEqual(again.status_code, 400)  # an active code already exists
        bad = self.client.post(
            f"{API}/users/forget-password",
            {"phone": "01711000001", "otp": "000000", "password": "New!Pass-2027x", "confirm_password": "New!Pass-2027x"},
            format="json",
        )
        self.assertEqual(bad.status_code, 400)
        ok = self.client.post(
            f"{API}/users/forget-password",
            {"phone": "01711000001", "otp": otp.code, "password": "New!Pass-2027x", "confirm_password": "New!Pass-2027x"},
            format="json",
        )
        self.assertEqual(ok.status_code, 200, ok.content)
        login = self.client.post(f"{API}/users/login", {"phone": "01711000001", "password": "New!Pass-2027x"}, format="json")
        self.assertEqual(login.status_code, 200)
        reuse = self.client.post(
            f"{API}/users/forget-password",
            {"phone": "01711000001", "otp": otp.code, "password": "Other!Pass-2028", "confirm_password": "Other!Pass-2028"},
            format="json",
        )
        self.assertEqual(reuse.status_code, 400)  # a code works once

    def test_expired_code_rejected_and_unknown_phone_404(self):
        self.client.credentials()
        self.client.post(f"{API}/users/forget-password", {"phone": "01711000001"}, format="json")
        OTP.objects.update(created_at=timezone.now() - timedelta(minutes=6))
        otp = OTP.objects.get()
        res = self.client.post(
            f"{API}/users/forget-password",
            {"phone": "01711000001", "otp": otp.code, "password": "New!Pass-2027x", "confirm_password": "New!Pass-2027x"},
            format="json",
        )
        self.assertEqual(res.status_code, 400)
        self.assertEqual(self.client.post(f"{API}/users/forget-password", {"phone": "01600000000"}, format="json").status_code, 404)

    def test_change_password(self):
        bad = self.client.post(
            f"{API}/users/me/change-password",
            {"old_password": "wrong", "new_password": "New!Pass-2027x", "confirm_new_password": "New!Pass-2027x"},
            format="json",
        )
        self.assertEqual(bad.status_code, 400)
        ok = self.client.post(
            f"{API}/users/me/change-password",
            {"old_password": PW, "new_password": "New!Pass-2027x", "confirm_new_password": "New!Pass-2027x"},
            format="json",
        )
        self.assertEqual(ok.status_code, 200, ok.content)
        self.client.credentials()
        self.assertEqual(
            self.client.post(f"{API}/users/login", {"phone": "01711000001", "password": "New!Pass-2027x"}, format="json").status_code,
            200,
        )

    def test_admin_force_resets_staff_password(self):
        staff = self.new_user("STAFF", "01911000001").data
        res = self.client.post(
            f"{API}/users/{staff['uid']}/force-reset-password",
            {"password": "Forced!Pass-2029", "confirm_password": "Forced!Pass-2029"},
            format="json",
        )
        self.assertEqual(res.status_code, 200, res.content)
        self.client.credentials()
        self.assertEqual(
            self.client.post(f"{API}/users/login", {"phone": "01911000001", "password": "Forced!Pass-2029"}, format="json").status_code,
            200,
        )

    def test_token_refresh_and_blocked_accounts(self):
        self.client.credentials()
        login = self.client.post(f"{API}/users/login", {"phone": "01711000001", "password": PW}, format="json").data
        ref = self.client.post(f"{API}/users/login/refresh", {"refresh_token": login["refresh_token"]}, format="json")
        self.assertEqual(ref.status_code, 200)
        self.assertEqual(self.client.post(f"{API}/users/login/refresh", {}, format="json").status_code, 400)
        self.assertEqual(self.client.post(f"{API}/users/login/refresh", {"refresh_token": "junk"}, format="json").status_code, 401)
        # an access token cannot be used as a refresh token
        self.assertEqual(
            self.client.post(f"{API}/users/login/refresh", {"refresh_token": login["access_token"]}, format="json").status_code, 401
        )
        User.objects.filter(phone="01711000001").update(is_active=False)
        self.assertEqual(self.client.post(f"{API}/users/login", {"phone": "01711000001", "password": PW}, format="json").status_code, 401)
        self.assertEqual(self.client.get(f"{API}/users/me", HTTP_AUTHORIZATION=f"Bearer {login['access_token']}").status_code, 401)

    def test_cancelled_organization_cannot_log_in(self):
        Organization.objects.update(subscription_status=SubscriptionStatus.CANCELLED)
        self.client.credentials()
        self.assertEqual(self.client.post(f"{API}/users/login", {"phone": "01711000001", "password": PW}, format="json").status_code, 403)


class ManagementTests(Base):
    def test_user_edit_and_deactivate(self):
        staff = self.new_user("STAFF", "01911000001").data
        upd = self.client.patch(f"{API}/users/{staff['uid']}", {"first_name": "Renamed"}, format="json")
        self.assertEqual(upd.status_code, 200, upd.content)
        self.assertEqual(self.client.delete(f"{API}/users/{staff['uid']}").status_code, 204)
        self.assertFalse(User.objects.get(phone="01911000001").is_active)
        me = User.objects.get(phone="01711000001")
        self.assertEqual(self.client.delete(f"{API}/users/{me.uid}").status_code, 400)  # not yourself

    def test_payment_edit_delete_and_collections(self):
        billing.generate_bills(self.org)
        res = self.client.post(f"{API}/payments", {"customer_id": self.customer.id, "amount": "100", "payment_method": "CASH"}, format="json")
        uid = res.data["uid"]
        upd = self.client.patch(f"{API}/payments/{uid}", {"amount": "600"}, format="json")
        self.assertEqual(upd.status_code, 200, upd.content)
        self.assertTrue(upd.data["paid"])
        self.assertEqual(self.client.get(f"{API}/payments/{uid}").status_code, 200)
        coll = self.client.get(f"{API}/payments/monthly/collections")
        self.assertEqual(coll.status_code, 200)
        self.assertEqual(self.client.get(f"{API}/customers/{self.customer.uid}/payments").data["count"], 1)
        self.assertEqual(self.client.delete(f"{API}/payments/{uid}").status_code, 204)
        self.assertEqual(Payment.objects.filter(uid=uid).count(), 0)

    def test_package_lifecycle(self):
        pkg = Package.objects.get()
        self.assertEqual(self.client.get(f"{API}/packages/{pkg.uid}").status_code, 200)
        self.assertEqual(self.client.patch(f"{API}/packages/{pkg.uid}", {"price": "650"}, format="json").status_code, 200)
        self.assertEqual(self.client.get(f"{API}/packages/{pkg.uid}/customers").data["count"], 1)
        self.assertEqual(self.client.delete(f"{API}/packages/{pkg.uid}").status_code, 400)  # still has customers
        Customer.objects.update(package=None)
        self.assertEqual(self.client.delete(f"{API}/packages/{pkg.uid}").status_code, 204)
        self.assertEqual(self.client.get(f"{API}/packages").data["count"], 0)

    def test_customer_soft_delete_updates_counter(self):
        self.assertEqual(self.client.delete(f"{API}/customers/{self.customer.uid}").status_code, 204)
        self.assertEqual(Organization.objects.get().total_customer, 0)
        self.assertEqual(Customer.objects.filter(status="REMOVED").count(), 1)

    def test_organization_rights(self):
        org = Organization.objects.get()
        listing = self.client.get(f"{API}/organizations")
        self.assertEqual(listing.status_code, 200)
        self.assertEqual(self.client.get(f"{API}/organizations/{org.uid}").status_code, 200)
        self.assertEqual(self.client.patch(f"{API}/organizations/{org.uid}", {"phone": "01711000099"}, format="json").status_code, 200)
        self.assertEqual(self.client.delete(f"{API}/organizations/{org.uid}").status_code, 403)
        self.assertEqual(self.client.post(f"{API}/organizations", {"name": "New", "phone": "01711000098"}, format="json").status_code, 403)

    def test_sessions_without_a_router_is_an_empty_state_not_an_error(self):
        res = self.client.get(f"{API}/organizations/active/sessions")
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.data, {"sessions": [], "router_configured": False})

    def test_only_platform_owner_manages_plans(self):
        plan = Subscription.objects.first()
        self.assertEqual(self.client.get(f"{API}/subscriptions").status_code, 200)
        self.assertEqual(self.client.patch(f"{API}/subscriptions/{plan.uid}", {"price": "1"}, format="json").status_code, 403)
        User.objects.create_superuser("Plat", "Owner", "01999000000", PW)
        self.login("01999000000")
        self.assertEqual(self.client.patch(f"{API}/subscriptions/{plan.uid}", {"price": "123"}, format="json").status_code, 200)
        self.assertEqual(Subscription.objects.get(pk=plan.pk).price, Decimal("123"))

    def test_package_without_billing_does_not_get_a_bill(self):
        Customer.objects.update(package=None)
        self.assertEqual(billing.generate_bills(self.org)["created"], 0)
        month, _ = current_period()
        self.assertEqual(Payment.objects.filter(billing_month=month).count(), 0)


class SubscriptionReconciliationTests(Base):
    PLATFORM = dict(
        PLATFORM_BKASH_APP_KEY="k", PLATFORM_BKASH_APP_SECRET="s",
        PLATFORM_BKASH_USERNAME="u", PLATFORM_BKASH_PASSWORD="p",
    )

    def make(self, age_minutes):
        from core.models import SubscriptionPayment

        plan = Subscription.objects.get(name="Basic")
        sp = SubscriptionPayment.objects.create(
            organization=self.org, plan=plan, months=1, amount=plan.price, payment_method="BKASH",
            transaction_id=f"online-{age_minutes}", gateway_payment_id=f"PAY{age_minutes}",
            payment_status=SubscriptionPayment.INITIATED,
        )
        SubscriptionPayment.objects.filter(pk=sp.pk).update(created_at=timezone.now() - timedelta(minutes=age_minutes))
        return sp

    def run_reconcile(self, status):
        from django.core.cache import cache
        from django.test import override_settings

        from core.services.billing import reconcile_subscription_payments

        cache.clear()
        token = mock.Mock(status_code=200)
        token.json.return_value = {"id_token": "t"}
        result = mock.Mock(status_code=200)
        result.json.return_value = {"transactionStatus": status, "trxID": "TRXSUB1", "amount": "500.00"}
        post = lambda url, **kw: token if url.endswith("/token/grant") else result  # noqa: E731
        with override_settings(**self.PLATFORM), mock.patch("common.bkash.requests.post", side_effect=post):
            return reconcile_subscription_payments()

    def test_lost_subscription_callback_is_recovered(self):
        sp = self.make(15)
        self.assertEqual(self.run_reconcile("Completed")["completed"], 1)
        sp.refresh_from_db()
        self.assertEqual((sp.payment_status, sp.transaction_id), ("APPROVED", "TRXSUB1"))
        org = Organization.objects.get()
        self.assertEqual((org.subscription.name, org.subscription_status), ("Basic", "ACTIVE"))

    def test_unfinished_subscription_checkout_expires_and_fresh_ones_wait(self):
        old, fresh = self.make(45), self.make(3)
        stats = self.run_reconcile("Initiated")
        self.assertEqual(stats["failed"], 1)
        old.refresh_from_db()
        fresh.refresh_from_db()
        self.assertEqual((old.payment_status, fresh.payment_status), ("REJECTED", "INITIATED"))

    def test_noop_without_platform_credentials(self):
        from core.services.billing import reconcile_subscription_payments

        self.make(15)
        self.assertEqual(reconcile_subscription_payments(), {"completed": 0, "failed": 0, "waiting": 0})


class QueryEfficiencyTests(Base):
    """Guards against N+1 regressions: query counts must not grow with the number of customers."""

    def make_customers(self, n):
        pkg = Package.objects.get()
        Customer.objects.bulk_create(
            [Customer(organization=self.org, name=f"C{i}", username=f"c{i}", phone=f"0172{i:07d}", package=pkg) for i in range(n)]
        )
        billing.generate_bills(self.org)

    def count_queries(self, fn):
        from django.db import connection
        from django.test.utils import CaptureQueriesContext

        with CaptureQueriesContext(connection) as ctx:
            fn()
        return len(ctx)

    def test_list_endpoints_do_not_grow_with_data(self):
        self.make_customers(5)
        small = {
            url: self.count_queries(lambda url=url: self.client.get(f"{API}{url}"))
            for url in ("/customers", "/payments", "/dashboard", "/reports/summary", "/payments/monthly/collections")
        }
        self.make_customers_more = self.make_customers
        pkg = Package.objects.get()
        Customer.objects.bulk_create(
            [Customer(organization=self.org, name=f"D{i}", username=f"d{i}", phone=f"0173{i:07d}", package=pkg) for i in range(60)]
        )
        billing.generate_bills(self.org, "JANUARY", 2020)
        big = {
            url: self.count_queries(lambda url=url: self.client.get(f"{API}{url}"))
            for url in small
        }
        self.assertEqual(small, big)

    def test_payments_list_has_no_distinct(self):
        from django.db import connection
        from django.test.utils import CaptureQueriesContext

        with CaptureQueriesContext(connection) as ctx:
            self.client.get(f"{API}/payments")
        self.assertFalse(any("DISTINCT" in q["sql"].upper() and "billing_month" in q["sql"] for q in ctx.captured_queries))

    def test_bulk_suspension_uses_constant_queries(self):
        self.make_customers(40)
        few = self.count_queries(lambda: billing.suspend_overdue(self.org))
        Customer.objects.update(is_active=True)
        pkg = Package.objects.get()
        Customer.objects.bulk_create(
            [Customer(organization=self.org, name=f"E{i}", username=f"e{i}", phone=f"0174{i:07d}", package=pkg) for i in range(40)]
        )
        billing.generate_bills(self.org)
        many = self.count_queries(lambda: billing.suspend_overdue(self.org))
        self.assertLessEqual(many, few + 2)  # ~2x the customers, same number of queries
        self.assertEqual(Customer.objects.filter(is_active=False).count(), 81)

    def test_collections_report_groups_per_collector(self):
        billing.generate_bills(self.org)
        for amount in ("200", "400"):
            self.client.post(f"{API}/payments", {"customer_id": self.customer.id, "amount": amount, "payment_method": "CASH"}, format="json")
        res = self.client.get(f"{API}/payments/monthly/collections")
        self.assertEqual(res.status_code, 200)
        (row,) = res.data["results"]
        self.assertEqual((row["first_name"], row["total_payments"]), ("R", 1))  # second POST completed the same bill
        self.assertEqual(str(row["total_amount"]), "600.00")
        self.assertEqual(row["payments"][0]["customer_name"], "Karim")


class DateRangeFilterTests(Base):
    def test_range_filters_include_the_whole_end_day(self):
        billing.generate_bills(self.org)
        self.client.post(
            f"{API}/payments", {"customer_id": self.customer.id, "amount": "600", "payment_method": "CASH"}, format="json"
        )
        today = timezone.localdate().isoformat()
        listed = self.client.get(f"{API}/payments?start_date={today}&end_date={today}&paid=true")
        self.assertEqual(listed.data["count"], 1)  # a payment made "now" is inside [today, today]
        yesterday = (timezone.localdate() - timedelta(days=1)).isoformat()
        past = self.client.get(f"{API}/payments?start_date={yesterday}&end_date={yesterday}&paid=true")
        self.assertEqual(past.data["count"], 0)
        coll = self.client.get(f"{API}/payments/monthly/collections?start_date={today}&end_date={today}")
        self.assertEqual(len(coll.data["results"]), 1)
        csv_ok = self.client.get(f"{API}/payments/export?start_date={today}&end_date={today}")
        self.assertIn("karim", b"".join(csv_ok.streaming_content).decode())
        self.assertEqual(self.client.get(f"{API}/payments/export?start_date=garbage").status_code, 400)


class ManualJobsOnCeleryTests(Base):
    """The Generate Bills / Suspend buttons queue Celery tasks, and still work when no worker is reachable."""

    def test_buttons_queue_celery_tasks(self):
        from django.test import override_settings

        with override_settings(BILLING_JOBS_IN_BACKGROUND=True), mock.patch.object(
            tasks.generate_customer_bills, "apply_async"
        ) as gen, mock.patch.object(tasks.deactivate_due_payment_customers, "apply_async") as sus:
            res = self.client.post(f"{API}/customers/bills/generate")
            self.assertEqual(res.status_code, 200, res.content)
            self.assertTrue(res.data["queued"])
            gen.assert_called_once()
            self.assertEqual(gen.call_args.kwargs["args"], [self.org.id])
            self.org.router_ip, self.org.router_username, self.org.router_password = "https://10.0.0.1", "a", "b"
            self.org.save()
            res = self.client.get(f"{API}/tasks/deactive-due-customer")
            self.assertTrue(res.data["queued"])
            sus.assert_called_once()
        self.assertFalse(Payment.objects.exists())  # nothing ran in the request: it was all queued

    def test_falls_back_to_running_now_when_the_broker_is_down(self):
        from django.test import override_settings

        with override_settings(BILLING_JOBS_IN_BACKGROUND=True), mock.patch.object(
            tasks.generate_customer_bills, "apply_async", side_effect=ConnectionError("redis down")
        ):
            res = self.client.post(f"{API}/customers/bills/generate")
        self.assertEqual(res.status_code, 200, res.content)
        self.assertFalse(res.data["queued"])
        self.assertEqual(res.data["created"], 1)  # the bill really was created
        self.assertEqual(Payment.objects.count(), 1)
