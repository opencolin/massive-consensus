# massive-consensus

Multi-LLM consensus + lead-enrichment tools built on the [Massive AI Chat API](https://docs.joinmassive.com/web-render/ai), which fans queries out across ChatGPT, Gemini, Perplexity, and Copilot.

A single LLM can confidently hallucinate. Asking 4 LLMs in parallel and surfacing where they (dis)agree is a much better signal — especially for fact-checky tasks like sales lead enrichment.

## Setup

```sh
export MASSIVE_TOKEN=...   # https://dashboard.joinmassive.com/developer/api-keys
```

Pure stdlib. No `pip install`.

## `consensus.py` — fact-check across all 4 models

```sh
python3 consensus.py "What does the YC company Browserbase do, and who founded it?"
```

Renders each model's answer side-by-side plus a "source-domain overlap" panel showing which domains were cited by 2+ models — a rough cross-LLM consensus signal.

## `enrich.py` — structured-JSON lead enrichment

```sh
python3 enrich.py "Tsenta" "Pentagon" "Control Seat" --csv-out enriched.csv
```

For each company, asks all 4 models for a structured JSON object (pitch, customer type, founders, hq, batch, hiring), parses each, votes per-field, and flags disagreement with `⚠ DISAGREE`. Output to stdout, JSON, or CSV.

Sample output:

```
COMPANY: Tsenta
  Models that returned valid JSON: 3/4
  ERROR (copilot): timeout
  customer_type      b2c                                    [3/3]
  hq_city            Indianapolis                           [3/3]   ← all 3 agreed, all wrong
  founded_year       2025                                   [2/3  ⚠ DISAGREE]
      chatgpt     -> 2025
      gemini      -> 2025
      perplexity  -> 2026
  founders           ['Pulkit Gupta', 'Agnay Srivastava']   [3/3]
```

## `massive.py` — the wrapper

Thin client:
- Strips HTML out of `completion` (the API returns HTML inside `format=json`).
- Strips `"<Model> said:"` prefix decoration.
- Parses `sources` HTML into `[{url, title}]`.
- Retries on 5xx (the API returns intermittent 500s).
- `ask_all(prompt)` fans out to all 4 models in parallel.

## Findings

See [`FEEDBACK.md`](./FEEDBACK.md) for a candid build-feedback writeup based on running these tools against real YC S26 companies — payload shape issues, copilot tail latency, source coverage variance, and consensus-isn't-truth gotchas.
