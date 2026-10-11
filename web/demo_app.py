"""Live web demo: the same ERP, gateway and agent as demo.py, behind a web page.

Each request builds a private stack (stand-in ERP + MCP gateway, with throwaway SQLite
files), replays the visitor's earlier clicks, applies the new one, and returns the state.
Nothing is kept on the server between requests, so visitors never see each other's data,
nobody can break the demo for anyone else, and reloading the page starts again.

The parts talk over real HTTP and real MCP (Streamable HTTP), carried in-process by
httpx's ASGI transport instead of network sockets. Emails are read by the strict
rule-based reader; no AI model is called here.

Run locally:  uvicorn web.demo_app:app --port 8200   (page at http://127.0.0.1:8200)
"""
from __future__ import annotations

import contextvars
import hashlib
import logging
import shutil
import tempfile
import uuid
from contextlib import asynccontextmanager
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, AsyncIterator, Literal

import httpx
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

import gateway.store as gateway_store
from agent.extract import RuleBasedExtractor
from agent.order_intake import ToolCall, ToolCallError, mcp_caller, process_order_email
from demo import AGENT, APPROVER, READER, TOKENS
from erp.app import create_app
from gateway.config import TokenTable
from gateway.erp_client import ErpReader, ErpWriter
from gateway.server import Deps, build_app
from gateway.store import Store

for noisy in ("httpx", "mcp"):  # one log line per in-process request is noise here
    logging.getLogger(noisy).setLevel(logging.WARNING)

ROOT = Path(__file__).resolve().parent.parent
ERP_BASE = "http://erp.internal"
GATEWAY_BASE = "http://127.0.0.1:8101"  # a loopback Host passes the MCP SDK's DNS-rebinding check
ERP_READ, ERP_WRITE = "web-demo-erp-read", "web-demo-erp-write"
MAX_HISTORY = 40

SAMPLES: dict[str, str] = {
    p.stem: p.read_text(encoding="utf-8") for p in sorted((ROOT / "samples" / "emails").glob("*.txt"))
}

# ---------------------------------------------------------------- repeatable change ids
# Change ids are random in the gateway. The demo rebuilds its state on every click, so inside
# a demo request they are numbered instead: the same clicks always give the same ids.
_change_counter: contextvars.ContextVar[list[int] | None] = contextvars.ContextVar("demo_change_counter", default=None)


class _NumberedIds:
    def uuid4(self) -> uuid.UUID:
        counter = _change_counter.get()
        if counter is None:
            return uuid.uuid4()
        counter[0] += 1
        return uuid.UUID(hashlib.sha256(f"web-demo-change-{counter[0]}".encode()).hexdigest()[:32])


gateway_store.uuid = _NumberedIds()  # type: ignore[assignment]


# ---------------------------------------------------------------- the stack
@dataclass
class Stack:
    deps: Deps
    gateway: Any  # ASGI app
    erp: httpx.AsyncClient  # ERP with the read key, for showing state
    change_ids: list[str] = field(default_factory=list)  # in the order the agent proposed them

    def gateway_client(self, token: str | None) -> httpx.AsyncClient:
        headers = {"Authorization": f"Bearer {token}"} if token else {}
        return httpx.AsyncClient(
            transport=httpx.ASGITransport(app=self.gateway), base_url=GATEWAY_BASE, headers=headers, timeout=30
        )

    @asynccontextmanager
    async def mcp(self, token: str) -> AsyncIterator[ToolCall]:
        from mcp import Client
        from mcp.client.streamable_http import streamable_http_client

        async with self.gateway_client(token) as http:
            async with Client(streamable_http_client(f"{GATEWAY_BASE}/mcp", http_client=http)) as client:
                yield mcp_caller(client)

    def change_id(self, number: int) -> str:
        if not 1 <= number <= len(self.change_ids):
            raise HTTPException(400, f"There is no change #{number}.")
        return self.change_ids[number - 1]


@asynccontextmanager
async def demo_stack() -> AsyncIterator[Stack]:
    workdir = tempfile.mkdtemp(prefix="erp-mcp-web-")
    erp_transport = httpx.ASGITransport(app=create_app(f"{workdir}/erp.db", ERP_READ, ERP_WRITE))

    def erp_client(key: str, timeout: float) -> httpx.AsyncClient:
        return httpx.AsyncClient(transport=erp_transport, base_url=ERP_BASE, headers={"X-API-Key": key}, timeout=timeout)

    # Same classes as production, with their HTTP client pointed at the in-process transport.
    # __init__ is skipped because it builds a network client (and its TLS setup) we would not use.
    reader, writer = ErpReader.__new__(ErpReader), ErpWriter.__new__(ErpWriter)
    reader._client, writer._client = erp_client(ERP_READ, 10), erp_client(ERP_WRITE, 15)
    deps = Deps(reader=reader, writer=writer, store=Store(f"{workdir}/gateway.db"), tokens=TokenTable(TOKENS))
    gateway = build_app(deps, "full")
    erp = erp_client(ERP_READ, 10)
    try:
        async with gateway.app.router.lifespan_context(gateway.app):  # starts the MCP session manager
            yield Stack(deps=deps, gateway=gateway, erp=erp)
    finally:
        for client in (erp, reader, writer):
            await client.aclose()
        shutil.rmtree(workdir, ignore_errors=True)


# ---------------------------------------------------------------- actions
class Action(BaseModel):
    type: Literal["email", "approve", "reject", "probe"]
    sample: str | None = Field(default=None, max_length=60)
    text: str | None = Field(default=None, max_length=4000)
    change: int | None = Field(default=None, ge=1, le=MAX_HISTORY)
    reason: str | None = Field(default=None, max_length=200)
    probe: Literal["no_token", "reader_propose", "agent_approve"] | None = None


class RunRequest(BaseModel):
    history: list[Action] = Field(default_factory=list, max_length=MAX_HISTORY)
    action: Action | None = None


async def apply(stack: Stack, action: Action, agent: ToolCall) -> dict[str, Any]:
    if action.type == "email":
        if action.text is not None:
            text = action.text
        elif action.sample in SAMPLES:
            text = SAMPLES[action.sample]
        else:
            raise HTTPException(400, "Unknown sample email.")
        result = await process_order_email(text, agent, RuleBasedExtractor())
        if result.change_id and result.change_id not in stack.change_ids:
            stack.change_ids.append(result.change_id)
        out = asdict(result)
        out.pop("extracted", None)
        if result.change_id:
            out["change"] = stack.change_ids.index(result.change_id) + 1
        return {"type": "email", "sample": action.sample, "result": out}

    if action.type in ("approve", "reject"):
        if action.change is None:
            raise HTTPException(400, "Say which change.")
        change_id = stack.change_id(action.change)
        async with stack.gateway_client(APPROVER) as human:
            if action.type == "approve":
                resp = await human.post(f"/approvals/{change_id}/approve")
            else:
                resp = await human.post(f"/approvals/{change_id}/reject", json={"reason": action.reason or ""})
        body = resp.json()
        return {"type": action.type, "change": action.change, "http_status": resp.status_code,
                "status": body.get("status"), "error": body.get("error")}

    # probes: show the doors that stay shut
    if action.probe == "no_token":
        async with stack.gateway_client(None) as anon:
            resp = await anon.post("/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
        return {"type": "probe", "probe": "no_token", "http_status": resp.status_code, "error": resp.json().get("error")}
    if action.probe == "reader_propose":
        async with stack.mcp(READER) as reader:
            try:
                await reader("propose_create_order", {"customer_id": "C001", "lines": [{"sku": "WID-100", "qty": 1}]})
                error = None
            except ToolCallError as exc:
                error = str(exc)
        return {"type": "probe", "probe": "reader_propose", "error": error}
    if action.probe == "agent_approve":
        target = stack.change_ids[0] if stack.change_ids else "chg_000000000000"
        async with stack.gateway_client(AGENT) as agent_http:
            resp = await agent_http.post(f"/approvals/{target}/approve")
        return {"type": "probe", "probe": "agent_approve", "http_status": resp.status_code, "error": resp.json().get("error")}
    raise HTTPException(400, "Unknown action.")


async def snapshot(stack: Stack) -> dict[str, Any]:
    changes = []
    for number, change_id in enumerate(stack.change_ids, start=1):
        c = stack.deps.store.get_change(change_id)
        if c:
            changes.append({
                "number": number, "id": c["id"], "status": c["status"], "summary": c["summary"],
                "warnings": c["warnings"], "error": c["error"], "result": c["result"],
                "requested_by": c["requested_by"], "decided_by": c["decided_by"],
            })
    products = (await stack.erp.get("/products")).json()
    orders = (await stack.erp.get("/orders")).json()
    audit = stack.deps.store.audit_entries(80)
    return {"changes": changes, "products": products, "orders": orders, "audit": audit}


_tools_cache: list[dict[str, str]] = []


async def tool_list(stack: Stack) -> list[dict[str, str]]:
    if not _tools_cache:
        from mcp import Client
        from mcp.client.streamable_http import streamable_http_client

        async with stack.gateway_client(AGENT) as http:
            async with Client(streamable_http_client(f"{GATEWAY_BASE}/mcp", http_client=http)) as client:
                listed = await client.list_tools()
        # Replaced in one step, so two first visitors at the same moment cannot list the tools twice.
        _tools_cache[:] = [
            {"name": t.name, "kind": "read" if t.annotations and t.annotations.read_only_hint else "propose"}
            for t in listed.tools
        ]
    return _tools_cache


def _inner(exc: BaseException, kind: type[BaseException]) -> BaseException | None:
    """Find an exception of `kind`, also inside the exception groups that MCP's task groups raise."""
    if isinstance(exc, kind):
        return exc
    for inner in getattr(exc, "exceptions", ()):
        found = _inner(inner, kind)
        if found is not None:
            return found
    return None


async def run(req: RunRequest) -> dict[str, Any]:
    reset = _change_counter.set([0])
    try:
        async with demo_stack() as stack:
            async with stack.mcp(AGENT) as agent:
                for past in req.history:
                    await apply(stack, past, agent)
                last = await apply(stack, req.action, agent) if req.action else None
            tools = await tool_list(stack)
            # Reading the tool list is a tools/list call, not a tools/call, so it adds no audit entry.
            return {"last": last, "tools": tools, **await snapshot(stack)}
    except Exception as exc:
        refused = _inner(exc, HTTPException)  # a bad click, raised inside the MCP session
        if refused is not None and refused is not exc:
            raise refused from None
        raise
    finally:
        _change_counter.reset(reset)


# ---------------------------------------------------------------- web app
app = FastAPI(title="ERP MCP Gateway live demo", docs_url=None, redoc_url=None, openapi_url=None)


@app.get("/api/samples")
def samples() -> list[dict[str, str]]:
    out = []
    for name, text in SAMPLES.items():
        headers = dict(
            line.split(":", 1) for line in text.split("\n\n", 1)[0].splitlines() if ":" in line
        )
        out.append({"id": name, "from": headers.get("From", "").strip(),
                    "subject": headers.get("Subject", "").strip(), "text": text})
    return out


@app.post("/api/run")
async def run_route(req: RunRequest) -> dict[str, Any]:
    return await run(req)


@app.get("/api/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/")
def index() -> FileResponse:
    return FileResponse(ROOT / "public" / "index.html")
