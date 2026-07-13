"""FastAPI entrypoint for the standalone unclaimed orders service."""

from __future__ import annotations

import asyncio
import json
import logging
import os
from contextlib import asynccontextmanager, suppress
from dataclasses import asdict, dataclass, field
from datetime import UTC, date, datetime, timedelta, timezone, tzinfo
from enum import Enum
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from fastapi import FastAPI
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, Field

from unclaimed_orders_service.adapters import (
    BitrixContactNotifier,
    CompositeCarrierClient,
    DemoCarrierClient,
    DryRunNotifier,
    DryRunOperatorTasks,
    ErpEmailCarrierClient,
)
from unclaimed_orders_service.domain import UnclaimedOrdersService
from unclaimed_orders_service.erp import ErpSourceLookup
from unclaimed_orders_service.list_due_emails import (
    _build_bitrix_contact_client,
    _build_fivepost_client,
    _build_yandex_client,
)
from unclaimed_orders_service.widgets import (
    build_widget_state,
    render_widget_html,
    widget_catalog,
)

_log = logging.getLogger(__name__)
_CRON_ENABLED_ENV = "UNCLAIMED_ORDERS_CRON_ENABLED"
_CRON_TIME_ENV = "UNCLAIMED_ORDERS_CRON_TIME"
_CRON_TZ_ENV = "UNCLAIMED_ORDERS_CRON_TZ"
_WIDGET_STATE_PATH_ENV = "UNCLAIMED_ORDERS_WIDGET_STATE_PATH"
_DEFAULT_CRON_TIME = "09:00"
_DEFAULT_CRON_TZ = "Europe/Moscow"
_DEFAULT_WIDGET_STATE_PATH = "/data/unclaimed_orders_widget_state.json"
_TRUE_VALUES = {"1", "true", "yes", "on"}
_FALSE_VALUES = {"0", "false", "no", "off"}


@dataclass(frozen=True, slots=True)
class CronConfig:
    """Daily cron configuration."""

    enabled: bool
    hour: int
    minute: int
    timezone_name: str
    timezone: tzinfo

    @property
    def time_label(self) -> str:
        """Return the configured local fire time."""
        return f"{self.hour:02d}:{self.minute:02d}"


@dataclass(slots=True)
class CronState:
    """In-memory status for the embedded daily scheduler."""

    next_run_at: datetime | None = None
    running: bool = False
    last_run_started_at: datetime | None = None
    last_run_finished_at: datetime | None = None
    last_status: str | None = None
    last_error: str | None = None
    last_summary: dict[str, Any] | None = None
    widget_runs: list[dict[str, Any]] = field(default_factory=list)


_cron_state = CronState()
_cron_task: asyncio.Task[None] | None = None
_daily_run_lock = asyncio.Lock()


@asynccontextmanager
async def lifespan(_app: FastAPI) -> Any:
    """Start and stop the embedded daily scheduler."""
    global _cron_task

    _load_persisted_widget_state()
    config = _load_cron_config()
    if config.enabled:
        _cron_task = asyncio.create_task(_daily_cron_loop(config), name="unclaimed-orders-cron")
        _log.info(
            "unclaimed_orders_cron_started",
            extra={"time": config.time_label, "timezone": config.timezone_name},
        )
    try:
        yield
    finally:
        if _cron_task is not None:
            _cron_task.cancel()
            with suppress(asyncio.CancelledError):
                await _cron_task
            _cron_task = None


app = FastAPI(title="Unclaimed Orders Service", version="0.1.0", lifespan=lifespan)


class DailyRunRequest(BaseModel):
    """Request body for a daily run."""

    today: date | None = Field(default=None, description="Override current date for tests.")


class WaitingOrdersRequest(BaseModel):
    """Request body for fetching waiting pickup orders."""

    today: date | None = Field(default=None, description="Override current date for tests.")


@app.get("/health")
async def health() -> dict[str, Any]:
    """Liveness check."""
    return {"ok": True, "time": datetime.now(UTC).isoformat()}


@app.get("/widgets/widgets.json")
async def widgets_catalog() -> dict[str, Any]:
    """Expose service widgets for Mary discovery."""
    return widget_catalog()


@app.get("/widgets/unclaimed-orders", response_class=HTMLResponse)
async def unclaimed_orders_widget() -> HTMLResponse:
    """Render the embeddable unclaimed orders monitor."""
    return HTMLResponse(render_widget_html())


@app.get("/widgets/unclaimed-orders/state")
async def unclaimed_orders_widget_state() -> dict[str, Any]:
    """Return normalized service state for the widget UI."""
    config = _load_cron_config()
    return build_widget_state(
        enabled=config.enabled,
        time_label=config.time_label,
        timezone_name=config.timezone_name,
        next_run_at=_isoformat_or_none(_cron_state.next_run_at),
        running=_cron_state.running,
        last_run_started_at=_isoformat_or_none(_cron_state.last_run_started_at),
        last_run_finished_at=_isoformat_or_none(_cron_state.last_run_finished_at),
        last_status=_cron_state.last_status,
        last_error=_cron_state.last_error,
        last_summary=_cron_state.last_summary,
        run_history=_cron_state.widget_runs,
    )


@app.post("/runs/daily")
async def run_daily(request: DailyRunRequest | None = None) -> dict[str, Any]:
    """Run the daily unclaimed-order check."""
    run_date = request.today if request and request.today else datetime.now(UTC).date()
    return await _run_daily_tracked(run_date, raise_errors=True)


@app.get("/runs/cron")
async def cron_status() -> dict[str, Any]:
    """Return embedded daily scheduler status."""
    config = _load_cron_config()
    return {
        "enabled": config.enabled,
        "time": config.time_label,
        "timezone": config.timezone_name,
        "next_run_at": _isoformat_or_none(_cron_state.next_run_at),
        "running": _cron_state.running,
        "last_run_started_at": _isoformat_or_none(_cron_state.last_run_started_at),
        "last_run_finished_at": _isoformat_or_none(_cron_state.last_run_finished_at),
        "last_status": _cron_state.last_status,
        "last_error": _cron_state.last_error,
        "last_summary": _cron_state.last_summary,
    }


async def _run_daily_once(run_date: date) -> dict[str, Any]:
    if _daily_run_lock.locked():
        return {"status": "already_running", "today": run_date.isoformat()}

    async with _daily_run_lock:
        return await _run_daily_for_date(run_date)


async def _run_daily_tracked(run_date: date, *, raise_errors: bool) -> dict[str, Any]:
    if _daily_run_lock.locked():
        return {"status": "already_running", "today": run_date.isoformat()}

    async with _daily_run_lock:
        _cron_state.running = True
        _cron_state.last_run_started_at = datetime.now(UTC)
        _cron_state.last_run_finished_at = None
        _cron_state.last_status = "running"
        _cron_state.last_error = None
        try:
            _cron_state.last_summary = await _run_daily_for_date(run_date)
        except Exception as exc:
            _cron_state.last_status = "failed"
            _cron_state.last_error = str(exc)
            _cron_state.last_summary = {
                "status": "failed",
                "today": run_date.isoformat(),
                "error": str(exc),
            }
            if raise_errors:
                raise
            return _cron_state.last_summary
        else:
            _cron_state.last_status = "succeeded"
            return _cron_state.last_summary
        finally:
            _cron_state.running = False
            _cron_state.last_run_finished_at = datetime.now(UTC)
            _record_widget_run()
            _persist_widget_state()


async def _run_daily_for_date(run_date: date) -> dict[str, Any]:
    service, mode = _build_daily_service()
    summary = await service.run_daily(today=run_date)
    payload = asdict(summary)
    payload["today"] = run_date.isoformat()
    payload["mode"] = mode
    return payload


@app.get("/orders/waiting")
async def list_waiting_orders(today: date | None = None) -> dict[str, Any]:
    """Fetch waiting pickup orders only, without side effects."""
    run_date = today or datetime.now(UTC).date()
    carrier, mode = _build_configured_carrier()
    if carrier is None:
        carrier = DemoCarrierClient()
        mode = "demo"
    orders = await carrier.list_waiting_pickup_orders(today=run_date)
    return {
        "today": run_date.isoformat(),
        "mode": mode,
        "orders": [asdict(order) for order in orders],
    }


def _build_daily_service() -> tuple[UnclaimedOrdersService, str]:
    """Build the live carrier service when env is configured, otherwise demo."""
    carrier, carrier_mode = _build_configured_carrier()
    bitrix = _build_bitrix_contact_client()
    if carrier is not None and bitrix is not None:
        return (
            UnclaimedOrdersService(
                carrier=ErpEmailCarrierClient(
                    carrier=carrier,
                    erp=ErpSourceLookup(),
                ),
                notifier=BitrixContactNotifier(bitrix),
                operator_tasks=DryRunOperatorTasks(),
            ),
            f"{carrier_mode}_live",
        )
    return (
        UnclaimedOrdersService(
            carrier=DemoCarrierClient(),
            notifier=DryRunNotifier(),
            operator_tasks=DryRunOperatorTasks(),
        ),
        "demo_dry_run",
    )


def _build_configured_carrier() -> tuple[Any | None, str]:
    carriers: list[Any] = []
    names: list[str] = []
    if _fivepost_configured():
        carriers.append(_build_fivepost_client())
        names.append("fivepost")
    if _yandex_configured():
        carriers.append(_build_yandex_client())
        names.append("yandex")
    if not carriers:
        return None, ""
    if len(carriers) == 1:
        return carriers[0], names[0]
    return CompositeCarrierClient(tuple(carriers)), "+".join(names)


def _fivepost_configured() -> bool:
    return bool(os.environ.get("FIVEPOST_LOGIN") and os.environ.get("FIVEPOST_PASSWORD"))


def _yandex_configured() -> bool:
    return bool(
        os.environ.get("YANDEX_DELIVERY_OAUTH_TOKEN") or os.environ.get("YANDEX_DELIVERY_TOKEN")
    )


async def _daily_cron_loop(config: CronConfig) -> None:
    while True:
        next_run_at = _next_run_at(datetime.now(UTC), config=config)
        _cron_state.next_run_at = next_run_at
        await asyncio.sleep(_seconds_until(next_run_at))
        await _run_scheduled_daily(config)


async def _run_scheduled_daily(config: CronConfig) -> None:
    run_date = datetime.now(config.timezone).date()
    summary = await _run_daily_tracked(run_date, raise_errors=False)
    if summary.get("status") == "already_running":
        _cron_state.last_status = "skipped_already_running"
        return
    if summary.get("status") == "failed":
        _log.error("unclaimed_orders_cron_run_failed", extra={"error": summary.get("error")})
        return
    _cron_state.last_status = "succeeded"
    _log.info("unclaimed_orders_cron_run_succeeded")


def _load_cron_config() -> CronConfig:
    enabled = _parse_bool(os.environ.get(_CRON_ENABLED_ENV), default=True)
    hour, minute = _parse_cron_time(os.environ.get(_CRON_TIME_ENV, _DEFAULT_CRON_TIME))
    timezone_name = os.environ.get(_CRON_TZ_ENV, _DEFAULT_CRON_TZ)
    return CronConfig(
        enabled=enabled,
        hour=hour,
        minute=minute,
        timezone_name=timezone_name,
        timezone=_load_timezone(timezone_name),
    )


def _parse_bool(raw: str | None, *, default: bool) -> bool:
    if raw is None:
        return default
    normalized = raw.strip().lower()
    if normalized in _TRUE_VALUES:
        return True
    if normalized in _FALSE_VALUES:
        return False
    msg = f"invalid boolean value for {_CRON_ENABLED_ENV}: {raw!r}"
    raise ValueError(msg)


def _parse_cron_time(raw: str) -> tuple[int, int]:
    parts = raw.strip().split(":")
    if len(parts) != 2:
        msg = f"invalid daily cron time {raw!r}; expected HH:MM"
        raise ValueError(msg)
    hour = int(parts[0])
    minute = int(parts[1])
    if not 0 <= hour <= 23 or not 0 <= minute <= 59:
        msg = f"invalid daily cron time {raw!r}; expected HH:MM"
        raise ValueError(msg)
    return hour, minute


def _load_timezone(name: str) -> tzinfo:
    try:
        return ZoneInfo(name)
    except ZoneInfoNotFoundError:
        if name in {"Europe/Moscow", "Europe/Minsk"}:
            return timezone(timedelta(hours=3), name)
        raise


def _next_run_at(now: datetime, *, config: CronConfig) -> datetime:
    local_now = now.astimezone(config.timezone)
    candidate = local_now.replace(
        hour=config.hour,
        minute=config.minute,
        second=0,
        microsecond=0,
    )
    if candidate <= local_now:
        candidate += timedelta(days=1)
    return candidate


def _seconds_until(target: datetime) -> float:
    return max((target.astimezone(UTC) - datetime.now(UTC)).total_seconds(), 0.0)


def _isoformat_or_none(value: datetime | None) -> str | None:
    if value is None:
        return None
    return value.isoformat()


def _widget_state_path() -> Path:
    return Path(os.environ.get(_WIDGET_STATE_PATH_ENV, _DEFAULT_WIDGET_STATE_PATH))


def _persist_widget_state() -> None:
    payload = {
        "last_run_started_at": _isoformat_or_none(_cron_state.last_run_started_at),
        "last_run_finished_at": _isoformat_or_none(_cron_state.last_run_finished_at),
        "last_status": _cron_state.last_status,
        "last_error": _cron_state.last_error,
        "last_summary": _jsonable(_cron_state.last_summary),
        "widget_runs": _jsonable(_cron_state.widget_runs),
    }
    path = _widget_state_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = path.with_suffix(f"{path.suffix}.tmp")
        tmp_path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        tmp_path.replace(path)
    except OSError as exc:
        _log.warning("unclaimed_orders_widget_state_persist_failed", extra={"error": str(exc)})


def _load_persisted_widget_state() -> None:
    path = _widget_state_path()
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return
    except (OSError, json.JSONDecodeError) as exc:
        _log.warning("unclaimed_orders_widget_state_load_failed", extra={"error": str(exc)})
        return
    if not isinstance(payload, dict):
        return
    _cron_state.last_run_started_at = _parse_datetime(payload.get("last_run_started_at"))
    _cron_state.last_run_finished_at = _parse_datetime(payload.get("last_run_finished_at"))
    _cron_state.last_status = _optional_str(payload.get("last_status"))
    _cron_state.last_error = _optional_str(payload.get("last_error"))
    last_summary = payload.get("last_summary")
    _cron_state.last_summary = last_summary if isinstance(last_summary, dict) else None
    runs = payload.get("widget_runs")
    if isinstance(runs, list):
        _cron_state.widget_runs = [run for run in runs if isinstance(run, dict)]
    elif _cron_state.last_summary is not None:
        _cron_state.widget_runs = [_widget_run_snapshot()]


def _record_widget_run() -> None:
    if not isinstance(_cron_state.last_summary, dict):
        return
    snapshot = _widget_run_snapshot()
    run_key = _widget_run_key(snapshot)
    runs = [run for run in _cron_state.widget_runs if _widget_run_key(run) != run_key]
    runs.append(snapshot)
    runs.sort(key=_widget_run_sort_key, reverse=True)
    _cron_state.widget_runs = runs


def _widget_run_snapshot() -> dict[str, Any]:
    return {
        "started_at": _isoformat_or_none(_cron_state.last_run_started_at),
        "finished_at": _isoformat_or_none(_cron_state.last_run_finished_at),
        "status": _cron_state.last_status,
        "error": _cron_state.last_error,
        "summary": _jsonable(_cron_state.last_summary),
    }


def _widget_run_key(run: dict[str, Any]) -> tuple[str, str, str]:
    summary = run.get("summary") if isinstance(run.get("summary"), dict) else {}
    return (
        _optional_str(summary.get("today")) or "",
        _optional_str(run.get("started_at")) or "",
        _optional_str(run.get("finished_at")) or "",
    )


def _widget_run_sort_key(run: dict[str, Any]) -> str:
    summary = run.get("summary") if isinstance(run.get("summary"), dict) else {}
    return (
        _optional_str(run.get("finished_at"))
        or _optional_str(run.get("started_at"))
        or _optional_str(summary.get("today"))
        or ""
    )


def _parse_datetime(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


def _optional_str(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _jsonable(value: Any) -> Any:
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, dict):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(item) for item in value]
    return value
