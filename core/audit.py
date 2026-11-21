"""Tiny audit-trail helper. Never raises: logging must not break the action being logged."""

import logging

from core.models import AuditLog

logger = logging.getLogger(__name__)


def client_ip(request) -> str | None:
    if request is None:
        return None
    forwarded = request.META.get("HTTP_X_FORWARDED_FOR", "")
    return (forwarded.split(",")[0].strip() or request.META.get("REMOTE_ADDR")) or None


def audit(request, action: str, target: str = "", detail: str = "", *, user=None, organization=None):
    try:
        actor = user or (request.user if request is not None and request.user.is_authenticated else None)
        AuditLog.objects.create(
            organization_id=getattr(organization, "id", None) or getattr(actor, "organization_id", None),
            user=actor,
            action=action,
            target=str(target)[:120],
            detail=str(detail)[:500],
            ip_address=client_ip(request),
        )
    except Exception:
        logger.exception("audit log failed for %s", action)
