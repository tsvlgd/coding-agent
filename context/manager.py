from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from client.response import TokenUsage
from config.config import Config
from prompts.system import get_system_prompt
from tools.base import Tool
from utils.text import count_tokens


@dataclass
class MessageItem:
    role: str
    content: str
    tool_call_id: str | None = None
    tool_calls: list[dict[str, Any]] = field(default_factory=list)
    token_count: int | None = None
    pruned_at: datetime | None = None

    def to_dict(self) -> dict[str, Any]:
        result: dict[str, Any] = {"role": self.role}

        if self.tool_call_id:
            result["tool_call_id"] = self.tool_call_id

        if self.tool_calls:
            result["tool_calls"] = self.tool_calls

        if self.role == "tool":
            result["content"] = self.content if self.content is not None else ""
        elif self.role == "assistant":
            if self.content:
                result["content"] = self.content
            elif not self.tool_calls:
                result["content"] = ""
        else:
            result["content"] = self.content if self.content is not None else ""

        return result


class ContextManager:
    PRUNED_PLACEHOLDER = "[tool output pruned to save context - re-run the tool if you need it]"

    def __init__(
        self,
        config: Config,
        user_memory: str | None,
        tools: list[Tool] | None,
    ) -> None:
        self._system_prompt = get_system_prompt(config, user_memory, tools)
        self.config = config
        self._model_name = self.config.model_name
        self._messages: list[MessageItem] = []
        self._latest_usage = TokenUsage()  # this one is specific to model
        self.total_usage = (
            TokenUsage()
        )  # specific to the session, accumulates over time (one model or multiple different models)
        self._local_transcript_tokens = 0
        self._pruned_token_savings = 0
        self._system_prefix_tokens: int | None = None
        self._system_message: dict[str, Any] = {
            "role": "system",
            "content": self._system_prompt,
        }

    @property
    def message_count(self) -> int:
        return len(self._messages)

    def _append(self, item: MessageItem) -> None:
        """Append and keep the local token counter in sync.

        token_count for tool messages covers the body only; we add a small
        per-message constant to approximate the role/envelope overhead the
        provider will actually bill for.
        """
        self._messages.append(item)
        self._local_transcript_tokens += (item.token_count or 0) + self._MESSAGE_OVERHEAD

    _MESSAGE_OVERHEAD = 4

    def add_user_message(self, content: str) -> None:
        item = MessageItem(
            role="user",
            content=content,
            token_count=count_tokens(
                content,
                self._model_name,
            ),
        )

        self._append(item)

    def add_assistant_message(
        self,
        content: str,
        tool_calls: list[dict[str, any]] | None = None,
    ) -> None:
        item = MessageItem(
            role="assistant",
            content=content or "",
            token_count=count_tokens(
                content or "",
                self._model_name,
            ),
            tool_calls=tool_calls or [],
        )

        self._append(item)

        if tool_calls:
            args_cost = sum(
                count_tokens(str(tc.get("function", {}).get("arguments", "")), self._model_name)
                + len(str(tc.get("function", {}).get("name", ""))) // 4
                for tc in tool_calls
            )
            self._local_transcript_tokens += args_cost

    def add_tool_result(self, tool_call_id: str, content: str) -> None:
        item = MessageItem(
            role="tool",
            content=content,
            tool_call_id=tool_call_id,
            token_count=count_tokens(content, self._model_name),
        )

        self._append(item)

    def get_messages(self) -> list[dict[str, Any]]:
        """Build the full request payload.

        Ordering is deliberate: the immutable system prefix comes first, then
        the volatile transcript. Prompt caches are prefix-based, so a stable
        leading block means the cached portion survives every turn. Never
        reorder or regenerate the system message per turn.
        """
        messages: list[dict[str, Any]] = []

        if self._system_message["content"]:
            messages.append(self._system_message)

        for item in self._messages:
            messages.append(item.to_dict())

        return messages

    def estimate_transcript_tokens(self) -> int:
        """Local token estimate for the transcript, excluding the system prefix.

        Used as a fallback when the provider reports no usage. Includes
        structural overhead (role/tool_call envelopes) so the estimate tracks
        the real wire cost rather than just message bodies.
        """
        return self._local_transcript_tokens

    def effective_usage_tokens(self) -> int:
        """Best available estimate of how full the context currently is.

        Prefers the provider's authoritative number, falls back to the local
        count. The previous implementation used ONLY the provider number,
        which meant a provider that omits usage made this permanently 0 and
        disabled compaction entirely.
        """
        return max(
            self._latest_usage.total_tokens,
            self._local_transcript_tokens + self.static_prefix_tokens(),
        )

    def static_prefix_tokens(self) -> int:
        """Approximate size of the always-present system prefix."""
        if not self._system_prompt:
            return 0
        if self._system_prefix_tokens is None:
            self._system_prefix_tokens = count_tokens(self._system_prompt, self._model_name)
        return self._system_prefix_tokens

    def needs_compression(self) -> bool:
        """Decide whether the transcript must be compacted before the next call.

        Two independent triggers:
          1. soft  - usage above `compaction_threshold` of the context window
          2. hard  - transcript above `max_transcript_tokens`
        """
        limit = self.config.model.context_window
        used = self.effective_usage_tokens()

        if used > (limit * self.config.context.compaction_threshold):
            return True

        return self._local_transcript_tokens > self.config.context.max_transcript_tokens

    def set_latest_usage(self, usage: TokenUsage):
        self._latest_usage = usage

    def add_usage(self, usage: TokenUsage):
        self.total_usage += usage

    def replace_with_summary(self, summary: str) -> None:
        self._messages = []
        self._local_transcript_tokens = 0
        self._pruned_token_savings = 0

        continuation_content = f"""# Context Restoration (Previous Session Compacted)

        The previous conversation was compacted due to context length limits. Below is a detailed summary of the work done so far.

        **CRITICAL: Actions listed under "COMPLETED ACTIONS" are already done. DO NOT repeat them.**

        ---

        {summary}

        ---

        Resume work from where we left off. Focus ONLY on the remaining tasks."""

        summary_item = MessageItem(
            role="user",
            content=continuation_content,
            token_count=count_tokens(continuation_content, self._model_name),
        )
        self._append(summary_item)

        ack_content = """I've reviewed the context from the previous session. I understand:
- The original goal and what was requested
- Which actions are ALREADY COMPLETED (I will NOT repeat these)
- The current state of the project
- What still needs to be done

I'll continue with the REMAINING tasks only, starting from where we left off."""
        ack_item = MessageItem(
            role="assistant",
            content=ack_content,
            token_count=count_tokens(ack_content, self._model_name),
        )
        self._append(ack_item)

        continue_content = (
            "Continue with the REMAINING work only. Do NOT repeat any completed actions. "
            "Proceed with the next step as described in the context above."
        )

        continue_item = MessageItem(
            role="user",
            content=continue_content,
            token_count=count_tokens(continue_content, self._model_name),
        )
        self._append(continue_item)

    def prune_tool_outputs(self) -> int:
        user_message_count = sum(1 for msg in self._messages if msg.role == "user")

        if user_message_count < 2:
            return 0

        ctx = self.config.context
        keep_recent = ctx.recent_tool_output_tokens
        protected_tokens = 0
        candidates: list[MessageItem] = []

        for msg in reversed(self._messages):
            if msg.role != "tool" or not msg.tool_call_id:
                continue
            if msg.pruned_at:
                continue

            tokens = msg.token_count or count_tokens(msg.content, self._model_name)

            if protected_tokens + tokens <= keep_recent:
                protected_tokens += tokens
                continue

            candidates.append(msg)

        if not candidates:
            return 0

        reclaimable = sum(
            (m.token_count or 0) - count_tokens(self.PRUNED_PLACEHOLDER, self._model_name)
            for m in candidates
        )
        reclaimable = max(0, reclaimable)
        over_budget = self._local_transcript_tokens > self.config.context.max_transcript_tokens
        if reclaimable < ctx.prune_minimum_tokens and not over_budget:
            return 0

        pruned_count = 0
        now = datetime.now()
        placeholder_cost = count_tokens(self.PRUNED_PLACEHOLDER, self._model_name)

        for msg in candidates:
            original = msg.token_count or 0
            msg.content = self.PRUNED_PLACEHOLDER
            msg.token_count = placeholder_cost
            msg.pruned_at = now
            saved = original - placeholder_cost
            if saved > 0:
                self._local_transcript_tokens -= saved
                self._pruned_token_savings += saved
            pruned_count += 1

        return pruned_count

    def clear(self) -> None:
        self._messages = []
        self._local_transcript_tokens = 0
        self._pruned_token_savings = 0
