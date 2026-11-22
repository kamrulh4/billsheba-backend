"""bKash merchant flows, with bKash itself mocked.

Core guarantee under test: a customer's bill is paid with the *ISP's own* merchant
credentials (money settles in the ISP's account) and a subscription fee with the
*platform's* credentials.
"""

from datetime import timedelta
from decimal import Decimal
from unittest import mock

from django.core.cache import cache
from django.core.management import call_command
from django.test import override_settings
from django.utils import timezone
from rest_framework.test import APITestCase

from core.models import Organization, Subscription, SubscriptionPayment
from customer.models import OnlinePayment, Payment

API = "/api/v1"
PW = "Str0ng!Pass-2026"
ISP_CREDS = {
    "bkash_app_key": "isp-key", "bkash_app_secret": "isp-secret",
    "bkash_username": "isp-user", "bkash_password": "isp-pass",
}
PLATFORM = dict(
    PLATFORM_BKASH_APP_KEY="plat-key", PLATFORM_BKASH_APP_SECRET="plat-secret",
    PLATFORM_BKASH_USERNAME="plat-user", PLATFORM_BKASH_PASSWORD="plat-pass",
)


class FakeBkash:
    """Stands in for requests.post; remembers every call."""

    def __init__(self, execute_amount=None, execute_status="Completed"):
        self.calls = []
        self.execute_amount = execute_amount
        self.execute_status = execute_status
        self.created_amount = None

    def __call__(self, url, json=None, headers=None, timeout=None):
        self.calls.append((url, json, headers))
        out = mock.Mock(status_code=200)
        if url.endswith("/token/grant"):
            out.json.return_value = {"id_token": f"tok-{headers['username']}", "statusCode": "0000"}
        elif url.endswith("/checkout/create"):
            self.created_amount = json["amount"]
            out.json.return_value = {
                "paymentID": f"PAY{len(self.calls)}", "bkashURL": "https://sandbox.bka.sh/pay/xyz",
            }
        elif url.endswith("/checkout/execute"):
            amount = self.execute_amount or self.created_amount
            out.json.return_value = {
                "transactionStatus": self.execute_status, "trxID": "TRX123ABC", "amount": amount,
                "customerMsisdn": "01700000000",
            }
        return out

    def headers_for(self, suffix):
        return [h for (u, _, h) in self.calls if u.endswith(suffix)]


class BkashBase(APITestCase):
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
        self.tokens = res.data
        self.auth()

    def auth(self):
        self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {self.tokens['access_token']}")

    def setup_customer(self, price=600):
        pkg = self.client.post(f"{API}/packages", {"name": "P", "speed_mbps": 10, "price": price}, format="json").data
        self.client.post(
            f"{API}/customers",
            {"name": "Karim", "phone": "01811000001", "username": "karim", "password": "pass1234", "package_id": pkg["id"]},
            format="json",
        )
        self.client.post(f"{API}/customers/bills/generate")
        return Organization.objects.get()

    def save_creds(self):
        return self.client.patch(f"{API}/organizations/me", {**ISP_CREDS, "bkash_sandbox": True}, format="json")

    def start_checkout(self, fake):
        self.client.credentials()
        with mock.patch("common.bkash.requests.post", fake):
            return self.client.post(f"{API}/portal/net-zone/bkash/create", {"phone": "01811000001"}, format="json")


class BkashTests(BkashBase):
    # ---- credentials ------------------------------------------------------
    def test_merchant_credentials_are_encrypted_and_never_returned(self):
        res = self.save_creds()
        self.assertEqual(res.status_code, 200, res.content)
        self.assertTrue(res.data["bkash_configured"])
        for secret in ISP_CREDS:
            self.assertNotIn(secret, res.data)
        org = Organization.objects.get()
        self.assertNotEqual(org.bkash_app_secret, "isp-secret")  # stored encrypted
        self.assertEqual(org.get_bkash_credentials().app_secret, "isp-secret")
        # blank on a later save keeps the stored secret
        self.client.patch(f"{API}/organizations/me", {"bkash_app_secret": ""}, format="json")
        self.assertEqual(Organization.objects.get().get_bkash_credentials().app_secret, "isp-secret")

    def test_online_payment_requires_configuration(self):
        self.setup_customer()
        res = self.start_checkout(FakeBkash())
        self.assertEqual(res.status_code, 400)
        self.assertFalse(self.client.get(f"{API}/portal/net-zone").data["online_payment"])

    # ---- customer pays the ISP -----------------------------------------------
    def test_customer_payment_uses_isp_merchant_account(self):
        self.setup_customer(600)
        self.save_creds()
        fake = FakeBkash()
        res = self.start_checkout(fake)
        self.assertEqual(res.status_code, 200, res.content)
        self.assertEqual(res.data["bkash_url"], "https://sandbox.bka.sh/pay/xyz")
        self.assertEqual(fake.created_amount, "600.00")
        # created with the ISP's merchant app key, on the sandbox host
        self.assertEqual(fake.headers_for("/checkout/create")[0]["X-APP-Key"], "isp-key")
        self.assertIn("sandbox", fake.calls[0][0])
        self.assertTrue(self.client.get(f"{API}/portal/net-zone").data["online_payment"])

        op = OnlinePayment.objects.get()
        with mock.patch("common.bkash.requests.post", fake):
            cb = self.client.get(f"{API}/portal/bkash/callback?paymentID={op.gateway_payment_id}&status=success")
        self.assertEqual(cb.status_code, 302)
        self.assertIn("/portal/net-zone?bkash=success&trx=TRX123ABC", cb["Location"])
        self.assertEqual(fake.headers_for("/checkout/execute")[0]["X-APP-Key"], "isp-key")

        payment = Payment.objects.get()
        self.assertTrue(payment.paid)
        self.assertEqual(payment.amount, Decimal("600.00"))
        self.assertEqual(payment.payment_method, "BKASH")
        self.assertEqual(payment.transaction_id, "TRX123ABC")

        # replaying the callback must not pay twice
        with mock.patch("common.bkash.requests.post", fake):
            self.client.get(f"{API}/portal/bkash/callback?paymentID={op.gateway_payment_id}&status=success")
        self.assertEqual(Payment.objects.count(), 1)
        self.assertEqual(Payment.objects.get().amount, Decimal("600.00"))

    def test_partial_online_payment_leaves_balance_due(self):
        self.setup_customer(600)
        self.save_creds()
        fake = FakeBkash()
        self.client.credentials()
        with mock.patch("common.bkash.requests.post", fake):
            self.client.post(f"{API}/portal/net-zone/bkash/create", {"phone": "01811000001", "amount": "250"}, format="json")
            op = OnlinePayment.objects.get()
            self.client.get(f"{API}/portal/bkash/callback?paymentID={op.gateway_payment_id}&status=success")
        payment = Payment.objects.get()
        self.assertFalse(payment.paid)
        self.assertEqual(payment.amount, Decimal("250.00"))

    def test_amount_over_due_rejected(self):
        self.setup_customer(600)
        self.save_creds()
        self.client.credentials()
        with mock.patch("common.bkash.requests.post", FakeBkash()):
            res = self.client.post(
                f"{API}/portal/net-zone/bkash/create", {"phone": "01811000001", "amount": "9999"}, format="json"
            )
        self.assertEqual(res.status_code, 400)

    def test_tampered_or_failed_execute_does_not_record_payment(self):
        self.setup_customer(600)
        self.save_creds()
        fake = FakeBkash(execute_amount="1.00")  # bKash says a different amount was charged
        self.start_checkout(fake)
        op = OnlinePayment.objects.get()
        with mock.patch("common.bkash.requests.post", fake):
            cb = self.client.get(f"{API}/portal/bkash/callback?paymentID={op.gateway_payment_id}&status=success")
        self.assertIn("bkash=failed", cb["Location"])
        self.assertEqual(Payment.objects.get().amount, Decimal("0.00"))
        self.assertEqual(OnlinePayment.objects.get().online_status, "FAILED")

    def test_cancelled_checkout(self):
        self.setup_customer()
        self.save_creds()
        self.start_checkout(FakeBkash())
        op = OnlinePayment.objects.get()
        cb = self.client.get(f"{API}/portal/bkash/callback?paymentID={op.gateway_payment_id}&status=cancel")
        self.assertIn("bkash=cancelled", cb["Location"])
        self.assertEqual(Payment.objects.get().amount, Decimal("0.00"))

    def test_unknown_payment_id_is_harmless(self):
        cb = self.client.get(f"{API}/portal/bkash/callback?paymentID=nope&status=success")
        self.assertEqual(cb.status_code, 302)
        self.assertEqual(Payment.objects.count(), 0)

    # ---- ISP pays the platform -------------------------------------------------
    @override_settings(**PLATFORM)
    def test_subscription_fee_uses_platform_merchant_account(self):
        premium = Subscription.objects.get(name="Premium")
        self.assertTrue(self.client.get(f"{API}/billing").data["online_bkash"])

        fake = FakeBkash()
        with mock.patch("common.bkash.requests.post", fake):
            res = self.client.post(f"{API}/billing/renew/bkash", {"plan_id": premium.id, "months": 2}, format="json")
            self.assertEqual(res.status_code, 200, res.content)
            self.assertEqual(fake.created_amount, f"{premium.price * 2:.2f}")
            self.assertEqual(fake.headers_for("/checkout/create")[0]["X-APP-Key"], "plat-key")

            sp = SubscriptionPayment.objects.get()
            self.assertEqual(sp.payment_status, "INITIATED")
            # abandoned checkouts must not clutter history / block manual payments
            self.assertEqual(self.client.get(f"{API}/billing").data["history"], [])

            self.client.credentials()
            cb = self.client.get(f"{API}/billing/bkash/callback?paymentID={sp.gateway_payment_id}&status=success")
        self.assertIn("/billing?bkash=success", cb["Location"])

        org = Organization.objects.get()
        self.assertEqual(org.subscription, premium)
        self.assertEqual(org.subscription_status, "ACTIVE")
        self.assertEqual(org.subscription_end_date, timezone.localdate() + timedelta(days=60))
        sp.refresh_from_db()
        self.assertEqual((sp.payment_status, sp.transaction_id), ("APPROVED", "TRX123ABC"))

    def test_subscription_online_unavailable_without_platform_credentials(self):
        premium = Subscription.objects.get(name="Premium")
        self.assertFalse(self.client.get(f"{API}/billing").data["online_bkash"])
        res = self.client.post(f"{API}/billing/renew/bkash", {"plan_id": premium.id, "months": 1}, format="json")
        self.assertEqual(res.status_code, 400)


class ReactivationTests(BkashBase):
    """A customer suspended for non-payment is switched back on when the bill is paid."""

    def suspend_customer(self):
        from customer.models import Customer

        org = Organization.objects.get()
        org.router_ip, org.router_username, org.router_password = "https://10.0.0.1", "admin", "pw"
        org.save()
        # queryset.update() = how the daily job suspends (router already handled there)
        Customer.objects.update(is_active=False)
        return Customer.objects.get()

    def pay_online(self, fake, amount=None):
        body = {"phone": "01811000001"}
        if amount:
            body["amount"] = amount
        self.client.credentials()
        with mock.patch("common.bkash.requests.post", fake):
            self.client.post(f"{API}/portal/net-zone/bkash/create", body, format="json")
            op = OnlinePayment.objects.latest("id")
            self.client.get(f"{API}/portal/bkash/callback?paymentID={op.gateway_payment_id}&status=success")

    def test_full_payment_reenables_customer_on_router(self):
        self.setup_customer(600)
        self.save_creds()
        customer = self.suspend_customer()
        self.assertFalse(customer.is_active)
        with mock.patch("customer.models.Mikrotik.toggle_ppp_user", return_value=(True, "ok")) as toggle:
            self.pay_online(FakeBkash())
        customer.refresh_from_db()
        self.assertTrue(customer.is_active)
        args = toggle.call_args.args
        self.assertEqual((args[0], args[1]), ("karim", False))  # username, disable=False

    def test_partial_payment_keeps_customer_suspended(self):
        self.setup_customer(600)
        self.save_creds()
        customer = self.suspend_customer()
        with mock.patch("customer.models.Mikrotik.toggle_ppp_user", return_value=(True, "ok")) as toggle:
            self.pay_online(FakeBkash(), amount="200")
        customer.refresh_from_db()
        self.assertFalse(customer.is_active)
        toggle.assert_not_called()

    def test_router_outage_does_not_lose_the_payment(self):
        self.setup_customer(600)
        self.save_creds()
        self.suspend_customer()
        with mock.patch("customer.models.Mikrotik.toggle_ppp_user", side_effect=Exception("router down")):
            self.pay_online(FakeBkash())
        self.assertTrue(Payment.objects.get().paid)
