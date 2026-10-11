"""HTTP clients for the ERP.

Two classes on purpose: tools only ever receive an `ErpReader`, which holds the
read-only key and has no write methods. `ErpWriter` holds the write key and is
used in exactly one place: applying a change a human has approved.
"""
from __future__ import annotations

import unicodedata
from typing import Any
from urllib.parse import quote

import httpx

def clean_text(value: Any, limit: int = 300) -> str:
    """One printable line, safe to store and show: control, format (for example right-to-left
    override) and other invisible characters become spaces."""
    text = "".join(" " if unicodedata.category(ch)[0] in "CZ" else ch for ch in str(value))
    return " ".join(text.split())[:limit]


class ErpError(Exception):
    """The ERP answered with an error status."""

    def __init__(self, status: int, detail: str):
        super().__init__(f"ERP {status}: {detail}")
        self.status = status
        self.detail = detail

    @property
    def refused(self) -> bool:
        """True for a clean business refusal (4xx): the ERP understood and wrote nothing."""
        return 400 <= self.status < 500


class ErpUnreachable(Exception):
    """No usable answer from the ERP, so the outcome of the request is unknown."""


def _detail(resp: httpx.Response) -> str:
    if resp.status_code >= 500:
        return "internal error"  # never pass a server's error body on; it can contain secrets
    try:
        detail = resp.json().get("detail", "")
    except (ValueError, AttributeError):
        return "request refused"  # an unexpected body is not passed on either
    if isinstance(detail, list):  # FastAPI validation errors
        parts = [f"{'.'.join(str(p) for p in d.get('loc', [])[1:])}: {d.get('msg', '')}" for d in detail if isinstance(d, dict)]
        return clean_text("; ".join(parts) or "invalid request")
    return clean_text(detail)


async def _request(client: httpx.AsyncClient, method: str, path: str, **kwargs: Any) -> Any:
    try:
        resp = await client.request(method, path, **kwargs)
    except httpx.HTTPError as exc:
        raise ErpUnreachable(type(exc).__name__) from exc
    if resp.status_code >= 400:
        raise ErpError(resp.status_code, _detail(resp))
    try:
        return resp.json()
    except ValueError as exc:
        raise ErpUnreachable(f"HTTP {resp.status_code} with a body that is not JSON") from exc


def _seg(value: Any) -> str:
    """A value used as one URL path segment: fully percent-encoded, so it cannot change the path."""
    return quote(str(value), safe="")


class ErpReader:
    def __init__(self, base_url: str, read_key: str, timeout: float = 10.0):
        self._client = httpx.AsyncClient(base_url=base_url, headers={"X-API-Key": read_key}, timeout=timeout)

    async def aclose(self) -> None:
        await self._client.aclose()

    async def customers(self, query: str) -> list[dict]:
        return await _request(self._client, "GET", "/customers", params={"q": query})

    async def customer(self, customer_id: str) -> dict:
        return await _request(self._client, "GET", f"/customers/{_seg(customer_id)}")

    async def products(self, query: str = "", low_stock: bool = False) -> list[dict]:
        return await _request(self._client, "GET", "/products", params={"q": query, "low_stock": low_stock})

    async def product(self, sku: str) -> dict:
        return await _request(self._client, "GET", f"/products/{_seg(sku)}")

    async def order(self, order_id: int) -> dict:
        return await _request(self._client, "GET", f"/orders/{int(order_id)}")

    async def orders(self, customer_id: str | None = None, status: str | None = None) -> list[dict]:
        params = {k: v for k, v in {"customer_id": customer_id, "status": status}.items() if v}
        return await _request(self._client, "GET", "/orders", params=params)

    async def invoices(self, status: str | None = None) -> list[dict]:
        return await _request(self._client, "GET", "/invoices", params={"status": status} if status else None)


class ErpWriter:
    def __init__(self, base_url: str, write_key: str, timeout: float = 15.0):
        self._client = httpx.AsyncClient(base_url=base_url, headers={"X-API-Key": write_key}, timeout=timeout)

    async def aclose(self) -> None:
        await self._client.aclose()

    async def create_order(self, payload: dict, idempotency_key: str) -> dict:
        return await _request(
            self._client, "POST", "/orders", json=payload, headers={"Idempotency-Key": idempotency_key}
        )

    async def update_order(self, order_id: int, payload: dict) -> dict:
        return await _request(self._client, "PATCH", f"/orders/{int(order_id)}", json=payload)

    async def create_invoice(self, order_id: int) -> dict:
        return await _request(self._client, "POST", "/invoices", json={"order_id": int(order_id)})
