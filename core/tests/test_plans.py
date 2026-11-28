from django.core.cache import cache
from django.core.management import call_command
from rest_framework.test import APITestCase

from core.models import Organization, Subscription, User

API = "/api/v1"
PW = "Str0ng!Pass-2026"


class PlanEditingTests(APITestCase):
    @classmethod
    def setUpTestData(cls):
        call_command("seed_plans", verbosity=0)
        cls.basic = Subscription.objects.get(name="Basic")
        cls.owner = User.objects.create_superuser("Plat", "Owner", "01999000000", PW)
        cls.normal = Organization.objects.create(
            name="Normal", phone="01711000001", subscription=cls.basic, allowed_customer=200
        )
        cls.special = Organization.objects.create(
            name="Special", phone="01711000002", subscription=cls.basic, allowed_customer=350
        )
        cls.admin = User.objects.create_user(
            "A", "B", "01711000003", PW, organization=cls.normal, kind="ADMIN", email="a@normal.example"
        )

    def setUp(self):
        cache.clear()

    def url(self):
        return f"{API}/subscriptions/{self.basic.uid}"

    def test_public_list_shows_numbers(self):
        res = self.client.get(f"{API}/subscriptions")
        self.assertEqual(res.status_code, 200)
        self.assertEqual([p["name"] for p in res.data], ["Basic", "Standard", "Premium"])
        self.assertEqual(res.data[0]["sms_monthly_quota"], 500)

    def test_only_platform_owner_can_edit(self):
        self.assertIn(self.client.patch(self.url(), {"price": 1}, format="json").status_code, (401, 403))
        self.client.force_authenticate(self.admin)
        self.assertEqual(self.client.patch(self.url(), {"price": 1}, format="json").status_code, 403)

    def test_limit_change_follows_the_plan_but_keeps_custom_limits(self):
        self.client.force_authenticate(self.owner)
        res = self.client.patch(self.url(), {"price": 1200, "max_customers": 250}, format="json")
        self.assertEqual(res.status_code, 200, res.content)
        self.normal.refresh_from_db()
        self.special.refresh_from_db()
        self.assertEqual(self.normal.customer_limit, 250)
        self.assertEqual(self.special.customer_limit, 350)

    def test_rejects_bad_numbers(self):
        self.client.force_authenticate(self.owner)
        for body in ({"price": -1}, {"max_customers": 0}, {"max_users": 0}):
            self.assertEqual(self.client.patch(self.url(), body, format="json").status_code, 400, body)
