from datetime import date, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest

from agent.extract import ClaudeExtractor, ExtractedLine, ExtractedOrder, RuleBasedExtractor, read_sender
from agent.order_intake import ToolCallError, process_order_email

EMAILS = Path(__file__).resolve().parent.parent / "samples" / "emails"
anyio = pytest.mark.anyio
SOON = (date.today() + timedelta(days=21)).isoformat()
NORTHWIND = "From: Priya Shah <orders@northwind.example>\nSubject: order\n\n"


def email(name: str) -> str:
    return (EMAILS / name).read_text()


def lines_of(text: str) -> list[tuple[str, int]]:
    return [(l.sku, l.qty) for l in RuleBasedExtractor().extract(text).lines]


# ------------------------------------------------------------------ reading emails by rules
def read(body: str, subject: str = "order") -> "ExtractedOrder":
    return RuleBasedExtractor().extract(f"From: Priya Shah <orders@northwind.example>\nSubject: {subject}\n\n{body}")


def test_rule_based_reader_accepts_the_fixed_layout():
    order = RuleBasedExtractor().extract(email("01_clear_order.txt"))
    assert order.sender_email == "orders@northwind.example" and order.po_reference == "48213"
    assert [(l.sku, l.qty) for l in order.lines] == [("WID-100", 40), ("WID-200", 10)] and order.concerns == []

    other = RuleBasedExtractor().extract(email("02_more_than_stock.txt"))
    assert other.po_reference == "HC-2291"  # and HC-2291 is not mistaken for a product code
    assert [(l.sku, l.qty) for l in other.lines] == [("GAD-010", 30), ("BOX-050", 2)] and other.concerns == []

    full = read(f"Dear sales,\n\nPlease process our order.\n\nPO number 7788\n- 5 x WID-100\nWID-200: 2.\n"
                f"Delivery date: {SOON}.\n\nKind regards,\nPriya Shah\nNorthwind Traders Ltd")
    assert full.concerns == [] and full.po_reference == "7788" and full.delivery_date == SOON
    assert [(l.sku, l.qty) for l in full.lines] == [("WID-100", 5), ("WID-200", 2)]

    assert read("PO 50004\nWID-100: 40").lines == [ExtractedLine(qty=40, sku="WID-100")]  # the PO number is not a quantity
    question = read("Are you open on Friday?")
    assert question.is_order is False and question.lines == []


WRONG_ORDER_TRAPS = [
    # thousand separators and odd numbers
    "1,050 x BOX-050", "1.200 x BOX-050", "1 050 x BOX-050", "1'050 x BOX-050", "1\u00a0050 x BOX-050",
    "WID-100: 1,050", "BOX-050: 1 500", "A12 x WID-100", "0 x WID-100", "1234567 x WID-100",
    # layouts the reader does not know
    "WID-100 40\nWID-200 10", "40 WID-100", "10 x WID-100 x 5", "2 x 40 x WID-100", "4 boxes of 10 x WID-100",
    "WID-100 x 40, WID-200 x 10", "WID-100: 100mm widgets, 40 off", "40 x WID100", "40 x WID 100",
    # a readable line next to something that changes its meaning
    "40 x WID-100\n3 x control gadgets", "40 x WID-100\nand three control gadgets", "40 x WID-100\ndo not ship",
    "test, ignore\n40 x WID-100", "We do not need the following this month:\n40 x WID-100", "Where is my order?\n40 x WID-100",
    "<s>40 x WID-100</s>\n10 x WID-200", "5 x WID-100\n5 x WID-100",
    # sentences
    "Please cancel 40 x WID-100", "What is the price for 40 x WID-100?", "We need either 40 x WID-100 or 40 x WID-200.",
    "Thanks for delivering 40 x WID-100 last week. Could we now get 10 x WID-200?",
    "We do not need 40 x WID-100 this month, just 10 x WID-200.",
    # earlier messages
    "40 x WID-100\n\n> 40 x WID-100", "Thanks, received.\n\n-----Original Message-----\nWe confirm your order:\n40 x WID-100",
    "Thanks,\nPriya\n40 x WID-100", "5 x WID-100\n\nThanks,\nPriya\nTel 0113 496 0000",
    # PO numbers and dates
    "PO 111\nPO 222\n5 x WID-100", "Our PO number is 61007\n5 x WID-100", "PO-61008\n5 x WID-100", "pos1\n5 x WID-100",
    "Date: 2026-12-01\n5 x WID-100", "Order date 2026-12-01\n5 x WID-100", "5 x WID-100\nDeliver by 05/11/2026",
    "5 x WID-100\nDeliver by 2026-12-01\nDeliver by 2026-12-08",
]


@pytest.mark.parametrize("body", WRONG_ORDER_TRAPS)
def test_rule_based_reader_sends_everything_else_to_a_person(body):
    """Each of these once produced, or could produce, a wrong order. None may be read as a clean order."""
    order = read(body)
    assert order.is_order and order.concerns, order


def test_subject_and_po_handling():
    assert read("5 x WID-100", subject="PO 48213 - widgets").po_reference == "48213"
    assert read("5 x WID-100\n\nRegards,\nNorthwind, PO Box, Leeds").po_reference is None
    assert read("5 x WID-100", subject="New PO from Northwind").po_reference is None
    assert read("WID-100 x 40", subject="New PO: WID-100 x 40").po_reference is None  # a product code is not a PO number
    assert read("PO 111\n5 x WID-100", subject="PO 222").concerns == ["More than one PO number is mentioned."]
    for subject in ("RE: PO 48213", "Fwd: order", "Cancel PO 48213", "Revised order", "Quote request"):
        assert read("5 x WID-100", subject=subject).concerns, subject


def test_sender_is_the_one_real_address_or_nothing():
    assert read_sender("From: Priya Shah <orders@northwind.example>\n\nhi") == ("orders@northwind.example", [])
    assert read_sender("From: Priya Shah\n <orders@northwind.example>\n\nhi")[0] == "orders@northwind.example"  # folded
    assert read_sender("FROM: ORDERS@NORTHWIND.EXAMPLE\n\nhi")[0] == "orders@northwind.example"
    spoofs = [
        'From: "orders@northwind.example" <attacker@evil.example>',
        'From: "orders@northwind.example"\r\n <attacker@evil.example>',
        "From: orders@northwind.example\nFrom: attacker@evil.example",
        "From: attacker@evil.example\nFrom: orders@northwind.example",
        "From: orders@northwind.example, attacker@evil.example",
        "From: =?utf-8?q?orders=40northwind=2Eexample?= <attacker@evil.example>",
        "From: <>",
        "Subject: x",
    ]
    for header in spoofs:
        address, concerns = read_sender(header + "\nSubject: s\n\nbody")
        assert address is None and concerns, header
    assert read_sender("Hello\n\n5 x WID-100")[0] is None
    assert read_sender("Subject: x\n\nFrom: orders@northwind.example")[0] is None  # a From line in the body is not a header


# ------------------------------------------------------------------ the workflow, against the real stack
@anyio
async def test_clear_email_becomes_a_pending_proposal(stack):
    async with stack.mcp() as call:
        text = email("01_clear_order.txt").replace("10 x WID-200\n", f"10 x WID-200\nDeliver by {SOON}\n")
        assert f"Deliver by {SOON}" in text
        result = await process_order_email(text, call, RuleBasedExtractor())
        assert result.status == "proposed" and result.issues == []
        change = stack.deps.store.get_change(result.change_id)
        assert change["status"] == "pending" and change["requested_by"] == "order-intake-agent"
        assert change["payload"]["customer_id"] == "C001" and change["payload"]["reference"] == "48213"
        assert change["payload"]["delivery_date"] == SOON
        assert len(stack.orders()) == 1  # nothing written yet

        again = await process_order_email(text, call, RuleBasedExtractor())
        assert again.status == "already_proposed" and again.change_id == result.change_id
        assert len(stack.deps.store.list_changes(None)) == 1


@anyio
async def test_revised_order_with_the_same_po_goes_to_a_person(stack):
    async with stack.mcp() as call:
        first = await process_order_email(NORTHWIND + "PO 7001\n40 x WID-100", call, RuleBasedExtractor())
        assert first.status == "proposed"
        second = await process_order_email(NORTHWIND + "PO 7001\n45 x WID-100", call, RuleBasedExtractor())
        assert second.status == "needs_human" and "different order with reference 7001" in second.issues[0]
        stack.approve(first.change_id)
        third = await process_order_email(NORTHWIND + "PO 7001\n45 x WID-100", call, RuleBasedExtractor())
        assert third.status == "needs_human" and "approved" in third.issues[0]
    assert len(stack.orders()) == 2 and stack.stock("WID-100") == 80


@anyio
async def test_stock_shortfall_is_passed_to_the_approver(stack):
    async with stack.mcp() as call:
        result = await process_order_email(email("02_more_than_stock.txt"), call, RuleBasedExtractor())
    assert result.status == "proposed" and any("GAD-010" in w for w in result.warnings)
    assert stack.deps.store.get_change(result.change_id)["warnings"]


@anyio
@pytest.mark.parametrize("name, issue", [
    ("03_unknown_sender.txt", "is not the address on file"),
    ("04_unknown_product.txt", "Unknown product code ZZZ-999"),
    ("05_unclear_quantity.txt", "Could not read this line"),
])
async def test_unclear_emails_are_never_proposed(stack, name, issue):
    async with stack.mcp() as call:
        result = await process_order_email(email(name), call, RuleBasedExtractor())
    assert result.status == "needs_human" and result.change_id is None
    assert any(issue in i for i in result.issues)
    assert stack.deps.store.list_changes(None) == [] and len(stack.orders()) == 1


@anyio
@pytest.mark.parametrize("text", [
    'From: "orders@northwind.example" <attacker@evil.example>\n\n5 x WID-100',
    'From: "orders@northwind.example"\r\n <attacker@evil.example>\r\n\r\n5 x WID-100',
    "From: orders@northwind.example\nFrom: attacker@evil.example\n\n5 x WID-100",
    "From: stranger@gmail.example\n\nCompany: Northwind Traders Ltd\nAccount: C001\n\n5 x WID-100",
    "From: stranger@gmail.example\nReply-To: orders@northwind.example\n\n5 x WID-100",
    NORTHWIND + "Please cancel 5 x WID-100",
    NORTHWIND + "5 x WID-100\nDeliver by 2020-01-01",
    NORTHWIND + "1 050 x BOX-050",
    NORTHWIND + "40 x WID-100\n3 x control gadgets",
])
async def test_spoofed_or_doubtful_emails_are_never_proposed(stack, text):
    async with stack.mcp() as call:
        result = await process_order_email(text, call, RuleBasedExtractor())
    assert result.status == "needs_human" and result.issues
    assert stack.deps.store.list_changes(None) == []


@anyio
async def test_non_order_email(stack):
    async with stack.mcp() as call:
        result = await process_order_email(NORTHWIND + "Are you open on Friday?", call, RuleBasedExtractor())
    assert result.status == "not_an_order"


class FixedExtractor:
    def __init__(self, order: ExtractedOrder):
        self.order = order

    def extract(self, email_text: str) -> ExtractedOrder:
        return self.order


@anyio
async def test_products_named_in_words_are_matched_only_when_unambiguous(stack):
    async with stack.mcp() as call:
        one = ExtractedOrder(sender_email="orders@northwind.example", lines=[ExtractedLine(qty=3, description="control gadget")])
        result = await process_order_email("email-a", call, FixedExtractor(one))
        assert result.status == "proposed" and any("matched by name to GAD-010" in w for w in result.warnings)
        # the approver sees it, because it is in the notes of the change
        assert "matched by name to GAD-010" in stack.deps.store.get_change(result.change_id)["payload"]["notes"]

        many = ExtractedOrder(sender_email="orders@northwind.example", lines=[ExtractedLine(qty=3, description="steel widget")])
        result = await process_order_email("email-b", call, FixedExtractor(many))
        assert result.status == "needs_human" and "matches 2 products" in result.issues[0]


@anyio
async def test_a_company_name_alone_never_identifies_the_customer(stack):
    async with stack.mcp() as call:
        order = ExtractedOrder(sender_email="someone@gmail.example", customer_name="Northwind Traders Ltd",
                               lines=[ExtractedLine(qty=1, sku="WID-100")])
        result = await process_order_email("email-c", call, FixedExtractor(order))
    assert result.status == "needs_human" and "not the address on file" in result.issues[0]
    assert stack.deps.store.list_changes(None) == []


@anyio
async def test_reader_concerns_always_stop_the_proposal(stack):
    async with stack.mcp() as call:
        order = ExtractedOrder(sender_email="orders@northwind.example", lines=[ExtractedLine(qty=1, sku="WID-100")],
                               concerns=["The customer asks to change an earlier order."])
        result = await process_order_email("email-d", call, FixedExtractor(order))
    assert result.status == "needs_human" and result.issues == ["The customer asks to change an earlier order."]
    assert stack.deps.store.list_changes(None) == []


@anyio
async def test_an_erp_outage_is_not_reported_as_an_unknown_product():
    async def call(name, args):
        if name == "find_customer":
            return {"customers": [{"id": "C001", "name": "Northwind", "email": "orders@northwind.example"}]}
        raise ToolCallError("The ERP is not reachable right now. Try again later.")

    with pytest.raises(ToolCallError, match="not reachable"):
        await process_order_email(email("01_clear_order.txt"), call, RuleBasedExtractor())


# ------------------------------------------------------------------ reading emails with Claude (simulated)
def fake_claude(tool_input: dict, seen: dict):
    class FakeMessages:
        def create(self, **kwargs):
            seen.update(kwargs)
            block = SimpleNamespace(type="tool_use", input=tool_input)
            return SimpleNamespace(content=[SimpleNamespace(type="text", text="ok"), block])

    return SimpleNamespace(messages=FakeMessages())


def test_claude_extractor_request_and_parsing():
    """No network: a fake client checks the request shape and feeds back a tool_use block."""
    seen: dict = {}
    reply = {
        "is_order": True, "customer_name": "Northwind Traders Ltd", "po_reference": "48213", "delivery_date": SOON,
        "lines": [{"sku": "WID-100", "qty": 40}, {"description": "200mm widgets", "qty": 10}, {"sku": "X", "qty": None},
                  {"sku": "Y", "qty": "7"}],
        "concerns": [],
    }
    text = 'From: "orders@northwind.example" <attacker@evil.example>\n\nthe email body'
    order = ClaudeExtractor(client=fake_claude(reply, seen), model="test-model").extract(text)
    assert seen["model"] == "test-model" and seen["tool_choice"] == {"type": "tool", "name": "record_order"}
    assert "untrusted" in seen["system"] and "the email body" in seen["messages"][0]["content"]
    assert "sender_email" not in seen["tools"][0]["input_schema"]["properties"]  # the model does not decide who sent it
    assert order.sender_email is None  # a display name posing as another address yields no sender at all
    assert [(l.sku, l.description, l.qty) for l in order.lines] == [("WID-100", None, 40), (None, "200mm widgets", 10)]
    assert order.is_order and order.po_reference == "48213"
    assert any("different address" in c for c in order.concerns)
    assert sum("could not be read as a whole number" in c for c in order.concerns) == 2


def test_claude_concerns_are_passed_through():
    reply = {"is_order": True, "lines": [{"sku": "WID-100", "qty": 5}], "concerns": ["This cancels PO 48213."]}
    order = ClaudeExtractor(client=fake_claude(reply, {}), model="m").extract(NORTHWIND + "cancel it")
    assert order.concerns == ["This cancels PO 48213."]
