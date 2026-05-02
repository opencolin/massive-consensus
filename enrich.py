"""Lead-enrichment via multi-LLM consensus.

For each company name, ask all 4 LLMs (chatgpt/gemini/perplexity/copilot)
the same structured-JSON prompt. Parse the JSON each one returns and
emit a per-company report:

  - per-field consensus value (majority across models)
  - per-field disagreement flag (so you know what to verify by hand)
  - source domains seen across all 4

Usage:
    MASSIVE_TOKEN=... python3 enrich.py "Browserbase" "Modal" "Replicate"
    MASSIVE_TOKEN=... python3 enrich.py --csv-out enriched.csv "Co A" "Co B"
"""
from __future__ import annotations

import argparse
import csv
import json
import re
import sys
from collections import Counter
from typing import Any

import massive

PROMPT = (
    'Information about the company "{company}". '
    'Reply with ONLY one JSON object — no prose before or after, no markdown fences. '
    'Use these exact keys (use null when unknown): '
    '{{"one_line_pitch": string, '
    '"customer_type": "b2b" | "b2c" | "both", '
    '"founded_year": int, '
    '"hq_city": string, '
    '"founders": [string], '
    '"yc_batch": string, '
    '"is_hiring": "yes" | "no" | "unknown"}}'
)

FIELDS = ["one_line_pitch", "customer_type", "founded_year", "hq_city",
          "founders", "yc_batch", "is_hiring"]


def extract_json(text: str) -> dict[str, Any] | None:
    """Pull the first {...} object out of a noisy completion."""
    if not text:
        return None
    text = re.sub(r"```(?:json)?", "", text)  # strip code fences
    # Find the first {...} blob that parses
    depth = 0
    start = -1
    for i, ch in enumerate(text):
        if ch == "{":
            if depth == 0:
                start = i
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0 and start >= 0:
                blob = text[start:i + 1]
                try:
                    return json.loads(blob)
                except json.JSONDecodeError:
                    start = -1
                    continue
    return None


def _norm(value: Any) -> Any:
    if isinstance(value, str):
        return value.strip().lower()
    if isinstance(value, list):
        return tuple(_norm(v) for v in value)
    return value


def consense(per_model: dict[str, dict | None]) -> dict[str, dict]:
    """For each field, pick the majority answer across models that returned it."""
    out: dict[str, dict] = {}
    for field in FIELDS:
        vals = []
        sources = []
        for model, parsed in per_model.items():
            if not parsed or field not in parsed:
                continue
            v = parsed[field]
            if v in (None, "", []):
                continue
            vals.append(v)
            sources.append(model)
        if not vals:
            out[field] = {"value": None, "agreement": "0/0", "models": [], "all": []}
            continue
        counts = Counter(_norm(v) for v in vals)
        top, top_n = counts.most_common(1)[0]
        # Pick a canonical original value matching the normalized top
        canonical = next(v for v in vals if _norm(v) == top)
        agreeing = [m for m, v in zip(sources, vals) if _norm(v) == top]
        out[field] = {
            "value": canonical,
            "agreement": f"{top_n}/{len(vals)}",
            "models": agreeing,
            "all": list(zip(sources, vals)),
        }
    return out


def enrich_one(company: str) -> dict[str, Any]:
    prompt = PROMPT.format(company=company)
    results = massive.ask_all(prompt)
    per_model: dict[str, dict | None] = {}
    raw_completions: dict[str, str] = {}
    timing: dict[str, float] = {}
    errors: dict[str, str] = {}
    all_sources: dict[str, list[str]] = {}
    for r in results:
        timing[r.model] = round(r.elapsed_s, 1)
        if r.error:
            errors[r.model] = r.error
            per_model[r.model] = None
        else:
            per_model[r.model] = extract_json(r.completion)
            raw_completions[r.model] = r.completion[:1500]
            all_sources[r.model] = [s.url for s in r.sources]
    return {
        "company": company,
        "consensus": consense(per_model),
        "per_model_parsed": per_model,
        "per_model_completion_excerpt": raw_completions,
        "elapsed_s": timing,
        "errors": errors,
        "sources": all_sources,
    }


def render_text(report: dict[str, Any]) -> str:
    lines: list[str] = []
    lines.append("=" * 78)
    lines.append(f"COMPANY: {report['company']}")
    lines.append("-" * 78)
    parsed_count = sum(1 for v in report["per_model_parsed"].values() if v)
    lines.append(f"Models that returned valid JSON: {parsed_count}/4")
    if report["errors"]:
        for m, e in report["errors"].items():
            lines.append(f"  ERROR ({m}): {e}")
    for m, t in report["elapsed_s"].items():
        ok = "ok " if report["per_model_parsed"].get(m) else "no-json"
        lines.append(f"  {m:11s} {t:5.1f}s  {ok}")
    lines.append("")
    for field in FIELDS:
        c = report["consensus"][field]
        v = c["value"]
        if v is None:
            lines.append(f"  {field:18s} (no model answered)")
            continue
        flag = "" if c["agreement"].split("/")[0] == c["agreement"].split("/")[1] else "  ⚠ DISAGREE"
        lines.append(f"  {field:18s} {v!s:50.50s}  [{c['agreement']}{flag}]")
        if flag:
            for model, value in c["all"]:
                lines.append(f"      {model:11s} -> {value}")
    lines.append("=" * 78)
    return "\n".join(lines)


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("companies", nargs="+", help="Company names")
    p.add_argument("--json-out", help="Write full reports as JSON to this path")
    p.add_argument("--csv-out", help="Write a one-row-per-company CSV to this path")
    args = p.parse_args()

    reports: list[dict[str, Any]] = []
    for c in args.companies:
        print(f"\n>>> enriching {c} ...", file=sys.stderr)
        report = enrich_one(c)
        reports.append(report)
        print(render_text(report))

    if args.json_out:
        with open(args.json_out, "w") as f:
            json.dump(reports, f, indent=2, default=str)
        print(f"\nwrote {args.json_out}", file=sys.stderr)

    if args.csv_out:
        with open(args.csv_out, "w", newline="") as f:
            w = csv.writer(f)
            w.writerow(["company"] + FIELDS + [f"{x}_agreement" for x in FIELDS])
            for r in reports:
                row = [r["company"]]
                for fld in FIELDS:
                    row.append(r["consensus"][fld]["value"])
                for fld in FIELDS:
                    row.append(r["consensus"][fld]["agreement"])
                w.writerow(row)
        print(f"wrote {args.csv_out}", file=sys.stderr)

    return 0


if __name__ == "__main__":
    sys.exit(main())
