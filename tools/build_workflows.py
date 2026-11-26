#!/usr/bin/env python3
"""Builds the two importable n8n workflows from src/*.js (single source of truth for the code).

    python3 tools/build_workflows.py

Writes workflow/gumroad-to-airtable.json and workflow/gumroad-backfill-reconcile.json.
No secrets: credentials are referenced by name only, values are set inside n8n.
"""
import json
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "src"
OUT = ROOT / "workflow"
NORMALIZE_JS = (SRC / "normalize.js").read_text()
ALERT_JS = (SRC / "format_alert.js").read_text()

AIRTABLE_BASE_PLACEHOLDER = "appYourBaseId"  # replace in n8n: pick your base from the list
AIRTABLE_TABLE = "Sales"
ALERT_TO = "you@example.com"
ALERT_FROM = "n8n-alerts@example.com"


def nid(ns, name):
    return str(uuid.uuid5(uuid.NAMESPACE_URL, f"gumroad-airtable/{ns}/{name}"))


def node(ns, name, type_, version, pos, params, **extra):
    n = {
        "parameters": params,
        "id": nid(ns, name),
        "name": name,
        "type": type_,
        "typeVersion": version,
        "position": pos,
    }
    n.update(extra)
    return n


def code_node(ns, name, js, pos, notes=None):
    extra = {"notes": notes, "notesInFlow": True} if notes else {}
    return node(ns, name, "n8n-nodes-base.code", 2, pos, {"jsCode": js}, **extra)


def if_valid(ns, pos):
    return node(ns, "Valid sale?", "n8n-nodes-base.if", 2.2, pos, {
        "conditions": {
            "options": {"caseSensitive": True, "leftValue": "", "typeValidation": "strict", "version": 2},
            "conditions": [{
                "id": nid(ns, "cond-valid"),
                "leftValue": "={{ $json._valid }}",
                "rightValue": "",
                "operator": {"type": "boolean", "operation": "true", "singleValue": True},
            }],
            "combinator": "and",
        },
        "options": {},
    })


def stop_invalid(ns, pos):
    return node(ns, "Reject invalid payload", "n8n-nodes-base.stopAndError", 1, pos, {
        "errorMessage": "=Invalid Gumroad payload (sale {{ $json['Sale ID'] || '?' }}): {{ $json._errors }}",
    })


def airtable_upsert(ns, pos):
    return node(ns, "Airtable: upsert by Sale ID", "n8n-nodes-base.airtable", 2.1, pos, {
        "operation": "upsert",
        "base": {"__rl": True, "value": AIRTABLE_BASE_PLACEHOLDER, "mode": "id"},
        "table": {"__rl": True, "value": AIRTABLE_TABLE, "mode": "id"},
        "columns": {
            "mappingMode": "autoMapInputData",
            "value": {},
            "matchingColumns": ["Sale ID"],
            "schema": [{
                "id": "Sale ID", "displayName": "Sale ID", "required": False, "defaultMatch": False,
                "display": True, "type": "string", "canBeUsedToMatch": True,
            }],
        },
        "options": {"typecast": True, "ignoreFields": "_valid,_errors"},
    },
        credentials={"airtableTokenApi": {"id": "AirtablePAT00001", "name": "Airtable PAT"}},
        retryOnFail=True, maxTries=3, waitBetweenTries=2000,
        notes="Create or Update, merge on 'Sale ID' = idempotent. Same sale twice -> same row.",
        notesInFlow=True,
    )


def error_branch(ns, x, y):
    trig = node(ns, "On any error", "n8n-nodes-base.errorTrigger", 1, [x, y], {})
    fmt = code_node(ns, "Format alert", ALERT_JS, [x + 220, y])
    mail = node(ns, "Email alert", "n8n-nodes-base.emailSend", 2.1, [x + 440, y], {
        "fromEmail": ALERT_FROM,
        "toEmail": ALERT_TO,
        "subject": "={{ $json.subject }}",
        "emailFormat": "text",
        "text": "={{ $json.text }}",
        "options": {"appendAttribution": False},
    }, credentials={"smtp": {"id": "SmtpAlerts000001", "name": "SMTP (alerts)"}})
    conns = {
        trig["name"]: {"main": [[{"node": fmt["name"], "type": "main", "index": 0}]]},
        fmt["name"]: {"main": [[{"node": mail["name"], "type": "main", "index": 0}]]},
    }
    return [trig, fmt, mail], conns


def sticky(ns, name, content, pos, w=520, h=300, color=7):
    return node(ns, name, "n8n-nodes-base.stickyNote", 1, pos,
                {"content": content, "height": h, "width": w, "color": color})


def build_live():
    ns = "live"
    hook = node(ns, "Gumroad Ping (sale webhook)", "n8n-nodes-base.webhook", 2, [0, 300], {
        "httpMethod": "POST",
        "path": "gumroad-sale",
        "responseMode": "onReceived",
        "options": {},
    }, webhookId=nid(ns, "webhook-id"),
        notes="Responds 200 instantly (Gumroad gives 5 s). Paste the Production URL into Gumroad > Settings > Advanced > Ping.",
        notesInFlow=True)
    norm = code_node(ns, "Normalize & validate", NORMALIZE_JS, [240, 300],
                     notes="Form fields -> clean typed columns. Validates sale_id, email, date, price, product_id.")
    iff = if_valid(ns, [480, 300])
    air = airtable_upsert(ns, [740, 200])
    stop = stop_invalid(ns, [740, 420])
    err_nodes, err_conns = error_branch(ns, 0, 700)
    note = sticky(ns, "README", (
        "## Gumroad -> Airtable (live)\n"
        "Every Gumroad sale/refund ping -> one row in Airtable **Sales**, keyed by **Sale ID**.\n\n"
        "1. Credentials: *Airtable PAT* (scopes data.records:read/write, schema.bases:read) and *SMTP (alerts)*.\n"
        "2. Airtable node: pick your base, table `Sales`.\n"
        "3. Email alert: set your address.\n"
        "4. Publish/activate, copy the **Production URL** of the webhook into Gumroad Ping.\n\n"
        "Errors (Airtable down, bad payload) -> email via the Error Trigger below. "
        "This workflow is its own error workflow."
    ), [-600, 120], w=520, h=380)
    nodes = [note, hook, norm, iff, air, stop] + err_nodes
    conns = {
        hook["name"]: {"main": [[{"node": norm["name"], "type": "main", "index": 0}]]},
        norm["name"]: {"main": [[{"node": iff["name"], "type": "main", "index": 0}]]},
        iff["name"]: {"main": [
            [{"node": air["name"], "type": "main", "index": 0}],
            [{"node": stop["name"], "type": "main", "index": 0}],
        ]},
    }
    conns.update(err_conns)
    return {
        "name": "Gumroad -> Airtable (live sales)",
        "nodes": nodes,
        "connections": conns,
        "active": False,
        "settings": {"executionOrder": "v1", "saveDataErrorExecution": "all",
                     "saveDataSuccessExecution": "all", "saveManualExecutions": True},
        "pinData": {},
        "meta": {"templateCredsSetupCompleted": False},
        "tags": [],
    }


def build_backfill():
    ns = "backfill"
    manual = node(ns, "Run full backfill (manual)", "n8n-nodes-base.manualTrigger", 1, [0, 200], {})
    sched = node(ns, "Every night 03:00", "n8n-nodes-base.scheduleTrigger", 1.2, [0, 420], {
        "rule": {"interval": [{"field": "days", "triggerAtHour": 3}]},
    })

    def set_after(name, value, pos):
        return node(ns, name, "n8n-nodes-base.set", 3.4, pos, {
            "assignments": {"assignments": [
                {"id": nid(ns, name + "-a"), "name": "after", "value": value, "type": "string"},
            ]},
            "options": {},
        })

    all_time = set_after("From: all time", "2010-01-01", [220, 200])
    recent = set_after("From: last 3 days", "={{ $now.minus({ days: 3 }).toFormat('yyyy-MM-dd') }}", [220, 420])
    http = node(ns, "Gumroad API: list sales", "n8n-nodes-base.httpRequest", 4.2, [460, 300], {
        "url": "https://api.gumroad.com/v2/sales",
        "authentication": "predefinedCredentialType",
        "nodeCredentialType": "gumroadApi",
        "sendQuery": True,
        "queryParameters": {"parameters": [{"name": "after", "value": "={{ $json.after }}"}]},
        "options": {"pagination": {"pagination": {
            "paginationMode": "updateAParameterInEachRequest",
            "parameters": {"parameters": [
                {"type": "qs", "name": "page_key", "value": "={{ $response.body.next_page_key }}"},
            ]},
            "paginationCompleteWhen": "other",
            "completeExpression": "={{ !$response.body.next_page_key }}",
            "limitPagesFetched": True,
            "maxRequests": 500,
            "requestInterval": 300,
        }}},
    }, credentials={"gumroadApi": {"id": "GumroadToken0001", "name": "Gumroad API"}},
        retryOnFail=True, maxTries=3, waitBetweenTries=3000,
        notes="GET /v2/sales, 100 per page, follows next_page_key.", notesInFlow=True)
    split = node(ns, "One item per sale", "n8n-nodes-base.splitOut", 1, [680, 300],
                 {"fieldToSplitOut": "sales", "options": {}})
    norm = code_node(ns, "Normalize & validate", NORMALIZE_JS, [900, 300],
                     notes="Same code as the live workflow. Merges duplicates inside the run.")
    iff = if_valid(ns, [1120, 300])
    loop = node(ns, "Batches of 4", "n8n-nodes-base.splitInBatches", 3, [1340, 200], {"batchSize": 4, "options": {}})
    air = airtable_upsert(ns, [1580, 120])
    wait = node(ns, "Respect Airtable 5 req/s", "n8n-nodes-base.wait", 1.1, [1800, 120],
                {"amount": 1, "unit": "seconds"}, webhookId=nid(ns, "wait-id"))
    stop = stop_invalid(ns, [1340, 460])
    err_nodes, err_conns = error_branch(ns, 0, 760)
    note = sticky(ns, "README", (
        "## Gumroad -> Airtable: backfill & reconcile\n"
        "* **Manual run** = import ALL past sales once.\n"
        "* **Nightly** = re-check the last 3 days. Catches any ping Gumroad dropped "
        "(Gumroad retries a failed ping only 4 times, then gives up).\n\n"
        "Upsert on Sale ID, so running it 10 times still gives one row per sale.\n"
        "Needs credential *Gumroad API* (access token from Gumroad > Settings > Advanced > Applications)."
    ), [-600, 120], w=520, h=320)
    nodes = [note, manual, sched, all_time, recent, http, split, norm, iff, loop, air, wait, stop] + err_nodes
    m = lambda dst: {"node": dst, "type": "main", "index": 0}  # noqa: E731
    conns = {
        manual["name"]: {"main": [[m(all_time["name"])]]},
        sched["name"]: {"main": [[m(recent["name"])]]},
        all_time["name"]: {"main": [[m(http["name"])]]},
        recent["name"]: {"main": [[m(http["name"])]]},
        http["name"]: {"main": [[m(split["name"])]]},
        split["name"]: {"main": [[m(norm["name"])]]},
        norm["name"]: {"main": [[m(iff["name"])]]},
        iff["name"]: {"main": [[m(loop["name"])], [m(stop["name"])]]},
        loop["name"]: {"main": [[], [m(air["name"])]]},  # output 0 = done, 1 = loop
        air["name"]: {"main": [[m(wait["name"])]]},
        wait["name"]: {"main": [[m(loop["name"])]]},
    }
    conns.update(err_conns)
    return {
        "name": "Gumroad -> Airtable (backfill & nightly reconcile)",
        "nodes": nodes,
        "connections": conns,
        "active": False,
        "settings": {"executionOrder": "v1", "saveDataErrorExecution": "all",
                     "saveDataSuccessExecution": "all", "saveManualExecutions": True},
        "pinData": {},
        "meta": {"templateCredsSetupCompleted": False},
        "tags": [],
    }


if __name__ == "__main__":
    OUT.mkdir(exist_ok=True)
    for fname, wf, wid in (
        ("gumroad-to-airtable.json", build_live(), "GumroadAirtbl001"),
        ("gumroad-backfill-reconcile.json", build_backfill(), "GumroadBackfl001"),
    ):
        wf = {"id": wid, **wf}
        (OUT / fname).write_text(json.dumps(wf, indent=2, ensure_ascii=False) + "\n")
        print("wrote", OUT / fname, len(wf["nodes"]), "nodes")
