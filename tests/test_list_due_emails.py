"""Tests for due-email discovery pipeline."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date

from unclaimed_orders_service.adapters import (
    BitrixContactLookupResult,
    BitrixContactNotificationRoute,
)
from unclaimed_orders_service.domain import PickupOrder
from unclaimed_orders_service.list_due_emails import _list_carrier_due_emails


async def test_due_email_pipeline_uses_fivepost_extension_flag_over_erp_flag() -> None:
    today = date(2026, 7, 8)
    carrier = FakeCarrier(
        [
            PickupOrder(
                external_id="fivepost-1",
                recipient_name="Client",
                pickup_deadline=date(2026, 7, 10),
                status="waiting_pickup",
                already_extended=False,
                metadata={
                    "lookup_number": "420941-z9xiu",
                    "already_extended_source": "fivepost.expirationDateExtensionAllowed",
                },
            ),
            PickupOrder(
                external_id="fivepost-2",
                recipient_name="Extended",
                pickup_deadline=date(2026, 7, 10),
                status="waiting_pickup",
                already_extended=True,
                metadata={
                    "lookup_number": "420861-svix0",
                    "already_extended_source": "fivepost.expirationDateExtensionAllowed",
                },
            ),
        ]
    )
    erp = FakeErp(
        {
            "420941-z9xiu": FakeErpRecord(
                lookup_number="420941-z9xiu",
                order_number="420941",
                email="client@example.com",
                already_extended=True,
            ),
            "420861-svix0": FakeErpRecord(
                lookup_number="420861-svix0",
                order_number="420861",
                email="extended@example.com",
                already_extended=False,
            ),
        }
    )
    bitrix = FakeBitrix()

    payload = await _list_carrier_due_emails(
        today=today,
        carrier_name="fivepost",
        carrier_client=carrier,
        limit=0,
        include_emails=False,
        bypass_due=False,
        erp=erp,
        bitrix=bitrix,
    )

    assert bitrix.emails == ["client@example.com"]
    assert payload["due_orders"] == 2
    assert payload["email_count"] == 1
    assert payload["skipped_extended"] == 1
    assert payload["bitrix_contact_found"] == 1
    assert payload["bitrix_contact_missing"] == 0
    assert payload["bitrix_contact_errors"] == 0
    assert payload["notification_openline_routes"] == 0
    assert payload["notification_email_fallback_routes"] == 1
    assert payload["notification_missing_routes"] == 0
    assert payload["emails"] == []
    assert payload["samples"][0]["bitrix_contact_found"] is True
    assert payload["samples"][0]["bitrix_contact_id"] == "contact-100"
    assert payload["samples"][0]["notification_channel"] == "email"
    assert payload["samples"][0]["notification_destination"] == "client@example.com"
    assert payload["samples"][0]["notification_connector_id"] is None
    assert payload["samples"][0]["already_extended"] is False
    assert payload["samples"][0]["already_extended_source"] == (
        "fivepost.expirationDateExtensionAllowed"
    )
    assert payload["samples"][1]["already_extended"] is True
    assert payload["samples"][1]["already_extended_source"] == (
        "fivepost.expirationDateExtensionAllowed"
    )
    assert "email" not in payload["samples"][0]
    assert "bitrix_contact_id" not in payload["samples"][1]

@dataclass(frozen=True, slots=True)
class FakeErpRecord:
    lookup_number: str
    found: bool = True
    order_number: str | None = None
    email: str | None = None
    already_extended: bool = False
    error: str | None = None


@dataclass(frozen=True, slots=True)
class FakeCarrier:
    orders: list[PickupOrder]

    async def list_waiting_pickup_orders(self, *, today: date) -> list[PickupOrder]:
        return self.orders


@dataclass(frozen=True, slots=True)
class FakeErp:
    records: dict[str, FakeErpRecord]

    async def find_order(self, lookup_number: str) -> FakeErpRecord:
        return self.records[lookup_number]


@dataclass(slots=True)
class FakeBitrix:
    emails: list[str]

    def __init__(self) -> None:
        self.emails = []

    async def find_contact_by_email(self, email: str) -> BitrixContactLookupResult:
        self.emails.append(email)
        return BitrixContactLookupResult(found=True, contact_id="contact-100", matches=1)

    async def resolve_contact_notification_route(
        self,
        contact_id: str,
        *,
        fallback_email: str | None,
    ) -> BitrixContactNotificationRoute:
        return BitrixContactNotificationRoute(
            channel="email",
            destination=fallback_email,
        )
