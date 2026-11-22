"""Template-driven, logged SMS sending for an organization."""

import logging

from django.conf import settings
from django.db.models import Case, F, IntegerField, Value, When
from django.utils import timezone

from common.bd import to_sms_number
from common.helpers.sms_helper import SMS
from common.sms_text import sms_segments
from core.models import Organization
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


def _month_start():
    return timezone.localdate().replace(day=1)


def sms_used(org) -> int:
    """SMS parts this ISP has sent so far this month (what the monthly limit is counted in).

    Read fresh from the database so a long-lived ``org`` object never reports a stale number.
    """
    row = Organization.objects.filter(pk=org.pk).values("sms_sent_month", "sms_sent_count").first()
    if not row or row["sms_sent_month"] != _month_start():
        return 0
    return row["sms_sent_count"]


def _count_sent(org, parts: int) -> None:
    """Add ``parts`` to the ISP's count for this month, starting a new month at 0 (one atomic UPDATE)."""
    month = _month_start()
    Organization.objects.filter(pk=org.pk).update(
        sms_sent_count=Case(
            When(sms_sent_month=month, then=F("sms_sent_count") + parts), default=Value(parts), output_field=IntegerField()
        ),
        sms_sent_month=month,
    )


def _record(log: SMSLog) -> None:
    if settings.SMS_LOG_ENABLED:
        log.save()


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
    """Send one message. Never raises. Returns an ``SMSLog`` whose ``sms_status`` is SENT / FAILED / BLOCKED.

    ``billable`` messages count against the ISP's monthly limit and are not sent (BLOCKED) once it is used up;
    each one that goes out adds its parts to the ISP's counter. Platform notices (``billable=False``, or no
    organization) are never limited or counted. The row is only saved when ``SMS_LOG_ENABLED``.
    """
    segments = sms_segments(message) or 1
    counted = billable and org is not None
    log = SMSLog(organization=org, customer=customer, to=to, message=message, segments=segments, billable=counted)
    if counted and sms_remaining(org) < segments:
        log.sms_status = SMSStatus.BLOCKED
        _record(log)
        logger.warning("SMS blocked: %s is over its monthly limit", org)
        return log
    try:
        ok = SMS.send_single_sms(to=to_sms_number(to), message=message)
    except Exception:
        logger.exception("SMS send crashed")
        ok = False
    log.sms_status = SMSStatus.SENT if ok else SMSStatus.FAILED
    if ok and counted:
        _count_sent(org, segments)
    _record(log)
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
    if ok:
        _count_sent(org, used)
    if settings.SMS_LOG_ENABLED:
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
    """Send one SMS if the org has SMS enabled (counted against its monthly limit)."""
    if not org.sms_feature or not customer or not customer.phone:
        return None
    message = render_message(org, kind, name=customer.name, **ctx)
    log = _deliver(org, customer, customer.phone, message)
    return None if log.sms_status == SMSStatus.BLOCKED else log  # callers treat a log as "an SMS went out"
