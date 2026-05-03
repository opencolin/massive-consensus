# Massive — Build Feedback

Two passes of testing:
1. **Raw HTTP API** (`render.joinmassive.com/ai`) — built a Python wrapper, multi-LLM consensus CLI, and YC-lead enrichment script. Ran against real YC S26 companies.
2. **MCP server** (`@joinmassive/mcp-server` 0.1.0) — unpacked the .mcpb, read the source, drove the server directly over stdio JSON-RPC, ran a 12-test matrix.

The MCP fixes some API-layer pain. It also passes through some, and adds its own. They're separated below.

---

## Part 1 — MCP server review (the higher-leverage feedback)

The MCP exposes 4 tools, doing meaningful post-processing on top of the API:

| Tool | What it does | Notes |
|---|---|---|
| `web_fetch` | URL → Markdown / rendered HTML / raw HTML | Markdown default — great |
| `web_search` | Google SERP → parsed JSON (organic, ai_overview, people_also_ask) | Cheerio-based, hardcoded selectors |
| `ai_chat_completion` | One LLM (chatgpt/gemini/perplexity/copilot) → text + structured sources | HTML stripped server-side |
| `account_status` | Credit balance | Free, sub-second |

### What I love

- **Markdown is the default** for `web_fetch`. That's the right default for LLM context.
- **`web_search` exists and is fast** (~2–8s). Parsed `organic` + `ai_overview` + `people_also_ask` is exactly the shape you want for an agent. This was a pleasant surprise — nothing in the AI-Chat docs led me to it. **Promote it harder**: it's arguably the most useful tool in the bundle for general agents.
- **`ai_chat_completion` strips HTML server-side** (cheerio) and returns `sources` as a structured `[{title, url}]` array. The two biggest pain points from raw-API testing are *fixed at the MCP layer*. Both `text` content and `structuredContent` are populated, which is the right pattern.
- **Validation errors are clean** — invalid args come back as `-32602 Input validation error` with a structured Zod path. Agents can self-correct.
- **`account_status`** — cute, fast, free. Useful as a pre-flight check.

### Top MCP-layer gotchas, ranked

#### MCP-1. The MCP doesn't strip the model's own prefix decoration
Every `ai_chat_completion` response leaks the source UI's chrome into the agent context:

| Model | Prefix observed |
|---|---|
| ChatGPT | `"ChatGPT said:\n..."` |
| Gemini | `"Gemini said\n..."` (sometimes also literal `"JSON\n"` before code blocks) |
| Copilot | `"Copilot said\n..."` |
| Perplexity | (no prefix, but inline citation tokens like `"research.contrary+2"` leak through into prose) |

The MCP source has a beautiful `stripCompletionHtml` cheerio function for HTML — but skips this trivial last-mile cleanup. **Ask:** strip the `^(ChatGPT|Gemini|Copilot|Perplexity)\s+said[:\s]*` prefix and any `"JSON"` / `"Show all"` / `"Edit in a page"` decoration, and ideally remove the perplexity inline citation tokens once they're already structured into `sources`.

#### MCP-2. There is no `compare_models` / fanout tool
The single most differentiated capability of this product (4 LLMs through one API) is **not** a first-class tool — every developer who wants consensus has to fan-out themselves. I wrote one in this repo (`consensus.py`); literally every other dev who hits this product seriously will too.

**Ask:** Ship a 5th tool — `ai_chat_compare(prompt, models?, fastest_n?)` — that fans out, returns aligned answers, and flags inter-model disagreement. This is the killer feature; let it be the headline tool. Bonus: it can de-duplicate sources across models (Perplexity's 10 + Copilot's 2 → unioned 12 with provenance).

#### MCP-3. Default per-call timeout is 180s — too patient for agentic use
`DEFAULT_TIMEOUT_MS = 180000` (3 min). Configurable via `MASSIVE_TIMEOUT_MS` env. In practice on a healthy session:

| Tool | Observed elapsed |
|---|---|
| `account_status` | 0.4s |
| `web_search` | 2.5–8s |
| `web_fetch` | 0.6–0.9s |
| `ai_chat_completion` (gemini) | 7–8s |
| `ai_chat_completion` (chatgpt) | 14–15s |
| `ai_chat_completion` (perplexity) | 18–51s |
| `ai_chat_completion` (copilot) | 35s nominal — 152s+ on structured-output prompts in earlier raw-API tests |

A 3-minute hang inside an agent loop is a UX disaster. **Ask:** set a smarter default per tool (`account_status` should fail at 5s, `ai_chat_completion` at 60s with retry on a different model), and document realistic p95 per model.

#### MCP-4. Upstream HTTP errors are silently swallowed by `web_fetch`
Test 5 in my probe: `web_fetch("https://www.ycombinator.com/companies/browserbase", format="markdown")` returned `is_error: false` with text content:

```
Y Combinator | File Not Found
# 404
## File Not Found
[Back to the homepage](/)
```

The agent now has to *read* the page content to figure out the request actually 404'd. The `structuredContent` only has `{format, url, bytes}` — no `status_code`. **Ask:** include the upstream HTTP status (and ideally a `redirect_chain`) in `structuredContent`. Better still: surface non-2xx as an MCP error with the body still attached, so agents can short-circuit.

#### MCP-5. Hardcoded Google selectors in `web_search`
`web_search` parses the SERP using cheerio with hardcoded class names: `.yuRUbf` (organic), `.pOOWX` (AIO), `.VwiC3b` (snippet), `.related-question-pair` (PAA). The source comments even say *"current (2026) AIO answer container"* — **you know it's brittle.** A Google A/B test rotates one class name and `web_search` returns `{organic: [], ai_overview: null, ...}` silently.

**Ask:** add a parser-version tag to the response (e.g. `parser_version: "serp-2026-05"`), expose a `parsed_count` field so callers can detect collapse, and add observability — emit a metric whenever a parser returns an empty `organic` array on a query that historically returns results.

#### MCP-6. Markdown output is half-markdown / half-HTML
`web_fetch` on `https://news.ycombinator.com` with `format: "markdown"` returned 13.8 KB containing literal `<table>`, `<tr>`, `<td>` tags interleaved with markdown links. Agents end up trying to parse a hybrid. **Ask:** either commit to HTML→markdown conversion of all block-level structure (drop the tables or convert to markdown-pipe tables) or rename the output `format: "mixed"`.

#### MCP-7. Tool descriptions don't help the LLM choose
`ai_chat_completion`'s description is *"Get a chatbot answer (ChatGPT, Gemini, Perplexity, or Copilot) with structured sources. Cost: 1 credit base."* This tells the LLM nothing about:
- When to use it instead of `web_search`
- Which model to pick (Perplexity for sourced facts? Gemini for fast structured output?)
- That ChatGPT will confidently hallucinate niche topics
- That `sources` may be empty

In contrast, `web_fetch`'s description is rich: lists capabilities, mentions geo-targeting, even pricing multipliers. **Ask:** rewrite `ai_chat_completion` similarly. Recommended: *"Ask one of four chatbots a free-text question. Use Perplexity for sourced/recent facts (best citation coverage), ChatGPT for general analysis, Gemini for fast structured output, Copilot for thorough but slower answers. Sources may be empty for ChatGPT/Gemini — for verifiable facts prefer `web_search` or fan out across models."*

#### MCP-8. `account_status` returns a unitless number
`"99832 credits remaining."` — but what does a credit cost? What's the spend rate? Tool descriptions mention "1 credit base" and "premium features add multipliers" but the user has no way to convert credits to dollars or to runway. **Ask:** include `usd_remaining`, `credits_used_30d`, and `estimated_days_at_current_rate` in the response. Or at least a `dollars_per_credit` constant.

#### MCP-9. The `low_balance` flag is hardcoded at <100 credits
With `99832` remaining and 1-credit base cost per call, that's ~99,732 calls of headroom. The flag flips at <100 — for a heavy user that's "you have ~30 seconds left." **Ask:** make the threshold configurable, or scale it by 7-day burn rate.

#### MCP-10. Hallucination passthrough — no model-fallback strategy
On the cold-query test ("What is the JoinMassive Web Render API?"), ChatGPT confidently invented *"a cloud-based rendering platform designed to handle large-scale rendering tasks... 3D scenes, animations, and simulations..."* Wrong. Perplexity got it right but in a degraded "I don't have tools access" mode and called the company "Masssive" with three S's. The MCP returns whatever the model gave with no warning.

**Ask:** for `ai_chat_completion`, optionally accept a `fallback_models: ["perplexity", "gemini"]` arg — if the primary returns 0 sources or below a confidence threshold, retry with the fallback. Or just bake this into `ai_chat_compare`.

### Stuff that's well-built, just to call out

- The retry-on-503 with `Retry-After` honoring (capped at 30s) is correct.
- 25 MB response cap (`DEFAULT_MAX_RESPONSE_BYTES`) is a sensible safety net.
- Streaming-read with backpressure (`reader.read()` loop with byte cap) avoids unbounded memory.
- `User-Agent` and `X-Source` headers identify the MCP version — good for upstream observability.
- Tool input schemas use Zod with helpful `.describe()` calls and good constraints (URL must be http(s), prompt ≤ 2047 chars, country ISO-2, max_results 1–50).
- The `.mcpb` install path with OS-keychain token storage is a great UX win for non-technical users.

### Bottom-line MCP asks (if I had to pick three)

1. **Ship `ai_chat_compare`.** It's the killer feature; not building it is leaving the moat on the table.
2. **Last-mile cleanup of `ai_chat_completion`** — strip "Model said:" prefix, drop inline citation tokens, surface upstream HTTP status on `web_fetch`.
3. **Per-tool timeout defaults + observability for `web_search`** — 180s blanket is wrong; hardcoded Google selectors will silently rot.

---

## Part 2 — Underlying API observations (mostly relevant if you don't use the MCP)

These are still real for direct API users but are mostly fixed at the MCP layer. Documenting them in case there are SDK / non-MCP callers.

### API-1. `format=json` returns HTML inside JSON
The response is JSON, but `completion`, `prompt`, **and** `sources` are HTML strings. Consumers have to ship a stripper. The MCP layer fixes this for `ai_chat_completion`'s `completion` and `sources` (cheerio); raw API consumers still see ~30 KB of chat-app DOM for a two-sentence answer.

**Ask:** add `format=text` returning `{model, answer, sources: [{url, title, snippet}]}` at the API layer. Don't make every SDK reimplement what the MCP already did.

### API-2. `sources` coverage varies wildly between models
Same query ("What is the Massive web render API?"):

| Model | Sources extracted |
|---|---|
| Perplexity | 11 |
| Copilot | 1 |
| Gemini | 1 |
| ChatGPT | **0** |

For lead enrichment, sources *are* the deliverable. **Ask:** inline-citation extraction (the `[1] [2]` markers in the chat) — surface those as a structured array. ChatGPT and Gemini both have sources visible in their UIs; the API should return them.

### API-3. `prompt` field returns the chat-UI's rendered HTML for the user's own query
This is *bizarre*. It's the chat site's `<h1>` element wrapping the user's question with full Tailwind classes and 600+ chars of decoration. Just don't return it (or rename it `chat_page_prompt_html` so consumers know to ignore).

### API-4. `/ai/devices` returns `[]`
Documented as the source of valid `device` names in the manifest's docs. Returns an empty array. Stale doc or unshipped endpoint.

### API-5. Single-model hallucination on niche topics
Documented above (MCP-10). Mostly a property of the underlying chatbots, but worth surfacing in API docs that single-model AI Chat answers should never be trusted for niche/recent facts.

### API-6. Payload bloat
A two-sentence Gemini answer = 1.4 MB raw response (driven by an embedded `html` field that's the full chat page DOM). MCP doesn't expose `html` to the agent, but every API call still pulls the bytes over the wire.

**Ask:** an `?include=completion,sources` query param to drop the `html` field server-side.

### API-7. No streaming
A 30-second call with no intermediate output looks like a hung connection. SSE on `/ai` would be a real UX upgrade.

---

## Real-data snapshot — three YC S26 companies (raw API)

```
COMPANY: Tsenta
  Models that returned valid JSON: 3/4 (copilot timed out at 75s — see MCP-3)
  customer_type      b2c            [3/3]
  hq_city            Indianapolis   [3/3]   ⚠ all wrong (actually Rose-Hulman / Terre Haute)
  founded_year       2025           [2/3]   chatgpt+gemini=2025, perplexity=2026
  founders           Pulkit Gupta, Agnay Srivastava   [3/3]
  yc_batch           {Summer 2026, S26, YC S26}       same answer, three formats

COMPANY: Pentagon
  Solid 3/3 agreement on every field except yc_batch formatting.
```

The "consensus = truth" assumption breaks on Tsenta HQ — three models share an upstream search-data error. **Sources matter, not just agreement.** The MCP's structured-sources output is exactly the right primitive to backstop this — once the inline-citation gap (#2) is closed.

---

## Summary asks for the PM

If I had to staff this:

1. (1 day) **Last-mile cleanup of `ai_chat_completion`** — prefix strip, citation-token strip, surface HTTP status on `web_fetch`, fix markdown table conversion in `web_fetch`. All small.
2. (3 days) **`ai_chat_compare` tool** — fanout, source-union, agreement flags, `fastest_n` knob.
3. (1 week) **API-layer `format=text` + inline-citation extraction.** This is the only foundational change; everything else is wrappers.
4. (always) **Observability on `web_search` parsers** — hardcoded selectors will rot, you want to know first.

Tools are good. The product surface (web_fetch + web_search + ai_chat_completion + account_status) is well-chosen. The biggest leverage is in #2 — the consensus story is the differentiated story.
