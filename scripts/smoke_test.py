"""End-to-end smoke test against a running API.

Exercises the paths a real user and a real support agent take:
  chat streaming -> rating -> escalation -> staff login -> RBAC -> KB lifecycle
  -> four-eyes enforcement -> promote-to-KB -> analytics -> audit chain.

    uvicorn app.main:app --app-dir api      # in another terminal
    python -u scripts/smoke_test.py
"""
import json
import subprocess
import sys
import time

import httpx

BASE = "http://localhost:8000"
PASSWORD = "dev12345"


def site_key() -> str:
    out = subprocess.run(
        ["docker", "exec", "support_chatbot_db", "psql", "-U", "chatbot", "-d",
         "support_chatbot", "-tAc",
         "SELECT site_key FROM tenants WHERE slug='mof-contracts'"],
        capture_output=True, text=True,
    )
    return out.stdout.strip()


def new_session(key: str) -> str:
    """Create a conversation, respecting the rate limiter's Retry-After.

    The suites share a per-IP budget, so running them back to back legitimately
    trips the limiter. Backing off is correct behaviour for a client; weakening the
    limiter to make tests pass would defeat the control being tested.
    """
    for attempt in range(4):
        r = httpx.post(f"{BASE}/api/chat/session", json={"site_key": key}, timeout=30)
        if r.status_code == 429:
            wait = int(r.headers.get("Retry-After", "5"))
            print(f"  … rate limited, waiting {wait}s")
            time.sleep(min(wait, 65))
            continue
        r.raise_for_status()
        return r.json()["session_token"]
    raise RuntimeError("still rate limited after retries")

passed = failed = 0


def check(name: str, ok: bool, detail: str = "") -> None:
    global passed, failed
    if ok:
        passed += 1
        print(f"  PASS  {name}")
    else:
        failed += 1
        print(f"  FAIL  {name}  {detail}")


def section(title: str) -> None:
    print(f"\n=== {title} ===")


def stream_chat(client: httpx.Client, token: str, message: str) -> dict:
    """Consume the SSE stream and return the final `done` payload plus the text."""
    text, meta = "", {}
    with client.stream(
        "POST", f"{BASE}/api/chat/stream",
        headers={"Authorization": f"Bearer {token}"},
        json={"message": message},
        timeout=120.0,
    ) as r:
        if r.status_code != 200:
            return {"_status": r.status_code}
        event, data_lines = "message", []
        for line in r.iter_lines():
            if line.startswith("event: "):
                event = line[7:].strip()
            elif line.startswith("data: "):
                data_lines.append(line[6:])
            elif line == "":
                data = "\n".join(data_lines)
                if event == "token":
                    text += data
                elif event == "done":
                    try:
                        meta = json.loads(data)
                    except json.JSONDecodeError:
                        pass
                data_lines = []
    meta["_text"] = text
    return meta


def main() -> None:
    client = httpx.Client(follow_redirects=True)

    section("health")
    r = client.get(f"{BASE}/health", timeout=30)
    check("health returns ok", r.status_code == 200 and r.json().get("database") is True, r.text[:120])

    section("session")
    key = site_key()
    check("site key readable", bool(key))
    session = new_session(key)
    check("session token issued", bool(session))

    section("chat — in-scope question")
    t0 = time.perf_counter()
    ans = stream_chat(client, session, "Sistemdə necə qeydiyyatdan keçə bilərəm?")
    elapsed = time.perf_counter() - t0
    check("stream returns text", len(ans.get("_text", "")) > 40, f"got {len(ans.get('_text',''))} chars")
    check("not escalated", ans.get("escalated") is False, f"confidence={ans.get('confidence')}")
    check("has sources", len(ans.get("sources") or []) > 0)
    check("has message_id", bool(ans.get("message_id")))
    check("answer is Azerbaijani", any(c in ans.get("_text", "") for c in "əğışçöü"))
    check("mode is grounded", ans.get("mode") == "grounded", str(ans.get("mode")))
    check("no handover offered on a good answer", ans.get("offer_contact") is False)
    print(f"        confidence={ans.get('confidence')}  {elapsed:.1f}s  "
          f"tokens={ans.get('prompt_tokens')}/{ans.get('completion_tokens')}")
    print(f"        {ans.get('_text','')[:160]!r}")

    section("chat — multi-turn follow-up")
    follow = stream_chat(client, session, "Bəs onu sonra dəyişə bilərəmmi?")
    check("follow-up answered", len(follow.get("_text", "")) > 30)

    section("chat — scope enforcement")
    oos = stream_chat(client, new_session(key), "Bakıda hava necədir?")
    check("off-topic refused", oos.get("mode") == "refused", str(oos.get("mode")))

    pol = stream_chat(client, new_session(key), "Prezident haqqında nə düşünürsən?")
    check("political question refused", pol.get("mode") == "refused", str(pol.get("mode")))
    check("refusal is categorised", bool(pol.get("refusal_category")),
          str(pol.get("refusal_category")))

    # A refusal must not become an operator task.
    out = subprocess.run(
        ["docker", "exec", "support_chatbot_db", "psql", "-U", "chatbot", "-d",
         "support_chatbot", "-tAc",
         "SELECT count(*) FROM escalations e JOIN refusals r "
         "ON r.message_id = e.message_id"],
        capture_output=True, text=True,
    )
    check("refusals do not create operator tasks", out.stdout.strip() == "0",
          f"{out.stdout.strip()} leaked into the queue")

    section("feedback")
    auth = {"Authorization": f"Bearer {session}"}
    mid = ans.get("message_id")
    if mid:
        r = client.post(f"{BASE}/api/chat/rate", headers=auth,
                        json={"message_id": mid, "value": 1})
        check("thumbs up accepted", r.status_code == 200, r.text[:120])
        r = client.post(f"{BASE}/api/chat/csat", headers=auth, json={"score": 5})
        check("csat accepted", r.status_code == 200, r.text[:120])

    section("contact request")
    r = client.post(f"{BASE}/api/chat/contact", headers=auth, json={
        "name": "Smoke Test", "channel": "phone", "phone": "+994501112233",
        "note": "smoke test contact request"})
    body = r.json() if r.status_code == 200 else {}
    check("contact request accepted", r.status_code == 200, r.text[:150])
    check("reference code returned", bool(body.get("reference_code")),
          str(body.get("reference_code")))
    print(f"        reference: {body.get('reference_code')}")

    r = client.post(f"{BASE}/api/chat/contact", headers=auth, json={
        "name": "Smoke Test", "channel": "phone", "phone": "+994501112233"})
    check("duplicate request reuses the same ticket",
          r.status_code == 200 and r.json().get("duplicate") is True, r.text[:120])

    r = client.post(f"{BASE}/api/chat/contact", headers={"Authorization": f"Bearer {new_session(key)}"},
                    json={"name": "No Contact", "channel": "phone"})
    check("contact request without a number rejected", r.status_code == 422,
          f"HTTP {r.status_code}")

    section("auth + RBAC")
    support = httpx.Client(follow_redirects=True)
    r = support.post(f"{BASE}/api/admin/login",
                     json={"email": "support@mof.local", "password": PASSWORD})
    check("support logs in", r.status_code == 200, r.text[:120])

    manager = httpx.Client(follow_redirects=True)
    r = manager.post(f"{BASE}/api/admin/login",
                     json={"email": "manager@mof.local", "password": PASSWORD})
    check("manager logs in", r.status_code == 200, r.text[:120])

    r = httpx.post(f"{BASE}/api/admin/login",
                   json={"email": "support@mof.local", "password": "wrong-password"})
    check("bad password rejected", r.status_code == 401, str(r.status_code))

    r = httpx.get(f"{BASE}/api/admin/kb")
    check("anonymous blocked from admin", r.status_code == 401, str(r.status_code))

    r = support.get(f"{BASE}/api/admin/analytics/overview")
    check("support blocked from analytics (403)", r.status_code == 403, str(r.status_code))

    r = manager.get(f"{BASE}/api/admin/analytics/overview")
    check("manager reads analytics", r.status_code == 200, r.text[:120])

    section("KB lifecycle + four-eyes")
    stamp = int(time.time())
    r = support.post(f"{BASE}/api/admin/kb", json={
        "question": f"Smoke test sualı {stamp} — silinməlidir",
        "answer": f"Bu yazı avtomatik testdən yaranıb ({stamp}) və silinməlidir.",
        "category": "Texniki dəstək", "tags": ["smoke"], "citation": "smoke test",
    })
    check("support creates draft", r.status_code == 201, r.text[:150])
    entry_id = r.json().get("id") if r.status_code == 201 else None

    if entry_id:
        r = support.post(f"{BASE}/api/admin/kb/{entry_id}/approve", json={})
        check("support cannot approve (403)", r.status_code == 403, str(r.status_code))

        r = manager.post(f"{BASE}/api/admin/kb/{entry_id}/approve", json={})
        check("cannot approve before submit (409)", r.status_code == 409, str(r.status_code))

        r = support.post(f"{BASE}/api/admin/kb/{entry_id}/submit")
        check("support submits for approval", r.status_code == 200, r.text[:120])

        r = manager.post(f"{BASE}/api/admin/kb/{entry_id}/approve",
                         json={"note": "smoke test approval"})
        check("manager approves", r.status_code == 200, r.text[:150])

        r = manager.get(f"{BASE}/api/admin/kb/{entry_id}")
        body = r.json() if r.status_code == 200 else {}
        check("entry is published", body.get("status") == "published", str(body.get("status")))

        # Four-eyes: an admin authoring their own entry must not approve it.
        admin = httpx.Client(follow_redirects=True)
        admin.post(f"{BASE}/api/admin/login",
                   json={"email": "admin@mof.local", "password": PASSWORD})
        r = admin.post(f"{BASE}/api/admin/kb", json={
            "question": f"Smoke four-eyes testi {stamp}",
            "answer": f"Öz yazısını təsdiqləyə bilməz ({stamp}).",
        })
        own_id = r.json().get("id") if r.status_code == 201 else None
        if own_id:
            admin.post(f"{BASE}/api/admin/kb/{own_id}/submit")
            r = admin.post(f"{BASE}/api/admin/kb/{own_id}/approve", json={})
            check("author cannot approve own entry (409)", r.status_code == 409,
                  f"{r.status_code} {r.text[:100]}")
            admin.post(f"{BASE}/api/admin/kb/{own_id}/archive")

        r = support.post(f"{BASE}/api/admin/kb", json={
            "question": f"Smoke test sualı {stamp} — silinməlidir",
            "answer": f"Bu yazı avtomatik testdən yaranıb ({stamp}) və silinməlidir.",
        })
        check("duplicate content rejected with 409", r.status_code == 409,
              f"HTTP {r.status_code}")

        r = manager.post(f"{BASE}/api/admin/kb/{entry_id}/archive")
        check("manager archives entry", r.status_code == 200, r.text[:120])

    section("review queue")
    r = support.get(f"{BASE}/api/admin/escalations?status=open&kind=contact_request")
    items = r.json().get("items", []) if r.status_code == 200 else []
    check("contact requests listed", r.status_code == 200 and len(items) > 0,
          f"{len(items)} open")
    check("contact request carries contact details",
          bool(items and items[0].get("contact_name") and items[0].get("reference_code")))

    r = support.get(f"{BASE}/api/admin/escalations?status=open&kind=content_gap")
    check("content gaps listed separately", r.status_code == 200, str(r.status_code))

    if items:
        eid = items[0]["id"]
        r = support.get(f"{BASE}/api/admin/escalations/{eid}")
        body = r.json() if r.status_code == 200 else {}
        check("escalation has transcript", len(body.get("transcript") or []) > 0)
        check("escalation has retrieval diagnostics", "retrieved" in body)

        r = support.post(f"{BASE}/api/admin/escalations/{eid}/promote", json={
            "question": f"Smoke promote sualı {stamp}",
            "answer": f"Söhbətdən yaradılmış cavab ({stamp}) — silinməlidir.",
            "category": "Texniki dəstək",
        })
        check("promote creates draft", r.status_code == 201 and r.json().get("status") == "draft",
              r.text[:150])
        if r.status_code == 201:
            manager.post(f"{BASE}/api/admin/kb/{r.json()['entry_id']}/archive")

    section("analytics")
    r = manager.get(f"{BASE}/api/admin/analytics/overview?days=1")
    body = r.json() if r.status_code == 200 else {}
    check("overview has kb_health", bool(body.get("kb_health")))
    check("overview has deflection", "deflection" in body)
    kb = body.get("kb_health") or {}
    print(f"        kb: total={kb.get('total')} published={kb.get('published')} "
          f"synthetic={kb.get('synthetic')}")

    r = support.get(f"{BASE}/api/admin/analytics/gaps")
    check("gaps queue readable by support", r.status_code == 200, str(r.status_code))

    section("attribution analytics")
    for path, name in [
        ("/api/admin/analytics/contributors", "who wrote what"),
        ("/api/admin/analytics/approvers", "who approved what"),
        ("/api/admin/analytics/operators", "operator workload"),
        ("/api/admin/analytics/activity", "activity feed"),
        ("/api/admin/analytics/refusals", "refusal stats"),
        ("/api/admin/analytics/queue-health", "queue health"),
    ]:
        r = manager.get(f"{BASE}{path}")
        check(f"{name}", r.status_code == 200, f"HTTP {r.status_code}")

    r = manager.get(f"{BASE}/api/admin/analytics/contributors")
    rows = r.json().get("items", []) if r.status_code == 200 else []
    check("contributor stats attribute entries to authors", len(rows) > 0,
          f"{len(rows)} contributors")

    section("tenant settings")
    r = manager.get(f"{BASE}/api/admin/tenant")
    body = r.json() if r.status_code == 200 else {}
    check("tenant settings readable", r.status_code == 200 and bool(body.get("site_key")))

    section("audit")
    r = manager.get(f"{BASE}/api/admin/audit?limit=5")
    check("audit readable", r.status_code == 200 and len(r.json().get("items", [])) > 0)
    r = manager.get(f"{BASE}/api/admin/audit/verify")
    body = r.json() if r.status_code == 200 else {}
    check("audit chain valid", body.get("valid") is True, json.dumps(body))
    print(f"        chain: {body.get('checked')} entries verified")

    section("widget + console assets")
    for path in ("/widget", "/demo", "/console", "/static/embed.js"):
        r = client.get(f"{BASE}{path}")
        check(f"{path} serves", r.status_code == 200, str(r.status_code))

    print(f"\n{'=' * 46}\n  {passed} passed, {failed} failed\n{'=' * 46}")
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
