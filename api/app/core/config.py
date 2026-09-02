"""Application configuration, loaded from environment / .env."""
from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # --- database ---
    database_url: str = "postgresql://chatbot:chatbot_local_dev@localhost:5434/support_chatbot"

    # --- DeepSeek (generation only; embedding + rerank stay local) ---
    deepseek_api_key: str = ""
    deepseek_base_url: str = "https://api.deepseek.com"
    deepseek_model: str = "deepseek-chat"

    # DeepSeek V3 pricing per 1M tokens, used to populate message_traces.cost_usd.
    price_in_per_1m: float = 0.27
    price_out_per_1m: float = 1.10

    # --- local models (CPU) ---
    embedding_model: str = "BAAI/bge-m3"
    embedding_dim: int = 1024
    reranker_model: str = "BAAI/bge-reranker-v2-m3"

    # --- retrieval tuning ---
    # Reranking is ~95% of retrieval latency (~57ms per candidate on CPU), and
    # 10 candidates measured identically to 20 on the golden set. Raise it if
    # recall suffers on real content; the cost is linear.
    retrieval_candidates: int = 10   # pulled from hybrid search, fed to the reranker
    retrieval_top_k: int = 3         # survive reranking, passed to the LLM

    # Calibrated against the golden set: in-scope scores have a median of 0.97
    # and a 10th percentile of 0.32, while off-topic questions peak at 0.0033.
    # 0.10 clears the off-topic ceiling by 30x while still escalating genuinely
    # weak matches. Re-tune on real content — see docs/RUNBOOK.md.
    confidence_threshold: float = 0.10

    # --- app ---
    app_env: str = "local"
    auth_session_secret: str = "change-me"
    session_ttl_hours: int = 12
    widget_allowed_origins: str = "http://localhost:3000,http://localhost:8000"
    default_tenant: str = "mof-contracts"

    # CIDRs whose X-Forwarded-For header may be trusted. Empty means the app is
    # exposed directly and the header is ignored entirely. See core/net.py.
    trusted_proxies: str = ""

    # Account lockout, which per-IP rate limiting cannot provide: it does not
    # stop a distributed brute force against a single account.
    max_failed_logins: int = 5
    lockout_minutes: int = 15

    # Off-topic questions are refused outright. A question scoring below this is
    # treated as out of scope and never becomes an operator task.
    refusal_threshold: float = 0.05

    @property
    def allowed_origins(self) -> list[str]:
        return [o.strip() for o in self.widget_allowed_origins.split(",") if o.strip()]

    @property
    def trusted_proxies_list(self) -> list[str]:
        return [p.strip() for p in self.trusted_proxies.split(",") if p.strip()]


@lru_cache
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
