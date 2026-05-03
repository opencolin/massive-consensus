"""Smoke test: drive compare_mcp.py over stdio and verify ai_chat_compare works."""
from __future__ import annotations

import json
import os
import sys

from mcp_probe import MCPClient, measure_call


def main() -> int:
    if not os.environ.get("MASSIVE_TOKEN"):
        print("Set MASSIVE_TOKEN", file=sys.stderr)
        return 1

    cmd = [sys.executable, "compare_mcp.py"]
    client = MCPClient(cmd)
    try:
        info = client.initialize()
        print("=== INITIALIZE ===")
        print(json.dumps(info, indent=2))

        print("\n=== TOOLS ===")
        tools = client.list_tools()
        for t in tools.get("tools", []):
            print(f"\n• {t['name']}")
            print(f"  desc: {t['description'][:300]}")
            print(f"  schema keys: {list(t['inputSchema']['properties'].keys())}")

        # Test 1: full fanout, all 4 models, on a fact-check question
        print("\n=== T1: full fanout (all 4) ===")
        r = measure_call(
            client, "ai_chat_compare",
            {"prompt": "Who founded Browserbase, the YC company?"},
            "T1: all 4 models",
        )
        print(f"  elapsed: {r['elapsed_s']}s  is_error: {r.get('is_error')}  "
              f"text_bytes: {r['text_bytes']}")
        sc = r.get("structured") or {}
        print(f"  models_returned: {sc.get('models_returned')}")
        print(f"  consensus_domains (top 5):")
        for cd in (sc.get("consensus_domains") or [])[:5]:
            print(f"    {cd['count']}× {cd['domain']}  ({', '.join(cd['models'])})")
        print(f"\n  text head:\n{r['text_excerpt'][:500]}")

        # Test 2: fastest_n=2, drop copilot — should return fast
        print("\n=== T2: fastest_n=2, no copilot ===")
        r = measure_call(
            client, "ai_chat_compare",
            {
                "prompt": "What does the YC company Pentagon (pentagon.run) do?",
                "models": ["chatgpt", "gemini", "perplexity"],
                "fastest_n": 2,
            },
            "T2: fastest_n=2",
        )
        print(f"  elapsed: {r['elapsed_s']}s  is_error: {r.get('is_error')}")
        sc = r.get("structured") or {}
        print(f"  models_returned: {sc.get('models_returned')}")
        for a in sc.get("answers", []):
            err = a.get("error") or "ok"
            print(f"    {a['model']:11s}  {a['elapsed_s']:5.1f}s  sources={len(a['sources'])}  {err}")

        # Test 3: validation error
        print("\n=== T3: validation (empty prompt) ===")
        r = measure_call(client, "ai_chat_compare", {"prompt": ""}, "T3")
        print(f"  is_error: {r.get('is_error')}  exception: {r.get('exception')}")

        # Test 4: validation error (bad model)
        print("\n=== T4: validation (bad model) ===")
        r = measure_call(
            client, "ai_chat_compare",
            {"prompt": "test", "models": ["nonexistent"]},
            "T4",
        )
        print(f"  is_error: {r.get('is_error')}  exception: {r.get('exception')}")

    finally:
        client.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
