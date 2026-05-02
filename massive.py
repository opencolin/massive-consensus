"""Wrapper around the Massive AI Chat API.

The raw API returns JSON with HTML payloads in `completion` and `sources`.
This module gives you plain text + parsed `[{title, url}]` source lists.
"""
from __future__ import annotations

import os
import re
import json
import time
import urllib.parse
import urllib.request
import concurrent.futures
from dataclasses import dataclass, asdict, field
from html.parser import HTMLParser
from typing import Iterable

API_URL = "https://render.joinmassive.com/ai"
MODELS = ("chatgpt", "gemini", "perplexity", "copilot")
DEFAULT_TIMEOUT = 75  # docs say up to 3 min, but in practice 60s covers ~99%; bail fast


def _token() -> str:
    tok = os.environ.get("MASSIVE_TOKEN")
    if not tok:
        raise RuntimeError("MASSIVE_TOKEN env var not set")
    return tok


class _TextExtractor(HTMLParser):
    """Strip tags but preserve readable spacing. Drops <script>/<style>."""

    _BLOCK = {"p", "div", "li", "tr", "br", "h1", "h2", "h3", "h4", "h5", "h6"}
    _SKIP = {"script", "style", "svg", "noscript"}

    def __init__(self) -> None:
        super().__init__()
        self.parts: list[str] = []
        self._skip_depth = 0

    def handle_starttag(self, tag, attrs):
        if tag in self._SKIP:
            self._skip_depth += 1
        elif tag in self._BLOCK:
            self.parts.append("\n")

    def handle_endtag(self, tag):
        if tag in self._SKIP and self._skip_depth:
            self._skip_depth -= 1
        elif tag in self._BLOCK:
            self.parts.append("\n")

    def handle_data(self, data):
        if not self._skip_depth:
            self.parts.append(data)


def html_to_text(html: str) -> str:
    if not html:
        return ""
    p = _TextExtractor()
    p.feed(html)
    text = "".join(p.parts)
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n[ \t]+", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


@dataclass
class Source:
    url: str
    title: str = ""


def parse_sources(html: str) -> list[Source]:
    """Pull out (url, anchor-text) pairs from a sources HTML blob."""
    if not html:
        return []
    seen: dict[str, Source] = {}
    for m in re.finditer(
        r'<a\b[^>]*href="(?P<href>[^"#][^"]*)"[^>]*>(?P<inner>.*?)</a>',
        html,
        re.DOTALL | re.IGNORECASE,
    ):
        url = m.group("href")
        if url.startswith(("javascript:", "mailto:", "#")):
            continue
        title = html_to_text(m.group("inner")).strip()
        # First sighting of a URL wins — usually has the cleanest title
        if url not in seen:
            seen[url] = Source(url=url, title=title)
    return list(seen.values())


@dataclass
class AIResponse:
    model: str
    query: str
    completion: str  # plain text
    sources: list[Source] = field(default_factory=list)
    elapsed_s: float = 0.0
    raw_bytes: int = 0
    error: str | None = None

    def to_dict(self) -> dict:
        d = asdict(self)
        d["sources"] = [asdict(s) for s in self.sources]
        return d


_PREFIX_RE = re.compile(r"^\s*(ChatGPT|Gemini|Copilot|Perplexity)\s+said[:\s]*", re.IGNORECASE)


def _clean_completion(text: str) -> str:
    return _PREFIX_RE.sub("", text).strip()


def ask(prompt: str, model: str = "chatgpt", *, timeout: int = DEFAULT_TIMEOUT,
        expiration: int | None = None, retries: int = 1) -> AIResponse:
    """Ask one model a single question. Returns plain-text completion + parsed sources.

    Retries on 5xx because the API returns intermittent 500/503s.
    """
    if model not in MODELS:
        raise ValueError(f"unknown model {model}; expected one of {MODELS}")
    params = {"prompt": prompt, "model": model, "format": "json"}
    if expiration is not None:
        params["expiration"] = str(expiration)
    url = f"{API_URL}?{urllib.parse.urlencode(params)}"
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {_token()}"})

    t_start = time.time()
    last_err: str | None = None
    for attempt in range(retries + 1):
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                body = r.read()
            data = json.loads(body)
            return AIResponse(
                model=model,
                query=prompt,
                completion=_clean_completion(html_to_text(data.get("completion") or "")),
                sources=parse_sources(data.get("sources") or ""),
                elapsed_s=time.time() - t_start,
                raw_bytes=len(body),
            )
        except urllib.error.HTTPError as e:
            last_err = f"HTTP {e.code}"
            if 500 <= e.code < 600 and attempt < retries:
                time.sleep(2 * (attempt + 1))
                continue
            break
        except Exception as e:
            last_err = f"{type(e).__name__}: {e}"
            if attempt < retries:
                time.sleep(2 * (attempt + 1))
                continue
            break
    return AIResponse(
        model=model, query=prompt, completion="",
        elapsed_s=time.time() - t_start, error=last_err,
    )


def ask_all(prompt: str, models: Iterable[str] = MODELS, **kw) -> list[AIResponse]:
    """Fan out the same prompt across multiple models in parallel."""
    models = list(models)
    with concurrent.futures.ThreadPoolExecutor(max_workers=len(models)) as ex:
        futs = {ex.submit(ask, prompt, m, **kw): m for m in models}
        out: list[AIResponse] = []
        for f in concurrent.futures.as_completed(futs):
            out.append(f.result())
    # Preserve requested order
    by_model = {r.model: r for r in out}
    return [by_model[m] for m in models if m in by_model]
