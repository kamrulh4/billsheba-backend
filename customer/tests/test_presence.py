"""Online / offline status of customers, read from the router's active PPPoE sessions."""

from unittest import mock

from customer.tests.test_reliability import API, Base

URL = f"{API}/organizations/me/presence"
GET = "core.views.organization.Mikrotik.get_active_names"
ACTIVE = {"karim": {"address": "10.0.0.5", "uptime": "1h2m"}, "rahim": {"address": "10.0.0.6", "uptime": "5m"}}


class PresenceTests(Base):
    def setUp(self):
        super().setUp()
        self.org.router_ip, self.org.router_username, self.org.router_password = "https://10.0.0.1", "a", "b"
        self.org.save()

    def test_not_configured_is_not_an_error(self):
        self.org.router_ip = ""
        self.org.save()
        res = self.client.get(URL)
        self.assertEqual(res.status_code, 200)
        self.assertFalse(res.data["configured"])
        self.assertIsNone(res.data["online"])

    def test_lists_connected_usernames(self):
        with mock.patch(GET, return_value=(True, ACTIVE)):
            res = self.client.get(URL)
        self.assertEqual(res.status_code, 200, res.content)
        self.assertEqual(res.data["count"], 2)
        self.assertIn("karim", res.data["online"])
        self.assertFalse(res.data["stale"])

    def test_router_is_asked_once_per_window_however_many_requests(self):
        with mock.patch(GET, return_value=(True, ACTIVE)) as m:
            for _ in range(5):
                self.client.get(URL)
        self.assertEqual(m.call_count, 1)

    def test_unreachable_router_returns_last_known_as_stale(self):
        from django.core.cache import cache

        with mock.patch(GET, return_value=(True, ACTIVE)):
            self.client.get(URL)
        cache.delete(f"presence:{self.org.pk}")  # the fresh window ended
        with mock.patch(GET, return_value=(False, "Request timed out")):
            res = self.client.get(URL)
        self.assertEqual(res.status_code, 200)
        self.assertTrue(res.data["stale"])
        self.assertEqual(res.data["count"], 2)

    def test_unreachable_with_nothing_known_is_unknown(self):
        with mock.patch(GET, return_value=(False, "Request timed out")):
            res = self.client.get(URL)
        self.assertEqual(res.status_code, 200)
        self.assertIsNone(res.data["online"])
        self.assertTrue(res.data["stale"])

    def test_other_isp_cannot_see_this_isps_presence(self):
        from django.core.cache import cache

        with mock.patch(GET, return_value=(True, ACTIVE)):
            self.client.get(URL)
        self.assertIsNone(cache.get("presence:999999"))
