"""Tests for the embedded daily scheduler."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING
from zoneinfo import ZoneInfo

from fastapi.testclient import TestClient
from unclaimed_orders_service import app as app_module
from unclaimed_orders_service.domain import DecisionAction, NotificationChannel

if TYPE_CHECKING:
    from pytest import MonkeyPatch


def test_next_run_uses_same_day_when_before_configured_time() -> None:
    timezone = ZoneInfo("Europe/Moscow")
    config = app_module.CronConfig(
        enabled=True,
        hour=9,
        minute=0,
        timezone_name="Europe/Moscow",
        timezone=timezone,
    )

    next_run = app_module._next_run_at(datetime(2026, 7, 9, 5, 30, tzinfo=UTC), config=config)

    assert next_run == datetime(2026, 7, 9, 9, 0, tzinfo=timezone)


def test_next_run_moves_to_tomorrow_after_configured_time() -> None:
    timezone = ZoneInfo("Europe/Moscow")
    config = app_module.CronConfig(
        enabled=True,
        hour=9,
        minute=0,
        timezone_name="Europe/Moscow",
        timezone=timezone,
    )

    next_run = app_module._next_run_at(datetime(2026, 7, 9, 7, 30, tzinfo=UTC), config=config)

    assert next_run == datetime(2026, 7, 10, 9, 0, tzinfo=timezone)


def test_parse_cron_time_accepts_single_digit_hour() -> None:
    assert app_module._parse_cron_time("9:00") == (9, 0)


def test_load_cron_config_defaults_to_daily_9_moscow(monkeypatch: MonkeyPatch) -> None:
    monkeypatch.delenv("UNCLAIMED_ORDERS_CRON_ENABLED", raising=False)
    monkeypatch.delenv("UNCLAIMED_ORDERS_CRON_TIME", raising=False)
    monkeypatch.delenv("UNCLAIMED_ORDERS_CRON_TZ", raising=False)

    config = app_module._load_cron_config()

    assert config.enabled is True
    assert config.time_label == "09:00"
    assert config.timezone_name == "Europe/Moscow"


def test_load_cron_config_can_be_disabled(monkeypatch: MonkeyPatch) -> None:
    monkeypatch.setenv("UNCLAIMED_ORDERS_CRON_ENABLED", "0")

    config = app_module._load_cron_config()

    assert config.enabled is False


def test_widgets_catalog_exposes_unclaimed_orders_widget(monkeypatch: MonkeyPatch) -> None:
    monkeypatch.setenv("UNCLAIMED_ORDERS_CRON_ENABLED", "0")

    with TestClient(app_module.app) as client:
        response = client.get("/widgets/widgets.json")

    assert response.status_code == 200
    assert response.json() == {
        "widgets": [
            {
                "path": "/widgets/unclaimed-orders",
                "name": "5Post storage monitor",
                "description": "Daily 5Post extension and customer notification status.",
            }
        ]
    }


def test_widget_state_projects_last_summary(monkeypatch: MonkeyPatch) -> None:
    monkeypatch.setenv("UNCLAIMED_ORDERS_CRON_ENABLED", "0")
    _reset_cron_state()
    app_module._cron_state.next_run_at = datetime(2026, 7, 10, 6, 0, tzinfo=UTC)
    app_module._cron_state.last_run_started_at = datetime(2026, 7, 9, 6, 0, tzinfo=UTC)
    app_module._cron_state.last_run_finished_at = datetime(2026, 7, 9, 6, 1, tzinfo=UTC)
    app_module._cron_state.last_status = "succeeded"
    app_module._cron_state.last_summary = {
        "today": "2026-07-09",
        "mode": "fivepost_live",
        "checked": 1,
        "decisions": [
            {
                "order_id": "6145602-1",
                "action": DecisionAction.EXTENDED,
                "reason": "extended_before_notification",
                "new_deadline": "2026-07-14",
            },
            {
                "order_id": "6145602-1",
                "action": DecisionAction.NOTIFIED,
                "reason": "client_notified",
                "channel": NotificationChannel.BITRIX,
                "new_deadline": "2026-07-14",
            },
        ],
    }

    try:
        with TestClient(app_module.app) as client:
            response = client.get("/widgets/unclaimed-orders/state")
    finally:
        _reset_cron_state()

    payload = response.json()
    assert response.status_code == 200
    assert payload["cron"]["time"] == "09:00"
    assert payload["last_run"]["today"] == "2026-07-09"
    assert payload["totals"] == {"checked": 1, "orders": 1, "success": 1, "errors": 0}
    assert payload["rows"] == [
        {
            "order_id": "6145602-1",
            "carrier": "5post",
            "result": "success",
            "outcome": "notified",
            "channel_label": "Bitrix IM/OpenLine",
            "new_deadline": "2026-07-14",
            "reason": "extended_before_notification; client_notified",
        }
    ]


def _reset_cron_state() -> None:
    app_module._cron_state.next_run_at = None
    app_module._cron_state.running = False
    app_module._cron_state.last_run_started_at = None
    app_module._cron_state.last_run_finished_at = None
    app_module._cron_state.last_status = None
    app_module._cron_state.last_error = None
    app_module._cron_state.last_summary = None
