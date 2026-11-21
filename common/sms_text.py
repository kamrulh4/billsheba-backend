"""How many SMS a message really costs.

A message is one SMS only while it is short: 160 characters in the plain GSM alphabet, but just **70 when
it contains any Bangla** (or other non-GSM) character.  Longer messages are split into parts of 153 / 67
characters and every part is billed as a separate SMS - a 140-letter Bangla text costs 3 SMS, not 1.
"""

import math

_GSM_BASIC = (
    "@£$¥èéùìòÇ\nØø\rÅåΔ_ΦΓΛΩΠΨΣΘΞÆæßÉ !\"#¤%&'()*+,-./0123456789:;<=>?¡"
    "ABCDEFGHIJKLMNOPQRSTUVWXYZÄÖÑÜ§¿abcdefghijklmnopqrstuvwxyzäöñüà"
)
_GSM_EXTENDED = "^{}\\[~]|€"  # each takes two characters
GSM_BASIC = frozenset(_GSM_BASIC)
GSM_EXTENDED = frozenset(_GSM_EXTENDED)


def sms_info(text: str) -> dict:
    """``{"length", "segments", "unicode", "per_segment"}`` for ``text`` (0 segments when empty)."""
    text = text or ""
    if all(c in GSM_BASIC or c in GSM_EXTENDED for c in text):
        length = sum(2 if c in GSM_EXTENDED else 1 for c in text)
        single, multi, unicode_ = 160, 153, False
    else:
        length = sum(2 if ord(c) > 0xFFFF else 1 for c in text)  # UTF-16 code units
        single, multi, unicode_ = 70, 67, True
    if length == 0:
        segments = 0
    elif length <= single:
        segments = 1
    else:
        segments = math.ceil(length / multi)
    return {"length": length, "segments": segments, "unicode": unicode_, "per_segment": single if segments <= 1 else multi}


def sms_segments(text: str) -> int:
    return sms_info(text)["segments"]
