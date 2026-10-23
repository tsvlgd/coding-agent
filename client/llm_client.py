import asyncio
import logging
from collections.abc import AsyncGenerator
from typing import Any

from openai import APIConnectionError, APIError, AsyncOpenAI, RateLimitError

from client.response import (
    StreamEvent,
    StreamEventType,
    TextDelta,
    TokenUsage,
    ToolCall,
    ToolCallDelta,
    parse_tool_call_arguments,
)
from config.config import Config

logger = logging.getLogger(__name__)


def _extract_usage(usage_obj: Any) -> TokenUsage:
    cached_tokens = 0
    prompt_tokens_details = getattr(usage_obj, "prompt_tokens_details", None)
    if prompt_tokens_details:
        cached_tokens = getattr(prompt_tokens_details, "cached_tokens", 0) or 0
    return TokenUsage(
        prompt_tokens=getattr(usage_obj, "prompt_tokens", 0) or 0,
        completion_tokens=getattr(usage_obj, "completion_tokens", 0) or 0,
        total_tokens=getattr(usage_obj, "total_tokens", 0) or 0,
        cached_tokens=cached_tokens,
    )


def _is_unknown_param_error(error: Exception, param: str) -> bool:
    """Detect a 400 complaining about an unsupported request parameter.

    Used to gracefully degrade when a provider does not implement
    `stream_options`. Deliberately conservative: only matches when the
    parameter name appears alongside unknown/unrecognized/unsupported
    wording, so we never swallow a genuine 400 for a different reason.
    """
    message = str(error).lower()
    if param.lower() not in message:
        return False
    return any(
        marker in message
        for marker in ("unknown", "unrecognized", "unsupported", "not supported", "invalid")
    )


class LLMClient:
    def __init__(self, config: Config) -> None:
        self._client: AsyncOpenAI | None = None
        self._max_retries: int = 3
        self.config = config
        self._tools_cache: tuple[list[dict[str, Any]], list[dict[str, Any]]] | None = None
        self._stream_options_unsupported = False

    def get_client(self) -> AsyncOpenAI:
        if self._client is None:
            raw_base_url = self.config.base_url
            base_url = raw_base_url.strip() if raw_base_url and raw_base_url.strip() else None
            self._client = AsyncOpenAI(
                api_key=self.config.api_key,
                base_url=base_url,
            )
        return self._client

    async def close(self) -> None:
        if self._client:
            await self._client.close()
            self._client = None

    def _build_tools(self, tools: list[dict[str, Any]]):
        """Map registry schemas to the OpenAI wire format.

        Memoized on the identity + length of the incoming list. ToolRegistry
        already returns a stable cached list object for a session, so the
        identity check short-circuits on every turn after the first and we
        skip rebuilding ~2.4k tokens of JSON on the hot path.
        """
        cached = self._tools_cache
        if cached is not None and cached[0] is tools:
            return cached[1]

        wire = [
            {
                "type": "function",
                "function": {
                    "name": tool["name"],
                    "description": tool.get("description", ""),
                    "parameters": tool.get(
                        "parameters",
                        {
                            "type": "object",
                            "properties": {},
                        },
                    ),
                },
            }
            for tool in tools
        ]
        self._tools_cache = (tools, wire)
        return wire

    async def chat_completion(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        stream: bool = True,
    ) -> AsyncGenerator[StreamEvent, None]:
        client = self.get_client()

        kwargs = {
            "model": self.config.model_name,
            "messages": messages,
            "stream": stream,
        }

        if self.config.max_tokens:
            kwargs["max_tokens"] = self.config.max_tokens

        if tools:
            kwargs["tools"] = self._build_tools(tools)
            kwargs["tool_choice"] = "auto"

        if stream and not self._stream_options_unsupported:
            kwargs["stream_options"] = {"include_usage": True}

        for attempt in range(self._max_retries + 1):
            try:
                if stream:
                    async for event in self._stream_response(client, kwargs):
                        yield event
                else:
                    event = await self._non_stream_response(client, kwargs)
                    yield event
                return
            except RateLimitError as e:
                if attempt < self._max_retries:
                    wait_time = 2**attempt
                    await asyncio.sleep(wait_time)
                else:
                    yield StreamEvent(
                        type=StreamEventType.ERROR,
                        error=f"Rate limit exceeded: {e}",
                    )
                    return
            except APIConnectionError as e:
                if attempt < self._max_retries:
                    wait_time = 2**attempt
                    await asyncio.sleep(wait_time)
                else:
                    yield StreamEvent(
                        type=StreamEventType.ERROR,
                        error=f"Connection error: {e}",
                    )
                    return
            except APIError as e:
                if "stream_options" in kwargs and _is_unknown_param_error(e, "stream_options"):
                    self._stream_options_unsupported = True
                    kwargs.pop("stream_options", None)
                    logger.info(
                        "Provider rejected stream_options; continuing without usage streaming"
                    )
                    continue
                yield StreamEvent(
                    type=StreamEventType.ERROR,
                    error=f"API error: {e}",
                )
                return

    async def _stream_response(
        self,
        client: AsyncOpenAI,
        kwargs: dict[str, Any],
    ) -> AsyncGenerator[StreamEvent, None]:
        response = await client.chat.completions.create(**kwargs)

        finish_reason: str | None = None
        usage: TokenUsage | None = None
        tool_calls: dict[int, dict[str, Any]] = {}

        async for chunk in response:
            if hasattr(chunk, "usage") and chunk.usage:
                usage = _extract_usage(chunk.usage)

            if not chunk.choices:
                continue

            choice = chunk.choices[0]
            delta = choice.delta

            if choice.finish_reason:
                finish_reason = choice.finish_reason

            if delta.content:
                yield StreamEvent(
                    type=StreamEventType.TEXT_DELTA,
                    text_delta=TextDelta(delta.content),
                )

            if delta.tool_calls:
                for tool_call_delta in delta.tool_calls:
                    idx = tool_call_delta.index

                    if idx not in tool_calls:
                        tool_calls[idx] = {
                            "id": tool_call_delta.id or "",
                            "name": "",
                            "arguments": "",
                        }

                    if tool_call_delta.id:
                        tool_calls[idx]["id"] = tool_call_delta.id

                    if tool_call_delta.function:
                        if tool_call_delta.function.name:
                            tool_calls[idx]["name"] = tool_call_delta.function.name
                            yield StreamEvent(
                                type=StreamEventType.TOOL_CALL_START,
                                tool_call_delta=ToolCallDelta(
                                    call_id=tool_calls[idx]["id"],
                                    name=tool_call_delta.function.name,
                                ),
                            )

                        if tool_call_delta.function.arguments:
                            tool_calls[idx]["arguments"] += tool_call_delta.function.arguments

                            yield StreamEvent(
                                type=StreamEventType.TOOL_CALL_DELTA,
                                tool_call_delta=ToolCallDelta(
                                    call_id=tool_calls[idx]["id"],
                                    name=tool_calls[idx]["name"],
                                    arguments_delta=tool_call_delta.function.arguments,
                                ),
                            )

        for tc in tool_calls.values():
            yield StreamEvent(
                type=StreamEventType.TOOL_CALL_COMPLETE,
                tool_call=ToolCall(
                    call_id=tc["id"],
                    name=tc["name"],
                    arguments=parse_tool_call_arguments(tc["arguments"]),
                ),
            )

        yield StreamEvent(
            type=StreamEventType.MESSAGE_COMPLETE,
            finish_reason=finish_reason,
            usage=usage,
        )

    async def _non_stream_response(
        self,
        client: AsyncOpenAI,
        kwargs: dict[str, Any],
    ) -> StreamEvent:
        response = await client.chat.completions.create(**kwargs)
        choice = response.choices[0]
        message = choice.message

        text_delta = None
        if message.content:
            text_delta = TextDelta(content=message.content)

        tool_calls: list[ToolCall] = []
        if message.tool_calls:
            for tc in message.tool_calls:
                tool_calls.append(
                    ToolCall(
                        call_id=tc.id,
                        name=tc.function.name,
                        arguments=parse_tool_call_arguments(tc.function.arguments),
                    )
                )

        usage = None
        if response.usage:
            usage = _extract_usage(response.usage)

        return StreamEvent(
            type=StreamEventType.MESSAGE_COMPLETE,
            text_delta=text_delta,
            finish_reason=choice.finish_reason,
            usage=usage,
        )
