"""Service widget catalog and rendering helpers."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import date, datetime
from typing import Any

from unclaimed_orders_service.domain import DecisionAction

WIDGET_PATH = "/widgets/unclaimed-orders"
WIDGET_STATE_PATH = f"{WIDGET_PATH}/state"
_REASON_LABELS = {
    "bitrix_contact_not_found": "Контакт Bitrix не найден",
    "missing_customer_email": "E-mail клиента не найден",
    "extension_not_allowed_or_already_extended": "Продление недоступно или уже выполнено",
    "extension_deadline_not_confirmed": "Новая дата продления не подтверждена",
    "yandex_extension_not_configured": "Продление Яндекс Доставки через API не настроено",
}


def widget_catalog() -> dict[str, Any]:
    """Return the service widgets discovery catalog."""
    return {
        "widgets": [
            {
                "path": WIDGET_PATH,
                "name": "Pickup storage monitor",
                "description": "Daily pickup storage extension and customer notification status.",
                "visibility": "org",
            }
        ]
    }


def build_widget_state(
    *,
    enabled: bool,
    time_label: str,
    timezone_name: str,
    next_run_at: str | None,
    running: bool,
    last_run_started_at: str | None,
    last_run_finished_at: str | None,
    last_status: str | None,
    last_error: str | None,
    last_summary: Mapping[str, Any] | None,
    run_history: Sequence[Mapping[str, Any]] | None = None,
) -> dict[str, Any]:
    """Build a stable DTO for the service widget."""
    rows = _history_rows(run_history)
    if not rows:
        rows = _summary_rows(
            last_summary,
            run_date=_summary_date(last_summary),
            processed_at=last_run_finished_at or last_run_started_at,
        )
    failed = sum(1 for row in rows if row["result"] == "error")
    return {
        "cron": {
            "enabled": enabled,
            "time": time_label,
            "timezone": timezone_name,
            "next_run_at": next_run_at,
            "running": running,
        },
        "last_run": {
            "started_at": last_run_started_at,
            "finished_at": last_run_finished_at,
            "status": last_status,
            "error": last_error,
            "today": _optional_text(last_summary.get("today") if last_summary else None),
            "mode": _optional_text(last_summary.get("mode") if last_summary else None),
        },
        "totals": {
            "checked": int(last_summary.get("checked", 0)) if last_summary else 0,
            "orders": len(rows),
            "success": len(rows) - failed,
            "errors": failed,
        },
        "rows": rows,
    }


def render_widget_html() -> str:
    """Return the embeddable HTML widget shell."""
    return """
<!doctype html>
<html lang="ru">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>Pickup storage monitor</title>
  <style>
    :root {
      color-scheme: light;
      --bg: #f6f7f9;
      --panel: #ffffff;
      --line: #d7dde5;
      --text: #1f2937;
      --muted: #64748b;
      --ok: #0f766e;
      --ok-bg: #dff7f3;
      --err: #b42318;
      --err-bg: #fee4e2;
      --warn: #9a6700;
      --warn-bg: #fff4ce;
      --accent: #2563eb;
    }
    * { box-sizing: border-box; }
    body {
      margin: 0;
      background: var(--bg);
      color: var(--text);
      font-family:
        Inter, ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont,
        "Segoe UI", sans-serif;
      font-size: 13px;
    }
    .shell { min-height: 100vh; padding: 16px; }
    .top {
      display: flex;
      align-items: flex-start;
      justify-content: space-between;
      gap: 12px;
      margin-bottom: 12px;
    }
    .title { font-size: 18px; font-weight: 700; line-height: 1.2; }
    .subtitle { margin-top: 4px; color: var(--muted); }
    .toolbar {
      display: flex;
      flex-wrap: wrap;
      justify-content: flex-end;
      gap: 8px;
    }
    input, select, button {
      height: 34px;
      border: 1px solid var(--line);
      border-radius: 6px;
      background: #fff;
      color: var(--text);
      font: inherit;
      padding: 0 10px;
    }
    button {
      background: var(--accent);
      border-color: var(--accent);
      color: #fff;
      font-weight: 600;
      cursor: pointer;
    }
    button:disabled { cursor: default; opacity: .65; }
    .summary {
      display: grid;
      grid-template-columns: repeat(4, minmax(0, 1fr));
      gap: 8px;
      margin-bottom: 12px;
    }
    .metric {
      min-width: 0;
      border: 1px solid var(--line);
      border-radius: 8px;
      background: var(--panel);
      padding: 10px 12px;
    }
    .metric .label {
      color: var(--muted);
      font-size: 11px;
      text-transform: uppercase;
      letter-spacing: 0;
    }
    .metric .value {
      margin-top: 5px;
      overflow: hidden;
      text-overflow: ellipsis;
      white-space: nowrap;
      font-size: 16px;
      font-weight: 700;
    }
    .panel {
      overflow: hidden;
      border: 1px solid var(--line);
      border-radius: 8px;
      background: var(--panel);
    }
    table {
      width: 100%;
      border-collapse: collapse;
      table-layout: fixed;
    }
    th, td {
      padding: 10px 12px;
      border-bottom: 1px solid var(--line);
      text-align: left;
      vertical-align: top;
    }
    th {
      color: var(--muted);
      font-size: 11px;
      font-weight: 700;
      text-transform: uppercase;
      letter-spacing: 0;
      background: #fbfcfe;
    }
    tr:last-child td { border-bottom: 0; }
    .order { font-weight: 700; }
    .muted { color: var(--muted); }
    .badge {
      display: inline-flex;
      align-items: center;
      min-height: 22px;
      max-width: 100%;
      border-radius: 999px;
      padding: 2px 8px;
      font-size: 12px;
      font-weight: 700;
      white-space: nowrap;
    }
    .badge.ok { background: var(--ok-bg); color: var(--ok); }
    .badge.err { background: var(--err-bg); color: var(--err); }
    .badge.warn { background: var(--warn-bg); color: var(--warn); }
    .empty {
      padding: 28px 16px;
      color: var(--muted);
      text-align: center;
    }
    @media (max-width: 720px) {
      .shell { padding: 12px; }
      .top { display: block; }
      .toolbar { justify-content: flex-start; margin-top: 12px; }
      .summary { grid-template-columns: repeat(2, minmax(0, 1fr)); }
      th:nth-child(5), td:nth-child(5) { display: none; }
    }
  </style>
</head>
<body>
  <main class="shell">
    <section class="top">
      <div>
        <div class="title">Pickup storage monitor</div>
        <div class="subtitle" data-subtitle>Загрузка...</div>
      </div>
      <div class="toolbar">
        <input id="dateFilter" type="date" aria-label="Дата" />
        <select id="resultFilter" aria-label="Результат">
          <option value="all">Все</option>
          <option value="success">Успешно</option>
          <option value="error">Ошибки</option>
        </select>
        <button id="refreshButton" type="button">Обновить</button>
      </div>
    </section>
    <section class="summary" aria-label="Сводка">
      <div class="metric">
        <div class="label">Cron</div>
        <div class="value" data-cron>-</div>
      </div>
      <div class="metric">
        <div class="label">Следующий запуск</div>
        <div class="value" data-next>-</div>
      </div>
      <div class="metric">
        <div class="label">Заказы</div>
        <div class="value" data-orders>-</div>
      </div>
      <div class="metric">
        <div class="label">Ошибки</div>
        <div class="value" data-errors>-</div>
      </div>
    </section>
    <section class="panel">
      <table>
        <thead>
          <tr>
            <th style="width: 22%">Заказ</th>
            <th style="width: 14%">Контакт</th>
            <th style="width: 14%">Результат</th>
            <th style="width: 14%">Канал</th>
            <th style="width: 14%">Новый срок</th>
            <th>Причина</th>
          </tr>
        </thead>
        <tbody id="rows">
          <tr><td colspan="6"><div class="empty">Загрузка...</div></td></tr>
        </tbody>
      </table>
    </section>
  </main>
  <script>
    const state = { raw: null, loading: false };
    const rowsEl = document.getElementById("rows");
    const dateFilter = document.getElementById("dateFilter");
    const resultFilter = document.getElementById("resultFilter");
    const refreshButton = document.getElementById("refreshButton");

    function text(value) {
      if (value === null || value === undefined || value === "") return "-";
      return String(value);
    }

    function escapeHtml(value) {
      return text(value)
        .replaceAll("&", "&amp;")
        .replaceAll("<", "&lt;")
        .replaceAll(">", "&gt;")
        .replaceAll('"', "&quot;")
        .replaceAll("'", "&#039;");
    }

    function formatDateTime(value) {
      if (!value) return "-";
      const parsed = new Date(value);
      if (Number.isNaN(parsed.getTime())) return text(value);
      return parsed.toLocaleString("ru-RU", {
        day: "2-digit",
        month: "2-digit",
        hour: "2-digit",
        minute: "2-digit"
      });
    }

    function resultBadge(row) {
      if (row.result === "error") return '<span class="badge err">Ошибка</span>';
      if (row.outcome === "skipped") return '<span class="badge warn">Пропуск</span>';
      return '<span class="badge ok">Успешно</span>';
    }

    function contactLink(row) {
      if (!row.contact_url) return escapeHtml(row.contact_label || "-");
      return `<a href="${escapeHtml(row.contact_url)}" target="_blank" rel="noreferrer">` +
        `${escapeHtml(row.contact_label || "Контакт")}</a>`;
    }

    function filteredRows() {
      const payload = state.raw || {};
      let rows = Array.isArray(payload.rows) ? payload.rows : [];
      if (dateFilter.value) {
        rows = rows.filter((row) => row.run_date === dateFilter.value);
      }
      if (resultFilter.value === "error") rows = rows.filter((row) => row.result === "error");
      if (resultFilter.value === "success") {
        rows = rows.filter((row) => row.result !== "error");
      }
      return rows;
    }

    function renderRows() {
      const rows = filteredRows();
      if (rows.length === 0) {
        rowsEl.innerHTML = [
          '<tr><td colspan="6">',
          '<div class="empty">Нет строк для выбранных фильтров</div>',
          '</td></tr>'
        ].join("");
        return;
      }
      rowsEl.innerHTML = rows.map((row) => `
        <tr>
          <td>
            <div class="order">${escapeHtml(row.order_id)}</div>
            <div class="muted">${escapeHtml(row.carrier)} · ${escapeHtml(row.run_date)}</div>
          </td>
          <td>${contactLink(row)}</td>
          <td>${resultBadge(row)}<div class="muted">${escapeHtml(row.outcome)}</div></td>
          <td>
            ${escapeHtml(row.channel_label)}
            ${row.message_id ? `<div class="muted">#${escapeHtml(row.message_id)}</div>` : ""}
          </td>
          <td>${escapeHtml(row.new_deadline)}</td>
          <td>${escapeHtml(row.reason)}</td>
        </tr>
      `).join("");
    }

    function render() {
      const payload = state.raw || {};
      const cron = payload.cron || {};
      const lastRun = payload.last_run || {};
      const totals = payload.totals || {};
      document.querySelector("[data-subtitle]").textContent =
        `Последний запуск: ${formatDateTime(lastRun.finished_at || lastRun.started_at)}` +
        ` · статус: ${text(lastRun.status)}`;
      document.querySelector("[data-cron]").textContent =
        cron.enabled ? `${text(cron.time)} ${text(cron.timezone)}` : "выключен";
      document.querySelector("[data-next]").textContent = formatDateTime(cron.next_run_at);
      document.querySelector("[data-orders]").textContent = text(totals.orders ?? totals.checked);
      document.querySelector("[data-errors]").textContent = text(totals.errors);
      renderRows();
    }

    async function load() {
      if (state.loading) return;
      state.loading = true;
      refreshButton.disabled = true;
      try {
        const response = await fetch("./state", { cache: "no-store" });
        if (!response.ok) throw new Error(`HTTP ${response.status}`);
        state.raw = await response.json();
        render();
      } catch (error) {
        rowsEl.innerHTML = [
          '<tr><td colspan="6">',
          `<div class="empty">Не удалось загрузить состояние: `,
          `${escapeHtml(error.message)}</div>`,
          '</td></tr>'
        ].join("");
      } finally {
        state.loading = false;
        refreshButton.disabled = false;
      }
    }

    refreshButton.addEventListener("click", load);
    dateFilter.addEventListener("change", renderRows);
    resultFilter.addEventListener("change", renderRows);
    load();
    setInterval(load, 60000);
  </script>
</body>
</html>
"""


def _history_rows(run_history: Sequence[Mapping[str, Any]] | None) -> list[dict[str, str]]:
    if not isinstance(run_history, Sequence) or isinstance(run_history, (str, bytes)):
        return []

    rows: list[dict[str, str]] = []
    for run in run_history:
        if not isinstance(run, Mapping):
            continue
        summary = run.get("summary")
        if not isinstance(summary, Mapping):
            continue
        rows.extend(
            _summary_rows(
                summary,
                run_date=_summary_date(summary),
                processed_at=_optional_text(run.get("finished_at"))
                or _optional_text(run.get("started_at")),
            )
        )
    rows.sort(key=lambda row: row.get("processed_at") or row.get("run_date") or "", reverse=True)
    return rows


def _summary_date(summary: Mapping[str, Any] | None) -> str | None:
    if not summary:
        return None
    return _optional_text(summary.get("today"))


def _summary_rows(
    summary: Mapping[str, Any] | None,
    *,
    run_date: str | None = None,
    processed_at: str | None = None,
) -> list[dict[str, str]]:
    if not summary:
        return []
    decisions = summary.get("decisions")
    if not isinstance(decisions, Sequence) or isinstance(decisions, (str, bytes)):
        return []

    grouped: dict[str, dict[str, Any]] = {}
    for decision in decisions:
        if not isinstance(decision, Mapping):
            continue
        order_id = _optional_text(decision.get("order_id")) or "unknown"
        group_key = _optional_text(decision.get("row_key")) or order_id
        row = grouped.setdefault(
            group_key,
            {
                "order_id": order_id,
                "carrier": _optional_text(decision.get("carrier")) or "5post",
                "result": "success",
                "outcome": "processed",
                "channel": None,
                "new_deadline": None,
                "contact_id": None,
                "contact_url": None,
                "message_id": None,
                "run_date": run_date,
                "processed_at": processed_at,
                "reasons": [],
            },
        )
        action = _optional_text(_enum_value(decision.get("action")))
        reason = _optional_text(decision.get("reason"))
        if reason and reason not in row["reasons"]:
            row["reasons"].append(reason)
        if _optional_text(decision.get("carrier")):
            row["carrier"] = _optional_text(decision.get("carrier"))
        if _optional_text(decision.get("channel")):
            row["channel"] = _optional_text(_enum_value(decision.get("channel")))
        if decision.get("new_deadline"):
            row["new_deadline"] = _date_text(decision.get("new_deadline"))
        if _optional_text(decision.get("contact_id")):
            row["contact_id"] = _optional_text(decision.get("contact_id"))
        if _optional_text(decision.get("contact_url")):
            row["contact_url"] = _optional_text(decision.get("contact_url"))
        if _optional_text(decision.get("message_id")):
            row["message_id"] = _optional_text(decision.get("message_id"))
        _apply_action(row, action)

    return [_finalize_row(row) for row in grouped.values() if _show_widget_row(row)]


def _show_widget_row(row: Mapping[str, Any]) -> bool:
    if row.get("result") == "error":
        return True
    return row.get("outcome") in {"extended", "notified"}


def _apply_action(row: dict[str, Any], action: str | None) -> None:
    if action == DecisionAction.OPERATOR_TASK.value:
        row["result"] = "error"
        row["outcome"] = "operator_task"
        return
    if row["result"] == "error":
        return
    if action == DecisionAction.NOTIFIED.value:
        row["outcome"] = "notified"
        return
    if action == DecisionAction.EXTENDED.value and row["outcome"] != "notified":
        row["outcome"] = "extended"
        return
    if action == DecisionAction.SKIPPED.value:
        row["outcome"] = "skipped"


def _finalize_row(row: Mapping[str, Any]) -> dict[str, str]:
    channel = _optional_text(row.get("channel"))
    return {
        "order_id": _optional_text(row.get("order_id")) or "unknown",
        "carrier": _optional_text(row.get("carrier")) or "5post",
        "result": _optional_text(row.get("result")) or "success",
        "outcome": _optional_text(row.get("outcome")) or "processed",
        "contact_id": _optional_text(row.get("contact_id")) or "",
        "contact_url": _optional_text(row.get("contact_url")) or "",
        "contact_label": _contact_label(row),
        "message_id": _optional_text(row.get("message_id")) or "",
        "run_date": _optional_text(row.get("run_date")) or "",
        "processed_at": _optional_text(row.get("processed_at")) or "",
        "channel_label": _channel_label(channel),
        "new_deadline": _optional_text(row.get("new_deadline")) or "-",
        "reason": "; ".join(_reason_label(reason) for reason in row.get("reasons") or []) or "-",
    }


def _enum_value(value: Any) -> Any:
    return getattr(value, "value", value)


def _optional_text(value: Any) -> str | None:
    if value is None:
        return None
    text = str(_enum_value(value)).strip()
    return text or None


def _date_text(value: Any) -> str:
    if isinstance(value, datetime):
        return value.date().isoformat()
    if isinstance(value, date):
        return value.isoformat()
    return str(value)


def _channel_label(channel: str | None) -> str:
    if channel == "bitrix":
        return "Bitrix IM/OpenLine"
    if channel == "email":
        return "E-mail"
    return "-"


def _contact_label(row: Mapping[str, Any]) -> str:
    contact_id = _optional_text(row.get("contact_id"))
    if contact_id:
        return f"Контакт {contact_id}"
    if _optional_text(row.get("contact_url")):
        return "Контакт"
    return "-"


def _reason_label(reason: Any) -> str:
    text = _optional_text(reason)
    if text is None:
        return "-"
    if text in _REASON_LABELS:
        return _REASON_LABELS[text]
    if text.startswith("carrier_auth_failed:"):
        return (
            "Ошибка авторизации в API перевозчика "
            f"({text.removeprefix('carrier_auth_failed:')})"
        )
    if text.startswith("carrier_http_failed:"):
        return f"Ошибка API перевозчика ({text.removeprefix('carrier_http_failed:')})"
    if text == "carrier_timeout":
        return "Таймаут API перевозчика"
    if text == "carrier_network_error":
        return "Сетевая ошибка API перевозчика"
    if text.startswith("carrier_fetch_failed:"):
        return f"Ошибка чтения заказов перевозчика: {text.removeprefix('carrier_fetch_failed:')}"
    if text.startswith("notification_failed:"):
        return f"Ошибка отправки уведомления: {text.removeprefix('notification_failed:')}"
    return text
