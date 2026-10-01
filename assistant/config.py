from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict

# Canonical defaults for the settings that are *also* constructor defaults
# somewhere else in the package.
#
# They are written down once, here, because every knob that was written down
# twice had already drifted: the agent context budget was 120_000 in the
# context builder and 200_000 here, and the prompt ceiling existed as 200_000,
# 240_000 and 400_000 in three different files. A constructor imports the
# constant instead of repeating the literal, so "the default" means one thing
# and `tests/test_defaults.py` fails if a literal creeps back in.
DEFAULT_LLM_TIMEOUT = 600.0
DEFAULT_THINKING = True
DEFAULT_MAX_PROMPT_CHARS = 400_000
DEFAULT_MAX_RESPONSE_CHARS = 250_000
DEFAULT_VISION_MAX_IMAGES = 2
DEFAULT_VISION_DETAIL = "original"
DEFAULT_AGENT_CONTEXT_CHARS = 200_000
DEFAULT_FINAL_RESPONSE_TIMEOUT = 900.0
DEFAULT_MAX_STEPS = 200
DEFAULT_PROJECTS_ROOT = "."


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
    deepseek_timeout: float = DEFAULT_LLM_TIMEOUT
    deepseek_temperature: float = 0.1
    deepseek_thinking: bool = DEFAULT_THINKING
    deepseek_reasoning_policy: str = "ORCHESTRATOR:high,AGENT:high,PLANNER:high,NODE_RESOLVER:low,REPLANNER:high,VERIFIER:off,FINAL_RESPONSE:low"
    deepseek_max_tokens: int = 16384
    deepseek_failure_threshold: int = 3
    deepseek_recovery_timeout: float = 30.0
    # Seconds a readiness result is reused before /models (or the completion
    # probe) is called again. 0 disables caching.
    deepseek_ready_cache_seconds: float = 60.0
    # Hard ceiling for one rendered prompt. The model window is the real limit;
    # this is the seatbelt that stops a runaway context from being sent at any
    # price. ~400000 characters is roughly 100k tokens.
    deepseek_max_prompt_chars: int = DEFAULT_MAX_PROMPT_CHARS
    deepseek_max_response_chars: int = DEFAULT_MAX_RESPONSE_CHARS
    # Whether the configured model accepts images. It is a property of the
    # model, not of the client: deepseek-flash reads images, deepseek-v4-pro
    # does not. When false, attachments and captures are references the model is
    # told not to describe; when true they are sent as multimodal parts.
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
    # Images embedded per request when a vision model is configured. One is a
    # fresh capture and the other a user attachment, so two is the useful
    # ceiling: more pictures per turn buy nothing and cost tokens every turn.
    vision_max_images: int = DEFAULT_VISION_MAX_IMAGES
    # Optional ``detail`` for an image part: "low" downscales before inference
    # (faster, cheaper), "original"/"high" keep it, "auto" lets the provider
    # choose. Empty keeps the provider default. Reading text in a screenshot
    # needs the full image, so ``original`` is the default: ``low`` only makes
    # sense when the picture is there to be recognised, not to be read.
    vision_detail: str = DEFAULT_VISION_DETAIL
    # DeepSeek peak hours (UTC, Mon-Fri) are twice the off-peak price.
    # "Ahorro de consumo" pauses paid work inside these windows.
    offpeak_savings_default: bool = False
    # ISO dates (YYYY-MM-DD) treated as Chinese public holidays, comma
    # separated. Fill from the official annual calendar: off-peak rates apply
    # on those days, so listing them stops the assistant from pausing.
    cn_holidays: str = ""
    # Timezone used by daily schedules when the caller does not name one. Only
    # UTC and fixed offsets work without the 'tzdata' package installed.
    schedule_timezone: str = "UTC"
    attachment_max_bytes: int = 5_000_000
    attachment_max_count: int = 4
    task_max_execution_time: float = 7200.0
    # Prompt-side evidence budget for one agent/orchestrator turn. The tool
    # catalog is never replaced or silently dropped; only volatile payloads are
    # trimmed, in a fixed order, so the cacheable prefix stays put. Raise it
    # when the model window allows (deepseek_max_prompt_chars is the hard cap).
    agent_context_chars: int = DEFAULT_AGENT_CONTEXT_CHARS
    # Per-task budgets. Every one of these is enforced by the ledger
    # (_consume_budget) and ends the task with BUDGET_EXHAUSTED when it runs
    # out, so they are configuration, not constants: a ceiling that is too low
    # stops useful work in the middle. 0 is not allowed; use a large number to
    # mean "effectively unlimited".
    task_max_llm_calls: int = 60
    task_max_tool_calls: int = 150
    task_max_codegraph_queries: int = 200
    task_max_project_reads: int = 200
    task_max_source_bytes: int = 60_000_000
    task_max_plan_nodes: int = 200
    task_max_retries: int = 3
    task_max_recovery_attempts: int = 3
    # Structural index limits. Wider caps cost one slower build and a larger
    # persisted JSON, but make codegraph answers usable for bigger projects.
    codegraph_max_files: int = 2000
    codegraph_max_symbols: int = 6000
    codegraph_max_edges: int = 20_000
    # Seconds a codegraph stays trusted when the project tree fingerprint is
    # unchanged. 0 = re-analyse before every LLM phase (previous behaviour).
    codegraph_refresh_seconds: int = 300
    # Semantic queries (project.types) run on pyright's language server. Empty
    # means autodetect: ASSISTANT_PYRIGHT_LANGSERVER, then the npx cache.
    pyright_langserver: str = ""
    semantic_timeout_seconds: float = 60.0
    # Independent tasks that may advance in parallel. 1 = strictly sequential
    # (original behaviour); commits are serialized in-process either way.
    max_concurrent_tasks: int = 1
    final_response_timeout: float = DEFAULT_FINAL_RESPONSE_TIMEOUT
    task_max_steps: int = DEFAULT_MAX_STEPS
    idle_enabled: bool = True
    event_retention_days: int = 30
    event_retention_keep_recent: int = 1000
    maintenance_interval: float = 300.0
    collect_system_facts: bool = True
    persist_system_facts: bool = True
    persist_user_profile: bool = True
    workspace_root: str = "."
    # Parent directory new projects are created in. "." keeps the code default
    # portable and obviously unset; .env.example documents the real path.
    projects_root: str = DEFAULT_PROJECTS_ROOT
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
