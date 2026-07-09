"""CLI for listing customer emails that need unclaimed-order reminders."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
from datetime import UTC, date, datetime
from typing import Any

from unclaimed_orders_service.adapters import (
    BitrixContactClient,
    BitrixContactLookupResult,
    BitrixContactNotificationRoute,
    FivePostClient,
    SafeRouteClient,
    YandexDeliveryClient,
)
from unclaimed_orders_service.domain import MagnitPostPolicy
from unclaimed_orders_service.erp import ErpSourceLookup


def main() -> None:
    """Run the due email dry-run."""
    parser = argparse.ArgumentParser(description="List due unclaimed-order customer emails.")
    parser.add_argument("--today", type=date.fromisoformat, default=datetime.now(UTC).date())
    parser.add_argument(
        "--carrier",
        # Only 5Post is enabled for now; saferoute/yandex temporarily disabled.
        choices=("fivepost",),
        default="fivepost",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=0,
        help="Optional max carrier orders to resolve in ERP.",
    )
    parser.add_argument("--include-emails", action="store_true", help="Print resolved emails.")
    parser.add_argument(
        "--bypass-due",
        action="store_true",
        help="Skip the notification-window filter and ERP-check the first --limit waiting orders.",
    )
    args = parser.parse_args()
    payload = asyncio.run(
        _list_due_emails(
            today=args.today,
            carrier=args.carrier,
            limit=args.limit,
            include_emails=args.include_emails,
            bypass_due=args.bypass_due,
        )
    )
    print(json.dumps(payload, ensure_ascii=False, indent=2, default=str))


async def _list_due_emails(
    *,
    today: date,
    carrier: str = "fivepost",
    limit: int = 0,
    include_emails: bool = False,
    bypass_due: bool = False,
) -> dict[str, Any]:
    # NOTE: Only 5Post is enabled for now. SafeRoute and Yandex are temporarily
    # disabled (Yandex deadline is only a heuristic). Re-enable the blocks below
    # to bring them back.
    if carrier == "fivepost":
        return await _list_carrier_due_emails(
            today=today,
            carrier_name="fivepost",
            carrier_client=_build_fivepost_client(),
            limit=limit,
            include_emails=include_emails,
            bypass_due=bypass_due,
        )
    # if carrier == "yandex":
    #     return await _list_carrier_due_emails(
    #         today=today,
    #         carrier_name="yandex",
    #         carrier_client=_build_yandex_client(),
    #         limit=limit,
    #         include_emails=include_emails,
    #         bypass_due=bypass_due,
    #     )
    # if carrier == "all":
    #     saferoute = await _list_saferoute_due_emails(
    #         today=today,
    #         limit=limit,
    #         include_emails=include_emails,
    #         bypass_due=bypass_due,
    #     )
    #     fivepost = await _list_carrier_due_emails(
    #         today=today,
    #         carrier_name="fivepost",
    #         carrier_client=_build_fivepost_client(),
    #         limit=limit,
    #         include_emails=include_emails,
    #         bypass_due=bypass_due,
    #     )
    #     yandex = await _list_carrier_due_emails(
    #         today=today,
    #         carrier_name="yandex",
    #         carrier_client=_build_yandex_client(),
    #         limit=limit,
    #         include_emails=include_emails,
    #         bypass_due=bypass_due,
    #     )
    #     return {"today": today.isoformat(), "carriers": [saferoute, fivepost, yandex]}
    # return await _list_saferoute_due_emails(
    #     today=today,
    #     limit=limit,
    #     include_emails=include_emails,
    #     bypass_due=bypass_due,
    # )
    msg = f"carrier {carrier!r} is disabled; only 'fivepost' is enabled for now"
    raise SystemExit(msg)


async def _list_saferoute_due_emails(
    *,
    today: date,
    limit: int = 0,
    include_emails: bool = False,
    bypass_due: bool = False,
) -> dict[str, Any]:
    policy = MagnitPostPolicy()
    carrier = _build_saferoute_client()
    erp = ErpSourceLookup()
    bitrix = _build_bitrix_contact_client()

    waiting_orders = await carrier.list_waiting_pickup_orders(today=today)
    selected = _select_orders(waiting_orders, today=today, policy=policy, bypass_due=bypass_due)
    if limit > 0:
        selected = selected[:limit]

    emails: list[str] = []
    skipped_extended = 0
    bitrix_found = 0
    bitrix_missing = 0
    bitrix_errors = 0
    openline_routes = 0
    email_fallback_routes = 0
    missing_routes = 0
    misses: list[dict[str, Any]] = []
    samples: list[dict[str, Any]] = []
    for order in selected:
        lookup_number = order.external_id or str(order.metadata.get("track_number") or "")
        record = await erp.find_order(lookup_number)
        already_extended = _already_extended(order, record)
        contact = (
            await _find_bitrix_contact(bitrix, record.email)
            if record.email and not already_extended
            else None
        )
        bitrix_found, bitrix_missing, bitrix_errors = _count_bitrix_lookup(
            contact,
            found=bitrix_found,
            missing=bitrix_missing,
            errors=bitrix_errors,
        )
        notification_route = None
        if not already_extended:
            notification_route = await _resolve_notification_route(bitrix, contact, record.email)
            openline_routes, email_fallback_routes, missing_routes = _count_notification_route(
                notification_route,
                openline=openline_routes,
                email_fallback=email_fallback_routes,
                missing=missing_routes,
            )
        samples.append(
            _build_sample(
                order,
                lookup_number,
                record,
                today,
                include_emails,
                contact,
                notification_route,
                already_extended=already_extended,
            )
        )
        if already_extended:
            skipped_extended += 1
            continue
        if record.email:
            emails.append(record.email)
            continue
        misses.append(
            {
                "lookup_number": lookup_number,
                "found": record.found,
                "order_number": record.order_number,
                "error": record.error,
            }
        )

    return {
        "carrier": "saferoute",
        "today": today.isoformat(),
        "waiting_orders": len(waiting_orders),
        "bypass_due": bypass_due,
        "due_orders": len(selected),
        "skipped_extended": skipped_extended,
        "bitrix_configured": bitrix is not None,
        "bitrix_contact_found": bitrix_found,
        "bitrix_contact_missing": bitrix_missing,
        "bitrix_contact_errors": bitrix_errors,
        "notification_openline_routes": openline_routes,
        "notification_email_fallback_routes": email_fallback_routes,
        "notification_missing_routes": missing_routes,
        "emails": emails if include_emails else [],
        "email_count": len(emails),
        "misses": misses,
        "samples": samples,
    }


async def _list_carrier_due_emails(
    *,
    today: date,
    carrier_name: str,
    carrier_client: Any,
    limit: int,
    include_emails: bool,
    bypass_due: bool = False,
    erp: Any | None = None,
    bitrix: BitrixContactClient | None = None,
) -> dict[str, Any]:
    policy = MagnitPostPolicy()
    erp = erp or ErpSourceLookup()
    bitrix = bitrix if bitrix is not None else _build_bitrix_contact_client()

    waiting_orders = await carrier_client.list_waiting_pickup_orders(today=today)
    selected = _select_orders(waiting_orders, today=today, policy=policy, bypass_due=bypass_due)
    if limit > 0:
        selected = selected[:limit]

    emails: list[str] = []
    skipped_extended = 0
    bitrix_found = 0
    bitrix_missing = 0
    bitrix_errors = 0
    openline_routes = 0
    email_fallback_routes = 0
    missing_routes = 0
    misses: list[dict[str, Any]] = []
    samples: list[dict[str, Any]] = []
    for order in selected:
        lookup_number = str(order.metadata.get("lookup_number") or order.external_id)
        record = await erp.find_order(lookup_number)
        already_extended = _already_extended(order, record)
        contact = (
            await _find_bitrix_contact(bitrix, record.email)
            if record.email and not already_extended
            else None
        )
        bitrix_found, bitrix_missing, bitrix_errors = _count_bitrix_lookup(
            contact,
            found=bitrix_found,
            missing=bitrix_missing,
            errors=bitrix_errors,
        )
        notification_route = None
        if not already_extended:
            notification_route = await _resolve_notification_route(bitrix, contact, record.email)
            openline_routes, email_fallback_routes, missing_routes = _count_notification_route(
                notification_route,
                openline=openline_routes,
                email_fallback=email_fallback_routes,
                missing=missing_routes,
            )
        samples.append(
            _build_sample(
                order,
                lookup_number,
                record,
                today,
                include_emails,
                contact,
                notification_route,
                already_extended=already_extended,
            )
        )
        if already_extended:
            skipped_extended += 1
            continue
        if record.email:
            emails.append(record.email)
            continue
        misses.append(
            {
                "lookup_number": lookup_number,
                "found": record.found,
                "order_number": record.order_number,
                "error": record.error,
            }
        )

    return {
        "carrier": carrier_name,
        "today": today.isoformat(),
        "waiting_orders": len(waiting_orders),
        "bypass_due": bypass_due,
        "due_orders": len(selected),
        "skipped_extended": skipped_extended,
        "bitrix_configured": bitrix is not None,
        "bitrix_contact_found": bitrix_found,
        "bitrix_contact_missing": bitrix_missing,
        "bitrix_contact_errors": bitrix_errors,
        "notification_openline_routes": openline_routes,
        "notification_email_fallback_routes": email_fallback_routes,
        "notification_missing_routes": missing_routes,
        "emails": emails if include_emails else [],
        "email_count": len(emails),
        "misses": misses,
        "samples": samples,
    }


def _select_orders(
    waiting_orders: list[Any],
    *,
    today: date,
    policy: MagnitPostPolicy,
    bypass_due: bool,
) -> list[Any]:
    if bypass_due:
        return list(waiting_orders)
    # Notify exactly notify_window_days (2) before the pickup deadline, not the
    # whole 0..N window — one reminder per order on its "2 days left" day.
    return [
        order
        for order in waiting_orders
        if (order.pickup_deadline - today).days == policy.notify_window_days
    ]


def _build_sample(
    order: Any,
    lookup_number: str,
    record: Any,
    today: date,
    include_emails: bool,
    contact: BitrixContactLookupResult | None = None,
    notification_route: BitrixContactNotificationRoute | None = None,
    already_extended: bool | None = None,
) -> dict[str, Any]:
    resolved_already_extended = (
        _already_extended(order, record) if already_extended is None else already_extended
    )
    sample = {
        "lookup_number": lookup_number,
        "carrier_deadline": order.pickup_deadline.isoformat(),
        "days_left": (order.pickup_deadline - today).days,
        "erp_found": record.found,
        "order_number": record.order_number,
        "email_present": bool(record.email),
        "already_extended": resolved_already_extended,
        "already_extended_source": _already_extended_source(order, record),
        "error": record.error,
    }
    if contact is not None:
        sample.update(
            {
                "bitrix_contact_found": contact.found,
                "bitrix_contact_id": contact.contact_id,
                "bitrix_contact_matches": contact.matches,
                "bitrix_error": contact.error,
            }
        )
    if notification_route is not None:
        sample.update(
            {
                "notification_channel": notification_route.channel,
                "notification_destination": notification_route.destination,
                "notification_connector_id": notification_route.connector_id,
                "notification_openline_active": notification_route.active,
                "notification_error": notification_route.error,
            }
        )
    if include_emails:
        sample["email"] = record.email
    return sample


def _already_extended(order: Any, record: Any) -> bool:
    if _already_extended_source(order, record, carrier_only=True) is not None:
        return bool(getattr(order, "already_extended", False))
    return bool(
        getattr(order, "already_extended", False) or getattr(record, "already_extended", False)
    )


def _already_extended_source(order: Any, record: Any, *, carrier_only: bool = False) -> str | None:
    metadata = getattr(order, "metadata", {})
    if isinstance(metadata, dict):
        source = metadata.get("already_extended_source")
        if source:
            return str(source)
    if carrier_only:
        return None
    if getattr(order, "already_extended", False):
        return "carrier.order.already_extended"
    if getattr(record, "already_extended", False):
        return "erp.delivery_data.has_extend_hold_request"
    return None


async def _find_bitrix_contact(
    bitrix: BitrixContactClient | None,
    email: str | None,
) -> BitrixContactLookupResult:
    if bitrix is None:
        return BitrixContactLookupResult(found=False, error="bitrix_not_configured")
    if not email:
        return BitrixContactLookupResult(found=False, error="email_not_found")
    return await bitrix.find_contact_by_email(email)


async def _resolve_notification_route(
    bitrix: BitrixContactClient | None,
    contact: BitrixContactLookupResult | None,
    email: str | None,
) -> BitrixContactNotificationRoute:
    if bitrix is not None and contact is not None and contact.found and contact.contact_id:
        return await bitrix.resolve_contact_notification_route(
            contact.contact_id,
            fallback_email=email,
        )
    if email:
        return BitrixContactNotificationRoute(channel="email", destination=email)
    return BitrixContactNotificationRoute(channel="none", error="no_contact_or_email")


def _count_bitrix_lookup(
    contact: BitrixContactLookupResult | None,
    *,
    found: int,
    missing: int,
    errors: int,
) -> tuple[int, int, int]:
    if contact is None:
        return found, missing, errors
    if contact.found:
        return found + 1, missing, errors
    if contact.error:
        return found, missing, errors + 1
    return found, missing + 1, errors


def _count_notification_route(
    route: BitrixContactNotificationRoute,
    *,
    openline: int,
    email_fallback: int,
    missing: int,
) -> tuple[int, int, int]:
    if route.channel == "openline":
        return openline + 1, email_fallback, missing
    if route.channel == "email":
        return openline, email_fallback + 1, missing
    return openline, email_fallback, missing + 1


def _build_saferoute_client() -> SafeRouteClient:
    base_url = os.environ.get("SAFEROUTE_BASE_URL") or os.environ.get("SAFEROUTE_API_BASE_URL")
    email = os.environ.get("SAFEROUTE_EMAIL")
    password = os.environ.get("SAFEROUTE_PASSWORD")
    if not base_url or not email or not password:
        msg = (
            "SAFEROUTE_BASE_URL/SAFEROUTE_API_BASE_URL, SAFEROUTE_EMAIL, "
            "and SAFEROUTE_PASSWORD are required"
        )
        raise SystemExit(msg)
    return SafeRouteClient(base_url=base_url, email=email, password=password)


def _build_fivepost_client() -> FivePostClient:
    base_url = os.environ.get("FIVEPOST_API_BASE_URL", "https://api-omni.x5.ru")
    login = os.environ.get("FIVEPOST_LOGIN")
    password = os.environ.get("FIVEPOST_PASSWORD")
    if not login or not password:
        msg = "FIVEPOST_LOGIN and FIVEPOST_PASSWORD are required"
        raise SystemExit(msg)
    max_pages = int(os.environ.get("FIVEPOST_MAX_PAGES", "0"))
    page_size = int(os.environ.get("FIVEPOST_PAGE_SIZE", "100"))
    return FivePostClient(
        base_url=base_url,
        login=login,
        password=password,
        max_pages=max_pages,
        page_size=page_size,
    )


def _build_bitrix_contact_client() -> BitrixContactClient | None:
    webhook_url = _bitrix_webhook_base_url()
    if webhook_url is None:
        return None
    max_pages = int(os.environ.get("BITRIX_MAX_PAGES", os.environ.get("BITRIX24_MAX_PAGES", "20")))
    page_size = int(os.environ.get("BITRIX_PAGE_SIZE", os.environ.get("BITRIX24_PAGE_SIZE", "50")))
    return BitrixContactClient(
        webhook_base_url=webhook_url,
        page_size=page_size,
        max_pages=max_pages,
    )


def _bitrix_webhook_base_url() -> str | None:
    direct = os.environ.get("BITRIX_WEBHOOK_URL") or os.environ.get("BITRIX24_WEBHOOK_URL")
    if direct:
        return direct.rstrip("/")

    webhook_path = os.environ.get("BITRIX_WEBHOOK_PATH") or os.environ.get("BITRIX24_WEBHOOK_PATH")
    if not webhook_path:
        return None

    base_url = os.environ.get("BITRIX_BASE_URL") or os.environ.get("BITRIX24_BASE_URL")
    if base_url:
        base = base_url.rstrip("/")
        if base.endswith("/rest"):
            return f"{base}/{webhook_path.strip('/')}"
        return f"{base}/rest/{webhook_path.strip('/')}"

    portal_host = os.environ.get("BITRIX_PORTAL_HOST") or os.environ.get("BITRIX24_PORTAL_HOST")
    if portal_host:
        host = portal_host.removeprefix("https://").removeprefix("http://").rstrip("/")
        return f"https://{host}/rest/{webhook_path.strip('/')}"

    portal = os.environ.get("BITRIX_PORTAL") or os.environ.get("BITRIX24_PORTAL")
    if portal:
        return f"https://{portal}.bitrix24.ru/rest/{webhook_path.strip('/')}"

    return None


def _build_yandex_client() -> YandexDeliveryClient:
    base_url = os.environ.get(
        "YANDEX_DELIVERY_API_BASE_URL",
        "https://b2b-authproxy.taxi.yandex.net",
    )
    oauth_token = os.environ.get("YANDEX_DELIVERY_OAUTH_TOKEN")
    if not oauth_token:
        msg = "YANDEX_DELIVERY_OAUTH_TOKEN is required"
        raise SystemExit(msg)
    storage_days = int(os.environ.get("YANDEX_STORAGE_DAYS", "7"))
    lookback_days = int(os.environ.get("YANDEX_LOOKBACK_DAYS", "90"))
    return YandexDeliveryClient(
        base_url=base_url,
        oauth_token=oauth_token,
        storage_days=storage_days,
        lookback_days=lookback_days,
    )


if __name__ == "__main__":
    main()
