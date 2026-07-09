"""CLI for fetching waiting pickup orders only."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
from dataclasses import asdict
from datetime import UTC, date, datetime
from typing import TYPE_CHECKING, Any

from unclaimed_orders_service.adapters import DemoCarrierClient, SafeRouteClient

if TYPE_CHECKING:
    from unclaimed_orders_service.domain import CarrierClient


def main() -> None:
    """Run the order-listing CLI."""
    parser = argparse.ArgumentParser(description="Fetch waiting pickup orders.")
    parser.add_argument("--today", type=date.fromisoformat, default=datetime.now(UTC).date())
    parser.add_argument(
        "--source",
        choices=("demo", "saferoute"),
        default="demo",
        help="Order source to query.",
    )
    args = parser.parse_args()
    payload = asyncio.run(_list_orders(source=args.source, today=args.today))
    print(json.dumps(payload, ensure_ascii=False, indent=2, default=str))


async def _list_orders(*, source: str, today: date) -> dict[str, Any]:
    carrier = _build_carrier(source)
    orders = await carrier.list_waiting_pickup_orders(today=today)
    return {
        "source": source,
        "today": today.isoformat(),
        "orders": [asdict(order) for order in orders],
    }


def _build_carrier(source: str) -> CarrierClient:
    if source == "demo":
        return DemoCarrierClient()
    base_url = os.environ.get("SAFEROUTE_BASE_URL") or os.environ.get("SAFEROUTE_API_BASE_URL")
    email = os.environ.get("SAFEROUTE_EMAIL")
    password = os.environ.get("SAFEROUTE_PASSWORD")
    if not base_url or not email or not password:
        msg = (
            "SAFEROUTE_BASE_URL/SAFEROUTE_API_BASE_URL, SAFEROUTE_EMAIL, "
            "and SAFEROUTE_PASSWORD are required for --source saferoute"
        )
        raise SystemExit(msg)
    return SafeRouteClient(base_url=base_url, email=email, password=password)


if __name__ == "__main__":
    main()
