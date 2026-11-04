"""On-demand SSM credential bootstrap; never puts secrets into CloudFormation."""
from __future__ import annotations

import os
import threading

_bootstrap_lock = threading.Lock()
_bootstrapped = False


def ensure_credentials() -> None:
    """Retrieve two SecureString parameters once per warm Lambda environment.

    Only local development can use COOKIDOO_EMAIL/PASSWORD directly, and it
    requires COOKIDOO_ALLOW_LOCAL_ENV=1. AWS deployment must use SSM.
    """
    global _bootstrapped
    if _bootstrapped:
        return
    with _bootstrap_lock:
        if _bootstrapped:
            return

        email_name = os.environ.get("COOKIDOO_EMAIL_PARAM", "")
        password_name = os.environ.get("COOKIDOO_PASSWORD_PARAM", "")
        if email_name and password_name:
            try:
                import boto3

                response = boto3.client("ssm").get_parameters(
                    Names=[email_name, password_name], WithDecryption=True
                )
                found = {p["Name"]: p["Value"] for p in response["Parameters"]}
                if (
                    set(found) != {email_name, password_name}
                    or not found[email_name]
                    or not found[password_name]
                ):
                    raise ValueError("Missing or empty secure parameters")
            except Exception:
                # Avoid accidentally returning SSM path contents or credentials.
                raise RuntimeError("Cookidoo credential bootstrap failed") from None

            os.environ["COOKIDOO_EMAIL"] = found[email_name]
            os.environ["COOKIDOO_PASSWORD"] = found[password_name]
        elif not (
            os.environ.get("COOKIDOO_ALLOW_LOCAL_ENV") == "1"
            and os.environ.get("COOKIDOO_EMAIL")
            and os.environ.get("COOKIDOO_PASSWORD")
        ):
            raise RuntimeError("Cookidoo SSM parameters must be configured")

        _bootstrapped = True
