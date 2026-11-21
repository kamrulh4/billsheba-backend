"""Bangladesh-specific helpers: phone numbers, billing period, money."""

import re
from decimal import Decimal

from django.utils import timezone

_BD_MOBILE = re.compile(r"^01[3-9]\d{8}$")


def normalize_bd_phone(raw: str | None) -> str | None:
    """Return a local ``01XXXXXXXXX`` number, or ``None`` if it isn't a BD mobile."""
    if not raw:
        return None
    digits = re.sub(r"[^\d+]", "", str(raw))
    if digits.startswith("+880"):
        digits = "0" + digits[4:]
    elif digits.startswith("880"):
        digits = "0" + digits[3:]
    elif digits.startswith("1") and len(digits) == 10:
        digits = "0" + digits
    return digits if _BD_MOBILE.match(digits) else None


def to_sms_number(phone: str) -> str:
    """Gateway format (``8801XXXXXXXXX``); unknown formats are passed through."""
    local = normalize_bd_phone(phone)
    return f"88{local}" if local else phone


def current_period() -> tuple[str, int]:
    """(``"OCTOBER"``, 2026) in Asia/Dhaka local time, matching ``Months`` values."""
    now = timezone.localtime()
    return now.strftime("%B").upper(), now.year


def taka(amount) -> str:
    return f"৳{Decimal(amount or 0):,.2f}"
