"""The live web demo: every request rebuilds a private stack and replays the visitor's clicks."""
from __future__ import annotations

import httpx
import pytest

from web.demo_app import MAX_HISTORY, app

pytestmark = pytest.mark.anyio

CLEAR, SHORT, INJECTED = (
    {"type": "email", "sample": s} for s in ("01_clear_order", "02_more_than_stock", "03_unknown_sender")
)


async def run(history: list[dict], action: dict | None = None) -> httpx.Response:
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://demo") as client:
        return await client.post("/api/run", json={"history": history, "action": action})


async def state(history: list[dict], action: dict | None = None) -> dict:
    resp = await run(history, action)
    assert resp.status_code == 200, resp.text
    return resp.json()


def stock(s: dict, sku: str) -> int:
    return next(p["stock"] for p in s["products"] if p["sku"] == sku)


async def test_fresh_session_has_seed_data_and_nothing_queued():
    s = await state([])
    assert s["last"] is None and s["changes"] == [] and s["audit"] == []
    assert stock(s, "WID-100") == 120 and len(s["orders"]) == 1
    assert {t["name"] for t in s["tools"] if t["kind"] == "propose"} == {
        "propose_create_order", "propose_update_order", "propose_create_invoice"}


async def test_email_is_proposed_and_the_erp_is_untouched_until_approval():
    s = await state([], CLEAR)
    assert s["last"]["result"]["status"] == "proposed" and s["last"]["result"]["change"] == 1
    assert s["changes"][0]["status"] == "pending"
    assert stock(s, "WID-100") == 120 and len(s["orders"]) == 1

    s = await state([CLEAR], {"type": "approve", "change": 1})
    assert s["last"]["status"] == "approved" and s["changes"][0]["decided_by"] == "sara (ops)"
    assert stock(s, "WID-100") == 80 and len(s["orders"]) == 2


async def test_approving_twice_creates_one_order():
    s = await state([CLEAR, {"type": "approve", "change": 1}], {"type": "approve", "change": 1})
    assert s["last"]["http_status"] == 409
    assert len(s["orders"]) == 2


async def test_replay_gives_the_same_change_ids_every_time():
    first = await state([CLEAR], SHORT)
    second = await state([CLEAR], SHORT)
    assert [c["id"] for c in first["changes"]] == [c["id"] for c in second["changes"]]
    assert len({c["id"] for c in first["changes"]}) == 2


async def test_sessions_do_not_share_state():
    await state([], CLEAR)
    assert (await state([]))["changes"] == []


async def test_stock_shortage_fails_at_approval_and_writes_nothing():
    s = await state([SHORT], {"type": "approve", "change": 1})
    assert s["last"]["status"] == "failed" and "insufficient stock" in s["last"]["error"]
    assert not [o for o in s["orders"] if o["reference"] == "HC-2291"]


async def test_injected_instruction_is_not_followed():
    s = await state([], INJECTED)
    assert s["last"]["result"]["status"] == "needs_human"
    assert s["changes"] == [] and len(s["orders"]) == 1


async def test_reject_records_the_reason():
    s = await state([CLEAR], {"type": "reject", "change": 1, "reason": "duplicate of a phone order"})
    assert s["changes"][0]["status"] == "rejected" and s["changes"][0]["error"] == "duplicate of a phone order"


async def test_probes_are_refused_and_audited():
    s = await state([CLEAR], {"type": "probe", "probe": "no_token"})
    assert s["last"]["http_status"] == 401
    s = await state([CLEAR], {"type": "probe", "probe": "reader_propose"})
    assert "Permission denied" in s["last"]["error"]
    assert any(a["action"] == "propose_create_order" and a["outcome"] == "denied" for a in s["audit"])
    s = await state([CLEAR], {"type": "probe", "probe": "agent_approve"})
    assert s["last"]["http_status"] == 403 and s["changes"][0]["status"] == "pending"
    assert any(a["action"] == "approve" and a["outcome"] == "denied" for a in s["audit"])


async def test_own_email_text_goes_through_the_same_checks():
    text = "From: orders@northwind.example\nSubject: PO 50117\n\nHi team,\n\nPO 50117\n12 x WID-200\n\nThanks,\nPriya"
    s = await state([], {"type": "email", "text": text})
    assert s["last"]["result"]["status"] == "proposed"
    assert "12 x WID-200" in s["changes"][0]["summary"]


@pytest.mark.parametrize(
    "history, action, status",
    [
        ([], {"type": "email", "sample": "no-such-email"}, 400),
        ([], {"type": "approve", "change": 1}, 400),
        ([], {"type": "email", "text": "x" * 4001}, 422),
        ([CLEAR] * (MAX_HISTORY + 1), None, 422),
        ([], {"type": "drop_tables"}, 422),
    ],
)
async def test_bad_requests_are_refused(history, action, status):
    assert (await run(history, action)).status_code == status


async def test_page_and_samples_are_served():
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://demo") as client:
        page = await client.get("/")
        samples = (await client.get("/api/samples")).json()
    assert page.status_code == 200 and "ERP MCP Gateway" in page.text
    assert [s["id"] for s in samples][:1] == ["01_clear_order"] and len(samples) == 5
