"""Command line for the human approver.

    python -m gateway.cli list
    python -m gateway.cli approve chg_1a2b3c4d5e6f
    python -m gateway.cli reject chg_1a2b3c4d5e6f --reason "wrong customer"
    python -m gateway.cli audit --limit 20

Reads GATEWAY_URL and GATEWAY_APPROVER_TOKEN from the environment or from .env.
"""
from __future__ import annotations

import argparse
import os
import sys

import httpx

from .envfile import load_env_file
from .erp_client import clean_text


def show(value: object) -> str:
    """Text from the queue is data written by agents: print it on one line, with control and
    other invisible characters removed, so it cannot imitate another line of output."""
    return clean_text(value, 2000)


def describe_result(change: dict, currency: str) -> str:
    result = change.get("result") or {}
    if change["kind"] == "create_order":
        return f"ERP order #{result.get('id')} created, total {result.get('total', 0) / 100:,.2f} {currency}"
    if change["kind"] == "create_invoice":
        note = " (it already existed)" if result.get("already_existed") else ""
        return f"ERP invoice #{result.get('id')} for order #{result.get('order_id')}, amount {result.get('amount', 0) / 100:,.2f} {currency}{note}"
    if change["kind"] == "update_order":
        return f"ERP order #{result.get('id')} updated, status {result.get('status')}"
    return "done"


def print_change(c: dict, currency: str) -> None:
    when = show(c["requested_at"]).replace("T", " ").replace("+00:00", "")
    print(f"{show(c['id'])}  [{show(c['status'])}]  requested by {show(c['requested_by'])} at {when} UTC")
    print(f"    {show(c['summary'])}")
    payload = c.get("payload") or {}
    if payload.get("notes"):
        print(f"    Notes: {show(payload['notes'])}")
    for w in c.get("warnings") or []:
        print(f"    WARNING: {show(w)}")
    if c["status"] == "approved":
        print(f"    Result: {describe_result(c, currency)}")
    if c.get("error"):
        label = "Rejected because" if c["status"] == "rejected" else "Reason"
        print(f"    {label}: {show(c['error'])}")


def main(argv: list[str] | None = None) -> int:
    load_env_file()
    parser = argparse.ArgumentParser(description="Approve or reject changes proposed by AI agents")
    parser.add_argument("--url", default=os.environ.get("GATEWAY_URL", "http://127.0.0.1:8101"))
    parser.add_argument("--token", default=os.environ.get("GATEWAY_APPROVER_TOKEN"))
    sub = parser.add_subparsers(dest="command", required=True)
    p_list = sub.add_parser("list", help="show changes waiting for approval")
    p_list.add_argument("--status", default="pending",
                        choices=["pending", "approved", "rejected", "failed", "unknown", "applying", "all"])
    sub.add_parser("approve", help="apply a change to the ERP").add_argument("change_id")
    p_reject = sub.add_parser("reject", help="discard a change")
    p_reject.add_argument("change_id")
    p_reject.add_argument("--reason", default="")
    sub.add_parser("audit", help="show the audit log").add_argument("--limit", type=int, default=20)
    args = parser.parse_args(argv)
    if not args.token:
        print("Set GATEWAY_APPROVER_TOKEN in .env or pass --token.", file=sys.stderr)
        return 2
    currency = os.environ.get("ERP_CURRENCY", "GBP")

    try:
        with httpx.Client(base_url=args.url.rstrip("/"), headers={"Authorization": f"Bearer {args.token}"}, timeout=60) as http:
            if args.command == "list":
                resp = http.get("/approvals", params={"status": args.status})
            elif args.command == "approve":
                resp = http.post(f"/approvals/{args.change_id}/approve")
            elif args.command == "reject":
                resp = http.post(f"/approvals/{args.change_id}/reject", json={"reason": args.reason})
            else:
                resp = http.get("/audit", params={"limit": args.limit})
        data = resp.json()
    except (httpx.HTTPError, ValueError) as exc:
        print(f"Could not reach the gateway at {args.url}: {type(exc).__name__}. Is it running?", file=sys.stderr)
        return 1
    if resp.status_code >= 400:
        print(f"Error {resp.status_code}: {show(data.get('error', data))}", file=sys.stderr)
        if resp.status_code == 401:
            print("Check GATEWAY_APPROVER_TOKEN in .env: the gateway does not know this token.", file=sys.stderr)
        return 1

    if args.command == "list":
        if not data["changes"]:
            print("Nothing waiting for approval." if args.status == "pending" else f"No {args.status} changes.")
        for c in data["changes"]:
            print_change(c, currency)
        return 0
    if args.command == "audit":
        for e in reversed(data["entries"]):
            print(f"{show(e['ts']).replace('T', ' ').replace('+00:00', '')}  {show(e['actor']):<18} {show(e['role']):<9} {show(e['action']):<22} "
                  f"{show(e['outcome']):<7} {show(e['arguments'])}")
        return 0
    print_change(data, currency)
    return 0 if data["status"] in ("approved", "rejected") else 1


if __name__ == "__main__":
    raise SystemExit(main())
