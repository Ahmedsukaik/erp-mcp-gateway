"""Order-intake agent: email in, *proposed* order out.

The workflow is ordinary code. The language model is used for one step only (reading
the email), which keeps running cost low and behaviour predictable. Every ERP action
goes through the MCP gateway, and nothing is written until a human approves.

    python -m agent.order_intake samples/emails/01_clear_order.txt \
        [--extractor claude]        (reads GATEWAY_URL and GATEWAY_AGENT_TOKEN from .env)
"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import re
from dataclasses import asdict, dataclass, field
from datetime import date
from pathlib import Path
from typing import Any, Awaitable, Callable

from .extract import ClaudeExtractor, ExtractedOrder, Extractor, RuleBasedExtractor

ToolCall = Callable[[str, dict[str, Any]], Awaitable[dict[str, Any]]]


class ToolCallError(Exception):
    """A gateway tool returned an error."""


@dataclass
class IntakeResult:
    # proposed          a new change is waiting for a human approver
    # already_proposed  the gateway already holds a change for this customer + reference
    # needs_human       the email was not understood with certainty; nothing was proposed
    # not_an_order      no order lines in the email
    status: str
    change_id: str | None = None
    summary: str | None = None
    issues: list[str] = field(default_factory=list)  # reasons a person must look
    warnings: list[str] = field(default_factory=list)  # shown to the approver
    extracted: dict[str, Any] | None = None


async def process_order_email(email_text: str, call: ToolCall, extractor: Extractor) -> IntakeResult:
    order: ExtractedOrder = extractor.extract(email_text)
    result = IntakeResult(status="needs_human", extracted=asdict(order))
    if not order.is_order or (not order.lines and not order.concerns):
        result.status = "not_an_order"
        result.issues.append("No order lines found in this email.")
        return result
    result.issues.extend(order.concerns)  # anything the reader was unsure about goes to a person

    # 1. Who is ordering? Only the address in the From header counts. A company name written
    #    in the email is never enough, because anyone can type one.
    customer = None
    if order.sender_email:
        matches = (await call("find_customer", {"query": order.sender_email}))["customers"]
        exact = [c for c in matches if c["email"].lower() == order.sender_email.lower()]
        if len(exact) == 1:
            customer = exact[0]
    if customer is None:
        result.issues.append(f"Sender {order.sender_email or 'unknown'} is not the address on file for any customer.")

    # 2. What are they ordering? Never guess a product.
    lines: list[dict[str, Any]] = []
    for line in order.lines:
        if line.qty <= 0:
            result.issues.append(f"Invalid quantity {line.qty} for {line.sku or line.description}.")
            continue
        sku = line.sku.strip().upper() if line.sku else None
        if sku is None and line.description:
            found = (await call("search_products", {"query": line.description}))["products"]
            if len(found) == 1:
                sku = found[0]["sku"]
                result.warnings.append(f"'{line.description}' was matched by name to {sku}.")
            else:
                result.issues.append(f"'{line.description}' matches {len(found)} products; a person must choose.")
                continue
        if sku is None:
            result.issues.append("An order line has no product.")
            continue
        try:
            await call("check_stock", {"sku": sku})
        except ToolCallError as exc:
            if "not found" not in str(exc) and "may only contain" not in str(exc):
                raise  # an outage is not an unknown product; let the caller retry later
            result.issues.append(f"Unknown product code {sku}.")
            continue
        lines.append({"sku": sku, "qty": line.qty})

    delivery_date = None
    if order.delivery_date:
        try:
            delivery_date = date.fromisoformat(order.delivery_date).isoformat()
            if date.fromisoformat(delivery_date) < date.today():
                result.issues.append(f"The requested delivery date {delivery_date} is in the past.")
        except ValueError:
            result.issues.append(f"The delivery date '{order.delivery_date}' is not a valid date.")

    if result.issues or customer is None or not lines:
        return result  # needs_human: nothing is proposed from a partly understood email

    # 3. Propose. The reference makes re-processing the same email harmless.
    reference = order.po_reference or "EMAIL-" + hashlib.sha256(email_text.encode()).hexdigest()[:10].upper()
    notes = "Created from email by order-intake agent."
    if result.warnings:
        notes += " Check: " + " ".join(result.warnings)
    try:
        proposal = await call(
            "propose_create_order",
            {
                "customer_id": customer["id"],
                "lines": lines,
                "reference": reference,
                "delivery_date": delivery_date,
                "notes": notes[:500],
            },
        )
    except ToolCallError as exc:
        if "not reachable" in str(exc) or "internal error" in str(exc).lower():
            raise
        result.issues.append(f"The gateway did not accept the order: {exc}")
        return result
    result.change_id = proposal["change_id"]
    result.summary = proposal["summary"]
    result.warnings.extend(proposal.get("warnings", []))
    if proposal.get("already_proposed"):
        result.status = "already_proposed"
        if proposal.get("differs_from_existing"):
            result.status = "needs_human"
            result.issues.append(
                f"A different order with reference {reference} already exists for this customer "
                f"({proposal['change_id']}, {proposal['status']}). This may be a revised order; a person must decide."
            )
        else:
            result.warnings.append(f"This order was already proposed ({proposal['change_id']}, {proposal['status']}).")
        return result
    result.status = "proposed"
    return result


# ---------------------------------------------------------------- MCP plumbing
def tool_payload(result: Any) -> dict[str, Any]:
    """Read a tool result: structured content when present, otherwise the JSON text block."""
    text = " ".join(getattr(block, "text", "") for block in result.content or [])
    if result.is_error:
        raise ToolCallError(re.sub(r"^Error executing tool \w+: ", "", text))
    if result.structured_content is not None:
        return result.structured_content
    return json.loads(text)


def mcp_caller(client: Any) -> ToolCall:
    async def call(name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        return tool_payload(await client.call_tool(name, arguments))

    return call


async def run(email_path: Path, gateway_url: str, token: str, extractor: Extractor) -> IntakeResult:
    from mcp import Client
    from mcp.client.streamable_http import create_mcp_http_client, streamable_http_client

    async with create_mcp_http_client(headers={"Authorization": f"Bearer {token}"}) as http:
        async with Client(streamable_http_client(gateway_url, http_client=http)) as client:
            return await process_order_email(email_path.read_text(encoding="utf-8"), mcp_caller(client), extractor)


def main() -> None:
    from gateway.envfile import load_env_file

    load_env_file()
    parser = argparse.ArgumentParser(description="Turn an order email into a proposed ERP order")
    parser.add_argument("email", type=Path)
    parser.add_argument("--gateway", default=os.environ.get("GATEWAY_URL", "http://127.0.0.1:8101").rstrip("/") + "/mcp")
    parser.add_argument("--token", default=os.environ.get("GATEWAY_AGENT_TOKEN"), help="gateway token with the 'agent' role")
    parser.add_argument("--extractor", choices=["rules", "claude"], default="rules")
    args = parser.parse_args()
    if not args.token:
        parser.error("set GATEWAY_AGENT_TOKEN in .env or pass --token")
    extractor: Extractor = ClaudeExtractor() if args.extractor == "claude" else RuleBasedExtractor()
    result = asyncio.run(run(args.email, args.gateway, args.token, extractor))
    print(json.dumps(asdict(result), indent=2))


if __name__ == "__main__":
    main()
