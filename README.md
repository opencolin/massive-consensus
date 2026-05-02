# massive-consensus

A small toolkit that turns Massive's [AI Chat API](https://docs.joinmassive.com/web-render/ai) into a multi-LLM consensus engine — query ChatGPT, Gemini, Perplexity, and Copilot in parallel and surface where they agree, disagree, and hallucinate.

Built end-to-end in an evening as part of testing the Massive MCP and producing feedback for the team.

## The idea

A single LLM can confidently hallucinate — especially on niche or recent topics. Sales lead enrichment, due-diligence research, and fact-checking are exactly the workflows where one wrong-but-confident answer costs you a meeting.

Massive's API is the only place I know of where you can hit four consumer-LLMs (ChatGPT, Gemini, Perplexity, Copilot) through one endpoint. That makes a fundamentally different workflow possible:

> Don't ask one model. Ask all four. Trust the answer when ≥3 agree with sources. Verify by hand when they don't.

Massive's docs frame the API as a way to query an LLM. The more interesting framing is: it's a primitive for multi-LLM consensus. That's the differentiator — and what this repo demonstrates.

## What I built

Three small Python modules, stdlib only, no `pip install`:

- **`massive.py`** — clean wrapper around the API. Strips HTML out of the `completion` payload (more on that below), parses `sources` into a structured `[{url, title}]` list, retries on 5xx, and exposes `ask_all(prompt)` that fans out to all four models in parallel.
- **`consensus.py`** — CLI that asks all four models the same question and renders aligned answers + a "source-domain overlap" panel showing which domains were cited by 2+ models (rough cross-LLM credibility signal).
- **`enrich.py`** — lead-enrichment workflow. For each company name, asks all four models for a single structured JSON object (one-line pitch, B2B/B2C, founders, HQ, YC batch, hiring status), parses each, votes per field, and flags disagreement with `⚠ DISAGREE` so a human knows exactly what to verify by hand.

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

## Review of the Massive AI Chat API + MCP

Caveat up front: I tested the underlying HTTP API directly (Claude Code, not Claude Desktop), so the MCP packaging itself is judged through the lens of "what would the wrapped tool feel like for an agent caller." The feedback is mostly about the API contract that the MCP exposes; full notes are in [`FEEDBACK.md`](./FEEDBACK.md).

### What's genuinely good

- **The multi-model fanout is a real moat.** No other API I know of lets you hit four consumer-LLMs in one place. This is the headline.
- **Perplexity's `sources` are excellent** — clean URLs, deduped, ranked. Best-in-class.
- **Built-in caching** with `expiration` is well-designed for repeated runs (lead enrichment naturally retries).
- **Auth is dead simple** — Bearer token, done.
- **Latency on the fast 3 models** (~10–30s) is reasonable for the depth of answer you get back.

### Top gotchas, ranked by how much they hurt programmatic use

#### 1. `format=json` returns HTML inside the JSON envelope
The response is JSON, but `completion`, `prompt`, **and** `sources` are HTML strings. Every consumer has to ship an HTML stripper. A 2-sentence Gemini answer comes back as ~30 KB of chat-app DOM. Worse, `prompt` returns the rendered chat-UI HTML for the user's own query — surfacing the chat site's frontend into every API consumer.

**Ask:** add `format=text` (or `format=plain`) returning `{model, answer, sources: [{url, title, snippet}]}`. The current shape leaks the AI-chat-site DOM into every downstream tool.

#### 2. `sources` coverage varies wildly per model — sometimes empty
Same query ("What is the Massive web render API?"):

| Model | Sources extracted |
|---|---|
| Perplexity | 11 |
| Copilot | 1 |
| Gemini | 1 |
| ChatGPT | **0** |

For lead enrichment, sources *are* the deliverable. Citing "Indianapolis" with zero sources is worse than useless. The chat UIs all show citation markers — the API should return them as a structured array, not regex-out hrefs from raw HTML.

#### 3. Copilot is unreliable for structured-output prompts
3/3 of my real enrichment runs, Copilot did not return inside 75s and timed out (server still running — total elapsed including one retry was 152s). The other three models returned in 11–30s. With a tighter SLA, every enrichment looks like "75% of models responded."

**Ask:** document realistic per-model p95 latency (the docs say "up to 3 minutes" but the practical 95% on the fast three is ~30s — copilot is the outlier), and add a `fastest_n=3` knob so callers can stop waiting on the laggard.

#### 4. Single-model answers can hallucinate badly
First smoke test, I asked ChatGPT what Massive does. It confidently described it as *"a tool that enables real-time rendering of 3D models and immersive environments directly in web browsers"* — completely wrong. Gemini, Perplexity, and Copilot all got it right. This isn't really a Massive bug — it's a property of cold ChatGPT queries on niche topics — but it's worth surfacing in docs that single-model AI Chat answers should never be trusted for niche/recent facts, and that fanning out across models is the documented mitigation.

#### 5. Each model adds redundant prefix decoration
`"ChatGPT said: ..."`, `"Gemini said\n\n..."`, `"Copilot said\n\n..."`, (Perplexity none). Trivial to strip but every consumer ends up writing the same regex.

#### 6. `/ai/devices` returns `[]`
Documented as the source of valid `device` names. Returns an empty list. Stale doc or unshipped endpoint.

#### 7. Payload size
Gemini returned 1.4 MB for a 2-sentence answer — driven by an embedded `html` field that's the full chat page DOM. For high-throughput enrichment, an `?include=completion,sources` opt-out would matter.

#### 8. No streaming
A 30-second call with no intermediate output looks like a hung connection. Even partial-completion streaming would make this feel like the chat-API products it wraps.

#### 9. Unnormalized model output
For `yc_batch`, the same company came back as `"Spring 2026"`, `"S26"`, and `"YC S26"`. A small server-side canonical enum would massively reduce post-processing pain.

#### 10. The MCP probably exposes a single `ask` tool
Without unpacking the .mcpb I'm guessing — but the differentiated capability of this product is *multi-model fanout*, not asking a single LLM. Every developer is going to rebuild this. **The MCP should ship a first-class `compare_models` tool** that fans out to all four and returns aligned answers + per-field agreement. That's the killer feature; let it be the headline tool.

### Bottom-line product asks

If I had to pick three:

1. **`format=text`** — return parsed completion + structured sources. Stop leaking HTML.
2. **First-class `compare` MCP tool** — fan out to all models, return aligned answers + agreement flags. The thing every developer will build anyway.
3. **Per-model SLA + a `fastest_n` knob** — Copilot's tail latency makes the slow path the slow path. Let callers opt out.

## Usage

```sh
export MASSIVE_TOKEN=...   # https://dashboard.joinmassive.com/developer/api-keys

python3 consensus.py "What does the YC company Browserbase do, and who founded it?"
python3 enrich.py "Tsenta" "Pentagon" "Control Seat" --csv-out enriched.csv
```

Pure stdlib. No `pip install`.
