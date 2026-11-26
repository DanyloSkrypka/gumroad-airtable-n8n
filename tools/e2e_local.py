#!/usr/bin/env python3
"""End-to-end check against a LOCAL n8n started by tools/run_local_n8n.sh (fake credentials).

Sends the sample Gumroad ping (form-encoded) + an invalid one to the real webhook, then reads
the executions from n8n's SQLite DB and prints which nodes ran, their output and errors.
With a fake Airtable token the Airtable node MUST fail -> Error Trigger -> alert email
(captured by tools/fake_smtp.py in e2e_out/mail.log). That is the error path working.
"""
import json
import sqlite3
import time
import urllib.request
from pathlib import Path
from urllib.parse import urlencode

ROOT = Path(__file__).resolve().parent.parent
URL = "http://127.0.0.1:5690/webhook/gumroad-sale"
DB = ROOT / ".n8n-local" / ".n8n" / "database.sqlite"
SAMPLE = json.loads((ROOT / "samples" / "sample_sale.json").read_text())


def post(fields):
    req = urllib.request.Request(URL, data=urlencode(fields).encode(), method="POST",
                                 headers={"Content-Type": "application/x-www-form-urlencoded"})
    t = time.time()
    with urllib.request.urlopen(req, timeout=10) as r:
        return r.status, r.read().decode()[:120], round(time.time() - t, 3)


def unflatten(data_str):
    """n8n stores execution data in 'flatted' format: list where refs are string indexes."""
    arr = json.loads(data_str)

    def res(v, seen=0):
        if seen > 60:
            return v
        if isinstance(v, str) and v.isdigit() and int(v) < len(arr):
            return res(arr[int(v)], seen + 1)
        if isinstance(v, dict):
            return {k: res(x, seen + 1) for k, x in v.items()}
        if isinstance(v, list):
            return [res(x, seen + 1) for x in v]
        return v
    return res(arr[0])


def main():
    before = sqlite3.connect(DB).execute("select coalesce(max(id),0) from execution_entity").fetchone()[0]
    print("valid ping   ->", post(SAMPLE))
    print("invalid ping ->", post({**SAMPLE, "email": "not-an-email", "sale_id": "FAKEsale_bad=="}))
    time.sleep(25)  # Airtable node retries 3x with 2 s pause, then error workflow runs
    con = sqlite3.connect(DB)
    rows = con.execute(
        "select e.id, e.workflowId, e.mode, e.status, d.data from execution_entity e "
        "join execution_data d on d.executionId = e.id where e.id > ? order by e.id", (before,)).fetchall()
    for eid, wid, mode, status, data in rows:
        run = unflatten(data).get("resultData", {})
        print(f"\n# execution {eid} workflow={wid} mode={mode} status={status}")
        for node, runs in (run.get("runData") or {}).items():
            r = runs[-1]
            err = (r.get("error") or {}).get("message")
            out = r.get("data", {}).get("main", [[]])
            first = next((b[0]["json"] for b in out if b), None)
            print(f"  - {node}: tries={len(runs)} {'ERROR: ' + str(err)[:160] if err else ''}")
            if first and node in ("Normalize & validate", "Format alert"):
                print("    ", json.dumps(first, ensure_ascii=False)[:700])
        if run.get("error"):
            print("  workflow error:", str(run["error"].get("message"))[:200])
    mail = ROOT / "e2e_out" / "mail.log"
    print("\nmails captured:", mail.read_text().count("=== MAIL") if mail.exists() else 0)


if __name__ == "__main__":
    main()
