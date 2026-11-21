"""Template-driven, logged SMS sending for an organization."""

import logging

from django.db.models import Sum
from django.utils import timezone

from common.bd import to_sms_number
from common.helpers.sms_helper import SMS
from common.sms_text import sms_segments
from customer.choices import SMSStatus
from customer.models import SMSLog
from customer.utils import month_name_to_bangla

logger = logging.getLogger(__name__)

# Short on purpose: Bangla text is 70 characters per SMS, so each of these fits in one SMS for a normal ISP name.
DEFAULT_TEMPLATES = {
    "bill": "{month} মাসের বিল {amount}৳। {org}",
    "payment": "{month} মাসের {amount}৳ পেয়েছি। ধন্যবাদ। {org}",
    "suspend": "{month} মাসের বিল বকেয়া, সংযোগ বন্ধ। বিল দিন। {org}",
    "reminder": "{month} মাসের {amount}৳ বকেয়া। {due_date} তারিখের মধ্যে দিন। {org}",
}

# Longest a saved template / announcement (placeholders filled in) may be, in SMS parts. One part keeps
# "1 message = 1 SMS charged": 70 Bangla or 160 English characters.
MAX_TEMPLATE_SEGMENTS = 1
MAX_BULK_SEGMENTS = 1



def render_message(org, kind: str, *, month: str = "", amount="", name: str = "") -> str:
    template = getattr(org, f"{kind}_sms_template", "") or DEFAULT_TEMPLATES[kind]
    try:
        return template.format(
            name=name,
            amount=amount,
            month=month_name_to_bangla.get(month, month),
            org=org.name or "",
            due_date=org.due_day,
        )
    except (KeyError, IndexError, ValueError):
        # A malformed custom template must not break billing.
        return DEFAULT_TEMPLATES[kind].format(
            amount=amount,
            month=month_name_to_bangla.get(month, month),
            org=org.name or "",
        )


def sms_used(org) -> int:
    """SMS parts this ISP has sent so far this month (what the monthly limit is counted in)."""
    month_start = timezone.localtime().replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    total = SMSLog.objects.filter(
        organization=org, billable=True, sms_status=SMSStatus.SENT, created_at__gte=month_start
    ).aggregate(total=Sum("segments"))["total"]
    return total or 0


def sms_remaining(org) -> int:
    return max(org.sms_monthly_limit - sms_used(org), 0)


def sample_message(org, template: str) -> str:
    """``template`` filled with typical (long-ish) values, to estimate how many SMS it will take."""
    try:
        return template.format(
            name="রহিম উদ্দিন", amount="1500", month="সেপ্টেম্বর", org=org.name or "", due_date=org.due_day
        )
    except (KeyError, IndexError, ValueError):
        return template


def _deliver(org, customer, to: str, message: str, *, billable: bool = True) -> SMSLog:
    """Send + log one message. Never raises; the log records SENT / FAILED / BLOCKED.

    ``billable`` messages count against the ISP's monthly limit and are not sent (BLOCKED) once it is used up.
    Platform notices (``billable=False``, or no organization) are never limited.
    """
    segments = sms_segments(message) or 1
    counted = billable and org is not None
    log = SMSLog.objects.create(
        organization=org, customer=customer, to=to, message=message, segments=segments, billable=counted
    )
    if counted and sms_remaining(org) < segments:
        log.sms_status = SMSStatus.BLOCKED
        log.save(update_fields=["sms_status"])
        logger.warning("SMS blocked: %s is over its monthly limit", org)
        return log
    try:
        ok = SMS.send_single_sms(to=to_sms_number(to), message=message)
    except Exception:
        logger.exception("SMS send crashed")
        ok = False
    log.sms_status = SMSStatus.SENT if ok else SMSStatus.FAILED
    log.save(update_fields=["sms_status"])
    return log


def deliver_bulk(org, items) -> dict:
    """Send many ``(customer, message)`` SMS in ONE gateway request (``SMS.send_bulk_sms``), within the monthly limit.

    Messages are taken in order while they fit the ISP's remaining quota; the rest are left over, untouched (not
    logged), so the caller can try them again later. Returns ``{"sent": [customers], "left": n, "failed": bool}``.
    Never raises.
    """
    remaining = sms_remaining(org)
    chosen, used = [], 0
    for customer, message in items:
        parts = sms_segments(message) or 1
        if used + parts > remaining:
            break
        chosen.append((customer, message, parts))
        used += parts
    left = len(items) - len(chosen)
    if not chosen:
        return {"sent": [], "left": left, "failed": False}
    try:
        ok = SMS.send_bulk_sms([{"to": to_sms_number(c.phone), "message": m} for c, m, _ in chosen])
    except Exception:
        logger.exception("Bulk SMS crashed")
        ok = False
    SMSLog.objects.bulk_create(
        [
            SMSLog(
                organization=org, customer=c, to=c.phone, message=m, segments=parts, billable=True,
                sms_status=SMSStatus.SENT if ok else SMSStatus.FAILED,
            )
            for c, m, parts in chosen
        ]
    )
    return {"sent": [c for c, _, _ in chosen] if ok else [], "left": left, "failed": not ok}


def send_org_sms(org, customer, kind: str, **ctx) -> SMSLog | None:
    """Send one SMS if the org has SMS enabled; always records an ``SMSLog``."""
    if not org.sms_feature or not customer or not customer.phone:
        return None
    message = render_message(org, kind, name=customer.name, **ctx)
    log = _deliver(org, customer, customer.phone, message)
    return None if log.sms_status == SMSStatus.BLOCKED else log  # callers treat a log as "an SMS went out"
