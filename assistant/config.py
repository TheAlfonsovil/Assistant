from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    deepseek_url: str = "https://api.deepseek.com"
    # API model id. This deployment advertises ``deepseek-flash``
    # (DeepSeek V4.1 Flash) and ``deepseek-v4-pro`` on /models.
    deepseek_model: str = "deepseek-flash"
    # Human-facing name for dashboards and reports; never sent to the provider.
    deepseek_model_label: str = "DeepSeek V4.1 Flash"
    deepseek_api_key: str = ""
    database_url: str = "sqlite:///./data/assistant.db"
    log_level: str = "INFO"
    tool_timeout: float = 60.0
    lease_seconds: int = 300
    deepseek_timeout: float = 600.0
    deepseek_temperature: float = 0.1
    deepseek_thinking: bool = True
    deepseek_reasoning_policy: str = "ORCHESTRATOR:high,AGENT:high,PLANNER:high,NODE_RESOLVER:low,REPLANNER:high,VERIFIER:off,FINAL_RESPONSE:low"
    deepseek_max_tokens: int = 16384
    deepseek_failure_threshold: int = 3
    deepseek_recovery_timeout: float = 30.0
    # Seconds a readiness result is reused before /models (or the completion
    # probe) is called again. 0 disables caching.
    deepseek_ready_cache_seconds: float = 60.0
    deepseek_max_prompt_chars: int = 240000
    deepseek_max_response_chars: int = 250000
    # The text models served by this deployment cannot read images. When a
    # vision model is configured, attachments are sent as multimodal parts
    # instead of references only.
    deepseek_supports_vision: bool = False
    # Stream the structured response. Enables time-to-first-token metrics;
    # requires an SSE-capable endpoint.
    deepseek_stream_responses: bool = False
    # Optional cost reporting. JSON map of model id -> USD per 1,000,000 tokens,
    # e.g. {"deepseek-flash": {"input": 0.27, "cached_input": 0.07, "output": 1.10}}.
    # Empty means "cost is not configured" and no cost is reported, instead of
    # inventing a price for a model whose tariff is unknown.
    model_pricing: str = ""
    # Unattended-operation policy. Empty values mean "no restriction".
    # Comma-separated lists.
    tool_denylist: str = ""
    tool_method_denylist: str = ""
    shell_command_allowlist: str = ""
    filesystem_allowed_roots: str = ""
    # Mouse/keyboard control can type into any window: off unless opted in.
    enable_input_control: bool = False
    # Minimum seconds between two calls to the same tool, as "tool=seconds"
    # pairs: "http=1,web=1,shell=2". Empty disables pacing.
    tool_rate_limits: str = ""
    # Images embedded per request when a vision model is configured.
    vision_max_images: int = 1
    # DeepSeek peak hours (UTC, Mon-Fri) are twice the off-peak price.
    # "Ahorro de consumo" pauses paid work inside these windows.
    offpeak_savings_default: bool = False
    # ISO dates (YYYY-MM-DD) treated as Chinese public holidays, comma
    # separated. Fill from the official annual calendar: off-peak rates apply
    # on those days, so listing them stops the assistant from pausing.
    cn_holidays: str = ""
    attachment_max_bytes: int = 5_000_000
    attachment_max_count: int = 4
    task_max_execution_time: float = 7200.0
    # Independent tasks that may advance in parallel. 1 = strictly sequential
    # (original behaviour); commits are serialized in-process either way.
    max_concurrent_tasks: int = 1
    final_response_timeout: float = 900.0
    task_max_steps: int = 200
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

    @staticmethod
    def _split(value: str) -> list[str]:
        return [item.strip() for item in (value or "").split(",") if item.strip()]

    def tool_policy(self):
        """Build the deterministic tool policy for this deployment."""
        from .policy import ToolPolicy

        return ToolPolicy(
            denied_tools=self._split(self.tool_denylist),
            denied_methods=self._split(self.tool_method_denylist),
            shell_allowlist=self._split(self.shell_command_allowlist),
            allowed_roots=self._split(self.filesystem_allowed_roots),
        )

    def rate_limiter(self):
        """Build the per-tool pacing rules (``tool=seconds`` pairs)."""
        from .policy import RateLimiter

        intervals: dict[str, float] = {}
        for item in self._split(self.tool_rate_limits):
            tool, separator, seconds = item.partition("=")
            if separator and tool.strip():
                intervals[tool.strip()] = seconds.strip() or 0
        return RateLimiter(intervals)

    model_config = SettingsConfigDict(
        env_prefix="ASSISTANT_",
        env_file=".env",
        env_ignore_empty=True,
        extra="ignore",
    )


@lru_cache
def get_settings() -> Settings:
    return Settings()
