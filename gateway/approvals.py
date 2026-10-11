"""Applying (or rejecting) a queued change. This is the only code path that writes to the ERP."""
from __future__ import annotations

from .erp_client import ErpError, ErpReader, ErpUnreachable, ErpWriter
from .store import Store

UNKNOWN = (
    "The ERP did not give a usable answer ({why}), so this change may or may not have been written. "
    "Approve it again: that is safe and will not create a duplicate."
)


def money(amount: int, currency: str) -> str:
    return f"{amount / 100:,.2f} {currency}"


def _same_order(result: dict, payload: dict) -> bool:
    got = sorted((line["sku"], line["qty"]) for line in result.get("lines", []))
    want = sorted((line["sku"], line["qty"]) for line in payload["lines"])
    return got == want and result.get("customer_id") == payload["customer_id"]


async def approve(store: Store, writer: ErpWriter, reader: ErpReader, change_id: str, approver: str,
                  currency: str = "GBP") -> dict:
    """Apply one approved change to the ERP and record what happened.

    Outcomes:
      approved  written (or found already written)
      failed    the ERP refused, or a check before the write stopped it; nothing was written
      unknown   no usable answer after a write was attempted; approving again is safe

    Once a write has been attempted, the change can only become `failed` if the ERP itself
    refuses it. Nothing on our side may then claim "nothing was written".
    """
    change = store.claim(change_id, approver)  # raises KeyError / AlreadyDecided
    payload, meta, kind = change["payload"], change["meta"], change["kind"]
    attempted = change["write_attempted"]
    try:
        if kind == "create_order" and not attempted:
            # The approver agreed to a total. If prices moved since then, stop before writing.
            try:
                total = 0
                for line in payload["lines"]:
                    total += line["qty"] * (await reader.product(line["sku"]))["unit_price"]
            except ErpError as exc:
                return store.finish(change_id, "failed", error=f"The ERP refused the change: {exc.detail}. Nothing was written.")
            except ErpUnreachable:
                return store.finish(change_id, "failed", error="The ERP could not be reached. Nothing was written. Approve again later.")
            expected = meta.get("expected_total")
            if expected is not None and total != expected:
                return store.finish(
                    change_id, "failed",
                    error=f"Prices changed since this was proposed (was {money(expected, currency)}, "
                          f"now {money(total, currency)}). Nothing was written. Reject it and propose again.",
                )
        if kind not in ("create_order", "update_order", "create_invoice"):
            return store.finish(change_id, "failed", error=f"Unknown change kind {kind!r}. Nothing was written.")

        store.mark_write_attempted(change_id)
        try:
            if kind == "create_order":
                result = await writer.create_order(payload, idempotency_key=meta["idempotency_key"])
                if not _same_order(result, payload):
                    return store.finish(
                        change_id, "failed",
                        error=f"The ERP already holds a different order for this customer and reference "
                              f"(order #{result.get('id')}). Nothing new was written. A person must compare the two.",
                    )
            elif kind == "update_order":
                body = {k: v for k, v in payload.items() if k != "order_id" and v is not None}
                result = await writer.update_order(payload["order_id"], body)
            else:
                try:
                    result = await writer.create_invoice(payload["order_id"])
                except ErpError as exc:
                    if exc.status != 409:
                        raise
                    # A retry after a lost answer lands here: the invoice may already exist.
                    existing = [i for i in await reader.invoices() if i["order_id"] == payload["order_id"]]
                    if not existing:
                        raise
                    result = {**existing[0], "already_existed": True}
        except ErpError as exc:
            if exc.refused:
                return store.finish(change_id, "failed", error=f"The ERP refused the change: {exc.detail}. Nothing was written.")
            return store.finish(change_id, "unknown", error=UNKNOWN.format(why=f"HTTP {exc.status}"))
        except ErpUnreachable as exc:
            return store.finish(change_id, "unknown", error=UNKNOWN.format(why=str(exc)))
    except Exception as exc:  # never leave a change stuck in 'applying'
        return store.finish(change_id, "unknown", error=UNKNOWN.format(why=f"gateway error {type(exc).__name__}"))
    return store.finish(change_id, "approved", result=result)


def reject(store: Store, change_id: str, approver: str, reason: str) -> dict:
    return store.reject(change_id, approver, reason or "no reason given")
