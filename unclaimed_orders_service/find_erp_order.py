"""CLI for finding one order through embedded ERP lookup."""

from __future__ import annotations

import argparse
import asyncio
import json
import os

from unclaimed_orders_service.erp import ErpSourceLookup


def main() -> None:
    """Run the ERP order lookup CLI."""
    parser = argparse.ArgumentParser(description="Find one ERP order through platform-admin.")
    parser.add_argument("order_number", help="ERP order number or carrier tracking number.")
    parser.add_argument(
        "--by-date",
        default=os.environ.get("ERP_PROXY_BY_DATE"),
        help="ERP date range, e.g. 01/01/2024 - 12/31/2030.",
    )
    parser.add_argument(
        "--show-email",
        action="store_true",
        help="Include the resolved customer email in CLI output.",
    )
    args = parser.parse_args()
    payload = asyncio.run(
        _find_order(
            order_number=args.order_number,
            by_date=args.by_date,
            show_email=args.show_email,
        )
    )
    print(json.dumps(payload, ensure_ascii=False, indent=2, default=str))


async def _find_order(
    *,
    order_number: str,
    by_date: str | None = None,
    show_email: bool = False,
) -> dict:
    record = await ErpSourceLookup().find_order(order_number, by_date=by_date)
    payload = {
        "lookup_number": record.lookup_number,
        "found": record.found,
        "order_number": record.order_number,
        "email_present": bool(record.email),
        "erp_id": record.platform_order.get("id"),
        "erp_status": record.platform_order.get("status"),
        "payment_status": record.platform_order.get("payment_status"),
        "delivery_system_id": record.platform_order.get("delivery_system_id"),
        "tracking_number_present": bool(record.platform_order.get("tracking_number")),
        "error": record.error,
    }
    if show_email:
        payload["email"] = record.email
    return payload


if __name__ == "__main__":
    main()
