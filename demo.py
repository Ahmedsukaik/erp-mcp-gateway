"""End-to-end demo in one command:  python demo.py   (add --pause to stop after each scene)

Starts the stand-in ERP and the MCP gateway on local ports with throwaway databases,
then walks through the story: an agent reads order emails, proposes orders, a human
approves, and every step lands in the audit log. No API key is needed (emails are read
by strict rules); pass --claude to use the Claude API for that step instead.
"""
from __future__ import annotations

import asyncio
import logging
import os
import socket
import sys
import tempfile
import textwrap
import threading
import time
from pathlib import Path

import httpx
import uvicorn

from agent.extract import ClaudeExtractor, RuleBasedExtractor
from agent.order_intake import ToolCallError, mcp_caller, process_order_email
from erp.app import create_app
from gateway.config import TokenTable
from gateway.envfile import load_env_file
from gateway.erp_client import ErpReader, ErpWriter
from gateway.server import Deps, build_app
from gateway.store import Store

ROOT = Path(__file__).parent
READER, AGENT, APPROVER = "demo-reader-token-0000001", "demo-agent-token-00000001", "demo-approver-token-000001"
TOKENS = {
    READER: {"name": "reporting-bot", "role": "reader"},
    AGENT: {"name": "order-intake-agent", "role": "agent"},
    APPROVER: {"name": "sara (ops)", "role": "approver"},
}
PAUSE = "--pause" in sys.argv
BOLD = sys.stdout.isatty() and (os.name != "nt" or "WT_SESSION" in os.environ)


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def serve(app, port: int) -> uvicorn.Server:
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="error"))
    threading.Thread(target=server.run, daemon=True).start()
    for _ in range(200):
        if server.started:
            return server
        time.sleep(0.05)
    raise RuntimeError("server did not start")


def start_stack(workdir: str) -> tuple[str, str, Deps]:
    """Start ERP + gateway. Returns (erp_url, gateway_url, deps)."""
    erp_port, gw_port = free_port(), free_port()
    erp_url, gw_url = f"http://127.0.0.1:{erp_port}", f"http://127.0.0.1:{gw_port}"
    serve(create_app(f"{workdir}/erp.db", "demo-erp-read", "demo-erp-write"), erp_port)
    deps = Deps(
        reader=ErpReader(erp_url, "demo-erp-read"),
        writer=ErpWriter(erp_url, "demo-erp-write"),
        store=Store(f"{workdir}/gateway.db"),
        tokens=TokenTable(TOKENS),
    )
    serve(build_app(deps, "full"), gw_port)
    return erp_url, gw_url, deps


def say(title: str) -> None:
    if PAUSE and not title.startswith("1."):
        try:
            input("\n(press Enter for the next scene) ")
        except EOFError:  # no keyboard attached: just carry on
            print()
    print(f"\n\033[1m=== {title} ===\033[0m" if BOLD else f"\n=== {title} ===")


def out(label: str, text: object = "") -> None:
    """Print 'label text', wrapped so long lines stay readable in a recording."""
    print(textwrap.fill(f"{label} {text}".rstrip(), width=100, subsequent_indent="    "))


async def main(use_claude: bool) -> None:
    from mcp import Client
    from mcp.client.streamable_http import create_mcp_http_client, streamable_http_client

    for noisy in ("httpx", "httpx2", "mcp", "gateway"):
        logging.getLogger(noisy).setLevel(logging.ERROR)
    load_env_file()
    if use_claude and not os.environ.get("ANTHROPIC_API_KEY"):
        raise SystemExit("--claude needs ANTHROPIC_API_KEY (set it in .env or in your shell).")
    extractor = ClaudeExtractor() if use_claude else RuleBasedExtractor()
    workdir = tempfile.mkdtemp(prefix="erp-mcp-demo-")
    erp_url, gw_url, _ = start_stack(workdir)
    erp = httpx.Client(base_url=erp_url, headers={"X-API-Key": "demo-erp-read"})
    human = httpx.Client(base_url=gw_url, headers={"Authorization": f"Bearer {APPROVER}"})
    reader_name = "Claude API" if use_claude else "strict rules, no AI model"

    def stock(sku: str) -> int:
        return erp.get(f"/products/{sku}").json()["stock"]

    def email(name: str) -> str:
        return (ROOT / "samples" / "emails" / name).read_text(encoding="utf-8")

    say("1. No token, no entry")
    out("POST /mcp without a token ->", f"HTTP {httpx.post(f'{gw_url}/mcp', json={}).status_code}")

    say("2. A read-only token can read, but cannot propose")
    async with create_mcp_http_client(headers={"Authorization": f"Bearer {READER}"}) as http:
        async with Client(streamable_http_client(f"{gw_url}/mcp", http_client=http)) as client:
            call = mcp_caller(client)
            unpaid = await call("list_unpaid_invoices", {})
            out("reporting-bot: list_unpaid_invoices ->",
                f"{unpaid['count']} unpaid, {unpaid['total_outstanding'] / 100:.2f} GBP outstanding")
            try:
                await call("propose_create_order", {"customer_id": "C001", "lines": [{"sku": "WID-100", "qty": 1}]})
            except ToolCallError as exc:
                out("reporting-bot: propose_create_order ->", exc)

    async with create_mcp_http_client(headers={"Authorization": f"Bearer {AGENT}"}) as http:
        async with Client(streamable_http_client(f"{gw_url}/mcp", http_client=http)) as client:
            call = mcp_caller(client)
            tools = await client.list_tools()
            say("3. Tools the agent sees")
            for t in tools.tools:
                kind = "read   " if t.annotations and t.annotations.read_only_hint else "propose"
                print(f"  [{kind}] {t.name}")

            say("4. Email 1: a clear order -> the agent proposes, the ERP is untouched")
            out("(emails in this run are read by:", f"{reader_name})")
            before = stock("WID-100")
            r1 = await process_order_email(email("01_clear_order.txt"), call, extractor)
            out("agent:", f"{r1.status} | {r1.change_id}")
            out("      ", r1.summary)
            out("ERP stock of WID-100:", f"{before} before, {stock('WID-100')} now (unchanged, waiting for a human)")

            say("5. A human approves -> now the order exists")
            done = human.post(f"/approvals/{r1.change_id}/approve").json()
            out(f"sara: approve {r1.change_id} ->",
                f"{done['status']}, ERP order #{done['result']['id']}, total {done['result']['total'] / 100:.2f} GBP")
            out("ERP stock of WID-100:", stock("WID-100"))
            again = human.post(f"/approvals/{r1.change_id}/approve")
            out("sara: approve the same change again ->", f"HTTP {again.status_code}, {again.json()['error']}")
            r1b = await process_order_email(email("01_clear_order.txt"), call, extractor)
            out("agent: the same email processed again ->", f"{r1b.status} | {r1b.change_id} (no second order)")

            say("6. Email 2: more than we have in stock -> warning, then the ERP refuses")
            r2 = await process_order_email(email("02_more_than_stock.txt"), call, extractor)
            out("agent:", f"{r2.status} | {r2.change_id}")
            for w in r2.warnings:
                out("       WARNING:", w)
            failed = human.post(f"/approvals/{r2.change_id}/approve").json()
            out("sara: approve anyway ->", f"{failed['status']}: {failed['error']}")
            orders = [o for o in erp.get("/orders", params={"customer_id": "C002"}).json() if o["reference"] == "HC-2291"]
            out("ERP orders with reference HC-2291:", len(orders))

            say("7. Email 3: unknown sender -> nothing is proposed")
            r3 = await process_order_email(email("03_unknown_sender.txt"), call, extractor)
            out("agent:", f"{r3.status} |")
            for issue in r3.issues:
                out("       -", issue)

            say("8. Emails 4 and 5: unknown product, unclear quantity -> flagged for a person")
            for name in ("04_unknown_product.txt", "05_unclear_quantity.txt"):
                r = await process_order_email(email(name), call, extractor)
                out(f"agent ({name}):", f"{r.status} |")
                for issue in r.issues:
                    out("       -", issue)

    say("9. The audit log: who called what, with which arguments, and the outcome")
    for e in reversed(human.get("/audit", params={"limit": 200}).json()["entries"]):
        args = e["arguments"] if len(e["arguments"]) <= 42 else e["arguments"][:39] + "..."
        print(f"  {e['ts'][11:19]}  {e['actor']:<19} {e['action']:<21} {e['outcome']:<7} {args}")
    print(f"\nDatabases for this run are in {workdir}")


if __name__ == "__main__":
    try:
        asyncio.run(main(use_claude="--claude" in sys.argv))
    except Exception as exc:
        if "--claude" not in sys.argv:
            raise
        # Most likely the API call itself: a wrong key, no credit, or no network.
        while getattr(exc, "exceptions", None):  # unwrap an exception group to the real cause
            exc = exc.exceptions[0]
        raise SystemExit(f"The run with --claude failed: {type(exc).__name__}: {str(exc)[:300]}")
