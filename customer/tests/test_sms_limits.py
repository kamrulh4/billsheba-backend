"""SMS cost control: segment counting, the monthly limit, template and bulk caps."""

from unittest import mock

from django.test import SimpleTestCase

from common.sms_text import sms_info, sms_segments
from core.models import Organization, Subscription
from customer.choices import SMSStatus
from customer.models import SMSLog
from customer.services import sms as sms_service
from customer.tests.test_reliability import API, Base

SEND = "customer.services.sms.SMS.send_single_sms"


class SegmentTests(SimpleTestCase):
    def test_english_and_bangla_limits(self):
        self.assertEqual(sms_segments("a" * 160), 1)
        self.assertEqual(sms_segments("a" * 161), 2)  # 153 per part once split
        self.assertEqual(sms_segments("ক" * 70), 1)
        self.assertEqual(sms_segments("ক" * 71), 2)  # one Bangla letter flips the whole text to 70/67
        self.assertEqual(sms_segments("ক" * 140), 3)
        self.assertEqual(sms_segments("a" * 100 + "ক"), 2)
        self.assertEqual(sms_segments(""), 0)

    def test_gsm_extended_characters_count_double(self):
        self.assertEqual(sms_info("{" * 80)["length"], 160)
        self.assertEqual(sms_segments("{" * 81), 2)

    def test_default_templates_fit_one_sms(self):
        org = Organization(name="Gulbahar Net", due_day=10)
        for template in sms_service.DEFAULT_TEMPLATES.values():
            self.assertEqual(sms_segments(sms_service.sample_message(org, template)), 1, template)


class LimitTests(Base):
    def setUp(self):
        super().setUp()
        Organization.objects.update(sms_feature=True, sms_quota_override=3)
        self.org.refresh_from_db()

    def test_sends_until_the_limit_then_blocks_without_calling_the_gateway(self):
        with mock.patch(SEND, return_value=True) as gateway:
            logs = [sms_service._deliver(self.org, self.customer, "01811000001", "ক" * 10) for _ in range(5)]
        self.assertEqual([x.sms_status for x in logs], [SMSStatus.SENT] * 3 + [SMSStatus.BLOCKED] * 2)
        self.assertEqual(gateway.call_count, 3)
        self.assertEqual(sms_service.sms_used(self.org), 3)

    def test_long_message_uses_several_parts_of_the_limit(self):
        with mock.patch(SEND, return_value=True):
            log = sms_service._deliver(self.org, self.customer, "01811000001", "ক" * 140)  # 3 parts
            blocked = sms_service._deliver(self.org, self.customer, "01811000001", "ক")
        self.assertEqual(log.segments, 3)
        self.assertEqual(blocked.sms_status, SMSStatus.BLOCKED)

    def test_send_org_sms_reports_a_blocked_message_as_not_sent(self):
        Organization.objects.update(sms_quota_override=0)
        self.org.refresh_from_db()
        self.org.subscription.sms_monthly_quota = 0
        with mock.patch(SEND, return_value=True) as gateway:
            self.assertIsNone(sms_service.send_org_sms(self.org, self.customer, "bill", month="January", amount=500))
        gateway.assert_not_called()
        self.assertEqual(SMSLog.objects.get().sms_status, SMSStatus.BLOCKED)

    def test_platform_notice_to_the_isp_is_never_limited(self):
        with mock.patch(SEND, return_value=True):
            for _ in range(5):
                log = sms_service._deliver(self.org, None, "01711000001", "ক", billable=False)
        self.assertEqual(log.sms_status, SMSStatus.SENT)
        self.assertEqual(sms_service.sms_used(self.org), 0)

    def test_a_free_trial_only_gets_a_taste_of_sms(self):
        self.org.sms_quota_override = 0
        self.org.subscription.sms_monthly_quota = 500
        self.org.subscription_status = "TRIAL"
        self.assertEqual(self.org.sms_monthly_limit, Organization.TRIAL_SMS_LIMIT)
        self.org.subscription_status = "ACTIVE"
        self.assertEqual(self.org.sms_monthly_limit, 500)

    def test_plan_quota_applies_unless_overridden(self):
        self.org.subscription_status = "ACTIVE"
        self.org.sms_quota_override = 0
        self.org.subscription.sms_monthly_quota = 300
        self.assertEqual(self.org.sms_monthly_limit, 300)
        self.org.sms_quota_override = 50
        self.assertEqual(self.org.sms_monthly_limit, 50)
        self.org.sms_feature = False
        self.assertEqual(self.org.sms_monthly_limit, 0)

    def test_usage_is_in_the_organization_api(self):
        with mock.patch(SEND, return_value=True):
            sms_service._deliver(self.org, self.customer, "01811000001", "ক" * 10)
        data = self.client.get(f"{API}/organizations/me").data
        self.assertEqual((data["sms_limit"], data["sms_used"]), (3, 1))

    def test_isp_cannot_raise_its_own_limit(self):
        self.client.patch(f"{API}/organizations/me", {"sms_quota_override": 99999}, format="json")
        self.org.refresh_from_db()
        self.assertEqual(self.org.sms_quota_override, 3)


class TemplateAndBulkTests(Base):
    def setUp(self):
        super().setUp()
        Organization.objects.update(sms_feature=True, sms_quota_override=10)

    def test_overlong_template_is_rejected(self):
        res = self.client.patch(f"{API}/organizations/me", {"bill_sms_template": "ক" * 200}, format="json")
        self.assertEqual(res.status_code, 400, res.content)
        self.assertIn("Too long", str(res.data))

    def test_reasonable_template_is_saved(self):
        res = self.client.patch(
            f"{API}/organizations/me", {"bill_sms_template": "{month} বিল {amount}৳ {org}"}, format="json"
        )
        self.assertEqual(res.status_code, 200, res.content)

    def test_bulk_dry_run_estimates_without_sending(self):
        with mock.patch(SEND, return_value=True) as gateway:
            res = self.client.post(f"{API}/sms/send", {"message": "ক" * 60, "dry_run": True}, format="json")
        self.assertEqual(res.status_code, 200, res.content)
        self.assertEqual((res.data["recipients"], res.data["segments_each"], res.data["total_sms"]), (1, 1, 1))
        gateway.assert_not_called()

    def test_bulk_over_the_limit_sends_nothing(self):
        Organization.objects.update(sms_quota_override=0)
        Subscription.objects.update(sms_monthly_quota=0)
        with mock.patch(SEND, return_value=True) as gateway:
            res = self.client.post(f"{API}/sms/send", {"message": "ক" * 60}, format="json")
        self.assertEqual(res.status_code, 400)
        gateway.assert_not_called()

    def test_bulk_message_longer_than_one_sms_is_refused(self):
        with mock.patch(SEND, return_value=True) as gateway:
            res = self.client.post(f"{API}/sms/send", {"message": "ক" * 71}, format="json")
        self.assertEqual(res.status_code, 400)
        gateway.assert_not_called()

    def test_template_of_two_sms_is_refused(self):
        res = self.client.patch(f"{API}/organizations/me", {"bill_sms_template": "ক" * 71}, format="json")
        self.assertEqual(res.status_code, 400)
