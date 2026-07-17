"""Dry-run adapters for the standalone unclaimed orders service."""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field, replace
from datetime import UTC, date, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

import httpx

from unclaimed_orders_service.domain import (
    CarrierListFailure,
    ExtensionResult,
    NotificationChannel,
    NotificationResult,
    PickupOrder,
)

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class DemoCarrierClient:
    """Demo carrier with one due order and one skipped order."""

    async def list_waiting_pickup_orders(self, *, today: date) -> list[PickupOrder]:
        """Return demo orders."""
        return [
            PickupOrder(
                external_id="MAGNIT-001",
                recipient_name="Ирина",
                pickup_deadline=today + timedelta(days=1),
                status="waiting_pickup",
                email="client@example.com",
                bitrix_entity_id="lead-100",
            ),
            PickupOrder(
                external_id="MAGNIT-002",
                recipient_name="Олег",
                pickup_deadline=today + timedelta(days=4),
                status="waiting_pickup",
                email="oleg@example.com",
            ),
        ]

    async def extend_storage(self, order: PickupOrder, *, days: int) -> ExtensionResult:
        """Pretend SafeRoute accepted the extension request."""
        return ExtensionResult(
            ok=True,
            new_deadline=order.pickup_deadline + timedelta(days=days),
            upstream_id=f"demo-extension-{order.external_id}",
        )


@dataclass(frozen=True, slots=True)
class CompositeCarrierClient:
    """Carrier adapter that combines several carrier sources."""

    carriers: tuple[Any, ...]
    tolerate_list_errors: bool = False
    _listing_failures: list[CarrierListFailure] = field(default_factory=list, init=False)

    async def list_waiting_pickup_orders(self, *, today: date) -> list[PickupOrder]:
        """Return waiting pickup orders from every configured carrier."""
        self._listing_failures.clear()
        orders: list[PickupOrder] = []
        for carrier in self.carriers:
            try:
                list_orders = carrier.list_waiting_pickup_orders
                orders.extend(await list_orders(today=today))
            except Exception as exc:
                if not self.tolerate_list_errors:
                    raise
                carrier_name = _carrier_name(carrier)
                logger.warning(
                    "carrier listing failed for %s: %s",
                    carrier_name,
                    exc,
                    exc_info=True,
                )
                self._listing_failures.append(
                    CarrierListFailure(
                        carrier=carrier_name,
                        reason=_carrier_list_failure_reason(exc),
                    )
                )
        return orders

    def consume_listing_failures(self) -> tuple[CarrierListFailure, ...]:
        """Return and clear carrier-level listing failures."""
        failures = tuple(self._listing_failures)
        self._listing_failures.clear()
        return failures

    async def extend_storage(self, order: PickupOrder, *, days: int) -> ExtensionResult:
        """Delegate extension to the carrier named in order metadata."""
        carrier_name = _optional_text(order.metadata.get("carrier"))
        for carrier in self.carriers:
            if _carrier_name(carrier) == carrier_name:
                extend = carrier.extend_storage
                return await extend(order, days=days)
        return ExtensionResult(
            ok=False,
            error=f"carrier_not_configured:{carrier_name or 'unknown'}",
        )


@dataclass(frozen=True, slots=True)
class SafeRouteClient:
    """SafeRoute HTTP client for Magnit Post pickup orders."""

    base_url: str
    email: str
    password: str
    per_page: int = 50
    max_pages: int = 3
    waiting_status_code: str = "42"
    delivery_company_name: str = "Магнит Пост"

    async def list_waiting_pickup_orders(self, *, today: date) -> list[PickupOrder]:
        """List Magnit Post orders waiting for pickup with full hold metadata."""
        token = await self._login()
        orders: list[PickupOrder] = []
        async with httpx.AsyncClient(base_url=self.base_url, timeout=30.0) as client:
            for page in range(1, self.max_pages + 1):
                response = await client.get(
                    "/v2/orders",
                    params={"page": page, "perPage": self.per_page},
                    headers=self._headers(token),
                )
                response.raise_for_status()
                rows = response.json()
                if not isinstance(rows, list) or not rows:
                    break
                for row in rows:
                    if not isinstance(row, dict) or not self._is_waiting_magnit_order(row):
                        continue
                    order = await self._safe_pickup_order(client, token, row)
                    if order is not None:
                        orders.append(order)
        return orders

    async def _safe_pickup_order(
        self,
        client: httpx.AsyncClient,
        token: str,
        row: dict,
    ) -> PickupOrder | None:
        """Build one pickup order, tolerating a single order's tracking failure."""
        try:
            tracking = await self._get_tracking(client, token, row["id"])
        except (httpx.HTTPError, RuntimeError) as exc:
            logger.warning("SafeRoute tracking failed for order %s: %s", row.get("id"), exc)
            tracking = {}
        try:
            return self._to_pickup_order(row, tracking)
        except RuntimeError as exc:
            logger.warning("SafeRoute order %s skipped: %s", row.get("id"), exc)
            return None

    async def extend_storage(self, order: PickupOrder, *, days: int) -> ExtensionResult:
        """Call SafeRoute hold extension endpoint."""
        saferoute_id = order.metadata.get("saferoute_id")
        if not saferoute_id:
            return ExtensionResult(ok=False, error="missing_saferoute_id")
        token = await self._login()
        payload = {"ids": [saferoute_id], "days": days}
        async with httpx.AsyncClient(base_url=self.base_url, timeout=30.0) as client:
            response = await client.post(
                "/v2/orders/extend-hold",
                json=payload,
                headers=self._headers(token),
            )
        if response.is_success:
            body = response.json()
            new_deadline_raw = body.get("new_deadline")
            return ExtensionResult(
                ok=True,
                new_deadline=date.fromisoformat(new_deadline_raw) if new_deadline_raw else None,
                upstream_id=str(body.get("id") or ""),
            )
        return ExtensionResult(ok=False, error=f"saferoute_http_{response.status_code}")

    async def _login(self) -> str:
        async with httpx.AsyncClient(base_url=self.base_url, timeout=30.0) as client:
            response = await client.post(
                "/v2/auth/login",
                json={"email": self.email, "password": self.password},
                headers={"Accept": "application/json", "Content-Type": "application/json"},
            )
        response.raise_for_status()
        payload = response.json()
        nested = payload.get("data") if isinstance(payload.get("data"), dict) else {}
        token = payload.get("token") or payload.get("access_token") or nested.get("token")
        if not token:
            msg = "SafeRoute auth response does not contain token"
            raise RuntimeError(msg)
        return str(token)

    @staticmethod
    def _headers(token: str) -> dict[str, str]:
        return {"Accept": "application/json", "Authorization": f"Bearer {token}"}

    async def _get_tracking(
        self,
        client: httpx.AsyncClient,
        token: str,
        saferoute_id: int,
    ) -> dict:
        response = await client.get(
            "/v2/tracking",
            params={"id": saferoute_id},
            headers=self._headers(token),
        )
        response.raise_for_status()
        payload = response.json()
        if not isinstance(payload, dict):
            msg = "SafeRoute tracking response is not an object"
            raise RuntimeError(msg)
        return payload

    def _is_waiting_magnit_order(self, row: dict) -> bool:
        delivery = _dict(row.get("delivery"))
        company = _dict(delivery.get("company"))
        latest = _latest_status(row)
        return (
            self.delivery_company_name.lower() in str(company.get("name") or "").lower()
            and str(latest.get("code") or latest.get("statusCode")) == self.waiting_status_code
        )

    def _to_pickup_order(self, row: dict, tracking: dict) -> PickupOrder:
        delivery = _dict(row.get("delivery"))
        tracking_delivery = _dict(tracking.get("delivery"))
        recipient = _dict(row.get("recipient"))
        latest = _latest_status(row)
        hold_days = _int_or_none(tracking_delivery.get("holdDays"))
        status_date = _parse_saferoute_datetime(str(latest.get("date") or ""))
        fallback_deadline = _parse_iso_date(_dict(delivery.get("date")).get("to"))
        if status_date and hold_days is not None:
            pickup_deadline = status_date.date() + timedelta(days=hold_days)
        elif fallback_deadline:
            pickup_deadline = fallback_deadline
        else:
            msg = f"SafeRoute order {row.get('id')} has no pickup deadline"
            raise RuntimeError(msg)

        return PickupOrder(
            external_id=str(row.get("cmsId") or row.get("id")),
            recipient_name=str(recipient.get("fullName") or ""),
            pickup_deadline=pickup_deadline,
            status="waiting_pickup",
            email=str(recipient.get("email") or "") or None,
            metadata={
                "saferoute_id": row.get("id"),
                "track_number": row.get("trackNumber"),
                "status_code": latest.get("code") or latest.get("statusCode"),
                "status_date": latest.get("date"),
                "delivery_date_to": _dict(delivery.get("date")).get("to"),
                "hold_days": hold_days,
                "deadline_source": "status_date_plus_hold_days"
                if status_date and hold_days is not None
                else "delivery_date_to",
            },
        )


@dataclass(frozen=True, slots=True)
class FivePostClient:
    """5Post carrier API client for pickup orders."""

    base_url: str
    login: str
    password: str
    page_size: int = 100
    max_pages: int = 0
    waiting_status_codes: tuple[str, ...] = ("RECEIVED_IN_STORE", "PLACED_IN_POSTAMAT")
    enrich_retries: int = 3
    enrich_concurrency: int = 8

    async def list_waiting_pickup_orders(self, *, today: date) -> list[PickupOrder]:
        """List 5Post orders that are waiting at pickup points."""
        token = await self._login()
        orders: list[PickupOrder] = []
        timeout = httpx.Timeout(connect=10.0, read=12.0, write=10.0, pool=10.0)
        async with httpx.AsyncClient(base_url=self.base_url.rstrip("/"), timeout=timeout) as client:
            page = 0
            while self.max_pages <= 0 or page < self.max_pages:
                response = await client.post(
                    "/partners-portal/api/v1/orders/query",
                    params={"page": page, "size": self.page_size, "sort": "createDate,desc"},
                    json={
                        "orderType": None,
                        "executionStatusList": list(self.waiting_status_codes),
                    },
                    headers=self._headers(token),
                )
                if response.status_code == 400 and page > 0:
                    break
                response.raise_for_status()
                payload = response.json()
                rows = payload.get("content") if isinstance(payload, dict) else None
                if not isinstance(rows, list) or not rows:
                    break
                semaphore = asyncio.Semaphore(max(1, self.enrich_concurrency))
                page_orders = await asyncio.gather(
                    *(
                        self._safe_enrich_pickup_order(client, token, row, semaphore)
                        for row in rows
                        if isinstance(row, dict) and self._is_waiting_order(row)
                    )
                )
                orders.extend(order for order in page_orders if order is not None)
                if isinstance(payload, dict) and payload.get("last") is True:
                    break
                page += 1
        return orders

    async def extend_storage(self, order: PickupOrder, *, days: int) -> ExtensionResult:
        """Extend 5Post pickup storage through the partners portal API."""
        order_id = _optional_text(order.metadata.get("order_id"))
        if not order_id:
            return ExtensionResult(ok=False, error="missing_fivepost_order_id")

        token = await self._login()
        timeout = httpx.Timeout(connect=10.0, read=20.0, write=10.0, pool=10.0)
        async with httpx.AsyncClient(base_url=self.base_url.rstrip("/"), timeout=timeout) as client:
            response = await client.put(
                f"/partners-portal/api/v1/orders/{order_id}/extend-expiration-date",
                json={},
                headers=self._headers(token),
            )
            if response.status_code == 401:
                token = await self._login()
                response = await client.put(
                    f"/partners-portal/api/v1/orders/{order_id}/extend-expiration-date",
                    json={},
                    headers=self._headers(token),
                )

        if response.status_code >= 400:
            return ExtensionResult(
                ok=False,
                error=f"fivepost_http_{response.status_code}",
            )

        body: object
        try:
            body = response.json()
        except ValueError:
            body = {}
        new_deadline = _fivepost_extension_deadline(body) or order.pickup_deadline + timedelta(
            days=days
        )
        return ExtensionResult(ok=True, new_deadline=new_deadline, upstream_id=order_id)

    async def _login(self) -> str:
        async with httpx.AsyncClient(base_url=self.base_url.rstrip("/"), timeout=30.0) as client:
            response = await client.post(
                "/partners-portal-auth/api/v2/auth",
                json={"login": self.login, "password": self.password},
                headers=self._headers(authorized=False),
            )
        response.raise_for_status()
        payload = response.json()
        token = payload.get("jwt") or payload.get("token") or payload.get("access_token")
        if not token:
            msg = "5Post auth response does not contain token"
            raise RuntimeError(msg)
        return str(token)

    @staticmethod
    def _headers(token: str | None = None, *, authorized: bool = True) -> dict[str, str]:
        headers = {
            "Accept": "application/json",
            "Accept-Language": "ru-RU;q=0.5",
            "Content-Type": "application/json",
        }
        if authorized:
            headers["Authorization"] = f"Bearer {token}"
        return headers

    def _is_waiting_order(self, row: dict) -> bool:
        return _status_code(row) in self.waiting_status_codes

    async def _enrich_order(
        self,
        client: httpx.AsyncClient,
        token: str,
        row: dict,
    ) -> dict[str, object]:
        order_id = row.get("orderId") or row.get("id")
        payload: dict[str, object] = {"order": row}
        if not order_id:
            return payload
        for name, path in (("details", f"/partners-portal/api/v1/order/{order_id}"),):
            response = await self._get_with_retries(client, token, path, order_id=str(order_id))
            if response is not None and response.status_code < 400:
                payload[name] = response.json()
        return payload

    async def _safe_enrich_pickup_order(
        self,
        client: httpx.AsyncClient,
        token: str,
        row: dict,
        semaphore: asyncio.Semaphore,
    ) -> PickupOrder | None:
        async with semaphore:
            try:
                enriched = await self._enrich_order(client, token, row)
                return self._to_pickup_order(enriched)
            except (httpx.HTTPError, RuntimeError, ValueError) as exc:
                logger.warning("5Post order %s skipped during enrich: %s", row.get("orderId"), exc)
                return None

    async def _get_with_retries(
        self,
        client: httpx.AsyncClient,
        token: str,
        path: str,
        *,
        order_id: str,
    ) -> httpx.Response | None:
        attempts = max(1, self.enrich_retries)
        last_error: Exception | None = None
        for attempt in range(1, attempts + 1):
            try:
                return await client.get(path, headers=self._headers(token))
            except (httpx.TimeoutException, httpx.NetworkError) as exc:
                last_error = exc
                if attempt >= attempts:
                    break
                await asyncio.sleep(0.25 * attempt)
        logger.warning(
            "5Post enrich request failed after retries for order %s path %s: %s",
            order_id,
            path,
            last_error,
        )
        return None

    def _to_pickup_order(self, payload: dict[str, object]) -> PickupOrder | None:
        order = _dict(payload.get("order"))
        details = _dict(payload.get("details"))
        latest = _latest_fivepost_history(payload)
        deadline = _parse_datetime_value(details.get("expiredDate"))
        if deadline is None:
            return None
        lookup_number = _first_text(
            details.get("senderOrderId"),
            details.get("clientOrderId"),
            order.get("senderOrderId"),
            order.get("clientOrderId"),
        )
        extension_allowed = _optional_bool(details.get("expirationDateExtensionAllowed"))
        return PickupOrder(
            external_id=lookup_number or str(order.get("orderId") or ""),
            recipient_name=str(
                details.get("receiverClientName") or order.get("receiverClientName") or ""
            ),
            pickup_deadline=deadline.date(),
            status="waiting_pickup",
            email=_optional_text(details.get("receiverClientEmail")),
            already_extended=extension_allowed is False,
            metadata={
                "carrier": "fivepost",
                "order_id": order.get("orderId") or order.get("id"),
                "lookup_number": lookup_number,
                "status_code": _status_code(details) or _status_code(order),
                "history_status_code": _status_code(latest),
                "status_date": latest.get("changeDate")
                or details.get("statusAssignmentDate")
                or order.get("statusAssignmentDate"),
                "expired_date": details.get("expiredDate"),
                "expiration_extension_allowed": details.get("expirationDateExtensionAllowed"),
                "already_extended_source": "fivepost.expirationDateExtensionAllowed"
                if extension_allowed is not None
                else None,
                "deadline_source": "details.expiredDate",
            },
        )


@dataclass(frozen=True, slots=True)
class BitrixContactLookupResult:
    """Bitrix contact lookup outcome."""

    found: bool
    contact_id: str | None = None
    matches: int = 0
    error: str | None = None


@dataclass(frozen=True, slots=True)
class BitrixOpenLineChat:
    """Open Line chat linked to a Bitrix CRM entity."""

    chat_id: str
    connector_id: str | None = None
    connector_title: str | None = None
    active: bool = False


@dataclass(frozen=True, slots=True)
class BitrixContactNotificationRoute:
    """Preferred notification route for a Bitrix contact."""

    channel: str
    destination: str | None = None
    connector_id: str | None = None
    connector_title: str | None = None
    active: bool = False
    error: str | None = None


@dataclass(frozen=True, slots=True)
class BitrixContactClient:
    """Bitrix24 contact lookup and notification client."""

    webhook_base_url: str
    email_from: str | None = None
    page_size: int = 50
    max_pages: int = 20
    excluded_openline_connectors: tuple[str, ...] = ("integracio_chat", "livechat")
    excluded_openline_title_terms: tuple[str, ...] = (
        "онлайн-чат",
        "онлайн чат",
        "online chat",
        "live chat",
        "web chat",
        "integracio",
    )
    allow_openline_notifications: bool = True

    async def find_contact_by_email(self, email: str) -> BitrixContactLookupResult:
        """Find the first Bitrix contact matching an email address."""
        normalized_email = email.strip()
        if not normalized_email:
            return BitrixContactLookupResult(found=False, error="empty_email")

        url = f"{self.webhook_base_url.rstrip('/')}/crm.contact.list.json"
        matches = 0
        first_contact_id: str | None = None
        start = 0

        async with httpx.AsyncClient(timeout=30.0) as client:
            for _ in range(self.max_pages):
                response = await client.post(
                    url,
                    json={
                        "filter": {"EMAIL": normalized_email},
                        "select": ["ID", "NAME", "LAST_NAME", "EMAIL", "PHONE", "IM"],
                        "order": {"ID": "ASC"},
                        "start": start,
                    },
                    headers={"Accept": "application/json", "Content-Type": "application/json"},
                )
                if response.status_code >= 400:
                    return BitrixContactLookupResult(
                        found=False,
                        matches=matches,
                        error=f"bitrix_http_{response.status_code}",
                    )

                payload = response.json()
                if not isinstance(payload, dict):
                    return BitrixContactLookupResult(
                        found=False,
                        error="bitrix_response_not_object",
                    )
                if payload.get("error"):
                    return BitrixContactLookupResult(
                        found=False,
                        error=f"bitrix_error:{payload.get('error')}",
                    )

                rows = payload.get("result")
                if not isinstance(rows, list):
                    return BitrixContactLookupResult(found=False, error="bitrix_result_not_list")

                for row in rows:
                    if not isinstance(row, dict):
                        continue
                    contact_id = _optional_text(row.get("ID") or row.get("id"))
                    if not contact_id:
                        continue
                    matches += 1
                    first_contact_id = first_contact_id or contact_id

                next_start = _int_or_none(payload.get("next"))
                if next_start is None or next_start <= start:
                    break
                start = next_start

        return BitrixContactLookupResult(
            found=first_contact_id is not None,
            contact_id=first_contact_id,
            matches=matches,
        )

    async def resolve_contact_notification_route(
        self,
        contact_id: str,
        *,
        fallback_email: str | None,
    ) -> BitrixContactNotificationRoute:
        """Return the allowed customer notification route for a Bitrix contact."""
        email = _optional_text(fallback_email)
        if not self.allow_openline_notifications:
            if email:
                return BitrixContactNotificationRoute(channel="email", destination=email)
            return BitrixContactNotificationRoute(channel="none", error="no_email_route")

        active_chats = await self.open_line_chats_for_contact(contact_id, active_only=True)
        route = self._first_openline_route(active_chats)
        if route is not None:
            return route

        all_chats = await self.open_line_chats_for_contact(contact_id, active_only=False)
        route = self._first_openline_route(all_chats)
        if route is not None:
            return route

        if email:
            return BitrixContactNotificationRoute(channel="email", destination=email)
        return BitrixContactNotificationRoute(channel="none", error="no_openline_or_email")

    async def notify_contact(
        self,
        contact_id: str,
        *,
        fallback_email: str | None,
        subject: str,
        message: str,
    ) -> NotificationResult:
        """Notify a contact through the configured Bitrix route."""
        route = await self.resolve_contact_notification_route(
            contact_id,
            fallback_email=fallback_email,
        )
        if route.channel == "openline" and route.destination:
            openline_result = await self._send_openline_message(route.destination, message)
            if openline_result is not None:
                if not route.active:
                    await self._finish_openline_dialog(route.destination)
                return replace(
                    openline_result,
                    contact_id=contact_id,
                    contact_url=self._contact_url(contact_id),
                )

        if fallback_email:
            return await self._send_contact_email(
                contact_id,
                fallback_email=fallback_email,
                subject=subject,
                message=message,
            )
        msg = f"Bitrix contact {contact_id} has no usable Open Line or e-mail route"
        raise ValueError(msg)

    async def open_line_chats_for_contact(
        self,
        contact_id: str,
        *,
        active_only: bool,
    ) -> list[BitrixOpenLineChat]:
        """Return Open Line chats linked to a Bitrix contact."""
        normalized_contact_id = _int_or_none(contact_id)
        if normalized_contact_id is None:
            return []

        url = f"{self.webhook_base_url.rstrip('/')}/imopenlines.crm.chat.get.json"
        response = await self._post_bitrix(
            url,
            {
                "CRM_ENTITY_TYPE": "contact",
                "CRM_ENTITY": normalized_contact_id,
                "ACTIVE_ONLY": "Y" if active_only else "N",
            },
        )
        if not isinstance(response, dict) or response.get("error"):
            return []

        rows = response.get("result")
        if not isinstance(rows, list):
            return []

        chats: list[BitrixOpenLineChat] = []
        for row in rows:
            if not isinstance(row, dict):
                continue
            chat_id = _optional_text(
                row.get("CHAT_ID") or row.get("chatId") or row.get("ID") or row.get("id")
            )
            if not chat_id:
                continue
            chats.append(
                BitrixOpenLineChat(
                    chat_id=chat_id,
                    connector_id=_optional_text(
                        row.get("CONNECTOR_ID") or row.get("connectorId") or row.get("connector_id")
                    ),
                    connector_title=_optional_text(
                        row.get("CONNECTOR_TITLE")
                        or row.get("connectorTitle")
                        or row.get("connector_title")
                    ),
                    active=active_only,
                )
            )
        return chats

    async def _send_openline_message(self, chat_id: str, message: str) -> NotificationResult | None:
        url = f"{self.webhook_base_url.rstrip('/')}/imopenlines.bot.session.message.send.json"
        response = await self._post_bitrix(
            url,
            {"CHAT_ID": _int_or_none(chat_id) or chat_id, "NAME": "DEFAULT", "MESSAGE": message},
        )
        if response.get("result") is True:
            return NotificationResult(
                channel=NotificationChannel.BITRIX,
                destination=f"openline:{chat_id}",
                message_id=f"bot-session:{chat_id}",
            )
        logger.warning("Bitrix Open Line send failed for chat %s: %s", chat_id, response)
        return None

    async def _finish_openline_dialog(self, chat_id: str) -> bool:
        methods = (
            ("imopenlines.operator.finish", {"CHAT_ID": _int_or_none(chat_id) or chat_id}),
            ("imopenlines.operator.another.finish", {"CHAT_ID": _int_or_none(chat_id) or chat_id}),
            (
                "imopenlines.bot.session.finish",
                {"CHAT_ID": _int_or_none(chat_id) or chat_id, "CLIENT_ID": ""},
            ),
        )
        for method, payload in methods:
            response = await self._post_bitrix(
                f"{self.webhook_base_url.rstrip('/')}/{method}.json",
                payload,
            )
            if response.get("result"):
                return True
        logger.warning("Bitrix Open Line finish failed for chat %s", chat_id)
        return False

    async def _send_contact_email(
        self,
        contact_id: str,
        *,
        fallback_email: str,
        subject: str,
        message: str,
    ) -> NotificationResult:
        normalized_contact_id = _int_or_none(contact_id)
        if normalized_contact_id is None:
            msg = f"Bitrix contact id {contact_id!r} is not numeric"
            raise ValueError(msg)

        contact = await self._get_contact(normalized_contact_id)
        email = _first_contact_email(contact) or fallback_email
        responsible_id = _int_or_none(contact.get("ASSIGNED_BY_ID"))
        if responsible_id is None:
            msg = f"Bitrix contact {contact_id} has no assigned user"
            raise ValueError(msg)
        sender = _optional_text(self.email_from) or await self._email_sender(responsible_id)

        now = datetime.now(UTC)
        response = await self._post_bitrix(
            f"{self.webhook_base_url.rstrip('/')}/crm.activity.add.json",
            {
                "fields": {
                    "SUBJECT": subject,
                    "DESCRIPTION": message,
                    "DESCRIPTION_TYPE": 3,
                    "COMPLETED": "Y",
                    "DIRECTION": 2,
                    "OWNER_ID": normalized_contact_id,
                    "OWNER_TYPE_ID": 3,
                    "TYPE_ID": 4,
                    "COMMUNICATIONS": [
                        {
                            "VALUE": email,
                            "ENTITY_ID": normalized_contact_id,
                            "ENTITY_TYPE_ID": 3,
                        }
                    ],
                    "START_TIME": now.isoformat(timespec="seconds"),
                    "END_TIME": (now + timedelta(hours=1)).isoformat(timespec="seconds"),
                    "RESPONSIBLE_ID": responsible_id,
                    "SETTINGS": {"MESSAGE_FROM": sender},
                }
            },
        )
        activity_id = _optional_text(response.get("result"))
        if not activity_id:
            recovered_activity_id = await self._recover_sent_email_activity(
                response,
                contact_id=normalized_contact_id,
                fallback_email=email,
                subject=subject,
                message=message,
            )
            if recovered_activity_id is not None:
                return NotificationResult(
                    channel=NotificationChannel.EMAIL,
                    destination=email,
                    message_id=recovered_activity_id,
                    contact_id=contact_id,
                    contact_url=self._contact_url(contact_id),
                )
            msg = f"Bitrix e-mail activity failed for contact {contact_id}: {response}"
            raise ValueError(msg)
        return NotificationResult(
            channel=NotificationChannel.EMAIL,
            destination=email,
            message_id=activity_id,
            contact_id=contact_id,
            contact_url=self._contact_url(contact_id),
        )

    async def _recover_sent_email_activity(
        self,
        response: dict[str, object],
        *,
        contact_id: int,
        fallback_email: str,
        subject: str,
        message: str,
    ) -> str | None:
        """Return the latest matching e-mail activity after a Bitrix HTTP 500."""
        if response.get("error") != "bitrix_http_500":
            return None
        activity_id = await self._latest_matching_email_activity(
            contact_id=contact_id,
            fallback_email=fallback_email,
            subject=subject,
            message=message,
        )
        if activity_id is not None:
            logger.warning(
                "Recovered Bitrix e-mail activity %s after HTTP 500 for contact %s",
                activity_id,
                contact_id,
            )
        return activity_id

    async def _latest_matching_email_activity(
        self,
        *,
        contact_id: int,
        fallback_email: str,
        subject: str,
        message: str,
    ) -> str | None:
        response = await self._post_bitrix(
            f"{self.webhook_base_url.rstrip('/')}/crm.activity.list.json",
            {
                "filter": {
                    "OWNER_ID": contact_id,
                    "OWNER_TYPE_ID": 3,
                    "TYPE_ID": 4,
                    "DIRECTION": 2,
                },
                "select": [
                    "ID",
                    "SUBJECT",
                    "DESCRIPTION",
                    "COMMUNICATIONS",
                ],
                "order": {"ID": "DESC"},
                "start": 0,
            },
        )
        rows = response.get("result")
        if not isinstance(rows, list) or not rows:
            return None
        latest = rows[0]
        if not isinstance(latest, dict):
            return None
        if not _email_activity_matches(
            latest,
            fallback_email=fallback_email,
            subject=subject,
            message=message,
        ):
            return None
        return _optional_text(latest.get("ID"))

    def _contact_url(self, contact_id: str) -> str | None:
        portal = _bitrix_portal_url(self.webhook_base_url)
        if portal is None:
            return None
        return f"{portal}/crm/contact/details/{contact_id}/"

    async def _get_contact(self, contact_id: int) -> dict:
        response = await self._post_bitrix(
            f"{self.webhook_base_url.rstrip('/')}/crm.contact.get.json",
            {"id": contact_id},
        )
        contact = response.get("result")
        if not isinstance(contact, dict):
            msg = f"Bitrix contact {contact_id} was not returned"
            raise ValueError(msg)
        return contact

    async def _email_sender(self, responsible_id: int) -> str:
        response = await self._post_bitrix(
            f"{self.webhook_base_url.rstrip('/')}/user.get.json",
            {"filter": {"ID": responsible_id}},
        )
        rows = response.get("result")
        user = (
            next((row for row in rows if isinstance(row, dict)), None)
            if isinstance(rows, list)
            else None
        )
        if not user:
            msg = f"Bitrix user {responsible_id} was not returned"
            raise ValueError(msg)
        email = _optional_text(user.get("EMAIL"))
        if not email:
            msg = f"Bitrix user {responsible_id} has no e-mail"
            raise ValueError(msg)
        name = " ".join(
            part
            for part in (
                _optional_text(user.get("NAME")),
                _optional_text(user.get("LAST_NAME")),
            )
            if part
        )
        return f"{name} <{email}>" if name else email

    async def _post_bitrix(self, url: str, payload: dict[str, object]) -> dict[str, object]:
        async with httpx.AsyncClient(timeout=30.0) as client:
            response = await client.post(
                url,
                json=payload,
                headers={"Accept": "application/json", "Content-Type": "application/json"},
            )
        if response.status_code >= 400:
            payload: dict[str, object] = {
                "error": f"bitrix_http_{response.status_code}",
                "status_code": response.status_code,
            }
            try:
                body = response.json()
            except ValueError:
                text = _optional_text(response.text[:2000])
                if text:
                    payload["body"] = text
            else:
                payload["body"] = body
                if isinstance(body, dict):
                    if "error" in body:
                        payload["bitrix_error"] = body["error"]
                    if "error_description" in body:
                        payload["error_description"] = body["error_description"]
            return payload
        body = response.json()
        return body if isinstance(body, dict) else {"error": "bitrix_response_not_object"}

    def _first_openline_route(
        self,
        chats: list[BitrixOpenLineChat],
    ) -> BitrixContactNotificationRoute | None:
        for chat in chats:
            if self._is_excluded_openline_chat(chat):
                continue
            return BitrixContactNotificationRoute(
                channel="openline",
                destination=chat.chat_id,
                connector_id=chat.connector_id,
                connector_title=chat.connector_title,
                active=chat.active,
            )
        return None

    def _is_excluded_openline_chat(self, chat: BitrixOpenLineChat) -> bool:
        connector_id = (chat.connector_id or "").strip().casefold()
        excluded_connectors = {item.casefold() for item in self.excluded_openline_connectors}
        if connector_id in excluded_connectors:
            return True
        title = (chat.connector_title or "").strip().casefold()
        return any(term.casefold() in title for term in self.excluded_openline_title_terms)


@dataclass(frozen=True, slots=True)
class YandexDeliveryClient:
    """Yandex Delivery pickup client backed by the corporate account API."""

    base_url: str
    session_id: str
    client_id: str
    previous_session_id: str | None = None
    timezone_name: str = "Europe/Moscow"
    max_pages: int = 20
    enrich_concurrency: int = 8
    poll_attempts: int = 120
    poll_interval_seconds: float = 5.0
    waiting_status_code: str = "accepted_on_destination_point"

    async def list_waiting_pickup_orders(self, *, today: date) -> list[PickupOrder]:
        """List waiting orders with exact storage metadata from the account API."""
        del today
        last_auth_error: httpx.HTTPStatusError | None = None
        for position, session_id in enumerate(self._session_candidates()):
            try:
                return await self._list_waiting_with_session(session_id)
            except httpx.HTTPStatusError as exc:
                if exc.response.status_code not in {401, 403}:
                    raise
                last_auth_error = exc
                logger.warning(
                    "Yandex session %s was rejected with HTTP %s",
                    "primary" if position == 0 else "fallback",
                    exc.response.status_code,
                )
        if last_auth_error is not None:
            raise last_auth_error
        msg = "Yandex account session is not configured"
        raise RuntimeError(msg)

    async def _list_waiting_with_session(self, session_id: str) -> list[PickupOrder]:
        timeout = httpx.Timeout(connect=10.0, read=30.0, write=10.0, pool=10.0)
        async with httpx.AsyncClient(
            base_url=self.base_url.rstrip("/"),
            cookies={"Session_id": session_id},
            timeout=timeout,
        ) as client:
            headers = await self._session_headers(client)
            rows = await self._list_order_rows(client, headers)
            semaphore = asyncio.Semaphore(max(1, self.enrich_concurrency))
            enriched = await asyncio.gather(
                *(self._load_pickup_order(client, headers, row, semaphore) for row in rows)
            )
        return [order for order in enriched if order is not None]

    async def extend_storage(self, order: PickupOrder, *, days: int) -> ExtensionResult:
        """Extend storage to the exact maximum date offered by Yandex."""
        del days
        customer_order_id = _optional_text(order.metadata.get("customer_order_id"))
        expiration_value = _optional_text(
            order.metadata.get("max_available_expiration_date")
        )
        if not customer_order_id:
            return ExtensionResult(ok=False, error="missing_yandex_customer_order_id")
        if not expiration_value:
            return ExtensionResult(ok=False, error="yandex_extension_not_available")

        new_deadline = self._local_date(expiration_value)
        if new_deadline is None:
            return ExtensionResult(ok=False, error="invalid_yandex_extension_deadline")

        auth_errors = {"yandex_extension_http_401", "yandex_extension_http_403"}
        last_result: ExtensionResult | None = None
        for position, session_id in enumerate(self._session_candidates()):
            result = await self._extend_with_session(
                session_id,
                customer_order_id=customer_order_id,
                expiration_value=expiration_value,
                new_deadline=new_deadline,
            )
            if result.error not in auth_errors:
                return result
            last_result = result
            logger.warning(
                "Yandex session %s was rejected during extension",
                "primary" if position == 0 else "fallback",
            )
        return last_result or ExtensionResult(ok=False, error="yandex_extension_auth_failed")

    async def check_session(self) -> dict[str, object]:
        """Verify current and fallback sessions without changing any order."""
        last_status_code: int | None = None
        for position, session_id in enumerate(self._session_candidates()):
            timeout = httpx.Timeout(connect=10.0, read=20.0, write=10.0, pool=10.0)
            async with httpx.AsyncClient(
                base_url=self.base_url.rstrip("/"),
                cookies={"Session_id": session_id},
                timeout=timeout,
            ) as client:
                headers = await self._session_headers(client)
                response = await client.post(
                    "/api/b2b/dcaa/delivery/v1/udp/customer-order/list",
                    json={
                        "filters": {"product_statuses": [self.waiting_status_code]},
                        "client_timezone": self.timezone_name,
                    },
                    headers=headers,
                )
                if response.status_code < 400:
                    payload = response.json()
                    rows = payload.get("customer_orders") if isinstance(payload, dict) else None
                    return {
                        "valid": True,
                        "using_fallback": position > 0,
                        "first_page_orders": len(rows) if isinstance(rows, list) else 0,
                    }
                last_status_code = response.status_code
                if response.status_code not in {401, 403}:
                    response.raise_for_status()
        return {
            "valid": False,
            "using_fallback": False,
            "status_code": last_status_code,
        }

    async def _extend_with_session(
        self,
        session_id: str,
        *,
        customer_order_id: str,
        expiration_value: str,
        new_deadline: date,
    ) -> ExtensionResult:
        timeout = httpx.Timeout(connect=10.0, read=30.0, write=10.0, pool=10.0)
        try:
            async with httpx.AsyncClient(
                base_url=self.base_url.rstrip("/"),
                cookies={"Session_id": session_id},
                timeout=timeout,
            ) as client:
                headers = await self._session_headers(client)
                response = await client.post(
                    "/api/b2b/dcaa/delivery/v1/udp/customer-order/storage-period/edit",
                    json={
                        "storage_expiration_date": expiration_value,
                        "customer_order_id": customer_order_id,
                    },
                    headers=headers,
                )
                if response.status_code >= 400:
                    return ExtensionResult(
                        ok=False,
                        error=f"yandex_extension_http_{response.status_code}",
                    )
                payload = response.json()
                editing_request_id = _optional_text(
                    payload.get("editing_request_id") if isinstance(payload, dict) else None
                )
                if not editing_request_id:
                    return ExtensionResult(
                        ok=False,
                        error="yandex_extension_request_id_missing",
                    )
                return await self._wait_for_extension(
                    client,
                    headers,
                    editing_request_id=editing_request_id,
                    new_deadline=new_deadline,
                )
        except httpx.HTTPStatusError as exc:
            return ExtensionResult(
                ok=False,
                error=f"yandex_extension_http_{exc.response.status_code}",
            )
        except httpx.TimeoutException:
            return ExtensionResult(ok=False, error="yandex_extension_timeout")
        except httpx.NetworkError:
            return ExtensionResult(ok=False, error="yandex_extension_network_error")

    def _session_candidates(self) -> tuple[str, ...]:
        candidates = [self.session_id]
        previous = _optional_text(self.previous_session_id)
        if previous and previous != self.session_id:
            candidates.append(previous)
        return tuple(candidate for candidate in candidates if candidate)

    async def _session_headers(self, client: httpx.AsyncClient) -> dict[str, str]:
        response = await client.get("/account/api/csrf_token/")
        response.raise_for_status()
        payload = response.json()
        csrf_token = _optional_text(payload.get("sk") if isinstance(payload, dict) else None)
        if not csrf_token:
            msg = "Yandex account API did not return a CSRF token"
            raise RuntimeError(msg)
        return {
            "Accept": "application/json",
            "Accept-Language": "ru-RU",
            "Content-Type": "application/json",
            "X-B2B-Client-Id": self.client_id,
            "X-CSRF-Token": csrf_token,
        }

    async def _list_order_rows(
        self,
        client: httpx.AsyncClient,
        headers: dict[str, str],
    ) -> list[dict]:
        rows: list[dict] = []
        cursor: str | None = None
        for _page in range(max(1, self.max_pages)):
            payload: dict[str, object] = {
                "filters": {"product_statuses": [self.waiting_status_code]},
                "client_timezone": self.timezone_name,
            }
            if cursor:
                payload["cursor"] = cursor
            response = await client.post(
                "/api/b2b/dcaa/delivery/v1/udp/customer-order/list",
                json=payload,
                headers=headers,
            )
            response.raise_for_status()
            body = response.json()
            page_rows = body.get("customer_orders") if isinstance(body, dict) else None
            if not isinstance(page_rows, list):
                return rows
            rows.extend(row for row in page_rows if isinstance(row, dict))
            next_cursor = _optional_text(body.get("cursor"))
            if not next_cursor or next_cursor == cursor:
                return rows
            cursor = next_cursor
        msg = f"Yandex order list exceeded {self.max_pages} pages"
        raise RuntimeError(msg)

    async def _load_pickup_order(
        self,
        client: httpx.AsyncClient,
        headers: dict[str, str],
        row: dict,
        semaphore: asyncio.Semaphore,
    ) -> PickupOrder | None:
        customer_order_id = _optional_text(row.get("customer_order_id"))
        if not customer_order_id:
            return None
        async with semaphore:
            response = await client.post(
                "/api/b2b/dcaa/delivery/v1/udp/customer-order/details",
                params={"customer_order_id": customer_order_id},
                json={"client_timezone": self.timezone_name},
                headers=headers,
            )
            response.raise_for_status()
            details = response.json()
        if not isinstance(details, dict):
            return None
        status = _dict(details.get("status"))
        if _optional_text(status.get("status")) != self.waiting_status_code:
            return None
        return self._to_pickup_order(details)

    def _to_pickup_order(self, details: dict) -> PickupOrder:
        customer_order_id = _optional_text(details.get("customer_order_id"))
        storage_period = _dict(details.get("storage_period"))
        deadline_value = storage_period.get("current_expiration_date")
        pickup_deadline = self._local_date(deadline_value)
        if pickup_deadline is None:
            msg = f"Yandex order {customer_order_id or 'unknown'} has no storage deadline"
            raise RuntimeError(msg)

        recipient = _dict(_dict(details.get("contacts")).get("recipient"))
        status = _dict(details.get("status"))
        extension_action = _dict(
            _dict(details.get("available_actions")).get("extend_storage_period")
        )
        max_expiration = _optional_text(
            extension_action.get("max_available_expiration_date")
        )
        edit_requests = details.get("edit_requests")
        storage_edits = [
            edit
            for edit in edit_requests
            if isinstance(edit, dict)
            and edit.get("edit_type") == "destination_point_storage_expiration_date_edit"
        ] if isinstance(edit_requests, list) else []
        active_edit_statuses = {"pending", "processing", "in_progress", "success"}
        already_extended = any(
            _optional_text(edit.get("status")) in active_edit_statuses for edit in storage_edits
        )
        lookup_number = _first_text(
            details.get("client_order_id"),
            _first_operator_order_id(details.get("operator_order_ids")),
            customer_order_id,
        )
        return PickupOrder(
            external_id=lookup_number or customer_order_id or "",
            recipient_name=" ".join(
                part
                for part in (
                    _optional_text(recipient.get("last_name")),
                    _optional_text(recipient.get("first_name")),
                    _optional_text(recipient.get("middle_name")),
                )
                if part
            ),
            pickup_deadline=pickup_deadline,
            status="waiting_pickup",
            email=_optional_text(recipient.get("email")),
            already_extended=already_extended,
            metadata={
                "carrier": "yandex",
                "customer_order_id": customer_order_id,
                "lookup_number": lookup_number,
                "status_code": status.get("status"),
                "status_label": status.get("name"),
                "is_about_to_expire": storage_period.get("is_about_to_expire"),
                "max_available_expiration_date": max_expiration,
                "expiration_extension_allowed": bool(max_expiration),
                "already_extended_source": "yandex.edit_requests",
                "storage_edit_statuses": [edit.get("status") for edit in storage_edits],
                "deadline_source": "yandex.storage_period.current_expiration_date",
            },
        )

    async def _wait_for_extension(
        self,
        client: httpx.AsyncClient,
        headers: dict[str, str],
        *,
        editing_request_id: str,
        new_deadline: date,
    ) -> ExtensionResult:
        terminal_failures = {"failed", "canceled", "cancelled", "rejected"}
        for attempt in range(max(1, self.poll_attempts)):
            response = await client.post(
                "/api/b2b/dcaa/delivery/v1/udp/customer-order/edit/status",
                params={"editing_request_id": editing_request_id},
                json={},
                headers=headers,
            )
            if response.status_code >= 400:
                return ExtensionResult(
                    ok=False,
                    error=f"yandex_extension_status_http_{response.status_code}",
                    upstream_id=editing_request_id,
                )
            payload = response.json()
            status = _optional_text(payload.get("status") if isinstance(payload, dict) else None)
            if status == "success":
                return ExtensionResult(
                    ok=True,
                    new_deadline=new_deadline,
                    upstream_id=editing_request_id,
                )
            if status in terminal_failures:
                return ExtensionResult(
                    ok=False,
                    error=f"yandex_extension_failed:{status}",
                    upstream_id=editing_request_id,
                )
            if attempt + 1 < max(1, self.poll_attempts):
                await asyncio.sleep(max(0.0, self.poll_interval_seconds))
        return ExtensionResult(
            ok=False,
            error="yandex_extension_status_timeout",
            upstream_id=editing_request_id,
        )

    def _local_date(self, value: object) -> date | None:
        parsed = _parse_datetime_value(value)
        if parsed is None:
            return None
        return parsed.astimezone(ZoneInfo(self.timezone_name)).date()


@dataclass(frozen=True, slots=True)
class DryRunNotifier:
    """Notifier that records the route without sending anything."""

    async def notify(self, order: PickupOrder, *, subject: str, message: str) -> NotificationResult:
        """Pretend to notify through Bitrix when possible, otherwise email."""
        if order.bitrix_entity_id:
            return NotificationResult(
                channel=NotificationChannel.BITRIX,
                destination=order.bitrix_entity_id,
                message_id=f"dry-bitrix-{order.external_id}",
            )
        if order.email:
            return NotificationResult(
                channel=NotificationChannel.EMAIL,
                destination=order.email,
                message_id=f"dry-email-{order.external_id}",
            )
        raise ValueError(f"order {order.external_id} has no notification destination")


@dataclass(frozen=True, slots=True)
class ErpEmailCarrierClient:
    """Carrier wrapper that resolves customer e-mail and ERP flags before processing."""

    carrier: Any
    erp: Any
    notify_window_days: int = 2

    async def list_waiting_pickup_orders(self, *, today: date) -> list[PickupOrder]:
        """Return orders, enriching ERP data only when the order can be acted on."""
        list_orders = self.carrier.list_waiting_pickup_orders
        orders = await list_orders(today=today)
        enriched: list[PickupOrder] = []
        for order in orders:
            days_left = (order.pickup_deadline - today).days
            if days_left > self.notify_window_days or days_left < 0 or order.already_extended:
                enriched.append(order)
                continue

            lookup_number = str(order.metadata.get("lookup_number") or order.external_id)
            record = await self.erp.find_order(lookup_number)
            metadata = {
                **order.metadata,
                "lookup_number": lookup_number,
                "erp_found": getattr(record, "found", False),
                "erp_order_number": getattr(record, "order_number", None),
                "erp_error": getattr(record, "error", None),
            }
            already_extended = order.already_extended
            if not metadata.get("already_extended_source"):
                already_extended = bool(
                    order.already_extended or getattr(record, "already_extended", False)
                )
                if getattr(record, "already_extended", False):
                    metadata["already_extended_source"] = (
                        "erp.delivery_data.has_extend_hold_request"
                    )
            enriched.append(
                replace(
                    order,
                    email=getattr(record, "email", None) or order.email,
                    already_extended=already_extended,
                    metadata=metadata,
                )
            )
        return enriched

    async def extend_storage(self, order: PickupOrder, *, days: int) -> ExtensionResult:
        """Delegate storage extension to the wrapped carrier."""
        extend = self.carrier.extend_storage
        return await extend(order, days=days)

    def consume_listing_failures(self) -> tuple[CarrierListFailure, ...]:
        """Return carrier-level listing failures from the wrapped carrier."""
        consume = getattr(self.carrier, "consume_listing_failures", None)
        if not callable(consume):
            return ()
        failures = consume()
        return failures if isinstance(failures, tuple) else ()


@dataclass(frozen=True, slots=True)
class BitrixContactNotifier:
    """Notifier that sends through Bitrix contact routes."""

    bitrix: BitrixContactClient

    async def notify(self, order: PickupOrder, *, subject: str, message: str) -> NotificationResult:
        """Find a Bitrix contact by e-mail and notify it."""
        if not order.email:
            msg = f"order {order.external_id} has no customer e-mail for Bitrix lookup"
            raise ValueError(msg)

        contact = await self.bitrix.find_contact_by_email(order.email)
        if not contact.found or not contact.contact_id:
            msg = f"Bitrix contact was not found for order {order.external_id}: {contact.error}"
            raise ValueError(msg)

        return await self.bitrix.notify_contact(
            contact.contact_id,
            fallback_email=order.email,
            subject=subject,
            message=message,
        )


@dataclass(frozen=True, slots=True)
class DryRunOperatorTasks:
    """Operator task adapter that does not persist anything."""

    async def create_task(self, order: PickupOrder, *, reason: str) -> str:
        """Pretend to create an operator task."""
        return f"dry-task-{order.external_id}-{reason}"


def _dict(value: object) -> dict:
    return value if isinstance(value, dict) else {}


def _carrier_name(carrier: Any) -> str:
    if isinstance(carrier, FivePostClient):
        return "fivepost"
    if isinstance(carrier, YandexDeliveryClient):
        return "yandex"
    if isinstance(carrier, SafeRouteClient):
        return "saferoute"
    return _optional_text(getattr(carrier, "name", None)) or carrier.__class__.__name__.lower()


def _carrier_list_failure_reason(exc: Exception) -> str:
    if isinstance(exc, httpx.HTTPStatusError):
        status_code = exc.response.status_code
        if status_code in {401, 403}:
            return f"carrier_auth_failed:{status_code}"
        return f"carrier_http_failed:{status_code}"
    if isinstance(exc, httpx.TimeoutException):
        return "carrier_timeout"
    if isinstance(exc, httpx.NetworkError):
        return "carrier_network_error"
    return f"carrier_fetch_failed:{type(exc).__name__}"


def _latest_status(row: dict) -> dict:
    history = row.get("statusHistory") or row.get("status_history") or []
    if isinstance(history, list) and history and isinstance(history[0], dict):
        return history[0]
    return {}


def _latest_fivepost_history(payload: dict[str, object]) -> dict:
    history = payload.get("history") if isinstance(payload.get("history"), list) else []
    return next((item for item in reversed(history) if isinstance(item, dict)), {})


def _fivepost_extension_deadline(payload: object) -> date | None:
    if not isinstance(payload, dict):
        return None
    for key in (
        "expiredDate",
        "expirationDate",
        "newExpirationDate",
        "newExpiredDate",
        "storageDeadline",
    ):
        parsed = _parse_datetime_value(payload.get(key))
        if parsed is not None:
            return parsed.date()
    result = payload.get("result")
    if isinstance(result, dict):
        return _fivepost_extension_deadline(result)
    return None


def _status_code(source: dict) -> str | None:
    for key in ("executionStatus", "status", "code", "key"):
        value = source.get(key)
        if value is not None and str(value).strip():
            return str(value).strip()
    return None


def _int_or_none(value: object) -> int | None:
    if value is None or value == "":
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _parse_iso_date(value: object) -> date | None:
    if not value:
        return None
    try:
        return date.fromisoformat(str(value))
    except ValueError:
        return None


def _parse_saferoute_datetime(value: str) -> datetime | None:
    if not value:
        return None
    try:
        return _parse_datetime_value(value)
    except ValueError:
        return None


def _parse_datetime_value(value: object) -> datetime | None:
    if value is None or value == "":
        return None
    if isinstance(value, int | float):
        return datetime.fromtimestamp(value, tz=UTC)
    raw = str(value).strip()
    if not raw:
        return None
    normalized = raw.replace(" ", "T")
    if normalized.endswith("Z"):
        normalized = f"{normalized[:-1]}+00:00"
    try:
        return datetime.fromisoformat(normalized)
    except ValueError:
        return None


def _first_text(*values: object) -> str | None:
    for value in values:
        text = _optional_text(value)
        if text:
            return text
    return None


def _first_operator_order_id(value: object) -> str | None:
    if not isinstance(value, list):
        return None
    for row in value:
        if isinstance(row, dict):
            order_id = _optional_text(row.get("operator_order_id"))
            if order_id:
                return order_id
    return None


def _optional_text(value: object) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _first_contact_email(contact: dict) -> str | None:
    values = contact.get("EMAIL")
    if not isinstance(values, list):
        return None
    for value in values:
        if isinstance(value, dict):
            email = _optional_text(value.get("VALUE"))
            if email:
                return email
    return None


def _email_activity_matches(
    activity: dict[str, object],
    *,
    fallback_email: str,
    subject: str,
    message: str,
) -> bool:
    if _optional_text(activity.get("SUBJECT")) != subject:
        return False
    description = _optional_text(activity.get("DESCRIPTION")) or ""
    if _normalize_message_text(message) not in _normalize_message_text(description):
        return False
    communications = activity.get("COMMUNICATIONS")
    if not isinstance(communications, list):
        return True
    expected_email = fallback_email.strip().casefold()
    return any(
        isinstance(row, dict)
        and (_optional_text(row.get("VALUE")) or "").casefold() == expected_email
        for row in communications
    )


def _normalize_message_text(value: str) -> str:
    return " ".join(value.split())


def _bitrix_portal_url(webhook_base_url: str) -> str | None:
    portal, marker, _rest = webhook_base_url.rstrip("/").partition("/rest/")
    if marker and portal:
        return portal
    return None


def _optional_bool(value: object) -> bool | None:
    if isinstance(value, bool):
        return value
    if isinstance(value, int | float):
        return value != 0
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in {"1", "true", "yes", "y", "да"}:
            return True
        if normalized in {"0", "false", "no", "n", "нет"}:
            return False
    return None
