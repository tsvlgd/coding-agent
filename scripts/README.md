# scripts/

Standalone developer utilities. **None of this ships with the agent** and none of
it is imported by `main.py` or any package under `agent/`, `client/`, `tools/`,
`context/`, `ui/`, `config/`, `hooks/`, `safety/`, or `prompts/`.

There were originally no files in this directory at all — it was created during
the context-efficiency work below.

---

## `bench_turn.py` — measurement harness

**Problem it solves.** When optimizing context usage you need a number, not a
vibe. This drives the real `ContextManager` and `ToolRegistry` through N
simulated agent turns and prints the per-turn request payload, split into
`system` / `tools` / `history`.

It makes **no network calls**, so it is deterministic and fast. That property is
what let me run it against both the original code and the optimized code (via
`git stash`) to produce a real before/after table instead of guessing.

```bash
uv run python -m scripts.bench_turn
uv run python -m scripts.bench_turn --turns 30 --tools-output-tokens 800
```

Key columns:

- `schema_ms` — cost of building tool schemas each turn. Dropped to `0.00`
  after caching was added (was ~4.4 ms/turn).
- `pruned` — how many tool outputs were reclaimed that turn.
- `system msg object ids` — `1` means the system prefix is byte-stable across
  every turn, which is the precondition for prompt caching to work.

Options:

| Flag | Default | Meaning |
|---|---|---|
| `--turns` | `25` | Number of simulated agent turns |
| `--tools-output-tokens` | `4000` | Approximate size of each tool result |

---

## `mock_provider.py` — offline proof for `stream_options`

**The problem.** The fix for token-usage streaming was to send
`stream_options={"include_usage": True}`. Verifying that needs an API provider
that (a) returns a usage block on a stream and (b) can be made to *reject* the
parameter, to prove the fallback works. Relying on a real provider for (b) is
not practical, and burning real tokens to check plumbing is wasteful.

So this spins up a **local fake OpenAI-compatible server** on `127.0.0.1` that
speaks SSE, and points the client at it. It runs two scenarios:

1. **`accept`** — returns a normal stream plus a trailing usage chunk. Asserts
   the parameter reached the wire and the usage was captured.
2. **`reject`** — returns HTTP 400 for any request containing
   `stream_options`, and serves everything else normally. Asserts the client
   degrades gracefully instead of failing the request, and remembers for the
   session.

```bash
uv run python -m scripts.mock_provider
```

Exits non-zero on failure, prints `RESULT: ALL PASS` on success.

---

## `__init__.py`

Empty marker so these can run as `python -m scripts.<name>`.

---

## Deleting them

Both files are **development-only**. If you would rather not keep them, delete
the directory contents — nothing in the agent imports them. The only cost is
losing the ability to re-run the before/after measurement.
