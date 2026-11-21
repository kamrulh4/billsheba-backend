from collections import defaultdict
from datetime import date, datetime, time, timedelta

from django.db.models import Q
from django.utils import timezone
from rest_framework import status
from rest_framework.generics import ListCreateAPIView, RetrieveUpdateDestroyAPIView
from rest_framework.response import Response
from rest_framework.views import APIView

from core.audit import audit
from core.permissions import (
    HasActiveSubscription,
    IsAdminUser,
    IsAuthenticated,
    IsManager,
    IsStaff,
)
from customer.models import Payment
from customer.serializers.payment import (
    PaymentDetailSerializer,
    PaymentListSerializer,
)


def day_start(day):
    """Midnight (local time) at the start of ``day`` as an aware datetime."""
    return timezone.make_aware(datetime.combine(day, time.min))


class PaymentsList(ListCreateAPIView):
    serializer_class = PaymentListSerializer

    def perform_create(self, serializer):
        payment = serializer.save()
        audit(self.request, "payment.create", f"payment:{payment.pk}",
              f"{payment.customer.name} {payment.billing_month} {payment.billing_year} ৳{payment.amount}"
              + (f" discount ৳{payment.discount}" if payment.discount else ""))
    permission_classes = [(IsAdminUser | IsManager | IsStaff) & HasActiveSubscription]

    # def get_permissions(self):
    #     if self.request.method in SAFE_METHODS:
    #         return [(IsAdminUser | IsManager | IsStaff)()]
    #     return [
    #         (IsAdminUser | IsManager)()
    # ]  # Only Admin and Manager can create payments

    def get_queryset(self):
        if not self.request.user.organization_id:
            return Payment.objects.none()

        start_date = self.request.query_params.get("start_date", None)
        end_date = self.request.query_params.get("end_date", None)
        if not start_date:
            start_date = timezone.localdate().replace(day=1)
        else:
            try:
                if len(start_date) > 10:
                    start_date = timezone.datetime.fromisoformat(start_date).date()
                else:
                    start_date = timezone.datetime.strptime(
                        start_date, "%Y-%m-%d"
                    ).date()
            except (ValueError, TypeError):
                start_date = timezone.localdate().replace(day=1)
        if not end_date:
            end_date = timezone.localdate()
        else:
            try:
                if len(end_date) > 10:
                    end_date = timezone.datetime.fromisoformat(end_date).date()
                else:
                    end_date = timezone.datetime.strptime(end_date, "%Y-%m-%d").date()
            except (ValueError, TypeError):
                end_date = timezone.localdate()
        queryset = (
            Payment()
            .get_all_actives()
            .filter(
                organization_id=self.request.user.organization_id,
            )
            .filter(
                # a plain datetime range (not __date, which wraps the column in a function)
                # lets the database use the payment_date index
                Q(
                    payment_date__gte=day_start(start_date),
                    payment_date__lt=day_start(end_date + timedelta(days=1)),
                )
                | Q(
                    payment_date__isnull=True,
                )
            )
            .order_by("-payment_date")
            .select_related("customer", "entry_by")
        )
        params = self.request.query_params
        if params.get("paid") is not None:
            queryset = queryset.filter(paid=params["paid"].lower() == "true")
        if params.get("search"):
            queryset = queryset.filter(
                Q(customer__name__icontains=params["search"])
                | Q(customer__phone__icontains=params["search"])
                | Q(customer__username__icontains=params["search"])
            )
        return queryset


class PaymentDetail(RetrieveUpdateDestroyAPIView):
    serializer_class = PaymentDetailSerializer

    def perform_update(self, serializer):
        payment = serializer.save()
        audit(self.request, "payment.update", f"payment:{payment.pk}",
              f"amount ৳{payment.amount}, discount ৳{payment.discount}, paid={payment.paid}")

    def perform_destroy(self, instance):
        audit(self.request, "payment.delete", f"payment:{instance.pk}",
              f"{instance.customer.name} {instance.billing_month} {instance.billing_year} ৳{instance.amount}")
        instance.delete()
    lookup_field = "uid"

    def get_permissions(self):
        # Only Admin / Manager can DELETE
        if self.request.method == "DELETE":
            return [((IsAdminUser | IsManager) & HasActiveSubscription)()]
        if self.request.method in ("GET", "HEAD", "OPTIONS"):
            return [(IsAdminUser | IsManager | IsStaff)()]
        # Admin, Manager or Staff can update
        return [((IsAdminUser | IsManager | IsStaff) & HasActiveSubscription)()]

    def get_queryset(self):
        queryset = (
            Payment()
            .get_all_actives()
            .filter(organization_id=self.request.user.organization_id)
            .select_related("customer", "entry_by")
        )

        return queryset


class MonthlyCollectionList(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request, format=None):
        if not request.user.organization_id:
            return Response({"detail": "Organization not found."}, status=404)

        # Get query parameters
        user_id = request.query_params.get("user_id", None)
        start_date = request.query_params.get("start_date", None)
        end_date = request.query_params.get("end_date", None)

        now = timezone.localtime()

        # Determine date range
        if start_date:
            try:
                # Support both YYYY-MM-DD and full ISO format
                if len(start_date) > 10:
                    start_date = timezone.datetime.fromisoformat(start_date).date()
                else:
                    start_date = timezone.datetime.strptime(
                        start_date, "%Y-%m-%d"
                    ).date()
            except (ValueError, TypeError):
                start_date = now.date().replace(day=1)
        else:
            start_date = now.date().replace(day=1)

        if end_date:
            try:
                if len(end_date) > 10:
                    end_date = timezone.datetime.fromisoformat(end_date).date()
                else:
                    end_date = timezone.datetime.strptime(end_date, "%Y-%m-%d").date()
            except (ValueError, TypeError):
                end_date = now.date()
        else:
            end_date = now.date()

        # Base filter - Using __date lookup to filter strictly on the date portion
        filters = Q(
            organization_id=request.user.organization_id,
            payment_date__gte=day_start(start_date),
            payment_date__lt=day_start(end_date + timedelta(days=1)),
            paid=True,
        )

        # Optional user filter
        if user_id:
            try:
                filters &= Q(entry_by_id=int(user_id))
            except (ValueError, TypeError):
                pass

        # .values() skips building a model object per row: this report can be thousands of rows long.
        monthly_payments = (
            Payment.objects.filter(filters)
            .order_by("entry_by_id", "payment_date")
            .values(
                "entry_by_id", "entry_by__first_name", "entry_by__last_name",
                "customer__name", "customer__phone", "customer__address",
                "bill_amount", "amount", "payment_date",
            )
        )

        # Group payments by collecting user
        collections_by_user = defaultdict(lambda: {"payments": [], "total_amount": 0})

        for row in monthly_payments.iterator(chunk_size=2000):
            user_key = (
                row["entry_by_id"],
                row["entry_by__first_name"] or "",
                row["entry_by__last_name"] or "",
            )
            collections_by_user[user_key]["payments"].append(
                {
                    "customer_name": row["customer__name"],
                    "customer_phone": row["customer__phone"],
                    "customer_address": row["customer__address"],
                    "bill_amount": row["bill_amount"],
                    "amount": row["amount"],
                    "payment_date": timezone.localtime(row["payment_date"]).date(),
                }
            )
            collections_by_user[user_key]["total_amount"] += row["amount"]

        # Format the response
        result = []
        for (user_id, first_name, last_name), data in collections_by_user.items():
            result.append(
                {
                    "user_id": user_id,
                    "first_name": first_name,
                    "last_name": last_name,
                    "total_payments": len(data["payments"]),
                    "total_amount": data["total_amount"],
                    "payments": data["payments"],
                }
            )

        return Response(
            {
                "results": result,
                "message": "Monthly collections retrieved successfully",
            },
            status=status.HTTP_200_OK,
        )


class PaymentReceipt(APIView):
    """Everything a printed receipt needs, for one payment of the caller's ISP."""

    permission_classes = [IsAdminUser | IsManager | IsStaff]

    def get(self, request, uid):
        payment = (
            Payment.objects.filter(uid=uid, organization_id=request.user.organization_id)
            .select_related("customer", "customer__package", "organization", "entry_by")
            .first()
        )
        if not payment:
            return Response({"message": "Payment not found."}, status=404)
        org, customer = payment.organization, payment.customer
        return Response(
            {
                "receipt_no": f"R{payment.pk:06d}",
                "uid": str(payment.uid),
                "organization": {
                    "name": org.name, "phone": org.phone, "address": org.address, "email": org.email,
                },
                "customer": {
                    "name": customer.name, "phone": customer.phone, "username": customer.username,
                    "address": customer.address, "area": customer.area,
                    "package": customer.package.name if customer.package else None,
                },
                "billing_month": payment.billing_month,
                "billing_year": payment.billing_year,
                "bill_amount": payment.bill_amount,
                "amount": payment.amount,
                "discount": payment.discount,
                "due": payment.due_amount,
                "paid": payment.paid,
                "payment_method": payment.payment_method,
                "transaction_id": payment.transaction_id,
                "payment_date": payment.payment_date,
                "received_by": (
                    f"{payment.entry_by.first_name} {payment.entry_by.last_name}".strip()
                    if payment.entry_by
                    else None
                ),
            }
        )


class PaymentsExport(APIView):
    """CSV of payments in a date range (default: this month) for the accountant."""

    permission_classes = [IsAdminUser | IsManager]

    def get(self, request):
        import csv

        from django.http import StreamingHttpResponse

        today = timezone.localdate()
        start = request.query_params.get("start_date") or today.replace(day=1).isoformat()
        end = request.query_params.get("end_date") or today.isoformat()
        try:
            start_day, end_day = date.fromisoformat(start), date.fromisoformat(end)
        except ValueError:
            return Response({"message": "start_date and end_date must look like 2026-11-30."}, status=400)
        rows = (
            Payment.objects.filter(
                organization_id=request.user.organization_id,
                payment_date__gte=day_start(start_day),
                payment_date__lt=day_start(end_day + timedelta(days=1)),
                amount__gt=0,
            )
            .order_by("payment_date")
            .values_list(
                "payment_date", "customer__name", "customer__phone", "customer__username",
                "billing_month", "billing_year", "bill_amount", "discount", "amount", "payment_method",
                "transaction_id", "entry_by__first_name",
            )
        )

        class Echo:  # csv.writer needs a file-like object; we stream each line instead of buffering
            def write(self, value):
                return value

        writer = csv.writer(Echo())

        def lines():
            yield "\ufeff" + writer.writerow(
                ["Date", "Customer", "Phone", "Username", "Month", "Year", "Bill", "Discount", "Paid", "Method", "TrxID", "Received by"]
            )
            for paid_at, *rest, collector in rows.iterator(chunk_size=2000):
                stamp = timezone.localtime(paid_at).strftime("%Y-%m-%d %H:%M") if paid_at else ""
                yield writer.writerow([stamp, *rest, collector or ""])

        response = StreamingHttpResponse(lines(), content_type="text/csv; charset=utf-8")
        response["Content-Disposition"] = f'attachment; filename="payments_{start}_{end}.csv"'
        return response
