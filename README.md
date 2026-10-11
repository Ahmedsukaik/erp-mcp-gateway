# ERP MCP Gateway

A reference implementation of how to let AI agents work inside a business system
without letting them write to it unsupervised.

It has three parts:

| Part | Folder | What it is |
|---|---|---|
| Stand-in ERP | `erp/` | A small REST API (orders, stock, invoices). It plays the role of a customer's own ERP. |
| MCP gateway | `gateway/` | An MCP server on top of the ERP API, plus an approval queue and an audit log. |
| Order-intake agent | `agent/` | Reads an order email and proposes the order through the gateway. |

The ERP here is a demo, not a client system. In a real project `erp/` is deleted and the
gateway is pointed at the customer's API (see "Adapting it to another system").

## The idea in one picture

```
 order email
     |
     v
 +---------------+   MCP (Streamable HTTP, bearer token)   +----------------------+
 | intake agent  | --------------------------------------> |     MCP gateway      |
 +---------------+   read tools: live data                 |  roles + audit log   |
                     propose_* tools: queue a change       +----+------------+----+
                                                                |            |
                                             read-only ERP key  |            | write ERP key,
                                                                v            | used only after
 +---------------+   approve / reject (HTTP or CLI)        +---------+       | approval
 | human approver| --------------------------------------> | approval|-------+
 +---------------+                                         |  queue  |       v
                                                           +---------+   +-------+
                                                                         |  ERP  |
                                                                         +-------+
```

What the code enforces:

1. **Read and write are separate.** Read tools use a read-only ERP key. Write tools are
   named `propose_*` and never call the ERP's write endpoints.
2. **A human approves every write.** A proposal sits in a queue until someone with the
   `approver` role approves it. Only then does the gateway write, with a key agents never see.
3. **The ERP has the last word.** At approval the gateway re-checks the prices the approver
   saw, and the ERP validates the request again. If stock ran out or a price changed in the
   meantime, the change fails and nothing is written.
4. **No duplicate orders for the same customer and reference.** The same customer +
   reference is queued once, and always maps to the same ERP idempotency key. Approving
   twice, or five times at once, creates one order. If the ERP's answer is lost, the change
   is marked `unknown`; it cannot be rejected in that state, and approving again is safe.
   If the ERP already holds a different order under that reference, the gateway says so.
   (A proposal without a reference has no such protection; the intake agent always sends one.)
5. **An audit trail.** Every tool call is recorded, including refused, malformed and
   unknown ones (who, tool, arguments, outcome, duration), and so is every request to the
   approval queue (who, action, outcome). Requests without a valid token are refused at
   the door and written to the application log instead.

## Quick start

Needs Python 3.10 or newer. Developed on Linux with Python 3.13. The test suite and the demo
were also run on Windows 11 with Python 3.12. Python 3.10 and 3.11 have not been run.

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

python demo.py
python -m pytest -q
```

On Windows use `python -m venv .venv` and `.venv\Scripts\activate`, and run the demo in
Windows Terminal or PowerShell.

`python demo.py` needs no API key. It starts both servers on free local ports with
throwaway databases and prints nine short scenes: the token check, the role check, the
tool list, a clear order, approval (with duplicate protection), a stock refusal, an unknown
sender, two emails the agent will not guess about, and the audit log. Add `--pause` to stop
after each scene (useful when recording). The test suite has 109 tests.

## Running the pieces yourself

Copy `.env.example` to `.env`. The `change-me` values work for a local try-out; the gateway
prints a warning while they are in use and refuses to listen on a public address with them.

```bash
cp .env.example .env
```

Then, in three terminals (with the virtual environment active in each):

```bash
python -m erp
```
```bash
python -m gateway.server
```
```bash
python -m agent.order_intake samples/emails/01_clear_order.txt
python -m gateway.cli list
python -m gateway.cli approve <change id from the list>
python -m gateway.cli audit
```

The ERP listens on port 8100 and the gateway on 8101 (MCP endpoint `/mcp`). Stop a server
with Ctrl+C. All commands read `.env` from the current folder; real environment variables
win over the file. `erp.db` and `gateway.db` are created in the current folder and kept
between runs; delete them to start again.

### Reading emails: rules or Claude

By default the agent reads emails with fixed rules and no AI model. The rules know one
simple layout: a greeting, a PO line (`PO 48213`), one product per line (`40 x WID-100` or
`WID-100: 40`), an optional `Deliver by 2026-11-05` line, and a closing. Any other line
sends the whole email to a person; the reader never interprets free text. That is enough
for the demo and needs no key. It is not meant for a real mailbox.

For real emails the intended path is the Claude extractor: one API call per email with a
forced tool schema, so the answer is always structured. It is written and unit-tested with
a simulated response, but has not been run against the live API yet.

```bash
# in .env:  ANTHROPIC_API_KEY=...
python -m agent.order_intake samples/emails/01_clear_order.txt --extractor claude
python demo.py --claude
```

In both modes the rest of the workflow is ordinary code, the sender address is read from
the From header by code, and the model cannot call ERP tools.

### Connecting another MCP client

Any MCP client that supports Streamable HTTP and a custom header can connect:

- URL: `http://127.0.0.1:8101/mcp`
- Header: `Authorization: Bearer <a token from GATEWAY_TOKENS>`

For example, with Claude Code:

```bash
claude mcp add --transport http erp http://127.0.0.1:8101/mcp --header "Authorization: Bearer <token>"
```

For an agent that must never see a write tool, run a second, read-only gateway:

```bash
python -m gateway.server --profile read --port 8102
```

That server registers no `propose_*` tools, serves no approval routes, and neither needs
nor loads the ERP write key.

## Roles

| Role | Read tools | `propose_*` tools | Approve / reject | Audit log |
|---|---|---|---|---|
| `reader` | yes | no | no | no |
| `agent` | yes | yes | no | no |
| `approver` (a person) | yes | no | yes | yes |

Tokens and roles are set in `GATEWAY_TOKENS`. A request without a known token gets `401`
before it reaches MCP. The role is checked again on every tool call. The MCP endpoint is
stateless: there is no server-side session for another caller to reuse.

The three roles are fixed. Finer control (for example one agent that may only propose
invoices) means adding a role in `gateway/config.py`.

## HTTP endpoints next to MCP

| Endpoint | Who | Purpose |
|---|---|---|
| `GET /healthz` | public | liveness check |
| `GET /approvals?status=pending` | approver | the queue (`pending`, `approved`, `rejected`, `failed`, `unknown`, `all`) |
| `POST /approvals/{id}/approve` | approver | apply the change to the ERP |
| `POST /approvals/{id}/reject` | approver | discard it; body `{"reason": "..."}` |
| `GET /audit?limit=50` | approver | latest audit entries |

`python -m gateway.cli` is a thin client for these. A Slack button or a web page can call the same endpoints.

## Statuses of a change

| Status | Meaning |
|---|---|
| `pending` | waiting for a human |
| `approved` | written to the ERP |
| `rejected` | discarded by a human; the reference can be proposed again |
| `failed` | the ERP refused it, or a check before the write stopped it (changed price, ERP not reachable); nothing was written; it can be approved again or rejected |
| `unknown` | the ERP gave no usable answer, so it may or may not be written; approve again to find out (safe); it cannot be rejected |
| `applying` | an approval is in flight; if the gateway dies here, it can be approved again after five minutes |

## What is in the box

```
erp/app.py            stand-in ERP API (FastAPI + SQLite)
gateway/server.py     MCP server: tools, input checks, role checks, audit, HTTP routes, outer gate
gateway/approvals.py  the only code path that writes to the ERP
gateway/store.py      approval queue + audit log (SQLite)
gateway/erp_client.py ErpReader (read key, no write methods) and ErpWriter
gateway/cli.py        approver command line
agent/extract.py      email -> structured order (strict rules, or Claude)
agent/order_intake.py the workflow: match the sender, check products, propose
docs/TOOLS.md         every tool with its arguments (generated)
docs/USER_GUIDE.md    for the people who approve changes
tests/                109 tests; about half start both servers and use real HTTP and MCP
```

## What the tests cover, and what they do not

Covered (`python -m pytest -q`): ERP rules; token, role and hostname checks; the read-only
profile; propose-then-approve; duplicate proposals and revised orders; five approvals at
the same moment; rejection rules; stock and price re-checks at approval; invalid and
hostile input (path tricks, control characters, bad dates and quantities); order update
and invoice flows; lost, garbled and internal-error answers from the ERP; a stale
`applying` change; the audit log; and the agent on clear, short-stock, unknown-sender,
spoofed-sender, unknown-product, unclear and non-order emails. 44 of the tests feed the
rule-based reader an email that could be misread, and check that it goes to a person.

Not covered:

- **The live Claude API call.** `ClaudeExtractor` is tested with a simulated response that
  checks the request and the parsing. It has not been run against the real API here.
- **Load and long-running behaviour.** No performance or soak tests.
- **A real ERP.** The stand-in is strict (transactions, stock checks, idempotency keys),
  but a real system will have its own surprises.
- **Python 3.10 and 3.11, and macOS.** The suite was run on Linux with Python 3.13 and on
  Windows 11 with Python 3.12 (109 passed on both).

## What this is not

It is a first phase: the MCP server and one workflow. Not included: hosting and TLS, an
email inbox connection (the agent takes the email text as input), PDF or scanned
documents, purchase orders, changing order lines, stock alerts, invoice chasing, reports,
a chat assistant, and routing "needs a person" emails to someone (the agent returns that
status; sending it to Slack or a mailbox is integration work).

## Before using this for real

- Put the gateway behind HTTPS (a reverse proxy or a platform that terminates TLS). Start it
  with `--host 0.0.0.0 --allowed-host your.domain`; requests for any other hostname are
  refused on every route except `/healthz`. Pass the hostname exactly as clients send it.
- Generate long random tokens, one per agent and per person, and keep `.env` out of git.
- Check the sender properly. The agent trusts the address in the From header, which can be
  forged. Use your mail provider's SPF/DKIM result before the agent sees an email.
- SQLite is fine for one gateway instance. For several instances, move `gateway/store.py` to Postgres.
- Tokens are static. For single sign-on, replace `TokenTable` with the MCP SDK's OAuth token verifier.
- The audit log stores tool arguments. Keep personal data out of arguments, or limit retention.
- Ask the ERP owner for an idempotency key on create endpoints. Without one, a retry after a
  timeout can create a duplicate.
- The price re-check and the write are two ERP calls. A price change in the instant between
  them is not caught; an ERP that accepts an expected total closes that gap.
- Running cost is one model call per email plus hosting. Estimate it from real email volume
  and average email length before quoting a number.

## Adapting it to another system

1. Rewrite the methods in `gateway/erp_client.py` for the target API. It is the only file
   that makes ERP calls.
2. Adjust the tools in `gateway/server.py` to the new fields. Keep the rule: reads return
   data, writes are `propose_*`.
3. Add one branch per new change kind in `gateway/approvals.py`.
4. Run `python docs/gen_tools.py` to refresh the tool reference.
