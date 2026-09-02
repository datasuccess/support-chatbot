"""Generate a synthetic Azerbaijani Q&A knowledge base for development.

WHY THIS EXISTS
---------------
The real knowledge base has not been supplied yet. Retrieval quality, the widget,
the review queue and the analytics views all need realistic content to be built
and measured against. This script produces that content.

WHAT IT IS NOT
--------------
Development scaffolding. Every row is written with source='synthetic' and MUST be
replaced with ministry-approved content before any real user sees it. The
procedures, button names and deadlines here are invented and plausible-sounding,
which makes them more dangerous than obvious nonsense, not less.

    SELECT count(*) FROM kb_entries WHERE source = 'synthetic';

must return 0 before go-live. See docs/RUNBOOK.md.

Usage:  python -u scripts/generate_seed_kb.py [--per-category 20]
"""
import argparse
import json
import os
import pathlib
import sys
import time

from dotenv import load_dotenv
from openai import OpenAI

load_dotenv()

# Topic map for an e-procurement / government-contracts portal.
CATEGORIES: dict[str, str] = {
    "Qeydiyyat və hesab": "sistemdə qeydiyyat, təşkilatın təsdiqi, istifadəçi profili, rolların idarə edilməsi",
    "Giriş və təhlükəsizlik": "sistemə giriş, ASAN İmza, şifrənin bərpası, ikifaktorlu doğrulama, hesabın bloklanması",
    "Tenderlərin axtarışı": "elan olunmuş tenderlərin axtarışı, filtrlər, kateqoriyalar, bildirişlərə abunə",
    "Təklifin hazırlanması": "təklifin yaradılması, qiymət cədvəli, texniki spesifikasiya, təklifin redaktəsi",
    "Təklifin göndərilməsi": "təklifin təsdiqi və göndərilməsi, son tarix, təklifin geri çağırılması",
    "Sənədlər və e-imza": "sənədlərin yüklənməsi, formatlar və ölçü limitləri, elektron imza ilə imzalama",
    "Müqavilə bağlanması": "qalibin elanı, müqavilənin imzalanması, müqaviləyə əlavələr",
    "Ödənişlər və hesabatlar": "ödəniş qrafiki, hesab-fakturalar, icra hesabatları, təminat məbləği",
    "Şikayət və apellyasiya": "şikayətin verilməsi, baxılma müddəti, apellyasiya prosedurası",
    "Texniki dəstək": "sistem xətaları, brauzer problemləri, faylın yüklənməməsi, səhifənin açılmaması",
}

PROMPT = """Sən Azərbaycan Respublikası Maliyyə Nazirliyinin dövlət satınalmaları \
(e-tender) portalı üçün dəstək bazası hazırlayırsan.

Kateqoriya: "{category}"
Mövzular: {topics}

Bu kateqoriya üçün {n} ədəd sual-cavab cütü yarat. Qaydalar:
- Hər şey TAM Azərbaycan dilində olmalıdır.
- Suallar real istifadəçilərin yazdığı kimi olsun: qısa, bəzən qeyri-rəsmi.
- Cavablar praktiki və addım-addım olsun (2-5 addım), 40-120 söz arası.
- Cavabda konkret düymə/bölmə adları işlət (məsələn: «Təkliflər» bölməsi, «Yeni təklif» düyməsi).
- Hər cavab üçün qısa mənbə adı ver (citation), məsələn: «İstifadəçi təlimatı — Təkliflərin verilməsi».
- 2-4 açar söz (tags) əlavə et.
- Sualları təkrarlama, müxtəlif aspektləri əhatə et.

Cavabı YALNIZ bu JSON formatında qaytar, başqa heç nə yazma:
{{"items": [{{"question": "...", "answer": "...", "citation": "...", "tags": ["...", "..."]}}]}}"""


def build_client() -> OpenAI:
    key = os.environ.get("DEEPSEEK_API_KEY", "")
    if not key:
        sys.exit("DEEPSEEK_API_KEY missing from .env")
    # Explicit timeout: the OpenAI SDK defaults to none, which turns one stuck
    # call into an indefinite hang.
    return OpenAI(
        api_key=key,
        base_url=os.environ.get("DEEPSEEK_BASE_URL", "https://api.deepseek.com"),
        timeout=180.0,
        max_retries=2,
    )


def generate(client: OpenAI, category: str, topics: str, n: int) -> list[dict]:
    resp = client.chat.completions.create(
        model=os.environ.get("DEEPSEEK_MODEL", "deepseek-chat"),
        messages=[{"role": "user", "content": PROMPT.format(category=category, topics=topics, n=n)}],
        response_format={"type": "json_object"},
        temperature=1.0,
        max_tokens=8000,
    )
    payload = json.loads(resp.choices[0].message.content)
    items = payload.get("items", [])
    for it in items:
        it["category"] = category
    usage = resp.usage
    return items, (usage.prompt_tokens, usage.completion_tokens)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--per-category", type=int, default=20)
    ap.add_argument("--out", default="data/seed_kb.json")
    args = ap.parse_args()

    client = build_client()
    all_items: list[dict] = []
    tok_in = tok_out = 0

    for i, (category, topics) in enumerate(CATEGORIES.items(), start=1):
        t0 = time.perf_counter()
        try:
            items, (pi, po) = generate(client, category, topics, args.per_category)
        except Exception as exc:  # noqa: BLE001 - one bad category must not lose the rest
            print(f"[{i:2}/{len(CATEGORIES)}] {category:28} FAILED: {exc}")
            continue
        tok_in += pi
        tok_out += po
        all_items.extend(items)
        print(f"[{i:2}/{len(CATEGORIES)}] {category:28} {len(items):3} items  "
              f"{time.perf_counter() - t0:5.1f}s")

    out = pathlib.Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(all_items, ensure_ascii=False, indent=2), encoding="utf-8")

    cost = tok_in / 1e6 * 0.27 + tok_out / 1e6 * 1.10
    print(f"\n{len(all_items)} entries -> {out}")
    print(f"tokens in={tok_in} out={tok_out}  approx cost ${cost:.4f}")


if __name__ == "__main__":
    main()
