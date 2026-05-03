"""Verify the patched Massive MCP fixes the issues we patched for."""
from __future__ import annotations

import json
import os
import sys

from mcp_probe import MCPClient, measure_call


def main() -> int:
    if not os.environ.get("MASSIVE_TOKEN"):
        return 1

    cmd = ["node", "/tmp/mcp-patched/dist/index.js"]
    client = MCPClient(cmd)
    try:
        client.initialize()

        print("=== TOOL DESCRIPTION (patched) ===")
        tools = client.list_tools()
        for t in tools.get("tools", []):
            if t["name"] == "ai_chat_completion":
                print(t["description"])

        # P1: prefix should be stripped now
        print("\n=== PATCH 1: prefix strip on ai_chat_completion ===")
        for model in ["chatgpt", "gemini", "copilot"]:
            r = measure_call(
                client, "ai_chat_completion",
                {"prompt": "In one short sentence, what year was the Magna Carta signed?",
                 "model": model},
                f"prefix-strip ({model})",
            )
            head = r.get("text_excerpt", "")[:200]
            has_prefix = bool(head.lstrip().lower().startswith((
                "chatgpt said", "gemini said", "copilot said", "perplexity said"
            )))
            print(f"  {model:11s} {r['elapsed_s']:5.1f}s  prefix_present={has_prefix}")
            print(f"    head: {head[:140]!r}")

        # P2: perplexity citation token cleanup
        print("\n=== PATCH 2: perplexity citation token strip ===")
        r = measure_call(
            client, "ai_chat_completion",
            {"prompt": "Who founded Browserbase, the YC company?", "model": "perplexity"},
            "citation-strip (perplexity)",
        )
        head = r.get("text_excerpt", "")
        # Look for things like "research.contrary+2" — the pattern we added
        import re
        leftover = re.findall(r"[a-z][a-z0-9.-]*\.[a-z]{2,}\+\d+", head)
        print(f"  elapsed: {r['elapsed_s']}s")
        print(f"  leftover citation tokens: {leftover}")
        print(f"  head: {head[:300]!r}")

        # P3: web_fetch heuristic on a 404 page
        print("\n=== PATCH 3: web_fetch error-page heuristic ===")
        r = measure_call(
            client, "web_fetch",
            {"url": "https://www.ycombinator.com/companies/browserbase", "format": "markdown"},
            "404 detection",
        )
        sc = r.get("structured") or {}
        print(f"  elapsed: {r['elapsed_s']}s  is_error: {r.get('is_error')}")
        print(f"  structured keys: {list(sc.keys())}")
        print(f"  looks_like_error_page: {sc.get('looks_like_error_page')}")
        print(f"  error_page_match: {sc.get('error_page_match')}")

        # P4: control — fetch a real working page should NOT trigger heuristic
        print("\n=== PATCH 3 (control): real page should not trigger heuristic ===")
        r = measure_call(
            client, "web_fetch",
            {"url": "https://news.ycombinator.com", "format": "markdown"},
            "real page control",
        )
        sc = r.get("structured") or {}
        print(f"  looks_like_error_page: {sc.get('looks_like_error_page', False)}")

    finally:
        client.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
