# User guide for approvers

This guide is for the person who checks and approves what the AI agent prepares.
You do not need to know how the system is built.

Words used here: the **ERP** is your order system. The **agent** is the program that reads
order emails. A **change** is one thing the agent wants to do in the ERP, for example
"create this order".

## What the agent does

When the agent is given an order email, it:

1. reads the email,
2. checks that the sender's address belongs to a customer, and checks each product in the ERP,
3. prepares the order and puts it in a waiting list.

The agent **cannot** create or change anything in the ERP. It can only prepare.
Nothing happens until you approve.

If the agent is not sure, it prepares nothing. This happens with an unknown sender, an
unknown product code, a quantity it cannot read with certainty, or an email that looks like
a cancellation or a question. Those emails do not appear in your list. Your administrator
decides where they go; enter those orders by hand as you do today.

## Before you start

Your administrator sets these up once:

- the ERP connection and the gateway are running,
- you have a project folder with a file named `.env` that holds your personal approver token.

Each time, open a terminal (a command window) in the project folder and switch on the
Python environment:

```
source .venv/bin/activate
```

On Windows: `.venv\Scripts\activate`

## Your daily steps

**1. See what is waiting**

```
python -m gateway.cli list
```

If nothing is waiting you see `Nothing waiting for approval.` Otherwise each change looks like this:

```
chg_17663dc35767  [pending]  requested by order-intake-agent at 2026-10-10 16:36:02 UTC
    New order for Northwind Traders Ltd (C001): 40 x WID-100 (Steel Widget 100mm), 10 x WID-200 (Steel Widget 200mm). Estimated total 259.00 GBP. Reference 48213.
    Notes: Created from email by order-intake agent.
```

The first line has the change id (it starts with `chg_`), who prepared it and when. Times
are in UTC. The next lines are the summary, the notes that will be saved on the order, and
any `WARNING` lines. Read them all. Compare with the email if something looks unusual.

**2. Approve**

```
python -m gateway.cli approve chg_17663dc35767
```

Use the id from your own list. You see the change again with a new status and a result line:

```
    Result: ERP order #2 created, total 259.00 GBP
```

**3. Or reject**

```
python -m gateway.cli reject chg_17663dc35767 --reason "wrong quantity"
```

Nothing is created. The reason is saved.

## What the statuses mean

| Status | Meaning | What you do |
|---|---|---|
| `pending` | Waiting for you | Approve or reject |
| `approved` | Written to the ERP | Nothing |
| `rejected` | You discarded it | Nothing |
| `failed` | You approved, but it was stopped before anything was written (the ERP said no, a price changed, or the ERP could not be reached) | Read the `Reason` line. Fix the cause and approve again, or reject |
| `unknown` | The ERP did not answer clearly. The change may or may not be in the ERP | Approve it again. This is safe and never creates a second order. You cannot reject it until the result is known |

To see other lists, add a status, for example:

```
python -m gateway.cli list --status failed
```

You can use `approved`, `rejected`, `failed`, `unknown` or `all`. Failed and rejected
changes show the reason.

## Lines to look for

| Line | Meaning |
|---|---|
| `WARNING: Stock for X is 12, order asks for 30` | Approval will fail until stock arrives. Wait, or reject and call the customer |
| `WARNING: Order is still open (not confirmed)` | You are invoicing an order that is not confirmed yet |
| `Notes: ... Check: 'control gadget' was matched by name to GAD-010.` | The email had no product code. Check the agent picked the right product |
| `Reason: Prices changed since this was proposed` | The price list changed after the agent prepared the order. Reject it; the order must be prepared again |

## Common questions

**I approved the same change twice by mistake.**
The second time you see `Error 409: change ... is already approved`. Only one order exists.

**The ERP was down when I approved.**
The change shows `failed` or `unknown`, with the reason. In both cases approve it again
when the ERP is back. It will not create a second order.

**The same email was processed twice.**
The agent gets the same change back. There is no duplicate.

**A customer sent a corrected order with the same PO number.**
The agent does not replace the first one. It reports the email as needing a person. If the
first order is still pending, reject it, then ask for the email to be processed again.

**Who did what?**

```
python -m gateway.cli audit --limit 50
```

Each line shows the time (UTC), who called, their role, the action, the result, and the details.

**The command says "Could not reach the gateway".**
The gateway is not running, or the address in `.env` is wrong. Ask your administrator.

**The command says "missing or invalid bearer token".**
The token in `.env` is mistyped or no longer valid. Ask your administrator.

**The command says "approver token required".**
Your token does not have the approver role. Ask your administrator for the right token.

**The command says "No module named ...".**
The Python environment is not switched on. See "Before you start".
