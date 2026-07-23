"""Databricks Apps per-user token helpers."""
from __future__ import annotations

import base64
import json
import logging
import os

from fastapi import Header, Request

logger = logging.getLogger("delta_editor.auth")


def jwt_scopes(token: str | None) -> list[str]:
    """Decode JWT payload (no verification) to inspect OAuth scopes."""
    if not token:
        return []
    try:
        parts = token.split(".")
        if len(parts) < 2:
            return []
        payload = parts[1]
        payload += "=" * (-len(payload) % 4)
        data = json.loads(base64.urlsafe_b64decode(payload))
        scope = data.get("scope") or data.get("scp") or ""
        if isinstance(scope, list):
            return [str(s) for s in scope]
        return [s for s in str(scope).split() if s]
    except Exception:
        return []


def resolve_user_token(
    request: Request,
    x_forwarded_access_token: str | None = Header(default=None),
) -> str | None:
    """Prefer x-forwarded-access-token from Databricks Apps; PAT for local dev only."""
    token = (
        x_forwarded_access_token
        or request.headers.get("x-forwarded-access-token")
        or request.headers.get("X-Forwarded-Access-Token")
    )
    if token:
        return token
    if os.environ.get("DATABRICKS_CLIENT_ID"):
        logger.warning(
            "No x-forwarded-access-token on %s — enable User authorization with "
            "'sql' scope, restart the app, and re-open it",
            request.url.path,
        )
        return None
    return os.environ.get("DATABRICKS_TOKEN")


def resolve_user_email(x_forwarded_email: str | None = Header(default=None)) -> str:
    return x_forwarded_email or os.environ.get("USER", "local_dev")


def sql_auth_hint(exc: Exception, token: str | None) -> str:
    """Turn SQL connector auth errors into actionable guidance."""
    msg = str(exc)
    upper = msg.upper()
    if "403" not in msg and "FORBIDDEN" not in upper and "401" not in msg and "UNAUTHORIZED" not in upper:
        return msg

    scopes = jwt_scopes(token)
    if not token:
        return (
            "No user access token was forwarded. In the App settings, enable User "
            "authorization, add the 'sql' scope, stop/restart the app, then re-open it."
        )
    if "sql" not in scopes:
        return (
            "Your login token does not include the 'sql' scope yet. Stop the app, "
            "restart it, log out of Databricks (and SSO if used), re-open the app, "
            "and accept the consent prompt. Current scopes: "
            + (", ".join(scopes) if scopes else "(none decoded)")
        )
    return (
        "Token has the 'sql' scope but SQL access was denied. Confirm you have "
        "'Can use' on the bound SQL warehouse and Unity Catalog grants (e.g. "
        "BROWSE/USE CATALOG). Original error: "
        + msg
    )
