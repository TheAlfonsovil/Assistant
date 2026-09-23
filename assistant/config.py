from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    ollama_url: str = "http://localhost:11434"
    ollama_model: str = "smtek/Qwen3.8-27B:Q3_K_M"
    database_url: str = "sqlite:///./data/assistant.db"
    log_level: str = "INFO"
    tool_timeout: float = 60.0
    lease_seconds: int = 300
    ollama_timeout: float = 36000.0
    ollama_temperature: float = 0.1
    ollama_num_ctx: int = 32768
    ollama_thinking: bool = True
    ollama_reasoning_effort: str = "low"
    ollama_reasoning_policy: str = "ORCHESTRATOR:high,AGENT:high,PLANNER:medium,NODE_RESOLVER:low,REPLANNER:high,VERIFIER:off,FINAL_RESPONSE:low"
    ollama_keep_alive: str = "30m"
    ollama_context_reserve_tokens: int = 4096
    ollama_failure_threshold: int = 3
    ollama_recovery_timeout: float = 30.0
    ollama_max_prompt_chars: int = 200000
    ollama_max_response_chars: int = 1000000
    task_max_execution_time: float = 86400.0
    final_response_timeout: float = 36000.0
    task_max_steps: int = 1000
    idle_enabled: bool = True
    event_retention_days: int = 30
    event_retention_keep_recent: int = 1000
    maintenance_interval: float = 300.0
    collect_system_facts: bool = True
    persist_system_facts: bool = True
    persist_user_profile: bool = True
    workspace_root: str = "."
    projects_root: str = r"C:\Assistant"
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
