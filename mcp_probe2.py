"""Second-wave MCP probe: edge cases, copilot timeout, structured prompts, working URLs."""
from __future__ import annotations

import json
import os
import sys

from mcp_probe import MCPClient, SERVER_CMD, measure_call


def main() -> int:
    token = os.environ.get("MASSIVE_TOKEN")
    if not token:
        print("Set MASSIVE_TOKEN", file=sys.stderr)
        return 1

    client = MCPClient(SERVER_CMD, env={"MASSIVE_TOKEN": token})
    try:
        client.initialize()
        results = []

        # T6: copilot — known to be slow/unreliable in our raw API tests
        results.append(measure_call(
            client, "ai_chat_completion",
            {"prompt": "In one sentence, what does the YC company Browserbase do?", "model": "copilot"},
            "T6: copilot (latency-prone)",
        ))

        # T7: gemini with structured-JSON-only prompt — does the MCP help?
        results.append(measure_call(
            client, "ai_chat_completion",
            {
                "prompt": (
                    'Information about company "Tsenta" (YC S26). '
                    'Reply with ONLY one JSON object: '
                    '{"one_line_pitch": string, "founders": [string], "hq_city": string}'
                ),
                "model": "gemini",
            },
            "T7: structured JSON prompt (gemini)",
        ))

        # T8: web_fetch on a known-working URL
        results.append(measure_call(
            client, "web_fetch",
            {"url": "https://news.ycombinator.com", "format": "markdown"},
            "T8: web_fetch real URL (markdown)",
        ))

        # T9: web_fetch raw — does the MCP truncate or pass through 1+ MB?
        results.append(measure_call(
            client, "web_fetch",
            {"url": "https://news.ycombinator.com", "format": "raw"},
            "T9: web_fetch raw HTML (size check)",
        ))

        # T10: web_search edge — query that triggers AI overview
        results.append(measure_call(
            client, "web_search",
            {"query": "best espresso machines 2026", "max_results": 5},
            "T10: web_search w/ AI overview",
        ))

        # T11: error path — invalid URL
        results.append(measure_call(
            client, "web_fetch",
            {"url": "ftp://nope.example"},
            "T11: invalid URL (validation error)",
        ))

        # T12: model=perplexity asking for sources count — same query twice for stability
        results.append(measure_call(
            client, "ai_chat_completion",
            {"prompt": "Who founded Browserbase, the Y Combinator company?", "model": "perplexity"},
            "T12: perplexity small fact-question",
        ))

        for r in results:
            print(f"\n--- {r['label']} ---")
            print(f"  args:        {json.dumps(r.get('args'))[:120]}")
            print(f"  elapsed:     {r.get('elapsed_s')}s")
            if "exception" in r:
                print(f"  EXCEPTION:   {r['exception']}")
                continue
            print(f"  is_error:    {r.get('is_error')}")
            print(f"  text_bytes:  {r.get('text_bytes')} ({r['text_bytes']/1024:.1f} KB)")
            sc = r.get("structured")
            if isinstance(sc, dict):
                print(f"  structured:  keys={list(sc.keys())}")
                # surface source count for ai_chat
                if "sources" in sc and isinstance(sc["sources"], list):
                    print(f"  sources:     {len(sc['sources'])}")
            print(f"  text head:   {r['text_excerpt'][:300]!r}")

        with open("/tmp/mcp-test2-results.json", "w") as f:
            json.dump(results, f, indent=2)
    finally:
        client.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
