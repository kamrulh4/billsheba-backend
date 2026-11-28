"""Carry a customer edit (username, password, package) over to the ISP's MikroTik."""

from customer.helpers import Mikrotik


class RouterSyncError(Exception):
    pass


def push_customer_changes(customer, data) -> dict | None:
    """
    Apply the router-relevant part of ``data`` (the validated edit) to ``customer``'s PPP secret.

    Call this *before* saving: when the router refuses or cannot be reached it raises, so the app and
    the router never end up disagreeing. Returns ``None`` when the router has nothing to learn from
    this edit, otherwise ``{"status": "updated" | "not_found", "changed": [...], "reconnect": ...}``.
    """
    org = customer.organization
    old_username = customer.username
    if not (org and org.router_configured and old_username):
        return None

    changes = {}
    username = data.get("username")
    if username and username != old_username:
        changes["name"] = username
    password = data.get("password")
    if password and password != customer.password:
        changes["password"] = password
    package = data.get("package")
    if package and (not customer.package or package.profile_name != customer.package.profile_name):
        changes["profile"] = package.profile_name
    if not changes:
        return None

    changed = ["username" if key == "name" else key for key in changes]
    found, error = Mikrotik.update_ppp_user(old_username, changes, org)
    if error:
        if "profile" in changes and "profile" in error.lower():
            error = (
                f"The router has no PPP profile '{changes['profile']}' (package '{package.name}'). "
                "Set the package's router profile, or create the profile on the router."
            )
        raise RouterSyncError(error)
    if not found:
        return {"status": "not_found", "changed": changed, "reconnect": None}

    # A new speed only applies to a new connection, so drop the current one. Not when the login also
    # changed: the customer's own router still has the old one and could not get back in.
    reconnect = None
    active = data.get("is_active", customer.is_active)
    if "profile" in changes and "name" not in changes and "password" not in changes and active:
        reconnect = Mikrotik.disconnect_ppp_user(old_username, org)
    return {"status": "updated", "changed": changed, "reconnect": reconnect}
