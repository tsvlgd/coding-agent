"""Local OpenAI-compatible mock to verify stream_options + usage capture.

Proves, without touching a real provider, that:
  1. `stream_options={"include_usage": True}` is actually put on the wire.
  2. The trailing usage chunk from the stream is captured into TokenUsage.
  3. A provider that REJECTS stream_options degrades gracefully instead of
     failing the request (falls back and remembers for the session).

Run: uv run python -m scripts.mock_provider
"""

from __future__ import annotations

import asyncio
import json
import sys
from http.server import BaseHTTPRequestHandler, HTTPServer

STATE = {"mode": "accept", "seen_stream_options": None, "requests": 0}


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):  # silence
        pass

    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        body = json.loads(self.rfile.read(length) or b"{}")
        STATE["requests"] += 1
        STATE["seen_stream_options"] = body.get("stream_options")

        # Model a real legacy provider: it rejects ONLY the unknown
        # stream_options parameter, and happily serves requests without it.
        if STATE["mode"] == "reject" and "stream_options" in body:
            payload = {
                "error": {
                    "message": "Unknown parameter: 'stream_options'.",
                    "type": "invalid_request_error",
                }
            }
            self.send_response(400)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps(payload).encode())
            return

        chunks = [
            {
                "id": "chatcmpl-1",
                "object": "chat.completion.chunk",
                "created": 0,
                "model": "mock-model",
                "choices": [
                    {
                        "index": 0,
                        "delta": {"role": "assistant", "content": "OK"},
                        "finish_reason": None,
                    }
                ],
            },
            {
                "id": "chatcmpl-1",
                "object": "chat.completion.chunk",
                "created": 0,
                "model": "mock-model",
                "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
            },
            {
                "id": "chatcmpl-1",
                "object": "chat.completion.chunk",
                "created": 0,
                "model": "mock-model",
                "choices": [],
                "usage": {
                    "prompt_tokens": 5534,
                    "completion_tokens": 7,
                    "total_tokens": 5541,
                    "prompt_tokens_details": {"cached_tokens": 5120},
                },
            },
        ]

        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.end_headers()
        for chunk in chunks:
            self.wfile.write(f"data: {json.dumps(chunk)}\n\n".encode())
        self.wfile.write(b"data: [DONE]\n\n")
        self.wfile.flush()


async def run_case(mode: str, port: int) -> None:
    from client.llm_client import LLMClient
    from client.response import StreamEventType
    from config.config import Config, ModelConfig

    STATE["mode"] = mode
    server = HTTPServer(("127.0.0.1", port), Handler)
    thread = asyncio.get_event_loop().run_in_executor(None, server.serve_forever)

    config = Config(
        model=ModelConfig(name="mock-model"),
        # config.base_url reads env; override the client directly for the test
    )
    import os

    os.environ["BASE_URL"] = f"http://127.0.0.1:{port}/v1"
    os.environ["API_KEY"] = "test-key"

    client = LLMClient(config=config)
    events = []
    async for ev in client.chat_completion([{"role": "user", "content": "hi"}]):
        events.append(ev)

    server.shutdown()
    await thread

    usage = next((e.usage for e in events if e.type == StreamEventType.MESSAGE_COMPLETE), None)
    errors = [e.error for e in events if e.type == StreamEventType.ERROR]

    print(f"--- mode={mode} ---")
    print(f"  stream_options on wire : {STATE['seen_stream_options']}")
    print(f"  usage captured         : {usage}")
    print(f"  cached_tokens          : {getattr(usage, 'cached_tokens', None)}")
    print(f"  errors                 : {errors}")
    print(f"  unsupported flag       : {client._stream_options_unsupported}")
    return STATE["seen_stream_options"], usage, errors, client


async def main() -> None:
    print("=" * 60)
    seen, usage, errors, _ = await run_case("accept", 8931)
    ok1 = seen == {"include_usage": True}
    ok2 = usage is not None and usage.total_tokens == 5541
    ok3 = usage is not None and usage.cached_tokens == 5120
    ok4 = not errors
    print(f"  ASSERT stream_options sent : {ok1}")
    print(f"  ASSERT usage captured      : {ok2}")
    print(f"  ASSERT cached_tokens read  : {ok3}")
    print(f"  ASSERT no errors           : {ok4}")

    print("=" * 60)
    seen2, usage2, errors2, client2 = await run_case("reject", 8932)
    ok5 = not errors2, "should degrade, not error out"
    ok6 = client2._stream_options_unsupported is True
    print(f"  ASSERT graceful degrade    : {ok5}")
    print(f"  ASSERT remembers & disables: {ok6}")

    print("=" * 60)
    all_ok = all([ok1, ok2, ok3, ok4, bool(ok5[0]), ok6])
    print("RESULT:", "ALL PASS" if all_ok else "FAILURES PRESENT")
    sys.exit(0 if all_ok else 1)


if __name__ == "__main__":
    asyncio.run(main())
