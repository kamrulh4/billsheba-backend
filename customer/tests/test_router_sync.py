"""Editing a customer is carried over to the router (MikroTik mocked)."""

from unittest import mock

import requests
from django.core.cache import cache
from rest_framework.test import APITestCase

from core.models import AuditLog, Organization, User
from customer.models import Customer, Package

MK = "customer.helpers.mikrotik.requests"
PW = "Str0ng!Pass-2026"


def resp(status=200, json=None, text=""):
    r = mock.Mock(status_code=status, text=text)
    r.json.return_value = json
    return r


class CustomerRouterSyncTests(APITestCase):
    def setUp(self):
        cache.clear()
        self.org = Organization.objects.create(
            name="Net", phone="01711000001", subscription_status="ACTIVE",
            router_ip="http://10.0.0.1", router_username="api", router_password="secret",
        )
        self.admin = User.objects.create_user(
            "A", "B", "01711000002", PW, organization=self.org, kind="ADMIN", email="a@net.example"
        )
        self.slow = Package.objects.create(organization=self.org, name="10Mbps", price=500)
        self.fast = Package.objects.create(organization=self.org, name="20Mbps", price=800)
        self.customer = Customer.objects.create(
            organization=self.org, name="Karim", phone="01911000001", username="karim",
            password="oldpass", package=self.slow, connection_type="PPPoE",
        )
        self.client.force_authenticate(self.admin)

    def edit(self, body, found=True, patched=None, sessions=None):
        secrets = [{".id": "*1", "name": "karim"}] if found else []
        with mock.patch(f"{MK}.post", return_value=resp(200, secrets)), mock.patch(
            f"{MK}.patch", return_value=patched or resp(200, {})
        ) as patch, mock.patch(f"{MK}.get", return_value=resp(200, sessions or [])), mock.patch(
            f"{MK}.delete", return_value=resp(204)
        ) as delete:
            res = self.client.patch(f"/api/v1/customers/{self.customer.uid}", body, format="json")
        self.customer.refresh_from_db()
        return res, patch, delete

    def test_package_change_sets_profile_and_reconnects(self):
        res, patch, delete = self.edit(
            {"package_id": self.fast.id}, sessions=[{".id": "*A", "name": "karim"}]
        )
        self.assertEqual(res.status_code, 200, res.content)
        self.assertEqual(patch.call_args.kwargs["json"], {"profile": "20Mbps"})
        self.assertIn("/rest/ppp/active/*A", delete.call_args.args[0])
        self.assertEqual(res.data["router"], {"status": "updated", "changed": ["profile"], "reconnect": "done"})
        self.assertEqual(self.customer.package, self.fast)

    def test_username_and_password_change_without_disconnecting(self):
        res, patch, delete = self.edit(
            {"username": "karim.new", "password": "newpass1", "package_id": self.fast.id},
            sessions=[{".id": "*A", "name": "karim"}],
        )
        self.assertEqual(res.status_code, 200, res.content)
        self.assertEqual(
            patch.call_args.kwargs["json"], {"name": "karim.new", "password": "newpass1", "profile": "20Mbps"}
        )
        delete.assert_not_called()
        self.assertEqual(self.customer.username, "karim.new")
        log = AuditLog.objects.get(action="customer.router_update")
        self.assertIn("karim -> karim.new", log.detail)
        self.assertNotIn("newpass1", log.detail)

    def test_full_save_with_nothing_new_leaves_the_router_alone(self):
        res, patch, _ = self.edit(
            {"name": "Karim Uddin", "username": "karim", "password": "oldpass", "package_id": self.slow.id}
        )
        self.assertEqual(res.status_code, 200, res.content)
        patch.assert_not_called()
        self.assertNotIn("router", res.data)
        self.assertEqual(self.customer.name, "Karim Uddin")

    def test_router_refusal_saves_nothing(self):
        res, _, _ = self.edit(
            {"package_id": self.fast.id, "name": "Changed"},
            patched=resp(400, {"detail": "input does not match any value of profile"}),
        )
        self.assertEqual(res.status_code, 400)
        self.assertIn("no PPP profile '20Mbps'", str(res.data))
        self.assertEqual((self.customer.package, self.customer.name), (self.slow, "Karim"))

    def test_unreachable_router_saves_nothing(self):
        with mock.patch(f"{MK}.post", side_effect=requests.exceptions.ConnectionError("down")):
            res = self.client.patch(
                f"/api/v1/customers/{self.customer.uid}", {"password": "newpass1"}, format="json"
            )
        self.assertEqual(res.status_code, 400)
        self.customer.refresh_from_db()
        self.assertEqual(self.customer.password, "oldpass")

    def test_user_missing_on_router_is_saved_with_a_warning(self):
        res, patch, _ = self.edit({"password": "newpass1"}, found=False)
        self.assertEqual(res.status_code, 200, res.content)
        patch.assert_not_called()
        self.assertEqual(res.data["router"]["status"], "not_found")
        self.assertEqual(self.customer.password, "newpass1")

    def test_username_already_taken(self):
        Customer.objects.create(organization=self.org, name="R", username="rahim")
        res, patch, _ = self.edit({"username": "rahim"})
        self.assertEqual(res.status_code, 400)
        patch.assert_not_called()

    def test_no_router_connected_saves_directly(self):
        Organization.objects.filter(pk=self.org.pk).update(router_ip="")
        res, patch, _ = self.edit({"username": "karim2"})
        self.assertEqual(res.status_code, 200, res.content)
        patch.assert_not_called()
        self.assertEqual(self.customer.username, "karim2")
