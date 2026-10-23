"""Measurement harness for the context-efficiency work.

Drives the real ContextManager + ToolRegistry without hitting the network, so
runs are deterministic and comparable. Prints the per-turn wire payload split
into system / tools / history, plus cumulative cacheable-prefix size.

Usage:
    uv run python -m scripts.bench_turn [--turns N] [--tools-output-tokens N]
"""

from __future__ import annotations

import argparse
import json
import time
from pathlib import Path

from config.loader import load_config
from context.manager import ContextManager
from tools.registry import create_default_registry
from utils.text import count_tokens

SAMPLE_LINE = (
    "def handler(event, context):\n"
    '    """Entry point."""\n'
    "    logger.info('handling %s', event)\n"
    "    return process(event, context, retries=3)\n"
)


def _make_blob(target_tokens: int, model: str) -> str:
    """Build a tool output of roughly `target_tokens` tokens."""
    per_block = count_tokens(SAMPLE_LINE * 4, model)
    blocks = max(1, round(target_tokens / max(1, per_block)))
    blob = SAMPLE_LINE * 4 * blocks
    return blob


def build(n_turns: int, tool_output_tokens: int) -> tuple[list[dict], ContextManager, object]:
    config = load_config(cwd=Path.cwd())
    registry = create_default_registry(config)
    cm = ContextManager(
        config=config,
        user_memory=None,
        tools=registry.get_tools(),
    )
    model = config.model_name

    # Calibrate a tool output of roughly `tool_output_tokens`.
    blob = _make_blob(tool_output_tokens, model)

    rows: list[dict] = []
    for turn in range(1, n_turns + 1):
        # Pruning runs at the END of a turn in agent.py, so run it before measuring.
        pruned = cm.prune_tool_outputs()

        schemas = registry.get_schemas()
        wire_tools = [
            {
                "type": "function",
                "function": {
                    "name": t["name"],
                    "description": t.get("description", ""),
                    "parameters": t.get("parameters", {}),
                },
            }
            for t in schemas
        ]

        t0 = time.perf_counter()
        schemas = registry.get_schemas()
        build_ms = (time.perf_counter() - t0) * 1000

        msgs = cm.get_messages()
        system = msgs[0]["content"] if msgs and msgs[0]["role"] == "system" else ""
        history = [m for m in msgs if m.get("role") != "system"]

        rows.append(
            {
                "turn": turn,
                "system": count_tokens(system, model),
                "tools": count_tokens(json.dumps(wire_tools), model),
                "history": count_tokens(json.dumps(history), model),
                "schema_build_ms": build_ms,
                "pruned_msgs": pruned,
                # Only available on the optimized ContextManager; lets the
                # same harness measure the before/after of a refactor.
                "local_est": getattr(cm, "estimate_transcript_tokens", lambda: -1)(),
                "effective": getattr(cm, "effective_usage_tokens", lambda: -1)(),
                "needs_compress": getattr(cm, "needs_compression", lambda: None)(),
                "system_id": id(msgs[0]) if msgs and msgs[0]["role"] == "system" else 0,
            }
        )

        # Simulate one full agent turn: user asks, assistant calls a tool,
        # tool returns a big blob, assistant answers.
        cm.add_user_message(f"Turn {turn}: please inspect module_{turn} and tell me what it does.")
        cm.add_assistant_message(
            f"Looking at module_{turn}.",
            [
                {
                    "id": f"call_{turn}",
                    "type": "function",
                    "function": {
                        "name": "read_file",
                        "arguments": json.dumps({"path": f"module_{turn}.py"}),
                    },
                }
            ],
        )
        cm.add_tool_result(f"call_{turn}", blob)
        cm.add_assistant_message(f"module_{turn} defines a handler entry point.")

    return rows, cm, registry


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--turns", type=int, default=25)
    ap.add_argument("--tools-output-tokens", type=int, default=4000)
    args = ap.parse_args()

    rows, cm, registry = build(args.turns, args.tools_output_tokens)

    print(
        f"{'turn':>4} {'system':>8} {'tools':>7} {'history':>9} "
        f"{'total':>8} {'schema_ms':>10} {'pruned':>7}"
    )
    print("-" * 62)
    for r in rows:
        total = r["system"] + r["tools"] + r["history"]
        print(
            f"{r['turn']:>4} {r['system']:>8} {r['tools']:>7} {r['history']:>9} "
            f"{total:>8} {r['schema_build_ms']:>9.2f} {r['pruned_msgs']:>7}"
        )

    last = rows[-1]
    total_last = last["system"] + last["tools"] + last["history"]
    total_first = rows[0]["system"] + rows[0]["tools"] + rows[0]["history"]
    avg_schema_ms = sum(r["schema_build_ms"] for r in rows) / len(rows)
    total_pruned = sum(r["pruned_msgs"] for r in rows)
    max_hist = max(r["history"] for r in rows)
    system_ids = {r["system_id"] for r in rows}

    print("-" * 62)
    print(f"first-turn wire total : {total_first}")
    print(f"final-turn wire total : {total_last}")
    print(f"growth over {len(rows)} turns : +{total_last - total_first} tokens")
    print(f"peak history tokens   : {max_hist}")
    print(f"avg schema build cost : {avg_schema_ms:.2f} ms/turn")
    print(f"total msgs pruned     : {total_pruned}")
    print(f"final message count   : {cm.message_count}")
    # Tolerate running against the pre-optimization ContextManager.
    local_est = getattr(cm, "estimate_transcript_tokens", lambda: -1)()
    effective = getattr(cm, "effective_usage_tokens", lambda: -1)()
    needs_compress = getattr(cm, "needs_compression", lambda: None)()

    print(f"local transcript est  : {local_est} tokens")
    print(f"effective usage       : {effective} tokens")
    print(f"needs_compression()   : {needs_compress}")
    print(
        f"cacheable prefix      : system={last['system']} + tools={last['tools']} "
        f"= {last['system'] + last['tools']} (stable)"
    )
    distinct = len(system_ids)
    print(f"system msg object ids : {distinct} across all turns (1 = prefix stable)")
    print(f"tools registered      : {len(registry.get_tools())}")


if __name__ == "__main__":
    main()
