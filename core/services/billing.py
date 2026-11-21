"""Subscription renewal rules (platform-side billing)."""

import logging
from datetime import timedelta
from datetime import timedelta as _td
from decimal import Decimal

from django.conf import settings
from django.db import transaction
from django.utils import timezone

from common.bkash import BkashClient, BkashCredentials, BkashError, is_completed
from core.choices import SubscriptionStatus
from core.models import Organization, SubscriptionPayment


@transaction.atomic
def approve_subscription_payment(payment: SubscriptionPayment, reviewer) -> SubscriptionPayment:
    """Activate / extend the organization for the paid plan and period."""
    org, plan = payment.organization, payment.plan
    today = timezone.localdate()
    same_plan_running = (
        org.subscription_id == plan.id
        and org.subscription_status == SubscriptionStatus.ACTIVE
        and org.subscription_end_date
        and org.subscription_end_date > today
    )
    # Renewing the same live plan stacks on the remaining time; anything else starts today.
    start = org.subscription_end_date if same_plan_running else today

    org.subscription = plan
    org.subscription_status = SubscriptionStatus.ACTIVE
    org.subscription_end_date = start + timedelta(days=plan.duration_days * payment.months)
    org.allowed_customer = plan.max_customers
    org.sms_feature = plan.sms_feature
    org.save()

    payment.payment_status = SubscriptionPayment.APPROVED
    payment.reviewed_by, payment.reviewed_at = reviewer, timezone.now()
    payment.save()
    return payment


@transaction.atomic
def finalize_subscription_payment(payment: SubscriptionPayment, result: dict) -> bool:
    """Activate the plan if a bKash execute/query ``result`` proves the fee was paid."""
    if payment.payment_status != SubscriptionPayment.INITIATED:
        return payment.payment_status == SubscriptionPayment.APPROVED
    if not is_completed(result) or Decimal(str(result.get("amount", "0"))) != payment.amount:
        payment.payment_status = SubscriptionPayment.REJECTED
        payment.review_note = str(result.get("statusMessage", "Payment not completed"))[:255]
        payment.save()
        return False
    payment.transaction_id = result["trxID"]
    payment.sender_number = str(result.get("customerMsisdn", ""))[:20]
    approve_subscription_payment(payment, None)
    return True


logger = logging.getLogger(__name__)


def platform_bkash_credentials() -> BkashCredentials:
    return BkashCredentials(
        app_key=settings.PLATFORM_BKASH_APP_KEY,
        app_secret=settings.PLATFORM_BKASH_APP_SECRET,
        username=settings.PLATFORM_BKASH_USERNAME,
        password=settings.PLATFORM_BKASH_PASSWORD,
        sandbox=settings.PLATFORM_BKASH_SANDBOX,
    )


def reconcile_subscription_payments() -> dict:
    """Same safety net for ISP subscription checkouts whose callback never arrived."""
    creds = platform_bkash_credentials()
    stats = {"completed": 0, "failed": 0, "waiting": 0}
    if not creds.configured:
        return stats
    now = timezone.now()
    stale = SubscriptionPayment.objects.filter(
        payment_status=SubscriptionPayment.INITIATED, created_at__lte=now - _td(minutes=10)
    ).exclude(gateway_payment_id="")
    for payment in stale.select_related("organization", "plan"):
        try:
            result = BkashClient(creds).query_payment(payment.gateway_payment_id)
        except BkashError as e:
            logger.warning("Subscription reconcile failed for %s: %s", payment.pk, e)
            result = {}
        if is_completed(result):
            stats["completed" if finalize_subscription_payment(payment, result) else "failed"] += 1
        elif now - payment.created_at > _td(minutes=30):
            payment.payment_status = SubscriptionPayment.REJECTED
            payment.review_note = "Not completed (expired)"
            payment.save()
            stats["failed"] += 1
        else:
            stats["waiting"] += 1
    return stats


REMINDER_DAYS = (7, 3, 1)


def send_subscription_reminders() -> dict:
    """SMS an ISP's contact number 7, 3 and 1 day(s) before its trial / plan ends."""
    from customer.models import SMSLog
    from customer.services.sms import _deliver  # shared sender (logs + never raises)

    today = timezone.localdate()
    sent = 0
    orgs = Organization.objects.filter(
        subscription_status__in=[SubscriptionStatus.TRIAL, SubscriptionStatus.ACTIVE],
        subscription_end_date__in=[today + _td(days=d) for d in REMINDER_DAYS],
    ).exclude(phone="")
    for org in orgs:
        days = (org.subscription_end_date - today).days
        kind = "ট্রায়াল" if org.subscription_status == SubscriptionStatus.TRIAL else "সাবস্ক্রিপশন"
        message = (
            f"{org.name}: আপনার বিলশেবা {kind} {days} দিনে শেষ হবে। "
            f"চালু রাখতে {settings.PUBLIC_BASE_URL}/billing এ রিনিউ করুন।"
        )
        if SMSLog.objects.filter(organization=org, customer__isnull=True, message=message, created_at__date=today).exists():
            continue
        _deliver(org, None, org.phone, message, billable=False)  # a platform notice, not the ISP's quota
        sent += 1
    return {"sent": sent}
