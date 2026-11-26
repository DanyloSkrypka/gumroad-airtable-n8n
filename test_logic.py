#!/usr/bin/env python3
"""Offline test of the workflow logic. No n8n, no Airtable, no network, no keys.

    python3 test_logic.py

- Runs the EXACT JavaScript that is inside the n8n Code node (src/normalize.js, checked
  byte-for-byte against both workflow JSON files) through Node.js.
- Emulates Airtable "Create or Update" (PATCH, merge on "Sale ID", 422 on unknown columns).
- Replays real-world delivery problems from Gumroad's Ping docs: retries (same ping 3x),
  refund arriving before the sale, invalid payloads, API backfill overlapping live pings,
  an Airtable outage followed by the nightly reconcile.
"""
import json
import subprocess
import sys
from pathlib import Path
from urllib.parse import parse_qsl, urlencode

ROOT = Path(__file__).resolve().parent
JS = (ROOT / "src" / "normalize.js").read_text()
SAMPLE = json.loads((ROOT / "samples" / "sample_sale.json").read_text())
IGNORE = {"_valid", "_errors"}  # Airtable node option "Ignore Fields"

# Columns of the Airtable "Sales" table (must match README)
COLUMNS = {
    "Sale ID", "Order Number", "Sale Date", "Email", "Full Name", "Product Name", "Product ID",
    "Product Permalink", "Amount", "Currency", "Quantity", "Variants", "Offer Code", "Country",
    "Can Contact", "Subscription ID", "Refunded", "Disputed", "Test", "Source", "Last Event", "Synced At",
}

HARNESS = r"""
const fs = require('fs');
const code = fs.readFileSync(process.argv[1], 'utf8');
const items = JSON.parse(fs.readFileSync(0, 'utf8'));
const $input = { all: () => items, first: () => items[0] };
const out = new Function('$input', code)($input);
process.stdout.write(JSON.stringify(out));
"""


def run_code_node(items):
    """Execute normalize.js exactly as the n8n Code node would (Run Once for All Items)."""
    p = subprocess.run(["node", "-e", HARNESS, str(ROOT / "src" / "normalize.js")],
                       input=json.dumps(items), capture_output=True, text=True)
    if p.returncode:
        raise RuntimeError(p.stderr)
    return [i["json"] for i in json.loads(p.stdout)]


def as_webhook_item(fields):
    """What the n8n Webhook node outputs for a form-encoded POST (Node querystring = flat keys)."""
    body = dict(parse_qsl(urlencode(fields), keep_blank_values=True))
    return {"json": {"headers": {"content-type": "application/x-www-form-urlencoded"},
                     "params": {}, "query": {}, "body": body}}


class FakeAirtable:
    def __init__(self):
        self.rows, self.requests, self.down = {}, 0, False

    def upsert(self, rec):
        self.requests += 1
        if self.down:
            raise ConnectionError("Airtable 503")
        fields = {k: v for k, v in rec.items() if k not in IGNORE}
        unknown = set(fields) - COLUMNS
        if unknown:
            raise ValueError(f"422 UNKNOWN_FIELD_NAME {unknown}")
        row = self.rows.setdefault(fields["Sale ID"], {})
        row.update(fields)  # PATCH semantics: omitted fields keep their value


def execute(items, table):
    """One workflow execution: Code -> If(_valid) -> Airtable | Stop and Error."""
    written, rejected = 0, []
    for rec in run_code_node(items):
        if rec["_valid"]:
            table.upsert(rec)
            written += 1
        else:
            rejected.append(rec["_errors"])
    return written, rejected


PASS = FAIL = 0


def check(name, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  PASS  {name}")
    else:
        FAIL += 1
        print(f"  FAIL  {name}  {detail}")


def main():
    print("0. The code in the workflow files is the tested code")
    for wf in ("gumroad-to-airtable.json", "gumroad-backfill-reconcile.json"):
        nodes = json.loads((ROOT / "workflow" / wf).read_text())["nodes"]
        js = [n["parameters"]["jsCode"] for n in nodes if n["name"] == "Normalize & validate"]
        check(f"{wf}: Code node == src/normalize.js", js == [JS])
    readme = (ROOT / "README.md").read_text()
    missing = [c for c in COLUMNS if f"| {c} |" not in readme]
    check("every column is documented in README", not missing, missing)

    print("1. Sample sale (form-encoded, like Gumroad sends it)")
    t = FakeAirtable()
    w, rej = execute([as_webhook_item(SAMPLE)], t)
    row = t.rows.get(SAMPLE["sale_id"], {})
    check("written once, no rejects", w == 1 and not rej, (w, rej))
    check("email trimmed + lowercased", row.get("Email") == "jane.buyer@example.com", row.get("Email"))
    check("price 2900 cents -> 29.0 USD", row.get("Amount") == 29.0 and row.get("Currency") == "USD")
    check("sale date ISO-8601", row.get("Sale Date") == "2026-11-26T09:41:12.000Z", row.get("Sale Date"))
    check("product id + name", row.get("Product ID") == "FAKEprod_Xyz789==" and row.get("Product Name") == "Notion Creator Kit")
    check("order number is a number", row.get("Order Number") == 524459995)
    check("variants[Tier] -> 'Tier: Pro'", row.get("Variants") == "Tier: Pro", row.get("Variants"))
    check("booleans parsed from strings", row.get("Can Contact") is True and row.get("Test") is True)
    check("not refunded -> Refunded not written", "Refunded" not in row)
    check("card details are NOT stored", not any("card" in k.lower() for k in row))

    print("2. Gumroad retries: the same ping 3 times in 3 separate executions")
    t = FakeAirtable()
    for n in range(3):
        execute([as_webhook_item({**SAMPLE, **({"retry_count": str(n)} if n else {})})], t)
    check("still exactly 1 row", len(t.rows) == 1, len(t.rows))

    print("3. Refund ping arrives BEFORE the sale ping (Gumroad: ordering not guaranteed)")
    t = FakeAirtable()
    execute([as_webhook_item({**SAMPLE, "resource_name": "refund", "refunded": "true"})], t)
    execute([as_webhook_item(SAMPLE)], t)
    row = t.rows[SAMPLE["sale_id"]]
    check("1 row", len(t.rows) == 1)
    check("late sale ping does not un-refund", row.get("Refunded") is True, row)
    check("row still has full data", row.get("Amount") == 29.0 and row.get("Email"))

    print("4. Normal refund after the sale")
    t = FakeAirtable()
    execute([as_webhook_item(SAMPLE)], t)
    execute([as_webhook_item({**SAMPLE, "resource_name": "refund", "refunded": "true"})], t)
    check("same row flagged Refunded", len(t.rows) == 1 and t.rows[SAMPLE["sale_id"]].get("Refunded") is True)
    check("Last Event = refund", t.rows[SAMPLE["sale_id"]].get("Last Event") == "refund")

    print("5. Invalid payloads go to the error branch, nothing written")
    bad = [
        ("bad email", {**SAMPLE, "email": "not-an-email"}, "invalid email"),
        ("no sale_id", {k: v for k, v in SAMPLE.items() if k != "sale_id"}, "missing sale_id"),
        ("bad date", {**SAMPLE, "sale_timestamp": "yesterday"}, "invalid sale timestamp"),
        ("bad price", {**SAMPLE, "price": "29.00$"}, "invalid price"),
        ("no product_id", {k: v for k, v in SAMPLE.items() if k != "product_id"}, "missing product_id"),
        ("empty body", {}, "missing sale_id"),
    ]
    for name, payload, expect in bad:
        t = FakeAirtable()
        w, rej = execute([as_webhook_item(payload)], t)
        check(f"{name}: rejected with '{expect}'", w == 0 and rej and expect in rej[0] and not t.rows, rej)

    print("6. JSON body (e.g. a manual test with curl -H 'Content-Type: application/json')")
    t = FakeAirtable()
    item = {"json": {"headers": {}, "query": {}, "body": {**SAMPLE, "price": 2900, "test": True,
                                                          "variants": {"Tier": "Pro"}}}}
    execute([item], t)
    row = t.rows.get(SAMPLE["sale_id"], {})
    check("same result as form-encoded", row.get("Amount") == 29.0 and row.get("Variants") == "Tier: Pro", row)

    print("7. Backfill from the API overlapping live pings")
    t = FakeAirtable()
    execute([as_webhook_item(SAMPLE)], t)  # already synced by Ping
    api_sale = lambda sid, email, price, cur="usd", **kw: {  # noqa: E731
        "id": sid, "email": email, "created_at": "2026-11-20T10:00:00Z", "product_name": "Notion Creator Kit",
        "product_id": "FAKEprod_Xyz789==", "product_permalink": "creatorkit", "price": price,
        "currency": cur, "order_id": 1000, "quantity": 1, "can_contact": True, **kw}
    page = [
        api_sale(SAMPLE["sale_id"], "jane.buyer@example.com", 2900),       # same as the ping
        api_sale("FAKEsale_old1==", "old.customer@example.com", 1500),
        api_sale("FAKEsale_old1==", "old.customer@example.com", 1500),     # duplicate inside the page
        api_sale("FAKEsale_jp1==", "taro@example.jp", 3000, cur="jpy"),     # zero-decimal currency
        api_sale("FAKEsale_rf1==", "refund@example.com", 900, refunded=True),
    ]
    w, rej = execute([{"json": s} for s in page], t)
    check("4 unique sales written (in-run duplicate merged)", w == 4, w)
    check("3 rows total + the ping row = 4 rows", len(t.rows) == 4, len(t.rows))
    check("JPY 3000 -> 3000.0 JPY (no /100)", t.rows["FAKEsale_jp1=="]["Amount"] == 3000 and t.rows["FAKEsale_jp1=="]["Currency"] == "JPY")
    check("API refunded flag kept", t.rows["FAKEsale_rf1=="].get("Refunded") is True)
    check("ping row not duplicated, Test flag kept", t.rows[SAMPLE["sale_id"]].get("Test") is True)

    print("8. Airtable outage -> error, then nightly reconcile recovers it (no 'seen' cache to lose it)")
    t = FakeAirtable()
    t.down = True
    try:
        execute([as_webhook_item({**SAMPLE, "sale_id": "FAKEsale_lost=="})], t)
        crashed = False
    except ConnectionError:
        crashed = True  # in n8n: execution fails -> Error Trigger -> email alert
    check("outage raises (-> Error Trigger email)", crashed)
    t.down = False
    execute([{"json": api_sale("FAKEsale_lost==", "jane.buyer@example.com", 2900)}], t)
    check("reconcile wrote the missed sale", "FAKEsale_lost==" in t.rows)

    print("9. Two different purchases by the same customer stay separate")
    t = FakeAirtable()
    execute([as_webhook_item(SAMPLE)], t)
    execute([as_webhook_item({**SAMPLE, "sale_id": "FAKEsale_second==", "order_number": "524459996"})], t)
    check("2 rows", len(t.rows) == 2)

    print("10. Test-ping toggle")
    js_skip = JS.replace("const SKIP_TEST_SALES = false;", "const SKIP_TEST_SALES = true;")
    p = subprocess.run(["node", "-e", HARNESS.replace("fs.readFileSync(process.argv[1], 'utf8')", "process.argv[1]"), js_skip],
                       input=json.dumps([as_webhook_item(SAMPLE)]), capture_output=True, text=True)
    check("SKIP_TEST_SALES=true drops test pings", json.loads(p.stdout or "null") == [], p.stderr[:200])

    print(f"\n{PASS} passed, {FAIL} failed")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
