"""Every setting has one default, and the example file agrees with it.

The failure this guards against does not crash anything. It is a knob whose
default is written down twice and disagrees between the copies: the agent
context budget was 120_000 in the context builder and 200_000 in the settings,
and the prompt ceiling existed as 200_000, 240_000 and 400_000 in three
different files. At runtime the deployment always passes the configured value,
so nothing looks broken — the next reader just cannot tell which number is real.

So the rule is: ``config.py`` owns the defaults, constructors import the
constants, and ``.env.example`` either repeats the default or appears in the
documented exceptions below with a reason. Both directions are asserted, so a
new drift and a stale exception both fail.
"""

from __future__ import annotations

import asyncio
import inspect
import re
from pathlib import Path
from typing import Any

from assistant.application.service import TaskService
from assistant.attachments import IMAGE_DETAILS
from assistant.config import Settings
from assistant.context import ContextBuilder
from assistant.llm import DeepSeekLLMProvider, _PromptLLMProvider

EXAMPLE_FILE = Path(__file__).resolve().parents[1] / ".env.example"

# Settings with no line in .env.example, on purpose.
NOT_IN_EXAMPLE = {
    "deepseek_api_key": "a secret belongs in the environment, not in a tracked file",
}

# Settings whose example value deliberately differs from the code default.
DIFFERENT_ON_PURPOSE = {
    "deepseek_supports_vision": (
        "the safe default is False: claiming vision you do not have sends images "
        "that fail, denying it only degrades to asking the user"
    ),
    "workspace_root": "the example documents a deployment path, not a portable default",
    "projects_root": "the example documents a deployment path, not a portable default",
    **{
        field: "personal data is never a code default"
        for field in (
            "user_name",
            "user_birth_date",
            "user_profession",
            "user_degrees",
            "user_expertise",
        )
    },
}


def example_values() -> dict[str, str]:
    """The ``ASSISTANT_`` lines of .env.example, keyed by field name."""
    text = EXAMPLE_FILE.read_text(encoding="utf-8")
    return {
        match.group(1).lower(): match.group(2)
        for match in re.finditer(r"^ASSISTANT_([A-Z0-9_]+)=(.*)$", text, re.M)
    }


def settings_from_example() -> Settings:
    """The settings the example file describes, without reading the real .env."""
    values = example_values()
    known = {
        name: value for name, value in values.items() if name in Settings.model_fields
    }
    return Settings(_env_file=None, **known)


def code_default(name: str) -> Any:
    """The default declared in ``Settings``, independent of the environment."""
    return Settings.model_fields[name].default


def signature_defaults(function: Any) -> dict[str, Any]:
    return {
        name: parameter.default
        for name, parameter in inspect.signature(function).parameters.items()
        if parameter.default is not inspect.Parameter.empty
    }


def test_every_setting_has_a_documented_key():
    documented = set(example_values())
    missing = set(Settings.model_fields) - documented - set(NOT_IN_EXAMPLE)
    assert missing == set()


def test_the_example_file_has_no_invented_keys():
    unknown = set(example_values()) - set(Settings.model_fields)
    assert unknown == set()


def test_the_example_file_repeats_the_code_default_or_says_why():
    example = settings_from_example()
    drift = {
        name: (code_default(name), getattr(example, name))
        for name in example_values()
        if name in Settings.model_fields
        and code_default(name) != getattr(example, name)
    }
    assert set(drift) == set(DIFFERENT_ON_PURPOSE), drift


def test_the_constructors_default_to_the_settings_defaults():
    """A constructor may not hold a second opinion about the same knob."""
    context_parameters = signature_defaults(ContextBuilder.__init__)
    bounded_parameters = signature_defaults(ContextBuilder._bound_agent_context)
    service_parameters = signature_defaults(TaskService.__init__)
    provider_parameters = signature_defaults(DeepSeekLLMProvider.__init__)
    prompt_parameters = signature_defaults(_PromptLLMProvider.__init__)

    assert context_parameters["agent_context_chars"] == code_default("agent_context_chars")
    assert bounded_parameters["limit"] == code_default("agent_context_chars")
    assert service_parameters["agent_context_chars"] == code_default("agent_context_chars")

    assert context_parameters["workspace_root"] == code_default("workspace_root")
    assert service_parameters["workspace_root"] == code_default("workspace_root")
    assert context_parameters["projects_root"] == code_default("projects_root")
    assert service_parameters["projects_root"] == code_default("projects_root")

    assert service_parameters["final_response_timeout"] == code_default(
        "final_response_timeout"
    )
    assert service_parameters["max_steps"] == code_default("task_max_steps")

    for parameters in (provider_parameters, prompt_parameters):
        assert parameters["thinking"] == code_default("deepseek_thinking")
        assert parameters["max_prompt_chars"] == code_default("deepseek_max_prompt_chars")
        assert parameters["max_response_chars"] == code_default("deepseek_max_response_chars")
        assert parameters["max_vision_images"] == code_default("vision_max_images")
        assert parameters["vision_detail"] == code_default("vision_detail")
        assert parameters["max_image_bytes"] == code_default("attachment_max_bytes")

    assert provider_parameters["timeout"] == code_default("deepseek_timeout")
    assert provider_parameters["max_tokens"] == code_default("deepseek_max_tokens")


def test_a_provider_without_an_explicit_timeout_gets_the_configured_one():
    """``None`` means "use the default", not "use some other number"."""
    provider = _PromptLLMProvider("http://127.0.0.1:1", "model", None)
    try:
        assert provider.request_timeout == code_default("deepseek_timeout")
    finally:
        asyncio.run(provider.close())


def test_the_vision_detail_default_is_one_the_api_accepts():
    assert code_default("vision_detail") in IMAGE_DETAILS
