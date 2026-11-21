"""One place that records money against a customer's bill.

Used by staff entry (API), approved portal claims and bKash callbacks so all
three follow identical rules (partial payments, reactivation, SMS).
"""

import uuid
from datetime import timedelta
from decimal import Decimal

from django.db import transaction
from django.utils import timezone

from common.bd import current_period
from core.choices import BillingCycle
from customer.models import Payment
from customer.services.sms import send_org_sms


class PaymentError(Exception):
    """A business-rule violation that should be shown to the user."""


MONTHS = [
    "JANUARY", "FEBRUARY", "MARCH", "APRIL", "MAY", "JUNE",
    "JULY", "AUGUST", "SEPTEMBER", "OCTOBER", "NOVEMBER", "DECEMBER",
]


def has_overdue(org, customer) -> bool:
    """True if any unpaid bill is already late: an earlier month, or this month past the due day."""
    cur_month, cur_year = current_period()
    cur = (cur_year, MONTHS.index(cur_month))
    past_due_day = timezone.localdate().day > org.due_day
    for bill in Payment.objects.filter(customer=customer, paid=False):
        if bill.billing_month not in MONTHS:
            continue
        key = (bill.billing_year, MONTHS.index(bill.billing_month))
        if key < cur or (key == cur and past_due_day):
            return True
    return False


def _finish(org, customer, payment, fully_paid, sms_month):
    # Paying one bill must not switch a customer back on while an older one is still unpaid.
    if fully_paid and not customer.is_active and not has_overdue(org, customer):
        customer.is_active = True  # pre_save signal re-enables the router account
        customer.save(update_fields=["is_active"])
    if fully_paid:
        transaction.on_commit(
            lambda: send_org_sms(org, customer, "payment", month=sms_month, amount=payment.amount)
        )


@transaction.atomic
def record_payment(
    org,
    customer,
    amount: Decimal,
    *,
    method: str = "CASH",
    month: str | None = None,
    year: int | None = None,
    mark_paid: bool = False,
    note: str = "",
    transaction_id: str | None = None,
    entry_by=None,
    payment_date=None,
    discount: Decimal = Decimal("0.00"),
) -> Payment:
    """Apply ``amount`` (and an optional ``discount``) to the bill of ``month``/``year`` (default: current period).

    A bill is settled once ``amount + discount >= bill_amount``. Marking it paid while money is still missing is
    refused: the difference has to be recorded as a discount, so nothing is written off silently.
    """
    if customer.is_free:
        raise PaymentError("Cannot create payment for free customers.")
    discount = Decimal(discount or 0)
    if discount < 0:
        raise PaymentError("Discount cannot be negative.")
    if discount and org.billing_cycle != BillingCycle.MONTHLY:
        raise PaymentError("Discount is only available with monthly billing.")

    cur_month, cur_year = current_period()
    payment_date = payment_date or timezone.now()
    transaction_id = transaction_id or str(uuid.uuid4())
    price = customer.package.price if customer.package else Decimal("0.00")
    who = {"entry_by": entry_by, "updated_by": entry_by}

    if org.billing_cycle == BillingCycle.MONTHLY:
        month, year = month or cur_month, year or cur_year
        payment = Payment.objects.filter(
            customer=customer, billing_month=month, billing_year=year
        ).first()
        if payment and payment.paid:
            raise PaymentError("Payment for this month has already been made.")
        if payment:
            total = payment.amount + amount
            total_discount = payment.discount + discount
            if total_discount > payment.bill_amount - payment.amount:
                raise PaymentError("Discount is more than what is left to pay on this bill.")
            payment.amount = total
            payment.discount = total_discount
            payment.paid = total + total_discount >= payment.bill_amount
            if mark_paid and not payment.paid:
                raise PaymentError("Money is still due. Record the difference as a discount to mark this bill paid.")
            payment.payment_date = payment_date
            payment.payment_method = method
            payment.transaction_id = transaction_id
            payment.note = note or payment.note
            if entry_by:
                payment.entry_by = payment.updated_by = entry_by
            payment.save()
        else:
            if discount > price:
                raise PaymentError("Discount is more than the bill.")
            if mark_paid and amount + discount < price:
                raise PaymentError("Money is still due. Record the difference as a discount to mark this bill paid.")
            payment = Payment.objects.create(
                organization=org, customer=customer, bill_amount=price, amount=amount, discount=discount,
                paid=amount + discount >= price, billing_month=month, billing_year=year,
                payment_method=method, payment_date=payment_date,
                transaction_id=transaction_id, note=note, **who,
            )
        _finish(org, customer, payment, payment.paid, month)
        return payment

    if org.billing_cycle == BillingCycle.DAYS30:
        if price <= 0:
            raise PaymentError("Customer has no priced package.")
        extend_days = max(int(amount // price), 1) * 30
        today = timezone.localdate()
        base = customer.subscription_end_date
        base = base if base and base > today else today
        new_end = base + timedelta(days=extend_days)
        customer.subscription_end_date = new_end
        customer.save(update_fields=["subscription_end_date"])
        payment = Payment.objects.create(
            organization=org, customer=customer, bill_amount=price, amount=amount, paid=True,
            billing_month=month or cur_month, billing_year=cur_year, payment_method=method,
            payment_date=payment_date, transaction_id=transaction_id,
            note=note or f"Subscription extended by {extend_days} days (until {new_end})", **who,
        )
        _finish(org, customer, payment, True, "")
        return payment

    raise PaymentError(f"Unsupported billing cycle: {org.billing_cycle}")


@transaction.atomic
def allocate_online_payment(org, customer, amount: Decimal, *, method: str, transaction_id: str) -> Payment:
    """Spend an online payment on the oldest unpaid bills first (customer paid "what I owe").

    Anything left over (or a payment with no open bill) goes to the current month.
    """
    remaining = Decimal(amount)
    last = None
    if org.billing_cycle == BillingCycle.MONTHLY:
        unpaid = Payment.objects.filter(customer=customer, paid=False).order_by("billing_year", "id")
        for bill in unpaid:
            if remaining <= 0:
                break
            due = bill.due_amount
            part = min(remaining, due) if due > 0 else remaining
            last = record_payment(
                org, customer, part, method=method, month=bill.billing_month,
                year=bill.billing_year, transaction_id=transaction_id,
                note=f"{method} TrxID {transaction_id}",
            )
            remaining -= part
    if remaining > 0 or last is None:
        last = record_payment(
            org, customer, remaining, method=method, transaction_id=transaction_id,
            note=f"{method} TrxID {transaction_id}",
        )
    return last
