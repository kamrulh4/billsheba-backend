"""Self-service onboarding: create an ISP organization with its first admin."""

from datetime import timedelta

from django.conf import settings
from django.db import transaction
from django.utils import timezone

from core.choices import SubscriptionStatus, SubscriptionType, UserKind
from core.models import Organization, Subscription, User


def get_trial_plan() -> Subscription | None:
    """Plan new signups start on: first public plan with a trial, else any FREE plan."""
    return (
        Subscription.objects.filter(is_public=True, trial_days__gt=0)
        .order_by("price")
        .first()
        or Subscription.objects.filter(plan=SubscriptionType.FREE).first()
    )


@transaction.atomic
def create_organization_with_owner(
    *,
    organization_name: str,
    first_name: str,
    last_name: str,
    phone: str,
    email: str,
    password: str,
    address: str = "",
    plan: Subscription | None = None,
) -> tuple[Organization, User]:
    plan = plan or get_trial_plan()
    trial_days = (plan.trial_days if plan and plan.trial_days else None) or settings.DEFAULT_TRIAL_DAYS
    org = Organization.objects.create(
        name=organization_name,
        phone=phone,
        email=email,
        address=address,
        subscription=plan,
        subscription_status=SubscriptionStatus.TRIAL,
        subscription_end_date=timezone.localdate() + timedelta(days=trial_days),
        allowed_customer=plan.max_customers if plan else 50,
        sms_feature=bool(plan and plan.sms_feature),
    )
    user = User(
        organization=org,
        first_name=first_name,
        last_name=last_name,
        phone=phone,
        email=email,
        kind=UserKind.ADMIN,
    )
    user.set_password(password)
    user.save()
    return org, user
