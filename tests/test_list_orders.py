"""Tests for order-listing entrypoints."""

from __future__ import annotations

from datetime import date
from typing import TYPE_CHECKING

import httpx
from unclaimed_orders_service.adapters import (
    BitrixContactClient,
    BitrixContactLookupResult,
    BitrixContactNotifier,
    FivePostClient,
    SafeRouteClient,
    YandexDeliveryClient,
)
from unclaimed_orders_service.domain import NotificationChannel, NotificationResult, PickupOrder
from unclaimed_orders_service.list_orders import _list_orders

if TYPE_CHECKING:
    import pytest


async def test_list_orders_cli_payload_uses_demo_source() -> None:
    payload = await _list_orders(source="demo", today=date(2026, 7, 4))

    assert payload["source"] == "demo"
    assert payload["today"] == "2026-07-04"
    assert [order["external_id"] for order in payload["orders"]] == [
        "MAGNIT-001",
        "MAGNIT-002",
    ]


async def test_saferoute_client_enriches_waiting_magnit_orders(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = SafeRouteClient(base_url="https://example.test", email="user", password="pass")
    monkeypatch.setattr(SafeRouteClient, "_login", _fake_login)

    class FakeHttpClient:
        def __init__(self, *args: object, **kwargs: object) -> None:
            return None

        async def __aenter__(self) -> FakeHttpClient:
            return self

        async def __aexit__(self, *args: object) -> None:
            return None

        async def get(self, path: str, **kwargs: object) -> FakeResponse:
            if path == "/v2/orders":
                params = kwargs.get("params")
                if isinstance(params, dict) and params.get("page") != 1:
                    return FakeResponse([])
                return FakeResponse(
                    [
                        {
                            "id": 10,
                            "cmsId": "site-10",
                            "trackNumber": "track-10",
                            "delivery": {
                                "company": {"name": "Магнит Пост"},
                                "date": {"to": "2026-07-04"},
                            },
                            "recipient": {
                                "fullName": "Ирина",
                                "email": "client@example.com",
                            },
                            "statusHistory": [{"code": 42, "date": "2026-07-04T13:35:41+0300"}],
                        },
                        {
                            "id": 11,
                            "cmsId": "site-11",
                            "delivery": {"company": {"name": "Магнит Пост"}},
                            "statusHistory": [{"code": 44, "date": "2026-07-04T13:35:41+0300"}],
                        },
                    ]
                )
            if path == "/v2/tracking":
                return FakeResponse({"delivery": {"holdDays": 5}})
            raise AssertionError(path)

    monkeypatch.setattr("unclaimed_orders_service.adapters.httpx.AsyncClient", FakeHttpClient)

    orders = await client.list_waiting_pickup_orders(today=date(2026, 7, 4))

    assert len(orders) == 1
    assert orders[0].external_id == "site-10"
    assert orders[0].pickup_deadline == date(2026, 7, 9)
    assert orders[0].metadata["hold_days"] == 5
    assert orders[0].metadata["deadline_source"] == "status_date_plus_hold_days"


async def test_saferoute_client_survives_bad_tracking_order(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = SafeRouteClient(base_url="https://example.test", email="user", password="pass")
    monkeypatch.setattr(SafeRouteClient, "_login", _fake_login)

    class FakeHttpClient:
        def __init__(self, *args: object, **kwargs: object) -> None:
            return None

        async def __aenter__(self) -> FakeHttpClient:
            return self

        async def __aexit__(self, *args: object) -> None:
            return None

        async def get(self, path: str, **kwargs: object) -> object:
            if path == "/v2/orders":
                params = kwargs.get("params")
                if isinstance(params, dict) and params.get("page") != 1:
                    return FakeResponse([])
                return FakeResponse(
                    [
                        {
                            "id": 10,
                            "cmsId": "site-10",
                            "delivery": {
                                "company": {"name": "Магнит Пост"},
                                "date": {"to": "2026-07-11"},
                            },
                            "recipient": {"fullName": "Ирина"},
                            "statusHistory": [{"code": 42, "date": "2026-07-04T13:35:41+0300"}],
                        },
                        {
                            "id": 12,
                            "cmsId": "site-12",
                            "delivery": {"company": {"name": "Магнит Пост"}},
                            "recipient": {"fullName": "Олег"},
                            "statusHistory": [{"code": 42, "date": "2026-07-04T13:35:41+0300"}],
                        },
                    ]
                )
            if path == "/v2/tracking":
                return FakeErrorResponse()
            raise AssertionError(path)

    monkeypatch.setattr("unclaimed_orders_service.adapters.httpx.AsyncClient", FakeHttpClient)

    orders = await client.list_waiting_pickup_orders(today=date(2026, 7, 4))

    assert len(orders) == 1
    assert orders[0].external_id == "site-10"
    assert orders[0].pickup_deadline == date(2026, 7, 11)
    assert orders[0].metadata["deadline_source"] == "delivery_date_to"


async def test_fivepost_client_lists_waiting_orders_with_expired_date(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = FivePostClient(base_url="https://fivepost.test", login="user", password="pass")
    query_payloads: list[object] = []

    class FakeHttpClient:
        def __init__(self, *args: object, **kwargs: object) -> None:
            return None

        async def __aenter__(self) -> FakeHttpClient:
            return self

        async def __aexit__(self, *args: object) -> None:
            return None

        async def post(self, path: str, **kwargs: object) -> FakeResponse:
            if path == "/partners-portal-auth/api/v2/auth":
                return FakeResponse({"jwt": "token"})
            if path == "/partners-portal/api/v1/orders/query":
                query_payloads.append(kwargs.get("json"))
                return FakeResponse(
                    {
                        "last": True,
                        "content": [
                            {
                                "orderId": "fivepost-1",
                                "senderOrderId": "430356FPerp",
                                "clientOrderId": "430356FPerp",
                                "executionStatus": "PLACED_IN_POSTAMAT",
                                "statusAssignmentDate": "2026-07-05T23:05:38Z",
                            },
                            {
                                "orderId": "fivepost-2",
                                "executionStatus": "APPROVED",
                            },
                        ],
                    }
                )
            raise AssertionError(path)

        async def get(self, path: str, **kwargs: object) -> FakeResponse:
            if path == "/partners-portal/api/v1/order/fivepost-1":
                return FakeResponse(
                    {
                        "orderId": "fivepost-1",
                        "senderOrderId": "430356FPerp",
                        "receiverClientName": "Алина",
                        "receiverClientEmail": "client@example.com",
                        "executionStatus": "PLACED_IN_POSTAMAT",
                        "expiredDate": "2026-07-20T23:59:59+03:00",
                        "expirationDateExtensionAllowed": True,
                    }
                )
            if path in {
                "/partners-portal/api/v1/order/fivepost-1/cargoes",
                "/partners-portal/api/v1/orders/fivepost-1/history-statuses",
            }:
                return FakeResponse([])
            raise AssertionError(path)

    monkeypatch.setattr("unclaimed_orders_service.adapters.httpx.AsyncClient", FakeHttpClient)

    orders = await client.list_waiting_pickup_orders(today=date(2026, 7, 6))

    assert len(orders) == 1
    assert orders[0].external_id == "430356FPerp"
    assert orders[0].pickup_deadline == date(2026, 7, 20)
    assert orders[0].already_extended is False
    assert orders[0].metadata["deadline_source"] == "details.expiredDate"
    assert orders[0].metadata["expiration_extension_allowed"] is True
    assert orders[0].metadata["already_extended_source"] == (
        "fivepost.expirationDateExtensionAllowed"
    )
    assert query_payloads == [
        {
            "orderType": None,
            "executionStatusList": ["RECEIVED_IN_STORE", "PLACED_IN_POSTAMAT"],
        }
    ]


async def test_fivepost_client_marks_extension_disallowed_order_as_already_extended(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = FivePostClient(base_url="https://fivepost.test", login="user", password="pass")

    class FakeHttpClient:
        def __init__(self, *args: object, **kwargs: object) -> None:
            return None

        async def __aenter__(self) -> FakeHttpClient:
            return self

        async def __aexit__(self, *args: object) -> None:
            return None

        async def post(self, path: str, **kwargs: object) -> FakeResponse:
            if path == "/partners-portal-auth/api/v2/auth":
                return FakeResponse({"jwt": "token"})
            if path == "/partners-portal/api/v1/orders/query":
                return FakeResponse(
                    {
                        "last": True,
                        "content": [
                            {
                                "orderId": "fivepost-1",
                                "senderOrderId": "430356FPerp",
                                "executionStatus": "RECEIVED_IN_STORE",
                            }
                        ],
                    }
                )
            raise AssertionError(path)

        async def get(self, path: str, **kwargs: object) -> FakeResponse:
            if path == "/partners-portal/api/v1/order/fivepost-1":
                return FakeResponse(
                    {
                        "orderId": "fivepost-1",
                        "senderOrderId": "430356FPerp",
                        "receiverClientName": "Алина",
                        "executionStatus": "RECEIVED_IN_STORE",
                        "expiredDate": "2026-07-20T23:59:59+03:00",
                        "expirationDateExtensionAllowed": False,
                    }
                )
            if path in {
                "/partners-portal/api/v1/order/fivepost-1/cargoes",
                "/partners-portal/api/v1/orders/fivepost-1/history-statuses",
            }:
                return FakeResponse([])
            raise AssertionError(path)

    monkeypatch.setattr("unclaimed_orders_service.adapters.httpx.AsyncClient", FakeHttpClient)

    orders = await client.list_waiting_pickup_orders(today=date(2026, 7, 6))

    assert len(orders) == 1
    assert orders[0].already_extended is True
    assert orders[0].metadata["expiration_extension_allowed"] is False
    assert orders[0].metadata["already_extended_source"] == (
        "fivepost.expirationDateExtensionAllowed"
    )


async def test_fivepost_client_retries_timed_out_detail_request(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = FivePostClient(
        base_url="https://fivepost.test",
        login="user",
        password="pass",
        enrich_retries=2,
    )
    detail_attempts = 0

    async def fake_sleep(delay: float) -> None:
        return None

    class FakeHttpClient:
        def __init__(self, *args: object, **kwargs: object) -> None:
            return None

        async def __aenter__(self) -> FakeHttpClient:
            return self

        async def __aexit__(self, *args: object) -> None:
            return None

        async def post(self, path: str, **kwargs: object) -> FakeResponse:
            if path == "/partners-portal-auth/api/v2/auth":
                return FakeResponse({"jwt": "token"})
            if path == "/partners-portal/api/v1/orders/query":
                return FakeResponse(
                    {
                        "last": True,
                        "content": [
                            {
                                "orderId": "fivepost-1",
                                "senderOrderId": "430356FPerp",
                                "executionStatus": "PLACED_IN_POSTAMAT",
                            }
                        ],
                    }
                )
            raise AssertionError(path)

        async def get(self, path: str, **kwargs: object) -> FakeResponse:
            nonlocal detail_attempts
            if path == "/partners-portal/api/v1/order/fivepost-1":
                detail_attempts += 1
                if detail_attempts == 1:
                    request = httpx.Request("GET", "https://fivepost.test/order")
                    raise httpx.ReadTimeout("timed out", request=request)
                return FakeResponse(
                    {
                        "orderId": "fivepost-1",
                        "senderOrderId": "430356FPerp",
                        "receiverClientName": "Алина",
                        "executionStatus": "PLACED_IN_POSTAMAT",
                        "expiredDate": "2026-07-20T23:59:59+03:00",
                        "expirationDateExtensionAllowed": True,
                    }
                )
            if path in {
                "/partners-portal/api/v1/order/fivepost-1/cargoes",
                "/partners-portal/api/v1/orders/fivepost-1/history-statuses",
            }:
                return FakeResponse([])
            raise AssertionError(path)

    monkeypatch.setattr("unclaimed_orders_service.adapters.asyncio.sleep", fake_sleep)
    monkeypatch.setattr("unclaimed_orders_service.adapters.httpx.AsyncClient", FakeHttpClient)

    orders = await client.list_waiting_pickup_orders(today=date(2026, 7, 6))

    assert detail_attempts == 2
    assert len(orders) == 1
    assert orders[0].external_id == "430356FPerp"


async def test_fivepost_client_reads_all_filtered_pages(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = FivePostClient(base_url="https://fivepost.test", login="user", password="pass")
    queried_pages: list[int] = []

    class FakeHttpClient:
        def __init__(self, *args: object, **kwargs: object) -> None:
            return None

        async def __aenter__(self) -> FakeHttpClient:
            return self

        async def __aexit__(self, *args: object) -> None:
            return None

        async def post(self, path: str, **kwargs: object) -> FakeResponse:
            if path == "/partners-portal-auth/api/v2/auth":
                return FakeResponse({"jwt": "token"})
            if path == "/partners-portal/api/v1/orders/query":
                params = kwargs.get("params")
                payload = kwargs.get("json")
                assert payload == {
                    "orderType": None,
                    "executionStatusList": ["RECEIVED_IN_STORE", "PLACED_IN_POSTAMAT"],
                }
                assert isinstance(params, dict)
                page = int(params["page"])
                queried_pages.append(page)
                if page == 0:
                    return FakeResponse(
                        {
                            "last": False,
                            "content": [
                                {
                                    "orderId": "fivepost-1",
                                    "senderOrderId": "430356FPerp",
                                    "executionStatus": "PLACED_IN_POSTAMAT",
                                }
                            ],
                        }
                    )
                if page == 1:
                    return FakeResponse(
                        {
                            "last": True,
                            "content": [
                                {
                                    "orderId": "fivepost-2",
                                    "senderOrderId": "430357FPerp",
                                    "executionStatus": "PLACED_IN_POSTAMAT",
                                }
                            ],
                        }
                    )
                raise AssertionError(f"unexpected page {page}")
            raise AssertionError(path)

        async def get(self, path: str, **kwargs: object) -> FakeResponse:
            if path == "/partners-portal/api/v1/order/fivepost-1":
                return FakeResponse(
                    {
                        "orderId": "fivepost-1",
                        "senderOrderId": "430356FPerp",
                        "receiverClientName": "Алина",
                        "executionStatus": "PLACED_IN_POSTAMAT",
                        "expiredDate": "2026-07-20T23:59:59+03:00",
                    }
                )
            if path == "/partners-portal/api/v1/order/fivepost-2":
                return FakeResponse(
                    {
                        "orderId": "fivepost-2",
                        "senderOrderId": "430357FPerp",
                        "receiverClientName": "Олег",
                        "executionStatus": "PLACED_IN_POSTAMAT",
                        "expiredDate": "2026-07-21T23:59:59+03:00",
                    }
                )
            if path in {
                "/partners-portal/api/v1/order/fivepost-1/cargoes",
                "/partners-portal/api/v1/order/fivepost-2/cargoes",
                "/partners-portal/api/v1/orders/fivepost-1/history-statuses",
                "/partners-portal/api/v1/orders/fivepost-2/history-statuses",
            }:
                return FakeResponse([])
            raise AssertionError(path)

    monkeypatch.setattr("unclaimed_orders_service.adapters.httpx.AsyncClient", FakeHttpClient)

    orders = await client.list_waiting_pickup_orders(today=date(2026, 7, 6))

    assert queried_pages == [0, 1]
    assert [order.external_id for order in orders] == ["430356FPerp", "430357FPerp"]


async def test_fivepost_client_extends_storage_with_partner_endpoint(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = FivePostClient(base_url="https://fivepost.test", login="user", password="pass")
    seen: list[tuple[str, object]] = []

    async def fake_login(self: FivePostClient) -> str:
        return "token"

    class FakeHttpClient:
        def __init__(self, *args: object, **kwargs: object) -> None:
            return None

        async def __aenter__(self) -> FakeHttpClient:
            return self

        async def __aexit__(self, *args: object) -> None:
            return None

        async def put(self, path: str, **kwargs: object) -> FakeResponse:
            seen.append((path, kwargs.get("json")))
            return FakeResponse({"newExpirationDate": "2026-07-15T23:59:59+03:00"})

    monkeypatch.setattr(FivePostClient, "_login", fake_login)
    monkeypatch.setattr("unclaimed_orders_service.adapters.httpx.AsyncClient", FakeHttpClient)

    result = await client.extend_storage(
        PickupOrder(
            external_id="430356FPerp",
            recipient_name="Алина",
            pickup_deadline=date(2026, 7, 10),
            status="waiting_pickup",
            metadata={"order_id": "550e8400-e29b-41d4-a716-446655440000"},
        ),
        days=5,
    )

    assert result.ok is True
    assert result.new_deadline == date(2026, 7, 15)
    assert result.upstream_id == "550e8400-e29b-41d4-a716-446655440000"
    assert seen == [
        (
            "/partners-portal/api/v1/orders/550e8400-e29b-41d4-a716-446655440000/extend-expiration-date",
            {},
        )
    ]


async def test_bitrix_contact_client_finds_contact_by_email(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    requests: list[dict[str, object]] = []

    class FakeHttpClient:
        def __init__(self, *args: object, **kwargs: object) -> None:
            return None

        async def __aenter__(self) -> FakeHttpClient:
            return self

        async def __aexit__(self, *args: object) -> None:
            return None

        async def post(self, url: str, **kwargs: object) -> FakeResponse:
            requests.append({"url": url, "json": kwargs.get("json")})
            return FakeResponse({"result": [{"ID": "123", "NAME": "Client"}]})

    monkeypatch.setattr("unclaimed_orders_service.adapters.httpx.AsyncClient", FakeHttpClient)

    client = BitrixContactClient(webhook_base_url="https://bitrix.test/rest/1/token")
    result = await client.find_contact_by_email("client@example.com")

    assert result.found is True
    assert result.contact_id == "123"
    assert result.matches == 1
    assert requests == [
        {
            "url": "https://bitrix.test/rest/1/token/crm.contact.list.json",
            "json": {
                "filter": {"EMAIL": "client@example.com"},
                "select": ["ID", "NAME", "LAST_NAME", "EMAIL", "PHONE", "IM"],
                "order": {"ID": "ASC"},
                "start": 0,
            },
        }
    ]


async def test_bitrix_contact_client_reads_next_page(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    starts: list[int] = []

    class FakeHttpClient:
        def __init__(self, *args: object, **kwargs: object) -> None:
            return None

        async def __aenter__(self) -> FakeHttpClient:
            return self

        async def __aexit__(self, *args: object) -> None:
            return None

        async def post(self, url: str, **kwargs: object) -> FakeResponse:
            payload = kwargs.get("json")
            assert isinstance(payload, dict)
            start = int(payload["start"])
            starts.append(start)
            if start == 0:
                return FakeResponse({"result": [], "next": 50})
            return FakeResponse({"result": [{"ID": "456", "NAME": "Client"}]})

    monkeypatch.setattr("unclaimed_orders_service.adapters.httpx.AsyncClient", FakeHttpClient)

    client = BitrixContactClient(webhook_base_url="https://bitrix.test/rest/1/token")
    result = await client.find_contact_by_email("client@example.com")

    assert starts == [0, 50]
    assert result.contact_id == "456"


async def test_bitrix_contact_client_prefers_non_web_openline(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    requests: list[dict[str, object]] = []

    class FakeHttpClient:
        def __init__(self, *args: object, **kwargs: object) -> None:
            return None

        async def __aenter__(self) -> FakeHttpClient:
            return self

        async def __aexit__(self, *args: object) -> None:
            return None

        async def post(self, url: str, **kwargs: object) -> FakeResponse:
            requests.append({"url": url, "json": kwargs.get("json")})
            return FakeResponse(
                {
                    "result": [
                        {
                            "CHAT_ID": "web-1",
                            "CONNECTOR_ID": "integracio_chat",
                            "CONNECTOR_TITLE": "Онлайн-чат",
                        },
                        {
                            "CHAT_ID": "tg-1",
                            "CONNECTOR_ID": "telegrambot",
                            "CONNECTOR_TITLE": "Telegram",
                        },
                    ]
                }
            )

    monkeypatch.setattr("unclaimed_orders_service.adapters.httpx.AsyncClient", FakeHttpClient)

    client = BitrixContactClient(webhook_base_url="https://bitrix.test/rest/1/token")
    route = await client.resolve_contact_notification_route(
        "123",
        fallback_email="client@example.com",
    )

    assert route.channel == "openline"
    assert route.destination == "tg-1"
    assert route.connector_id == "telegrambot"
    assert route.connector_title == "Telegram"
    assert route.active is True
    assert requests == [
        {
            "url": "https://bitrix.test/rest/1/token/imopenlines.crm.chat.get.json",
            "json": {
                "CRM_ENTITY_TYPE": "contact",
                "CRM_ENTITY": 123,
                "ACTIVE_ONLY": "Y",
            },
        }
    ]


async def test_bitrix_contact_client_can_disable_openline_lookup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    requests: list[dict[str, object]] = []

    class FakeHttpClient:
        def __init__(self, *args: object, **kwargs: object) -> None:
            return None

        async def __aenter__(self) -> FakeHttpClient:
            return self

        async def __aexit__(self, *args: object) -> None:
            return None

        async def post(self, url: str, **kwargs: object) -> FakeResponse:
            requests.append({"url": url, "json": kwargs.get("json")})
            return FakeResponse({"result": [{"CHAT_ID": "tg-1", "CONNECTOR_ID": "telegrambot"}]})

    monkeypatch.setattr("unclaimed_orders_service.adapters.httpx.AsyncClient", FakeHttpClient)

    client = BitrixContactClient(
        webhook_base_url="https://bitrix.test/rest/1/token",
        allow_openline_notifications=False,
    )
    route = await client.resolve_contact_notification_route(
        "123",
        fallback_email="client@example.com",
    )

    assert requests == []
    assert route.channel == "email"
    assert route.destination == "client@example.com"


async def test_bitrix_contact_client_falls_back_to_email_without_allowed_openline(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    active_only_values: list[str] = []

    class FakeHttpClient:
        def __init__(self, *args: object, **kwargs: object) -> None:
            return None

        async def __aenter__(self) -> FakeHttpClient:
            return self

        async def __aexit__(self, *args: object) -> None:
            return None

        async def post(self, url: str, **kwargs: object) -> FakeResponse:
            payload = kwargs.get("json")
            assert isinstance(payload, dict)
            active_only_values.append(str(payload["ACTIVE_ONLY"]))
            return FakeResponse(
                {
                    "result": [
                        {
                            "CHAT_ID": "web-1",
                            "CONNECTOR_ID": "custom",
                            "CONNECTOR_TITLE": "Онлайн-чат",
                        }
                    ]
                }
            )

    monkeypatch.setattr("unclaimed_orders_service.adapters.httpx.AsyncClient", FakeHttpClient)

    client = BitrixContactClient(webhook_base_url="https://bitrix.test/rest/1/token")
    route = await client.resolve_contact_notification_route(
        "123",
        fallback_email="client@example.com",
    )

    assert active_only_values == ["Y", "N"]
    assert route.channel == "email"
    assert route.destination == "client@example.com"


async def test_bitrix_contact_client_notifies_openline_first(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str] = []

    class FakeHttpClient:
        def __init__(self, *args: object, **kwargs: object) -> None:
            return None

        async def __aenter__(self) -> FakeHttpClient:
            return self

        async def __aexit__(self, *args: object) -> None:
            return None

        async def post(self, url: str, **kwargs: object) -> FakeResponse:
            calls.append(url.rsplit("/", maxsplit=1)[-1])
            if url.endswith("/imopenlines.crm.chat.get.json"):
                return FakeResponse(
                    {"result": [{"CHAT_ID": "tg-1", "CONNECTOR_ID": "telegrambot"}]}
                )
            if url.endswith("/imopenlines.bot.session.message.send.json"):
                return FakeResponse({"result": True})
            raise AssertionError(f"unexpected Bitrix method: {url}")

    monkeypatch.setattr("unclaimed_orders_service.adapters.httpx.AsyncClient", FakeHttpClient)

    client = BitrixContactClient(webhook_base_url="https://bitrix.test/rest/1/token")
    result = await client.notify_contact(
        "123",
        fallback_email="client@example.com",
        subject="Subject",
        message="Message",
    )

    assert result.channel is NotificationChannel.BITRIX
    assert result.destination == "openline:tg-1"
    assert calls == [
        "imopenlines.crm.chat.get.json",
        "imopenlines.bot.session.message.send.json",
    ]


async def test_bitrix_contact_client_closes_opened_inactive_openline(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str] = []

    class FakeHttpClient:
        def __init__(self, *args: object, **kwargs: object) -> None:
            return None

        async def __aenter__(self) -> FakeHttpClient:
            return self

        async def __aexit__(self, *args: object) -> None:
            return None

        async def post(self, url: str, **kwargs: object) -> FakeResponse:
            calls.append(url.rsplit("/", maxsplit=1)[-1])
            payload = kwargs.get("json")
            if url.endswith("/imopenlines.crm.chat.get.json"):
                assert isinstance(payload, dict)
                if payload["ACTIVE_ONLY"] == "Y":
                    return FakeResponse({"result": []})
                return FakeResponse(
                    {"result": [{"CHAT_ID": "tg-1", "CONNECTOR_ID": "telegrambot"}]}
                )
            if url.endswith("/imopenlines.bot.session.message.send.json"):
                return FakeResponse({"result": True})
            if url.endswith("/imopenlines.operator.finish.json"):
                return FakeResponse({"result": True})
            raise AssertionError(f"unexpected Bitrix method: {url}")

    monkeypatch.setattr("unclaimed_orders_service.adapters.httpx.AsyncClient", FakeHttpClient)

    client = BitrixContactClient(webhook_base_url="https://bitrix.test/rest/1/token")
    result = await client.notify_contact(
        "123",
        fallback_email="client@example.com",
        subject="Subject",
        message="Message",
    )

    assert result.channel is NotificationChannel.BITRIX
    assert result.destination == "openline:tg-1"
    assert calls == [
        "imopenlines.crm.chat.get.json",
        "imopenlines.crm.chat.get.json",
        "imopenlines.bot.session.message.send.json",
        "imopenlines.operator.finish.json",
    ]


async def test_bitrix_contact_client_notifies_email_for_online_chat(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    activity_payloads: list[dict[str, object]] = []
    calls: list[str] = []

    class FakeHttpClient:
        def __init__(self, *args: object, **kwargs: object) -> None:
            return None

        async def __aenter__(self) -> FakeHttpClient:
            return self

        async def __aexit__(self, *args: object) -> None:
            return None

        async def post(self, url: str, **kwargs: object) -> FakeResponse:
            calls.append(url.rsplit("/", maxsplit=1)[-1])
            payload = kwargs.get("json")
            if url.endswith("/imopenlines.crm.chat.get.json"):
                return FakeResponse(
                    {
                        "result": [
                            {
                                "CHAT_ID": "web-1",
                                "CONNECTOR_ID": "livechat",
                                "CONNECTOR_TITLE": "Онлайн-чат",
                            }
                        ]
                    }
                )
            if url.endswith("/crm.contact.get.json"):
                return FakeResponse(
                    {
                        "result": {
                            "ID": "123",
                            "ASSIGNED_BY_ID": "42",
                            "EMAIL": [{"VALUE": "client@example.com", "VALUE_TYPE": "WORK"}],
                        }
                    }
                )
            if url.endswith("/crm.activity.add.json"):
                assert isinstance(payload, dict)
                activity_payloads.append(payload)
                return FakeResponse({"result": 3165})
            raise AssertionError(f"unexpected Bitrix method: {url}")

    monkeypatch.setattr("unclaimed_orders_service.adapters.httpx.AsyncClient", FakeHttpClient)

    client = BitrixContactClient(
        webhook_base_url="https://bitrix.test/rest/1/token",
        email_from="Support <support@example.com>",
    )
    result = await client.notify_contact(
        "123",
        fallback_email="client@example.com",
        subject="Subject",
        message="Message",
    )

    assert result.channel is NotificationChannel.EMAIL
    assert result.destination == "client@example.com"
    assert result.message_id == "3165"
    assert "imopenlines.bot.session.message.send.json" not in calls
    assert "user.get.json" not in calls
    fields = activity_payloads[0]["fields"]
    assert isinstance(fields, dict)
    assert fields["TYPE_ID"] == 4
    assert fields["DIRECTION"] == 2
    assert fields["DESCRIPTION_TYPE"] == 3
    assert fields["OWNER_TYPE_ID"] == 3
    assert fields["COMMUNICATIONS"] == [
        {"VALUE": "client@example.com", "ENTITY_ID": 123, "ENTITY_TYPE_ID": 3}
    ]
    assert fields["SETTINGS"] == {"MESSAGE_FROM": "Support <support@example.com>"}


async def test_bitrix_contact_client_preserves_http_error_body(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FakeHttpClient:
        def __init__(self, *args: object, **kwargs: object) -> None:
            return None

        async def __aenter__(self) -> FakeHttpClient:
            return self

        async def __aexit__(self, *args: object) -> None:
            return None

        async def post(self, url: str, **kwargs: object) -> object:
            return FakeBitrixErrorResponse()

    class FakeBitrixErrorResponse:
        status_code = 500
        text = '{"error":"INTERNAL_SERVER_ERROR"}'

        def json(self) -> object:
            return {
                "error": "INTERNAL_SERVER_ERROR",
                "error_description": "Email send error. \"From\" is not found",
            }

    monkeypatch.setattr("unclaimed_orders_service.adapters.httpx.AsyncClient", FakeHttpClient)

    client = BitrixContactClient(webhook_base_url="https://bitrix.test/rest/1/token")

    response = await client._post_bitrix(
        "https://bitrix.test/rest/1/token/crm.activity.add.json",
        {},
    )

    assert response["error"] == "bitrix_http_500"
    assert response["status_code"] == 500
    assert response["bitrix_error"] == "INTERNAL_SERVER_ERROR"
    assert response["error_description"] == 'Email send error. "From" is not found'


async def test_bitrix_contact_notifier_finds_contact_and_notifies_route() -> None:
    class FakeBitrix:
        async def find_contact_by_email(self, email: str) -> BitrixContactLookupResult:
            assert email == "client@example.com"
            return BitrixContactLookupResult(found=True, contact_id="123", matches=1)

        async def notify_contact(
            self,
            contact_id: str,
            *,
            fallback_email: str | None,
            subject: str,
            message: str,
        ) -> NotificationResult:
            assert contact_id == "123"
            assert fallback_email == "client@example.com"
            assert subject == "Subject"
            assert message == "Message"
            return NotificationResult(
                channel=NotificationChannel.BITRIX,
                destination="openline:tg-1",
                message_id="message-1",
            )

    order = PickupOrder(
        external_id="order-1",
        recipient_name="Client",
        pickup_deadline=date(2026, 7, 10),
        status="waiting_pickup",
        email="client@example.com",
    )

    notifier = BitrixContactNotifier(bitrix=FakeBitrix())
    result = await notifier.notify(order, subject="Subject", message="Message")

    assert result.channel is NotificationChannel.BITRIX
    assert result.destination == "openline:tg-1"


async def test_yandex_client_lists_waiting_orders_from_requests_info(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = YandexDeliveryClient(
        base_url="https://yandex.test",
        oauth_token="token",
        storage_days=7,
    )

    class FakeHttpClient:
        def __init__(self, *args: object, **kwargs: object) -> None:
            return None

        async def __aenter__(self) -> FakeHttpClient:
            return self

        async def __aexit__(self, *args: object) -> None:
            return None

        async def post(self, path: str, **kwargs: object) -> FakeResponse:
            if path == "/api/b2b/platform/requests/info":
                return FakeResponse(
                    {
                        "requests": [
                            {
                                "request_id": "request-1",
                                "courier_order_id": "courier-1",
                                "request": {
                                    "info": {"operator_request_id": "430439FPerp"},
                                    "recipient_info": {
                                        "first_name": "Ирина",
                                        "last_name": "Петрова",
                                        "email": "client@example.com",
                                    },
                                },
                                "state": {
                                    "status": "DELIVERY_ARRIVED_PICKUP_POINT",
                                    "description": "Ждёт в пункте выдачи",
                                    "timestamp": "2026-07-06T08:04:08.000000Z",
                                },
                            },
                            {
                                "request_id": "request-2",
                                "request": {"info": {"operator_request_id": "430440FPerp"}},
                                "state": {"status": "DELIVERY_DELIVERED"},
                            },
                        ]
                    }
                )
            raise AssertionError(path)

    monkeypatch.setattr("unclaimed_orders_service.adapters.httpx.AsyncClient", FakeHttpClient)

    orders = await client.list_waiting_pickup_orders(today=date(2026, 7, 6))

    assert len(orders) == 1
    assert orders[0].external_id == "430439FPerp"
    assert orders[0].pickup_deadline == date(2026, 7, 13)
    assert orders[0].metadata["deadline_source"] == "state.timestamp_plus_storage_days"


async def _fake_login(self: SafeRouteClient) -> str:
    return "token"


class FakeResponse:
    def __init__(self, payload: object) -> None:
        self._payload = payload
        self.status_code = 200

    def raise_for_status(self) -> None:
        return None

    def json(self) -> object:
        return self._payload


class FakeErrorResponse:
    def __init__(self) -> None:
        self.status_code = 400

    def raise_for_status(self) -> None:
        request = httpx.Request("GET", "https://example.test/v2/tracking")
        response = httpx.Response(400, request=request)
        raise httpx.HTTPStatusError("bad tracking", request=request, response=response)

    def json(self) -> object:
        return {}
