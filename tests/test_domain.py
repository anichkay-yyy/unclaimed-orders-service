"""Tests for the standalone unclaimed orders service."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, timedelta

from unclaimed_orders_service.adapters import ErpEmailCarrierClient
from unclaimed_orders_service.domain import (
    CarrierListFailure,
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
class FailingNotifier:
    fail_order_id: str
    messages: list[str] = field(default_factory=list)

    async def notify(
        self,
        order: PickupOrder,
        *,
        subject: str,
        message: str,
    ) -> NotificationResult:
        assert subject
        if order.external_id == self.fail_order_id:
            msg = f"Bitrix contact was not found for order {order.external_id}: None"
            raise ValueError(msg)
        self.messages.append(message)
        return NotificationResult(NotificationChannel.BITRIX, order.bitrix_entity_id or "")


@dataclass(slots=True)
class FakeTasks:
    reasons: list[str] = field(default_factory=list)

    async def create_task(self, order: PickupOrder, *, reason: str) -> str:
        self.reasons.append(reason)
        return f"task-{order.external_id}"


@dataclass(slots=True)
class CarrierWithListingFailure:
    orders: list[PickupOrder]
    failures: tuple[CarrierListFailure, ...]
    result: ExtensionResult

    async def list_waiting_pickup_orders(self, *, today: date) -> list[PickupOrder]:
        return self.orders

    def consume_listing_failures(self) -> tuple[CarrierListFailure, ...]:
        failures = self.failures
        self.failures = ()
        return failures

    async def extend_storage(self, order: PickupOrder, *, days: int) -> ExtensionResult:
        assert days == 5
        return self.result


@dataclass(slots=True)
class FakeErpRecord:
    found: bool
    order_number: str | None = None
    email: str | None = None
    phone: str | None = None
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
        "Обратите внимание, Ваш заказ m-1 ожидает получения до 05.07.2026.\n\n"
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


async def test_daily_summary_preserves_carrier_from_order_metadata() -> None:
    today = date(2026, 7, 4)
    yandex_order = PickupOrder(
        external_id="431501FPerp",
        recipient_name="Ирина",
        pickup_deadline=today + timedelta(days=1),
        status="waiting_pickup",
        email="client@example.com",
        metadata={"carrier": "yandex"},
    )
    carrier = FakeCarrier(
        orders=[yandex_order],
        result=ExtensionResult(ok=False, error="yandex_extension_not_configured"),
    )
    tasks = FakeTasks()

    summary = await UnclaimedOrdersService(carrier, FakeNotifier(), tasks).run_daily(today=today)

    assert summary.decisions[0].action is DecisionAction.OPERATOR_TASK
    assert summary.decisions[0].carrier == "yandex"
    assert summary.decisions[0].reason == "yandex_extension_not_configured"
    assert tasks.reasons == ["yandex_extension_not_configured"]


async def test_records_carrier_listing_failure_and_continues_orders() -> None:
    today = date(2026, 7, 4)
    new_deadline = today + timedelta(days=6)
    yandex_order = PickupOrder(
        external_id="431501FPerp",
        recipient_name="Ирина",
        pickup_deadline=today + timedelta(days=1),
        status="waiting_pickup",
        email="client@example.com",
        metadata={"carrier": "yandex"},
    )
    carrier = CarrierWithListingFailure(
        orders=[yandex_order],
        failures=(CarrierListFailure(carrier="fivepost", reason="carrier_auth_failed:401"),),
        result=ExtensionResult(ok=True, new_deadline=new_deadline),
    )
    tasks = FakeTasks()

    summary = await UnclaimedOrdersService(carrier, FakeNotifier(), tasks).run_daily(today=today)

    assert [decision.action for decision in summary.decisions] == [
        DecisionAction.OPERATOR_TASK,
        DecisionAction.EXTENDED,
        DecisionAction.NOTIFIED,
    ]
    assert summary.decisions[0].order_id == "carrier:fivepost"
    assert summary.decisions[0].carrier == "fivepost"
    assert summary.decisions[0].reason == "carrier_auth_failed:401"
    assert tasks.reasons == ["carrier_auth_failed:401"]


async def test_skips_notification_when_extension_is_not_allowed() -> None:
    today = date(2026, 7, 4)
    blocked_order = PickupOrder(
        external_id="m-1",
        recipient_name="Ирина",
        pickup_deadline=today + timedelta(days=1),
        status="waiting_pickup",
        email="client@example.com",
        already_extended=True,
    )
    carrier = FakeCarrier(
        orders=[blocked_order],
        result=ExtensionResult(ok=True, new_deadline=today + timedelta(days=6)),
    )
    notifier = FakeNotifier()

    summary = await UnclaimedOrdersService(carrier, notifier, FakeTasks()).run_daily(today=today)

    assert summary.decisions[0].action is DecisionAction.SKIPPED
    assert summary.decisions[0].reason == "extension_not_allowed_or_already_extended"
    assert carrier.extended == []
    assert notifier.messages == []


async def test_skips_notification_when_extension_deadline_is_not_confirmed() -> None:
    today = date(2026, 7, 4)
    carrier = FakeCarrier(
        orders=[order(today + timedelta(days=1))],
        result=ExtensionResult(ok=True, new_deadline=None),
    )
    notifier = FakeNotifier()

    summary = await UnclaimedOrdersService(carrier, notifier, FakeTasks()).run_daily(today=today)

    assert summary.decisions[0].action is DecisionAction.SKIPPED
    assert summary.decisions[0].reason == "extension_deadline_not_confirmed"
    assert carrier.extended == ["m-1"]
    assert notifier.messages == []


async def test_records_notification_error_and_continues_next_order() -> None:
    today = date(2026, 7, 4)
    new_deadline = today + timedelta(days=6)
    first_order = order(today + timedelta(days=1))
    second_order = PickupOrder(
        external_id="m-2",
        recipient_name="Ольга",
        pickup_deadline=today + timedelta(days=1),
        status="waiting_pickup",
        email="olga@example.com",
        bitrix_entity_id="lead-2",
    )
    carrier = FakeCarrier(
        orders=[first_order, second_order],
        result=ExtensionResult(ok=True, new_deadline=new_deadline),
    )
    notifier = FailingNotifier(fail_order_id="m-1")
    tasks = FakeTasks()

    summary = await UnclaimedOrdersService(carrier, notifier, tasks).run_daily(today=today)

    assert [decision.action for decision in summary.decisions] == [
        DecisionAction.EXTENDED,
        DecisionAction.OPERATOR_TASK,
        DecisionAction.EXTENDED,
        DecisionAction.NOTIFIED,
    ]
    assert summary.decisions[1].order_id == "m-1"
    assert summary.decisions[1].reason == "bitrix_contact_not_found"
    assert tasks.reasons == ["bitrix_contact_not_found"]
    assert carrier.extended == ["m-1", "m-2"]
    assert len(notifier.messages) == 1


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
            phone="+7 967 612 79 60",
            already_extended=False,
        )
    )

    orders = await ErpEmailCarrierClient(carrier=carrier, erp=erp).list_waiting_pickup_orders(
        today=today
    )

    assert erp.lookups == ["m-1"]
    assert orders[0].email == "erp@example.com"
    assert orders[0].metadata["erp_order_number"] == "420861"
    assert orders[0].metadata["erp_phone"] == "+7 967 612 79 60"


async def test_erp_email_carrier_does_not_enrich_outside_window_orders() -> None:
    today = date(2026, 7, 4)
    carrier = FakeCarrier(
        orders=[order(today + timedelta(days=4))],
        result=ExtensionResult(ok=True),
    )
    erp = FakeErp(FakeErpRecord(found=True, email="erp@example.com"))

    orders = await ErpEmailCarrierClient(carrier=carrier, erp=erp).list_waiting_pickup_orders(
        today=today
    )

    assert erp.lookups == []
    assert orders[0].email == "client@example.com"
    assert "erp_order_number" not in orders[0].metadata


async def test_erp_email_carrier_does_not_enrich_already_extended_orders() -> None:
    today = date(2026, 7, 4)
    blocked_order = PickupOrder(
        external_id="m-1",
        recipient_name="Ирина",
        pickup_deadline=today + timedelta(days=1),
        status="waiting_pickup",
        email="client@example.com",
        already_extended=True,
    )
    carrier = FakeCarrier(
        orders=[blocked_order],
        result=ExtensionResult(ok=True),
    )
    erp = FakeErp(FakeErpRecord(found=True, email="erp@example.com"))

    orders = await ErpEmailCarrierClient(carrier=carrier, erp=erp).list_waiting_pickup_orders(
        today=today
    )

    assert erp.lookups == []
    assert orders[0].email == "client@example.com"
