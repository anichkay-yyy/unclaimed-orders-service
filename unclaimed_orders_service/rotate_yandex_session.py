"""Rotate the Yandex account session from an authenticated local Chrome."""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx

_DEFAULT_CHROME_TOOL_URL = "http://127.0.0.1:8787"
_DEFAULT_YANDEX_URL = "https://dostavka.yandex.ru"
_WAITING_STATUS = "accepted_on_destination_point"


def main() -> None:
    """Validate the active Chrome session and write it to the SOPS env."""
    parser = argparse.ArgumentParser(
        description="Rotate Yandex Delivery Session_id from the local Chrome Tool session."
    )
    parser.add_argument("--env-file", type=Path, default=Path(".env.sops"))
    parser.add_argument("--chrome-tool-url", default=_DEFAULT_CHROME_TOOL_URL)
    parser.add_argument("--yandex-url", default=_DEFAULT_YANDEX_URL)
    parser.add_argument("--check-only", action="store_true")
    args = parser.parse_args()

    session = load_chrome_session(args.chrome_tool_url)
    first_page_orders = verify_yandex_session(
        base_url=args.yandex_url,
        session_id=session["session_id"],
        client_id=session["client_id"],
    )
    print(
        "Yandex session verified: "
        f"{first_page_orders} waiting order(s) on the first page; "
        f"expires at {session['expires_at']}"
    )
    if args.check_only:
        return

    rotate_sops_session(
        args.env_file,
        session_id=session["session_id"],
        client_id=session["client_id"],
        expires_at=session["expires_at"],
    )
    print(f"Encrypted environment updated: {args.env_file}. Redeploy the service to apply it.")


def load_chrome_session(chrome_tool_url: str) -> dict[str, str]:
    """Read the minimum required Yandex cookies from Chrome Tool."""
    response = httpx.get(f"{chrome_tool_url.rstrip('/')}/cookies", timeout=10.0)
    response.raise_for_status()
    payload = response.json()
    rows = payload.get("cookies") if isinstance(payload, dict) else None
    if not isinstance(rows, list):
        msg = "Chrome Tool did not return a cookie list"
        raise RuntimeError(msg)

    session_cookie = _find_cookie(rows, "Session_id")
    client_cookie = _find_cookie(rows, "delivery_client_id")
    if session_cookie is None or client_cookie is None:
        msg = "Open and sign in to https://dostavka.yandex.ru/account/ in Chrome first"
        raise RuntimeError(msg)
    expiration = session_cookie.get("expirationDate")
    if not isinstance(expiration, int | float):
        msg = "Yandex Session_id has no expiration date"
        raise RuntimeError(msg)
    return {
        "session_id": str(session_cookie["value"]),
        "client_id": str(client_cookie["value"]),
        "expires_at": datetime.fromtimestamp(expiration, tz=UTC).isoformat(),
    }


def verify_yandex_session(*, base_url: str, session_id: str, client_id: str) -> int:
    """Make a read-only internal API request with the candidate session."""
    with httpx.Client(
        base_url=base_url.rstrip("/"),
        cookies={"Session_id": session_id},
        timeout=20.0,
    ) as client:
        csrf_response = client.get("/account/api/csrf_token/")
        csrf_response.raise_for_status()
        csrf_payload = csrf_response.json()
        csrf_token = csrf_payload.get("sk") if isinstance(csrf_payload, dict) else None
        if not isinstance(csrf_token, str) or not csrf_token:
            msg = "Yandex did not return a CSRF token"
            raise RuntimeError(msg)
        response = client.post(
            "/api/b2b/dcaa/delivery/v1/udp/customer-order/list",
            json={
                "filters": {"product_statuses": [_WAITING_STATUS]},
                "client_timezone": "Europe/Moscow",
            },
            headers={
                "Accept": "application/json",
                "Accept-Language": "ru-RU",
                "Content-Type": "application/json",
                "X-B2B-Client-Id": client_id,
                "X-CSRF-Token": csrf_token,
            },
        )
        response.raise_for_status()
        payload = response.json()
    rows = payload.get("customer_orders") if isinstance(payload, dict) else None
    if not isinstance(rows, list):
        msg = "Yandex session check returned an unexpected response"
        raise RuntimeError(msg)
    return len(rows)


def rotate_sops_session(
    env_file: Path,
    *,
    session_id: str,
    client_id: str,
    expires_at: str,
) -> None:
    """Write the verified session and keep the old value as fallback."""
    env_file = env_file.resolve()
    values = _decrypt_sops_env(env_file)
    current_session = values.get("YANDEX_DELIVERY_SESSION_ID")
    updates: list[tuple[str, str]] = []
    if current_session and current_session != session_id:
        updates.append(("YANDEX_DELIVERY_SESSION_ID_PREVIOUS", current_session))
    updates.extend(
        (
            ("YANDEX_DELIVERY_SESSION_ID", session_id),
            ("YANDEX_DELIVERY_CLIENT_ID", client_id),
            ("YANDEX_DELIVERY_SESSION_EXPIRES_AT", expires_at),
        )
    )
    for key, value in updates:
        _set_sops_value(env_file, key=key, value=value)

    written = _decrypt_sops_env(env_file)
    expected = dict(updates)
    if any(written.get(key) != value for key, value in expected.items()):
        msg = "SOPS session rotation verification failed"
        raise RuntimeError(msg)


def _decrypt_sops_env(env_file: Path) -> dict[str, str]:
    result = subprocess.run(  # noqa: S603
        [
            _sops_binary(),
            "-d",
            "--input-type",
            "dotenv",
            "--output-type",
            "json",
            str(env_file),
        ],
        check=True,
        capture_output=True,
        text=True,
        env=os.environ.copy(),
    )
    payload = json.loads(result.stdout)
    if not isinstance(payload, dict):
        msg = "SOPS environment is not an object"
        raise RuntimeError(msg)
    return {str(key): str(value) for key, value in payload.items()}


def _set_sops_value(env_file: Path, *, key: str, value: str) -> None:
    subprocess.run(  # noqa: S603
        [
            _sops_binary(),
            "set",
            "--input-type",
            "dotenv",
            "--output-type",
            "dotenv",
            "--value-stdin",
            str(env_file),
            f'["{key}"]',
        ],
        input=json.dumps(value),
        check=True,
        capture_output=True,
        text=True,
        env=os.environ.copy(),
    )


def _find_cookie(rows: list[Any], name: str) -> dict[str, Any] | None:
    for row in rows:
        if (
            isinstance(row, dict)
            and row.get("name") == name
            and isinstance(row.get("value"), str)
            and row["value"]
        ):
            return row
    return None


def _sops_binary() -> str:
    binary = shutil.which("sops")
    if binary is None:
        msg = "sops executable was not found"
        raise RuntimeError(msg)
    return binary


if __name__ == "__main__":
    main()
