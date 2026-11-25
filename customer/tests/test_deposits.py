"""Collector cash handover: collected - deposited = still with the collector."""

from datetime import timedelta
from decimal import Decimal

from django.utils import timezone

from core.models import AuditLog, User
from customer.models import Deposit, Payment
from customer.tests.test_reliability import API, Base

SUMMARY = f"{API}/collections/summary"
DEPOSITS = f"{API}/collections/deposits"
D = Decimal


class DepositTests(Base):
    def setUp(self):
        super().setUp()
        self.new_user("STAFF", "01911000002")
        self.new_user("STAFF", "01911000003")
        self.rakib = User.objects.get(phone="01911000002")
        self.jasim = User.objects.get(phone="01911000003")
        self.admin = User.objects.get(phone="01711000001")

    def collect(self, user, amount, method="CASH", when=None):
        Payment.objects.create(
            organization=self.org, customer=self.customer, bill_amount=amount, amount=amount, paid=True,
            payment_method=method, entry_by=user, payment_date=when or timezone.now(),
        )

    def row(self, res, user):
        return next(r for r in res.data["rows"] if r["collector_id"] == user.pk)

    def test_pending_is_collected_minus_deposited(self):
        self.collect(self.rakib, 500)
        self.collect(self.rakib, 700)
        self.collect(self.jasim, 300)
        res = self.client.post(DEPOSITS, {"collector_id": self.rakib.pk, "amount": "1000", "note": "bank"}, format="json")
        self.assertEqual(res.status_code, 201, res.content)
        summary = self.client.get(SUMMARY)
        rakib = self.row(summary, self.rakib)
        self.assertEqual((rakib["collected"], rakib["deposited"], rakib["pending"]), (D("1200"), D("1000"), D("200")))
        self.assertEqual(self.row(summary, self.jasim)["pending"], D("300"))
        self.assertEqual(summary.data["total_pending"], D("500"))
        self.assertTrue(AuditLog.objects.filter(action="deposit.create").exists())
        self.assertEqual(Deposit.objects.get().received_by, self.admin)

    def test_everything_a_person_collected_counts_but_not_gateway_payments(self):
        self.collect(self.rakib, 500, method="BKASH")  # sent to the collector's own number: his money
        self.collect(self.rakib, 400)
        self.collect(self.rakib, 250, method="OTHER")  # e.g. imported history: still the collector's money
        Payment.objects.create(  # online payment: nobody's hand
            organization=self.org, customer=self.customer, bill_amount=100, amount=100, paid=True,
            payment_method="BKASH", entry_by=None, payment_date=timezone.now(),
        )
        res = self.client.get(SUMMARY)
        self.assertEqual(len(res.data["rows"]), 1)
        self.assertEqual(self.row(res, self.rakib)["collected"], D("1150"))

    def test_date_range_and_carry_over(self):
        last_month = timezone.now() - timedelta(days=40)
        self.collect(self.rakib, 600, when=last_month)
        self.collect(self.rakib, 100)
        self.assertEqual(self.row(self.client.get(SUMMARY), self.rakib)["collected"], D("100"))  # this month
        start = last_month.date().isoformat()
        self.assertEqual(self.row(self.client.get(SUMMARY, {"start_date": start}), self.rakib)["pending"], D("700"))
        self.assertEqual(self.client.get(SUMMARY, {"start_date": "nope"}).status_code, 400)

    def test_staff_sees_only_their_own_line_and_cannot_record_deposits(self):
        self.collect(self.rakib, 500)
        self.collect(self.jasim, 300)
        self.client.post(DEPOSITS, {"collector_id": self.jasim.pk, "amount": "100"}, format="json")
        self.login("01911000002")
        res = self.client.get(SUMMARY)
        self.assertEqual([r["collector_id"] for r in res.data["rows"]], [self.rakib.pk])
        listed = self.client.get(DEPOSITS).data
        self.assertEqual(len(listed["results"] if isinstance(listed, dict) else listed), 0)  # Jasim's, not mine
        denied = self.client.post(DEPOSITS, {"collector_id": self.rakib.pk, "amount": "5"}, format="json")
        self.assertEqual(denied.status_code, 403)

    def test_validation_and_tenant_isolation(self):
        bad = self.client.post(DEPOSITS, {"collector_id": self.rakib.pk, "amount": "0"}, format="json")
        self.assertEqual(bad.status_code, 400)
        other = User.objects.create_user("X", "Y", "01955555555", password="x", email="x@x.com")
        stranger = self.client.post(DEPOSITS, {"collector_id": other.pk, "amount": "10"}, format="json")
        self.assertEqual(stranger.status_code, 400)  # not in this ISP

    def test_a_wrong_deposit_can_be_removed_and_balance_returns(self):
        self.collect(self.rakib, 500)
        made = self.client.post(DEPOSITS, {"collector_id": self.rakib.pk, "amount": "500"}, format="json").data
        self.assertEqual(self.row(self.client.get(SUMMARY), self.rakib)["pending"], D("0"))
        self.assertEqual(self.client.delete(f"{DEPOSITS}/{made['uid']}").status_code, 204)
        self.assertEqual(self.row(self.client.get(SUMMARY), self.rakib)["pending"], D("500"))
        self.assertTrue(AuditLog.objects.filter(action="deposit.delete").exists())
