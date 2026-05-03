"""Multi-LLM consensus: ask all 4 models the same thing, surface (dis)agreement.

Usage:
    MASSIVE_TOKEN=... python3 consensus.py "your question here"
    MASSIVE_TOKEN=... python3 consensus.py --json "your question here"
    MASSIVE_TOKEN=... python3 consensus.py --via-mcp /path/to/dist/index.js "..."

The point: a single LLM can confidently hallucinate. Asking 4 LLMs and seeing
where they agree is a much better signal — especially for fact-checky tasks
like lead enrichment.

Two backends are supported:
    * default — direct HTTP calls to render.joinmassive.com (faster, fewer moving parts)
    * --via-mcp — fan out via Massive's MCP server over stdio JSON-RPC.
      Validates that the official MCP surface is sufficient for consensus workflows.
"""
from __future__ import annotations

import argparse
import concurrent.futures
import json
import sys
import time

import massive


def render(results: list[massive.AIResponse]) -> str:
    out: list[str] = []
    out.append("=" * 78)
    out.append(f"QUERY: {results[0].query}")
    out.append("=" * 78)
    for r in results:
        out.append(f"\n[{r.model.upper()}]  {r.elapsed_s:.1f}s  {r.raw_bytes // 1024} KB")
        if r.error:
            out.append(f"  ERROR: {r.error}")
            continue
        out.append("  " + (r.completion[:600] or "<empty>").replace("\n", "\n  "))
        if r.sources:
            out.append(f"  Sources ({len(r.sources)}):")
            for s in r.sources[:5]:
                out.append(f"    - {s.url}")
    out.append("\n" + "-" * 78)
    out.append("Source-domain overlap (rough consensus signal):")
    domains: dict[str, list[str]] = {}
    for r in results:
        for s in r.sources:
            d = s.url.split("/")[2] if "://" in s.url else s.url
            domains.setdefault(d, []).append(r.model)
    shared = sorted(
        ((d, ms) for d, ms in domains.items() if len(set(ms)) >= 2),
        key=lambda x: -len(set(x[1])),
    )
    if not shared:
        out.append("  (no domain cited by 2+ models)")
    for d, ms in shared[:10]:
        out.append(f"  {len(set(ms))}/4  {d}  ({','.join(sorted(set(ms)))})")
    out.append("-" * 78)
    return "\n".join(out)


def ask_all_via_mcp(prompt: str, models: list[str], server_path: str) -> list[massive.AIResponse]:
    """Spawn one MCP server connection per model and call ai_chat_completion in parallel.

    Each model gets its own server process — simplest way to fan out without
    serializing on a single stdio pipe. Validates that the official MCP surface
    is sufficient for the consensus workflow.
    """
    from mcp_probe import MCPClient  # local helper

    def call_one(model: str) -> massive.AIResponse:
        t0 = time.time()
        client = MCPClient(["node", server_path])
        try:
            client.initialize()
            res = client.call_tool("ai_chat_completion", {"prompt": prompt, "model": model})
            elapsed = time.time() - t0
            sc = res.get("structuredContent") or {}
            text = ""
            for c in res.get("content", []):
                if c.get("type") == "text":
                    text += c.get("text", "")
            sources = [
                massive.Source(url=s.get("url", ""), title=s.get("title", ""))
                for s in sc.get("sources", [])
            ]
            return massive.AIResponse(
                model=sc.get("model", model),
                query=prompt,
                completion=sc.get("completion", text),
                sources=sources,
                elapsed_s=elapsed,
                raw_bytes=len(text),
                error=("MCP error" if res.get("isError") else None),
            )
        except Exception as e:
            return massive.AIResponse(
                model=model, query=prompt, completion="",
                elapsed_s=time.time() - t0,
                error=f"{type(e).__name__}: {e}",
            )
        finally:
            client.close()

    with concurrent.futures.ThreadPoolExecutor(max_workers=len(models)) as ex:
        futs = {ex.submit(call_one, m): m for m in models}
        out = [f.result() for f in concurrent.futures.as_completed(futs)]
    by_model = {r.model: r for r in out}
    return [by_model[m] for m in models if m in by_model]


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("question", help="The prompt to send to all models")
    p.add_argument("--json", action="store_true", help="Emit JSON instead of human text")
    p.add_argument("--models", default=",".join(massive.MODELS),
                   help=f"Comma-separated subset of {massive.MODELS}")
    p.add_argument("--via-mcp", metavar="DIST_INDEX_JS",
                   help="Fan out via Massive's MCP server over stdio "
                        "(absolute path to dist/index.js). Validates the MCP surface "
                        "is sufficient for consensus.")
    args = p.parse_args()

    models = [m.strip() for m in args.models.split(",") if m.strip()]

    backend = "MCP/stdio" if args.via_mcp else "HTTP/direct"
    print(f"# backend: {backend}", file=sys.stderr)
    if args.via_mcp:
        results = ask_all_via_mcp(args.question, models, args.via_mcp)
    else:
        results = massive.ask_all(args.question, models=models)

    if args.json:
        print(json.dumps([r.to_dict() for r in results], indent=2))
    else:
        print(render(results))
    return 0


if __name__ == "__main__":
    sys.exit(main())
