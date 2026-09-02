"""Lazy-loaded local models: bge-m3 embeddings + bge-reranker-v2-m3 cross-encoder.

Both run on CPU. Loading costs ~10-30s and ~2.5GB of RAM, so they are loaded once
at application startup (see main.py lifespan) rather than per request.

Why a reranker at all: the embedding model encodes query and document separately,
so it never actually compares them — it compares two lossy summaries. The
cross-encoder reads both together and scores the pair directly. That gap widens on
lower-resource languages like Azerbaijani, which is exactly our case.
"""
import logging
import threading

from sentence_transformers import CrossEncoder, SentenceTransformer

from app.core.config import settings

log = logging.getLogger(__name__)

_embedder: SentenceTransformer | None = None
_reranker: CrossEncoder | None = None
_lock = threading.Lock()


def get_embedder() -> SentenceTransformer:
    global _embedder
    if _embedder is None:
        with _lock:
            if _embedder is None:
                log.info("loading embedding model %s (cpu)", settings.embedding_model)
                _embedder = SentenceTransformer(settings.embedding_model, device="cpu")
    return _embedder


def get_reranker() -> CrossEncoder:
    global _reranker
    if _reranker is None:
        with _lock:
            if _reranker is None:
                log.info("loading reranker %s (cpu)", settings.reranker_model)
                _reranker = CrossEncoder(settings.reranker_model, device="cpu", max_length=512)
    return _reranker


def embed_one(text: str) -> list[float]:
    # bge-m3 needs no instruction prefix, unlike the e5 family.
    vec = get_embedder().encode(text, normalize_embeddings=True)
    return vec.tolist()


def embed_many(texts: list[str], batch_size: int = 8) -> list[list[float]]:
    vecs = get_embedder().encode(
        texts, normalize_embeddings=True, batch_size=batch_size, show_progress_bar=False
    )
    return [v.tolist() for v in vecs]


def rerank(query: str, documents: list[str]) -> list[float]:
    """Relevance scores for (query, doc) pairs. Higher is better."""
    if not documents:
        return []
    pairs = [(query, d) for d in documents]
    scores = get_reranker().predict(pairs, batch_size=8, show_progress_bar=False)
    return [float(s) for s in scores]


def warmup() -> None:
    """Force both models into memory and run one tiny inference each."""
    embed_one("salam")
    rerank("salam", ["salam dünya"])
    log.info("models warmed up")
