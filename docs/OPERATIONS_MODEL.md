# Operations model

How work reaches a human, who owns it, and why the queue is split in two.

---

## The core distinction

An earlier version treated every unanswered question as an "escalation". That was
wrong, and it fails in a specific way: the operator queue fills with political
questions, typos and idle curiosity, and the three people genuinely waiting for a
phone call are buried underneath.

Two different things, separated:

| | **Content gap** | **Contact request** |
|---|---|---|
| Created by | The system, automatically | The user, explicitly |
| Trigger | Low confidence, or 👎 | They filled in the contact form |
| Means | The knowledge base is missing something | A person is waiting for a reply |
| Owner | Support, as content work | Support, as a service commitment |
| Contact details | None | **Required** — name + phone or email |
| Reference code | No | Yes, shown to the user |
| Urgency | Batch it | Clock is running |
| SLA applies | No | Yes |

Both live in `escalations`, separated by `kind`. The console lists them
separately and sorts contact requests first.

A third outcome creates neither: **refusals**. Off-topic, political and abusive
questions go to the `refusals` table for monitoring. Nobody owes that person a
call, and routing them into the queue would recreate the problem.

## When the handover is offered

The button is **not** under every answer. A permanent "contact support" control
beneath every reply signals that the bot has no confidence in itself, and trains
people to skip straight past it to a human — which defeats the point of building
it.

It appears when:

| Trigger | Reasoning |
|---|---|
| Confidence below threshold | The bot already admitted it does not know |
| Question refused as out of scope | Nothing more the bot can offer |
| **Two 👎 in a row** | Not "an answer was wrong" but "this is not working" |
| The header link, always | Available without being pushed |

The header link is deliberately quiet — small, outlined, no fill. Discoverable
when wanted, not competing with the conversation.

## What the user does

1. Clicks **«Operatorla əlaqə»**
2. Fills in name, preferred channel (phone or email), the matching detail, and an
   optional note
3. Gets a reference code: `DS-WNUHQH`

Contact details are **required and validated server-side**. A ticket with no way
to reach the person is worse than no ticket — it looks like work in the queue but
cannot be actioned.

Clicking twice reuses the existing open request rather than creating a second
ticket for the same person.

## What the operator gets

Opening a contact request in the console shows:

* **Contact details** — name, channel, number or address, reference code
* **The full transcript** — so the person never repeats their question
* **Retrieval diagnostics** — every candidate the search considered, with scores

That third block distinguishes two failure modes that look identical from outside
and need opposite fixes:

* Nothing was retrieved → **content gap**. Write a new entry.
* The right entry was retrieved but ranked 7th → **retrieval gap**. Adding a
  near-duplicate makes retrieval *worse*; fix the wording of the existing entry
  or the retrieval settings instead.

Missing this distinction is how knowledge bases rot: near-duplicate entries
accumulate, split the retrieval signal, and quality falls as content grows.

## The flywheel

```
user asks something the bot cannot answer
        │
        ├──▶ content gap logged automatically
        │
        ▼
support reads it + the retrieval trace
        │
        ├──▶ entry existed but ranked poorly → fix wording
        │
        └──▶ nothing existed → «Bilik bazasına əlavə et»
                    │
                    ▼
             draft created (never published directly)
                    │
                    ▼
             manager approves — four-eyes, author cannot self-approve
                    │
                    ▼
             published, embedded, retrievable
```

Every step is audited. `v_operator_stats.turned_into_kb_entries` counts how often
the loop actually closes — the single best indicator of whether the system is
improving or merely being used.

## Roles

| | support | manager | admin |
|---|:--:|:--:|:--:|
| Work the queue, contact users | ✅ | ✅ | ✅ |
| Draft and edit entries | ✅ | — | ✅ |
| Promote a conversation to a draft | ✅ | — | ✅ |
| Approve / publish / archive | — | ✅ | ✅ |
| Analytics and attribution | — | ✅ | ✅ |
| Audit log and chain verification | — | ✅ | ✅ |
| Staff accounts, tenant settings | — | — | ✅ |

Four-eyes is enforced on **identity, not role**: an admin cannot approve their own
draft either.

## Metrics that matter

| Metric | View | What it tells you |
|---|---|---|
| Deflection rate | `v_deflection` | Share of answers that avoided a human. The headline number |
| Open contact requests | `v_queue_health` | People currently waiting |
| Open over 24h | `v_queue_health` | Service failures |
| Gap → entry conversion | `v_operator_stats` | Whether the flywheel actually turns |
| Entries never retrieved | `v_entry_usage` | Content answering questions nobody asks |
| Refusals by category | `v_refusal_stats` | Abuse patterns, and over-aggressive guarding |
| Contributor stats | `v_contributor_stats` | Who writes, who edits, who is stuck |

Watch the refusal stats in both directions. Rising political refusals is the guard
working. Rising `out_of_scope` refusals on legitimate procurement questions means
the threshold is too high or the knowledge base too thin.
