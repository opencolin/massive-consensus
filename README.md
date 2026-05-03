# massive-consensus

A small toolkit that turns Massive's [AI Chat API](https://docs.joinmassive.com/web-render/ai) into a multi-LLM consensus engine — query ChatGPT, Gemini, Perplexity, and Copilot in parallel and surface where they agree, disagree, and hallucinate.

Built end-to-end in an evening as part of testing the Massive MCP and producing feedback for the team.

## The idea

A single LLM can confidently hallucinate — especially on niche or recent topics. Sales lead enrichment, due-diligence research, and fact-checking are exactly the workflows where one wrong-but-confident answer costs you a meeting.

Massive's API is the only place I know of where you can hit four consumer-LLMs (ChatGPT, Gemini, Perplexity, Copilot) through one endpoint. That makes a fundamentally different workflow possible:

> Don't ask one model. Ask all four. Trust the answer when ≥3 agree with sources. Verify by hand when they don't.

Massive's docs frame the API as a way to query an LLM. The more interesting framing is: it's a primitive for multi-LLM consensus. That's the differentiator — and what this repo demonstrates.

## What I built

Pure-stdlib Python — no `pip install`.

**The consensus toolkit:**
- **`massive.py`** — clean wrapper around the API. Strips HTML out of the `completion` payload, parses `sources` into a structured `[{url, title}]` list, retries on 5xx, exposes `ask_all(prompt)` that fans out to all four models in parallel.
- **`consensus.py`** — CLI that asks all four models the same question and renders aligned answers + a "source-domain overlap" panel showing which domains were cited by 2+ models. Has a `--via-mcp DIST_INDEX_JS` flag that fans out via Massive's official MCP server over stdio JSON-RPC instead of the raw HTTP API — validates that the MCP surface alone is enough for the consensus workflow.
- **`enrich.py`** — lead-enrichment workflow. For each company name, asks all four models for a single structured JSON object (one-line pitch, B2B/B2C, founders, HQ, YC batch, hiring status), parses each, votes per field, and flags disagreement with `⚠ DISAGREE`.

**Implementations of the top three feedback asks** (so the founder can run them, not just read about them):
- **`compare_mcp.py`** — drop-in standalone MCP server exposing `ai_chat_compare(prompt, models?, fastest_n?)`, the missing 5th tool from the feedback. Pure-stdlib stdio JSON-RPC; no SDK dependency. Returns aligned per-model answers + cross-model source-domain consensus + an optional fast-fail-on-laggard knob.
- **`patches/massive-mcp-0.1.0.patch`** — unified diff against the unpacked `dist/index.js` implementing four trivial fixes from the feedback: prefix stripping, Perplexity citation-token cleanup, trailing UI chrome removal, an error-page heuristic in `web_fetch`'s `structuredContent`, and a richer model-selection guide in the `ai_chat_completion` tool description. Applied + verified working — see [`patches/README.md`](./patches/README.md).

**MCP-side test harness** (used to verify everything above):
- **`mcp_probe.py` / `mcp_probe2.py`** — drove Massive's MCP server directly over stdio JSON-RPC for a 12-test matrix, before any patches.
- **`test_compare_mcp.py`** — smoke-tests `compare_mcp.py` end-to-end via stdio.
- **`test_patched.py`** — verifies the patched MCP fixes the issues each patch was meant to fix.

## How it performed in real use

I ran the enrichment against three companies pulled from a real YC S26 ICP-qualification spreadsheet (Tsenta, Control Seat, Pentagon).

```
COMPANY: Tsenta
  Models that returned valid JSON: 3/4
  ERROR (copilot): timeout
  customer_type      b2c                                  [3/3]
  hq_city            Indianapolis                         [3/3]   ← all wrong (actually Terre Haute)
  founded_year       2025                                 [2/3  ⚠ DISAGREE]
      chatgpt     -> 2025
      gemini      -> 2025
      perplexity  -> 2026
  founders           Pulkit Gupta, Agnay Srivastava       [3/3]
  yc_batch           Summer 2026 / S26 / YC S26           same answer, three formats
```

Three patterns showed up over and over:

1. **Disagreement is honest signal.** When `is_hiring` came back as `unknown / yes / yes`, ChatGPT was being conservative because the careers page was sparse. That's exactly the kind of "go check yourself" flag enrichment needs — and you only get it by polling multiple models.
2. **Consensus is not truth.** All three models that returned data agreed on `Tsenta HQ = Indianapolis`. The actual address is Rose-Hulman in Terre Haute. When models share the same upstream training-data error, agreement doesn't validate. Sources are what matter — which leads directly to the feedback section.
3. **One model is a 5x latency outlier.** Three models returned in 11–30s. Copilot timed out past 75s on every JSON-style prompt I gave it. Without a timeout knob, every consensus call takes as long as the slowest model.

## Review of the Massive MCP server

Two passes: HTTP API tested directly with `consensus.py` and `enrich.py`, then the MCP server unpacked, source-read, and driven over stdio JSON-RPC for a 12-test matrix. Full writeup in [`FEEDBACK.md`](./FEEDBACK.md).

### What I love

- **Four well-chosen tools, not just one.** `web_fetch` (URL → Markdown), `web_search` (parsed Google SERP), `ai_chat_completion`, `account_status`. The bundle is broader and more useful than the AI-Chat docs alone suggest.
- **Markdown is the default for `web_fetch`** — the right default for LLM context.
- **`web_search` exists, is fast (~2–8s), and is parsed** (organic + AI overview + people-also-ask). This was a pleasant surprise — promote it harder, it's arguably the most generally useful tool.
- **`ai_chat_completion` strips HTML server-side** (cheerio) and returns sources as a structured `[{title, url}]` array. The two biggest pain points from raw-API testing are *fixed at the MCP layer*.
- **Validation errors are clean MCP errors** with structured Zod paths — agents can self-correct.
- **`.mcpb` install with OS-keychain token storage** — great UX win for non-technical users.

### Top MCP-layer gotchas, ranked

#### 1. There is no `compare_models` / fanout tool
The single most differentiated capability of this product (4 LLMs through one API) is **not** a first-class tool — every developer who wants consensus has to fan out themselves. I wrote one in this repo (`consensus.py`); literally every other dev who hits this product seriously will too.

**Ask:** ship a 5th tool — `ai_chat_compare(prompt, models?, fastest_n?)` — that fans out, returns aligned answers, flags inter-model disagreement, and unions sources across models with provenance. **This is the killer feature. Let it be the headline tool.**

#### 2. The MCP doesn't strip the model's own prefix decoration
Every `ai_chat_completion` response leaks the source UI's chrome:

| Model | Prefix observed |
|---|---|
| ChatGPT | `"ChatGPT said:\n..."` |
| Gemini | `"Gemini said\nJSON\n..."` |
| Copilot | `"Copilot said\n...\nShow all\nEdit in a page"` |
| Perplexity | (no prefix, but inline tokens like `"research.contrary+2"` leak through) |

The MCP source has a beautiful `stripCompletionHtml` cheerio function — but skips this trivial last-mile cleanup. **Ask:** strip the `^(ChatGPT|Gemini|Copilot|Perplexity)\s+said[:\s]*` prefix and remove perplexity inline citation tokens once they're already structured into `sources`.

#### 3. Default per-call timeout is 180s — too patient for agentic use
`DEFAULT_TIMEOUT_MS = 180000` (3 min). Observed latencies in my matrix:

| Tool | Observed elapsed |
|---|---|
| `account_status` | 0.4s |
| `web_fetch` | 0.6–0.9s |
| `web_search` | 2.5–8s |
| `ai_chat_completion` (gemini) | 7–8s |
| `ai_chat_completion` (chatgpt) | 14–15s |
| `ai_chat_completion` (perplexity) | 18–51s |
| `ai_chat_completion` (copilot) | 35s nominal — 152s on structured-output prompts in raw-API tests |

A 3-minute hang inside an agent loop is a UX disaster. **Ask:** set a smarter default per tool and document realistic p95 per model.

#### 4. `web_fetch` silently returns 404 pages as success
`web_fetch("https://www.ycombinator.com/companies/browserbase", "markdown")` returned `is_error: false` with text content `"Y Combinator | File Not Found / # 404 / ..."`. The `structuredContent` only has `{format, url, bytes}` — no `status_code`. The agent has to *read* the page content to know the request 404'd. **Ask:** include the upstream HTTP status (and `redirect_chain`) in `structuredContent`. Surface non-2xx as MCP errors.

#### 5. Hardcoded Google selectors in `web_search`
`web_search` parses with cheerio and hardcoded class names: `.yuRUbf`, `.pOOWX`, `.VwiC3b`, `.related-question-pair`. The source comments even say *"current (2026) AIO answer container"* — you know it's brittle. A Google A/B test rotates one class and the tool returns `{organic: [], ai_overview: null}` silently. **Ask:** add a `parser_version` tag, expose `parsed_count`, emit a metric on empty-result regressions.

#### 6. Markdown output is half-markdown / half-HTML
`web_fetch("https://news.ycombinator.com", "markdown")` returned 13.8 KB containing literal `<table>`, `<tr>`, `<td>` tags interleaved with markdown links. Agents end up parsing a hybrid. **Ask:** commit to full HTML→markdown of block-level structure or rename `format: "mixed"`.

#### 7. Tool descriptions don't help the LLM choose
`ai_chat_completion`'s description: *"Get a chatbot answer (ChatGPT, Gemini, Perplexity, or Copilot) with structured sources. Cost: 1 credit base."* This says nothing about: when to use it instead of `web_search`, which model to pick, hallucination risk, or that `sources` may be empty.

In contrast `web_fetch`'s description is rich (capabilities, geo-targeting, pricing). **Ask:** rewrite `ai_chat_completion`'s similarly: *"Use Perplexity for sourced/recent facts (best citation coverage), ChatGPT for general analysis, Gemini for fast structured output, Copilot for thorough but slower answers. For verifiable facts, prefer `web_search` or fan out across models."*

#### 8. Single-model hallucination passthrough
On the cold-query test ("What is the JoinMassive Web Render API?"), ChatGPT confidently invented *"a cloud-based rendering platform... 3D scenes, animations, and simulations..."* — completely wrong. Perplexity got it right, but in a degraded "I don't have tools access" mode and called the company "Masssive". The MCP returns whatever the model gave with no warning or fallback. **Ask:** optional `fallback_models` arg — if the primary returns 0 sources or below a confidence threshold, retry with the fallback. Or just bake this into `ai_chat_compare`.

#### 9. `account_status` returns a unitless number with a hardcoded threshold
`"99832 credits remaining."` But what does a credit cost? What's the spend rate? The `low_balance` flag flips at <100 — for a heavy user that's "30 seconds left." **Ask:** include `usd_remaining`, `credits_used_30d`, `estimated_days_at_current_rate`. Make the low-balance threshold configurable.

#### 10. Source coverage varies wildly across models (passthrough)
Same query, source counts: Perplexity 11, ChatGPT 0. ChatGPT and Gemini show inline citation markers in their UIs but the API can't extract them, so the MCP can't surface them either. **Ask (API layer):** real inline-citation extraction.

### Bottom-line product asks

If I had to pick three:

1. **Ship `ai_chat_compare`.** The killer feature; not building it is leaving the moat on the table.
2. **Last-mile cleanup of `ai_chat_completion`** — strip "Model said:" prefix, drop inline citation tokens, surface upstream HTTP status on `web_fetch`. All small, all visible to every agent call.
3. **Per-tool timeout defaults + observability for `web_search` parsers** — 180s blanket is wrong; hardcoded Google selectors will silently rot.

## Usage

```sh
export MASSIVE_TOKEN=...   # https://dashboard.joinmassive.com/developer/api-keys

# Consensus across 4 models via direct HTTP
python3 consensus.py "What does the YC company Browserbase do, and who founded it?"

# Same, but routed through Massive's official MCP server via stdio
python3 consensus.py --via-mcp /path/to/massive-mcp/dist/index.js "..."

# Lead enrichment with per-field consensus + disagreement flags
python3 enrich.py "Tsenta" "Pentagon" "Control Seat" --csv-out enriched.csv
```

**Run the standalone `ai_chat_compare` MCP server** (drop into Claude Desktop config or any MCP client):

```json
{
  "mcpServers": {
    "massive-compare": {
      "command": "python3",
      "args": ["/absolute/path/to/compare_mcp.py"],
      "env": { "MASSIVE_TOKEN": "your-token" }
    }
  }
}
```

Pure stdlib. No `pip install`.
