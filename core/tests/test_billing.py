from datetime import timedelta

from django.core.cache import cache
from django.core.management import call_command
from django.utils import timezone
from rest_framework.test import APITestCase

from core.choices import SubscriptionStatus
from core.models import Organization, Subscription, User

API = "/api/v1"
PW = "Str0ng!Pass-2026"


class SubscriptionRenewalTests(APITestCase):
    @classmethod
    def setUpTestData(cls):
        call_command("seed_plans", verbosity=0)
        User.objects.create_superuser("Plat", "Owner", "01999000000", PW)

    def setUp(self):
        cache.clear()

    def login(self, phone):
        res = self.client.post(f"{API}/users/login", {"phone": phone, "password": PW}, format="json")
        self.assertEqual(res.status_code, 200, res.content)
        self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {res.data['access_token']}")

    def signup(self):
        res = self.client.post(
            f"{API}/auth/signup",
            {
                "organization_name": "Net Zone", "first_name": "R", "phone": "01711000001",
                "email": "o@example.com", "password": PW, "confirm_password": PW,
            },
            format="json",
        )
        self.assertEqual(res.status_code, 201, res.content)
        return res.data

    def test_renewal_flow_activates_plan(self):
        tokens = self.signup()
        self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {tokens['access_token']}")
        premium = Subscription.objects.get(name="Premium")

        info = self.client.get(f"{API}/billing").data
        self.assertEqual(info["current"]["status"], "TRIAL")
        self.assertEqual(len(info["plans"]), 3)  # paid plans only

        body = {"plan_id": premium.id, "months": 2, "payment_method": "BKASH", "transaction_id": "tx123abc"}
        self.assertEqual(self.client.post(f"{API}/billing/renew", body, format="json").status_code, 201)
        self.assertEqual(self.client.post(f"{API}/billing/renew", body, format="json").status_code, 400)  # duplicate trx
        free = Subscription.objects.get(name="Starter")
        bad = dict(body, plan_id=free.id, transaction_id="other")
        self.assertEqual(self.client.post(f"{API}/billing/renew", bad, format="json").status_code, 400)

        # an ISP cannot approve its own payment
        pid = self.client.get(f"{API}/billing").data["history"][0]["id"]
        self.assertEqual(self.client.post(f"{API}/billing/platform/payments/{pid}/approve").status_code, 403)

        self.client.credentials()
        self.login("01999000000")
        stats = self.client.get(f"{API}/billing/platform/stats").data
        self.assertEqual(stats["pending_payments"], 1)
        self.assertEqual(self.client.get(f"{API}/billing/platform/payments?payment_status=PENDING").data["count"], 1)

        ok = self.client.post(f"{API}/billing/platform/payments/{pid}/approve")
        self.assertEqual(ok.status_code, 200, ok.content)

        org = Organization.objects.get()
        self.assertEqual(org.subscription, premium)
        self.assertEqual(org.subscription_status, SubscriptionStatus.ACTIVE)
        self.assertEqual(org.subscription_end_date, timezone.localdate() + timedelta(days=60))
        self.assertEqual(org.allowed_customer, premium.max_customers)
        self.assertTrue(org.sms_feature)
        # already reviewed
        self.assertEqual(self.client.post(f"{API}/billing/platform/payments/{pid}/reject").status_code, 404)

    def test_expired_org_can_renew_and_regain_write_access(self):
        tokens = self.signup()
        Organization.objects.update(
            subscription_status=SubscriptionStatus.EXPIRED,
            subscription_end_date=timezone.localdate() - timedelta(days=3),
        )
        self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {tokens['access_token']}")
        pkg = {"name": "P", "speed_mbps": 5, "price": 300}
        self.assertEqual(self.client.post(f"{API}/packages", pkg, format="json").status_code, 403)

        basic = Subscription.objects.get(name="Basic")
        self.client.post(
            f"{API}/billing/renew",
            {"plan_id": basic.id, "payment_method": "NAGAD", "transaction_id": "nag1"},
            format="json",
        )
        pid = self.client.get(f"{API}/billing").data["history"][0]["id"]
        self.client.credentials()
        self.login("01999000000")
        self.client.post(f"{API}/billing/platform/payments/{pid}/approve")

        self.client.credentials()
        self.login("01711000001")
        self.assertEqual(self.client.post(f"{API}/packages", pkg, format="json").status_code, 201)

    def test_platform_endpoints_are_super_admin_only(self):
        tokens = self.signup()
        self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {tokens['access_token']}")
        self.assertEqual(self.client.get(f"{API}/billing/platform/stats").status_code, 403)
        self.assertEqual(self.client.get(f"{API}/billing/platform/payments").status_code, 403)


class AuthStatusTests(APITestCase):
    def test_missing_or_bad_token_is_401(self):
        self.assertEqual(self.client.get(f"{API}/customers").status_code, 401)
        self.client.credentials(HTTP_AUTHORIZATION="Bearer not-a-token")
        self.assertEqual(self.client.get(f"{API}/customers").status_code, 401)
