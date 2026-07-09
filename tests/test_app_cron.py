"""Tests for the embedded daily scheduler."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING
from zoneinfo import ZoneInfo

from unclaimed_orders_service import app as app_module

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
