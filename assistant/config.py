from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    ollama_url: str = "http://localhost:11434"
    ollama_model: str = "smtek/Qwen3.8-27B:Q3_K_M"
    database_url: str = "sqlite:///./data/assistant.db"
    log_level: str = "INFO"
    tool_timeout: float = 60.0
    lease_seconds: int = 300
    ollama_timeout: float | None = None
    collect_system_facts: bool = True
    persist_system_facts: bool = True
    persist_user_profile: bool = True
    workspace_root: str = "."
    user_name: str | None = None
    user_birth_date: str | None = None
    user_profession: str | None = None
    user_degrees: str = ""
    user_expertise: str = ""

    model_config = SettingsConfigDict(
        env_prefix="ASSISTANT_",
        env_file=".env",
        env_ignore_empty=True,
        extra="ignore",
    )


@lru_cache
def get_settings() -> Settings:
    return Settings()
