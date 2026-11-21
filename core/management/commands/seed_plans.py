"""Create the default public pricing plans (idempotent)."""

from django.core.management.base import BaseCommand

from core.choices import SubscriptionType
from core.models import Subscription

PLANS = [
    dict(name="Starter", plan=SubscriptionType.FREE, price=0, max_customers=50, max_users=2, trial_days=14,
         sms_feature=False, features=["৫০ জন গ্রাহক পর্যন্ত", "মাসিক বিল জেনারেশন", "পেমেন্ট ট্র্যাকিং", "১৪ দিন ফ্রি ট্রায়াল"]),
    dict(name="Basic", plan=SubscriptionType.BASIC, price=500, max_customers=300, max_users=5, trial_days=0,
         sms_feature=True, sms_monthly_quota=300, features=["৩০০ জন গ্রাহক পর্যন্ত", "MikroTik অটো সংযোগ বন্ধ/চালু", "এসএমএস নোটিফিকেশন", "রিপোর্ট ও খরচ হিসাব"]),
    dict(name="Premium", plan=SubscriptionType.PREMIUM, price=1200, max_customers=1500, max_users=15, trial_days=0,
         sms_feature=True, sms_monthly_quota=1500, features=["১৫০০ জন গ্রাহক পর্যন্ত", "কাস্টমার পোর্টাল (bKash/Nagad)", "অভিযোগ ব্যবস্থাপনা", "অগ্রাধিকার সাপোর্ট"]),
    dict(name="Enterprise", plan=SubscriptionType.ENTERPRISE, price=3000, max_customers=10000, max_users=50, trial_days=0,
         sms_feature=True, sms_monthly_quota=5000, features=["১০,০০০ গ্রাহক পর্যন্ত", "সব ফিচার", "ডেডিকেটেড সাপোর্ট"]),
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
