"""Core algorithm for unclaimed pickup orders."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import TYPE_CHECKING, Any, Protocol

if TYPE_CHECKING:
    from datetime import date


class NotificationChannel(StrEnum):
    """Customer notification channel."""

    BITRIX = "bitrix"
    EMAIL = "email"


class DecisionAction(StrEnum):
    """Action produced by the daily run."""

    SKIPPED = "skipped"
    EXTENDED = "extended"
    NOTIFIED = "notified"
    OPERATOR_TASK = "operator_task"


@dataclass(frozen=True, slots=True)
class PickupOrder:
    """Order waiting for customer pickup."""

    external_id: str
    recipient_name: str
    pickup_deadline: date
    status: str
    email: str | None = None
    bitrix_entity_id: str | None = None
    already_extended: bool = False
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class ExtensionResult:
    """Carrier storage extension result."""

    ok: bool
    new_deadline: date | None = None
    error: str | None = None
    upstream_id: str | None = None


@dataclass(frozen=True, slots=True)
class NotificationResult:
    """Customer notification result."""

    channel: NotificationChannel
    destination: str
    message_id: str | None = None
    contact_id: str | None = None
    contact_url: str | None = None


@dataclass(frozen=True, slots=True)
class RunDecision:
    """Auditable decision for one order action."""

    order_id: str
    action: DecisionAction
    reason: str
    channel: NotificationChannel | None = None
    new_deadline: date | None = None
    message_id: str | None = None
    contact_id: str | None = None
    contact_url: str | None = None
    row_key: str | None = None
    carrier: str | None = None


@dataclass(frozen=True, slots=True)
class RunSummary:
    """Daily run summary."""

    checked: int
    decisions: tuple[RunDecision, ...]


@dataclass(frozen=True, slots=True)
class MagnitPostPolicy:
    """Magnit Post/SafeRoute rules."""

    notify_window_days: int = 2
    extension_days: int = 5
    subject: str = "Срок хранения заказа продлен"


class CarrierClient(Protocol):
    """Carrier API port."""

    async def list_waiting_pickup_orders(self, *, today: date) -> list[PickupOrder]:
        """Return orders waiting for pickup."""

    async def extend_storage(self, order: PickupOrder, *, days: int) -> ExtensionResult:
        """Extend pickup storage for an order."""


class ClientNotifier(Protocol):
    """Customer notification port."""

    async def notify(self, order: PickupOrder, *, subject: str, message: str) -> NotificationResult:
        """Notify customer."""


class OperatorTasks(Protocol):
    """Manual handoff port."""

    async def create_task(self, order: PickupOrder, *, reason: str) -> str:
        """Create an operator task."""


class UnclaimedOrdersService:
    """Daily unclaimed-order workflow."""

    def __init__(
        self,
        carrier: CarrierClient,
        notifier: ClientNotifier,
        operator_tasks: OperatorTasks,
        *,
        policy: MagnitPostPolicy | None = None,
    ) -> None:
        self._carrier = carrier
        self._notifier = notifier
        self._operator_tasks = operator_tasks
        self._policy = policy or MagnitPostPolicy()

    async def run_daily(self, *, today: date) -> RunSummary:
        """Run one daily check."""
        orders = await self._carrier.list_waiting_pickup_orders(today=today)
        decisions: list[RunDecision] = []

        for order in orders:
            days_left = (order.pickup_deadline - today).days
            carrier = _order_carrier(order)
            if days_left > self._policy.notify_window_days:
                decisions.append(
                    RunDecision(
                        order.external_id,
                        DecisionAction.SKIPPED,
                        "outside_window",
                        carrier=carrier,
                    )
                )
                continue
            if days_left < 0:
                decisions.append(await self._operator_task(order, "deadline_already_passed"))
                continue
            if order.already_extended:
                decisions.append(
                    RunDecision(
                        order.external_id,
                        DecisionAction.SKIPPED,
                        "extension_not_allowed_or_already_extended",
                        carrier=carrier,
                    )
                )
                continue

            extension = await self._extend(order)
            if not extension.ok:
                decisions.append(
                    await self._operator_task(order, extension.error or "extension_failed")
                )
                continue
            if extension.new_deadline is None:
                decisions.append(
                    RunDecision(
                        order.external_id,
                        DecisionAction.SKIPPED,
                        "extension_deadline_not_confirmed",
                        carrier=carrier,
                    )
                )
                continue
            decisions.append(
                RunDecision(
                    order_id=order.external_id,
                    action=DecisionAction.EXTENDED,
                    reason="extended_before_notification",
                    new_deadline=extension.new_deadline,
                    carrier=carrier,
                )
            )

            try:
                notification = await self._notifier.notify(
                    order,
                    subject=self._policy.subject,
                    message=build_client_message(order, extension),
                )
            except Exception as exc:
                decisions.append(
                    await self._operator_task(order, _notification_failure_reason(exc))
                )
                continue
            decisions.append(
                RunDecision(
                    order_id=order.external_id,
                    action=DecisionAction.NOTIFIED,
                    reason="client_notified",
                    channel=notification.channel,
                    new_deadline=extension.new_deadline,
                    message_id=notification.message_id,
                    contact_id=notification.contact_id,
                    contact_url=notification.contact_url,
                    carrier=carrier,
                )
            )

        return RunSummary(checked=len(orders), decisions=tuple(decisions))

    async def _extend(self, order: PickupOrder) -> ExtensionResult:
        return await self._carrier.extend_storage(order, days=self._policy.extension_days)

    async def _operator_task(self, order: PickupOrder, reason: str) -> RunDecision:
        await self._operator_tasks.create_task(order, reason=reason)
        return RunDecision(
            order.external_id,
            DecisionAction.OPERATOR_TASK,
            reason,
            carrier=_order_carrier(order),
        )


def _order_carrier(order: PickupOrder) -> str:
    carrier = order.metadata.get("carrier")
    if isinstance(carrier, str) and carrier.strip():
        return carrier.strip()
    return "5post"


def _notification_failure_reason(exc: Exception) -> str:
    """Return a stable reason code for notification failures."""
    message = str(exc)
    if "Bitrix contact was not found" in message:
        return "bitrix_contact_not_found"
    if "has no customer e-mail" in message:
        return "missing_customer_email"
    if message:
        return f"notification_failed:{type(exc).__name__}:{message}"
    return f"notification_failed:{type(exc).__name__}"


def build_client_message(order: PickupOrder, extension: ExtensionResult) -> str:
    """Build the customer-facing reminder."""
    if extension.new_deadline is not None:
        return (
            "Здравствуйте!💛\n\n"
            "Обратите внимание, Ваш заказ ожидает получения до "
            f"{order.pickup_deadline:%d.%m.%Y}.\n\n"
            f"Но мы уже продлили срок его хранения до {extension.new_deadline:%d.%m.%Y}.✔\n"
            "Заберите, пожалуйста, заказ до этого времени."
        )
    return (
        "Здравствуйте!💛\n\n"
        "Обратите внимание, Ваш заказ ожидает получения до "
        f"{order.pickup_deadline:%d.%m.%Y}.\n\n"
        "Заберите, пожалуйста, заказ до этого времени."
    )
