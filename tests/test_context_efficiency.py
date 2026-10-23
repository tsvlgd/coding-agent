"""Tests for context-efficiency work: schema caching, usage streaming,
prompt-cache-stable prefixes, and aggressive tool-output pruning.

These are the regression guards for the four optimizations. Each test asserts
observable behaviour, not implementation detail, so they stay valid if the
internals are refactored again.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from client.llm_client import LLMClient, _is_unknown_param_error
from client.response import TokenUsage
from config.config import Config, ContextConfig, ModelConfig
from context.manager import ContextManager
from tools.base import Tool, ToolInvocation, ToolResult
from tools.registry import ToolRegistry


@pytest.fixture
def config(tmp_path: Path) -> Config:
    return Config(cwd=tmp_path, model=ModelConfig(name="gpt-4", context_window=100_000))


@pytest.fixture
def cm(config: Config) -> ContextManager:
    return ContextManager(config=config, user_memory=None, tools=[])


def _tool_call(call_id: str, args: dict) -> dict:
    return {
        "id": call_id,
        "type": "function",
        "function": {"name": "read_file", "arguments": json.dumps(args)},
    }


# --------------------------------------------------------------------------
# Fix 1: schema caching
# --------------------------------------------------------------------------


class _DummyTool(Tool):
    name = "dummy"
    description = "dummy tool"
    schema = {"type": "object", "properties": {"x": {"type": "string"}}}

    async def execute(self, invocation: ToolInvocation) -> ToolResult:
        return ToolResult.success_result("ok")


def test_get_schemas_returns_identical_object_on_repeat_calls(config: Config):
    """The cache must hand back the SAME object, not a rebuilt copy."""
    registry = ToolRegistry(config)
    registry.register(_DummyTool(config))

    first = registry.get_schemas()
    second = registry.get_schemas()

    assert first is second


def test_schema_cache_invalidated_on_register(config: Config):
    registry = ToolRegistry(config)
    registry.register(_DummyTool(config))
    assert len(registry.get_schemas()) == 1

    class _Second(_DummyTool):
        name = "second"

    registry.register(_Second(config))
    assert len(registry.get_schemas()) == 2


def test_schema_cache_invalidated_on_unregister(config: Config):
    registry = ToolRegistry(config)
    registry.register(_DummyTool(config))
    registry.get_schemas()

    registry.unregister("dummy")
    assert registry.get_schemas() == []


def test_wire_tools_memo_hits_when_source_list_is_identical(config: Config):
    """_build_tools short-circuits on list identity."""
    client = LLMClient(config=config)
    schemas = [{"name": "t", "description": "d", "parameters": {"type": "object"}}]

    first = client._build_tools(schemas)
    second = client._build_tools(schemas)

    assert first is second


def test_wire_tools_memo_misses_on_new_list(config: Config):
    client = LLMClient(config=config)
    client._build_tools([{"name": "a", "description": "", "parameters": {}}])
    result = client._build_tools([{"name": "b", "description": "", "parameters": {}}])

    assert result[0]["function"]["name"] == "b"


# --------------------------------------------------------------------------
# Fix 2: usage streaming
# --------------------------------------------------------------------------


def test_is_unknown_param_error_matches_unsupported_param():
    err = Exception("400 unknown parameter: 'stream_options'")
    assert _is_unknown_param_error(err, "stream_options")


def test_is_unknown_param_error_ignores_unrelated_param():
    err = Exception("400 unknown parameter: 'temperature'")
    assert not _is_unknown_param_error(err, "stream_options")


def test_is_unknown_param_error_ignores_generic_api_error():
    err = Exception("API error: rate limit exceeded")
    assert not _is_unknown_param_error(err, "stream_options")


def test_stream_options_flag_defaults_to_supported(config: Config):
    assert LLMClient(config=config)._stream_options_unsupported is False


# --------------------------------------------------------------------------
# Fix 3: cache-stable prefix
# --------------------------------------------------------------------------


def test_system_message_is_first_and_stable(cm: ContextManager):
    cm.add_user_message("hello")
    cm.add_assistant_message("hi")

    first = cm.get_messages()[0]
    second = cm.get_messages()[0]

    assert first["role"] == "system"
    assert first is second, "system prefix must be the same object every turn"


def test_system_message_content_never_changes(cm: ContextManager):
    before = cm.get_messages()[0]["content"]

    for i in range(10):
        cm.add_user_message(f"q{i}")
        cm.add_assistant_message(f"a{i}")
        cm.add_tool_result("c", "result")

    assert cm.get_messages()[0]["content"] == before


def test_system_prefix_token_count_is_memoized(cm: ContextManager):
    first = cm.static_prefix_tokens()
    assert first > 0
    # Second call must not re-tokenize; assert it returns the cached value.
    assert cm._system_prefix_tokens == first
    assert cm.static_prefix_tokens() == first


def test_effective_usage_falls_back_to_local_when_provider_reports_nothing(cm: ContextManager):
    """A provider that omits usage must not make the context look empty."""
    cm.set_latest_usage(TokenUsage())  # provider returned nothing
    for i in range(20):
        cm.add_tool_result(f"c{i}", "x" * 400)

    assert cm.effective_usage_tokens() > 0


def test_effective_usage_prefers_provider_number_when_larger(cm: ContextManager):
    cm.set_latest_usage(TokenUsage(total_tokens=999_999))
    assert cm.effective_usage_tokens() == 999_999


def test_needs_compression_triggers_on_transcript_cap(config: Config):
    config.context = ContextConfig(max_transcript_tokens=1_000)
    manager = ContextManager(config=config, user_memory=None, tools=[])

    assert manager.needs_compression() is False

    for i in range(50):
        manager.add_tool_result(f"c{i}", "y" * 400)

    assert manager.needs_compression() is True


def test_needs_compression_triggers_on_context_window(config: Config):
    config.model = ModelConfig(name="gpt-4", context_window=10_000)
    config.context = ContextConfig(compaction_threshold=0.80, max_transcript_tokens=10**9)
    manager = ContextManager(config=config, user_memory=None, tools=[])

    manager.set_latest_usage(TokenUsage(total_tokens=8_500))
    assert manager.needs_compression() is True

    manager.set_latest_usage(TokenUsage(total_tokens=1_000))
    assert manager.needs_compression() is False


# --------------------------------------------------------------------------
# Fix 4: aggressive pruning
# --------------------------------------------------------------------------


def test_prune_keeps_recent_tool_output_intact(cm: ContextManager):
    for i in range(4):
        cm.add_user_message(f"q{i}")
        cm.add_assistant_message("a", [_tool_call(f"c{i}", {"p": i})])
        cm.add_tool_result(f"c{i}", "recent-content" * 100)

    cm.prune_tool_outputs()

    newest = [m for m in cm._messages if m.role == "tool"][-1]
    assert newest.pruned_at is None, "newest tool output must survive pruning"
    assert "recent-content" in newest.content


def test_prune_clears_old_tool_outputs_and_reclaims_tokens(cm: ContextManager):
    for i in range(30):
        cm.add_user_message(f"q{i}")
        cm.add_assistant_message("a", [_tool_call(f"c{i}", {"p": i})])
        cm.add_tool_result(f"c{i}", "z" * 2000)

    before = cm.estimate_transcript_tokens()
    pruned = cm.prune_tool_outputs()
    after = cm.estimate_transcript_tokens()

    assert pruned > 0
    assert after < before, "pruning must reduce the local token estimate"


def test_pruned_placeholder_is_self_describing(cm: ContextManager):
    for i in range(30):
        cm.add_user_message(f"q{i}")
        cm.add_assistant_message("a", [_tool_call(f"c{i}", {"p": i})])
        cm.add_tool_result(f"c{i}", "z" * 2000)

    cm.prune_tool_outputs()
    pruned = [m for m in cm._messages if m.role == "tool" and m.pruned_at]

    assert pruned
    for msg in pruned:
        assert msg.content == ContextManager.PRUNED_PLACEHOLDER


def test_prune_is_idempotent(cm: ContextManager):
    for i in range(30):
        cm.add_user_message(f"q{i}")
        cm.add_assistant_message("a", [_tool_call(f"c{i}", {"p": i})])
        cm.add_tool_result(f"c{i}", "z" * 2000)

    first = cm.prune_tool_outputs()
    second = cm.prune_tool_outputs()

    assert first > 0
    assert second == 0, "already-pruned messages must not be re-processed"


def test_prune_respects_message_pairing_with_openai_protocol(cm: ContextManager):
    """Pruning must never orphan a tool result from its assistant tool_call.

    OpenAI rejects a payload where a `tool` message is not preceded by the
    assistant message that requested it.
    """
    for i in range(30):
        cm.add_user_message(f"q{i}")
        cm.add_assistant_message("a", [_tool_call(f"c{i}", {"p": i})])
        cm.add_tool_result(f"c{i}", "z" * 2000)

    cm.prune_tool_outputs()
    messages = cm.get_messages()

    for idx, msg in enumerate(messages):
        if msg.get("role") != "tool":
            continue
        # Walk back over consecutive tool messages to find the assistant.
        j = idx - 1
        while j >= 0 and messages[j].get("role") == "tool":
            j -= 1
        assert j >= 0, "tool message has no preceding assistant"
        assert messages[j].get("role") == "assistant"
        call_ids = {tc["id"] for tc in messages[j].get("tool_calls", [])}
        assert msg["tool_call_id"] in call_ids, "tool result must match its call"


def test_local_token_counter_resets_on_replace_with_summary(cm: ContextManager):
    for i in range(30):
        cm.add_user_message(f"q{i}")
        cm.add_assistant_message("a" * 200, [_tool_call(f"c{i}", {"p": i})])
        cm.add_tool_result(f"c{i}", "z" * 2000)

    assert cm.estimate_transcript_tokens() > 0

    cm.replace_with_summary("a summary of prior work")

    # Counter must reflect only the replacement messages, not the old history.
    assert cm.estimate_transcript_tokens() < 5_000


def test_local_token_counter_resets_on_clear(cm: ContextManager):
    for _ in range(10):
        cm.add_user_message("x" * 500)
    assert cm.estimate_transcript_tokens() > 0

    cm.clear()
    assert cm.estimate_transcript_tokens() == 0


def test_prune_skips_first_turn(cm: ContextManager):
    """With a single user turn there is nothing worth reclaiming."""
    cm.add_user_message("q")
    cm.add_assistant_message("a", [_tool_call("c", {"p": 1})])
    cm.add_tool_result("c", "z" * 50_000)

    assert cm.prune_tool_outputs() == 0


def test_prune_respects_configurable_recent_window(config: Config):
    config.context = ContextConfig(recent_tool_output_tokens=50_000)
    manager = ContextManager(config=config, user_memory=None, tools=[])

    for i in range(20):
        manager.add_user_message(f"q{i}")
        manager.add_assistant_message("a", [_tool_call(f"c{i}", {"p": i})])
        manager.add_tool_result(f"c{i}", "z" * 1000)

    # Window is large enough that nothing should be reclaimed.
    assert manager.prune_tool_outputs() == 0
