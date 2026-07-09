"""Tests for the standalone unclaimed orders service."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, timedelta

from unclaimed_orders_service.adapters import ErpEmailCarrierClient
from unclaimed_orders_service.domain import (
    DecisionAction,
    ExtensionResult,
    NotificationChannel,
    NotificationResult,
    PickupOrder,
    UnclaimedOrdersService,
)


@dataclass(slots=True)
class FakeCarrier:
    orders: list[PickupOrder]
    result: ExtensionResult
    extended: list[str] = field(default_factory=list)

    async def list_waiting_pickup_orders(self, *, today: date) -> list[PickupOrder]:
        return self.orders

    async def extend_storage(self, order: PickupOrder, *, days: int) -> ExtensionResult:
        assert days == 5
        self.extended.append(order.external_id)
        return self.result


@dataclass(slots=True)
class FakeNotifier:
    messages: list[str] = field(default_factory=list)

    async def notify(
        self,
        order: PickupOrder,
        *,
        subject: str,
        message: str,
    ) -> NotificationResult:
        assert subject
        self.messages.append(message)
        return NotificationResult(NotificationChannel.BITRIX, order.bitrix_entity_id or "")


@dataclass(slots=True)
class FakeTasks:
    reasons: list[str] = field(default_factory=list)

    async def create_task(self, order: PickupOrder, *, reason: str) -> str:
        self.reasons.append(reason)
        return f"task-{order.external_id}"


@dataclass(slots=True)
class FakeErpRecord:
    found: bool
    order_number: str | None = None
    email: str | None = None
    already_extended: bool = False
    error: str | None = None


@dataclass(slots=True)
class FakeErp:
    record: FakeErpRecord
    lookups: list[str] = field(default_factory=list)

    async def find_order(self, order_number: str) -> FakeErpRecord:
        self.lookups.append(order_number)
        return self.record


def order(deadline: date) -> PickupOrder:
    return PickupOrder(
        external_id="m-1",
        recipient_name="Ирина",
        pickup_deadline=deadline,
        status="waiting_pickup",
        email="client@example.com",
        bitrix_entity_id="lead-1",
    )


async def test_extends_and_notifies_due_order() -> None:
    today = date(2026, 7, 4)
    new_deadline = today + timedelta(days=6)
    carrier = FakeCarrier(
        orders=[order(today + timedelta(days=1))],
        result=ExtensionResult(ok=True, new_deadline=new_deadline),
    )
    notifier = FakeNotifier()
    tasks = FakeTasks()

    summary = await UnclaimedOrdersService(carrier, notifier, tasks).run_daily(today=today)

    assert summary.checked == 1
    assert [decision.action for decision in summary.decisions] == [
        DecisionAction.EXTENDED,
        DecisionAction.NOTIFIED,
    ]
    assert carrier.extended == ["m-1"]
    assert notifier.messages[0] == (
        "Здравствуйте!💛\n\n"
        "Обратите внимание, Ваш заказ ожидает получения до 05.07.2026.\n\n"
        "Но мы уже продлили срок его хранения до 10.07.2026.✔\n"
        "Заберите, пожалуйста, заказ до этого времени."
    )
    assert tasks.reasons == []


async def test_skips_order_outside_window() -> None:
    today = date(2026, 7, 4)
    carrier = FakeCarrier(
        orders=[order(today + timedelta(days=4))],
        result=ExtensionResult(ok=True),
    )
    notifier = FakeNotifier()

    summary = await UnclaimedOrdersService(carrier, notifier, FakeTasks()).run_daily(today=today)

    assert summary.decisions[0].action is DecisionAction.SKIPPED
    assert carrier.extended == []
    assert notifier.messages == []


async def test_creates_operator_task_when_extension_fails() -> None:
    today = date(2026, 7, 4)
    carrier = FakeCarrier(
        orders=[order(today + timedelta(days=1))],
        result=ExtensionResult(ok=False, error="carrier_error"),
    )
    tasks = FakeTasks()

    summary = await UnclaimedOrdersService(carrier, FakeNotifier(), tasks).run_daily(today=today)

    assert summary.decisions[0].action is DecisionAction.OPERATOR_TASK
    assert tasks.reasons == ["carrier_error"]


async def test_erp_email_carrier_enriches_order_before_domain_flow() -> None:
    today = date(2026, 7, 4)
    carrier = FakeCarrier(
        orders=[order(today + timedelta(days=1))],
        result=ExtensionResult(ok=True, new_deadline=today + timedelta(days=6)),
    )
    erp = FakeErp(
        FakeErpRecord(
            found=True,
            order_number="420861",
            email="erp@example.com",
            already_extended=False,
        )
    )

    orders = await ErpEmailCarrierClient(carrier=carrier, erp=erp).list_waiting_pickup_orders(
        today=today
    )

    assert erp.lookups == ["m-1"]
    assert orders[0].email == "erp@example.com"
    assert orders[0].metadata["erp_order_number"] == "420861"
