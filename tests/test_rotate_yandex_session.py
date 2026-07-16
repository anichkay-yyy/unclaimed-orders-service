"""Tests for Yandex account session rotation."""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from unclaimed_orders_service import rotate_yandex_session

if TYPE_CHECKING:
    from pytest import MonkeyPatch


def test_load_chrome_session_extracts_required_cookies(monkeypatch: MonkeyPatch) -> None:
    class FakeResponse:
        def raise_for_status(self) -> None:
            return None

        def json(self) -> dict[str, object]:
            return {
                "cookies": [
                    {
                        "name": "Session_id",
                        "value": "session-id",
                        "expirationDate": 1818488982,
                    },
                    {"name": "delivery_client_id", "value": "client-id"},
                ]
            }

    monkeypatch.setattr(rotate_yandex_session.httpx, "get", lambda *args, **kwargs: FakeResponse())

    session = rotate_yandex_session.load_chrome_session("http://chrome-tool.test")

    assert session == {
        "session_id": "session-id",
        "client_id": "client-id",
        "expires_at": "2027-08-17T07:49:42+00:00",
    }


def test_rotate_sops_session_keeps_previous_session(monkeypatch: MonkeyPatch) -> None:
    values = {"YANDEX_DELIVERY_SESSION_ID": "old-session"}
    writes: list[tuple[str, str]] = []

    def fake_decrypt(_path: Path) -> dict[str, str]:
        return values.copy()

    def fake_set(_path: Path, *, key: str, value: str) -> None:
        writes.append((key, value))
        values[key] = value

    monkeypatch.setattr(rotate_yandex_session, "_decrypt_sops_env", fake_decrypt)
    monkeypatch.setattr(rotate_yandex_session, "_set_sops_value", fake_set)

    rotate_yandex_session.rotate_sops_session(
        Path(".env.sops"),
        session_id="new-session",
        client_id="client-id",
        expires_at="2027-08-17T07:49:42+00:00",
    )

    assert writes == [
        ("YANDEX_DELIVERY_SESSION_ID_PREVIOUS", "old-session"),
        ("YANDEX_DELIVERY_SESSION_ID", "new-session"),
        ("YANDEX_DELIVERY_CLIENT_ID", "client-id"),
        ("YANDEX_DELIVERY_SESSION_EXPIRES_AT", "2027-08-17T07:49:42+00:00"),
    ]
