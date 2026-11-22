"""Router (MikroTik) and SMS gateway clients, with the network mocked."""

import os
from unittest import mock

import requests
from django.core.management import call_command
from django.test import SimpleTestCase, TestCase

from common.helpers import sms_helper
from core.models import Organization
from customer.helpers import Mikrotik
from customer.models import Customer
from customer.tests.test_reliability import Base

MK = "customer.helpers.mikrotik.requests"


def resp(status=200, data=None, text=""):
    r = mock.Mock(status_code=status, text=text)
    r.json.return_value = data if data is not None else {}
    return r


class MikrotikTests(Base):
    def setUp(self):
        super().setUp()
        self.org.router_ip, self.org.router_username, self.org.router_password = "https://10.0.0.1", "api", "pw"
        self.org.save()
        self.org = Organization.objects.get()

    def test_unconfigured_router_is_refused_without_network(self):
        bare = Organization.objects.create(name="Bare", phone="01700000001")
        with mock.patch(f"{MK}.post") as post, mock.patch(f"{MK}.get") as get, mock.patch(f"{MK}.put") as put:
            for result in (
                Mikrotik.get_user_by_username("u", bare),
                Mikrotik.get_users_from_server(bare),
                Mikrotik.create_ppp_user({"username": "u", "password": "p"}, bare),
                Mikrotik.delete_ppp_user("u", bare),
                Mikrotik.toggle_ppp_user("u", True, bare),
            ):
                self.assertEqual(result, (False, "Router configuration is not added yet"))
            self.assertEqual(Mikrotik.get_user_sessions(bare), (False, []))
            self.assertFalse(Mikrotik.delete_user_session("1", bare))
        for client in (post, get, put):
            client.assert_not_called()

    def test_lookup_uses_decrypted_credentials_timeout_and_tls_setting(self):
        with mock.patch(f"{MK}.post", return_value=resp(200, [{".id": "*1", "name": "karim"}])) as post:
            ok, data = Mikrotik.get_user_by_username("karim", self.org)
        self.assertEqual((ok, data[".id"]), (True, "*1"))
        kwargs = post.call_args.kwargs
        self.assertEqual(kwargs["auth"], ("api", "pw"))  # stored encrypted, sent decrypted
        self.assertEqual(kwargs["timeout"], 15)
        self.assertIs(kwargs["verify"], False)
        self.assertTrue(post.call_args.args[0].startswith("https://10.0.0.1/rest/ppp/secret"))
        with mock.patch(f"{MK}.post", return_value=resp(200, [{".id": "*1"}])) as post, mock.patch(
            "customer.helpers.mikrotik.MIKROTIK_VERIFY_SSL", True
        ):
            Mikrotik.get_user_by_username("karim", self.org)
        self.assertIs(post.call_args.kwargs["verify"], True)  # TLS verification can be switched on

    def test_lookup_failure_modes(self):
        with mock.patch(f"{MK}.post", return_value=resp(200, [])):
            self.assertFalse(Mikrotik.get_user_by_username("x", self.org)[0])
        with mock.patch(f"{MK}.post", return_value=resp(500, text="boom")):
            self.assertIn("HTTP 500", Mikrotik.get_user_by_username("x", self.org)[1])
        with mock.patch(f"{MK}.post", side_effect=requests.exceptions.Timeout()):
            self.assertEqual(Mikrotik.get_user_by_username("x", self.org), (False, "Request timed out"))
        with mock.patch(f"{MK}.post", side_effect=requests.exceptions.ConnectionError("down")):
            self.assertIn("Network error", Mikrotik.get_user_by_username("x", self.org)[1])
        self.assertEqual(Mikrotik.get_user_by_username("", self.org), (False, "Username is required"))

    def test_create_ppp_user(self):
        with mock.patch(f"{MK}.put", return_value=resp(201)) as put:
            ok, _ = Mikrotik.create_ppp_user({"username": "u", "password": "p", "profile": "10M"}, self.org)
        self.assertTrue(ok)
        self.assertEqual(put.call_args.kwargs["json"]["profile"], "10M")
        with mock.patch(f"{MK}.put", return_value=resp(400, {"detail": "already exists"})):
            self.assertEqual(Mikrotik.create_ppp_user({"username": "u", "password": "p"}, self.org), (False, "already exists"))
        self.assertFalse(Mikrotik.create_ppp_user({"username": "u"}, self.org)[0])

    def test_disable_kicks_active_session_enable_does_not(self):
        sessions = [{"name": "other", ".id": "*9"}, {"name": "karim", ".id": "*7"}]
        with mock.patch(f"{MK}.post", return_value=resp(200, [{".id": "*1"}])), mock.patch(
            f"{MK}.patch", return_value=resp(200)
        ) as patch, mock.patch(f"{MK}.get", return_value=resp(200, sessions)), mock.patch(
            f"{MK}.delete", return_value=resp(204)
        ) as delete:
            ok, msg = Mikrotik.toggle_ppp_user("karim", True, self.org)
        self.assertTrue(ok, msg)
        self.assertEqual(patch.call_args.kwargs["json"], {"disabled": "true"})
        self.assertTrue(delete.call_args.args[0].endswith("/rest/ppp/active/*7"))  # only karim's session

        with mock.patch(f"{MK}.post", return_value=resp(200, [{".id": "*1"}])), mock.patch(
            f"{MK}.patch", return_value=resp(200)
        ) as patch, mock.patch(f"{MK}.get") as get, mock.patch(f"{MK}.delete") as delete:
            ok, _ = Mikrotik.toggle_ppp_user("karim", False, self.org)
        self.assertTrue(ok)
        self.assertEqual(patch.call_args.kwargs["json"], {"disabled": "false"})
        get.assert_not_called()
        delete.assert_not_called()

    def test_toggle_reports_router_errors(self):
        with mock.patch(f"{MK}.post", return_value=resp(200, [{".id": "*1"}])), mock.patch(
            f"{MK}.patch", return_value=resp(400, {"message": "no such item"})
        ):
            ok, msg = Mikrotik.toggle_ppp_user("karim", True, self.org)
        self.assertFalse(ok)
        self.assertIn("no such item", msg)
        with mock.patch(f"{MK}.post", return_value=resp(200, [])):
            self.assertFalse(Mikrotik.toggle_ppp_user("ghost", True, self.org)[0])

    def test_delete_ppp_user_and_sessions(self):
        with mock.patch(f"{MK}.post", return_value=resp(200, [{".id": "*1"}])), mock.patch(
            f"{MK}.delete", return_value=resp(204)
        ):
            self.assertTrue(Mikrotik.delete_ppp_user("karim", self.org)[0])
        with mock.patch(f"{MK}.post", return_value=resp(200, [{".id": "*1"}])), mock.patch(
            f"{MK}.delete", return_value=resp(500, text="no")
        ):
            self.assertFalse(Mikrotik.delete_ppp_user("karim", self.org)[0])
        with mock.patch(f"{MK}.get", return_value=resp(200, [{"name": "a"}])):
            self.assertEqual(Mikrotik.get_user_sessions(self.org), (True, [{"name": "a"}]))
        with mock.patch(f"{MK}.get", return_value=resp(500)):
            self.assertEqual(Mikrotik.get_user_sessions(self.org), (False, []))
        with mock.patch(f"{MK}.get", return_value=resp(200, [{"id": 1}])):
            self.assertTrue(Mikrotik.get_users_from_server(self.org)[0])
        with mock.patch(f"{MK}.delete", return_value=resp(200)):
            self.assertTrue(Mikrotik.delete_user_session("*7", self.org))
        with mock.patch(f"{MK}.delete", side_effect=Exception("x")):
            self.assertFalse(Mikrotik.delete_user_session("*7", self.org))


class SmsGatewayTests(SimpleTestCase):
    def test_without_api_key_nothing_is_sent(self):
        with mock.patch.object(sms_helper, "SMS_API_KEY", ""), mock.patch("common.helpers.sms_helper.requests.post") as post:
            self.assertFalse(sms_helper.SMS.send_single_sms("8801711000001", "hi"))
            self.assertFalse(sms_helper.SMS.send_bulk_sms([{"to": "8801711000001", "message": "hi"}]))
        post.assert_not_called()

    def test_single_and_bulk_success_and_failure(self):
        # the real gateway: HTTP 200, "accepted" is response_code 202 inside the body
        ok = mock.Mock(status_code=200, text="")
        ok.raise_for_status.return_value = None
        ok.json.return_value = {"response_code": 202, "success_message": "SMS Submitted Successfully 1"}
        with mock.patch.object(sms_helper, "SMS_API_KEY", "key"), mock.patch(
            "common.helpers.sms_helper.requests.post", return_value=ok
        ) as post:
            self.assertTrue(sms_helper.SMS.send_single_sms("8801711000001", "hello"))
            self.assertEqual(post.call_args.kwargs["data"]["api_key"], "key")
            self.assertEqual(post.call_args.kwargs["data"]["number"], "8801711000001")
            self.assertTrue(sms_helper.SMS.send_bulk_sms([{"to": "8801711000001", "message": "m"}]))
        rejected = mock.Mock(status_code=200, text="rejected")
        rejected.raise_for_status.return_value = None
        rejected.json.return_value = {"response_code": 1007, "error_message": "Balance Insufficient"}
        with mock.patch.object(sms_helper, "SMS_API_KEY", "key"), mock.patch(
            "common.helpers.sms_helper.requests.post", return_value=rejected
        ):
            self.assertFalse(sms_helper.SMS.send_single_sms("8801711000001", "x"))  # refused inside a 200
        with mock.patch.object(sms_helper, "SMS_API_KEY", "key"), mock.patch(
            "common.helpers.sms_helper.requests.post", side_effect=requests.RequestException("down")
        ):
            self.assertFalse(sms_helper.SMS.send_single_sms("8801711000001", "x"))
            self.assertFalse(sms_helper.SMS.send_bulk_sms([{"to": "1", "message": "m"}]))


class SeedCommandTests(TestCase):
    def test_seed_demo_builds_a_realistic_isp_and_is_rerunnable(self):
        devnull = open(os.devnull, "w")
        call_command("seed_plans", stdout=devnull)
        call_command("seed_demo", stdout=devnull)
        call_command("seed_demo", stdout=devnull)  # rebuilds, never duplicates
        org = Organization.objects.get(name="Demo Broadband")
        self.assertEqual(Organization.objects.filter(name="Demo Broadband").count(), 1)
        self.assertEqual(Customer.objects.filter(organization=org).count(), 60)
        self.assertEqual(org.payments.count(), 360)
        self.assertGreater(org.payments.filter(paid=True).count(), 250)
        self.assertEqual(Customer.objects.filter(organization=org, is_active=False).count(), 4)
        self.assertEqual(org.users.get().phone, "01700000000")
