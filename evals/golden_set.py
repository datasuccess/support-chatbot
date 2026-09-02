"""Golden evaluation set for retrieval.

Each case is a question phrased the way a real user would ask it — NOT the way the
knowledge-base entry is worded — paired with keywords that must appear in the
correct entry. Paraphrase is the entire point: matching an entry against its own
title proves nothing.

`expect_keywords` is used instead of hard-coded entry ids because entry ids change
whenever the knowledge base is regenerated, which would make the suite fail for
reasons that have nothing to do with retrieval quality.

When the real ministry content arrives, this file must be rewritten by someone who
knows the domain. An eval set written by the same model that wrote the content
measures self-consistency, not correctness.
"""

GOLDEN: list[dict] = [
    # --- registration ---
    {"q": "yeni şirkət kimi necə qeydiyyatdan keçim?",
     "keywords": ["qeydiyyat"], "category": "Qeydiyyat və hesab"},
    {"q": "VÖEN-i sistem qəbul etmir",
     "keywords": ["VÖEN"], "category": "Qeydiyyat və hesab"},
    {"q": "profil məlumatlarımı dəyişmək istəyirəm",
     "keywords": ["profil"], "category": "Qeydiyyat və hesab"},

    # --- login & security ---
    {"q": "şifrəmi unutmuşam nə edim",
     "keywords": ["şifrə"], "category": "Giriş və təhlükəsizlik"},
    {"q": "asan imza ilə girə bilmirəm",
     "keywords": ["ASAN", "İmza", "imza"], "category": "Giriş və təhlükəsizlik"},
    {"q": "hesabım bağlanıb, açmaq üçün nə etməliyəm",
     "keywords": ["blok", "hesab"], "category": "Giriş və təhlükəsizlik"},

    # --- searching tenders ---
    {"q": "hansı tenderlər var, necə tapım",
     "keywords": ["tender", "axtar"], "category": "Tenderlərin axtarışı"},
    {"q": "yeni elanlardan xəbərdar olmaq istəyirəm",
     "keywords": ["bildiriş", "abunə"], "category": "Tenderlərin axtarışı"},

    # --- preparing a bid ---
    {"q": "qiymət cədvəlini necə dolduraram",
     "keywords": ["qiymət"], "category": "Təklifin hazırlanması"},
    {"q": "təklifimi hazırlayıram amma saxlaya bilmirəm",
     "keywords": ["təklif"], "category": "Təklifin hazırlanması"},

    # --- submitting ---
    {"q": "təklifi göndərdikdən sonra dəyişə bilərəmmi",
     "keywords": ["geri", "dəyiş", "redaktə"], "category": "Təklifin göndərilməsi"},
    {"q": "son tarix keçib, hələ də göndərə bilərəm?",
     "keywords": ["son tarix", "müddət"], "category": "Təklifin göndərilməsi"},

    # --- documents & e-signature ---
    {"q": "faylı yükləyə bilmirəm çox böyükdür deyir",
     "keywords": ["ölçü", "fayl", "MB", "limit"], "category": "Sənədlər və e-imza"},
    {"q": "hansı formatda sənəd qəbul olunur",
     "keywords": ["format", "PDF"], "category": "Sənədlər və e-imza"},
    {"q": "sənədi elektron imzalamaq lazımdır?",
     "keywords": ["imza"], "category": "Sənədlər və e-imza"},

    # --- contracts ---
    {"q": "tenderi udmuşuq, indi nə olacaq",
     "keywords": ["qalib", "müqavilə"], "category": "Müqavilə bağlanması"},
    {"q": "müqaviləyə əlavə etmək olar?",
     "keywords": ["əlavə", "müqavilə"], "category": "Müqavilə bağlanması"},

    # --- payments ---
    {"q": "pul nə vaxt köçürüləcək",
     "keywords": ["ödəniş"], "category": "Ödənişlər və hesabatlar"},
    {"q": "hesab-fakturanı haradan götürüm",
     "keywords": ["faktura", "hesab"], "category": "Ödənişlər və hesabatlar"},
    {"q": "təminat məbləği nə qədərdir",
     "keywords": ["təminat"], "category": "Ödənişlər və hesabatlar"},

    # --- complaints ---
    {"q": "nəticədən narazıyam şikayət etmək istəyirəm",
     "keywords": ["şikayət"], "category": "Şikayət və apellyasiya"},
    {"q": "şikayətə neçə gün baxılır",
     "keywords": ["gün", "müddət", "şikayət"], "category": "Şikayət və apellyasiya"},

    # --- technical ---
    {"q": "sayt açılmır xəta verir",
     "keywords": ["xəta", "brauzer", "səhifə"], "category": "Texniki dəstək"},
    {"q": "hansı brauzerdən istifadə etməliyəm",
     "keywords": ["brauzer"], "category": "Texniki dəstək"},
]

# Questions that must NOT be answered from the knowledge base. The bot's scope is
# this system; anything else should fall through to escalation.
OUT_OF_SCOPE: list[str] = [
    "Bakıda hava necədir?",
    "Mənə plov resepti de",
    "Dollar məzənnəsi nə qədərdir?",
    "2+2 neçə edir?",
]
