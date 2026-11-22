"""Manual triggers for billing jobs.

Per-organization actions are queued on Celery (falling back to running inline when no worker/broker is
reachable). Platform-wide actions are always queued and restricted to the platform owner.
"""

import logging

from django.conf import settings
from rest_framework import status
from rest_framework.response import Response
from rest_framework.views import APIView

from core.permissions import HasActiveSubscription, HasCronSecret, IsAdminUser, IsManager, IsSuperAdmin
from customer.services import billing
from customer.services.jobs import run_in_background
from customer.tasks import (
    daily_billing_cycle,
    deactivate_all_organizations_expired_subscription_customers,
    deactivate_due_payment_customers,
    deactivate_expired_subscription_customers,
    deactivate_organizations_due_payment_customers,
    generate_customer_bills,
    generate_organizations_bills,
    reconcile_payments,
)


logger = logging.getLogger(__name__)


class _OrgJob(APIView):
    permission_classes = [(IsAdminUser | IsManager) & HasActiveSubscription]

    def _org(self, request):
        org = request.user.organization
        if not org:
            return None, Response(
                {"message": "This user doesn't belong to any organization"},
                status=status.HTTP_400_BAD_REQUEST,
            )
        return org, None


class GenerateBillTask(_OrgJob):
    def get(self, request, *args, **kwargs):
        org, err = self._org(request)
        if err:
            return err
        result = run_in_background(generate_customer_bills, org, lambda: billing.generate_bills(org))
        message = "Bill generation started in the background." if result["queued"] else "Bills generated."
        return Response({"message": message, **result})


class DeactiveDueCustomer(_OrgJob):
    def get(self, request, *args, **kwargs):
        org, err = self._org(request)
        if err:
            return err
        if not org.router_configured:
            return Response(
                {"message": "Organization MikroTik router details are not configured."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        result = run_in_background(deactivate_due_payment_customers, org, lambda: billing.suspend_overdue(org))
        message = "Suspending overdue customers in the background." if result["queued"] else "Overdue customers suspended."
        return Response({"message": message, **result})


class DeactiveExpiredSubscriptionCustomer(_OrgJob):
    def get(self, request, *args, **kwargs):
        org, err = self._org(request)
        if err:
            return err
        result = run_in_background(
            deactivate_expired_subscription_customers, org, lambda: billing.suspend_expired_subscriptions(org)
        )
        message = "Suspending expired customers in the background." if result["queued"] else "Expired customers suspended."
        return Response({"message": message, **result})


class _PlatformJob(APIView):
    """Runs for every ISP. Called by the platform owner, or by the scheduler with the ``X-Cron-Secret`` header."""

    permission_classes = [IsSuperAdmin | HasCronSecret]
    task = None

    def get(self, request, *args, **kwargs):
        if settings.BILLING_JOBS_IN_BACKGROUND:
            try:
                self.task.apply_async(retry=False)  # fail fast when Redis is down
                return Response({"message": "Background task started!", "queued": True})
            except Exception:
                logger.warning("Celery is not reachable, running %s now", self.task.name, exc_info=True)
        return Response({"message": "Task finished.", "queued": False, "result": self.task()})


class GenerateOrganizationsBillTask(_PlatformJob):
    task = generate_organizations_bills


class DeactiveOrganizationsDueCustomer(_PlatformJob):
    task = deactivate_organizations_due_payment_customers


class DailyCycleTask(_PlatformJob):
    task = daily_billing_cycle


class DeactiveOrganizationsExpiredCustomer(_PlatformJob):
    task = deactivate_all_organizations_expired_subscription_customers


class ReconcilePaymentsTask(_PlatformJob):
    task = reconcile_payments
