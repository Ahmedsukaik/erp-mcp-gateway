"""Regenerate docs/TOOLS.md from the server's own tool list:  python docs/gen_tools.py"""
import asyncio
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from gateway.config import TokenTable  # noqa: E402
from gateway.erp_client import ErpReader, ErpWriter  # noqa: E402
from gateway.server import Deps, build_server  # noqa: E402
from gateway.store import Store  # noqa: E402


def arg_type(spec: dict) -> str:
    if "anyOf" in spec:
        parts = []
        for option in spec["anyOf"]:
            if option.get("type") == "null":
                continue
            parts.append("one of " + ", ".join(option["enum"]) if "enum" in option else option.get("type", ""))
        return " or ".join(parts)
    if spec.get("type") == "array":
        return "list of {sku, qty}"
    return spec.get("type", "")


def main() -> None:
    deps = Deps(ErpReader("http://unused", "k"), ErpWriter("http://unused", "k"),
                Store(str(Path(tempfile.mkdtemp()) / "g.db")), TokenTable({}))
    tools = asyncio.run(build_server(deps).list_tools())
    out = [
        "# Tool reference", "",
        "Generated from the server's own tool list (`python docs/gen_tools.py`). Amounts are integers in minor units (pence).",
        "Tools return JSON objects; the field names are the ERP's own (for example `id`, `status`, `total`, `lines`).", "",
        "| Tool | Kind | Roles allowed |", "|---|---|---|",
    ]
    for t in tools:
        read = bool(t.annotations and t.annotations.read_only_hint)
        kind = "read" if read else "propose (queues a change, writes nothing)"
        out.append(f"| `{t.name}` | {kind} | {'reader, agent, approver' if read else 'agent'} |")
    out.append("")
    for t in tools:
        out += [f"## {t.name}", "", " ".join((t.description or "").split()), ""]
        props = t.input_schema.get("properties", {})
        required = set(t.input_schema.get("required", []))
        if props:
            out += ["| Argument | Type | Required |", "|---|---|---|"]
            out += [f"| `{n}` | {arg_type(s)} | {'yes' if n in required else 'no'} |" for n, s in props.items()]
        else:
            out.append("No arguments.")
        out.append("")
    (ROOT / "docs" / "TOOLS.md").write_text("\n".join(out), encoding="utf-8", newline="\n")
    print(f"wrote docs/TOOLS.md ({len(tools)} tools)")


if __name__ == "__main__":
    main()
