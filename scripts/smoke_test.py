"""End-to-end smoke test against a running API.

Exercises the paths a real user and a real support agent take:
  chat streaming -> rating -> escalation -> staff login -> RBAC -> KB lifecycle
  -> four-eyes enforcement -> promote-to-KB -> analytics -> audit chain.

    uvicorn app.main:app --app-dir api      # in another terminal
    python -u scripts/smoke_test.py
"""
import json
import sys
import time

import httpx

BASE = "http://localhost:8000"
PASSWORD = "dev12345"

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


def stream_chat(client: httpx.Client, session: str, message: str) -> dict:
    """Consume the SSE stream and return the final `done` payload plus the text."""
    text, meta = "", {}
    with client.stream(
        "POST", f"{BASE}/api/chat/stream",
        json={"session_id": session, "message": message, "tenant": "mof-contracts"},
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

    section("chat — in-scope question")
    session = f"smoke_{int(time.time())}"
    t0 = time.perf_counter()
    ans = stream_chat(client, session, "Sistemdə necə qeydiyyatdan keçə bilərəm?")
    elapsed = time.perf_counter() - t0
    check("stream returns text", len(ans.get("_text", "")) > 40, f"got {len(ans.get('_text',''))} chars")
    check("not escalated", ans.get("escalated") is False, f"confidence={ans.get('confidence')}")
    check("has sources", len(ans.get("sources") or []) > 0)
    check("has message_id", bool(ans.get("message_id")))
    check("answer is Azerbaijani", any(c in ans.get("_text", "") for c in "əğışçöü"))
    print(f"        confidence={ans.get('confidence')}  {elapsed:.1f}s  "
          f"tokens={ans.get('prompt_tokens')}/{ans.get('completion_tokens')}")
    print(f"        {ans.get('_text','')[:160]!r}")

    section("chat — multi-turn follow-up")
    follow = stream_chat(client, session, "Bəs onu sonra dəyişə bilərəmmi?")
    check("follow-up answered", len(follow.get("_text", "")) > 30)

    section("chat — out of scope")
    oos = stream_chat(client, f"{session}_oos", "Bakıda hava necədir?")
    check("out-of-scope escalates", oos.get("escalated") is True,
          f"confidence={oos.get('confidence')}")

    section("feedback")
    mid = ans.get("message_id")
    if mid:
        r = client.post(f"{BASE}/api/chat/rate", json={"message_id": mid, "value": 1})
        check("thumbs up accepted", r.status_code == 200, r.text[:120])
        r = client.post(f"{BASE}/api/chat/csat", json={"session_id": session, "score": 5})
        check("csat accepted", r.status_code == 200, r.text[:120])
        r = client.post(f"{BASE}/api/chat/escalate",
                        json={"session_id": session, "note": "smoke test", "message_id": mid})
        check("escalation created", r.status_code == 200 and r.json().get("ok"), r.text[:120])

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
    r = support.post(f"{BASE}/api/admin/kb", json={
        "question": "Smoke test sualı — silinməlidir",
        "answer": "Bu yazı avtomatik testdən yaranıb və silinməlidir.",
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
            "question": "Smoke four-eyes testi", "answer": "Öz yazısını təsdiqləyə bilməz.",
        })
        own_id = r.json().get("id") if r.status_code == 201 else None
        if own_id:
            admin.post(f"{BASE}/api/admin/kb/{own_id}/submit")
            r = admin.post(f"{BASE}/api/admin/kb/{own_id}/approve", json={})
            check("author cannot approve own entry (409)", r.status_code == 409,
                  f"{r.status_code} {r.text[:100]}")
            admin.post(f"{BASE}/api/admin/kb/{own_id}/archive")

        r = manager.post(f"{BASE}/api/admin/kb/{entry_id}/archive")
        check("manager archives entry", r.status_code == 200, r.text[:120])

    section("review queue")
    r = support.get(f"{BASE}/api/admin/escalations?status=open")
    items = r.json().get("items", []) if r.status_code == 200 else []
    check("escalations listed", r.status_code == 200 and len(items) > 0, f"{len(items)} open")

    if items:
        eid = items[0]["id"]
        r = support.get(f"{BASE}/api/admin/escalations/{eid}")
        body = r.json() if r.status_code == 200 else {}
        check("escalation has transcript", len(body.get("transcript") or []) > 0)
        check("escalation has retrieval diagnostics", "retrieved" in body)

        r = support.post(f"{BASE}/api/admin/escalations/{eid}/promote", json={
            "question": "Smoke promote sualı",
            "answer": "Söhbətdən yaradılmış cavab — silinməlidir.",
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

    section("audit")
    r = manager.get(f"{BASE}/api/admin/audit?limit=5")
    check("audit readable", r.status_code == 200 and len(r.json().get("items", [])) > 0)
    r = manager.get(f"{BASE}/api/admin/audit/verify")
    body = r.json() if r.status_code == 200 else {}
    check("audit chain valid", body.get("valid") is True, json.dumps(body))
    print(f"        chain: {body.get('checked')} entries verified")

    section("widget assets")
    for path in ("/widget", "/demo", "/static/embed.js"):
        r = client.get(f"{BASE}{path}")
        check(f"{path} serves", r.status_code == 200, str(r.status_code))

    print(f"\n{'=' * 46}\n  {passed} passed, {failed} failed\n{'=' * 46}")
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
