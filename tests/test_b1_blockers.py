from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest

from client.response import StreamEvent, StreamEventType, TextDelta, TokenUsage
from config.config import Config, ModelConfig
from context.compaction import ChatCompactor
from context.manager import ContextManager
from main import CLI
from tools.base import Tool, ToolInvocation, ToolResult
from tools.registry import ToolRegistry


@pytest.fixture
def test_config(tmp_path: Path) -> Config:
    return Config(cwd=tmp_path, model=ModelConfig(name="test-model", context_window=100_000))


@pytest.mark.asyncio
async def test_compactor_handles_none_text_delta_without_attribute_error(test_config: Config):
    """When MESSAGE_COMPLETE has text_delta=None, compress() must not raise AttributeError."""
    mock_client = MagicMock()

    # Simulate an event where text_delta is None
    event = StreamEvent(
        type=StreamEventType.MESSAGE_COMPLETE,
        text_delta=None,
        usage=TokenUsage(prompt_tokens=10, completion_tokens=10, total_tokens=20),
    )

    async def mock_stream(*args, **kwargs):
        yield event

    mock_client.chat_completion = mock_stream
    compactor = ChatCompactor(client=mock_client)

    cm = ContextManager(config=test_config, user_memory=None, tools=[])
    cm.add_user_message("Hello")
    cm.add_assistant_message("Hi", None)
    cm.add_user_message("Need summary")

    summary, usage = await compactor.compress(cm)
    assert summary is None
    assert usage is None


@pytest.mark.asyncio
async def test_compactor_accumulates_summary_successfully(test_config: Config):
    """Compactor should successfully build summary from text_delta and return usage."""
    mock_client = MagicMock()

    delta_event = StreamEvent(
        type=StreamEventType.TEXT_DELTA,
        text_delta=TextDelta(content="Compacted summary content"),
    )
    complete_event = StreamEvent(
        type=StreamEventType.MESSAGE_COMPLETE,
        text_delta=None,
        usage=TokenUsage(prompt_tokens=50, completion_tokens=20, total_tokens=70),
    )

    async def mock_stream(*args, **kwargs):
        yield delta_event
        yield complete_event

    mock_client.chat_completion = mock_stream
    compactor = ChatCompactor(client=mock_client)

    cm = ContextManager(config=test_config, user_memory=None, tools=[])
    cm.add_user_message("Turn 1")
    cm.add_assistant_message("Turn 2", None)
    cm.add_user_message("Turn 3")

    summary, usage = await compactor.compress(cm)
    assert summary == "Compacted summary content"
    assert usage is not None
    assert usage.total_tokens == 70


@pytest.mark.asyncio
async def test_tool_registry_exception_metadata_is_dict(test_config: Config):
    """When a tool raises an unhandled exception, result.metadata must be a dict, not a set."""
    registry = ToolRegistry(test_config)

    class FailingTool(Tool):
        name = "failing_tool"
        description = "A tool that throws an unexpected error"
        schema = {"type": "object"}

        async def execute(self, invocation: ToolInvocation) -> ToolResult:
            raise RuntimeError("Database connection exploded")

    registry.register(FailingTool(test_config))

    from hooks.hook_system import HookSystem

    result = await registry.invoke(
        name="failing_tool",
        params={},
        cwd=test_config.cwd,
        hook_system=HookSystem(test_config),
    )
    assert result.success is False
    assert "Internal error: Database connection exploded" in (result.error or "")

    # Crucial assertion: metadata must be a dict, not a set!
    assert isinstance(result.metadata, dict)
    assert result.metadata.get("tool_name") == "failing_tool"


@pytest.mark.asyncio
async def test_restore_command_does_not_crash_with_unbound_local(test_config: Config):
    """Running /restore with invalid checkpoint ID prints error without
    raising UnboundLocalError."""
    cli = CLI(test_config)
    cli.agent = MagicMock()

    # Calling /restore should handle checkpoint_id cleanly without NameError / UnboundLocalError
    result = await cli._handle_command("/restore test_checkpoint_123")
    assert result is True
