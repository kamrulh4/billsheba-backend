"""Turning a confirmed gateway result into money on the books (shared by callback + reconciliation)."""

import logging
from datetime import timedelta
from decimal import Decimal

from django.db import transaction
from django.utils import timezone

from common.bkash import BkashClient, BkashError, is_completed
from customer.choices import OnlinePaymentStatus
from customer.models import OnlinePayment
from customer.services.payments import allocate_online_payment


def finalize_online_payment(op: OnlinePayment, result: dict) -> bool:
    """Record ``op`` as paid if ``result`` (execute/query response) proves it. Caller holds the row lock."""
    if op.online_status != OnlinePaymentStatus.INITIATED:
        return op.online_status == OnlinePaymentStatus.COMPLETED
    if not is_completed(result) or Decimal(str(result.get("amount", "0"))) != op.amount:
        op.online_status = OnlinePaymentStatus.FAILED
        op.failure_reason = str(result.get("statusMessage", "Payment not completed"))[:255]
        op.save()
        return False
    op.trx_id = result["trxID"]
    op.payment = allocate_online_payment(
        op.organization, op.customer, op.amount, method="BKASH", transaction_id=op.trx_id
    )
    op.online_status = OnlinePaymentStatus.COMPLETED
    op.save()
    return True


logger = logging.getLogger(__name__)
GRACE = timedelta(minutes=10)      # give the normal browser callback time to arrive first
EXPIRE = timedelta(minutes=30)     # an unfinished checkout older than this is dead
ABANDON = timedelta(days=2)


def reconcile_online_payments() -> dict:
    """Settle checkouts whose browser callback never arrived (tab closed, network drop).

    Asks bKash directly for the final state, so money that really moved is never missed.
    """
    now = timezone.now()
    stats = {"completed": 0, "failed": 0, "waiting": 0}
    stale = OnlinePayment.objects.filter(
        online_status=OnlinePaymentStatus.INITIATED, created_at__lte=now - GRACE
    ).values_list("pk", flat=True)
    for pk in stale:
        with transaction.atomic():
            op = (
                OnlinePayment.objects.select_for_update()
                .select_related("organization", "customer", "customer__package")
                .get(pk=pk)
            )
            if op.online_status != OnlinePaymentStatus.INITIATED:
                continue
            try:
                result = BkashClient(op.organization.get_bkash_credentials()).query_payment(
                    op.gateway_payment_id
                )
            except BkashError as e:
                logger.warning("Reconcile query failed for %s: %s", op.invoice_number, e)
                result = {}
            if is_completed(result):
                stats["completed" if finalize_online_payment(op, result) else "failed"] += 1
            elif now - op.created_at > EXPIRE or now - op.created_at > ABANDON:
                op.online_status = OnlinePaymentStatus.FAILED
                op.failure_reason = "Not completed (expired)"
                op.save()
                stats["failed"] += 1
            else:
                stats["waiting"] += 1
    return stats
