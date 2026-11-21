import logging as log

from celery import shared_task
from django.utils import timezone

from common.bd import to_sms_number
from common.choices import Status
from common.helpers import SMS
from core.choices import BillingCycle, SubscriptionStatus
from core.models import Organization
from core.services.billing import reconcile_subscription_payments, send_subscription_reminders
from customer.services import billing
from customer.services.online import reconcile_online_payments


def _active_orgs():
    return Organization.objects.filter(status=Status.ACTIVE).exclude(
        subscription_status__in=[SubscriptionStatus.CANCELLED, SubscriptionStatus.EXPIRED]
    )


@shared_task
def add(x=10, y=20):
    return x + y


@shared_task
def generate_customer_bills(org_id: int):
    org = Organization.objects.filter(id=org_id).first()
    if not org:
        log.warning("generate_customer_bills: org %s not found", org_id)
        return
    if org.billing_cycle != BillingCycle.MONTHLY:
        return
    result = billing.generate_bills(org)
    log.info("Bills for %s: %s", org.name, result)
    return result


@shared_task
def deactivate_due_payment_customers(org_id: int):
    org = Organization.objects.filter(id=org_id).first()
    if not org or org.billing_cycle != BillingCycle.MONTHLY:
        return
    return billing.suspend_overdue(org)


@shared_task
def generate_organizations_bills():
    for org in _active_orgs():
        generate_customer_bills(org.id)


@shared_task
def deactivate_organizations_due_payment_customers():
    for org in _active_orgs():
        deactivate_due_payment_customers(org.id)


@shared_task
def deactivate_expired_subscription_customers(org_id: int):
    org = Organization.objects.filter(id=org_id).first()
    if org:
        return billing.suspend_expired_subscriptions(org)


@shared_task
def deactivate_all_organizations_expired_subscription_customers():
    for org in _active_orgs().exclude(billing_cycle=BillingCycle.MONTHLY):
        deactivate_expired_subscription_customers(org.id)


@shared_task
def expire_lapsed_organizations():
    """Mark trial/paid organizations whose end date passed as EXPIRED (read-only)."""
    return Organization.objects.filter(
        subscription_status__in=[SubscriptionStatus.TRIAL, SubscriptionStatus.ACTIVE],
        subscription_end_date__lt=timezone.localdate(),
    ).update(subscription_status=SubscriptionStatus.EXPIRED)


@shared_task
def reconcile_payments():
    """Settle bKash checkouts whose browser callback never arrived (runs every few minutes)."""
    return {
        "customers": reconcile_online_payments(),
        "subscriptions": reconcile_subscription_payments(),
    }


@shared_task
def daily_billing_cycle():
    """Idempotent daily driver: bill, remind, suspend, and repair router state."""
    today = timezone.localdate()
    expire_lapsed_organizations()
    send_subscription_reminders()
    for org in _active_orgs():
        if org.billing_cycle == BillingCycle.MONTHLY:
            billing.generate_bills(org)
            # Due-bill reminder SMS is switched off for now (SMS costs). Uncomment to re-enable;
            # REMINDER_DAYS_BEFORE_DUE in settings controls whether an earlier one is sent too.
            # billing.send_due_reminders(org)
            if today.day > org.due_day:
                billing.suspend_overdue(org)
        else:
            billing.suspend_expired_subscriptions(org)
        billing.retry_router_sync(org)


@shared_task
def send_single_sms(to, message):
    log.info("Sending sms to the customer")
    SMS.send_single_sms(to_sms_number(to), message)
