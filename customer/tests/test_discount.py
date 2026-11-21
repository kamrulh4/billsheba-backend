"""Discounts: money the ISP waives is recorded on the bill, restricted, and reported."""

from decimal import Decimal

from customer.models import Payment
from customer.tests.test_reliability import API, Base

PAY = f"{API}/payments"


class DiscountTests(Base):
    def pay(self, **extra):
        body = {"customer_id": self.customer.id, "payment_method": "CASH", **extra}
        return self.client.post(PAY, body, format="json")

    def test_amount_plus_discount_settles_the_bill(self):
        res = self.pay(amount="500", discount="100", note="Regular customer")
        self.assertEqual(res.status_code, 201, res.content)
        self.assertTrue(res.data["paid"])
        self.assertEqual((res.data["amount"], res.data["discount"], res.data["due"]), ("500.00", "100.00", "0.00"))
        payment = Payment.objects.get()
        self.assertEqual((payment.bill_amount, payment.discount), (Decimal("600.00"), Decimal("100.00")))  # bill unchanged

    def test_partial_payment_with_discount_stays_unpaid(self):
        res = self.pay(amount="300", discount="100", note="Eid offer")
        self.assertFalse(res.data["paid"])
        self.assertEqual(res.data["due"], "200.00")

    def test_a_later_payment_finishes_the_bill(self):
        self.pay(amount="300", discount="100", note="Eid offer")
        res = self.pay(amount="200")
        self.assertTrue(res.data["paid"])
        payment = Payment.objects.get()
        self.assertEqual((payment.amount, payment.discount), (Decimal("500.00"), Decimal("100.00")))

    def test_marking_paid_with_money_missing_is_refused(self):
        res = self.pay(amount="400", paid=True)
        self.assertEqual(res.status_code, 400)
        self.assertIn("discount", str(res.data).lower())
        self.assertFalse(Payment.objects.exists())

    def test_discount_needs_a_note(self):
        res = self.pay(amount="500", discount="100")
        self.assertEqual(res.status_code, 400)
        self.assertFalse(Payment.objects.exists())

    def test_discount_cannot_exceed_the_bill_or_be_negative(self):
        self.assertEqual(self.pay(amount="0", discount="700", note="x").status_code, 400)
        self.assertEqual(self.pay(amount="500", discount="-5", note="x").status_code, 400)

    def test_staff_cannot_give_a_discount_but_manager_can(self):
        for kind, phone in (("STAFF", "01911000010"), ("MANAGER", "01911000011")):
            self.client.post(
                f"{API}/users",
                {"first_name": kind, "phone": phone, "email": f"{phone}@example.com", "kind": kind,
                 "password": "Str0ng!Pass-2026", "confirm_password": "Str0ng!Pass-2026"},
                format="json",
            )
        self.login("01911000010")
        self.assertEqual(self.pay(amount="500", discount="100", note="x").status_code, 400)
        self.assertEqual(self.pay(amount="600").status_code, 201)  # staff can still take full payment
        Payment.objects.all().delete()
        self.login("01911000011")
        self.assertEqual(self.pay(amount="500", discount="100", note="x").status_code, 201)

    def test_editing_a_payment_applies_the_same_rules(self):
        payment = Payment.objects.create(
            organization=self.org, customer=self.customer, bill_amount=600, amount=0, paid=False,
            billing_month="JANUARY", billing_year=2025,
        )
        url = f"{PAY}/{payment.uid}"
        self.assertEqual(self.client.patch(url, {"amount": "400", "paid": True}, format="json").status_code, 400)
        res = self.client.patch(url, {"amount": "500", "discount": "100", "note": "waived"}, format="json")
        self.assertEqual(res.status_code, 200, res.content)
        payment.refresh_from_db()
        self.assertTrue(payment.paid)
        self.assertEqual(payment.due_amount, Decimal("0.00"))

    def test_outstanding_and_reports_use_the_discount(self):
        self.pay(amount="300", discount="100", note="Eid offer")
        report = self.client.get(f"{API}/reports/summary").data
        self.assertEqual(Decimal(str(report["total_outstanding"])), Decimal("200"))
        self.assertEqual(Decimal(str(report["this_month"]["discount"])), Decimal("100"))
        self.assertEqual(Decimal(str(report["this_month"]["revenue"])), Decimal("300"))

    def test_receipt_shows_the_discount(self):
        self.pay(amount="500", discount="100", note="Regular customer")
        uid = Payment.objects.get().uid
        data = self.client.get(f"{API}/payments/{uid}/receipt").data
        self.assertEqual((Decimal(str(data["discount"])), Decimal(str(data["due"]))), (Decimal("100"), Decimal("0")))
