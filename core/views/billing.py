"""Subscription fee collection: ISP submits, platform owner approves."""

import uuid
from decimal import Decimal

from django.conf import settings
from django.db import transaction
from django.db.models import Count, Sum
from django.http import HttpResponseRedirect
from django.utils import timezone
from rest_framework import status
from rest_framework.generics import ListAPIView, RetrieveUpdateAPIView
from rest_framework.response import Response
from rest_framework.throttling import ScopedRateThrottle
from rest_framework.views import APIView

from common.bkash import BkashClient, BkashError
from core.audit import audit
from core.choices import SubscriptionStatus
from core.models import Lead, Organization, Subscription, SubscriptionPayment
from core.permissions import AllowAny, IsAdminUser, IsAuthenticated, IsSuperAdmin
from core.serializers.billing import RenewSerializer, SubscriptionPaymentSerializer
from core.serializers.lead import LeadCreateSerializer, LeadSerializer
from core.serializers.subscription import SubscriptionListSerializer
from core.services.billing import (
    approve_subscription_payment,
    finalize_subscription_payment,
    platform_bkash_credentials,
)


class BillingInfo(APIView):
    """What an ISP needs to renew: current plan, where to pay, past payments."""

    permission_classes = [IsAuthenticated]

    def get(self, request):
        org = request.user.organization
        if not org:
            return Response({"message": "Organization not found."}, status=404)
        history = org.subscription_payments.exclude(
            payment_status=SubscriptionPayment.INITIATED
        ).select_related("organization", "plan")[:20]
        return Response(
            {
                "current": {
                    "plan": org.subscription.name if org.subscription else None,
                    "status": org.subscription_status,
                    "active": org.is_subscription_active(),
                    "end_date": org.subscription_end_date,
                },
                "online_bkash": platform_bkash_credentials().configured,
                "pay_to": {
                    "BKASH": settings.PLATFORM_BKASH_NUMBER,
                    "NAGAD": settings.PLATFORM_NAGAD_NUMBER,
                    "ROCKET": settings.PLATFORM_ROCKET_NUMBER,
                },
                "plans": SubscriptionListSerializer(
                    Subscription.objects.filter(is_public=True, price__gt=0).order_by("price"),
                    many=True,
                ).data,
                "history": SubscriptionPaymentSerializer(history, many=True).data,
            }
        )


class Renew(APIView):
    permission_classes = [IsAdminUser]

    def post(self, request):
        org = request.user.organization
        if not org:
            return Response({"message": "Organization not found."}, status=404)
        serializer = RenewSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        d = serializer.validated_data
        plan = serializer.plan
        SubscriptionPayment.objects.create(
            organization=org, plan=plan, months=d["months"], amount=plan.price * d["months"],
            payment_method=d["payment_method"], transaction_id=d["transaction_id"],
            sender_number=d.get("sender_number", ""),
        )
        return Response(
            {"message": "Payment submitted. We will activate your plan after verifying it."},
            status=status.HTTP_201_CREATED,
        )


class RenewBkash(APIView):
    """Start an online bKash checkout; the fee lands in the platform's merchant account."""

    permission_classes = [IsAdminUser]

    def post(self, request):
        org = request.user.organization
        creds = platform_bkash_credentials()
        if not org or not creds.configured:
            return Response({"message": "Online payment is not available."}, status=400)
        plan = Subscription.objects.filter(
            id=request.data.get("plan_id"), is_public=True, price__gt=0
        ).first()
        try:
            months = int(request.data.get("months", 1))
        except (TypeError, ValueError):
            months = 0
        if not plan or not 1 <= months <= 12:
            return Response({"message": "Choose a paid plan and 1–12 months."}, status=400)

        amount = plan.price * months
        invoice = f"SUB{org.id}-{uuid.uuid4().hex[:12]}"
        try:
            data = BkashClient(creds).create_payment(
                amount=amount, invoice=invoice, payer=org.phone,
                callback_url=f"{settings.PUBLIC_BASE_URL}/api/v1/billing/bkash/callback",
            )
        except BkashError as e:
            return Response({"message": str(e)}, status=status.HTTP_502_BAD_GATEWAY)

        SubscriptionPayment.objects.create(
            organization=org, plan=plan, months=months, amount=amount, payment_method="BKASH",
            transaction_id=f"online-{invoice}", gateway_payment_id=data["paymentID"],
            payment_status=SubscriptionPayment.INITIATED,
        )
        return Response({"bkash_url": data["bkashURL"]})


class BkashCallback(APIView):
    """bKash sends the browser here after checkout. We confirm server-side before activating."""

    permission_classes = [AllowAny]
    authentication_classes = []

    def get(self, request):
        payment_id = request.query_params.get("paymentID", "")
        flag = request.query_params.get("status", "")

        def done(result):
            return HttpResponseRedirect(f"{settings.PUBLIC_BASE_URL}/billing?bkash={result}")

        with transaction.atomic():
            payment = (
                SubscriptionPayment.objects.select_for_update()
                .select_related("organization", "plan")
                .filter(gateway_payment_id=payment_id)
                .first()
            )
            if not payment:
                return done("failed")
            if payment.payment_status != SubscriptionPayment.INITIATED:
                return done("success" if payment.payment_status == SubscriptionPayment.APPROVED else "failed")
            if flag != "success":
                payment.payment_status = SubscriptionPayment.REJECTED
                payment.review_note = f"bKash checkout {flag or 'not completed'}"
                payment.save()
                return done("cancelled" if flag == "cancel" else "failed")

            try:
                result = BkashClient(platform_bkash_credentials()).execute_payment(payment_id)
            except BkashError as e:
                payment.payment_status = SubscriptionPayment.REJECTED
                payment.review_note = str(e)[:255]
                payment.save()
                return done("failed")

            ok = finalize_subscription_payment(payment, result)
        return done("success" if ok else "failed")


class PlatformPayments(ListAPIView):
    serializer_class = SubscriptionPaymentSerializer
    permission_classes = [IsSuperAdmin]

    def get_queryset(self):
        qs = SubscriptionPayment.objects.exclude(
            payment_status=SubscriptionPayment.INITIATED
        ).select_related("organization", "plan")
        if self.request.query_params.get("payment_status"):
            qs = qs.filter(payment_status=self.request.query_params["payment_status"])
        return qs


class PlatformPaymentReview(APIView):
    permission_classes = [IsSuperAdmin]

    def post(self, request, pk, action):
        if action not in ("approve", "reject"):
            return Response({"message": "Action must be approve or reject."}, status=400)
        payment = SubscriptionPayment.objects.filter(
            pk=pk, payment_status=SubscriptionPayment.PENDING
        ).select_related("organization", "plan").first()
        if not payment:
            return Response({"message": "Pending payment not found."}, status=404)
        if action == "approve":
            approve_subscription_payment(payment, request.user)
        else:
            payment.payment_status = SubscriptionPayment.REJECTED
            payment.review_note = str(request.data.get("note", ""))[:255]
            payment.reviewed_by, payment.reviewed_at = request.user, timezone.now()
            payment.save()
        audit(request, f"subscription.{action}", f"org:{payment.organization_id}",
              f"{payment.plan.name} x{payment.months} {payment.transaction_id}", organization=payment.organization)
        return Response(SubscriptionPaymentSerializer(payment).data)


class PlatformStats(APIView):
    """Platform-owner overview: tenants by state, MRR, pending approvals."""

    permission_classes = [IsSuperAdmin]

    def get(self, request):
        orgs = Organization.objects.all()
        by_status = dict(orgs.values_list("subscription_status").annotate(n=Count("id")))
        soon = timezone.localdate() + timezone.timedelta(days=7)
        mrr = (
            orgs.filter(subscription_status=SubscriptionStatus.ACTIVE, subscription__isnull=False)
            .aggregate(v=Sum("subscription__price"))["v"]
            or Decimal("0")
        )
        expiring = orgs.filter(
            subscription_status__in=[SubscriptionStatus.TRIAL, SubscriptionStatus.ACTIVE],
            subscription_end_date__lte=soon,
        ).values("id", "name", "phone", "subscription_status", "subscription_end_date")
        return Response(
            {
                "organizations": orgs.count(),
                "by_status": by_status,
                "total_customers": orgs.aggregate(v=Sum("total_customer"))["v"] or 0,
                "mrr": mrr,
                "pending_payments": SubscriptionPayment.objects.filter(
                    payment_status=SubscriptionPayment.PENDING
                ).count(),
                "expiring_soon": list(expiring[:20]),
            }
        )


class LeadCreate(APIView):
    """Public: the website's contact / demo form."""

    permission_classes = [AllowAny]
    authentication_classes = []
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = "lead"

    def post(self, request):
        serializer = LeadCreateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data
        if data.pop("website", ""):  # bot filled the honeypot: pretend success, store nothing
            return Response({"message": "Thanks! We will contact you soon."}, status=201)
        lead = Lead.objects.create(**data)
        notify = settings.PLATFORM_NOTIFY_PHONE
        if notify:
            from customer.services.sms import _deliver  # logs + never raises

            _deliver(None, None, notify, f"নতুন লিড: {lead.name} ({lead.company or '-'}) {lead.phone}")
        return Response({"message": "Thanks! We will contact you soon."}, status=201)


class PlatformLeads(ListAPIView):
    serializer_class = LeadSerializer
    permission_classes = [IsSuperAdmin]

    def get_queryset(self):
        qs = Lead.objects.all()
        if self.request.query_params.get("lead_status"):
            qs = qs.filter(lead_status=self.request.query_params["lead_status"])
        return qs


class PlatformLeadDetail(RetrieveUpdateAPIView):
    serializer_class = LeadSerializer
    permission_classes = [IsSuperAdmin]
    queryset = Lead.objects.all()
