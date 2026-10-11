"""Gateway settings and the token -> identity table.

Roles
-----
reader    read tools only
agent     read tools + propose_* tools (never writes to the ERP directly)
approver  a human: may list, approve and reject pending changes, and read the audit log
"""
from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass
from typing import Literal

from .envfile import load_env_file

Role = Literal["reader", "agent", "approver"]
Permission = Literal["read", "propose", "approve"]

ROLE_PERMISSIONS: dict[str, frozenset[str]] = {
    "reader": frozenset({"read"}),
    "agent": frozenset({"read", "propose"}),
    "approver": frozenset({"read", "approve"}),
}


@dataclass(frozen=True)
class Identity:
    name: str
    role: Role

    def can(self, permission: Permission) -> bool:
        return permission in ROLE_PERMISSIONS[self.role]


def _digest(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


class TokenTable:
    """Maps bearer tokens to identities. Only SHA-256 digests are kept in memory."""

    def __init__(self, tokens: dict[str, dict[str, str]]):
        self._by_digest: dict[str, Identity] = {}
        for token, info in tokens.items():
            if len(token) < 16:
                raise ValueError("gateway tokens must be at least 16 characters")
            role = info.get("role")
            if role not in ROLE_PERMISSIONS:
                raise ValueError(f"unknown role {role!r} for token of {info.get('name')!r}")
            self._by_digest[_digest(token)] = Identity(name=info["name"], role=role)  # type: ignore[arg-type]

    def identify(self, authorization_header: str | None) -> Identity | None:
        if not authorization_header:
            return None
        scheme, _, token = authorization_header.partition(" ")
        if scheme.lower() != "bearer" or not token.strip():
            return None
        return self._by_digest.get(_digest(token.strip()))


@dataclass(frozen=True)
class Settings:
    erp_url: str
    erp_read_key: str
    erp_write_key: str
    db_path: str
    tokens: TokenTable
    currency: str = "GBP"
    uses_placeholders: bool = False  # still running on the change-me values from .env.example

    @classmethod
    def from_env(cls, need_write_key: bool = True) -> "Settings":
        load_env_file()
        required = ["ERP_URL", "ERP_READ_KEY", "GATEWAY_TOKENS"] + (["ERP_WRITE_KEY"] if need_write_key else [])
        missing = [k for k in required if not os.environ.get(k)]
        if missing:
            raise SystemExit(f"Missing settings: {', '.join(missing)}. Copy .env.example to .env and edit it.")
        return cls(
            erp_url=os.environ["ERP_URL"].rstrip("/"),
            erp_read_key=os.environ["ERP_READ_KEY"],
            erp_write_key=os.environ.get("ERP_WRITE_KEY", "") if need_write_key else "",
            db_path=os.environ.get("GATEWAY_DB", "gateway.db"),
            tokens=TokenTable(json.loads(os.environ["GATEWAY_TOKENS"])),
            currency=os.environ.get("ERP_CURRENCY", "GBP"),
            uses_placeholders=any(
                "change-me" in os.environ.get(k, "") for k in ("ERP_READ_KEY", "ERP_WRITE_KEY", "GATEWAY_TOKENS")
            ),
        )
