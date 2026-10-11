from __future__ import annotations

import sys
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path

import httpx
import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from demo import AGENT, APPROVER, READER, TOKENS, free_port, serve  # noqa: E402
from erp.app import create_app  # noqa: E402
from gateway.config import TokenTable  # noqa: E402
from gateway.erp_client import ErpReader, ErpWriter  # noqa: E402
from gateway.server import Deps, build_app  # noqa: E402
from gateway.store import Store  # noqa: E402


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@dataclass
class Stack:
    erp_url: str
    gw_url: str
    deps: Deps
    erp: httpx.Client  # ERP with the read key
    erp_admin: httpx.Client  # ERP with the write key (tests only)
    human: httpx.Client  # gateway as the approver

    @asynccontextmanager
    async def mcp(self, token: str = AGENT):
        """Yield `call(tool, args) -> dict` over a real MCP streamable-HTTP session."""
        from mcp import Client
        from mcp.client.streamable_http import create_mcp_http_client, streamable_http_client

        from agent.order_intake import mcp_caller

        async with create_mcp_http_client(headers={"Authorization": f"Bearer {token}"}) as http:
            async with Client(streamable_http_client(f"{self.gw_url}/mcp", http_client=http)) as client:
                call = mcp_caller(client)
                call.client = client  # type: ignore[attr-defined]
                yield call

    def approve(self, change_id: str) -> dict:
        return self.human.post(f"/approvals/{change_id}/approve").json()

    def stock(self, sku: str) -> int:
        return self.erp.get(f"/products/{sku}").json()["stock"]

    def orders(self) -> list[dict]:
        return self.erp.get("/orders").json()


@pytest.fixture
def stack(tmp_path):
    erp_port, gw_port = free_port(), free_port()
    erp_url, gw_url = f"http://127.0.0.1:{erp_port}", f"http://127.0.0.1:{gw_port}"
    servers = [serve(create_app(tmp_path / "erp.db", "erp-read", "erp-write"), erp_port)]
    deps = Deps(
        reader=ErpReader(erp_url, "erp-read"),
        writer=ErpWriter(erp_url, "erp-write"),
        store=Store(str(tmp_path / "gateway.db")),
        tokens=TokenTable(TOKENS),
    )
    servers.append(serve(build_app(deps, "full"), gw_port))
    s = Stack(
        erp_url, gw_url, deps,
        erp=httpx.Client(base_url=erp_url, headers={"X-API-Key": "erp-read"}),
        erp_admin=httpx.Client(base_url=erp_url, headers={"X-API-Key": "erp-write"}),
        human=httpx.Client(base_url=gw_url, headers={"Authorization": f"Bearer {APPROVER}"}),
    )
    yield s
    for client in (s.erp, s.erp_admin, s.human):
        client.close()
    for server in servers:
        server.should_exit = True


@pytest.fixture
def settings_env(stack, monkeypatch, tmp_path):
    """Environment variables as a real deployment would set them, pointing at the test stack."""
    import json

    monkeypatch.chdir(tmp_path)  # no stray .env
    monkeypatch.setenv("ERP_URL", stack.erp_url)
    monkeypatch.setenv("ERP_READ_KEY", "erp-read")
    monkeypatch.setenv("ERP_WRITE_KEY", "erp-write")
    monkeypatch.setenv("GATEWAY_DB", str(tmp_path / "gateway-env.db"))
    monkeypatch.setenv("GATEWAY_TOKENS", json.dumps(TOKENS))


__all__ = ["AGENT", "APPROVER", "READER"]
