"""The stand-in ERP must behave like a real one, or the gateway tests prove nothing."""
import httpx


def test_api_key_required_and_read_key_cannot_write(stack):
    assert httpx.get(f"{stack.erp_url}/orders").status_code == 401
    resp = stack.erp.post("/orders", json={"customer_id": "C001", "lines": [{"sku": "WID-100", "qty": 1}]})
    assert resp.status_code == 403
    assert stack.stock("WID-100") == 120


def test_create_order_prices_and_reduces_stock(stack):
    resp = stack.erp_admin.post("/orders", json={"customer_id": "C001", "lines": [{"sku": "wid-100", "qty": 10}]})
    assert resp.status_code == 201
    order = resp.json()
    assert order["total"] == 10 * 450 and order["status"] == "open"
    assert stack.stock("WID-100") == 110


def test_failed_order_writes_nothing(stack):
    # Second line exceeds stock: the first line must not be reserved either.
    resp = stack.erp_admin.post(
        "/orders", json={"customer_id": "C001", "lines": [{"sku": "WID-100", "qty": 5}, {"sku": "GAD-010", "qty": 99}]}
    )
    assert resp.status_code == 409
    assert stack.stock("WID-100") == 120 and stack.stock("GAD-010") == 12
    assert len(stack.orders()) == 1  # only the seeded order


def test_idempotency_key_prevents_duplicates(stack):
    body = {"customer_id": "C001", "lines": [{"sku": "WID-100", "qty": 10}]}
    first = stack.erp_admin.post("/orders", json=body, headers={"Idempotency-Key": "k1"}).json()
    second = stack.erp_admin.post("/orders", json=body, headers={"Idempotency-Key": "k1"}).json()
    assert first["id"] == second["id"]
    assert stack.stock("WID-100") == 110


def test_status_rules_and_cancel_returns_stock(stack):
    order = stack.erp_admin.post("/orders", json={"customer_id": "C001", "lines": [{"sku": "WID-100", "qty": 10}]}).json()
    assert stack.erp_admin.patch(f"/orders/{order['id']}", json={"status": "shipped"}).status_code == 409
    assert stack.erp_admin.patch(f"/orders/{order['id']}", json={"status": "cancelled"}).json()["status"] == "cancelled"
    assert stack.stock("WID-100") == 120
    assert stack.erp_admin.patch(f"/orders/{order['id']}", json={"status": "confirmed"}).status_code == 409


def test_one_invoice_per_order(stack):
    order = stack.erp_admin.post("/orders", json={"customer_id": "C001", "lines": [{"sku": "WID-100", "qty": 2}]}).json()
    first = stack.erp_admin.post("/invoices", json={"order_id": order["id"]})
    assert first.status_code == 201 and first.json()["amount"] == 900
    assert stack.erp_admin.post("/invoices", json={"order_id": order["id"]}).status_code == 409
