"""Standalone MCP server exposing `ai_chat_compare` — the missing 5th tool.

Implements the killer-feature ask from FEEDBACK.md: fan out one prompt across
ChatGPT / Gemini / Perplexity / Copilot in parallel, return aligned answers
+ per-domain agreement signal. Unioned sources, optional `fastest_n` to
short-circuit on the slow models.

Pure stdlib MCP stdio server. No external dependencies.

Run via Claude Desktop / Claude Code config:

    {
      "mcpServers": {
        "massive-compare": {
          "command": "python3",
          "args": ["/absolute/path/to/compare_mcp.py"],
          "env": { "MASSIVE_TOKEN": "your-token" }
        }
      }
    }
"""
from __future__ import annotations

import json
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any

import massive  # local module, ./massive.py

PROTOCOL_VERSION = "2024-11-05"
SERVER_NAME = "massive-compare"
SERVER_VERSION = "0.1.0"

TOOL_NAME = "ai_chat_compare"
TOOL_DESCRIPTION = (
    "Ask the same question across multiple chatbots (ChatGPT, Gemini, Perplexity, Copilot) "
    "in parallel and return aligned answers with cross-model agreement signal. "
    "Best for fact-checking, lead enrichment, and any task where a single LLM might "
    "confidently hallucinate. Returns each model's plain-text answer + structured sources, "
    "plus a `consensus_domains` array showing which source domains were cited by 2+ models. "
    "Use `fastest_n` to short-circuit on slow models (Copilot can take 60s+; the others "
    "usually return in 10-30s)."
)


def _stderr(msg: str) -> None:
    print(f"[{SERVER_NAME}] {msg}", file=sys.stderr, flush=True)


# ---------- core: run the comparison ----------

def run_compare(prompt: str, models: list[str], fastest_n: int | None) -> dict[str, Any]:
    """Fan out and gather. Honor fastest_n by returning early."""
    n_models = len(models)
    target_n = fastest_n if fastest_n and 0 < fastest_n < n_models else n_models

    results: dict[str, massive.AIResponse] = {}
    started_at = time.time()

    with ThreadPoolExecutor(max_workers=n_models) as ex:
        futs = {ex.submit(massive.ask, prompt, m): m for m in models}
        for fut in as_completed(futs):
            m = futs[fut]
            try:
                results[m] = fut.result()
            except Exception as e:
                results[m] = massive.AIResponse(
                    model=m, query=prompt, completion="",
                    error=f"{type(e).__name__}: {e}",
                )
            if len(results) >= target_n:
                # Mark the rest as skipped without waiting
                for f, mm in futs.items():
                    if mm not in results and not f.done():
                        results[mm] = massive.AIResponse(
                            model=mm, query=prompt, completion="",
                            error=f"skipped (fastest_n={fastest_n} reached)",
                        )
                break

    answers = []
    for m in models:
        r = results.get(m)
        if r is None:
            continue
        answers.append({
            "model": r.model,
            "answer": r.completion,
            "sources": [{"url": s.url, "title": s.title} for s in r.sources],
            "elapsed_s": round(r.elapsed_s, 2),
            "error": r.error,
        })

    # consensus = which domains were cited by 2+ models?
    domain_to_models: dict[str, set[str]] = {}
    for a in answers:
        for s in a["sources"]:
            try:
                d = s["url"].split("/")[2] if "://" in s["url"] else s["url"]
            except IndexError:
                continue
            domain_to_models.setdefault(d, set()).add(a["model"])
    consensus_domains = sorted(
        ({"domain": d, "models": sorted(ms), "count": len(ms)}
         for d, ms in domain_to_models.items() if len(ms) >= 2),
        key=lambda x: -x["count"],
    )

    successful = [a for a in answers if not a.get("error")]
    return {
        "query": prompt,
        "models_called": models,
        "models_returned": [a["model"] for a in successful],
        "wall_clock_s": round(time.time() - started_at, 2),
        "answers": answers,
        "consensus_domains": consensus_domains,
    }


def render_text_summary(report: dict[str, Any]) -> str:
    """Human-readable summary that works as the `content[0].text` payload."""
    lines = []
    lines.append(f"QUERY: {report['query']}")
    lines.append(f"Models returned: {len(report['models_returned'])}/{len(report['models_called'])}  "
                 f"(wall clock {report['wall_clock_s']}s)")
    lines.append("")
    for a in report["answers"]:
        lines.append(f"--- {a['model'].upper()} ({a['elapsed_s']}s, sources={len(a['sources'])}) ---")
        if a["error"]:
            lines.append(f"  ERROR: {a['error']}")
        else:
            ans = a["answer"][:1200] + ("…" if len(a["answer"]) > 1200 else "")
            lines.append(ans)
        lines.append("")
    if report["consensus_domains"]:
        lines.append("Domains cited by 2+ models (rough consensus signal):")
        for cd in report["consensus_domains"][:10]:
            lines.append(f"  {cd['count']}/{len(report['models_returned'])}  {cd['domain']}  "
                         f"({', '.join(cd['models'])})")
    else:
        lines.append("No domains cited by 2+ models.")
    return "\n".join(lines)


# ---------- MCP stdio JSON-RPC server ----------

INPUT_SCHEMA = {
    "type": "object",
    "properties": {
        "prompt": {
            "type": "string",
            "description": "Question to ask all selected models. Max ~2000 chars.",
            "minLength": 1,
            "maxLength": 2047,
        },
        "models": {
            "type": "array",
            "items": {"enum": list(massive.MODELS)},
            "description": (
                "Subset of models to query. Defaults to all four. "
                "Drop 'copilot' if you need fast responses (it has the worst tail latency)."
            ),
            "default": list(massive.MODELS),
        },
        "fastest_n": {
            "type": "integer",
            "minimum": 1,
            "maximum": len(massive.MODELS),
            "description": (
                "Return as soon as this many models have replied. "
                "Useful to bound latency: fastest_n=3 typically returns in ~30s "
                "instead of waiting on Copilot."
            ),
        },
    },
    "required": ["prompt"],
}

TOOL_DEFINITION = {
    "name": TOOL_NAME,
    "description": TOOL_DESCRIPTION,
    "inputSchema": INPUT_SCHEMA,
}


def _ok(req_id: Any, result: dict) -> dict:
    return {"jsonrpc": "2.0", "id": req_id, "result": result}


def _err(req_id: Any, code: int, message: str, data: Any = None) -> dict:
    e = {"code": code, "message": message}
    if data is not None:
        e["data"] = data
    return {"jsonrpc": "2.0", "id": req_id, "error": e}


def handle_initialize(req: dict) -> dict:
    return _ok(req["id"], {
        "protocolVersion": PROTOCOL_VERSION,
        "capabilities": {"tools": {}},
        "serverInfo": {"name": SERVER_NAME, "version": SERVER_VERSION},
    })


def handle_tools_list(req: dict) -> dict:
    return _ok(req["id"], {"tools": [TOOL_DEFINITION]})


def handle_tools_call(req: dict) -> dict:
    params = req.get("params") or {}
    if params.get("name") != TOOL_NAME:
        return _err(req["id"], -32602, f"Unknown tool: {params.get('name')!r}")
    args = params.get("arguments") or {}
    prompt = args.get("prompt")
    if not isinstance(prompt, str) or not prompt.strip():
        return _err(req["id"], -32602, "`prompt` is required and must be a non-empty string")
    models = args.get("models") or list(massive.MODELS)
    if not isinstance(models, list) or not all(m in massive.MODELS for m in models):
        return _err(req["id"], -32602,
                    f"`models` must be a subset of {list(massive.MODELS)}")
    fastest_n = args.get("fastest_n")
    if fastest_n is not None and (not isinstance(fastest_n, int) or fastest_n < 1):
        return _err(req["id"], -32602, "`fastest_n` must be a positive integer")

    try:
        report = run_compare(prompt, models, fastest_n)
    except Exception as e:
        return _ok(req["id"], {
            "isError": True,
            "content": [{"type": "text", "text": f"Error: {type(e).__name__}: {e}"}],
        })

    return _ok(req["id"], {
        "isError": False,
        "content": [{"type": "text", "text": render_text_summary(report)}],
        "structuredContent": report,
    })


HANDLERS = {
    "initialize": handle_initialize,
    "tools/list": handle_tools_list,
    "tools/call": handle_tools_call,
}


def serve() -> None:
    # Validate token at startup so we fail fast.
    try:
        massive._token()
    except Exception as e:
        _stderr(f"startup error: {e}")
        sys.exit(1)

    write_lock = threading.Lock()

    def write(msg: dict) -> None:
        line = json.dumps(msg) + "\n"
        with write_lock:
            sys.stdout.write(line)
            sys.stdout.flush()

    _stderr(f"ready (protocol={PROTOCOL_VERSION}, tool={TOOL_NAME})")

    for raw_line in sys.stdin:
        raw_line = raw_line.strip()
        if not raw_line:
            continue
        try:
            req = json.loads(raw_line)
        except json.JSONDecodeError as e:
            _stderr(f"bad json: {e}: {raw_line[:120]!r}")
            continue

        method = req.get("method")
        if method and method.startswith("notifications/"):
            continue  # client-side notification; ignore
        handler = HANDLERS.get(method)
        if not handler:
            if "id" in req:
                write(_err(req["id"], -32601, f"Method not found: {method}"))
            continue

        # Run tool calls in a worker thread so the server can keep reading.
        if method == "tools/call":
            t = threading.Thread(
                target=lambda r=req: write(handler(r)),
                daemon=True,
            )
            t.start()
        else:
            write(handler(req))


if __name__ == "__main__":
    serve()
