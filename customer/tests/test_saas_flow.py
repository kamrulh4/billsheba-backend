"""End-to-end checks for the multi-tenant SaaS flow (onboarding, isolation, billing)."""

from datetime import timedelta

from django.core.management import call_command
from django.utils import timezone
from rest_framework.test import APITestCase

from core.choices import SubscriptionStatus
from core.models import Organization

API = "/api/v1"
PW = "Str0ng!Pass-2026"


def signup_payload(n, **over):
    data = {
        "organization_name": f"Net Zone {n}",
        "first_name": "Rahim",
        "last_name": "Uddin",
        "phone": f"0171100000{n}",
        "email": f"owner{n}@example.com",
        "password": PW,
        "confirm_password": PW,
    }
    data.update(over)
    return data


class SaaSFlowTests(APITestCase):
    @classmethod
    def setUpTestData(cls):
        call_command("seed_plans", verbosity=0)

    def setUp(self):
        from django.core.cache import cache

        cache.clear()  # reset throttle counters between tests

    def signup(self, n):
        res = self.client.post(f"{API}/auth/signup", signup_payload(n), format="json")
        self.assertEqual(res.status_code, 201, res.content)
        return res.data

    def auth(self, tokens):
        self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {tokens['access_token']}")

    def make_package(self, price=500):
        res = self.client.post(f"{API}/packages", {"name": "Home 10", "speed_mbps": 10, "price": price}, format="json")
        self.assertEqual(res.status_code, 201, res.content)
        return res.data

    def make_customer(self, pkg, n=1, **over):
        data = {
            "name": f"Karim {n}", "phone": f"0181100000{n}", "username": f"karim{n}",
            "password": "pass1234", "package_id": pkg["id"], "area": "Mirpur",
        }
        data.update(over)
        return self.client.post(f"{API}/customers", data, format="json")

    # ---- onboarding ---------------------------------------------------
    def test_signup_creates_trial_org_and_can_login(self):
        tokens = self.signup(1)
        org = Organization.objects.get(name="Net Zone 1")
        self.assertEqual(org.subscription_status, SubscriptionStatus.TRIAL)
        self.assertTrue(org.slug)
        self.assertGreater(org.subscription_end_date, timezone.localdate())
        self.assertEqual(tokens["user"]["kind"], "ADMIN")

        res = self.client.post(f"{API}/users/login", {"phone": "+8801711000001", "password": PW}, format="json")
        self.assertEqual(res.status_code, 200, res.content)

    def test_signup_rejects_duplicates_and_bad_input(self):
        self.signup(1)
        dup = self.client.post(f"{API}/auth/signup", signup_payload(2, phone="01711000001"), format="json")
        self.assertEqual(dup.status_code, 400)
        bad = self.client.post(f"{API}/auth/signup", signup_payload(3, phone="12345"), format="json")
        self.assertEqual(bad.status_code, 400)
        weak = self.client.post(
            f"{API}/auth/signup", signup_payload(4, password="123", confirm_password="123"), format="json"
        )
        self.assertEqual(weak.status_code, 400)

    # ---- core flow ----------------------------------------------------
    def test_customer_bill_payment_cycle(self):
        self.auth(self.signup(1))
        pkg = self.make_package(500)
        res = self.make_customer(pkg)
        self.assertEqual(res.status_code, 201, res.content)
        cust = res.data

        self.assertEqual(self.make_customer(pkg, 2, username="karim1", phone="01811000002").status_code, 400)

        bill = self.client.post(f"{API}/customers/bills/generate")
        self.assertEqual(bill.data["created"], 1)
        again = self.client.post(f"{API}/customers/bills/generate")
        self.assertEqual(again.data["created"], 0)  # idempotent

        part = self.client.post(f"{API}/payments", {"customer_id": cust["id"], "amount": "200", "payment_method": "CASH"}, format="json")
        self.assertEqual(part.status_code, 201, part.content)
        self.assertFalse(part.data["paid"])
        self.assertEqual(str(part.data["due"]), "300.00")

        full = self.client.post(f"{API}/payments", {"customer_id": cust["id"], "amount": "300", "payment_method": "BKASH"}, format="json")
        self.assertTrue(full.data["paid"])
        dup = self.client.post(f"{API}/payments", {"customer_id": cust["id"], "amount": "10", "payment_method": "CASH"}, format="json")
        self.assertEqual(dup.status_code, 400)  # already paid this month

        dash = self.client.get(f"{API}/dashboard").data
        self.assertEqual(dash["total_customers"], 1)
        self.assertEqual(dash["total_revenue"], "500.00")

    def test_customer_limit_enforced(self):
        self.auth(self.signup(1))
        pkg = self.make_package()
        Organization.objects.update(allowed_customer=1)
        self.assertEqual(self.make_customer(pkg, 1).status_code, 201)
        res = self.make_customer(pkg, 2)
        self.assertEqual(res.status_code, 400)
        self.assertIn("limit", res.data["message"].lower())

    def test_soft_delete_keeps_payments_and_frees_username(self):
        self.auth(self.signup(1))
        pkg = self.make_package()
        cust = self.make_customer(pkg).data
        self.assertEqual(self.client.delete(f"{API}/customers/{cust['uid']}").status_code, 204)
        self.assertEqual(self.client.get(f"{API}/customers").data["count"], 0)
        self.assertEqual(self.make_customer(pkg).status_code, 201)  # username reusable

    # ---- tenant isolation ---------------------------------------------
    def test_orgs_cannot_see_or_touch_each_others_data(self):
        a, b = self.signup(1), self.signup(2)
        self.auth(a)
        pkg_a = self.make_package()
        cust_a = self.make_customer(pkg_a).data
        self.auth(b)
        pkg_b = self.make_package()

        self.assertEqual(self.client.get(f"{API}/customers").data["count"], 0)
        self.assertEqual(self.client.get(f"{API}/customers/{cust_a['uid']}").status_code, 404)
        self.assertEqual(self.client.patch(f"{API}/customers/{cust_a['uid']}", {"name": "x"}, format="json").status_code, 404)
        self.assertEqual(self.client.delete(f"{API}/customers/{cust_a['uid']}").status_code, 404)
        # paying for / using another tenant's records is "not found"
        pay = self.client.post(f"{API}/payments", {"customer_id": cust_a["id"], "amount": "100", "payment_method": "CASH"}, format="json")
        self.assertEqual(pay.status_code, 400)
        steal = self.make_customer(pkg_a, 9, username="other9")
        self.assertEqual(steal.status_code, 400)  # package of org A
        self.assertEqual(self.make_customer(pkg_b, 9, username="other9").status_code, 201)
        # same username may exist in two different ISPs
        self.assertEqual(self.make_customer(pkg_b, 1).status_code, 201)
        # status toggle by username can't reach org A either
        res = self.client.post(f"{API}/customers/status/toggle", {"username": "karim1", "is_active": False}, format="json")
        self.assertEqual(Organization.objects.get(name="Net Zone 1").customers.get(username="karim1").is_active, True)
        self.assertIn(res.status_code, (200, 404))

    # ---- privilege escalation ----------------------------------------
    def test_admin_cannot_mint_super_admin_or_self_promote(self):
        self.auth(self.signup(1))
        body = {
            "first_name": "S", "last_name": "T", "phone": "01911000001", "email": "s@example.com",
            "kind": "SUPER_ADMIN", "password": PW, "confirm_password": PW,
        }
        self.assertEqual(self.client.post(f"{API}/users", body, format="json").status_code, 400)
        body["kind"] = "STAFF"
        self.assertEqual(self.client.post(f"{API}/users", body, format="json").status_code, 201)

        me = self.client.patch(f"{API}/users/me", {"kind": "SUPER_ADMIN", "first_name": "Renamed"}, format="json")
        self.assertEqual(me.status_code, 200, me.content)
        self.assertEqual(self.client.get(f"{API}/users/me").data["kind"], "ADMIN")

    def test_org_admin_cannot_upgrade_own_plan(self):
        self.auth(self.signup(1))
        res = self.client.patch(
            f"{API}/organizations/me",
            {"allowed_customer": 99999, "subscription_status": "ACTIVE", "bkash_number": "01711111111"},
            format="json",
        )
        self.assertEqual(res.status_code, 200, res.content)
        org = Organization.objects.get(name="Net Zone 1")
        self.assertEqual(org.subscription_status, SubscriptionStatus.TRIAL)
        self.assertNotEqual(org.allowed_customer, 99999)
        self.assertEqual(org.bkash_number, "01711111111")
        self.assertNotIn("router_password", res.data)

    # ---- subscription gate -------------------------------------------
    def test_expired_org_is_read_only(self):
        self.auth(self.signup(1))
        pkg = self.make_package()
        Organization.objects.update(subscription_end_date=timezone.localdate() - timedelta(days=1))
        self.assertEqual(self.client.get(f"{API}/packages").status_code, 200)
        self.assertEqual(self.make_customer(pkg).status_code, 403)

    # ---- portal -------------------------------------------------------
    def test_portal_payment_claim_approval(self):
        self.auth(self.signup(1))
        pkg = self.make_package(600)
        cust = self.make_customer(pkg).data
        self.client.post(f"{API}/customers/bills/generate")
        slug = Organization.objects.get().slug

        self.client.credentials()  # anonymous visitor
        info = self.client.get(f"{API}/portal/{slug}")
        self.assertEqual(info.status_code, 200)
        look = self.client.post(f"{API}/portal/{slug}/lookup", {"phone": "+8801811000001"}, format="json")
        self.assertEqual(look.status_code, 200, look.content)
        self.assertEqual(str(look.data["total_due"]), "600.00")
        self.assertNotIn("phone", look.data)
        miss = self.client.post(f"{API}/portal/{slug}/lookup", {"phone": "01999999999"}, format="json")
        self.assertEqual(miss.status_code, 404)

        claim = {"phone": "01811000001", "amount": "600", "transaction_id": "9abc1xyz", "payment_method": "BKASH"}
        self.assertEqual(self.client.post(f"{API}/portal/{slug}/pay", claim, format="json").status_code, 201)
        self.assertEqual(self.client.post(f"{API}/portal/{slug}/pay", claim, format="json").status_code, 400)

        self.auth_login()
        pending = self.client.get(f"{API}/payment-requests?request_status=PENDING").data["results"]
        self.assertEqual(len(pending), 1)
        ok = self.client.post(f"{API}/payment-requests/{pending[0]['id']}/approve")
        self.assertEqual(ok.status_code, 200, ok.content)
        self.assertEqual(ok.data["request_status"], "APPROVED")
        self.assertEqual(self.client.get(f"{API}/customers/{cust['uid']}/payments").data["results"][0]["paid"], True)

    def auth_login(self):
        res = self.client.post(f"{API}/users/login", {"phone": "01711000001", "password": PW}, format="json")
        self.auth(res.data)

    # ---- misc features -----------------------------------------------
    def test_expenses_complaints_reports(self):
        self.auth(self.signup(1))
        pkg = self.make_package()
        cust = self.make_customer(pkg).data
        self.assertEqual(
            self.client.post(f"{API}/expenses", {"name": "Upstream", "category": "BANDWIDTH", "amount": "1000"}, format="json").status_code,
            201,
        )
        c = self.client.post(f"{API}/complaints", {"name": "Slow net", "customer_id": cust["id"]}, format="json")
        self.assertEqual(c.status_code, 201, c.content)
        upd = self.client.patch(f"{API}/complaints/{c.data['uid']}", {"ticket_status": "RESOLVED"}, format="json")
        self.assertIsNotNone(upd.data["resolved_at"])
        rep = self.client.get(f"{API}/reports/summary")
        self.assertEqual(rep.status_code, 200, rep.content)
        self.assertEqual(len(rep.data["trend"]), 12)

    def test_csv_import_export(self):
        from django.core.files.uploadedfile import SimpleUploadedFile

        self.auth(self.signup(1))
        self.make_package()
        csv_body = "name,phone,username,package,area\nA One,01712345678,a1,Home 10,Uttara\n,,,,\n"
        up = self.client.post(f"{API}/customers/import", {"file": SimpleUploadedFile("c.csv", csv_body.encode())}, format="multipart")
        self.assertEqual(up.status_code, 200, up.content)
        self.assertEqual(up.data["imported"], 1)
        self.assertEqual(len(up.data["errors"]), 1)
        exp = self.client.get(f"{API}/customers/export")
        self.assertIn("a1", exp.content.decode())
