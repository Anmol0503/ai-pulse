"""Fetch every source at once and turn whatever it returns into Items.

Eight kinds of source, one function each:
  feed          RSS / Atom: blogs, YouTube channels, GitHub releases, Reddit
  hn            Hacker News, through Algolia's public search API
  hf_papers     Hugging Face Daily Papers (JSON, with upvotes)
  sitemap       sites with no feed: /sitemap.xml lists every page with a "lastmod" date
  md_changelog  API changelogs published as Markdown (Claude, OpenAI, Gemini)
  hf_models     newly released open-weight models trending on Hugging Face, with sizes
  openrouter    every model newly listed on OpenRouter, with prices and context windows
  gh_trending   github.com/trending: the repos on every "#1 on GitHub" reel, with stars gained

A source that fails never sinks the pull: fetch_all() reports the error and carries on.
"""

from __future__ import annotations

import asyncio
import hashlib
import html
import json
import re
import zlib
from collections.abc import Callable
from datetime import datetime, timedelta, timezone
from html.parser import HTMLParser
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit
from xml.etree import ElementTree as ET

import feedparser
import httpx

from .models import Item
from .sources import Source

UA = "Mozilla/5.0 (Macintosh; Apple Silicon) AI-Pulse/0.1 (personal AI news reader)"
TIMEOUT = httpx.Timeout(20.0, connect=10.0)
SOURCE_DEADLINE = 30  # seconds one source may take, all its requests included
MAX_BYTES = 3_000_000  # never read more than this from one response

# ---------------------------------------------------------------- small helpers


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def canonical(url: str) -> str:
    """Normalise a URL so the same page reached two ways gets one id.

    Drops tracking parameters, fragments, a trailing slash and "www.".
    Keeps every other query parameter, because some are the identity
    (YouTube's ?v=..., HN's ?id=...).
    """
    parts = urlsplit(url.strip())
    query = [
        (k, v)
        for k, v in parse_qsl(parts.query, keep_blank_values=True)
        if not k.lower().startswith("utm_") and k.lower() not in {"ref", "ref_src", "fbclid", "gclid"}
    ]
    host = parts.netloc.lower().removeprefix("www.")
    path = parts.path.rstrip("/") or "/"
    return urlunsplit((parts.scheme.lower(), host, path, urlencode(query), ""))


def item_id(url: str) -> str:
    return hashlib.sha1(canonical(url).encode()).hexdigest()[:16]


def clean(text: str) -> str:
    """Unescape entities and squash whitespace — for titles and one-liners."""
    return re.sub(r"\s+", " ", html.unescape(text or "")).strip()


def parse_date(value: str | None) -> datetime | None:
    """ISO-8601 in, aware UTC datetime out; None when absent or unparseable."""
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt.astimezone(timezone.utc)


def _struct_to_dt(st) -> datetime | None:
    """feedparser gives dates as time.struct_time, already in UTC."""
    return datetime(*st[:6], tzinfo=timezone.utc) if st else None


def is_http(url: str) -> bool:
    return url.startswith(("http://", "https://"))


# ---------------------------------------------------------------- HTML -> text


class _TextExtractor(HTMLParser):
    SKIP = {"script", "style", "noscript", "svg", "nav", "header", "footer", "aside", "form", "button", "iframe", "template"}
    BLOCK = {"p", "div", "br", "li", "h1", "h2", "h3", "h4", "h5", "h6", "tr", "pre", "blockquote", "section", "article", "figcaption"}
    PROSE = {"p", "li", "h1", "h2", "h3", "h4", "pre", "blockquote", "figcaption", "td"}

    def __init__(self, prose_only: bool = False) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.skip_depth = 0
        self.prose_only = prose_only
        self.prose_depth = 0

    def handle_starttag(self, tag, attrs):
        if tag in self.SKIP:
            self.skip_depth += 1
        elif tag in self.BLOCK:
            self.parts.append("\n")
        if tag in self.PROSE:
            self.prose_depth += 1

    def handle_endtag(self, tag):
        if tag in self.SKIP:
            self.skip_depth = max(0, self.skip_depth - 1)
        elif tag in self.BLOCK:
            self.parts.append("\n")
        if tag in self.PROSE:
            self.prose_depth = max(0, self.prose_depth - 1)

    def handle_data(self, data):
        if not self.skip_depth and (not self.prose_only or self.prose_depth):
            self.parts.append(data)


def _extract(fragment: str, prose_only: bool = False) -> str:
    parser = _TextExtractor(prose_only)
    parser.feed(fragment)
    parser.close()
    lines = (re.sub(r"[ \t\r\f\v]+", " ", ln).strip() for ln in "".join(parser.parts).split("\n"))
    return re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip()


def html_to_text(raw: str, limit: int = 12_000) -> str:
    """Readable text from an HTML page or fragment.

    First choice: only the text inside paragraphs, list items and headings. Posts are
    written in those; menus, widgets and "related models" cards mostly aren't. (Trusting
    <article> read a Hugging Face post's sidebar of model ads instead of the post.)
    If that finds little, fall back to the <article> or <main> element, then the page.
    Uses find(), not a regex across the document, so a huge page stays linear.
    """
    if not raw:
        return ""
    prose = _extract(raw, prose_only=True)
    if len(prose) >= 600:
        return prose[:limit]
    low = raw.lower()
    for tag in ("article", "main"):
        start, end = low.find(f"<{tag}"), low.rfind(f"</{tag}>")
        if 0 <= start < end:
            text = _extract(raw[start : end + len(tag) + 3])
            if len(text) >= 500:
                return text[:limit]
    return _extract(raw)[:limit]


class _HeadMeta(HTMLParser):
    """Collects <title> and <meta> tags from a page's <head>."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.meta: dict[str, str] = {}
        self.title = ""
        self._in_title = False

    def handle_starttag(self, tag, attrs):
        a = {k.lower(): (v or "") for k, v in attrs}
        if tag == "meta":
            key = (a.get("property") or a.get("name") or "").lower()
            if key and "content" in a:
                self.meta.setdefault(key, a["content"])
        elif tag == "title":
            self._in_title = True

    def handle_endtag(self, tag):
        if tag == "title":
            self._in_title = False

    def handle_data(self, data):
        if self._in_title:
            self.title += data


def head_meta(raw: str) -> _HeadMeta:
    end = raw.lower().find("</head>")
    parser = _HeadMeta()
    parser.feed(raw[: end if end > 0 else 200_000])
    return parser


# ---------------------------------------------------------------- HTTP


async def get(client: httpx.AsyncClient, url: str, max_bytes: int = MAX_BYTES) -> tuple[bytes, str]:
    """GET a URL, following redirects, reading at most max_bytes. Returns (body, content-type).

    Decompresses by hand, with a cap on every step: httpx would inflate a gzip in one go,
    so a 300-byte "gzip of a gzip of a gzip" from some page linked on HN could ask for
    gigabytes before any byte limit got a look.
    """
    async with client.stream("GET", url, headers={"Accept-Encoding": "gzip, deflate"}) as r:
        r.raise_for_status()
        codings = [c.strip().lower() for c in r.headers.get("content-encoding", "").split(",")]
        codings = [c for c in codings if c and c != "identity"]
        if len(codings) > 1 or (codings and codings[0] not in ("gzip", "x-gzip", "deflate")):
            raise ValueError(f"unsupported Content-Encoding: {', '.join(codings)}")
        inflate = zlib.decompressobj(32 + zlib.MAX_WBITS) if codings else None  # 32+: gzip or zlib header
        body = bytearray()
        async for raw in r.aiter_raw():
            body.extend(inflate.decompress(raw, max_bytes - len(body)) if inflate else raw)
            if len(body) >= max_bytes:
                break
        return bytes(body[:max_bytes]), r.headers.get("content-type", "")


def new_client() -> httpx.AsyncClient:
    return httpx.AsyncClient(timeout=TIMEOUT, follow_redirects=True, headers={"User-Agent": UA})


# ---------------------------------------------------------------- feeds (RSS / Atom)


def _lead_with(text: str, pattern: str, limit: int = 1_500) -> str:
    """Put the lines matching `pattern` first, e.g. "Added ..." lines from release notes full of "Fixed ..."."""
    lines = [ln for ln in text.splitlines() if ln.strip()]
    rx = re.compile(pattern, re.IGNORECASE)
    hits = [ln for ln in lines if rx.match(ln.lstrip("-*• ").strip())]
    rest = [ln for ln in lines if ln not in hits]
    return "\n".join(hits + rest)[:limit]


async def fetch_feed(client: httpx.AsyncClient, src: Source, since: datetime) -> list[Item]:
    body, _ = await get(client, src.url, src.max_bytes)
    parsed = feedparser.parse(body)
    if not parsed.entries and parsed.get("bozo"):
        raise ValueError(f"not a readable feed ({parsed.get('bozo_exception')})")
    skip = {c.lower() for c in src.skip_categories}
    skip_title = re.compile(src.skip_titles) if src.skip_titles else None
    link_count: dict[str, int] = {}
    for e in parsed.entries:
        link_count[canonical(e.get("link") or "")] = link_count.get(canonical(e.get("link") or ""), 0) + 1
    items = []
    for rank, e in enumerate(parsed.entries):
        link = e.get("link") or ""
        if not is_http(link) or "/shorts/" in link:
            continue
        if skip and {t.get("term", "").lower() for t in e.get("tags", [])} & skip:
            continue
        if skip_title and skip_title.search(e.get("title", "")):
            continue
        content = ""
        if e.get("content"):
            content = html_to_text(e.content[0].get("value", ""))
        # YouTube puts the video description in media:description
        summary_html = e.get("summary") or e.get("media_description") or ""
        summary = html_to_text(summary_html)
        if not content and len(summary) > 1_500:  # some feeds (Simon Willison's) ship the whole post as the summary
            content = summary
        if src.prefer_lines:
            summary = _lead_with(content or summary, src.prefer_lines)
        title = clean(e.get("title", "")) or link
        page = canonical(link)
        # Some feeds point several posts at one page (Hamel's cross-posts all link to hamel.dev/).
        # Then the link alone can't tell them apart: add the title, or later posts look "seen".
        shared = link_count.get(page, 0) > 1 or urlsplit(page).path == "/"
        items.append(
            Item(
                id=hashlib.sha1(f"{page}|{title}".encode()).hexdigest()[:16] if shared else item_id(link),
                source=src.name,
                title=title,
                url=link,
                published=_struct_to_dt(e.get("published_parsed") or e.get("updated_parsed")),
                summary=summary[:1_500],
                content=content,
                # a ranked feed's order is its crowd signal; an unlabelled signal is never shown on the page
                signal=len(parsed.entries) - rank if src.ranked else None,
            )
        )
    return items


# ---------------------------------------------------------------- Hacker News

# A story is kept when its title looks like AI. The list errs on the side of letting
# things in: triage drops the stragglers for a fraction of a cent.
AI_TITLE = re.compile(
    r"\b(LLMs?|GPT-?[\w.]*|ChatGPT|OpenAI|Anthropic|Claude|Opus|Sonnet|Haiku|Gemini|Gemma|DeepMind|Llama|llama\.cpp|"
    r"Mistral|Ollama|Qwen|DeepSeek|Kimi|Grok|Copilot|Codex|Cursor|Windsurf|Perplexity|Hugging ?Face|vLLM|MLX|GGUF|"
    r"LangChain|LangGraph|chatbots?|agentic|agents?|machine learning|deep learning|neural|language models?|transformers?|"
    r"diffusion|FLUX|embeddings?|fine-?tun\w*|inference|prompts?|prompting|vibe[- ]cod\w*|reasoning models?|"
    r"reinforcement learning|RLHF|LLM-as-a-judge|evals?|vector (?:database|search|DB)s?|open-?weights?|"
    r"frontier models?|superintelligence|artificial intelligence)\b",
    re.IGNORECASE,
)
AI_ACRONYM = re.compile(r"\b(AI|ML|RAG|MCP|AGI|GLM|xAI|RL)\b")  # case-sensitive: "AI", never "ai" in "kaiju"
AI_DOMAINS = ("openai.com", "anthropic.com", "claude.com", "claude.dev", "huggingface.co", "deepmind",
              "ollama.com", "simonwillison.net", "mistral.ai", "arxiv.org")


def looks_like_ai(title: str, url: str) -> bool:
    return bool(AI_TITLE.search(title) or AI_ACRONYM.search(title) or any(d in url for d in AI_DOMAINS))


async def fetch_hn(client: httpx.AsyncClient, src: Source, since: datetime) -> list[Item]:
    # /search with no query ranks by points, so the 1,000-hit ceiling keeps the best stories of
    # the whole window. /search_by_date kept the NEWEST 1,000 and lost the start of a long gap.
    url = (
        "https://hn.algolia.com/api/v1/search?tags=story&hitsPerPage=1000"
        f"&numericFilters=points%3E%3D{src.min_points},created_at_i%3E{int(since.timestamp())}"
    )
    body, _ = await get(client, url)
    data = json.loads(body)
    items = []
    for hit in data.get("hits", []):
        title = clean(hit.get("title") or "")
        hn_page = f"https://news.ycombinator.com/item?id={hit['objectID']}"
        link = hit.get("url") or hn_page
        points = hit.get("points") or 0
        if not title or not is_http(link):
            continue
        if not looks_like_ai(title, link) and not (src.any_topic_points and points >= src.any_topic_points):
            continue
        items.append(
            Item(
                id=item_id(link),
                source=src.name,
                title=title,
                url=link,
                published=datetime.fromtimestamp(hit["created_at_i"], tz=timezone.utc),
                summary=html_to_text(hit.get("story_text") or "", limit=1500),
                signal=points,
                signal_label="points",
                discussion_url=hn_page,
            )
        )
    return items


# ---------------------------------------------------------------- Hugging Face papers


async def fetch_hf_papers(client: httpx.AsyncClient, src: Source, since: datetime) -> list[Item]:
    """One request per day in the window (at most 8), then the best by upvotes."""
    today = utcnow().date()
    first = max(since.date(), today - timedelta(days=7))
    days = [first + timedelta(days=n) for n in range((today - first).days + 1)]

    async def one_day(day):
        body, _ = await get(client, f"{src.url}?date={day.isoformat()}")
        return day, json.loads(body)

    by_id: dict[str, Item] = {}
    for result in await asyncio.gather(*(one_day(d) for d in days), return_exceptions=True):
        if isinstance(result, Exception):
            continue  # one missing day is fine; the whole source failing is caught below
        day, rows = result
        featured = datetime(day.year, day.month, day.day, 12, tzinfo=timezone.utc)
        for row in rows:
            paper = row.get("paper") or {}
            pid = paper.get("id")
            if not pid:
                continue
            link = f"https://huggingface.co/papers/{pid}"
            item = Item(
                id=item_id(link),
                source=src.name,
                title=clean(paper.get("title") or row.get("title") or pid),
                url=link,
                # the day HF featured it, not the arXiv date (often a week earlier): "new to you" is what counts
                published=parse_date(paper.get("submittedOnDailyAt")) or featured,
                summary=clean(paper.get("summary") or row.get("summary") or "")[:2500],
                signal=paper.get("upvotes"),
                signal_label="upvotes",
            )
            by_id[item.id] = item
    if not by_id:
        raise ValueError("no papers returned for any day in the window")
    return list(by_id.values())


# ---------------------------------------------------------------- sitemaps

_SM = "{http://www.sitemaps.org/schemas/sitemap/0.9}"


async def _page_item(client: httpx.AsyncClient, src: Source, loc: str, lastmod: datetime | None) -> Item:
    body, ctype = await get(client, loc)
    raw = body.decode("utf-8", errors="replace")
    head = head_meta(raw)
    title = head.meta.get("og:title") or head.title or loc
    published = parse_date(head.meta.get("article:published_time")) or lastmod
    summary = head.meta.get("og:description") or head.meta.get("description") or ""
    return Item(
        id=item_id(loc),
        source=src.name,
        title=clean(title),
        url=loc,
        published=published,
        summary=clean(summary),
        content=html_to_text(raw),
    )


async def fetch_sitemap(client: httpx.AsyncClient, src: Source, since: datetime) -> list[Item]:
    body, _ = await get(client, src.url, src.max_bytes)
    root = ET.fromstring(body)
    if root.tag == f"{_SM}sitemapindex":  # an index of sitemaps: follow the matching children
        children = [s.findtext(f"{_SM}loc", "").strip() for s in root.iter(f"{_SM}sitemap")]
        children = [c for c in children if not src.sitemap_hint or src.sitemap_hint in c][:5]
        roots = []
        for child in children:
            child_body, _ = await get(client, child, src.max_bytes)
            roots.append(ET.fromstring(child_body))
    else:
        roots = [root]

    fresh: list[tuple[str, datetime]] = []
    for r in roots:
        for u in r.iter(f"{_SM}url"):
            loc = (u.findtext(f"{_SM}loc") or "").strip()
            lastmod = parse_date(u.findtext(f"{_SM}lastmod"))
            if not is_http(loc) or (src.path_prefix and src.path_prefix not in loc):
                continue
            if lastmod and lastmod >= since:
                fresh.append((loc, lastmod))
    fresh.sort(key=lambda pair: pair[1], reverse=True)
    pages = await asyncio.gather(*(_page_item(client, src, loc, lm) for loc, lm in fresh[: src.cap * 2]), return_exceptions=True)
    return [p for p in pages if isinstance(p, Item)]


# ---------------------------------------------------------------- Markdown changelogs

_MONTHS = {m: i for i, m in enumerate(
    ["january", "february", "march", "april", "may", "june", "july", "august", "september", "october", "november", "december"], 1)}
_MONTHS.update({m[:3]: i for m, i in list(_MONTHS.items())})
_MONTHS["sept"] = 9

_HEADING = re.compile(r"^(#{1,4})\s+(.+?)\s*#*\s*$")
_FULL_DATE = re.compile(r"^([A-Za-z]{3,9})\.?\s+(\d{1,2}),?\s+(\d{4})$")  # October 7, 2026
_MONTH_YEAR = re.compile(r"^([A-Za-z]{3,9}),?\s+(\d{4})$")  # October, 2026  (sets the year for what follows)
_MONTH_DAY = re.compile(r"^([A-Za-z]{3,9})\.?\s+(\d{1,2})$")  # Oct 7  (year taken from the heading above)
_BULLET = re.compile(r"^[*-]\s+")
_MD_LINK = re.compile(r"\[([^\]]*)\]\((https?://[^)\s]+)\)")
_BADGE = re.compile(r"^(Feature|Update|Fix|Deprecation|Breaking|Announcement|Change|Removal|Beta)(\s+·.*)?$")  # "Feature · Model: x"
_BOLD_LEAD = re.compile(r"^\s*(?:[*-]\s+)?\*\*(.+?)\*\*")  # Gemini: "- **Title** : body"


def _md_plain(text: str) -> str:
    """Markdown to plain text: links become their words, emphasis and code marks go."""
    text = _MD_LINK.sub(r"\1", text)
    return clean(re.sub(r"[*_`]+", "", text))


def _md_date(text: str, year: int | None) -> datetime | None:
    month = day = None
    if m := _FULL_DATE.match(text):
        month, day, year = _MONTHS.get(m[1].lower()), int(m[2]), int(m[3])
    elif (m := _MONTH_DAY.match(text)) and year:
        month, day = _MONTHS.get(m[1].lower()), int(m[2])
    if not month:
        return None
    try:
        return datetime(year, month, day, 12, tzinfo=timezone.utc)
    except ValueError:
        return None


def parse_md_changelog(md: str) -> list[tuple[datetime, str, str]]:
    """Split a dated Markdown changelog into (date, heading, entry text) — one per entry.

    Handles the three formats in use:
      ### October 7, 2026  then "* bullet" entries               (Claude)
      ## October 6, 2026   then "- **Title** : body" entries      (Gemini)
      ## October, 2026     then "### Oct 6" + a paragraph each    (OpenAI)
    Scans line by line: no regex runs across the whole document.
    """
    sections: list[tuple[datetime, str, int, list[str]]] = []
    year: int | None = None
    current = None
    for line in md.splitlines():
        if m := _HEADING.match(line):
            level, text = len(m[1]), _md_plain(m[2])
            if ym := _MONTH_YEAR.match(text):
                year, current = int(ym[2]), None
                continue
            if date := _md_date(text, year):
                current = (date, text, level, [])
                sections.append(current)
                continue
            if current and level <= current[2]:
                current = None  # an undated heading at the same level ends the dated section
            continue
        if current:
            current[3].append(line)

    entries = []
    for date, heading, _, lines in sections:
        blocks: list[list[str]] = []
        for line in lines:
            if _BULLET.match(line):  # a top-level bullet starts a new entry
                blocks.append([line])
            elif blocks and (line.startswith((" ", "\t")) or not line.strip()):
                blocks[-1].append(line)  # continuation of the current bullet
            elif blocks and not _BULLET.match(blocks[-1][0]):
                blocks[-1].append(line)  # prose continues a prose entry
            else:
                blocks.append([line])  # prose entry (OpenAI style)
        for block in blocks:
            text = "\n".join(block).strip()
            if text:
                entries.append((date, heading, text))
    return entries


async def fetch_md_changelog(client: httpx.AsyncClient, src: Source, since: datetime) -> list[Item]:
    body, _ = await get(client, src.url, src.max_bytes)
    md = body.decode("utf-8", errors="replace")
    homepage = src.homepage or src.url
    items = []
    for date, heading, text in parse_md_changelog(md):
        if date < since - timedelta(days=1):
            continue
        prose = [ln for ln in text.splitlines() if ln.strip() and not _BADGE.match(_md_plain(ln))]
        if bold := _BOLD_LEAD.match(prose[0] if prose else text):
            title = _md_plain(bold[1])[:180]
        else:
            first = _md_plain(_BULLET.sub("", " ".join(prose) if prose else text))
            title = re.split(r"(?<=[.!?])\s", first, maxsplit=1)[0][:180]
        links = [u for _, u in _MD_LINK.findall(text)]
        anchor = re.sub(r"[^a-z0-9]+", "-", heading.lower()).strip("-")
        items.append(
            Item(
                # one id per entry's text (if a lab edits an entry, you see the new version once). Hashed
                # directly: item_id() would canonicalise the URL and drop the #anchor, merging every entry.
                id=hashlib.sha1(f"{homepage}|{anchor}|{_md_plain(text)}".encode()).hexdigest()[:16],  # a moved link isn't news
                source=src.name,
                title=title or f"{src.name}, {heading}",
                # link to the docs page the entry points at: that's where the detail is
                url=links[0] if links else f"{homepage}#{anchor}",
                published=date,
                summary=_md_plain(text)[:1_500],
                content=_md_plain(text),
            )
        )
    return items


# ---------------------------------------------------------------- new open-weight models

_DERIVATIVE = re.compile(r"uncensor|abliterat|obliterat|heretic|gguf|mlx|exl\d|awq|gptq|\dbit|bpw", re.IGNORECASE)
_MODEL_TASKS = {"text-generation", "image-text-to-text", "any-to-any", "feature-extraction", "sentence-similarity"}


def _is_original_release(m: dict) -> bool:
    """Trending lists are mostly re-quantised copies and fine-tunes. Keep the originals."""
    if m.get("pipeline_tag") not in _MODEL_TASKS or _DERIVATIVE.search(m.get("id", "")):
        return False
    author = (m.get("author") or m.get("id", "/").split("/")[0]).lower()
    for tag in m.get("tags") or []:
        kind, _, base = tag.partition(":")[2].partition(":") if tag.startswith("base_model:") else ("", "", "")
        if kind in {"quantized", "merge", "adapter"} and base.split("/")[0].lower() != author:
            return False
        if kind == "finetune" and (m.get("likes") or 0) < 200:
            return False
    return True


def _model_size(m: dict) -> str:
    params = (m.get("safetensors") or {}).get("total") or (m.get("gguf") or {}).get("total")
    if not params:
        return "size not listed"
    b = params / 1e9
    gb = b * 0.6  # ~0.6 bytes per parameter at 4-bit (Q4_K_M), the usual way to run a model on a Mac
    return f"{b:.1f}B parameters, about {gb:.1f} GB of memory at 4-bit" if gb < 10 else \
        f"{b:.1f}B parameters, about {gb:.0f} GB of memory at 4-bit"


async def fetch_hf_models(client: httpx.AsyncClient, src: Source, since: datetime) -> list[Item]:
    body, _ = await get(client, src.url, src.max_bytes)
    # Trending is a ranking, not a stream: a model made public last week may have been created
    # (privately) a month earlier. 45 days catches those; the seen-list stops repeats.
    recent = utcnow() - timedelta(days=45)
    items = []
    for m in json.loads(body):
        created = parse_date(m.get("createdAt"))
        if not created or created < recent or not _is_original_release(m):
            continue
        mid = m["id"]
        url = f"https://huggingface.co/{mid}"
        licence = next((t.split(":", 1)[1] for t in m.get("tags") or [] if t.startswith("license:")), "licence not listed")
        items.append(
            Item(
                id=item_id(url),
                source=src.name,
                title=f"{mid}: new open {m.get('pipeline_tag')} model ({_model_size(m)})",
                url=url,
                published=None,  # see `recent` above: the window is applied here, not by date
                summary=f"{mid} by {m.get('author') or mid.split('/')[0]}. Task: {m.get('pipeline_tag')}. "
                f"{_model_size(m)}. Licence: {licence}. Created {created.date().isoformat()}. {m.get('likes', 0)} likes.",
                signal=m.get("likes"),
                signal_label="likes",
            )
        )
    return items


# ---------------------------------------------------------------- OpenRouter new models


def _per_million(price) -> str:
    try:
        p = float(price) * 1e6
    except (TypeError, ValueError):
        return "?"
    return "free" if p == 0 else f"${p:.2f}"


async def fetch_openrouter(client: httpx.AsyncClient, src: Source, since: datetime) -> list[Item]:
    body, _ = await get(client, src.url, src.max_bytes)
    models = json.loads(body).get("data", [])
    free_routes = {m["id"].split(":")[0] for m in models if m.get("id", "").endswith(":free")}
    listed = {m.get("id", "") for m in models}
    items = []
    for m in models:
        mid = m.get("id", "")
        if not mid or mid.startswith("~"):  # "~" ids are aliases for "the latest" of a family
            continue
        if ":" in mid and mid.split(":")[0] in listed:  # a :free / :batch route of a base model listed separately
            continue
        created = datetime.fromtimestamp(m["created"], tz=timezone.utc) if m.get("created") else None
        if not created or created < since:
            continue
        pricing = m.get("pricing") or {}
        price_in, price_out = _per_million(pricing.get("prompt")), _per_million(pricing.get("completion"))
        ctx = m.get("context_length") or 0
        facts = (f"Price per million tokens: {price_in} in, {price_out} out. Context window: {ctx:,} tokens."
                 + (" Also has a free route on OpenRouter." if mid in free_routes else ""))
        url = f"https://openrouter.ai/{mid}"
        items.append(
            Item(
                id=item_id(url),
                source=src.name,
                title=f"{m.get('name') or mid} is now callable ({price_in} in / {price_out} out per M tokens, {ctx // 1000}K context)",
                url=url,
                published=created,
                summary=f"{clean(m.get('description') or '')[:600]} {facts}".strip(),
            )
        )
    return items


# ---------------------------------------------------------------- GitHub Trending

# github.com/trending has no API, so we read the page. Each repo is one <article class="Box-row">.
_GH_ROW = re.compile(r'<article class="Box-row">(.*?)</article>', re.S)
_GH_NAME = re.compile(r'<h2[^>]*>.*?href="/([\w.-]+/[\w.-]+)"', re.S)
_GH_DESC = re.compile(r"<p\b[^>]*>(.*?)</p>", re.S)  # \b: not the <path> inside an icon
_GH_ICON = re.compile(r"<svg\b.*?</svg>", re.S)  # icons sit between a link and its number: drop them first
_GH_LANG = re.compile(r'itemprop="programmingLanguage">([^<]+)<')
_GH_COUNT = re.compile(r'href="/[\w.-]+/[\w.-]+/(stargazers|forks)"[^>]*>\s*([\d,]+)\s*<', re.S)
_GH_GAINED = re.compile(r"([\d,]+)\s+stars?\s+(today|this week|this month)")


def _num(text: str) -> int:
    return int(text.replace(",", ""))


async def fetch_gh_trending(client: httpx.AsyncClient, src: Source, since: datetime) -> list[Item]:
    body, _ = await get(client, src.url, src.max_bytes)
    page = body.decode("utf-8", "replace")
    rows = _GH_ROW.findall(page)
    if not rows:
        raise ValueError("no repos found: GitHub may have changed the trending page's layout")
    items = []
    for row in rows:
        row = _GH_ICON.sub("", row)
        name = _GH_NAME.search(row)
        if not name:
            continue
        repo = name.group(1)
        url = f"https://github.com/{repo}"
        desc = clean(html.unescape(re.sub(r"<[^>]+>", "", m.group(1)))) if (m := _GH_DESC.search(row)) else ""
        lang = clean(m.group(1)) if (m := _GH_LANG.search(row)) else ""
        counts = {kind: _num(n) for kind, n in _GH_COUNT.findall(row)}
        gained = _GH_GAINED.search(row)
        new_stars, period = (_num(gained.group(1)), gained.group(2)) if gained else (None, "")
        total = counts.get("stargazers", 0)
        # The hype check: a repo with 95% of its stars from today went viral; one with 2% is an old
        # favourite having a good day. Triage sees the share and can tell the two apart.
        share = f" ({new_stars / total:.0%} of all its stars)" if new_stars and total else ""
        facts = [desc if not desc or desc[-1] in ".!?)" else f"{desc}.", f"Language: {lang}." if lang else "",
                 f"{total:,} stars, {counts.get('forks', 0):,} forks.",
                 f"Gained {new_stars:,} stars {period}{share}." if new_stars else ""]
        items.append(
            Item(
                id=item_id(url),  # same id as any other source linking the repo, so they merge
                source=src.name,
                title=f"{repo}: {desc}" if desc else repo,
                url=url,
                published=None,  # a ranking, not a stream: the seen-list stops repeats, not a date
                summary=" ".join(f for f in facts if f),
                signal=new_stars,
                signal_label=f"stars {period}" if period else "",
            )
        )
    return items


FETCHERS = {
    "gh_trending": fetch_gh_trending,
    "feed": fetch_feed,
    "hn": fetch_hn,
    "hf_papers": fetch_hf_papers,
    "sitemap": fetch_sitemap,
    "md_changelog": fetch_md_changelog,
    "hf_models": fetch_hf_models,
    "openrouter": fetch_openrouter,
}


# ---------------------------------------------------------------- all at once


async def fetch_all(
    client: httpx.AsyncClient,
    sources: list[Source],
    since: datetime,
    on_source: Callable[[Source, int, str | None], None] = lambda *_: None,
) -> tuple[list[Item], dict[str, str]]:
    """Fetch every source concurrently. Returns (items, {source name: error})."""
    errors: dict[str, str] = {}

    async def one(src: Source) -> list[Item]:
        try:
            items = await asyncio.wait_for(FETCHERS[src.kind](client, src, since), SOURCE_DEADLINE)
        except Exception as exc:  # noqa: BLE001 — any failure is reported, never fatal
            errors[src.name] = describe_error(exc)
            on_source(src, 0, errors[src.name])
            return []
        # report what's in the window, not the size of the archive (OpenAI's feed is 1,000+ posts back to 2015)
        on_source(src, sum(1 for it in items if it.published is None or it.published >= since), None)
        return items

    batches = await asyncio.gather(*(one(s) for s in sources))
    return [item for batch in batches for item in batch], errors


def describe_error(exc: Exception) -> str:
    if isinstance(exc, (asyncio.TimeoutError, httpx.TimeoutException)):
        return "timed out"
    if isinstance(exc, httpx.HTTPStatusError):
        return f"HTTP {exc.response.status_code}"
    if isinstance(exc, httpx.ConnectError):
        return "could not connect"
    return f"{type(exc).__name__}: {exc}"[:200]


# ---------------------------------------------------------------- cheap context for triage


async def add_descriptions(client: httpx.AsyncClient, items: list[Item], parallel: int = 16) -> int:
    """Give title-only items (most Hacker News links) the page's own one-line description.

    Triage can't judge "Clef: open-weight decision models" from the title alone; the
    page's og:description usually says what it is. Reads only the top of each page.
    """
    thin = [it for it in items if len(it.summary) < 80 and len(it.content) < 80 and "news.ycombinator.com" not in it.url]
    gate = asyncio.Semaphore(parallel)

    async def one(it: Item) -> bool:
        async with gate:
            try:
                body, ctype = await asyncio.wait_for(get(client, it.url, 300_000), 6)  # a nicety: never worth a long wait
            except Exception:  # noqa: BLE001 — no description is fine
                return False
        if "html" not in ctype:
            return False
        meta = head_meta(body.decode("utf-8", errors="replace")).meta
        text = clean(meta.get("og:description") or meta.get("description") or meta.get("twitter:description") or "")
        if text and text.lower() != it.title.lower():
            it.summary = text[:600]
            return True
        return False

    return sum(await asyncio.gather(*(one(it) for it in thin)))


# ---------------------------------------------------------------- full text for "learn" items


_GITHUB_REPO = re.compile(r"^https?://(?:www\.)?github\.com/([\w.-]+)/([\w.-]+)/?$")
_HF_MODEL = re.compile(r"^https?://huggingface\.co/(?!papers/|blog/|datasets/|spaces/|docs/)([\w.-]+)/([\w.-]+)/?$")
_ARXIV = re.compile(r"arxiv\.org/(?:abs|pdf|html)/(\d{4}\.\d{4,5})")


async def _readable(client: httpx.AsyncClient, url: str, limit: int) -> str:
    readme = None
    if m := _GITHUB_REPO.match(url):
        readme = f"https://raw.githubusercontent.com/{m[1]}/{m[2]}/HEAD/README.md"
    elif m := _HF_MODEL.match(url):
        readme = f"https://huggingface.co/{m[1]}/raw/main/README.md"
    if readme:
        try:
            body, _ = await get(client, readme, 400_000)
            text = body.decode("utf-8", errors="replace")[:limit]
            if len(text.strip()) >= 200:
                return text
        except httpx.HTTPError:
            pass  # gated (Google's models need a login) or no README.md: read the page itself instead
    if m := _ARXIV.search(url):
        body, _ = await get(client, f"https://arxiv.org/abs/{m[1]}", 600_000)
        return clean(head_meta(body.decode("utf-8", errors="replace")).meta.get("citation_abstract", ""))[:limit]
    body, ctype = await get(client, url)
    if "html" in ctype or not ctype:
        return html_to_text(body.decode("utf-8", errors="replace"), limit)
    return ""


async def full_text(client: httpx.AsyncClient, item: Item, limit: int = 9_000) -> str:
    """The best text for explaining an item: what the source said, plus the page it points to."""
    own = item.content or item.summary
    if len(item.content) >= 1_500:
        return item.content[:limit]
    if "huggingface.co/papers/" in item.url and len(item.summary) >= 400:
        return item.summary[:limit]  # the abstract is exactly what we need
    try:
        fetched = await asyncio.wait_for(_readable(client, item.url, limit), 15)
    except Exception:  # noqa: BLE001 — a page we can't read just means a thinner explanation
        fetched = ""
    if not fetched.strip() or fetched.strip() in own:
        return own[:limit]
    return (f"{own}\n\n--- from {item.url} ---\n\n{fetched}" if own else fetched)[:limit]
