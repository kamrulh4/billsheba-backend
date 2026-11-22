"""With SMS_LOG_ENABLED off (production default) the monthly limit still works: it is counted on the ISP."""

from datetime import date
from unittest import mock

from django.test import override_settings
from django.utils import timezone

from core.models import Organization
from core.services.billing import send_subscription_reminders
from customer.choices import SMSStatus
from customer.models import SMSLog
from customer.services import billing
from customer.services import sms as sms_service
from customer.tests.test_reliability import Base

SEND = "customer.services.sms.SMS.send_single_sms"
BULK = "customer.services.sms.SMS.send_bulk_sms"


@override_settings(SMS_LOG_ENABLED=False)
class CounterTests(Base):
    def setUp(self):
        super().setUp()
        Organization.objects.update(sms_feature=True, sms_quota_override=3)
        self.org.refresh_from_db()

    def used(self):
        return sms_service.sms_used(self.org)

    def test_sent_messages_are_counted_on_the_organization_and_no_log_rows_are_made(self):
        with mock.patch(SEND, return_value=True):
            sms_service._deliver(self.org, self.customer, "01811000001", "ক" * 10)
            sms_service._deliver(self.org, self.customer, "01811000001", "ক" * 10)
        self.assertEqual(self.used(), 2)
        self.assertEqual(Organization.objects.get().sms_sent_count, 2)
        self.assertEqual(SMSLog.objects.count(), 0)

    def test_the_limit_still_blocks_and_blocked_or_failed_messages_do_not_count(self):
        with mock.patch(SEND, return_value=True) as gateway:
            results = [sms_service._deliver(self.org, self.customer, "01811000001", "ক" * 10) for _ in range(5)]
        self.assertEqual([r.sms_status for r in results], [SMSStatus.SENT] * 3 + [SMSStatus.BLOCKED] * 2)
        self.assertEqual((gateway.call_count, self.used()), (3, 3))
        Organization.objects.update(sms_sent_count=0)
        with mock.patch(SEND, return_value=False):
            failed = sms_service._deliver(self.org, self.customer, "01811000001", "ক" * 10)
        self.assertEqual((failed.sms_status, self.used()), (SMSStatus.FAILED, 0))

    def test_a_long_message_counts_all_its_parts(self):
        with mock.patch(SEND, return_value=True):
            sms_service._deliver(self.org, self.customer, "01811000001", "ক" * 71)  # 2 parts
        self.assertEqual(self.used(), 2)

    def test_platform_notices_are_not_counted(self):
        with mock.patch(SEND, return_value=True):
            sms_service._deliver(self.org, None, "01711000001", "ক", billable=False)
        self.assertEqual(self.used(), 0)

    def test_a_new_month_starts_again_from_zero(self):
        Organization.objects.update(sms_sent_count=3, sms_sent_month=date(2020, 1, 1))  # an old month's total
        self.assertEqual(self.used(), 0)
        with mock.patch(SEND, return_value=True):
            sms_service._deliver(self.org, self.customer, "01811000001", "ক" * 10)
        org = Organization.objects.get()
        self.assertEqual((org.sms_sent_count, org.sms_sent_month), (1, timezone.localdate().replace(day=1)))

    def test_bulk_sends_count_every_part_and_stop_at_the_limit(self):
        customers = [self.customer] * 5
        items = [(c, "ক" * 10) for c in customers]
        with mock.patch(BULK, return_value=True) as gateway:
            result = sms_service.deliver_bulk(self.org, items)
        self.assertEqual((len(result["sent"]), result["left"]), (3, 2))  # quota is 3
        self.assertEqual(self.used(), 3)
        self.assertEqual(len(gateway.call_args.args[0]), 3)
        self.assertEqual(SMSLog.objects.count(), 0)

    def test_a_failed_bulk_request_counts_nothing(self):
        with mock.patch(BULK, return_value=False):
            sms_service.deliver_bulk(self.org, [(self.customer, "ক" * 10)])
        self.assertEqual(self.used(), 0)

    def test_the_counter_is_what_the_org_endpoint_reports(self):
        with mock.patch(SEND, return_value=True):
            sms_service._deliver(self.org, self.customer, "01811000001", "ক" * 10)
        self.as_admin()
        data = self.client.get("/api/v1/organizations/me").data
        self.assertEqual((data["sms_limit"], data["sms_used"]), (3, 1))


@override_settings(SMS_LOG_ENABLED=False)
class ReminderOnceADayTests(Base):
    def test_due_reminders_go_out_once_a_day_even_without_logs(self):
        billing.generate_bills(self.org, send_sms=False)
        Organization.objects.update(sms_feature=True, sms_quota_override=1000, due_day=timezone.localdate().day)
        org = Organization.objects.get()
        with mock.patch(SEND, return_value=True) as sms:
            self.assertEqual(billing.send_due_reminders(org), {"sent": 1})
            self.assertEqual(billing.send_due_reminders(org), {"sent": 0})
        self.assertEqual(sms.call_count, 1)

    def test_the_subscription_expiry_reminder_is_sent_once_a_day(self):
        Organization.objects.update(
            subscription_status="TRIAL", subscription_end_date=timezone.localdate() + timezone.timedelta(days=3)
        )
        with mock.patch(SEND, return_value=True) as sms:
            self.assertEqual(send_subscription_reminders(), {"sent": 1})
            self.assertEqual(send_subscription_reminders(), {"sent": 0})
        self.assertEqual(sms.call_count, 1)
