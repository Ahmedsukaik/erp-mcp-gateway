# Tool reference

Generated from the server's own tool list (`python docs/gen_tools.py`). Amounts are integers in minor units (pence).
Tools return JSON objects; the field names are the ERP's own (for example `id`, `status`, `total`, `lines`).

| Tool | Kind | Roles allowed |
|---|---|---|
| `find_customer` | read | reader, agent, approver |
| `search_products` | read | reader, agent, approver |
| `check_stock` | read | reader, agent, approver |
| `list_low_stock` | read | reader, agent, approver |
| `find_order` | read | reader, agent, approver |
| `list_orders` | read | reader, agent, approver |
| `list_unpaid_invoices` | read | reader, agent, approver |
| `get_change_status` | read | reader, agent, approver |
| `propose_create_order` | propose (queues a change, writes nothing) | agent |
| `propose_update_order` | propose (queues a change, writes nothing) | agent |
| `propose_create_invoice` | propose (queues a change, writes nothing) | agent |

## find_customer

Find customers by name, email address or customer id. Returns up to 20 matches.

| Argument | Type | Required |
|---|---|---|
| `query` | string | yes |

## search_products

Search products by name or SKU. Returns SKU, name, unit price (pence) and stock.

| Argument | Type | Required |
|---|---|---|
| `query` | string | yes |

## check_stock

Stock level for one SKU, with its reorder level and whether it is running low.

| Argument | Type | Required |
|---|---|---|
| `sku` | string | yes |

## list_low_stock

Products whose stock is at or below their reorder level.

No arguments.

## find_order

One order by its order number, with its lines, status, total (pence) and delivery date.

| Argument | Type | Required |
|---|---|---|
| `order_id` | integer | yes |

## list_orders

Latest orders (max 50), optionally filtered by customer id and/or status.

| Argument | Type | Required |
|---|---|---|
| `customer_id` | string | no |
| `status` | one of open, confirmed, shipped, cancelled | no |

## list_unpaid_invoices

Unpaid invoices, oldest due date first, with the total outstanding (pence).

No arguments.

## get_change_status

Status of a proposed change: pending, applying, approved, rejected, failed or unknown. When approved, `result` holds what the ERP created; otherwise `error` says why.

| Argument | Type | Required |
|---|---|---|
| `change_id` | string | yes |

## propose_create_order

Propose a new sales order. Does NOT create it: a human must approve first. `reference` is the customer's PO number or another unique reference. Proposing the same customer + reference again returns the existing change (`already_proposed`: true) and queues nothing; `differs_from_existing` tells you if the content is different. Without a reference there is no duplicate protection. `delivery_date` is ISO format (YYYY-MM-DD), today or later.

| Argument | Type | Required |
|---|---|---|
| `customer_id` | string | yes |
| `lines` | list of {sku, qty} | yes |
| `reference` | string | no |
| `delivery_date` | string | no |
| `notes` | string | no |

## propose_update_order

Propose a change to an existing order: its status (confirmed, shipped, cancelled), delivery date (YYYY-MM-DD) and/or notes. Does NOT change it: a human must approve first. Order lines cannot be changed with this tool.

| Argument | Type | Required |
|---|---|---|
| `order_id` | integer | yes |
| `status` | one of confirmed, shipped, cancelled | no |
| `delivery_date` | string | no |
| `notes` | string | no |

## propose_create_invoice

Propose an invoice for an order, for the order total. Does NOT create it: a human must approve first.

| Argument | Type | Required |
|---|---|---|
| `order_id` | integer | yes |
