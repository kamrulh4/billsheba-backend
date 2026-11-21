"""/pay-bill: a customer types only a mobile number and is taken to the right ISP."""

from django.core.cache import cache
from rest_framework.test import APIClient

from core.models import Organization
from customer.models import Customer
from customer.tests.test_reliability import API, Base

FIND = f"{API}/portal/find"


class PayBillFindTests(Base):
    def setUp(self):
        super().setUp()
        self.anon = APIClient()

    def find(self, phone):
        return self.anon.post(FIND, {"phone": phone}, format="json")

    def test_finds_the_one_isp_of_a_number(self):
        res = self.find("01811000001")
        self.assertEqual(res.status_code, 200, res.content)
        self.assertEqual(res.data["isps"], [{"name": "Net Zone", "slug": self.org.slug}])

    def test_accepts_the_plus_88_form(self):
        self.assertEqual(len(self.find("+8801811000001").data["isps"]), 1)

    def test_unknown_number_finds_nothing(self):
        self.assertEqual(self.find("01911999999").data["isps"], [])

    def test_a_number_in_two_isps_lists_both_so_the_customer_chooses(self):
        other = Organization.objects.create(name="Other Net", slug="other-net", phone="01700000009")
        Customer.objects.create(organization=other, name="Karim", phone="01811000001", username="k2", password="pw12345")
        names = [i["name"] for i in self.find("01811000001").data["isps"]]
        self.assertEqual(names, ["Net Zone", "Other Net"])

    def test_only_isp_names_leak_no_customer_data(self):
        res = self.find("01811000001")
        self.assertEqual(set(res.data), {"isps"})
        self.assertNotIn("Karim", str(res.data))

    def test_invalid_number_is_rejected(self):
        self.assertEqual(self.find("12345").status_code, 400)

    def test_searching_is_rate_limited(self):
        cache.clear()
        codes = [self.find("01811000001").status_code for _ in range(12)]
        self.assertEqual(codes[:10], [200] * 10)
        self.assertEqual(codes[-1], 429)
