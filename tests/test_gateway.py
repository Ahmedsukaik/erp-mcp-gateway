"""Gateway behaviour over a real MCP streamable-HTTP connection."""
import asyncio
import sqlite3
from datetime import date, timedelta

import httpx
import pytest

from agent.order_intake import ToolCallError
from conftest import AGENT, APPROVER, READER
from gateway import approvals
from gateway.config import TokenTable
from gateway.erp_client import ErpReader, ErpUnreachable, ErpWriter
from gateway.server import Deps, build_app, build_server

pytestmark = pytest.mark.anyio

ORDER = {"customer_id": "C001", "lines": [{"sku": "WID-100", "qty": 10}], "reference": "PO-1"}
SOON = (date.today() + timedelta(days=30)).isoformat()


def direct(stack, writer: ErpWriter, change_id: str):
    """Call the approval code directly with a special ERP writer (to simulate ERP trouble)."""
    return approvals.approve(stack.deps.store, writer, ErpReader(stack.erp_url, "erp-read"), change_id, "sara")


def audit(stack) -> list[tuple[str, str, str]]:
    return [(e["actor"], e["action"], e["outcome"]) for e in reversed(stack.deps.store.audit_entries(500))]


# ------------------------------------------------------------------ authentication and roles
async def test_no_token_is_rejected_before_mcp(stack):
    async with httpx.AsyncClient() as http:
        assert (await http.post(f"{stack.gw_url}/mcp", json={})).status_code == 401
        for header in ("Bearer wrong-token-000000000", f"Basic {AGENT}", "bearer", "Bearer x", AGENT, f"Token {AGENT}"):
            bad = await http.post(f"{stack.gw_url}/mcp", json={}, headers={"Authorization": header})
            assert bad.status_code == 401, header
        assert (await http.get(f"{stack.gw_url}/approvals")).status_code == 401
        assert (await http.get(f"{stack.gw_url}/healthz")).status_code == 200


async def test_reader_can_read_but_not_propose_and_denial_is_audited(stack):
    async with stack.mcp(READER) as call:
        assert (await call("check_stock", {"sku": "WID-200"}))["low"] is True
        with pytest.raises(ToolCallError, match="Permission denied"):
            await call("propose_create_order", ORDER)
    assert stack.deps.store.list_changes(None) == []
    assert ("reporting-bot", "propose_create_order", "denied") in audit(stack)


async def test_approver_token_cannot_propose(stack):
    async with stack.mcp(APPROVER) as call:
        assert (await call("check_stock", {"sku": "WID-100"}))["stock"] == 120
        with pytest.raises(ToolCallError, match="Permission denied"):
            await call("propose_create_order", ORDER)
    assert stack.deps.store.list_changes(None) == []


async def test_read_profile_has_no_write_tools_no_approval_routes_and_no_write_key(stack, settings_env, monkeypatch):
    names = {t.name for t in await build_server(stack.deps, profile="read").list_tools()}
    assert names and not any(n.startswith("propose_") for n in names)
    full = {t.name for t in await build_server(stack.deps, profile="full").list_tools()}
    assert {"propose_create_order", "propose_update_order", "propose_create_invoice"} <= full

    from gateway.config import Settings
    monkeypatch.delenv("ERP_WRITE_KEY")  # a read-only gateway does not need the write key at all
    with pytest.raises(SystemExit, match="ERP_WRITE_KEY"):
        Settings.from_env()
    read_deps = Deps.from_settings(Settings.from_env(need_write_key=False), "read")
    assert read_deps.writer is None
    transport = httpx.ASGITransport(app=build_app(read_deps, "read"))
    async with httpx.AsyncClient(transport=transport, base_url="http://127.0.0.1",
                                 headers={"Authorization": f"Bearer {APPROVER}"}) as http:
        assert (await http.get("/healthz")).status_code == 200
        assert (await http.get("/approvals")).status_code == 404
        assert (await http.post("/approvals/chg_x/approve")).status_code == 404
        assert (await http.get("/audit")).status_code == 404


async def test_stdio_transport_needs_an_explicit_identity(stack):
    """Without HTTP headers (local stdio / in-process) there is no token, so the caller's
    role must be set explicitly when the server is built. No identity means no access."""
    from mcp import Client

    from agent.order_intake import mcp_caller
    from gateway.config import Identity

    async with Client(build_server(stack.deps, stdio_identity=Identity("local-reader", "reader"))) as client:
        call = mcp_caller(client)
        assert (await call("check_stock", {"sku": "WID-100"}))["stock"] == 120
        with pytest.raises(ToolCallError, match="Permission denied"):
            await call("propose_create_invoice", {"order_id": 1})
    async with Client(build_server(stack.deps)) as client:
        with pytest.raises(ToolCallError, match="Not authenticated"):
            await mcp_caller(client)("check_stock", {"sku": "WID-100"})
    assert ("unknown", "check_stock", "denied") in audit(stack)


async def test_only_approvers_can_use_the_queue_and_refusals_are_audited(stack):
    async with stack.mcp() as call:
        change_id = (await call("propose_create_order", ORDER))["change_id"]
    for token in (AGENT, READER):
        headers = {"Authorization": f"Bearer {token}"}
        assert httpx.post(f"{stack.gw_url}/approvals/{change_id}/approve", headers=headers).status_code == 403
        assert httpx.post(f"{stack.gw_url}/approvals/{change_id}/reject", headers=headers).status_code == 403
        assert httpx.get(f"{stack.gw_url}/approvals", headers=headers).status_code == 403
        assert httpx.get(f"{stack.gw_url}/audit", headers=headers).status_code == 403
    assert len(stack.orders()) == 1
    assert stack.human.post("/approvals/chg_missing/approve").status_code == 404
    log = audit(stack)
    assert ("order-intake-agent", "approve", "denied") in log and ("reporting-bot", "audit.read", "denied") in log
    assert ("sara (ops)", "approve", "error") in log  # the 404


async def test_public_hostname_allow_list_covers_every_route(stack):
    transport = httpx.ASGITransport(app=build_app(stack.deps, "full", "0.0.0.0", ["erp-mcp.example.com"]))
    auth = {"Authorization": f"Bearer {APPROVER}"}
    async with httpx.AsyncClient(transport=transport, base_url="http://erp-mcp.example.com", headers=auth) as http:
        assert (await http.get("/approvals")).status_code == 200
    async with httpx.AsyncClient(transport=transport, base_url="http://erp-mcp.example.com:8443", headers=auth) as http:
        assert (await http.get("/approvals")).status_code == 200  # the same name with a port is fine
    async with httpx.AsyncClient(transport=transport, base_url="http://evil.example.net", headers=auth) as http:
        assert (await http.get("/approvals")).status_code == 421
        assert (await http.get("/audit")).status_code == 421
        assert (await http.post("/mcp", json={})).status_code == 421
        assert (await http.get("/healthz")).status_code == 200
        for host in ("erp-mcp.example.com.evil.net", "evil-erp-mcp.example.com", "xerp-mcp.example.com",
                     "erp-mcp.example.com:evil.example", "erp-mcp.example.com:80:80", ""):
            assert (await http.get("/audit", headers={"Host": host})).status_code == 421, host


# ------------------------------------------------------------------ read tools
async def test_read_tools_return_live_erp_data(stack):
    async with stack.mcp() as call:
        assert (await call("find_customer", {"query": "northwind"}))["customers"][0]["id"] == "C001"
        assert {p["sku"] for p in (await call("list_low_stock", {}))["products"]} == {"WID-200"}
        unpaid = await call("list_unpaid_invoices", {})
        assert unpaid["count"] == 1 and unpaid["total_outstanding"] == 9000
        assert (await call("find_order", {"order_id": 1}))["reference"] == "PO-7001"
        assert len((await call("list_orders", {"customer_id": "C002"}))["orders"]) == 1
        with pytest.raises(ToolCallError, match="not found"):
            await call("find_order", {"order_id": 999})


async def test_ids_cannot_be_used_to_reach_other_erp_paths(stack):
    async with stack.mcp() as call:
        for sku in ("../orders/1", "../docs", "WID-100?x=1", "WID-100/../../customers", "", "A" * 41):
            with pytest.raises(ToolCallError, match="may only contain"):
                await call("check_stock", {"sku": sku})
        for customer_id in ("../products/WID-100", "C001?x=1", "C001\nX"):
            with pytest.raises(ToolCallError, match="may only contain"):
                await call("propose_create_order", {**ORDER, "customer_id": customer_id})
            with pytest.raises(ToolCallError, match="may only contain"):
                await call("list_orders", {"customer_id": customer_id})
    assert stack.deps.store.list_changes(None) == []


# ------------------------------------------------------------------ propose -> approve
async def test_propose_writes_nothing_until_a_human_approves(stack):
    async with stack.mcp() as call:
        proposal = await call("propose_create_order", {**ORDER, "delivery_date": SOON, "notes": "Leave at gate 2"})
        assert proposal["status"] == "pending" and proposal["warnings"] == []
        assert f"Delivery {SOON}" in proposal["summary"] and "45.00 GBP" in proposal["summary"]
        assert stack.stock("WID-100") == 120 and len(stack.orders()) == 1

        done = stack.approve(proposal["change_id"])
        assert done["status"] == "approved" and done["decided_by"] == "sara (ops)"
        assert stack.stock("WID-100") == 110 and len(stack.orders()) == 2
        order = stack.erp.get(f"/orders/{done['result']['id']}").json()
        assert order["delivery_date"] == SOON and order["notes"] == "Leave at gate 2" and order["reference"] == "PO-1"

        status = await call("get_change_status", {"change_id": proposal["change_id"]})
        assert status["status"] == "approved" and status["result"]["total"] == 4500


async def test_same_reference_is_not_proposed_twice(stack):
    async with stack.mcp() as call:
        first = await call("propose_create_order", ORDER)
        second = await call("propose_create_order", {**ORDER, "reference": "po-1"})  # case does not matter
        assert second["change_id"] == first["change_id"] and second["already_proposed"] is True
        assert second["differs_from_existing"] is True  # only because the reference text differs in case
        same = await call("propose_create_order", ORDER)
        assert same["already_proposed"] is True and same["differs_from_existing"] is False
        revised = await call("propose_create_order", {**ORDER, "lines": [{"sku": "WID-100", "qty": 99}]})
        assert revised["change_id"] == first["change_id"] and revised["differs_from_existing"] is True
        stack.approve(first["change_id"])
        third = await call("propose_create_order", ORDER)  # even after it became an order
        assert third["change_id"] == first["change_id"] and third["status"] == "approved"
    assert len(stack.orders()) == 2 and stack.stock("WID-100") == 110


async def test_double_and_concurrent_approval_create_one_order(stack):
    async with stack.mcp() as call:
        change_id = (await call("propose_create_order", ORDER))["change_id"]
    headers = dict(stack.human.headers)
    async with httpx.AsyncClient(base_url=stack.gw_url, headers=headers) as http:
        results = await asyncio.gather(*[http.post(f"/approvals/{change_id}/approve") for _ in range(5)])
    assert sorted(r.status_code for r in results) == [200, 409, 409, 409, 409]
    assert stack.stock("WID-100") == 110 and len(stack.orders()) == 2


async def test_reject_rules(stack):
    async with stack.mcp() as call:
        change_id = (await call("propose_create_order", ORDER))["change_id"]
        rejected = stack.human.post(f"/approvals/{change_id}/reject", json={"reason": "wrong customer"}).json()
        assert rejected["status"] == "rejected" and rejected["error"] == "wrong customer"
        assert stack.human.post(f"/approvals/{change_id}/approve").status_code == 409
        assert stack.human.post(f"/approvals/{change_id}/reject").status_code == 409
        # the reference is free again after a rejection
        second = (await call("propose_create_order", ORDER))["change_id"]
        assert second != change_id
        assert stack.approve(second)["status"] == "approved"
        # an approved change cannot be rejected afterwards
        assert stack.human.post(f"/approvals/{second}/reject", json={"reason": "too late"}).status_code == 409
        assert stack.deps.store.get_change(second)["status"] == "approved"
    assert len(stack.orders()) == 2


async def test_stock_shortfall_is_warned_then_refused_at_approval(stack):
    async with stack.mcp() as call:
        proposal = await call("propose_create_order", {"customer_id": "C002", "lines": [{"sku": "GAD-010", "qty": 30}]})
        assert "Stock for GAD-010 is 12" in proposal["warnings"][0]
        failed = stack.approve(proposal["change_id"])
        assert failed["status"] == "failed" and "insufficient stock" in failed["error"] and "Nothing was written" in failed["error"]
        assert stack.stock("GAD-010") == 12 and len(stack.orders()) == 1
        assert (await call("get_change_status", {"change_id": proposal["change_id"]}))["status"] == "failed"


async def test_stock_is_rechecked_at_approval_time(stack):
    async with stack.mcp() as call:
        proposal = await call("propose_create_order", {"customer_id": "C001", "lines": [{"sku": "GAD-010", "qty": 10}]})
        assert proposal["warnings"] == []
    # Someone else takes the stock before the human approves.
    stack.erp_admin.post("/orders", json={"customer_id": "C003", "lines": [{"sku": "GAD-010", "qty": 5}]})
    assert stack.approve(proposal["change_id"])["status"] == "failed" and stack.stock("GAD-010") == 7


async def test_price_change_after_proposal_blocks_the_write(stack, tmp_path):
    async with stack.mcp() as call:
        proposal = await call("propose_create_order", ORDER)
    assert "45.00 GBP" in proposal["summary"]
    with sqlite3.connect(tmp_path / "erp.db") as conn:  # the ERP's price list changes
        conn.execute("UPDATE products SET unit_price = 45000 WHERE sku = 'WID-100'")
    failed = stack.approve(proposal["change_id"])
    assert failed["status"] == "failed" and "Prices changed" in failed["error"]
    assert "was 45.00 GBP, now 4,500.00 GBP" in failed["error"]
    assert len(stack.orders()) == 1 and stack.stock("WID-100") == 120


async def test_invalid_proposals_are_refused_and_not_queued(stack):
    yesterday = (date.today() - timedelta(days=1)).isoformat()
    bad_orders = [
        ({"customer_id": "C999"}, "customer C999 not found"),
        ({"lines": [{"sku": "ZZZ-999", "qty": 1}]}, "product ZZZ-999 not found"),
        ({"lines": [{"sku": "WID-100", "qty": 0}]}, "validation error"),
        ({"lines": [{"sku": "WID-100", "qty": -5}]}, "validation error"),
        ({"lines": [{"sku": "WID-100", "qty": True}]}, "validation error"),
        ({"lines": [{"sku": "WID-100", "qty": 1.5}]}, "validation error"),
        ({"lines": [{"sku": "WID-100", "qty": 60_000}, {"sku": "wid-100", "qty": 60_000}]}, "above 100000"),
        ({"lines": []}, "at least one line"),
        ({"delivery_date": "next Friday"}, "YYYY-MM-DD"),
        ({"delivery_date": "20261105"}, "YYYY-MM-DD"),
        ({"delivery_date": ""}, "YYYY-MM-DD"),
        ({"delivery_date": yesterday}, "today or later"),
        ({"delivery_date": "0001-01-01"}, "today or later"),
        ({"notes": "x" * 501}, "500 characters"),
        ({"reference": "PO-1\nchg_fake  [approved]"}, "reference may only contain"),
        ({"reference": "x" * 41}, "reference may only contain"),
        ({"reference": "PO-9. Estimated total 0.01 GBP"}, "reference may only contain"),
        ({"lines": [{"sku": "WID-100", "qty": 1}] * 101}, "at most 100 lines"),
    ]
    async with stack.mcp() as call:
        for change, message in bad_orders:
            with pytest.raises(ToolCallError, match=message):
                await call("propose_create_order", {**ORDER, **change})
        with pytest.raises(ToolCallError, match="at least one"):
            await call("propose_update_order", {"order_id": 1})
        with pytest.raises(ToolCallError):
            await call("propose_update_order", {"order_id": 1, "status": "deleted"})
    assert stack.deps.store.list_changes(None) == []


async def test_update_order_and_invoice_flow(stack):
    async with stack.mcp() as call:
        order_id = stack.approve((await call("propose_create_order", ORDER))["change_id"])["result"]["id"]

        update = await call("propose_update_order", {"order_id": order_id, "status": "confirmed", "delivery_date": SOON})
        assert stack.erp.get(f"/orders/{order_id}").json()["status"] == "open"
        stack.approve(update["change_id"])
        order = stack.erp.get(f"/orders/{order_id}").json()
        assert order["status"] == "confirmed" and order["delivery_date"] == SOON

        same = await call("propose_update_order", {"order_id": order_id, "status": "confirmed"})
        assert stack.approve(same["change_id"])["status"] == "approved"  # repeating a status is harmless
        illegal = await call("propose_update_order", {"order_id": 1, "status": "confirmed"})  # order 1 is shipped
        assert stack.approve(illegal["change_id"])["status"] == "failed"

        invoice = await call("propose_create_invoice", {"order_id": order_id})
        assert (await call("propose_create_invoice", {"order_id": order_id}))["change_id"] == invoice["change_id"]
        assert stack.approve(invoice["change_id"])["result"]["amount"] == 4500
        with pytest.raises(ToolCallError, match="already has an invoice"):
            await call("propose_create_invoice", {"order_id": 1})


# ------------------------------------------------------------------ outages and lost answers
class LostAnswerWriter(ErpWriter):
    """The ERP does the work, but its answer never arrives (timeout, dropped connection)."""

    async def create_order(self, payload, idempotency_key):
        await super().create_order(payload, idempotency_key)
        raise ErpUnreachable("ReadTimeout")

    async def create_invoice(self, order_id):
        await super().create_invoice(order_id)
        raise ErpUnreachable("ReadTimeout")


async def test_lost_answer_never_creates_a_duplicate_order(stack):
    async with stack.mcp() as call:
        change_id = (await call("propose_create_order", ORDER))["change_id"]
        lossy = LostAnswerWriter(stack.erp_url, "erp-write")
        lost = await direct(stack, lossy, change_id)
        assert lost["status"] == "unknown" and "may or may not have been written" in lost["error"]
        assert len(stack.orders()) == 2  # the ERP did write it

        # It cannot be rejected while the outcome is unknown, and proposing again changes nothing.
        refused = stack.human.post(f"/approvals/{change_id}/reject", json={"reason": "try again"})
        assert refused.status_code == 409 and "unknown outcome" in refused.json()["error"]
        assert (await call("propose_create_order", ORDER))["change_id"] == change_id

        # Approving again finds the order that already exists.
        assert stack.approve(change_id)["status"] == "approved"
    assert len(stack.orders()) == 2 and stack.stock("WID-100") == 110


async def test_same_reference_reuses_the_erp_idempotency_key_across_changes(stack):
    """Belt and braces: even if an order reached the ERP and its change was then removed from
    the queue, a new proposal for the same customer + reference maps to the same ERP key."""
    async with stack.mcp() as call:
        first = (await call("propose_create_order", ORDER))["change_id"]
        lossy = LostAnswerWriter(stack.erp_url, "erp-write")
        await direct(stack, lossy, first)
        stack.deps.store.finish(first, "rejected", error="forced by the test")  # not possible through the API
        second = (await call("propose_create_order", ORDER))["change_id"]
        assert second != first
        assert stack.approve(second)["status"] == "approved"
        assert len(stack.orders()) == 2 and stack.stock("WID-100") == 110

        # And if the new proposal is a DIFFERENT order, the gateway says so instead of
        # reporting the old ERP order as the new one.
        stack.deps.store.finish(second, "rejected", error="forced by the test")
        third = (await call("propose_create_order", {**ORDER, "lines": [{"sku": "WID-100", "qty": 25}]}))["change_id"]
        clash = stack.approve(third)
        assert clash["status"] == "failed" and "already holds a different order" in clash["error"]
    assert len(stack.orders()) == 2 and stack.stock("WID-100") == 110


async def test_after_a_write_attempt_nothing_can_claim_that_nothing_was_written(stack, tmp_path):
    """A lost answer leaves the change `unknown`. If prices then change, or the ERP is briefly
    unreadable, a retry must not turn it into `failed` (which could then be rejected while the
    ERP holds the order)."""
    async with stack.mcp() as call:
        change_id = (await call("propose_create_order", ORDER))["change_id"]
    assert (await direct(stack, LostAnswerWriter(stack.erp_url, "erp-write"), change_id))["status"] == "unknown"
    assert stack.deps.store.get_change(change_id)["write_attempted"] is True
    with sqlite3.connect(tmp_path / "erp.db") as conn:  # prices move while the outcome is unknown
        conn.execute("UPDATE products SET unit_price = 45000 WHERE sku = 'WID-100'")

    dead_reader = ErpReader("http://127.0.0.1:9", "erp-read", timeout=1)  # reads fail, the write path works
    again = await approvals.approve(stack.deps.store, ErpWriter(stack.erp_url, "erp-write"), dead_reader, change_id, "sara")
    assert again["status"] == "approved" and again["result"]["total"] == 4500  # the order written at the approved price
    assert len(stack.orders()) == 2 and stack.stock("WID-100") == 110


async def test_a_crash_inside_the_approval_never_leaves_a_change_stuck(stack):
    class ExplodingWriter(ErpWriter):
        async def create_order(self, payload, idempotency_key):
            raise RuntimeError("boom")

    async with stack.mcp() as call:
        change_id = (await call("propose_create_order", ORDER))["change_id"]
    crashed = await direct(stack, ExplodingWriter(stack.erp_url, "erp-write"), change_id)
    assert crashed["status"] == "unknown" and "boom" not in crashed["error"]
    assert stack.approve(change_id)["status"] == "approved" and len(stack.orders()) == 2


async def test_erp_outage_before_the_write_is_a_clean_failure_and_can_be_retried(stack):
    async with stack.mcp() as call:
        change_id = (await call("propose_create_order", ORDER))["change_id"]
    dead = ErpWriter("http://127.0.0.1:9", "erp-write", timeout=1)
    lost = await direct(stack, dead, change_id)
    assert lost["status"] == "unknown"  # we cannot tell a refused connection from a lost answer
    assert stack.approve(change_id)["status"] == "approved"
    assert len(stack.orders()) == 2 and stack.stock("WID-100") == 110


async def test_lost_answer_on_invoice_is_resolved_on_retry(stack):
    async with stack.mcp() as call:
        order_id = stack.approve((await call("propose_create_order", ORDER))["change_id"])["result"]["id"]
        change_id = (await call("propose_create_invoice", {"order_id": order_id}))["change_id"]
    lossy = LostAnswerWriter(stack.erp_url, "erp-write")
    assert (await direct(stack, lossy, change_id))["status"] == "unknown"
    done = stack.approve(change_id)
    assert done["status"] == "approved" and done["result"]["already_existed"] is True
    assert len([i for i in stack.erp.get("/invoices").json() if i["order_id"] == order_id]) == 1


async def test_garbled_or_internal_erp_answers_do_not_leak_and_do_not_get_stuck(stack):
    async with stack.mcp() as call:
        change_id = (await call("propose_create_order", ORDER))["change_id"]

        def broken_erp(request: httpx.Request) -> httpx.Response:
            if request.url.path == "/orders":
                return httpx.Response(500, text="Traceback ... password=hunter2")
            return httpx.Response(200, text="<html>not json</html>")

        writer = ErpWriter("http://erp.invalid", "k")
        writer._client = httpx.AsyncClient(base_url="http://erp.invalid", transport=httpx.MockTransport(broken_erp))
        lost = await direct(stack, writer, change_id)
        assert lost["status"] == "unknown" and "hunter2" not in lost["error"] and "HTTP 500" in lost["error"]
        status = await call("get_change_status", {"change_id": change_id})
        assert "hunter2" not in str(status)

        writer._client = httpx.AsyncClient(
            base_url="http://erp.invalid", transport=httpx.MockTransport(lambda r: httpx.Response(201, text="OK"))
        )
        garbled = await direct(stack, writer, change_id)
        assert garbled["status"] == "unknown" and "not JSON" in garbled["error"]
    assert stack.approve(change_id)["status"] == "approved"  # not stuck in 'applying'


async def test_stale_applying_change_can_be_retried_but_a_fresh_one_cannot(stack, tmp_path):
    async with stack.mcp() as call:
        change_id = (await call("propose_create_order", ORDER))["change_id"]
    store = stack.deps.store
    store.claim(change_id, "sara")  # the gateway "dies" right after claiming
    assert stack.human.post(f"/approvals/{change_id}/approve").status_code == 409
    assert stack.human.post(f"/approvals/{change_id}/reject").status_code == 409
    with sqlite3.connect(tmp_path / "gateway.db") as conn:
        conn.execute("UPDATE pending_changes SET decided_at = '2020-01-01T00:00:00+00:00' WHERE id = ?", (change_id,))
    assert stack.approve(change_id)["status"] == "approved"
    assert len(stack.orders()) == 2


# ------------------------------------------------------------------ audit log
async def test_audit_log_covers_good_bad_and_malformed_calls(stack):
    async with stack.mcp() as call:
        await call("check_stock", {"sku": "WID-100"})
        change_id = (await call("propose_create_order", ORDER))["change_id"]
        for name, args in [("check_stock", {"sku": "NOPE-1"}), ("check_stock", {}), ("no_such_tool", {"a": 1}),
                           ("propose_create_order", {**ORDER, "lines": [{"sku": "WID-100", "qty": 0}]})]:
            with pytest.raises(ToolCallError):
                await call(name, args)
    stack.human.get("/approvals")
    stack.approve(change_id)
    stack.human.post(f"/approvals/{change_id}/reject")
    entries = list(reversed(stack.human.get("/audit").json()["entries"]))
    assert [(e["actor"], e["action"], e["kind"], e["outcome"]) for e in entries] == [
        ("order-intake-agent", "check_stock", "read", "ok"),
        ("order-intake-agent", "propose_create_order", "propose", "ok"),
        ("order-intake-agent", "check_stock", "read", "error"),
        ("order-intake-agent", "check_stock", "read", "error"),
        ("order-intake-agent", "no_such_tool", "unknown", "error"),
        ("order-intake-agent", "propose_create_order", "propose", "error"),
        ("sara (ops)", "approvals.list", "approve", "ok"),
        ("sara (ops)", "approve", "approve", "ok"),
        ("sara (ops)", "reject", "approve", "error"),
    ]
    assert '"reference": "PO-1"' in entries[1]["arguments"] and entries[1]["detail"] == change_id
    assert ("sara (ops)", "audit.read", "ok") in audit(stack)


async def test_long_orders_are_fully_recorded_in_the_audit_log(stack):
    lines = [{"sku": "WID-100", "qty": 1}] * 99 + [{"sku": "BOX-050", "qty": 7}]
    async with stack.mcp() as call:
        await call("propose_create_order", {**ORDER, "lines": lines})
    assert '"sku": "BOX-050", "qty": 7' in stack.deps.store.audit_entries(1)[0]["arguments"]


def test_token_table_rejects_weak_config():
    with pytest.raises(ValueError):
        TokenTable({"short": {"name": "x", "role": "agent"}})
    with pytest.raises(ValueError):
        TokenTable({"long-enough-token-000000": {"name": "x", "role": "admin"}})
    table = TokenTable({"long-enough-token-000000": {"name": "x", "role": "agent"}})
    assert table.identify("Bearer long-enough-token-000000").role == "agent"
    assert table.identify("Basic long-enough-token-000000") is None and table.identify(None) is None


async def test_erp_internal_errors_never_reach_the_model_or_the_audit_log(stack):
    from mcp import Client

    from agent.order_intake import mcp_caller
    from gateway.config import Identity

    reader = ErpReader("http://erp.invalid", "k")
    reader._client = httpx.AsyncClient(
        base_url="http://erp.invalid",
        transport=httpx.MockTransport(lambda r: httpx.Response(500, text="Traceback ... password=hunter2")),
    )
    deps = Deps(reader, None, stack.deps.store, stack.deps.tokens)
    async with Client(build_server(deps, "read", stdio_identity=Identity("local", "reader"))) as client:
        with pytest.raises(ToolCallError) as caught:
            await mcp_caller(client)("check_stock", {"sku": "WID-100"})
    assert "internal error" in str(caught.value) and "hunter2" not in str(caught.value)
    assert "hunter2" not in str(stack.deps.store.audit_entries(5))


async def test_one_huge_argument_cannot_hide_the_others_in_the_audit_log(stack):
    async with stack.mcp() as call:
        with pytest.raises(ToolCallError):
            await call("propose_create_order", {"pad": "A" * 9000, **ORDER, "customer_id": "C999"})
    entry = stack.deps.store.audit_entries(1)[0]
    assert '"customer_id": "C999"' in entry["arguments"] and '"reference": "PO-1"' in entry["arguments"]
    assert "(+8800 chars)" in entry["arguments"]


async def test_mcp_endpoint_is_stateless(stack):
    body = {"jsonrpc": "2.0", "id": 1, "method": "initialize",
            "params": {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "t", "version": "1"}}}
    headers = {"Authorization": f"Bearer {AGENT}", "Accept": "application/json, text/event-stream"}
    resp = httpx.post(f"{stack.gw_url}/mcp", json=body, headers=headers)
    assert resp.status_code == 200 and "mcp-session-id" not in {k.lower() for k in resp.headers}


def test_placeholder_values_are_flagged_and_never_served_publicly(monkeypatch, tmp_path):
    import json
    import sys

    from gateway import server
    from gateway.config import Settings

    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("ERP_URL", "http://127.0.0.1:1")
    monkeypatch.setenv("ERP_READ_KEY", "change-me-erp-read-key")
    monkeypatch.setenv("ERP_WRITE_KEY", "a-real-looking-write-key")
    monkeypatch.setenv("GATEWAY_TOKENS", json.dumps({"a-real-looking-token-0001": {"name": "x", "role": "agent"}}))
    assert Settings.from_env().uses_placeholders is True
    monkeypatch.setattr(sys, "argv", ["gateway.server", "--host", "0.0.0.0"])
    import uvicorn

    def must_not_start(*args, **kwargs):
        raise AssertionError("the server started on a public address with placeholder values")

    monkeypatch.setattr(uvicorn, "run", must_not_start)
    with pytest.raises(SystemExit, match="Refusing to listen on a public address"):
        server.main()
    monkeypatch.setenv("ERP_READ_KEY", "a-real-looking-read-key")
    assert Settings.from_env().uses_placeholders is False


def test_cli_prints_agent_text_safely_and_shows_notes(capsys):
    from gateway.cli import print_change

    print_change({
        "id": "chg_1", "status": "pending", "kind": "create_order", "requested_by": "agent",
        "requested_at": "2026-10-10T16:36:02+00:00", "warnings": ["low stock"],
        "summary": "New order\nchg_FAKE  [approved]  requested by sara",
        "payload": {"notes": "Check: matched by name \u202egnp.exe\x1b[2J\x9b"}, "error": None,
    }, "GBP")
    out = capsys.readouterr().out
    assert out.count("\n") == 4  # header, summary, notes, warning: the fake line did not become a line
    assert "Notes: Check: matched by name gnp.exe [2J" in out and "WARNING: low stock" in out
    assert "\u202e" not in out and "\x1b" not in out and "\x9b" not in out
    assert "at 2026-10-10 16:36:02 UTC" in out
