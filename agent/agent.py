from __future__ import annotations

import json
from collections.abc import AsyncGenerator, Callable

from agent.events import AgentEvent, AgentEventType
from agent.session import Session
from client.response import StreamEventType, TokenUsage, ToolCall, ToolResultMessage
from config.config import Config
from prompts.system import create_loop_breaker_prompt
from tools.base import ToolConfirmation


class Agent:
    def __init__(
        self,
        config: Config,
        confirmation_callback: Callable[[ToolConfirmation], bool] | None = None,
    ):
        self.config = config
        self.session: Session | None = Session(self.config)
        self.session.approval_manager.confirmation_callback = confirmation_callback
        self._pending_config: tuple[Config, Config] | None = None
        self._turn_in_progress: bool = False

    def request_config_change(self, new_config: Config) -> None:
        """Queue a config change. Applied at the next turn boundary.

        Rule: a turn is atomic. Config lands between turns, never during.
        """
        self._pending_config = (self.config, new_config)

    async def _apply_pending_config(self) -> None:
        """Apply a queued config change. Called from async context only."""
        if self._pending_config is None or self.session is None:
            if self._pending_config is not None:
                self.config = self._pending_config[1]
                self._pending_config = None
            return
        old_config, new_config = self._pending_config
        await self.session.reconcile(old_config, new_config)
        self.config = new_config
        self._pending_config = None

    async def run(self, message: str) -> AsyncGenerator[AgentEvent, None]:
        await self._apply_pending_config()
        await self.session.hook_system.trigger_before_agent(message)
        yield AgentEvent.agent_start(message)
        self.session.context_manager.add_user_message(message)

        final_response: str | None = None

        async for event in self._agentic_loop():
            yield event

            if event.type == AgentEventType.TEXT_COMPLETE:
                final_response = event.data.get("content")

        self._turn_in_progress = False
        await self.session.hook_system.trigger_after_agent(message, final_response)
        yield AgentEvent.agent_end(final_response)

    async def _agentic_loop(self) -> AsyncGenerator[AgentEvent, None]:
        max_turns = self.config.max_turns

        for _turn_num in range(max_turns):
            await self._apply_pending_config()

            self._turn_in_progress = True
            if self.session:
                self.config = self.session.config
            self.session.increment_turn()
            response_text = ""

            if self.session.context_manager.needs_compression():
                summary, usage = await self.session.chat_compactor.compress(
                    self.session.context_manager
                )

                if summary:
                    self.session.context_manager.replace_with_summary(summary)
                    self.session.context_manager.set_latest_usage(usage)
                    self.session.context_manager.add_usage(usage)

            tool_schemas = self.session.tool_registry.get_schemas()

            tool_calls: list[ToolCall] = []
            usage: TokenUsage | None = None

            async for event in self.session.client.chat_completion(
                self.session.context_manager.get_messages(),
                tools=tool_schemas if tool_schemas else None,
            ):
                if event.type == StreamEventType.TEXT_DELTA:
                    if event.text_delta:
                        content = event.text_delta.content
                        response_text += content
                        yield AgentEvent.text_delta(content)
                elif event.type == StreamEventType.TOOL_CALL_COMPLETE:
                    if event.tool_call:
                        tool_calls.append(event.tool_call)
                elif event.type == StreamEventType.ERROR:
                    yield AgentEvent.agent_error(
                        event.error or "Unknown error occurred.",
                    )
                elif event.type == StreamEventType.MESSAGE_COMPLETE:
                    usage = event.usage

            self.session.context_manager.add_assistant_message(
                response_text or None,
                (
                    [
                        {
                            "id": tc.call_id,
                            "type": "function",
                            "function": {
                                "name": tc.name,
                                "arguments": json.dumps(tc.arguments),
                            },
                        }
                        for tc in tool_calls
                    ]
                    if tool_calls
                    else None
                ),
            )
            if response_text:
                yield AgentEvent.text_complete(response_text)
                self.session.loop_detector.record_action(
                    "response",
                    text=response_text,
                )

            if not tool_calls:
                if usage:
                    self.session.context_manager.set_latest_usage(usage)
                    self.session.context_manager.add_usage(usage)

                self.session.context_manager.prune_tool_outputs()
                self._turn_in_progress = False
                return

            tool_call_results: list[ToolResultMessage] = []

            for tool_call in tool_calls:
                yield AgentEvent.tool_call_start(
                    tool_call.call_id,
                    tool_call.name,
                    tool_call.arguments,
                )

                self.session.loop_detector.record_action(
                    "tool_call",
                    tool_name=tool_call.name,
                    args=tool_call.arguments,
                )

                result = await self.session.tool_registry.invoke(
                    tool_call.name,
                    tool_call.arguments,
                    self.config.cwd,
                    self.session.hook_system,
                    self.session.approval_manager,
                )

                yield AgentEvent.tool_call_complete(
                    tool_call.call_id,
                    tool_call.name,
                    result,
                )

                tool_call_results.append(
                    ToolResultMessage(
                        tool_call_id=tool_call.call_id,
                        content=result.to_model_output(),
                        is_error=not result.success,
                    )
                )

            for tool_result in tool_call_results:
                self.session.context_manager.add_tool_result(
                    tool_result.tool_call_id,
                    tool_result.content,
                )

            loop_detection_error = self.session.loop_detector.check_for_loop()
            if loop_detection_error:
                loop_prompt = create_loop_breaker_prompt(loop_detection_error)
                self.session.context_manager.add_user_message(loop_prompt)

            if usage:
                self.session.context_manager.set_latest_usage(usage)
                self.session.context_manager.add_usage(usage)

            self.session.context_manager.prune_tool_outputs()

        self._turn_in_progress = False
        yield AgentEvent.agent_error(f"Maximum turns ({max_turns}) reached")

    async def __aenter__(self) -> Agent:
        await self.session.initialize()
        return self

    async def __aexit__(
        self,
        exc_type,
        exc_val,
        exc_tb,
    ) -> None:
        self._turn_in_progress = False
        if self.session and self.session.client and self.session.mcp_manager:
            await self.session.client.close()
            await self.session.mcp_manager.shutdown()
            self.session = None
