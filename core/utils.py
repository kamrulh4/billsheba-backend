"""Utils functions for core app."""

import secrets


# Media File Prefixes
def get_user_media_path_prefix(instance, filename):
    return f"users/{instance.slug}/{filename}"


def generate_unique_otp(length=6) -> str:
    otp = "".join(secrets.choice("0123456789") for _ in range(length))

    return otp
