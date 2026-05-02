"""Multi-LLM consensus: ask all 4 models the same thing, surface (dis)agreement.

Usage:
    MASSIVE_TOKEN=... python3 consensus.py "your question here"
    MASSIVE_TOKEN=... python3 consensus.py --json "your question here"

The point: a single LLM can confidently hallucinate. Asking 4 LLMs and seeing
where they agree is a much better signal — especially for fact-checky tasks
like lead enrichment.
"""
from __future__ import annotations

import argparse
import json
import sys

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


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("question", help="The prompt to send to all models")
    p.add_argument("--json", action="store_true", help="Emit JSON instead of human text")
    p.add_argument("--models", default=",".join(massive.MODELS),
                   help=f"Comma-separated subset of {massive.MODELS}")
    args = p.parse_args()

    models = [m.strip() for m in args.models.split(",") if m.strip()]
    results = massive.ask_all(args.question, models=models)

    if args.json:
        print(json.dumps([r.to_dict() for r in results], indent=2))
    else:
        print(render(results))
    return 0


if __name__ == "__main__":
    sys.exit(main())
