"""Tests for direct ERP source lookup."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from unclaimed_orders_service.erp import ErpSourceLookup

if TYPE_CHECKING:
    import pytest


@dataclass(slots=True)
class FakePlatformService:
    orders: list[dict]
    transport_order_number: str | None = None
    orders_by_number: dict[str, list[dict]] = field(default_factory=dict)
    transport_adapter: object | None = None
    calls: list[dict[str, object]] = field(default_factory=list)

    def get_orders(
        self,
        order_number: str = "",
        query_type: str = "number",
        by_date: str = "",
        length: int = 25,
        filters: dict | None = None,
    ) -> list[dict]:
        self.calls.append(
            {
                "order_number": order_number,
                "query_type": query_type,
                "by_date": by_date,
                "length": length,
                "filters": filters,
            }
        )
        if self.orders_by_number:
            return self.orders_by_number.get(order_number, [])[:length]
        return self.orders[:length]

    def find_transport_order_number(self, *numbers: str) -> str | None:
        self.calls.append({"transport_numbers": numbers})
        return self.transport_order_number


async def test_erp_source_lookup_returns_first_order_email(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = FakePlatformService(
        orders=[
            {
                "id": 1,
                "number": "427634",
                "status": "sent",
                "delivery_data": {
                    "email": "client@example.com",
                    "has_extend_hold_request": False,
                },
            },
            {
                "id": 2,
                "number": "427634",
                "delivery_data": {"email": "other@example.com"},
            },
        ]
    )
    monkeypatch.setattr("unclaimed_orders_service.erp._build_platform_service", lambda: service)
    monkeypatch.setattr("unclaimed_orders_service.erp._normalize_track_number", _fake_normalize)

    record = await ErpSourceLookup().find_order(
        "427634koibf",
        by_date="01/01/2024 - 12/31/2030",
    )

    assert record.found is True
    assert record.lookup_number == "427634koibf"
    assert record.order_number == "427634"
    assert record.email == "client@example.com"
    assert record.already_extended is False
    assert record.platform_order["id"] == 1
    assert service.calls[0] == {
        "order_number": "427634",
        "query_type": "number",
        "by_date": "01/01/2024 - 12/31/2030",
        "length": 1,
        "filters": None,
    }


async def test_erp_source_lookup_prefers_customer_phone(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = FakePlatformService(
        orders=[
            {
                "id": 1,
                "number": "430339",
                "delivery_data": {
                    "email": "info@graniphoto.ru",
                    "phone": "+7 913 839 40 03",
                },
                "payment_data": {"phone": "+7 967 612 79 60"},
                "user": {"phoneFormatted": "+7 967 612 79 60"},
            }
        ]
    )
    monkeypatch.setattr("unclaimed_orders_service.erp._build_platform_service", lambda: service)
    monkeypatch.setattr("unclaimed_orders_service.erp._normalize_track_number", _fake_normalize)

    record = await ErpSourceLookup().find_order("430339")

    assert record.found is True
    assert record.email == "info@graniphoto.ru"
    assert record.phone == "+7 967 612 79 60"


async def test_erp_source_lookup_stops_when_email_is_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = FakePlatformService(orders=[{"id": 1, "number": "427634", "delivery_data": {}}])
    monkeypatch.setattr("unclaimed_orders_service.erp._build_platform_service", lambda: service)
    monkeypatch.setattr("unclaimed_orders_service.erp._normalize_track_number", _fake_normalize)

    record = await ErpSourceLookup().find_order("427634")

    assert record.found is True
    assert record.order_number == "427634"
    assert record.email is None
    assert record.error == "email_not_found"


async def test_erp_source_lookup_marks_already_extended_order(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = FakePlatformService(
        orders=[
            {
                "id": 1,
                "number": "427634",
                "delivery_data": {
                    "email": "client@example.com",
                    "has_extend_hold_request": "1",
                },
            }
        ]
    )
    monkeypatch.setattr("unclaimed_orders_service.erp._build_platform_service", lambda: service)
    monkeypatch.setattr("unclaimed_orders_service.erp._normalize_track_number", _fake_normalize)

    record = await ErpSourceLookup().find_order("427634")

    assert record.found is True
    assert record.email == "client@example.com"
    assert record.already_extended is True


async def test_erp_source_lookup_uses_transport_fallback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = FakePlatformService(
        orders=[],
        transport_order_number="427634",
        orders_by_number={"427634": [{"id": 1, "delivery_data": {"email": "client@example.com"}}]},
    )
    monkeypatch.setattr("unclaimed_orders_service.erp._build_platform_service", lambda: service)
    monkeypatch.setattr("unclaimed_orders_service.erp._normalize_track_number", _fake_normalize)

    record = await ErpSourceLookup().find_order("TRACK-1")

    assert record.found is True
    assert record.order_number == "427634"
    assert record.email == "client@example.com"


async def test_erp_source_lookup_returns_not_found(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = FakePlatformService(orders=[])
    monkeypatch.setattr("unclaimed_orders_service.erp._build_platform_service", lambda: service)
    monkeypatch.setattr("unclaimed_orders_service.erp._normalize_track_number", _fake_normalize)

    record = await ErpSourceLookup().find_order("missing")

    assert record.found is False
    assert record.error is None


async def test_erp_source_lookup_rejects_empty_order_number() -> None:
    record = await ErpSourceLookup().find_order(" ")

    assert record.found is False
    assert record.error == "empty_order_number"


def _fake_normalize(value: str) -> str:
    digits = "".join(char for char in value if char.isdigit())
    return digits or value
