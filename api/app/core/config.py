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
    retrieval_candidates: int = 20   # pulled from hybrid search, fed to the reranker
    retrieval_top_k: int = 3         # survive reranking, passed to the LLM
    confidence_threshold: float = 0.45  # below this -> escalate rather than guess

    # --- app ---
    app_env: str = "local"
    auth_session_secret: str = "change-me"
    session_ttl_hours: int = 12
    widget_allowed_origins: str = "http://localhost:3000,http://localhost:8000"
    default_tenant: str = "mof-contracts"

    @property
    def allowed_origins(self) -> list[str]:
        return [o.strip() for o in self.widget_allowed_origins.split(",") if o.strip()]


@lru_cache
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
