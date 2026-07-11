# Unclaimed Orders Service

Standalone MVP service for pickup-point unclaimed orders.

Current carrier sources: SafeRoute/Magnit Post, 5Post, and Yandex Delivery.

## Flow

1. Read pickup orders from the carrier API.
2. Keep only carrier orders that are currently waiting at pickup points.
3. Compute the pickup deadline from carrier data:
   SafeRoute uses tracking `holdDays`, 5Post uses `details.expiredDate`, and
   Yandex uses pickup-status timestamp plus `YANDEX_STORAGE_DAYS`.
4. Keep orders whose storage deadline is inside the notification window.
5. Resolve each due order in ERP by order number and read the customer email.
6. Exclude already-extended orders. For 5Post the source of truth is carrier
   `details.expirationDateExtensionAllowed == false`; other carriers fall back to
   ERP `delivery_data.has_extend_hold_request`.
7. Find the Bitrix contact by email with `crm.contact.list`.
8. After storage is extended, notify the Bitrix contact: prefer a non-web Open
   Line chat linked to the contact; exclude online-chat connectors
   (`integracio_chat`, `livechat`).
9. Fall back to the contact/customer email when no allowed Open Line chat exists
   or Open Line sending fails.
10. Create an operator task when extension or notification cannot be completed.

Customer message after a successful extension:

```text
Здравствуйте!💛

Обратите внимание, Ваш заказ ожидает получения до DD.MM.YYYY.

Но мы уже продлили срок его хранения до DD.MM.YYYY.✔
Заберите, пожалуйста, заказ до этого времени.
```

## Run

```bash
uv run --project tools/unclaimed_orders_service uvicorn unclaimed_orders_service.app:app --reload --port 8000
```

The service starts an embedded daily scheduler by default:

```bash
UNCLAIMED_ORDERS_CRON_ENABLED=1
UNCLAIMED_ORDERS_CRON_TIME=09:00
UNCLAIMED_ORDERS_CRON_TZ=Europe/Moscow
```

Check the scheduler state:

```bash
curl http://127.0.0.1:8000/runs/cron
```

Run this service with one `uvicorn` worker when the embedded scheduler is
enabled. If deployment uses system cron or a Kubernetes CronJob instead, set
`UNCLAIMED_ORDERS_CRON_ENABLED=0` and call `POST /runs/daily` from the external
scheduler.

Docker:

```bash
docker compose -f docker-compose.example.yml up -d --build
```

Mary/server deploy payload:

```text
repo_url: https://github.com/anichkay-yyy/unclaimed-orders-service.git
ref: main
dockerfile: Dockerfile
env_file_path: .env.sops
port: 8000
health: /health
```

Decrypt `.env.sops` with the same age private key used for `erp-proxy-service`.

Then:

```bash
curl -X POST http://127.0.0.1:8000/runs/daily
```

Fetch orders only:

```bash
curl http://127.0.0.1:8000/orders/waiting
uv run --project tools/unclaimed_orders_service python -m unclaimed_orders_service.list_orders
```

Fetch real SafeRoute orders:

```bash
SAFEROUTE_API_BASE_URL=https://api.saferoute.ru \
SAFEROUTE_EMAIL=... \
SAFEROUTE_PASSWORD=... \
uv run --project tools/unclaimed_orders_service \
  python -m unclaimed_orders_service.list_orders --source saferoute
```

Find one ERP order and check whether ERP has a customer email:

```bash
uv run --project tools/unclaimed_orders_service \
  python -m unclaimed_orders_service.find_erp_order 427634koibf \
  --by-date '01/01/2024 - 12/31/2030'
```

This uses the embedded platform-admin lookup code and does not require the
`erp-proxy-service` source checkout or HTTP server. It returns only non-personal
technical fields on stdout unless `--show-email` is passed.

List emails for currently due carrier orders:

```bash
uv run --project tools/unclaimed_orders_service \
  python -m unclaimed_orders_service.list_due_emails \
  --today 2026-07-06 \
  --carrier saferoute \
  --include-emails
```

Use `--carrier fivepost`, `--carrier yandex`, or `--carrier all` for the other
carrier-first flows. 5Post reads `/partners-portal/api/v1/orders/query` and
Yandex reads `/api/b2b/platform/requests/info`; ERP is used only after the
carrier due-filter to resolve email. The 5Post already-extended flag comes from
5Post details; ERP is only a fallback for carriers that do not provide it.

Bitrix contact lookup is read-only and optional. Configure either a full webhook
base URL:

```bash
BITRIX_WEBHOOK_URL=https://example.bitrix24.ru/rest/1/webhook-token
```

or split values:

```bash
BITRIX_PORTAL_HOST=example.bitrix24.ru
BITRIX_WEBHOOK_PATH=1/webhook-token
```

Set `BITRIX_EMAIL_FROM` to a Bitrix-connected outbound mailbox for e-mail
fallbacks:

```bash
BITRIX_EMAIL_FROM="Служба поддержки клиентов Фабрики Фотокниги <support@fabrika-fotoknigi.com>"
```

The output includes `bitrix_configured`, `bitrix_contact_found`,
`bitrix_contact_missing`, `bitrix_contact_errors`,
`notification_openline_routes`, `notification_email_fallback_routes`,
`notification_missing_routes`, and per-order sample fields when lookup was
attempted. Without Bitrix env, the service still resolves ERP emails and marks
the Bitrix lookup as not configured.

To test the carrier -> ERP linkage without waiting for orders to enter the
notification window, add `--bypass-due --limit 2`. This skips the due-window
filter, takes the first two waiting orders per carrier, runs the ERP lookup on
each, and reports per-order `samples` (lookup number, ERP found, resolved order
number, email present, already-extended, error).

When `FIVEPOST_LOGIN`, `FIVEPOST_PASSWORD`, and Bitrix webhook env are present,
the daily service uses live 5Post extension and Bitrix notification adapters.
Operator tasks still use the dry-run adapter. Without the live env, the service
falls back to demo dry-run mode.
