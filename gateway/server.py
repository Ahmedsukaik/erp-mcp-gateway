"""MCP server that exposes an ERP to AI agents, safely.

Design rules
------------
1. Read tools and write tools are separate. Write tools are named `propose_*` and
   never touch the ERP: they queue a change for a human.
2. Only an approved change is written, by `gateway.approvals`, with a key the
   agents never see.
3. Every request needs a bearer token; the token's role is checked on every tool call.
4. Every tool call (including refused and malformed ones) and every action on the
   approval queue is recorded in the audit log.

Run:  python -m gateway.server --profile full --port 8101
"""
from __future__ import annotations

import argparse
import contextvars
import functools
import logging
import re
import sys
import time
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Any, Awaitable, Callable, Literal, Mapping

from mcp.server.mcpserver import Context, MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.server.transport_security import TransportSecuritySettings
from mcp.types import ToolAnnotations
from pydantic import BaseModel, Field
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

from . import approvals
from .approvals import money
from .config import Identity, Permission, Settings, TokenTable
from .erp_client import ErpError, ErpReader, ErpUnreachable, ErpWriter, clean_text
from .store import AlreadyDecided, Store

Profile = Literal["read", "full"]
log = logging.getLogger("gateway")

INSTRUCTIONS = (
    "Tools for working with the company ERP. Read tools return live data. "
    "Tools named propose_* do NOT change the ERP: they queue a change that a human approver must approve. "
    "After proposing, tell the user the change id and that it is waiting for approval; "
    "use get_change_status to check the outcome. Amounts are integers in minor currency units (pence)."
)

# What the guard around a tool learned about the current call; read by the audit middleware.
_call_note: contextvars.ContextVar[dict[str, Any]] = contextvars.ContextVar("call_note")
# Who is making the current tool call. Set by the guard around every tool.
_caller: contextvars.ContextVar[Identity] = contextvars.ContextVar("caller")

_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,39}")
_REFERENCE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_/#-]{0,39}")
MAX_QTY = 100_000
MAX_LINES = 100
MAX_NOTES = 500


class OrderLine(BaseModel):
    sku: str = Field(description="Product SKU, for example WID-100", min_length=1, max_length=40)
    qty: int = Field(description="Quantity, a whole number above zero", gt=0, le=MAX_QTY, strict=True)


@dataclass
class Deps:
    reader: ErpReader
    writer: ErpWriter | None  # None on a read-only gateway: it does not even hold the write key
    store: Store
    tokens: TokenTable
    currency: str = "GBP"

    @classmethod
    def from_settings(cls, s: Settings, profile: Profile = "full") -> "Deps":
        return cls(
            reader=ErpReader(s.erp_url, s.erp_read_key),
            writer=ErpWriter(s.erp_url, s.erp_write_key) if profile == "full" else None,
            store=Store(s.db_path),
            tokens=s.tokens,
            currency=s.currency,
        )


# --------------------------------------------------------------------------- input checks
def clean_id(value: str, what: str) -> str:
    """Ids and SKUs go into ERP URLs, so only plain characters are accepted."""
    value = (value or "").strip()
    if not _ID.fullmatch(value):
        raise ToolError(f"{what} may only contain letters, digits, '-', '_' and '.' (max 40 characters).")
    return value


def clean_reference(value: str | None) -> str | None:
    if value is None or not value.strip():
        return None
    value = value.strip()
    if not _REFERENCE.fullmatch(value):
        raise ToolError("reference may only contain letters, digits and _ / # - with no spaces (max 40 characters).")
    return value


def clean_date(value: str | None) -> str | None:
    if value is None:
        return None
    try:
        parsed = date.fromisoformat(value)
        if len(value) != 10:
            raise ValueError
    except (ValueError, TypeError):
        raise ToolError("delivery_date must be in YYYY-MM-DD format.") from None
    today = date.today()
    if parsed < today or parsed > today + timedelta(days=730):
        raise ToolError("delivery_date must be today or later, and within two years.")
    return parsed.isoformat()


def clean_notes(value: str | None) -> str | None:
    if value is None:
        return None
    if len(value) > MAX_NOTES:
        raise ToolError(f"notes must be {MAX_NOTES} characters or fewer.")
    return clean_text(value, MAX_NOTES)


def build_server(deps: Deps, profile: Profile = "full", stdio_identity: Identity | None = None) -> MCPServer:
    """Build the MCP server.

    profile="read" registers read tools only, so an agent connected to that server cannot
    even see a write tool. profile="full" adds the propose_* tools and the approval routes;
    each call is still checked against the caller's role.
    stdio_identity is used only when there are no HTTP headers (local stdio transport).
    """
    store = deps.store
    tool_kinds: dict[str, str] = {}

    def identify(headers: Mapping[str, str] | None) -> Identity | None:
        if headers is None:
            return stdio_identity
        return deps.tokens.identify(headers.get("authorization"))

    async def audit_tool_calls(ctx: Any, call_next: Callable[[Any], Awaitable[Any]]) -> Any:
        """Record every tools/call, including unknown tools and arguments that fail validation,
        which never reach the tool's own code."""
        if ctx.method != "tools/call":
            return await call_next(ctx)
        params = ctx.params if isinstance(ctx.params, dict) else {}
        name = str(params.get("name", "?"))
        who = identify(getattr(getattr(ctx, "request", None), "headers", None))
        note: dict[str, Any] = {}
        reset = _call_note.set(note)
        started = time.monotonic()
        outcome, detail = "error", None
        try:
            result = await call_next(ctx)
            failed = bool(result.get("isError")) if isinstance(result, dict) else bool(getattr(result, "is_error", False))
            outcome = note.get("outcome") or ("error" if failed else "ok")
            detail = note.get("detail")
            if failed and not detail:
                content = result.get("content") if isinstance(result, dict) else None
                detail = (content[0].get("text") if content and isinstance(content[0], dict) else None) or "tool error"
            return result
        except Exception as exc:
            detail = f"{type(exc).__name__}"
            raise
        finally:
            _call_note.reset(reset)
            try:
                store.audit(
                    actor=who.name if who else "unknown", role=who.role if who else "none", action=name,
                    kind=tool_kinds.get(name, "unknown"), arguments=params.get("arguments") or {},
                    outcome=outcome, detail=detail, duration_ms=int((time.monotonic() - started) * 1000),
                )
            except Exception:  # the audit log must never break a call
                log.exception("could not write audit entry for %s", name)

    server = MCPServer("erp-gateway", instructions=INSTRUCTIONS, version="1.0.0", middleware=[audit_tool_calls])

    def tool(permission: Permission, title: str) -> Callable[[Callable[..., Awaitable[dict]]], Any]:
        """Register a tool with authentication and a role check around it."""

        def decorator(fn: Callable[..., Awaitable[dict]]) -> Any:
            @functools.wraps(fn)
            async def guarded(*args: Any, **kwargs: Any) -> dict[str, Any]:
                note = _call_note.get({})
                who = identify(kwargs["ctx"].headers)
                if who is None:
                    note.update(outcome="denied", detail="missing or invalid token")
                    raise ToolError("Not authenticated: send a valid bearer token.")
                if not who.can(permission):
                    note.update(outcome="denied", detail=f"role {who.role} lacks {permission}")
                    raise ToolError(f"Permission denied: role '{who.role}' cannot use {fn.__name__}.")
                reset = _caller.set(who)
                try:
                    result = await fn(*args, **kwargs)
                except ToolError as exc:
                    note.update(detail=str(exc))
                    raise
                except ErpError as exc:
                    note.update(detail=str(exc))
                    raise ToolError(exc.detail if exc.refused else "The ERP returned an internal error.") from exc
                except ErpUnreachable as exc:
                    note.update(detail=f"ERP unreachable: {exc}")
                    raise ToolError("The ERP is not reachable right now. Try again later.") from exc
                except Exception as exc:  # never leak internals to the model
                    note.update(detail=f"{type(exc).__name__}: {exc}")
                    log.exception("tool %s failed", fn.__name__)
                    raise ToolError("Internal gateway error. It has been logged.") from exc
                finally:
                    _caller.reset(reset)
                if isinstance(result, dict) and result.get("change_id"):
                    note.update(detail=result["change_id"])
                return result

            tool_kinds[fn.__name__] = permission
            annotations = ToolAnnotations(
                title=title,
                readOnlyHint=permission == "read",
                destructiveHint=False,
                idempotentHint=permission == "read",
                openWorldHint=False,
            )
            server.add_tool(guarded, name=fn.__name__, title=title, description=fn.__doc__, annotations=annotations)
            return guarded

        return decorator

    # ------------------------------------------------------------------ read tools
    @tool("read", "Find customer")
    async def find_customer(query: str, ctx: Context) -> dict[str, Any]:
        """Find customers by name, email address or customer id. Returns up to 20 matches."""
        return {"customers": await deps.reader.customers(query[:100])}

    @tool("read", "Search products")
    async def search_products(query: str, ctx: Context) -> dict[str, Any]:
        """Search products by name or SKU. Returns SKU, name, unit price (pence) and stock."""
        return {"products": await deps.reader.products(query[:100])}

    @tool("read", "Check stock")
    async def check_stock(sku: str, ctx: Context) -> dict[str, Any]:
        """Stock level for one SKU, with its reorder level and whether it is running low."""
        p = await deps.reader.product(clean_id(sku, "sku"))
        return {**p, "low": p["stock"] <= p["reorder_level"]}

    @tool("read", "List low stock")
    async def list_low_stock(ctx: Context) -> dict[str, Any]:
        """Products whose stock is at or below their reorder level."""
        return {"products": await deps.reader.products(low_stock=True)}

    @tool("read", "Find order")
    async def find_order(order_id: int, ctx: Context) -> dict[str, Any]:
        """One order by its order number, with its lines, status, total (pence) and delivery date."""
        return await deps.reader.order(order_id)

    @tool("read", "List orders")
    async def list_orders(
        ctx: Context,
        customer_id: str | None = None,
        status: Literal["open", "confirmed", "shipped", "cancelled"] | None = None,
    ) -> dict[str, Any]:
        """Latest orders (max 50), optionally filtered by customer id and/or status."""
        return {"orders": await deps.reader.orders(clean_id(customer_id, "customer_id") if customer_id else None, status)}

    @tool("read", "List unpaid invoices")
    async def list_unpaid_invoices(ctx: Context) -> dict[str, Any]:
        """Unpaid invoices, oldest due date first, with the total outstanding (pence)."""
        invoices = await deps.reader.invoices("unpaid")
        return {
            "invoices": invoices,
            "count": len(invoices),
            "total_outstanding": sum(i["amount"] for i in invoices),
        }

    @tool("read", "Get change status")
    async def get_change_status(change_id: str, ctx: Context) -> dict[str, Any]:
        """Status of a proposed change: pending, applying, approved, rejected, failed or unknown.
        When approved, `result` holds what the ERP created; otherwise `error` says why."""
        change = store.get_change(change_id)
        if change is None:
            raise ToolError("No change with that id.")
        return {k: change[k] for k in ("id", "kind", "status", "summary", "warnings", "result", "error")}

    if profile == "read":
        _add_health_route(server)
        return server

    # ------------------------------------------------------------- propose (write) tools
    def queued(change: dict, created: bool, payload: dict) -> dict[str, Any]:
        out = {
            "change_id": change["id"],
            "status": change["status"],
            "summary": change["summary"],
            "warnings": change["warnings"],
            "already_proposed": not created,
            "next_step": "Nothing has been written to the ERP. A human approver must approve this change.",
        }
        if not created:
            out["differs_from_existing"] = change["payload"] != payload
            out["next_step"] = (
                "A change with this customer and reference already exists (see status). Nothing new was queued."
            )
        return out

    @tool("propose", "Propose new order")
    async def propose_create_order(
        customer_id: str,
        lines: list[OrderLine],
        ctx: Context,
        reference: str | None = None,
        delivery_date: str | None = None,
        notes: str | None = None,
    ) -> dict[str, Any]:
        """Propose a new sales order. Does NOT create it: a human must approve first.
        `reference` is the customer's PO number or another unique reference. Proposing the same
        customer + reference again returns the existing change (`already_proposed`: true) and
        queues nothing; `differs_from_existing` tells you if the content is different.
        Without a reference there is no duplicate protection.
        `delivery_date` is ISO format (YYYY-MM-DD), today or later."""
        customer_id = clean_id(customer_id, "customer_id")
        reference, delivery_date, notes = clean_reference(reference), clean_date(delivery_date), clean_notes(notes)
        if not lines:
            raise ToolError("An order needs at least one line.")
        if len(lines) > MAX_LINES:
            raise ToolError(f"An order can have at most {MAX_LINES} lines.")
        wanted: dict[str, int] = {}
        for line in lines:
            sku = clean_id(line.sku, "sku").upper()
            wanted[sku] = wanted.get(sku, 0) + line.qty
            if wanted[sku] > MAX_QTY:
                raise ToolError(f"Total quantity for {sku} is above {MAX_QTY}.")
        customer = await deps.reader.customer(customer_id)
        total, parts, warnings = 0, [], []
        for sku, qty in wanted.items():
            product = await deps.reader.product(sku)
            total += qty * product["unit_price"]
            parts.append(f"{qty} x {sku} ({clean_text(product['name'], 60)})")
            if product["stock"] < qty:
                warnings.append(f"Stock for {sku} is {product['stock']}, order asks for {qty}. Approval will fail unless stock arrives first.")
        payload = {
            "customer_id": customer["id"],
            "lines": [{"sku": s, "qty": q} for s, q in wanted.items()],
            "reference": reference,
            "delivery_date": delivery_date,
            "notes": notes,
        }
        summary = (
            f"New order for {clean_text(customer['name'], 80)} ({customer['id']}): {', '.join(parts)}. "
            f"Estimated total {money(total, deps.currency)}."
        )
        if reference:
            summary += f" Reference {reference}."
        if delivery_date:
            summary += f" Delivery {delivery_date}."
        dedupe = f"create_order:{customer['id']}:{reference.lower()}" if reference else None
        meta = {"expected_total": total}
        if dedupe:
            # Same customer + reference always maps to the same ERP idempotency key, so even a
            # rejected-and-proposed-again order can never be created twice.
            meta["idempotency_key"] = dedupe
        change, created = store.add_change("create_order", payload, summary, warnings, _caller.get().name, dedupe, meta)
        return queued(change, created, payload)

    @tool("propose", "Propose order update")
    async def propose_update_order(
        order_id: int,
        ctx: Context,
        status: Literal["confirmed", "shipped", "cancelled"] | None = None,
        delivery_date: str | None = None,
        notes: str | None = None,
    ) -> dict[str, Any]:
        """Propose a change to an existing order: its status (confirmed, shipped, cancelled),
        delivery date (YYYY-MM-DD) and/or notes. Does NOT change it: a human must approve first.
        Order lines cannot be changed with this tool."""
        delivery_date, notes = clean_date(delivery_date), clean_notes(notes)
        if status is None and delivery_date is None and notes is None:
            raise ToolError("Give at least one of: status, delivery_date, notes.")
        order = await deps.reader.order(order_id)
        changes = []
        if status:
            changes.append(f"status {order['status']} -> {status}")
        if delivery_date:
            changes.append(f"delivery date {order['delivery_date'] or 'none'} -> {delivery_date}")
        if notes is not None:
            changes.append("notes replaced")
        payload = {"order_id": order["id"], "status": status, "delivery_date": delivery_date, "notes": notes}
        summary = f"Update order {order['id']} ({order['customer_id']}): {'; '.join(changes)}."
        change, created = store.add_change("update_order", payload, summary, [], _caller.get().name)
        return queued(change, created, payload)

    @tool("propose", "Propose invoice")
    async def propose_create_invoice(order_id: int, ctx: Context) -> dict[str, Any]:
        """Propose an invoice for an order, for the order total. Does NOT create it: a human must approve first."""
        order = await deps.reader.order(order_id)
        warnings = []
        if order["status"] == "cancelled":
            raise ToolError(f"Order {order['id']} is cancelled and cannot be invoiced.")
        if any(i["order_id"] == order["id"] for i in await deps.reader.invoices()):
            raise ToolError(f"Order {order['id']} already has an invoice.")
        if order["status"] == "open":
            warnings.append("Order is still open (not confirmed).")
        payload = {"order_id": order["id"]}
        summary = f"Invoice order {order['id']} ({order['customer_id']}) for {money(order['total'], deps.currency)}."
        change, created = store.add_change(
            "create_invoice", payload, summary, warnings, _caller.get().name, dedupe_key=f"create_invoice:{order['id']}"
        )
        return queued(change, created, payload)

    _add_health_route(server)
    _add_approval_routes(server, deps)
    return server


def _add_health_route(server: MCPServer) -> None:
    @server.custom_route("/healthz", methods=["GET"])
    async def healthz(request: Request) -> Response:
        return JSONResponse({"status": "ok"})


def _add_approval_routes(server: MCPServer, deps: Deps) -> None:
    """Plain HTTP endpoints for the human approver: the approval queue and the audit log.
    Every request to them is audited, including refused ones."""
    store = deps.store

    def approver(request: Request, action: str, arguments: dict) -> Identity | None:
        who = deps.tokens.identify(request.headers.get("authorization"))
        if who and who.can("approve"):
            return who
        store.audit(who.name if who else "unknown", who.role if who else "none", action, "approve", arguments,
                    "denied", "approver token required")
        return None

    def denied() -> Response:
        return JSONResponse({"error": "approver token required"}, status_code=403)

    @server.custom_route("/approvals", methods=["GET"])
    async def list_approvals(request: Request) -> Response:
        status = request.query_params.get("status", "pending")
        who = approver(request, "approvals.list", {"status": status})
        if who is None:
            return denied()
        changes = store.list_changes(None if status == "all" else status)
        store.audit(who.name, who.role, "approvals.list", "approve", {"status": status}, "ok", f"{len(changes)} changes")
        return JSONResponse({"changes": changes})

    @server.custom_route("/approvals/{change_id}/approve", methods=["POST"])
    async def approve_change(request: Request) -> Response:
        change_id = request.path_params["change_id"]
        who = approver(request, "approve", {"change_id": change_id})
        if who is None:
            return denied()
        outcome, detail, response = "error", None, None
        try:
            assert deps.writer is not None
            change = await approvals.approve(store, deps.writer, deps.reader, change_id, who.name, deps.currency)
            outcome = "ok" if change["status"] == "approved" else change["status"]
            detail = change.get("error")
            response = JSONResponse(change)
        except KeyError:
            detail, response = "no such change", JSONResponse({"error": "no change with that id"}, status_code=404)
        except AlreadyDecided as exc:
            detail, response = str(exc), JSONResponse({"error": str(exc)}, status_code=409)
        finally:
            store.audit(who.name, who.role, "approve", "approve", {"change_id": change_id}, outcome, detail)
        return response

    @server.custom_route("/approvals/{change_id}/reject", methods=["POST"])
    async def reject_change(request: Request) -> Response:
        change_id = request.path_params["change_id"]
        try:
            body = await request.json()
        except ValueError:
            body = {}
        reason = clean_text(body.get("reason", ""), 300) if isinstance(body, dict) else ""
        arguments = {"change_id": change_id, "reason": reason}
        who = approver(request, "reject", arguments)
        if who is None:
            return denied()
        try:
            change = approvals.reject(store, change_id, who.name, reason)
        except KeyError:
            store.audit(who.name, who.role, "reject", "approve", arguments, "error", "no such change")
            return JSONResponse({"error": "no change with that id"}, status_code=404)
        except AlreadyDecided as exc:
            store.audit(who.name, who.role, "reject", "approve", arguments, "error", str(exc))
            return JSONResponse({"error": str(exc)}, status_code=409)
        store.audit(who.name, who.role, "reject", "approve", arguments, "ok")
        return JSONResponse(change)

    @server.custom_route("/audit", methods=["GET"])
    async def audit(request: Request) -> Response:
        try:
            limit = int(request.query_params.get("limit", "50"))
        except ValueError:
            limit = 50
        who = approver(request, "audit.read", {"limit": limit})
        if who is None:
            return denied()
        entries = store.audit_entries(limit)
        store.audit(who.name, who.role, "audit.read", "approve", {"limit": limit}, "ok")
        return JSONResponse({"entries": entries})


class Gate:
    """ASGI middleware, the outer door for every path except /healthz:

    * if public hostnames are configured, requests for any other Host are refused;
    * requests without a known bearer token are refused before they reach MCP.

    Role checks still happen per tool call and per approval route. Refused requests here
    are written to the application log (not the audit table, so that anonymous traffic
    cannot fill it).
    """

    def __init__(self, app: Any, tokens: TokenTable, allowed_hosts: list[str] | None = None,
                 public_paths: frozenset[str] = frozenset({"/healthz"})):
        self.app, self.tokens, self.public_paths = app, tokens, public_paths
        self.allowed_hosts = {h.lower() for h in allowed_hosts or []}

    async def __call__(self, scope: dict, receive: Any, send: Any) -> None:
        if scope["type"] != "http" or scope["path"] in self.public_paths:
            await self.app(scope, receive, send)
            return
        headers = {k.decode("latin-1").lower(): v.decode("latin-1") for k, v in scope.get("headers", [])}
        if self.allowed_hosts:
            host = headers.get("host", "").lower()
            name, _, port = host.rpartition(":") if host.count(":") == 1 else ("", "", "")
            if host not in self.allowed_hosts and not (port.isdigit() and name in self.allowed_hosts):
                log.warning("refused request for host %r on %s", clean_text(host, 80), clean_text(scope["path"], 80))
                await JSONResponse({"error": "invalid host"}, status_code=421)(scope, receive, send)
                return
        if self.tokens.identify(headers.get("authorization")) is None:
            log.warning("refused request without a valid token on %s", clean_text(scope["path"], 80))
            response = JSONResponse(
                {"error": "missing or invalid bearer token"}, status_code=401, headers={"WWW-Authenticate": "Bearer"}
            )
            await response(scope, receive, send)
            return
        await self.app(scope, receive, send)


def build_app(deps: Deps, profile: Profile = "full", host: str = "127.0.0.1",
              allowed_hosts: list[str] | None = None) -> Any:
    """The ASGI app to serve: MCP at /mcp and /healthz; with profile="full" also /approvals and /audit.

    The MCP endpoint is stateless: every request carries its own token and there is no
    server-side session that another caller could reuse.
    """
    server = build_server(deps, profile)
    security = None
    if allowed_hosts:
        security = TransportSecuritySettings(enable_dns_rebinding_protection=True, allowed_hosts=allowed_hosts)
    app = server.streamable_http_app(host=host, transport_security=security, stateless_http=True)
    return Gate(app, deps.tokens, allowed_hosts)


def main() -> None:
    import uvicorn

    parser = argparse.ArgumentParser(description="ERP MCP gateway")
    parser.add_argument("--profile", choices=["read", "full"], default="full")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8101)
    parser.add_argument("--allowed-host", action="append", default=[],
                        help="public hostname(s) this server answers on, e.g. erp-mcp.example.com (repeatable)")
    args = parser.parse_args()
    settings = Settings.from_env(need_write_key=args.profile == "full")
    if settings.uses_placeholders:
        if args.host not in ("127.0.0.1", "localhost", "::1"):
            raise SystemExit("Refusing to listen on a public address with the change-me values from .env.example. "
                             "Set real tokens and ERP keys in .env first.")
        print("WARNING: using the change-me values from .env.example. Fine for a local demo only.", file=sys.stderr)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
    deps = Deps.from_settings(settings, args.profile)
    uvicorn.run(build_app(deps, args.profile, args.host, args.allowed_host or None), host=args.host, port=args.port)


if __name__ == "__main__":
    main()
