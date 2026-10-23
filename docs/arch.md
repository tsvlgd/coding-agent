# Architecture

This document describes the architecture of the AI Coding Agent. For setup and usage, see [README.md](../README.md).

## Overview

A terminal-native AI coding agent built in Python. The core is an async generator-based agent loop that streams responses, calls tools, and manages context automatically. Inspired by Claude Code and OpenCode, but open and hackable.

## High-Level Architecture

```mermaid
flowchart TD
    subgraph CLI["main.py — CLI Entry"]
        RUN[run_single / run_interactive]
        CMD[Slash commands<br/>/help /config /model /refresh ...]
    end

    subgraph AGENT["agent/ — Agent Loop"]
        A[Agent.run<br/>async generator]
        LOOP[_agentic_loop<br/>turn-by-turn execution]
        EVT[AgentEvent<br/>typed event stream]
    end

    subgraph SESSION["agent/session.py — Session"]
        SESS[Session<br/>owns all subsystems]
        CTX[ContextManager<br/>transcript + compaction + pruning]
        TD[LoopDetector<br/>repetition detection]
    end

    subgraph CLIENT["client/ — LLM"]
        LLM[LLMClient<br/>OpenAI-compatible streaming]
        RESP[StreamEvent / ToolCall<br/>response types]
    end

    subgraph TOOLS["tools/ — Tool System"]
        REG[ToolRegistry<br/>schema cache + invoke pipeline]
        BASE[Tool ABC<br/>validate → hook → approve → execute]
        BUILTIN[Built-in tools<br/>read write edit shell grep ...]
        MCP[MCPManager<br/>connect / reconcile / adapt]
        DISC[ToolDiscovery<br/>.agent/tools/*.py]
        SUB[Subagents<br/>investigator, reviewer]
    end

    subgraph SAFETY["safety/ — Approval"]
        AP[ApprovalManager<br/>policies + dangerous detection]
    end

    subgraph HOOKS["hooks/ — Lifecycle"]
        HS[HookSystem<br/>before/after agent & tool]
    end

    subgraph CONFIG["config/ — Configuration"]
        CFG[Config<br/>pydantic model]
        LOAD[load_config<br/>global + local merge]
    end

    subgraph UI["ui/ — Terminal"]
        TUI[TUI<br/>streaming + tool panels]
    end

    RUN --> A
    A --> LOOP
    LOOP --> LLM
    LOOP --> REG
    LOOP --> CTX
    REG --> BASE
    BASE --> BUILTIN
    BASE --> MCP
    BASE --> DISC
    BASE --> SUB
    REG --> HS
    REG --> AP
    A --> EVT
    EVT --> TUI
    CMD --> SESS
    SESS --> CTX
    SESS --> TD
    SESS --> REG
    SESS --> AP
    SESS --> HS
    SESS --> MCP
    LOAD --> CFG
    CFG --> SESS

    style CLI fill:#1a1a2e,stroke:#4a4a6a,color:#e0e0e0
    style AGENT fill:#16213e,stroke:#4a4a6a,color:#e0e0e0
    style SESSION fill:#0f3460,stroke:#4a4a6a,color:#e0e0e0
    style CLIENT fill:#1a1a2e,stroke:#4a4a6a,color:#e0e0e0
    style TOOLS fill:#16213e,stroke:#4a4a6a,color:#e0e0e0
    style SAFETY fill:#1a1a2e,stroke:#4a4a6a,color:#e0e0e0
    style HOOKS fill:#16213e,stroke:#4a4a6a,color:#e0e0e0
    style CONFIG fill:#0f3460,stroke:#4a4a6a,color:#e0e0e0
    style UI fill:#1a1a2e,stroke:#4a4a6a,color:#e0e0e0
```

## Agent Loop

The core loop in `agent/agent.py:_agentic_loop()`:

```mermaid
flowchart TD
    START([User message]) --> ADD[Add to context]
    ADD --> COMPRESS{Needs compression?}
    COMPRESS -->|Yes| SUM[Replace with summary]
    COMPRESS -->|No| SCHEMA
    SUM --> SCHEMA[Build tool schemas]
    SCHEMA --> LLM[Stream from LLM]
    LLM --> HAS_TOOLS{Tool calls?}
    HAS_TOOLS -->|No| DONE([Return response])
    HAS_TOOLS -->|Yes| EXEC[Execute each tool]
    EXEC --> RESULTS[Add tool results to context]
    RESULTS --> LOOP_CHECK{Loop detected?}
    LOOP_CHECK -->|Yes| INJECT[Inject loop-breaker prompt]
    LOOP_CHECK -->|No| PRUNE[Prune old tool outputs]
    INJECT --> PRUNE
    PRUNE --> COMPRESS

    style START fill:#533483,stroke:#4a4a6a,color:#e0e0e0
    style DONE fill:#533483,stroke:#4a4a6a,color:#e0e0e0
```

**Key rules:**
- A turn is atomic. Config changes land between turns, never during.
- The transcript is append-only. Only compaction (wipes everything) and pruning (blanks old content) reduce it.
- Tool results must immediately follow the assistant message that requested them (OpenAI protocol).

## Component Descriptions

### Agent (`agent/agent.py`)
Async generator yielding `AgentEvent` types. `run()` wraps `_agentic_loop()`. Events: `AGENT_START`, `TEXT_DELTA`, `TEXT_COMPLETE`, `TOOL_CALL_START`, `TOOL_CALL_COMPLETE`, `AGENT_ERROR`, `AGENT_END`.

### Session (`agent/session.py`)
Owns all subsystems. `initialize()` connects MCP, discovers tools, builds context manager. `reconcile()` applies config changes without wiping conversation.

### ContextManager (`context/manager.py`)
Stores the transcript as `list[MessageItem]`. Converts to OpenAI-shaped dicts via `get_messages()`. Handles:
- `needs_compression()` — checks usage against context window
- `replace_with_summary()` — compacts transcript (lossy, irreversible)
- `prune_tool_outputs()` — clears old tool results beyond a token budget
- Local token accounting as fallback when provider omits usage

### LLMClient (`client/llm_client.py`)
OpenAI-compatible streaming client. Handles retries, rate limits, connection errors. Sends `stream_options={"include_usage": True}` to get token counts. Memoizes tool wire-format.

### ToolRegistry (`tools/registry.py`)
Registers built-in, MCP, and discovered tools. Memoizes OpenAI schemas. Invoke pipeline: validate → before_tool hook → approval gate → execute → after_tool hook.

### MCPManager (`tools/mcp/mcp_manager.py`)
Connects to MCP servers (stdio/HTTP). Adapts remote tools as `MCPTool` instances. Supports per-server connect/disconnect for `/refresh`.

### ApprovalManager (`safety/approval.py`)
Policy-based approval: `on-request`, `auto`, `never`, `yolo`, `auto-edit`, `on-failure`. Dangerous commands are always rejected regardless of policy.

### HookSystem (`hooks/hook_system.py`)
Runs shell commands/scripts on lifecycle events: before/after agent, before/after tool, on error.

## Context Management

### Token accounting
Two sources: provider-reported (`_latest_usage`) and local estimate (`_local_transcript_tokens`). `effective_usage_tokens()` takes the max — so compaction works even when the provider omits usage.

### Compaction
When tokens exceed 80% of context window: send transcript to LLM for summarization, replace all messages with summary + synthetic ack + continue prompt. **Lossy and irreversible.**

### Pruning
Clears old tool outputs beyond a 6,000-token recent window. Replaces content with `"[tool output pruned to save context]"`. Idempotent (won't re-prune).

## Tool Pipeline

```
tool_call → validate_params (Pydantic)
         → before_tool hook
         → get_confirmation (if mutating)
         → approval_manager.check_approval()
         → execute(invocation)
         → after_tool hook
         → ToolResult
```

Tools normalize into a single `Tool` ABC. Built-ins, subagents, discovered `.py` files, and MCP tools all use the same pipeline.

## Config System

### Layering
1. `~/.config/ai-agent/config.json` (global)
2. `<project>/.agent/config.json` (local, overrides global)
3. Merged with `_merge_dicts()` (deep merge)

### Live reload
`/refresh` command: reload config → reconcile (diff old vs new) → patch only what changed. Turn-atomic: changes apply between turns, never during.

### Environment
- `API_KEY` (required), `BASE_URL` (optional, defaults to OpenAI)
- `.env` file loaded via `python-dotenv`

## Cache Strategy

| Cache | Key | Invalidated by |
|---|---|---|
| Tool schemas (`_schema_cache`) | None (computed once) | `register()`, `unregister()`, `register_mcp_tool()` |
| Wire format (`_tools_cache`) | List identity | New list object |
| System message (`_system_message`) | None (built once) | Never (structural) |
| Prefix tokens (`_system_prefix_tokens`) | None (memoized) | `on_model_changed()` |

## Dependencies

| Package | Purpose |
|---|---|
| `openai` | LLM client (OpenAI-compatible) |
| `pydantic` | Config validation, tool schemas |
| `httpx` | HTTP transport |
| `rich` | Terminal UI |
| `tiktoken` | Token counting |
| `platformdirs` | Cross-platform paths |
| `fastmcp` | MCP client |
| `ddgs` | Web search |
| `click` | CLI framework |

## Feature Checklist

- [x] Interactive and single-run modes
- [x] Streaming text responses
- [x] Multi-turn conversations with tool calling
- [x] Configurable model, temperature, context window
- [x] Any OpenAI-compatible provider
- [x] File operations (read, write, edit, apply_patch)
- [x] Shell execution with safety checks
- [x] Web search and fetch
- [x] Memory (user preferences)
- [x] Todo management
- [x] Subagents (investigator, reviewer)
- [x] Context compaction (LLM summarization)
- [x] Tool output pruning
- [x] Token usage tracking
- [x] Prompt caching (stable prefix)
- [x] Approval policies (6 modes)
- [x] Dangerous command detection
- [x] Path-based safety checks
- [x] Session save/resume
- [x] Checkpoints
- [x] MCP integration (stdio + HTTP)
- [x] Live config reload (/refresh)
- [x] Turn-atomic config application
- [x] Lifecycle hooks
- [x] Loop detection
- [x] Tool discovery (.agent/tools/*.py)
- [ ] File watcher (optional)
- [ ] Diff-based reconcile
- [ ] UI improvements (spinner, timing)