"""Create the default public pricing plans (idempotent)."""

from django.core.management.base import BaseCommand

from core.choices import SubscriptionType
from core.models import Subscription

# Three public plans, priced by how many customers the ISP manages. The SMS quota is what keeps a plan's
# cost bounded (every SMS is billed to us), so it grows with the plan. The 14-day free trial runs on the
# smallest plan, with its SMS capped (see Organization.TRIAL_SMS_LIMIT).
PLANS = [
    dict(name="Basic", plan=SubscriptionType.BASIC, price=1000, max_customers=200, max_users=5, trial_days=14,
         sms_feature=True, sms_monthly_quota=500, is_public=True,
         features=["২০০ জন গ্রাহক পর্যন্ত", "মাসে ৫০০ এসএমএস", "MikroTik অটো সংযোগ বন্ধ/চালু", "রিপোর্ট ও খরচ হিসাব", "১৪ দিন ফ্রি ট্রায়াল"]),
    dict(name="Standard", plan=SubscriptionType.STANDARD, price=2000, max_customers=500, max_users=10, trial_days=0,
         sms_feature=True, sms_monthly_quota=1250, is_public=True,
         features=["৫০০ জন গ্রাহক পর্যন্ত", "মাসে ১২৫০ এসএমএস", "কাস্টমার পোর্টাল (bKash/Nagad)", "অভিযোগ ব্যবস্থাপনা"]),
    dict(name="Premium", plan=SubscriptionType.PREMIUM, price=3000, max_customers=1000, max_users=20, trial_days=0,
         sms_feature=True, sms_monthly_quota=2500, is_public=True,
         features=["১০০০ জন গ্রাহক পর্যন্ত", "মাসে ২৫০০ এসএমএস", "সব ফিচার", "অগ্রাধিকার সাপোর্ট"]),
    # Not shown to the public. The platform owner puts a big ISP on it and sets that ISP's own customer limit
    # (allowed_customer) and SMS limit (sms_quota_override) on the organization; the price is agreed one by one.
    dict(name="Custom", plan=SubscriptionType.ENTERPRISE, price=0, max_customers=5000, max_users=50, trial_days=0,
         sms_feature=True, sms_monthly_quota=5000, is_public=False,
         features=["আলাদা চুক্তি অনুযায়ী গ্রাহক ও এসএমএস সীমা"]),
]


class Command(BaseCommand):
    help = "Seed default subscription plans"

    def add_arguments(self, parser):
        parser.add_argument(
            "--if-empty",
            action="store_true",
            help="Do nothing when plans already exist (safe on every deploy; never overwrites edited prices).",
        )

    def handle(self, *args, **options):
        if options["if_empty"] and Subscription.objects.exists():
            self.stdout.write("plans already present - left untouched")
            return
        for data in PLANS:
            _, created = Subscription.objects.update_or_create(name=data["name"], defaults=data)
            self.stdout.write(f"{'created' if created else 'updated'}: {data['name']}")
        # Older plans stay in the database (ISPs may still be on them) but are no longer offered.
        hidden = Subscription.objects.exclude(name__in=[p["name"] for p in PLANS]).filter(is_public=True)
        for plan in hidden:
            self.stdout.write(f"hidden from the pricing page: {plan.name}")
        hidden.update(is_public=False)
