"""Input guard: refuse clearly out-of-scope questions before spending any work.

Layered defence, cheapest first
-------------------------------
1. **This guard** — pattern match for political, abusive and personal-data
   requests. No retrieval, no LLM call, no cost, no latency.
2. **The retrieval gate** — the primary and strongest control. A question with no
   match in the knowledge base scores near zero and is refused. Measured
   separation on the golden set is stark: in-scope median 0.968 against an
   off-topic ceiling of 0.0033.
3. **The system prompt** — grounding stated as an absolute, with a per-tenant
   scope description.

Layer 2 does the real work. This guard exists because a politically loaded
question can incidentally share vocabulary with the knowledge base — "tender
saxtakarlığı" overlaps procurement content — and might score high enough to reach
the model. Structural refusal is more reliable than asking the model nicely.

Deliberately conservative: patterns match topics the bot must never discuss, not
merely unusual phrasing. A false refusal is visible in `v_refusal_stats` and easy
to correct; a political answer published under a ministry logo is not.
"""
import re

# Azerbaijani plus common Russian/English forms, since users mix languages.
_POLITICAL = [
    r"\bprezident\b", r"\bhökumət", r"\bnazir\b", r"\bnazirlik.*\b(pis|yaxşı|korrup)",
    r"\bsiyasi\b", r"\bsiyasət", r"\bseçki", r"\bmüxalifət", r"\bpartiya\b",
    r"\bdeputat", r"\bkorrupsiya", r"\brüşvət", r"\bqanunsuz\s+(pul|sxem)",
    r"\bmüharibə", r"\bqarabağ", r"\bermən", r"\bsanksiya",
    r"\bпрезидент", r"\bправительств", r"\bкоррупц", r"\bвыбор[ыа]\b", r"\bполитик",
    r"\bpresident\b", r"\bgovernment\s+(is|policy)", r"\bcorrupt", r"\belection",
]

_ABUSIVE = [
    r"\bsöy(üş|ürəm)", r"\bax[mM]aq\b", r"\bidiot\b", r"\bstupid\b",
    r"\bhack\b", r"\bexploit\b", r"\bsql\s*injection", r"\bddos\b",
    r"ignore\s+(all\s+)?(previous|above)\s+instructions",
    r"əvvəlki\s+göstərişləri\s+(unut|nəzərə\s+alma)",
    r"\bsystem\s+prompt\b", r"\bsistem\s+promptu",
]

# Requests for someone else's data. The bot has no access to it, and must not
# imply that it might.
_PERSONAL_DATA = [
    r"\b(kimin|kimə)\s+(məxsus|aid)\b.*\b(şirkət|təşkilat|VÖEN)\b",
    r"\bbaşqa\s+(şirkət|iştirakçı)nın\s+(təklif|qiymət)",
    r"\brəqib(imiz|in)?\s+(neçəyə|qiymət)",
    r"\bkim\s+(qalib|udub)\b.*\bgizli\b",
]

_COMPILED = [
    ("political", [re.compile(p, re.IGNORECASE) for p in _POLITICAL]),
    ("abusive", [re.compile(p, re.IGNORECASE) for p in _ABUSIVE]),
    ("personal_data", [re.compile(p, re.IGNORECASE) for p in _PERSONAL_DATA]),
]


def classify(question: str) -> str | None:
    """Return a refusal category, or None to let the question proceed."""
    text = question.strip()
    if not text:
        return None
    for category, patterns in _COMPILED:
        if any(p.search(text) for p in patterns):
            return category
    return None
