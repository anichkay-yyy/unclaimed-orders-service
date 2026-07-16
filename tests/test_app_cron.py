"""Tests for the embedded daily scheduler."""

from __future__ import annotations

from datetime import UTC, date, datetime
from typing import TYPE_CHECKING
from zoneinfo import ZoneInfo

from fastapi.testclient import TestClient
from unclaimed_orders_service import app as app_module
from unclaimed_orders_service.domain import (
    DecisionAction,
    NotificationChannel,
    RunDecision,
    RunSummary,
)

if TYPE_CHECKING:
    from pathlib import Path

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
                    "name": "Pickup storage monitor",
                    "description": (
                        "Daily pickup storage extension and customer notification status."
                    ),
                    "visibility": "org",
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
            "contact_id": "",
            "contact_url": "",
            "contact_label": "-",
            "message_id": "",
            "run_date": "2026-07-09",
            "processed_at": "2026-07-09T06:01:00+00:00",
            "channel_label": "Bitrix IM/OpenLine",
            "new_deadline": "2026-07-14",
            "reason": "extended_before_notification; client_notified",
        }
    ]


def test_widget_state_marks_bitrix_contact_missing_as_error(monkeypatch: MonkeyPatch) -> None:
    monkeypatch.setenv("UNCLAIMED_ORDERS_CRON_ENABLED", "0")
    _reset_cron_state()
    app_module._cron_state.last_status = "succeeded"
    app_module._cron_state.last_summary = {
        "today": "2026-07-09",
        "mode": "fivepost_live",
        "checked": 1,
        "decisions": [
            {
                "order_id": "418858-mgdb2",
                "action": DecisionAction.EXTENDED,
                "reason": "extended_before_notification",
                "new_deadline": "2026-07-14",
            },
            {
                "order_id": "418858-mgdb2",
                "action": DecisionAction.OPERATOR_TASK,
                "reason": "bitrix_contact_not_found",
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
    assert payload["totals"] == {"checked": 1, "orders": 1, "success": 0, "errors": 1}
    assert payload["rows"] == [
        {
            "order_id": "418858-mgdb2",
            "carrier": "5post",
            "result": "error",
            "outcome": "operator_task",
            "contact_id": "",
            "contact_url": "",
            "contact_label": "-",
            "message_id": "",
            "run_date": "2026-07-09",
            "processed_at": "",
            "channel_label": "-",
            "new_deadline": "2026-07-14",
            "reason": "extended_before_notification; Контакт Bitrix не найден",
        }
    ]


def test_widget_state_projects_carrier_and_extension_error(monkeypatch: MonkeyPatch) -> None:
    monkeypatch.setenv("UNCLAIMED_ORDERS_CRON_ENABLED", "0")
    _reset_cron_state()
    app_module._cron_state.last_status = "succeeded"
    app_module._cron_state.last_summary = {
        "today": "2026-07-13",
        "mode": "fivepost+yandex_live",
        "checked": 1,
        "decisions": [
            {
                "order_id": "431501FPerp",
                "carrier": "yandex",
                "action": DecisionAction.OPERATOR_TASK,
                "reason": "yandex_extension_not_configured",
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
    assert payload["rows"][0]["carrier"] == "yandex"
    assert payload["rows"][0]["result"] == "error"
    assert payload["rows"][0]["reason"] == "Продление Яндекс Доставки через API не настроено"


def test_widget_state_projects_carrier_auth_error(monkeypatch: MonkeyPatch) -> None:
    monkeypatch.setenv("UNCLAIMED_ORDERS_CRON_ENABLED", "0")
    _reset_cron_state()
    app_module._cron_state.last_status = "succeeded"
    app_module._cron_state.last_summary = {
        "today": "2026-07-15",
        "mode": "fivepost+yandex_live",
        "checked": 0,
        "decisions": [
            {
                "order_id": "carrier:fivepost",
                "carrier": "fivepost",
                "action": DecisionAction.OPERATOR_TASK,
                "reason": "carrier_auth_failed:401",
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
    assert payload["rows"] == [
        {
            "order_id": "carrier:fivepost",
            "carrier": "fivepost",
            "result": "error",
            "outcome": "operator_task",
            "contact_id": "",
            "contact_url": "",
            "contact_label": "-",
            "message_id": "",
            "run_date": "2026-07-15",
            "processed_at": "",
            "channel_label": "-",
            "new_deadline": "-",
            "reason": "Ошибка авторизации в API перевозчика (401)",
        }
    ]


def test_widget_state_hides_routine_and_unavailable_skips(monkeypatch: MonkeyPatch) -> None:
    monkeypatch.setenv("UNCLAIMED_ORDERS_CRON_ENABLED", "0")
    _reset_cron_state()
    app_module._cron_state.last_status = "succeeded"
    app_module._cron_state.last_summary = {
        "today": "2026-07-09",
        "mode": "fivepost_live",
        "checked": 3,
        "decisions": [
            {
                "order_id": "outside-window",
                "action": DecisionAction.SKIPPED,
                "reason": "outside_window",
            },
            {
                "order_id": "not-allowed",
                "action": DecisionAction.SKIPPED,
                "reason": "extension_not_allowed_or_already_extended",
            },
            {
                "order_id": "notified",
                "action": DecisionAction.EXTENDED,
                "reason": "extended_before_notification",
                "new_deadline": "2026-07-14",
            },
            {
                "order_id": "notified",
                "action": DecisionAction.NOTIFIED,
                "reason": "client_notified",
                "channel": NotificationChannel.EMAIL,
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
    assert payload["totals"] == {"checked": 3, "orders": 1, "success": 1, "errors": 0}
    assert [row["order_id"] for row in payload["rows"]] == ["notified"]
    assert payload["rows"][0]["outcome"] == "notified"


def test_widget_state_projects_contact_link(monkeypatch: MonkeyPatch) -> None:
    monkeypatch.setenv("UNCLAIMED_ORDERS_CRON_ENABLED", "0")
    _reset_cron_state()
    app_module._cron_state.last_status = "succeeded"
    app_module._cron_state.last_summary = {
        "today": "2026-07-09",
        "mode": "fivepost_live",
        "checked": 1,
        "decisions": [
            {
                "order_id": "413163-fgykh",
                "action": DecisionAction.EXTENDED,
                "reason": "extended_before_notification",
                "new_deadline": "2026-07-15",
            },
            {
                "order_id": "413163-fgykh",
                "action": DecisionAction.NOTIFIED,
                "reason": "client_notified",
                "channel": NotificationChannel.EMAIL,
                "new_deadline": "2026-07-15",
                "message_id": "184231",
                "contact_id": "14243",
                "contact_url": "https://bitrix.photo-print.co/crm/contact/details/14243/",
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
    assert payload["rows"][0]["contact_id"] == "14243"
    assert payload["rows"][0]["contact_label"] == "Контакт 14243"
    assert payload["rows"][0]["contact_url"] == "https://bitrix.photo-print.co/crm/contact/details/14243/"
    assert payload["rows"][0]["message_id"] == "184231"


def test_widget_state_keeps_distinct_attempt_rows(monkeypatch: MonkeyPatch) -> None:
    monkeypatch.setenv("UNCLAIMED_ORDERS_CRON_ENABLED", "0")
    _reset_cron_state()
    app_module._cron_state.last_status = "succeeded"
    app_module._cron_state.last_summary = {
        "today": "2026-07-11",
        "mode": "fivepost_live",
        "checked": 1,
        "decisions": [
            {
                "row_key": "413163-fgykh:184128",
                "order_id": "413163-fgykh",
                "action": DecisionAction.EXTENDED,
                "reason": "extended_before_notification",
                "new_deadline": "2026-07-15",
            },
            {
                "row_key": "413163-fgykh:184128",
                "order_id": "413163-fgykh",
                "action": DecisionAction.OPERATOR_TASK,
                "reason": "notification_failed:ValueError:bitrix_http_500",
                "new_deadline": "2026-07-15",
                "message_id": "184128",
                "contact_id": "14243",
                "contact_url": "https://bitrix.photo-print.co/crm/contact/details/14243/",
            },
            {
                "row_key": "413163-fgykh:184231",
                "order_id": "413163-fgykh",
                "action": DecisionAction.EXTENDED,
                "reason": "extended_before_notification",
                "new_deadline": "2026-07-15",
            },
            {
                "row_key": "413163-fgykh:184231",
                "order_id": "413163-fgykh",
                "action": DecisionAction.NOTIFIED,
                "reason": "client_notified",
                "channel": NotificationChannel.EMAIL,
                "new_deadline": "2026-07-15",
                "message_id": "184231",
                "contact_id": "14243",
                "contact_url": "https://bitrix.photo-print.co/crm/contact/details/14243/",
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
    assert payload["totals"] == {"checked": 1, "orders": 2, "success": 1, "errors": 1}
    assert [row["order_id"] for row in payload["rows"]] == ["413163-fgykh", "413163-fgykh"]
    assert [row["message_id"] for row in payload["rows"]] == ["184128", "184231"]


def test_widget_state_projects_history_newest_first(monkeypatch: MonkeyPatch) -> None:
    monkeypatch.setenv("UNCLAIMED_ORDERS_CRON_ENABLED", "0")
    _reset_cron_state()
    app_module._cron_state.last_status = "succeeded"
    app_module._cron_state.last_run_started_at = datetime(2026, 7, 13, 6, 0, tzinfo=UTC)
    app_module._cron_state.last_run_finished_at = datetime(2026, 7, 13, 6, 1, tzinfo=UTC)
    app_module._cron_state.last_summary = {
        "today": "2026-07-13",
        "mode": "fivepost_live",
        "checked": 1,
        "decisions": [
            {
                "order_id": "new-order",
                "action": DecisionAction.NOTIFIED,
                "reason": "client_notified",
                "channel": NotificationChannel.EMAIL,
                "new_deadline": "2026-07-17",
            },
        ],
    }
    app_module._cron_state.widget_runs = [
        {
            "started_at": "2026-07-12T06:00:00+00:00",
            "finished_at": "2026-07-12T06:01:00+00:00",
            "status": "succeeded",
            "error": None,
            "summary": {
                "today": "2026-07-12",
                "mode": "fivepost_live",
                "checked": 1,
                "decisions": [
                    {
                        "order_id": "old-order",
                        "action": DecisionAction.NOTIFIED,
                        "reason": "client_notified",
                        "channel": NotificationChannel.EMAIL,
                        "new_deadline": "2026-07-16",
                    },
                ],
            },
        },
        {
            "started_at": "2026-07-13T06:00:00+00:00",
            "finished_at": "2026-07-13T06:01:00+00:00",
            "status": "succeeded",
            "error": None,
            "summary": app_module._cron_state.last_summary,
        },
    ]

    try:
        with TestClient(app_module.app) as client:
            response = client.get("/widgets/unclaimed-orders/state")
    finally:
        _reset_cron_state()

    payload = response.json()
    assert response.status_code == 200
    assert payload["totals"] == {"checked": 1, "orders": 2, "success": 2, "errors": 0}
    assert [row["order_id"] for row in payload["rows"]] == ["new-order", "old-order"]
    assert [row["run_date"] for row in payload["rows"]] == ["2026-07-13", "2026-07-12"]


async def test_manual_run_updates_widget_state(
    monkeypatch: MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("UNCLAIMED_ORDERS_CRON_ENABLED", "0")
    monkeypatch.setenv("UNCLAIMED_ORDERS_WIDGET_STATE_PATH", str(tmp_path / "widget-state.json"))
    _reset_cron_state()
    service = _FakeDailyService(
        RunSummary(
            checked=1,
            decisions=(
                RunDecision(
                    "418858-mgdb2",
                    DecisionAction.OPERATOR_TASK,
                    "bitrix_contact_not_found",
                ),
            ),
        )
    )
    monkeypatch.setattr(app_module, "_build_daily_service", lambda: (service, "test_mode"))

    try:
        payload = await app_module._run_daily_tracked(date(2026, 7, 9), raise_errors=True)
        last_status = app_module._cron_state.last_status
        last_summary = app_module._cron_state.last_summary
    finally:
        _reset_cron_state()

    assert payload["today"] == "2026-07-09"
    assert payload["mode"] == "test_mode"
    assert last_status == "succeeded"
    assert last_summary == payload


async def test_manual_run_persists_widget_state(
    monkeypatch: MonkeyPatch,
    tmp_path: Path,
) -> None:
    state_path = tmp_path / "widget-state.json"
    monkeypatch.setenv("UNCLAIMED_ORDERS_CRON_ENABLED", "0")
    monkeypatch.setenv("UNCLAIMED_ORDERS_WIDGET_STATE_PATH", str(state_path))
    _reset_cron_state()
    service = _FakeDailyService(
        RunSummary(
            checked=1,
            decisions=(
                RunDecision(
                    "413163-fgykh",
                    DecisionAction.NOTIFIED,
                    "client_notified",
                    channel=NotificationChannel.EMAIL,
                    new_deadline=date(2026, 7, 15),
                    message_id="184231",
                    contact_id="14243",
                    contact_url="https://bitrix.photo-print.co/crm/contact/details/14243/",
                ),
            ),
        )
    )
    monkeypatch.setattr(app_module, "_build_daily_service", lambda: (service, "test_mode"))

    try:
        await app_module._run_daily_tracked(date(2026, 7, 11), raise_errors=True)
        _reset_cron_state()
        app_module._load_persisted_widget_state()
        loaded_summary = app_module._cron_state.last_summary
        loaded_runs = app_module._cron_state.widget_runs
    finally:
        _reset_cron_state()

    assert loaded_summary is not None
    assert loaded_summary["today"] == "2026-07-11"
    assert loaded_summary["decisions"][0]["contact_id"] == "14243"
    assert loaded_summary["decisions"][0]["message_id"] == "184231"
    assert loaded_runs[0]["summary"]["today"] == "2026-07-11"


class _FakeDailyService:
    def __init__(self, summary: RunSummary) -> None:
        self.summary = summary

    async def run_daily(self, *, today: date) -> RunSummary:
        return self.summary


def _reset_cron_state() -> None:
    app_module._cron_state.next_run_at = None
    app_module._cron_state.running = False
    app_module._cron_state.last_run_started_at = None
    app_module._cron_state.last_run_finished_at = None
    app_module._cron_state.last_status = None
    app_module._cron_state.last_error = None
    app_module._cron_state.last_summary = None
    app_module._cron_state.widget_runs = []
