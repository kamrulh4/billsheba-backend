"""Portal OTP, receipts, payments CSV, audit trail."""

import re
from unittest import mock

from core.models import AuditLog, Organization
from customer.models import Payment, SMSLog
from customer.services import billing
from customer.tests.test_reliability import API, Base


class PortalOtpTests(Base):
    def enable_otp(self):
        Organization.objects.update(sms_feature=True, sms_quota_override=1000, portal_otp_required=True)
        billing.generate_bills(Organization.objects.get())
        self.client.credentials()

    def request_code(self, phone="01811000001"):
        with mock.patch("common.helpers.sms_helper.SMS.send_single_sms", return_value=True):
            return self.client.post(f"{API}/portal/net-zone/otp", {"phone": phone}, format="json")

    def test_lookup_blocked_until_phone_verified(self):
        self.enable_otp()
        self.assertTrue(self.client.get(f"{API}/portal/net-zone").data["otp_required"])
        res = self.client.post(f"{API}/portal/net-zone/lookup", {"phone": "01811000001"}, format="json")
        self.assertEqual(res.status_code, 401)

        self.assertEqual(self.request_code().status_code, 200)
        code = re.match(r"(\d{6})", SMSLog.objects.get(message__contains="ভেরিফিকেশন").message).group(1)

        bad = self.client.post(f"{API}/portal/net-zone/verify", {"phone": "01811000001", "otp": "000000"}, format="json")
        self.assertEqual(bad.status_code, 400)
        ok = self.client.post(f"{API}/portal/net-zone/verify", {"phone": "01811000001", "otp": code}, format="json")
        self.assertEqual(ok.status_code, 200, ok.content)
        token = ok.data["token"]

        look = self.client.post(f"{API}/portal/net-zone/lookup", {"phone": "01811000001", "token": token}, format="json")
        self.assertEqual(look.status_code, 200)
        # a token is bound to the phone it was issued for
        other = self.client.post(f"{API}/portal/net-zone/lookup", {"phone": "01899999999", "token": token}, format="json")
        self.assertEqual(other.status_code, 401)
        # and a code works once
        again = self.client.post(f"{API}/portal/net-zone/verify", {"phone": "01811000001", "otp": code}, format="json")
        self.assertEqual(again.status_code, 400)

    def test_unknown_number_gets_same_reply_and_no_sms(self):
        self.enable_otp()
        res = self.request_code("01899999999")
        self.assertEqual(res.status_code, 200)
        self.assertEqual(SMSLog.objects.filter(message__contains="ভেরিফিকেশন").count(), 0)

    def test_code_requests_are_rate_limited_and_tries_capped(self):
        self.enable_otp()
        self.request_code()
        self.assertEqual(self.request_code().status_code, 400)  # within the minute
        for _ in range(5):
            self.client.post(f"{API}/portal/net-zone/verify", {"phone": "01811000001", "otp": "111111"}, format="json")
        code = re.match(r"(\d{6})", SMSLog.objects.get(message__contains="ভেরিফিকেশন").message).group(1)
        locked = self.client.post(f"{API}/portal/net-zone/verify", {"phone": "01811000001", "otp": code}, format="json")
        self.assertEqual(locked.status_code, 400)  # too many wrong guesses burns the code

    def test_portal_unchanged_when_otp_off(self):
        billing.generate_bills(self.org)
        self.client.credentials()
        res = self.client.post(f"{API}/portal/net-zone/lookup", {"phone": "01811000001"}, format="json")
        self.assertEqual(res.status_code, 200)


class ReceiptAndExportTests(Base):
    def pay(self):
        res = self.client.post(
            f"{API}/payments", {"customer_id": self.customer.id, "amount": "600", "payment_method": "CASH"}, format="json"
        )
        self.assertEqual(res.status_code, 201, res.content)
        return Payment.objects.get()

    def test_receipt_has_everything_for_printing(self):
        payment = self.pay()
        res = self.client.get(f"{API}/payments/{payment.uid}/receipt")
        self.assertEqual(res.status_code, 200, res.content)
        self.assertEqual(res.data["receipt_no"], f"R{payment.pk:06d}")
        self.assertEqual(res.data["organization"]["name"], "Net Zone")
        self.assertEqual(res.data["customer"]["username"], "karim")
        self.assertTrue(res.data["paid"])

    def test_receipt_is_tenant_scoped(self):
        payment = self.pay()
        other = self.client.post(
            f"{API}/auth/signup",
            {"organization_name": "Other", "first_name": "O", "phone": "01722000002", "email": "x@e.com",
             "password": "Str0ng!Pass-2026", "confirm_password": "Str0ng!Pass-2026"},
            format="json",
        ).data
        self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {other['access_token']}")
        self.assertEqual(self.client.get(f"{API}/payments/{payment.uid}/receipt").status_code, 404)

    def test_payments_csv_export(self):
        self.pay()
        res = self.client.get(f"{API}/payments/export")
        self.assertEqual(res.status_code, 200)
        text = b"".join(res.streaming_content).decode()  # the export is streamed, not buffered
        self.assertTrue(text.startswith("\ufeffDate,Customer"))  # BOM so Excel shows Bangla correctly
        self.assertIn("karim", text)
        self.assertIn("600", text)
        self.assertIn("payments_", res["Content-Disposition"])


class AuditTrailTests(Base):
    def test_money_and_permission_changes_are_recorded(self):
        self.client.post(
            f"{API}/payments", {"customer_id": self.customer.id, "amount": "600", "payment_method": "CASH"}, format="json"
        )
        payment = Payment.objects.get()
        self.client.patch(f"{API}/payments/{payment.uid}", {"note": "x"}, format="json")
        self.client.post(f"{API}/customers/status/toggle", {"username": "karim", "is_active": False}, format="json")
        self.new_user("STAFF", "01911000001")
        self.client.delete(f"{API}/payments/{payment.uid}")

        actions = list(AuditLog.objects.values_list("action", flat=True))
        for expected in ("customer.create", "payment.create", "payment.update", "customer.disable", "user.create", "payment.delete"):
            self.assertIn(expected, actions)
        entry = AuditLog.objects.get(action="user.create")
        self.assertEqual(entry.organization_id, self.org.id)
        self.assertIn("STAFF", entry.detail)
        self.assertIsNotNone(entry.user)

    def test_settings_audit_never_stores_secret_values(self):
        self.client.patch(f"{API}/organizations/me", {"bkash_app_secret": "super-secret-value"}, format="json")
        entry = AuditLog.objects.get(action="settings.update")
        self.assertIn("bkash_app_secret", entry.detail)
        self.assertNotIn("super-secret-value", entry.detail)

    def test_audit_list_is_scoped_and_admin_only(self):
        self.new_user("STAFF", "01911000001")
        res = self.client.get(f"{API}/organizations/audit-logs")
        self.assertEqual(res.status_code, 200)
        self.assertGreater(res.data["count"], 0)
        self.login("01911000001")
        self.assertEqual(self.client.get(f"{API}/organizations/audit-logs").status_code, 403)


class PortalHistoryTests(Base):
    def test_subscriber_sees_recent_payments(self):
        billing.generate_bills(self.org)
        self.client.post(
            f"{API}/payments", {"customer_id": self.customer.id, "amount": "250", "payment_method": "BKASH"}, format="json"
        )
        self.client.credentials()
        res = self.client.post(f"{API}/portal/net-zone/lookup", {"phone": "01811000001"}, format="json")
        self.assertEqual(res.status_code, 200)
        self.assertEqual(len(res.data["history"]), 1)
        self.assertEqual(str(res.data["history"][0]["amount"]), "250.00")
        self.assertEqual(res.data["history"][0]["method"], "BKASH")


class SubscriptionReminderTests(Base):
    def test_isp_is_warned_before_trial_ends_once_per_day(self):
        from datetime import timedelta

        from django.utils import timezone

        from core.services.billing import send_subscription_reminders

        Organization.objects.update(subscription_end_date=timezone.localdate() + timedelta(days=3))
        with mock.patch("customer.services.sms.SMS.send_single_sms", return_value=True) as sms:
            self.assertEqual(send_subscription_reminders(), {"sent": 1})
            self.assertEqual(send_subscription_reminders(), {"sent": 0})
        self.assertEqual(sms.call_count, 1)
        self.assertIn("3 দিনে", sms.call_args.kwargs["message"])

    def test_no_warning_when_plenty_of_time_left(self):
        from core.services.billing import send_subscription_reminders

        self.assertEqual(send_subscription_reminders(), {"sent": 0})


class LeadTests(Base):
    def setUp(self):
        super().setUp()
        self.client.credentials()

    def post(self, **over):
        body = {"name": "Rahim", "company": "Star Net", "phone": "+8801755500011", "topic": "demo",
                "message": "Need demo", "customers_estimate": 400}
        body.update(over)
        return self.client.post(f"{API}/leads", body, format="json")

    def test_public_form_stores_lead_with_normalised_phone(self):
        from core.models import Lead

        res = self.post()
        self.assertEqual(res.status_code, 201, res.content)
        lead = Lead.objects.get()
        self.assertEqual((lead.phone, lead.lead_status, lead.customers_estimate), ("01755500011", "NEW", 400))

    def test_bad_phone_rejected_and_honeypot_ignored(self):
        from core.models import Lead

        self.assertEqual(self.post(phone="123").status_code, 400)
        self.assertEqual(self.post(website="http://spam.example").status_code, 201)
        self.assertEqual(Lead.objects.count(), 0)

    def test_only_platform_owner_can_read_and_update_leads(self):
        from core.models import Lead, User

        self.post()
        lead = Lead.objects.get()
        self.as_admin()  # an ISP admin
        self.assertEqual(self.client.get(f"{API}/billing/platform/leads").status_code, 403)
        User.objects.create_superuser("Plat", "Owner", "01999000000", "Str0ng!Pass-2026")
        self.login("01999000000")
        listing = self.client.get(f"{API}/billing/platform/leads")
        self.assertEqual(listing.data["count"], 1)
        upd = self.client.patch(f"{API}/billing/platform/leads/{lead.id}", {"lead_status": "DEMO", "notes": "call Sat"}, format="json")
        self.assertEqual(upd.status_code, 200, upd.content)
        lead.refresh_from_db()
        self.assertEqual((lead.lead_status, lead.notes), ("DEMO", "call Sat"))

    def test_owner_gets_sms_for_new_lead(self):
        from django.test import override_settings

        with override_settings(PLATFORM_NOTIFY_PHONE="01999000000"), mock.patch(
            "customer.services.sms.SMS.send_single_sms", return_value=True
        ) as sms:
            self.post()
        self.assertEqual(sms.call_count, 1)
        self.assertIn("Rahim", sms.call_args.kwargs["message"])
