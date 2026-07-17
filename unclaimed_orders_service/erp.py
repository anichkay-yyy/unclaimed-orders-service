"""Embedded ERP lookup through platform-admin."""

from __future__ import annotations

import asyncio
import os
import re
import time
from dataclasses import dataclass, field
from http.cookiejar import MozillaCookieJar
from typing import Any

import requests

PLATFORM_ADMIN_BASE_URL = "https://platform-admin.fabrika-fotoknigi.com"
FIVEPOST_API_BASE_URL = "https://api-omni.x5.ru"
DEFAULT_COOKIE_JAR = ".platform_admin_cookies.txt"
DEFAULT_BY_DATE = "01/01/2024 - 12/31/2030"


class OrderIdNotFound(Exception):
    """ERP order was not found."""


@dataclass(frozen=True, slots=True)
class ErpOrderRecord:
    """Resolved ERP order record."""

    lookup_number: str
    found: bool
    order_number: str | None = None
    email: str | None = None
    phone: str | None = None
    already_extended: bool = False
    platform_order: dict[str, Any] = field(default_factory=dict)
    error: str | None = None


@dataclass(frozen=True, slots=True)
class ErpSourceLookup:
    """ERP lookup adapter with local platform-admin mechanics."""

    by_date: str = DEFAULT_BY_DATE

    async def find_order(
        self,
        order_number: str,
        *,
        by_date: str | None = None,
    ) -> ErpOrderRecord:
        """Find an ERP order record by order number or transport tracking number."""
        lookup_number = order_number.strip()
        if not lookup_number:
            return ErpOrderRecord(
                lookup_number=order_number,
                found=False,
                error="empty_order_number",
            )
        return await asyncio.to_thread(
            self._find_order_sync,
            lookup_number,
            by_date or self.by_date,
        )

    def _find_order_sync(self, lookup_number: str, by_date: str) -> ErpOrderRecord:
        try:
            service = _build_platform_service()
            resolved_order_number, platform_order = _resolve_platform_order(
                service,
                lookup_number,
                by_date,
            )
        except OrderIdNotFound:
            return ErpOrderRecord(lookup_number=lookup_number, found=False)
        except Exception as exc:
            return ErpOrderRecord(lookup_number=lookup_number, found=False, error=str(exc))
        email = _extract_email(platform_order)
        phone = _extract_phone(platform_order)
        already_extended = _extract_already_extended(platform_order)
        if not email:
            return ErpOrderRecord(
                lookup_number=lookup_number,
                found=True,
                order_number=resolved_order_number,
                phone=phone,
                already_extended=already_extended,
                platform_order=platform_order,
                error="email_not_found",
            )
        return ErpOrderRecord(
            lookup_number=lookup_number,
            found=True,
            order_number=resolved_order_number,
            email=email,
            phone=phone,
            already_extended=already_extended,
            platform_order=platform_order,
        )


def _build_platform_service() -> PlatformAdminLookupService:
    return PlatformAdminLookupService(
        email=_required_env("ERP_PROXY_EMAIL", "PHOTO_PRINT_EMAIL"),
        password=_required_env("ERP_PROXY_PASSWORD", "PHOTO_PRINT_PASSWORD"),
        base_url=os.environ.get("ERP_PROXY_BASE_URL", PLATFORM_ADMIN_BASE_URL),
        locale=os.environ.get("ERP_PROXY_LOCALE", "en"),
        company=os.environ.get("ERP_PROXY_COMPANY", "company-wavwh"),
        cookie_jar_path=os.environ.get("ERP_PROXY_COOKIE_JAR", DEFAULT_COOKIE_JAR),
        default_by_date=os.environ.get("ERP_PROXY_BY_DATE", DEFAULT_BY_DATE),
        fivepost_resolver=_build_fivepost_resolver(),
    )


def _required_env(*names: str) -> str:
    for name in names:
        value = os.environ.get(name)
        if value:
            return value
    msg = f"{' or '.join(names)} is required"
    raise RuntimeError(msg)


@dataclass(slots=True)
class PlatformAdminLookupService:
    """Small platform-admin client for order search."""

    email: str
    password: str
    base_url: str = PLATFORM_ADMIN_BASE_URL
    locale: str = "en"
    company: str = "company-wavwh"
    cookie_jar_path: str = DEFAULT_COOKIE_JAR
    default_by_date: str = DEFAULT_BY_DATE
    fivepost_resolver: FivePostOrderResolver | None = None
    client: PlatformAdminClient = field(init=False)

    def __post_init__(self) -> None:
        self.client = PlatformAdminClient(
            base_url=self.base_url,
            cookie_jar_path=self.cookie_jar_path,
        )

    def ensure_authenticated(self) -> None:
        try:
            self.client.ensure_page_csrf(self.locale, self.company)
        except Exception:
            self.client.login(self.locale, self.email, self.password)
            self.client.ensure_page_csrf(self.locale, self.company)

    def get_orders(
        self,
        order_number: str | None = None,
        query: str | None = None,
        query_type: str = "number",
        by_date: str | None = None,
        start: int = 0,
        length: int = 25,
        filters: dict[str, object] | None = None,
    ) -> list[dict[str, Any]]:
        self.ensure_authenticated()
        request = {
            "locale": self.locale,
            "company": self.company,
            "by_date": by_date or self.default_by_date,
            "query": query if query is not None else (order_number or ""),
            "query_type": query_type,
            "start": start,
            "length": length,
            "filters": filters,
        }
        try:
            payload = self.client.get_orders(**request)
        except (RuntimeError, ValueError, requests.RequestException):
            self.client.login(self.locale, self.email, self.password)
            self.client.ensure_page_csrf(self.locale, self.company)
            payload = self.client.get_orders(**request)
        rows = payload.get("data")
        return [row for row in rows if isinstance(row, dict)] if isinstance(rows, list) else []

    def find_transport_order_number(self, *numbers: str | None) -> str | None:
        if self.fivepost_resolver is None:
            return None
        seen: set[str] = set()
        for number in numbers:
            lookup_number = str(number or "").strip()
            if not lookup_number or lookup_number in seen:
                continue
            seen.add(lookup_number)
            result = self.fivepost_resolver.find_order_number(lookup_number)
            if result:
                return result
        return None


class PlatformAdminClient:
    """HTTP client for platform-admin order pages."""

    def __init__(self, *, base_url: str, cookie_jar_path: str) -> None:
        self.base_url = base_url.rstrip("/")
        self.cookie_jar_path = cookie_jar_path
        self.cookies = MozillaCookieJar(cookie_jar_path)
        self.session = requests.Session()
        self.session.cookies = self.cookies
        self.csrf_token: str | None = None
        self.csrf_update_url: str | None = None
        self.csrf_update_timeout_minutes: int | None = None
        self.csrf_timestamp: int | None = None

        if os.path.exists(cookie_jar_path):
            self.cookies.load(ignore_discard=True, ignore_expires=True)

    def save_cookies(self) -> None:
        self.cookies.save(ignore_discard=True, ignore_expires=True)

    def login(self, locale: str, email: str, password: str) -> None:
        self.get_login_page(locale)
        response = self.session.post(
            f"{self.base_url}/{locale}/login",
            data={
                "_token": self.csrf_token,
                "backurl": "/",
                "email": email,
                "password": password,
                "remember": "on",
            },
            headers={
                **self._headers(),
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                "Content-Type": "application/x-www-form-urlencoded",
            },
            allow_redirects=True,
            timeout=30,
        )
        response.raise_for_status()
        self.csrf_token = self._extract_csrf(response.text)
        self.save_cookies()

        if re.search(r'name="password"|/login', response.text) and "Logout" not in response.text:
            msg = "Login did not reach an authenticated page"
            raise RuntimeError(msg)

    def get_login_page(self, locale: str) -> str:
        response = self.session.get(
            f"{self.base_url}/{locale}/login",
            params={"backurl": "/"},
            headers=self._headers(),
            timeout=30,
        )
        response.raise_for_status()
        self.csrf_token = self._extract_csrf(response.text)
        self.save_cookies()
        return response.text

    def ensure_page_csrf(self, locale: str, company: str) -> None:
        response = self.session.get(
            f"{self.base_url}/{locale}/{company}/orders",
            headers=self._headers(),
            timeout=30,
        )
        if response.status_code in (401, 419) or "/login" in response.url:
            msg = "Session is not authenticated"
            raise RuntimeError(msg)
        response.raise_for_status()
        self.csrf_token = self._extract_csrf(response.text)
        self.save_cookies()

    def get_orders(
        self,
        *,
        locale: str,
        company: str,
        by_date: str,
        query: str = "",
        query_type: str = "number",
        start: int = 0,
        length: int = 25,
        draw: int = 1,
        filters: dict[str, object] | None = None,
    ) -> dict[str, Any]:
        params = _datatables_order_params(
            by_date=by_date,
            query=query,
            query_type=query_type,
            start=start,
            length=length,
            draw=draw,
            filters=filters,
        )
        response = self.session.get(
            f"{self.base_url}/{locale}/{company}/orders",
            params=params,
            headers={
                **self._headers(ajax=True),
                "Referer": f"{self.base_url}/{locale}/{company}/orders",
            },
            timeout=30,
        )
        if response.status_code in (401, 419) or "/login" in response.url:
            msg = f"Session expired or CSRF rejected: HTTP {response.status_code}"
            raise RuntimeError(msg)
        response.raise_for_status()
        self.save_cookies()
        payload = response.json()
        if not isinstance(payload, dict):
            msg = "platform-admin orders response is not an object"
            raise RuntimeError(msg)
        return payload

    def refresh_csrf(self, *, force: bool = False) -> dict[str, Any]:
        if not force and self.csrf_timestamp and self.csrf_update_timeout_minutes:
            refresh_at = self.csrf_timestamp + self.csrf_update_timeout_minutes * 60
            now = int(time.time())
            if now < refresh_at:
                return {
                    "skipped": "csrf token is still fresh",
                    "seconds_until_refresh": refresh_at - now,
                }

        url = self.csrf_update_url or f"{self.base_url}/api/csrf-update"
        response = self.session.get(url, headers=self._headers(ajax=True), timeout=30)
        if response.status_code == 405:
            response = self.session.post(url, headers=self._headers(ajax=True), timeout=30)
        response.raise_for_status()
        self.save_cookies()
        payload = response.json()
        if not isinstance(payload, dict):
            return {"raw": response.text[:300]}

        for key in ("token", "csrf_token", "csrfToken"):
            token = payload.get(key)
            if token:
                self.csrf_token = str(token)
                break
        return payload

    def _headers(self, *, ajax: bool = False) -> dict[str, str]:
        headers = {
            "Accept-Language": "en-US,en;q=0.9,ru;q=0.8",
            "User-Agent": (
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/148.0 Safari/537.36"
            ),
        }
        if ajax:
            headers.update(
                {
                    "Accept": "application/json, text/javascript, */*; q=0.01",
                    "X-Requested-With": "XMLHttpRequest",
                }
            )
        if self.csrf_token:
            headers["X-CSRF-TOKEN"] = self.csrf_token
        return headers

    def _extract_csrf(self, html: str) -> str:
        for pattern in (
            r'<meta\s+name="csrf-token"([^>]*)>',
            r'name="_token"\s+value="([^"]+)"',
        ):
            match = re.search(pattern, html)
            if match is None:
                continue
            if pattern.startswith("<meta"):
                attrs = match.group(1)
                token = self._extract_attr(attrs, "content")
                self.csrf_update_url = self._extract_attr(attrs, "data-update-url")
                timeout = self._extract_attr(attrs, "data-update-timeout")
                timestamp = self._extract_attr(attrs, "data-timestamp")
                self.csrf_update_timeout_minutes = int(timeout) if timeout else None
                self.csrf_timestamp = int(timestamp) if timestamp else None
                if token:
                    return token
            else:
                return match.group(1)
        msg = "CSRF token not found in HTML"
        raise RuntimeError(msg)

    @staticmethod
    def _extract_attr(attrs: str, name: str) -> str | None:
        match = re.search(rf'{re.escape(name)}="([^"]*)"', attrs)
        return match.group(1) if match else None


@dataclass(slots=True)
class FivePostOrderResolver:
    """Resolve ERP order numbers from 5Post order query."""

    base_url: str
    login: str
    password: str
    timeout: int = 15
    token: str | None = None
    session: requests.Session = field(init=False)

    def __post_init__(self) -> None:
        self.base_url = self.base_url.rstrip("/")
        self.session = requests.Session()

    def find_order_number(self, number: str) -> str | None:
        lookup_number = str(number or "").strip()
        if not lookup_number:
            return None
        for query in self._query_variants(lookup_number):
            order = self._find_order(query, lookup_number)
            order_number = self._erp_order_number(order)
            if order_number:
                return order_number
        return None

    def _find_order(self, query: dict[str, str], lookup_number: str) -> dict[str, Any]:
        try:
            payload = self._post_order_query(query)
        except requests.RequestException:
            return {}
        rows = payload.get("content")
        if not isinstance(rows, list):
            return {}
        selected = self._select_order(rows, lookup_number)
        return selected or {}

    def _post_order_query(self, query: dict[str, str]) -> dict[str, Any]:
        self._ensure_authenticated()
        response = self.session.post(
            f"{self.base_url}/partners-portal/api/v1/orders/query",
            params={"page": 0, "size": 20, "sort": "createDate,desc"},
            json={**query, "orderType": query.get("orderType")},
            headers=self._headers(),
            timeout=self.timeout,
        )
        if response.status_code == 401:
            self.token = None
            self._ensure_authenticated()
            response = self.session.post(
                f"{self.base_url}/partners-portal/api/v1/orders/query",
                params={"page": 0, "size": 20, "sort": "createDate,desc"},
                json={**query, "orderType": query.get("orderType")},
                headers=self._headers(),
                timeout=self.timeout,
            )
        if response.status_code >= 400:
            return {}
        payload = response.json()
        return payload if isinstance(payload, dict) else {}

    def _ensure_authenticated(self) -> None:
        if self.token:
            return
        response = self.session.post(
            f"{self.base_url}/partners-portal-auth/api/v2/auth",
            json={"login": self.login, "password": self.password},
            headers=self._headers(authorized=False),
            timeout=self.timeout,
        )
        response.raise_for_status()
        payload = response.json()
        token = payload.get("jwt") or payload.get("token") or payload.get("access_token")
        if not token:
            msg = "5Post auth response does not contain token"
            raise RuntimeError(msg)
        self.token = str(token)

    def _headers(self, *, authorized: bool = True) -> dict[str, str]:
        headers = {
            "Accept": "application/json",
            "Accept-Language": "ru-RU;q=0.5",
            "Content-Type": "application/json",
        }
        if authorized:
            headers["Authorization"] = f"Bearer {self.token}"
        return headers

    def _query_variants(self, number: str) -> list[dict[str, str]]:
        variants = [
            {"senderOrderId": number},
            {"clientOrderId": number},
            {"omniBarcode": number},
            {"cargoBarcode": number},
            {"barcode": number},
        ]
        normalized = _normalize_track_number(number)
        if normalized and normalized != number:
            variants.extend(
                [
                    {"senderOrderId": normalized},
                    {"clientOrderId": normalized},
                ]
            )
        return variants

    def _select_order(self, rows: list[object], number: str) -> dict[str, Any] | None:
        exact_fields = ("senderOrderId", "clientOrderId", "omniBarcode", "cargoBarcode", "barcode")
        for row in rows:
            if not isinstance(row, dict):
                continue
            if any(self._same_number(row.get(field), number) for field in exact_fields):
                return row
        return next((row for row in rows if isinstance(row, dict)), None)

    @staticmethod
    def _same_number(left: object, right: str) -> bool:
        if left is None:
            return False
        left_raw = str(left).strip()
        right_raw = str(right).strip()
        if left_raw == right_raw:
            return True
        return _normalize_track_number(left_raw) == _normalize_track_number(right_raw)

    @staticmethod
    def _erp_order_number(order: dict[str, Any]) -> str | None:
        for key in ("senderOrderId", "clientOrderId", "sender_order_id", "client_order_id"):
            value = order.get(key)
            normalized = _normalize_track_number(str(value or ""))
            if normalized:
                return normalized
        return None


def _build_fivepost_resolver() -> FivePostOrderResolver | None:
    login = os.environ.get("FIVEPOST_LOGIN")
    password = os.environ.get("FIVEPOST_PASSWORD")
    if not login or not password:
        return None
    return FivePostOrderResolver(
        base_url=os.environ.get("FIVEPOST_API_BASE_URL", FIVEPOST_API_BASE_URL),
        login=login,
        password=password,
    )


def _resolve_platform_order(
    service: PlatformAdminLookupService,
    lookup_number: str,
    by_date: str,
) -> tuple[str, dict[str, Any]]:
    normalized_order_number = _normalize_track_number(lookup_number)
    if not normalized_order_number:
        raise OrderIdNotFound("Order number is empty")

    platform_order = _first_platform_order(service, normalized_order_number, by_date)
    if platform_order is not None:
        return normalized_order_number, platform_order

    transport_order_number = service.find_transport_order_number(
        lookup_number,
        normalized_order_number,
    )
    normalized_transport_number = _normalize_track_number(transport_order_number or "")
    if normalized_transport_number:
        platform_order = _first_platform_order(service, normalized_transport_number, by_date)
        if platform_order is not None:
            return normalized_transport_number, platform_order

    raise OrderIdNotFound(f"Order {normalized_order_number} was not found")


def _first_platform_order(
    service: PlatformAdminLookupService,
    order_number: str,
    by_date: str,
) -> dict[str, Any] | None:
    for query_type in ("number", "track"):
        orders = service.get_orders(
            order_number=order_number,
            query_type=query_type,
            by_date=by_date,
            length=1,
        )
        if orders:
            return orders[0]
    return None


def _extract_email(platform_order: dict[str, Any]) -> str | None:
    for source_key in ("delivery_data", "payment_data", "customer_data"):
        source = platform_order.get(source_key)
        if not isinstance(source, dict):
            continue
        value = source.get("email")
        if value:
            return str(value).strip() or None
    value = platform_order.get("email")
    return str(value).strip() if value else None


def _extract_phone(platform_order: dict[str, Any]) -> str | None:
    user = platform_order.get("user")
    sources = (
        platform_order.get("payment_data"),
        user if isinstance(user, dict) else None,
        platform_order.get("customer_data"),
        platform_order.get("delivery_data"),
    )
    keys = ("phone", "phoneFormatted", "recipient_phone")
    for source in sources:
        if not isinstance(source, dict):
            continue
        for key in keys:
            value = source.get(key)
            if value:
                return str(value).strip() or None
    value = platform_order.get("recipient_phone") or platform_order.get("phone")
    return str(value).strip() if value else None


def _extract_already_extended(platform_order: dict[str, Any]) -> bool:
    delivery_data = platform_order.get("delivery_data")
    if not isinstance(delivery_data, dict):
        return False
    return _bool_value(delivery_data.get("has_extend_hold_request"))


def _bool_value(value: object) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, int):
        return value != 0
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "y", "on"}
    return False


def _normalize_track_number(track_number: str) -> str:
    normalized = str(track_number or "").strip()
    if "_" in normalized:
        normalized = normalized.split("_", 1)[0]
    if "-" in normalized:
        normalized = normalized.split("-", 1)[0]
    leading_order_number = re.match(r"^\s*(\d{5,12})(?=\D|$)", normalized)
    if leading_order_number:
        return leading_order_number.group(1)
    normalized = re.sub(r"\D+", "", normalized)
    return normalized.strip()


def _datatables_order_params(
    *,
    by_date: str,
    query: str = "",
    query_type: str = "number",
    start: int = 0,
    length: int = 25,
    draw: int = 1,
    filters: dict[str, object] | None = None,
) -> dict[str, str]:
    columns = [
        "number",
        "created_at",
        "number",
        "status",
        "price",
        "payment_status",
        "delivery_system_id",
        "buyer_id",
        "number",
        "showcase_id",
        "status_updated_at",
    ]
    params = {
        "draw": str(draw),
        "order[0][column]": "0",
        "order[0][dir]": "desc",
        "start": str(start),
        "length": str(length),
        "search[value]": "",
        "search[regex]": "false",
        "by_showcase": "",
        "by_status": "",
        "by_payment_status": "",
        "by_payment_system": "",
        "by_delivery": "",
        "by_track_status": "",
        "by_date": by_date,
        "by_query_type": query_type,
        "by_query": query,
    }
    if filters:
        for key, value in filters.items():
            params[key] = "" if value is None else str(value)
    for index, data in enumerate(columns):
        params[f"columns[{index}][data]"] = data
        params[f"columns[{index}][name]"] = ""
        params[f"columns[{index}][searchable]"] = "true"
        params[f"columns[{index}][orderable]"] = "false" if index in (2, 8) else "true"
        params[f"columns[{index}][search][value]"] = ""
        params[f"columns[{index}][search][regex]"] = "false"
    return params
