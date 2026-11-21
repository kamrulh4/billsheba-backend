"""Billing rules shared by API views and Celery tasks."""

import logging
from decimal import Decimal

from django.db import transaction
from django.utils import timezone

from common.bd import current_period
from common.choices import Status
from core.choices import BillingCycle
from customer.choices import PaymentMethod
from customer.helpers import Mikrotik
from customer.models import Customer, Payment
from customer.services.sms import deliver_bulk, render_message, send_org_sms

logger = logging.getLogger(__name__)


def billable_customers(org):
    return Customer.objects.filter(
        organization=org, status=Status.ACTIVE, is_active=True, is_free=False
    ).select_related("package")


def generate_bills(
    org, month: str | None = None, year: int | None = None, *, send_sms: bool = True
) -> dict:
    """Create the unpaid bill for every billable customer that has none yet.

    Idempotent: running it twice for the same period creates nothing new.
    """
    cur_month, cur_year = current_period()
    month, year = month or cur_month, year or cur_year

    already = set(
        Payment.objects.filter(
            organization=org, billing_month=month, billing_year=year
        ).values_list("customer_id", flat=True)
    )
    new_payments = []
    for customer in billable_customers(org):
        if customer.id in already or not customer.package or customer.package.price <= 0:
            continue
        new_payments.append(
            Payment(
                organization=org,
                customer=customer,
                bill_amount=customer.package.price,
                amount=Decimal("0.00"),
                billing_month=month,
                billing_year=year,
                payment_method=PaymentMethod.OTHER,
                paid=False,
                note=f"Auto-generated bill for {month} {year}",
            )
        )
    Payment.objects.bulk_create(new_payments)
    # Bills first (never lost); then, if the ISP has SMS on, one bulk request for the new bills,
    # as many as the monthly limit allows (the rest are skipped).
    sms_sent = 0
    if send_sms and org.sms_feature and new_payments:
        items = [
            (p.customer, render_message(org, "bill", name=p.customer.name, month=month, amount=p.bill_amount))
            for p in new_payments
            if p.customer.phone
        ]
        if items:
            sms_sent = len(deliver_bulk(org, items)["sent"])
    return {"created": len(new_payments), "sms_sent": sms_sent, "month": month, "year": year}


def suspend_overdue(org, month: str | None = None, year: int | None = None) -> dict:
    """Disable customers who still owe the bill for the period."""
    cur_month, cur_year = current_period()
    month, year = month or cur_month, year or cur_year
    unpaid = Payment.objects.filter(
        organization=org, billing_month=month, billing_year=year, paid=False,
        customer__is_active=True, customer__is_free=False, customer__status=Status.ACTIVE,
    ).select_related("customer")

    suspended_ids = []
    for payment in unpaid:
        customer = payment.customer
        if org.router_configured and customer.username:
            ok, msg = Mikrotik.toggle_ppp_user(customer.username, True, org)
            if not ok:
                logger.warning("Could not suspend %s on router: %s", customer.username, msg)
                continue
        suspended_ids.append(customer.pk)
        send_org_sms(org, customer, "suspend", month=month, amount=payment.bill_amount)
    # One UPDATE for everyone (queryset.update() bypasses the pre_save signal; the router was handled above)
    Customer.objects.filter(pk__in=suspended_ids).update(is_active=False)
    return {"suspended": len(suspended_ids)}


def suspend_expired_subscriptions(org) -> dict:
    """For day-based cycles: disable customers whose paid period ended."""
    if org.billing_cycle == BillingCycle.MONTHLY:
        return {"suspended": 0}
    expired = billable_customers(org).filter(
        subscription_end_date__isnull=False, subscription_end_date__lt=timezone.localdate()
    )
    suspended_ids = []
    for customer in expired:
        if org.router_configured and customer.username:
            ok, msg = Mikrotik.toggle_ppp_user(customer.username, True, org)
            if not ok:
                logger.warning("Could not suspend %s: %s", customer.username, msg)
                continue
        suspended_ids.append(customer.pk)
        send_org_sms(org, customer, "suspend", month="", amount="")
    Customer.objects.filter(pk__in=suspended_ids).update(is_active=False)
    return {"suspended": len(suspended_ids)}


@transaction.atomic
def reactivate(customer) -> None:
    """Re-enable a customer on the router + DB after payment."""
    org = customer.organization
    if org and org.router_configured and customer.username:
        Mikrotik.toggle_ppp_user(customer.username, False, org)
    Customer.objects.filter(pk=customer.pk).update(is_active=True)
    customer.is_active = True


def retry_router_sync(org) -> dict:
    """Re-apply is_active to the router for customers whose earlier attempt failed."""
    if not org.router_configured:
        return {"retried": 0, "fixed": 0}
    pending = Customer.objects.filter(organization=org, router_sync_pending=True).exclude(username="")
    fixed = 0
    for customer in pending:
        ok, _ = Mikrotik.toggle_ppp_user(customer.username, not customer.is_active, org)
        if ok:
            Customer.objects.filter(pk=customer.pk).update(router_sync_pending=False)
            fixed += 1
    return {"retried": pending.count(), "fixed": fixed}


def send_due_reminders(org) -> dict:
    """SMS unpaid customers on the ISP's suspension day (and, if enabled, N days earlier). Once per day."""
    from django.conf import settings

    from customer.models import SMSLog
    from customer.services.sms import render_message

    today = timezone.localdate()
    days = {org.due_day}
    if settings.REMINDER_DAYS_BEFORE_DUE > 0:
        days.add(max(org.due_day - settings.REMINDER_DAYS_BEFORE_DUE, 1))
    if org.billing_cycle != BillingCycle.MONTHLY or today.day not in days:
        return {"sent": 0}
    month, year = current_period()
    sent = 0
    unpaid = Payment.objects.filter(
        organization=org, billing_month=month, billing_year=year, paid=False,
        customer__is_active=True, customer__is_free=False, customer__status=Status.ACTIVE,
    ).select_related("customer")
    for bill in unpaid:
        message = render_message(
            org, "reminder", month=month, amount=bill.due_amount, name=bill.customer.name
        )
        if SMSLog.objects.filter(customer=bill.customer, message=message, created_at__date=today).exists():
            continue
        if send_org_sms(org, bill.customer, "reminder", month=month, amount=bill.due_amount):
            sent += 1
    return {"sent": sent}
