"""Import an ISP's existing MikroTik PPP profiles and secrets as packages and customers.

Two steps so nothing is created by surprise: ``build_preview`` only reads the router,
``run_import`` creates records. Both re-read the router (never trust client-side data) and
both ignore usernames that already exist for the organization, so the import is repeatable.
"""

import re
from decimal import Decimal, InvalidOperation

from common.bd import normalize_bd_phone
from common.choices import Status
from customer.choices import ConnectionType
from customer.helpers import Mikrotik
from customer.models import Customer, Package

IMPORTABLE_SERVICES = {"", "any", "pppoe"}  # l2tp/ovpn/sstp/... secrets are not broadband subscribers
BUILTIN_PROFILES = {"default", "default-encryption"}
_PHONE = re.compile(r"01[3-9]\d{8}")
_RATE = re.compile(r"^(\d+(?:\.\d+)?)([kKmMgG]?)$")


class RouterImportError(Exception):
    """The router could not be read (shown to the user as-is)."""


def parse_rate_mbps(rate_limit: str | None, default: int = 10) -> int:
    """MikroTik ``rx/tx`` rate-limit -> download Mbps.  "10M/20M" -> 20, "5M" -> 5, "512k" -> 1.

    ``rx`` is the client's upload and ``tx`` its download, so the second value wins when present.
    Burst/threshold parts after the first token are ignored.
    """
    if not rate_limit:
        return default
    head = rate_limit.split()[0]
    parts = [p for p in head.split("/") if p]
    token = parts[1] if len(parts) > 1 else (parts[0] if parts else "")
    match = _RATE.match(token)
    if not match:
        return default
    value, unit = float(match.group(1)), match.group(2).lower()
    mbps = value * {"": 1e-6, "k": 1e-3, "m": 1.0, "g": 1000.0}[unit]
    return max(int(round(mbps)), 1) if mbps > 0 else default


def _package_index(org) -> dict[str, Package]:
    """Existing packages by lower-cased router profile name.

    A package is found by the profile it is mapped to (``mikrotik_profile``) and, failing that, by its own
    name — so "Silver" mapped to profile ``Home-10M`` is reused instead of a duplicate being created.
    """
    packages = list(Package.objects.filter(organization=org, status=Status.ACTIVE))
    index = {p.name.lower(): p for p in packages}
    index.update({p.profile_name.lower(): p for p in packages})  # an explicit mapping wins over a name match
    return index


def _read_router(org):
    if not org.router_configured:
        raise RouterImportError("Router credentials are not saved yet.")
    ok, profiles = Mikrotik.get_profiles(org)
    if not ok:
        raise RouterImportError(f"Could not read PPP profiles: {profiles}")
    ok, secrets = Mikrotik.get_users_from_server(org)
    if not ok:
        raise RouterImportError(f"Could not read PPP users: {secrets}")
    return profiles, secrets


def _is_disabled(secret: dict) -> bool:
    return str(secret.get("disabled", "false")).lower() == "true"


def _classify(org, secrets):
    existing = set(
        Customer.objects.filter(organization=org, status=Status.ACTIVE).values_list("username", flat=True)
    )
    new, already, other = [], 0, 0
    for secret in secrets:
        if str(secret.get("service", "")).lower() not in IMPORTABLE_SERVICES:
            other += 1
        elif secret.get("name") in existing:
            already += 1
        elif secret.get("name"):
            new.append(secret)
    return new, already, other


def build_preview(org) -> dict:
    profiles, secrets = _read_router(org)
    new, already, other = _classify(org, secrets)
    limit_left = max(org.customer_limit - Customer.objects.filter(organization=org, status=Status.ACTIVE).count(), 0)
    packages = set(_package_index(org))

    per_profile: dict[str, int] = {}
    disabled_per_profile: dict[str, int] = {}
    for secret in new:
        key = secret.get("profile") or "default"
        per_profile[key] = per_profile.get(key, 0) + 1
        if _is_disabled(secret):
            disabled_per_profile[key] = disabled_per_profile.get(key, 0) + 1
    known = {p.get("name"): p for p in profiles}
    rows = []
    for name in sorted(set(per_profile) | {p for p in known if p not in BUILTIN_PROFILES}):
        rate = (known.get(name) or {}).get("rate-limit", "")
        rows.append(
            {
                "name": name,
                "rate_limit": rate,
                "speed_mbps": parse_rate_mbps(rate),
                "new_customers": per_profile.get(name, 0),
                "disabled_customers": disabled_per_profile.get(name, 0),
                "package_exists": name.lower() in packages,
                "builtin": name in BUILTIN_PROFILES,
            }
        )
    return {
        "total_secrets": len(secrets),
        "new_customers": len(new),
        "already_imported": already,
        "other_services": other,
        "disabled_in_new": sum(1 for s in new if _is_disabled(s)),
        "capacity_left": limit_left,
        "exceeds_limit": len(new) > limit_left,
        "profiles": rows,
        "sample": [
            {"username": s["name"], "profile": s.get("profile") or "default", "comment": s.get("comment", ""),
             "disabled": _is_disabled(s)}
            for s in new[:5]
        ],
    }


def _price(raw) -> Decimal:
    try:
        value = Decimal(str(raw))
    except (InvalidOperation, TypeError):
        return Decimal("0")
    return value if value >= 0 else Decimal("0")


def run_import(
    org, user, prices: dict | None = None, include_disabled: bool = True, only_profiles: list[str] | None = None
) -> dict:
    """``only_profiles``: import just the users on these router profiles (None = every profile).

    One router often serves several resellers, each with their own profiles, so an ISP must be able to
    pull in only its own.
    """
    prices = prices or {}
    profiles, secrets = _read_router(org)
    new, already, other = _classify(org, secrets)
    if only_profiles is not None:
        wanted = set(only_profiles)
        new = [s for s in new if (s.get("profile") or "default") in wanted]
    if not include_disabled:
        new = [s for s in new if not _is_disabled(s)]

    known = {p.get("name"): p for p in profiles}
    packages = _package_index(org)
    used_phones = set(
        Customer.objects.filter(organization=org, status=Status.ACTIVE).exclude(phone="").values_list("phone", flat=True)
    )

    capacity = max(org.customer_limit - Customer.objects.filter(organization=org, status=Status.ACTIVE).count(), 0)
    to_create, skipped_limit, created_packages = [], 0, 0
    for secret in new:
        if len(to_create) >= capacity:
            skipped_limit += 1
            continue
        profile = secret.get("profile") or "default"
        package = packages.get(profile.lower())
        if package is None and profile not in BUILTIN_PROFILES:
            package = Package.objects.create(
                organization=org,
                name=profile,
                mikrotik_profile=profile,
                speed_mbps=parse_rate_mbps((known.get(profile) or {}).get("rate-limit")),
                price=_price(prices.get(profile, 0)),
                entry_by=user,
                updated_by=user,
            )
            packages[profile.lower()] = package
            created_packages += 1
        comment = (secret.get("comment") or "").strip()
        phone = next((normalize_bd_phone(m) for m in _PHONE.findall(comment)), None)
        if phone in used_phones:
            phone = None
        if phone:
            used_phones.add(phone)
        to_create.append(
            Customer(
                organization=org,
                name=(comment[:80] or secret["name"]),
                description=comment or None,
                username=secret["name"],
                password=secret.get("password", ""),
                phone=phone or "",
                package=package,
                connection_type=ConnectionType.PPPoE,
                is_active=not _is_disabled(secret),
                mikrotik_synced=True,
                entry_by=user,
                updated_by=user,
            )
        )
    # bulk_create bypasses the pre_save router toggle on purpose: the router already has these users.
    Customer.objects.bulk_create(to_create)
    org.total_customer = Customer.objects.filter(organization=org, status=Status.ACTIVE).count()
    org.save(update_fields=["total_customer"])
    return {
        "customers_created": len(to_create),
        "packages_created": created_packages,
        "already_imported": already,
        "other_services": other,
        "skipped_over_limit": skipped_limit,
        # the index holds a package under both its name and its profile: count each package once
        "packages_without_price": len({p.pk for p in packages.values() if p.price == 0 and p.mikrotik_profile}),
    }


def _linked_package(org, name, packages=None):
    """The package whose router profile is ``name`` (profile_name falls back to the package name)."""
    for package in packages if packages is not None else Package.objects.filter(organization=org, status=Status.ACTIVE):
        if package.profile_name == name:
            return package
    return None


def list_profiles(org) -> list[dict]:
    """The router's PPP profiles, each with the package already linked to it (if any)."""
    ok, profiles = Mikrotik.get_profiles(org) if org.router_configured else (False, "Router credentials are not saved yet.")
    if not ok:
        raise RouterImportError(f"Could not read PPP profiles: {profiles}")
    packages = list(Package.objects.filter(organization=org, status=Status.ACTIVE))
    rows = []
    for profile in profiles:
        name = profile.get("name", "")
        linked = _linked_package(org, name, packages)
        rows.append(
            {
                "name": name,
                "rate_limit": profile.get("rate-limit", ""),
                "speed_mbps": parse_rate_mbps(profile.get("rate-limit")),
                "builtin": name in BUILTIN_PROFILES,
                "package": {"uid": str(linked.uid), "name": linked.name} if linked else None,
            }
        )
    return rows


def import_profiles(org, user, items: list[dict]) -> dict:
    """Create a package for each chosen router profile that has none yet (no customers involved)."""
    available = {row["name"]: row for row in list_profiles(org)}
    created, skipped = [], []
    for item in items:
        name = (item or {}).get("name", "")
        row = available.get(name)
        if row is None or row["package"] is not None:
            skipped.append(name)
            continue
        Package.objects.create(
            organization=org,
            name=name,
            mikrotik_profile=name,
            speed_mbps=row["speed_mbps"],
            price=_price(item.get("price", 0)),
            entry_by=user,
            updated_by=user,
        )
        created.append(name)
    return {"packages_created": len(created), "created": created, "skipped": skipped}
