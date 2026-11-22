"""Importing an ISP's existing MikroTik PPP profiles/users as packages/customers."""

from decimal import Decimal
from unittest import mock

from django.test import SimpleTestCase

from core.models import AuditLog, Organization, User
from customer.models import Customer, Package
from customer.services.router_import import parse_rate_mbps
from customer.tests.test_reliability import API, PW, Base

PROFILES = [
    {"name": "default", "rate-limit": ""},
    {"name": "Home-10M", "rate-limit": "10M/10M"},
    {"name": "Home-20M", "rate-limit": "5M/20M"},
    {"name": "Unused", "rate-limit": "2M/2M"},
]
SECRETS = [
    {"name": "rahim", "password": "p1", "profile": "Home-10M", "service": "pppoe", "disabled": "false", "comment": "Rahim Uddin 01711223344 Mirpur"},
    {"name": "fatema", "password": "p2", "profile": "Home-20M", "service": "pppoe", "disabled": "true", "comment": ""},
    {"name": "karim", "password": "x", "profile": "Home-10M", "service": "pppoe", "disabled": "false"},  # already in BillSheba
    {"name": "vpnuser", "password": "v", "profile": "default", "service": "l2tp", "disabled": "false"},  # not broadband
    {"name": "jahid", "password": "p3", "profile": "default", "service": "any", "disabled": "false", "comment": "01711223344"},
]
URL = f"{API}/organizations/me/router-import"
MK = "customer.services.router_import.Mikrotik"


class RateParsingTests(SimpleTestCase):
    def test_download_speed_is_the_second_value(self):
        self.assertEqual(parse_rate_mbps("10M/10M"), 10)
        self.assertEqual(parse_rate_mbps("5M/20M"), 20)
        self.assertEqual(parse_rate_mbps("8M"), 8)

    def test_units_and_noise(self):
        self.assertEqual(parse_rate_mbps("1G/1G"), 1000)
        self.assertEqual(parse_rate_mbps("512k/512k"), 1)  # never below 1 Mbps
        self.assertEqual(parse_rate_mbps("10M/10M 20M/20M 8M/8M 8"), 10)  # burst parts ignored
        self.assertEqual(parse_rate_mbps(""), 10)
        self.assertEqual(parse_rate_mbps(None), 10)
        self.assertEqual(parse_rate_mbps("garbage"), 10)


class RouterImportTests(Base):
    def setUp(self):
        super().setUp()
        self.org.router_ip, self.org.router_username, self.org.router_password = "https://10.0.0.1", "a", "b"
        self.org.save()

    def router(self, profiles=PROFILES, secrets=SECRETS):
        return mock.patch.multiple(MK, get_profiles=mock.Mock(return_value=(True, profiles)),
                                   get_users_from_server=mock.Mock(return_value=(True, secrets)))

    def test_preview_reads_without_creating_anything(self):
        before = (Customer.objects.count(), Package.objects.count())
        with self.router():
            res = self.client.get(URL)
        self.assertEqual(res.status_code, 200, res.content)
        d = res.data
        self.assertEqual((d["total_secrets"], d["new_customers"], d["already_imported"], d["other_services"]),
                         (5, 3, 1, 1))
        self.assertEqual(d["disabled_in_new"], 1)
        names = {p["name"]: p for p in d["profiles"]}
        self.assertEqual(names["Home-20M"]["speed_mbps"], 20)
        self.assertEqual(names["Home-10M"]["new_customers"], 1)
        # users on the router's built-in profile are listed but flagged: no package is created for it
        self.assertTrue(names["default"]["builtin"])
        self.assertEqual(names["default"]["new_customers"], 1)
        self.assertFalse(names["Home-10M"]["builtin"])
        self.assertFalse(d["exceeds_limit"])
        self.assertEqual((Customer.objects.count(), Package.objects.count()), before)

    def test_import_creates_packages_with_prices_and_customers(self):
        with self.router():
            res = self.client.post(URL, {"prices": {"Home-10M": "500", "Home-20M": "900"}}, format="json")
        self.assertEqual(res.status_code, 201, res.content)
        self.assertEqual((res.data["customers_created"], res.data["packages_created"]), (3, 2))

        p20 = Package.objects.get(name="Home-20M")
        self.assertEqual((p20.speed_mbps, p20.price, p20.mikrotik_profile), (20, Decimal("900"), "Home-20M"))
        rahim = Customer.objects.get(username="rahim")
        self.assertEqual((rahim.package.name, rahim.password, rahim.mikrotik_synced, rahim.is_active), ("Home-10M", "p1", True, True))
        self.assertEqual((rahim.phone, rahim.name), ("01711223344", "Rahim Uddin 01711223344 Mirpur"))
        self.assertFalse(Customer.objects.get(username="fatema").is_active)  # disabled on the router
        self.assertEqual(Customer.objects.get(username="jahid").package, None)  # built-in profile => no package
        self.assertFalse(Customer.objects.filter(username="vpnuser").exists())
        # a phone number already used by another customer is not duplicated
        self.assertEqual(Customer.objects.get(username="jahid").phone, "")
        self.assertEqual(Organization.objects.get().total_customer, 4)  # 1 existing + 3 imported
        self.assertTrue(AuditLog.objects.filter(action="router.import").exists())

    def test_import_is_repeatable_and_never_touches_the_router(self):
        with self.router(), mock.patch("customer.models.Mikrotik.toggle_ppp_user") as toggle:
            self.client.post(URL, {}, format="json")
            again = self.client.post(URL, {}, format="json")
        self.assertEqual(again.data["customers_created"], 0)
        self.assertEqual(Customer.objects.filter(username="rahim").count(), 1)
        toggle.assert_not_called()  # imported users already exist on the router

    def test_only_the_chosen_profiles_are_imported(self):
        with self.router():
            res = self.client.post(URL, {"profiles": ["Home-20M"]}, format="json")
        self.assertEqual((res.data["customers_created"], res.data["packages_created"]), (1, 1))
        self.assertEqual(list(Customer.objects.exclude(username="karim").values_list("username", flat=True)), ["fatema"])
        self.assertFalse(Package.objects.filter(name="Home-10M").exists())

    def test_no_chosen_profile_imports_nothing_and_bad_input_is_rejected(self):
        with self.router():
            none = self.client.post(URL, {"profiles": []}, format="json")
            bad = self.client.post(URL, {"profiles": "Home-20M"}, format="json")
        self.assertEqual(none.data["customers_created"], 0)
        self.assertEqual(bad.status_code, 400)

    def test_can_skip_disabled_users(self):
        with self.router():
            res = self.client.post(URL, {"include_disabled": False}, format="json")
        self.assertEqual(res.data["customers_created"], 2)
        self.assertFalse(Customer.objects.filter(username="fatema").exists())

    def test_customer_limit_is_respected(self):
        Organization.objects.update(allowed_customer=2)  # 1 existing + room for 1
        with self.router():
            preview = self.client.get(URL).data
            res = self.client.post(URL, {}, format="json")
        self.assertTrue(preview["exceeds_limit"])
        self.assertEqual(preview["capacity_left"], 1)
        self.assertEqual((res.data["customers_created"], res.data["skipped_over_limit"]), (1, 2))
        self.assertEqual(Customer.objects.count(), 2)

    def test_other_isps_customers_do_not_count_as_existing(self):
        other = Organization.objects.create(name="Other", phone="01722000009")
        Customer.objects.create(organization=other, name="X", username="rahim")
        with self.router():
            res = self.client.post(URL, {}, format="json")
        self.assertEqual(res.data["customers_created"], 3)

    def test_router_problems_are_reported_cleanly(self):
        with mock.patch.multiple(MK, get_profiles=mock.Mock(return_value=(False, "Request timed out")),
                                 get_users_from_server=mock.Mock()):
            res = self.client.get(URL)
        self.assertEqual(res.status_code, 400)
        self.assertIn("timed out", res.data["message"])
        Organization.objects.update(router_ip="", router_username="", router_password="")
        self.assertEqual(self.client.get(URL).status_code, 400)  # no credentials saved

    def test_only_admins_and_managers_may_import(self):
        self.new_user("STAFF", "01911000001")
        self.login("01911000001")
        with self.router():
            self.assertEqual(self.client.get(URL).status_code, 403)
            self.assertEqual(self.client.post(URL, {}, format="json").status_code, 403)
        self.client.credentials()
        self.assertEqual(self.client.get(URL).status_code, 401)

    def test_bad_prices_fall_back_to_zero(self):
        with self.router():
            self.client.post(URL, {"prices": {"Home-10M": "abc", "Home-20M": "-5"}}, format="json")
        self.assertEqual(Package.objects.get(name="Home-10M").price, Decimal("0"))
        self.assertEqual(Package.objects.get(name="Home-20M").price, Decimal("0"))
        self.assertEqual(self.client.post(URL, {"prices": "nope"}, format="json").status_code, 400)

    def test_expired_plan_blocks_import_but_not_preview(self):
        from datetime import timedelta

        from django.utils import timezone

        Organization.objects.update(subscription_end_date=timezone.localdate() - timedelta(days=1))
        with self.router():
            self.assertEqual(self.client.get(URL).status_code, 200)
            self.assertEqual(self.client.post(URL, {}, format="json").status_code, 403)
        self.assertTrue(User.objects.filter(phone="01711000001").exists() and PW)


MKH = "customer.helpers.mikrotik.Mikrotik"
PROFILES_URL = f"{API}/organizations/me/router-profiles"


class RouterProfileTests(Base):
    """Profiles on their own: fetch for the package form, create packages, and keep packages valid."""

    def setUp(self):
        super().setUp()
        self.org.router_ip, self.org.router_username, self.org.router_password = "https://10.0.0.1", "a", "b"
        self.org.save()

    def profiles(self, data=PROFILES, ok=True):
        return mock.patch(f"{MKH}.get_profiles", return_value=(ok, data))

    def test_list_profiles_shows_speed_and_linked_package(self):
        pkg = Package.objects.get()  # created in setUp as "P"
        Package.objects.filter(pk=pkg.pk).update(mikrotik_profile="Home-10M")
        with self.profiles():
            res = self.client.get(PROFILES_URL)
        self.assertEqual(res.status_code, 200, res.content)
        rows = {r["name"]: r for r in res.data["profiles"]}
        self.assertEqual(rows["Home-20M"]["speed_mbps"], 20)
        self.assertEqual(rows["Home-10M"]["package"]["name"], "P")  # already linked
        self.assertIsNone(rows["Home-20M"]["package"])
        self.assertTrue(rows["default"]["builtin"])

    def test_create_packages_from_profiles_only(self):
        customers_before = Customer.objects.count()
        with self.profiles():
            res = self.client.post(
                PROFILES_URL,
                {"profiles": [{"name": "Home-20M", "price": "900"}, {"name": "Nope", "price": "1"}]},
                format="json",
            )
        self.assertEqual(res.status_code, 201, res.content)
        self.assertEqual((res.data["packages_created"], res.data["skipped"]), (1, ["Nope"]))  # unknown profile ignored
        pkg = Package.objects.get(name="Home-20M")
        self.assertEqual((pkg.speed_mbps, pkg.price, pkg.mikrotik_profile), (20, Decimal("900"), "Home-20M"))
        self.assertEqual(Customer.objects.count(), customers_before)  # no customers touched
        with self.profiles():  # running it again does nothing
            again = self.client.post(PROFILES_URL, {"profiles": [{"name": "Home-20M", "price": "1"}]}, format="json")
        self.assertEqual(again.data["packages_created"], 0)
        self.assertEqual(Package.objects.filter(name="Home-20M").count(), 1)

    def test_package_must_point_at_a_real_router_profile(self):
        with self.profiles():
            bad = self.client.post(
                f"{API}/packages", {"name": "Gold", "speed_mbps": 10, "price": 700}, format="json"
            )
            self.assertEqual(bad.status_code, 400)
            self.assertIn("Home-10M", bad.data["message"])  # tells them what is available
            ok = self.client.post(
                f"{API}/packages",
                {"name": "Gold", "speed_mbps": 10, "price": 700, "mikrotik_profile": "Home-10M"},
                format="json",
            )
            self.assertEqual(ok.status_code, 201, ok.content)
            named_like_profile = self.client.post(
                f"{API}/packages", {"name": "Home-20M", "speed_mbps": 20, "price": 900}, format="json"
            )
            self.assertEqual(named_like_profile.status_code, 201)  # name doubles as profile when blank

    def test_editing_only_the_price_does_not_recheck_the_router(self):
        pkg = Package.objects.get()  # profile "P" does not exist on this router
        with mock.patch(f"{MKH}.get_profiles") as get_profiles:
            res = self.client.patch(f"{API}/packages/{pkg.uid}", {"price": "650"}, format="json")
        self.assertEqual(res.status_code, 200, res.content)
        get_profiles.assert_not_called()

    def test_full_form_edit_with_unchanged_profile_is_not_blocked(self):
        pkg = Package.objects.get()  # profile "P" is not on the router
        body = {"name": "P", "speed_mbps": 10, "price": "650", "description": "", "mikrotik_profile": ""}
        with self.profiles() as get_profiles:  # the UI PUTs the whole form
            res = self.client.put(f"{API}/packages/{pkg.uid}", body, format="json")
        self.assertEqual(res.status_code, 200, res.content)
        get_profiles.assert_not_called()
        with self.profiles():  # but pointing it at a profile that does not exist is refused
            bad = self.client.put(f"{API}/packages/{pkg.uid}", {**body, "mikrotik_profile": "Ghost"}, format="json")
        self.assertEqual(bad.status_code, 400)

    def test_unreachable_router_never_blocks_saving_a_package(self):
        with self.profiles("Request timed out", ok=False):
            res = self.client.post(f"{API}/packages", {"name": "Silver", "speed_mbps": 5, "price": 400}, format="json")
        self.assertEqual(res.status_code, 201, res.content)

    def test_without_a_router_packages_save_freely(self):
        Organization.objects.update(router_ip="", router_username="", router_password="")
        res = self.client.post(f"{API}/packages", {"name": "Any name", "speed_mbps": 5, "price": 400}, format="json")
        self.assertEqual(res.status_code, 201)

    def test_customer_creation_explains_a_missing_profile(self):
        pkg = Package.objects.get()
        body = {"name": "New", "phone": "01822000111", "username": "newuser", "password": "pw123456", "package_id": pkg.id}
        with mock.patch(f"{MKH}.create_ppp_user", return_value=(False, "input does not match any value of profile")):
            res = self.client.post(f"{API}/customers", body, format="json")
        self.assertEqual(res.status_code, 400)
        self.assertIn("no PPP profile 'P'", res.data["message"])
        self.assertIn("Set the package's router profile", res.data["message"])
        self.assertFalse(Customer.objects.filter(username="newuser").exists())  # nothing half-created

    def test_customer_is_created_on_the_router_with_the_package_profile(self):
        pkg = Package.objects.get()
        Package.objects.filter(pk=pkg.pk).update(mikrotik_profile="Home-10M")
        body = {"name": "New", "phone": "01822000111", "username": "newuser", "password": "pw123456", "package_id": pkg.id}
        with mock.patch(f"{MKH}.create_ppp_user", return_value=(True, "ok")) as create:
            res = self.client.post(f"{API}/customers", body, format="json")
        self.assertEqual(res.status_code, 201, res.content)
        sent = create.call_args.args[0]
        self.assertEqual((sent["username"], sent["profile"]), ("newuser", "Home-10M"))  # not the package's display name
        self.assertTrue(Customer.objects.get(username="newuser").mikrotik_synced)

    def test_permissions(self):
        self.new_user("STAFF", "01911000001")
        self.login("01911000001")
        with self.profiles():
            self.assertEqual(self.client.get(PROFILES_URL).status_code, 403)
            self.assertEqual(self.client.post(PROFILES_URL, {"profiles": [{"name": "Home-10M"}]}, format="json").status_code, 403)
        self.as_admin()
        self.assertEqual(self.client.post(PROFILES_URL, {"profiles": []}, format="json").status_code, 400)
        with self.profiles("down", ok=False):
            self.assertEqual(self.client.get(PROFILES_URL).status_code, 400)


class ReimportMatchingTests(Base):
    """Running the import again must reuse what exists — including packages mapped to a profile."""

    def setUp(self):
        super().setUp()
        self.org.router_ip, self.org.router_username, self.org.router_password = "https://10.0.0.1", "a", "b"
        self.org.save()

    def router(self):
        return mock.patch.multiple(
            MK,
            get_profiles=mock.Mock(return_value=(True, PROFILES)),
            get_users_from_server=mock.Mock(return_value=(True, SECRETS)),
        )

    def test_package_mapped_to_a_profile_is_reused_not_duplicated(self):
        silver = Package.objects.create(
            organization=self.org, name="Silver", mikrotik_profile="Home-10M", speed_mbps=10, price=Decimal("500")
        )
        with self.router():
            preview = self.client.get(URL).data
            self.client.post(URL, {"prices": {"Home-10M": "999"}}, format="json")
        rows = {p["name"]: p for p in preview["profiles"]}
        self.assertTrue(rows["Home-10M"]["package_exists"])  # recognised through its profile mapping
        self.assertFalse(Package.objects.filter(name="Home-10M").exists())  # no duplicate package
        self.assertEqual(Customer.objects.get(username="rahim").package, silver)
        self.assertEqual(Package.objects.get(pk=silver.pk).price, Decimal("500"))  # existing price untouched

    def test_existing_records_are_skipped_never_updated(self):
        with self.router():
            self.client.post(URL, {"prices": {"Home-10M": "500", "Home-20M": "900"}}, format="json")
        rahim = Customer.objects.get(username="rahim")
        Customer.objects.filter(pk=rahim.pk).update(name="Renamed locally", phone="01899999999", password="local-pw")
        Package.objects.filter(name="Home-20M").update(price=Decimal("1100"))
        with self.router():
            again = self.client.post(URL, {"prices": {"Home-20M": "1"}}, format="json")
        self.assertEqual((again.data["customers_created"], again.data["packages_created"]), (0, 0))
        rahim.refresh_from_db()
        self.assertEqual((rahim.name, rahim.phone, rahim.password), ("Renamed locally", "01899999999", "local-pw"))
        self.assertEqual(Package.objects.get(name="Home-20M").price, Decimal("1100"))
        self.assertEqual(Customer.objects.filter(username="rahim").count(), 1)
        self.assertEqual(Package.objects.filter(name="Home-10M").count(), 1)

    def test_new_router_users_are_added_on_a_later_import(self):
        with self.router():
            self.client.post(URL, {}, format="json")
        more = [*SECRETS, {"name": "newguy", "password": "n", "profile": "Home-10M", "service": "pppoe", "disabled": "false"}]
        with mock.patch.multiple(
            MK, get_profiles=mock.Mock(return_value=(True, PROFILES)), get_users_from_server=mock.Mock(return_value=(True, more))
        ):
            res = self.client.post(URL, {}, format="json")
        self.assertEqual(res.data["customers_created"], 1)
        self.assertEqual(Customer.objects.filter(username="newguy").count(), 1)
        self.assertEqual(Package.objects.filter(name="Home-10M").count(), 1)
