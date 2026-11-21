"""Bill SMS: bills are created first, then texted in one bulk request, within the ISP's monthly limit."""

from unittest import mock

from core.models import Organization
from customer.choices import SMSStatus
from customer.models import Customer, Payment, SMSLog
from customer.services import billing
from customer.tests.test_reliability import Base

BULK = "customer.services.sms.SMS.send_bulk_sms"
SINGLE = "customer.services.sms.SMS.send_single_sms"


class BillSmsTests(Base):
    def setUp(self):
        super().setUp()
        Organization.objects.update(sms_feature=True, sms_quota_override=100)
        self.org.refresh_from_db()
        for i in range(2, 6):  # five customers in all
            Customer.objects.create(
                organization=self.org, name=f"C{i}", phone=f"0181100000{i}", username=f"c{i}",
                password="pw12345", package=self.customer.package,
            )

    def test_all_new_bills_go_in_one_bulk_request(self):
        with mock.patch(BULK, return_value=True) as bulk, mock.patch(SINGLE) as single:
            result = billing.generate_bills(self.org)
        self.assertEqual((result["created"], result["sms_sent"]), (5, 5))
        bulk.assert_called_once()
        single.assert_not_called()
        self.assertEqual(len(bulk.call_args.args[0]), 5)
        self.assertEqual(SMSLog.objects.filter(sms_status=SMSStatus.SENT).count(), 5)

    def test_running_again_creates_no_bills_and_texts_nobody(self):
        with mock.patch(BULK, return_value=True) as bulk:
            billing.generate_bills(self.org)
            again = billing.generate_bills(self.org)
        self.assertEqual((again["created"], again["sms_sent"]), (0, 0))
        bulk.assert_called_once()

    def test_gateway_failure_keeps_the_bills(self):
        with mock.patch(BULK, return_value=False):
            result = billing.generate_bills(self.org)
        self.assertEqual((result["created"], result["sms_sent"]), (5, 0))
        self.assertEqual(Payment.objects.count(), 5)

    def test_limit_sends_what_fits_and_skips_the_rest(self):
        Organization.objects.update(sms_quota_override=3)
        self.org.refresh_from_db()
        with mock.patch(BULK, return_value=True) as bulk:
            result = billing.generate_bills(self.org)
        self.assertEqual((result["created"], result["sms_sent"]), (5, 3))
        self.assertEqual(len(bulk.call_args.args[0]), 3)

    def test_no_sms_when_the_isp_has_sms_off(self):
        Organization.objects.update(sms_feature=False)
        self.org.refresh_from_db()
        with mock.patch(BULK, return_value=True) as bulk:
            result = billing.generate_bills(self.org)
        self.assertEqual((result["created"], result["sms_sent"]), (5, 0))
        bulk.assert_not_called()

    def test_customer_without_a_phone_is_skipped(self):
        Customer.objects.filter(username="c2").update(phone="")
        with mock.patch(BULK, return_value=True) as bulk:
            result = billing.generate_bills(self.org)
        self.assertEqual((result["created"], result["sms_sent"]), (5, 4))
        self.assertEqual(len(bulk.call_args.args[0]), 4)

    def test_messages_are_the_short_bill_text(self):
        with mock.patch(BULK, return_value=True) as bulk:
            billing.generate_bills(self.org)
        text = bulk.call_args.args[0][0]["message"]
        self.assertIn("600", text)
        self.assertIn("Net Zone", text)
