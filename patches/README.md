# Patches for `@joinmassive/mcp-server` 0.1.0

Drop-in patch implementing four of the trivial fixes from [`../FEEDBACK.md`](../FEEDBACK.md). Verified working — applied to a copy of the unpacked `.mcpb`, then driven over stdio JSON-RPC to confirm each change has the intended effect (see `../test_patched.py`).

The patch is against the **bundled** `dist/index.js`. The PM should map each change to the equivalent TypeScript source in their repo (presumably `src/parsers/ai-html.ts`, `src/tools/web-fetch.ts`, `src/tools/ai-chat.ts`).

## What's in the patch

| # | File | Change | Verifies |
|---|---|---|---|
| 1 | `src/parsers/ai-html.ts` (or `dist/index.js`) | Strip `^(ChatGPT\|Gemini\|Copilot\|Perplexity)\s+said[:\s]*` from `stripCompletionHtml` output | All four chatbots' `"<Name> said:"` prefix is gone from `content[0].text` |
| 2 | same file | Strip Perplexity inline citation tokens (`research.contrary+2` style) | Trailing `+N` source-count badges no longer leak into prose |
| 3 | same file | Strip trailing UI chrome (`JSON`, `Show all`, `Show more`, `Edit in a page`) | Copilot `Show all` / `Edit in a page` no longer appears in answers |
| 4 | `src/tools/web-fetch.ts` | Heuristic `looks_like_error_page` flag in `web_fetch` `structuredContent` | YC 404 page now sets `looks_like_error_page: true, error_page_match: "File Not Found"` |
| 5 | `src/tools/ai-chat.ts` | Richer `ai_chat_completion` tool description with model-selection guidance | Tool description now tells the LLM when to use Perplexity vs ChatGPT vs `web_search` |

Note: surfacing the actual upstream HTTP status (the *real* fix for the 404 case) needs a server-side change to the `/browser` API endpoint — the MCP only sees the body. The heuristic in patch #4 is a band-aid; the real fix is API-level.

## Apply

```sh
# from the @joinmassive/mcp-server repo root
patch -p1 -i path/to/massive-mcp-0.1.0.patch
```

Or apply manually — only ~5 hunks across ~3 files.

## Verify

After applying and rebuilding, the test script `test_patched.py` in the parent repo passes against your fresh build:

```sh
MASSIVE_TOKEN=... python3 test_patched.py
```

Expected output:
- `prefix_present=False` for chatgpt/gemini/copilot
- `leftover citation tokens: []` for perplexity
- `looks_like_error_page: True` on a 404 URL
- `looks_like_error_page: False` on a real page
