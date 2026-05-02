# Massive AI Chat API — Build Feedback

Hands-on testing of the AI Chat API (`render.joinmassive.com/ai`) by building a multi-LLM consensus tool and a YC-lead enrichment script that fans out to chatgpt / gemini / perplexity / copilot in parallel and parses structured JSON answers.

What I built (in this repo):
- `massive.py` — thin Python wrapper: HTML-stripped `completion`, parsed `sources: [{url,title}]`, retry on 5xx, parallel fanout.
- `consensus.py` — CLI that asks all 4 models the same question and shows where they agree/disagree.
- `enrich.py` — lead enrichment: ask 4 models for structured JSON about a company, vote per-field, flag disagreement.

I ran the enrichment on three companies from your YC sample CSV (Tsenta, Control Seat, Pentagon). Total wall time per company: ~30s when all 4 return; ~150s when copilot is in the mix because it doesn't.

## Top issues, ranked by how much they hurt programmatic use

### 1. `format=json` returns HTML inside JSON
The response envelope is JSON, but `completion`, `prompt`, **and** `sources` are HTML strings — not parsed text. Every consumer has to ship an HTML stripper.

- `completion` for the same question (gemini): 29 KB of HTML for a 2-sentence answer.
- `prompt` is the rendered chat-UI HTML for the user's own query — bizarre to surface.
- `sources` is a slab of HTML you have to regex `href`s out of.

**Asks:**
- Add a `format=text` (or `format=plain`) that returns a clean object: `{model, answer, sources: [{url, title, snippet}]}`.
- If keeping the current shape, at minimum return `completion` as plain text and `sources` as a JSON array. The current setup leaks the AI-chat-site DOM into every API consumer.

### 2. `sources` coverage varies wildly between models — sometimes empty
Same question ("What is the Massive web render API"):

| Model | Sources extracted |
|---|---|
| perplexity | 11 |
| copilot | 1 |
| gemini | 1 |
| chatgpt | 0 |

ChatGPT's `sources` field was literally empty even though the answer cited inline. For lead enrichment, sources are the actual deliverable — citing "Indianapolis" with zero sources is worthless. Worse, when all 3 models that returned data confidently agreed Tsenta's HQ is **Indianapolis** — the founders are at Rose-Hulman in **Terre Haute**, IN. 3/3 agreement, all wrong, and no sources to backstop the call.

**Asks:**
- Inline-citation extraction (the little [1] [2] markers in the chat) — surface those as a structured array, not regexed-out hrefs from raw HTML. ChatGPT and Gemini both have sources visible in their UIs; the API should return them.
- A `min_sources` parameter that retries / falls back if the model gives a sourceless answer.

### 3. Copilot is unreliable for structured-output prompts
3/3 of my real enrichment runs, copilot did not return inside 75s and timed out (server-side likely still running — total elapsed including 1 retry was 152s). The other 3 models returned in 11–30s.

If I'd had a tighter SLA, every enrichment would have looked like "75% of models responded." Bear's not great for production.

**Asks:**
- Document a realistic per-model p95 latency. Docs say "up to 3 minutes" but the practical 95% on chatgpt/gemini/perplexity is ~30s — copilot is the outlier.
- A `models=auto` or `fastest_n=3` option so callers can stop waiting on the laggard.

### 4. Each model adds a redundant prefix to its answer
- `"ChatGPT said: ..."`
- `"Gemini said\n\n..."`
- `"Copilot said\n\n..."`
- (perplexity: no prefix)

Trivial to strip but every consumer ends up writing the same regex. Strip server-side.

### 5. Hallucination risk on a single-model call
First smoke test, asking ChatGPT what Massive does, got back: *"a tool that enables real-time rendering of 3D models and immersive environments directly in web browsers."* Confidently wrong. Gemini, Perplexity, and Copilot all got it right.

This isn't really a Massive bug — it's a feature of the underlying ChatGPT product on cold queries — but it's worth surfacing in docs that single-model AI Chat answers can hallucinate badly, especially for niche/recent topics, and that fanning out across `model=` is the mitigation.

### 6. `/ai/devices` returns `[]`
Documented as the source of valid `device` names. Empty list. Either the endpoint hasn't shipped or docs are out of date.

### 7. Payload size
A two-sentence answer from Gemini = 1.4 MB response (driven by the embedded `html` field). For high-throughput enrichment that's wasteful.

**Ask:** option to opt out of the `html` payload — `?include=completion,sources` — if you don't need it.

### 8. No streaming
A 30-second call with no intermediate output looks like a hung connection. Even just streaming the `completion` token-by-token would make this feel like the chat-API products it wraps.

### 9. Inconsistent batch / model labelling
On enrichment, asking the same `yc_batch` field across models produced `"Spring 2026"`, `"S26"`, and `"YC S26"` for the same company. This is a model-side issue but a server-side normalizer (or a small "canonical YC batch" enum) would massively reduce post-processing.

### 10. The MCP packaging surface is probably one tool
Without unpacking the .mcpb, my guess is it exposes a single `ask` tool. The unique thing this API enables is *multi-model query in one place* — every other AI API only gives you one model. The MCP should ship a first-class `compare_models` tool (fans out, returns aligned answers + agreement flags). I had to build it; every other developer will too.

## What's good
- **Multi-model fanout is the differentiator.** No other API I know of lets you hit ChatGPT, Gemini, Perplexity, *and* Copilot in one place. That's a real moat.
- Perplexity sources are excellent — clean URLs, deduplicated, ranked.
- Built-in caching with `expiration` is well-designed for repeated runs (lead-enrichment workflows naturally retry).
- Bearer-token auth is dead simple.
- Latency on the fast 3 models (~10–30s) is reasonable for the depth of answer.

## Real-data snapshot — three YC S26 companies

```
COMPANY: Tsenta
  Models returning valid JSON: 3/4 (copilot timeout)
  customer_type      b2c            [3/3]
  hq_city            Indianapolis   [3/3]   ⚠ all wrong (actually Rose-Hulman / Terre Haute)
  founded_year       2025           [2/3]   chatgpt+gemini=2025, perplexity=2026
  founders           Pulkit Gupta, Agnay Srivastava   [3/3]
  yc_batch           {Summer 2026, S26, YC S26}       all "right" but unnormalized

COMPANY: Control Seat
  founders           Jack Grodnick    [1/2]
                     chatgpt: 1 founder; gemini: 2 founders (correct)
  hq_city            San Francisco    [2/2]   ⚠ user CSV says Dartmouth/US — needs verification

COMPANY: Pentagon
  Solid 3/3 agreement on every field except yc_batch formatting.
```

The agreement scores are honest — "3/3 agree" is meaningfully different from "1 model said it" — but it doesn't catch the case where all models share the same upstream training-data error (Tsenta HQ). Fact-checking with the `sources` array would catch this if the sources were structured.

## Bottom-line product asks (if I had to pick three)

1. **`format=text`** — return parsed completion + structured sources. Stop leaking HTML.
2. **`compare` MCP tool** — fan out to all models, return aligned answers + per-field agreement. The thing every developer is going to build.
3. **Per-model SLA + a `fastest_n` knob** — copilot's tail latency makes the slow path the slow path. Let callers opt out.
