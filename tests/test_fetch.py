"""Parsing: each source kind against a canned response, through a fake HTTP transport."""

from __future__ import annotations

import asyncio
import json
from datetime import timedelta

import httpx

from pulse import fetch
from pulse.fetch import canonical, html_to_text, item_id, utcnow
from pulse.sources import Source


def run(coro):
    return asyncio.run(coro)


def client_for(routes: dict[str, tuple[int, str, str]]) -> httpx.AsyncClient:
    """routes: {url-prefix: (status, content-type, body)} — first matching prefix answers."""

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        for prefix, (status, ctype, body) in routes.items():
            if url.startswith(prefix):
                data = body if isinstance(body, bytes) else body.encode()
                headers = {"content-type": ctype}
                if isinstance(body, bytes) and body[:2] == b"\x1f\x8b":  # a gzip body: say so, like a server would
                    headers["content-encoding"] = "gzip"
                # stream=, not text=: a real response arrives as a stream, and get() reads it raw
                return httpx.Response(status, headers=headers, stream=httpx.ByteStream(data))
        return httpx.Response(404, stream=httpx.ByteStream(b"not found"))

    return httpx.AsyncClient(transport=httpx.MockTransport(handler), follow_redirects=True)


# ---------------------------------------------------------------- helpers


def test_canonical_drops_tracking_but_keeps_identity():
    assert canonical("https://www.Example.com/post/?utm_source=x&ref=hn#top") == "https://example.com/post"
    assert canonical("https://youtube.com/watch?v=abc&utm_medium=feed") == "https://youtube.com/watch?v=abc"
    assert item_id("https://example.com/a/") == item_id("https://www.example.com/a?utm_campaign=z")
    assert item_id("https://example.com/a") != item_id("https://example.com/b")


def test_html_to_text_prefers_the_article_and_drops_chrome():
    body = "<p>" + "Real sentence about attention. " * 30 + "</p>"
    page = f"<html><nav>Menu Login</nav><article><h1>Title</h1>{body}<script>evil()</script></article><footer>(c)</footer></html>"
    text = html_to_text(page)
    assert text.startswith("Title")
    assert "Real sentence about attention." in text
    for junk in ("Menu", "evil", "(c)"):
        assert junk not in text


# ---------------------------------------------------------------- feeds

ATOM = """<?xml version="1.0" encoding="utf-8"?>
<feed xmlns="http://www.w3.org/2005/Atom"><title>Blog</title>
<entry><title>Prompt caching, explained</title><link href="https://blog.example.com/caching"/>
<updated>2026-10-05T10:00:00Z</updated>
<summary type="html">&lt;p&gt;{long}&lt;/p&gt;</summary></entry>
<entry><title>Short note</title><link href="https://blog.example.com/note"/>
<updated>2026-10-06T10:00:00Z</updated><summary>Tiny.</summary></entry>
<entry><title>No link</title><updated>2026-10-06T10:00:00Z</updated></entry>
</feed>""".replace("{long}", "Cache the prefix. " * 120)


def test_feed_parses_entries_and_promotes_a_full_post_summary_to_content():
    src = Source("Blog", "feed", "https://blog.example.com/atom", "why")
    with_client = client_for({"https://blog.example.com/atom": (200, "application/atom+xml", ATOM)})

    async def go():
        async with with_client as c:
            return await fetch.fetch_feed(c, src, utcnow() - timedelta(days=30))

    items = run(go())
    assert [i.title for i in items] == ["Prompt caching, explained", "Short note"]  # linkless entry dropped
    long_one, short_one = items
    assert len(long_one.content) > 1_500 and len(long_one.summary) == 1_500
    assert short_one.content == "" and short_one.summary == "Tiny."
    assert long_one.published.isoformat() == "2026-10-05T10:00:00+00:00"


# ---------------------------------------------------------------- Hacker News


def test_hn_keeps_ai_titles_with_points_and_comment_links():
    hits = {"hits": [
        {"objectID": "1", "title": "Claude adds a new caching mode", "url": "https://anthropic.com/news/x", "points": 420, "created_at_i": 1_791_000_000},
        {"objectID": "2", "title": "Show HN: My woodworking bench", "url": "https://wood.example.com", "points": 500, "created_at_i": 1_791_000_000},
        {"objectID": "3", "title": "Ask HN: How do you evaluate LLM apps?", "url": None, "points": 200, "created_at_i": 1_791_000_000, "story_text": "<p>We tried evals</p>"},
    ]}
    src = Source("Hacker News", "hn", "https://hn.algolia.com/api/v1/search_by_date", "why", min_points=150)

    async def go():
        async with client_for({"https://hn.algolia.com/": (200, "application/json", json.dumps(hits))}) as c:
            return await fetch.fetch_hn(c, src, utcnow() - timedelta(days=7))

    items = run(go())
    assert [i.title for i in items] == ["Claude adds a new caching mode", "Ask HN: How do you evaluate LLM apps?"]
    claude, ask = items
    assert claude.signal == 420 and claude.signal_label == "points"
    assert claude.discussion_url == "https://news.ycombinator.com/item?id=1"
    assert ask.url == ask.discussion_url  # no external link: the HN page is the item
    assert ask.summary == "We tried evals"


# ---------------------------------------------------------------- Hugging Face papers


def test_hf_papers_uses_the_featured_date_and_upvotes():
    day = [{"paper": {"id": "2610.00001", "title": "Faster decoding", "summary": "We speed up decoding.",
                      "upvotes": 52, "publishedAt": "2026-09-20T00:00:00.000Z",
                      "submittedOnDailyAt": "2026-10-06T00:00:00.000Z"}}]
    src = Source("HF Papers", "hf_papers", "https://huggingface.co/api/daily_papers", "why")

    async def go():
        async with client_for({"https://huggingface.co/api/daily_papers": (200, "application/json", json.dumps(day))}) as c:
            return await fetch.fetch_hf_papers(c, src, utcnow() - timedelta(days=3))

    items = run(go())
    assert len(items) == 1  # the same paper on every queried day collapses to one item
    paper = items[0]
    assert paper.url == "https://huggingface.co/papers/2610.00001"
    assert paper.signal == 52 and paper.signal_label == "upvotes"
    assert paper.published.date().isoformat() == "2026-10-06"  # featured date, not the arXiv date


# ---------------------------------------------------------------- sitemaps


def test_sitemap_reads_recent_pages_matching_the_prefix():
    now = utcnow()
    recent = (now - timedelta(days=1)).strftime("%Y-%m-%dT%H:%M:%SZ")
    old = (now - timedelta(days=90)).strftime("%Y-%m-%d")
    sitemap = f"""<?xml version="1.0" encoding="UTF-8"?>
<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
  <url><loc>https://lab.example.com/news/new-model</loc><lastmod>{recent}</lastmod></url>
  <url><loc>https://lab.example.com/news/ancient</loc><lastmod>{old}</lastmod></url>
  <url><loc>https://lab.example.com/careers/job</loc><lastmod>{recent}</lastmod></url>
</urlset>"""
    page = ('<html><head><title>Lab | New model</title><meta property="og:title" content="Introducing New Model">'
            '<meta name="description" content="A smaller, faster model."></head>'
            "<body><article>" + "<p>Details about the model. </p>" * 40 + "</article></body></html>")
    src = Source("Lab", "sitemap", "https://lab.example.com/sitemap.xml", "why", path_prefix="/news/")

    async def go():
        async with client_for({
            "https://lab.example.com/sitemap.xml": (200, "application/xml", sitemap),
            "https://lab.example.com/news/new-model": (200, "text/html", page),
        }) as c:
            return await fetch.fetch_sitemap(c, src, now - timedelta(days=7))

    items = run(go())
    assert [i.title for i in items] == ["Introducing New Model"]
    assert items[0].summary == "A smaller, faster model."
    assert "Details about the model." in items[0].content


# ---------------------------------------------------------------- isolation


def test_one_broken_source_does_not_sink_the_others():
    good = Source("Good", "feed", "https://good.example.com/atom", "why")
    bad = Source("Bad", "feed", "https://bad.example.com/atom", "why")
    seen = []

    async def go():
        async with client_for({
            "https://good.example.com/": (200, "application/atom+xml", ATOM),
            "https://bad.example.com/": (503, "text/plain", "down"),
        }) as c:
            return await fetch.fetch_all(c, [good, bad], utcnow() - timedelta(days=30),
                                         lambda src, n, err: seen.append((src.name, n, err)))

    items, errors = run(go())
    assert len(items) == 2
    assert errors == {"Bad": "HTTP 503"}
    assert ("Bad", 0, "HTTP 503") in seen
    assert ("Good", 2, None) in seen  # the count reported is what's inside the window


# ---------------------------------------------------------------- Markdown changelogs

CLAUDE_MD = """---
title: Release notes
---
# Claude Platform release notes

### October 7, 2026

* We've launched **Claude Haiku 5.5** (`claude-haiku-5-5`). See [models](https://docs.example.com/models).
* Code written for Claude Haiku 4.5 can break on Claude Haiku 5.5.
  Manual extended thinking is no longer supported.

### September 1, 2026

* An old change.
"""

OPENAI_MD = """# Changelog

## October, 2026

### Oct 6

Feature · Model: gpt-6-luna · API: v1/decisions

Released the [Decisions API](https://developers.openai.com/api/docs/guides/decisions) in beta. It is fast.

### Oct 6

Update

Simplified API usage tiers from five to three.

## September, 2026

### Sep 29

Feature

Added computer use to the Agents API.
"""

GEMINI_MD = """## October 6, 2026

- **Gemini Nano Banana 2.1 generally available (GA)** : Released
  [Gemini Nano Banana 2.1](https://ai.google.dev/models/nano), an image model.

  See the guide to get started.
- **Deprecation announcement** : The `gemini-3.1-flash-image` model is
  deprecated.
"""


def test_changelog_parser_handles_all_three_formats():
    from pulse.fetch import parse_md_changelog

    claude = parse_md_changelog(CLAUDE_MD)
    assert [(d.date().isoformat(), t.split("\n")[0][:30]) for d, _, t in claude][:2] == [
        ("2026-10-07", "* We've launched **Claude Haik"), ("2026-10-07", "* Code written for Claude Haik")]
    assert "Manual extended thinking" in claude[1][2]  # an indented line continues its bullet

    openai = parse_md_changelog(OPENAI_MD)
    assert [d.date().isoformat() for d, _, _ in openai] == ["2026-10-06", "2026-10-06", "2026-09-29"]  # year from "## October, 2026"
    assert openai[0][2].startswith("Feature · Model: gpt-6-luna")

    gemini = parse_md_changelog(GEMINI_MD)
    assert len(gemini) == 2 and "See the guide" in gemini[0][2]  # a blank line inside a bullet doesn't split it


def test_changelog_items_link_to_the_docs_they_mention_and_get_readable_titles():
    src = Source("OpenAI API changelog", "md_changelog", "https://dev.example.com/changelog.md", "why",
                 homepage="https://dev.example.com/changelog")
    gem = Source("Gemini API changelog", "md_changelog", "https://ai.example.com/changelog.md.txt", "why",
                 homepage="https://ai.example.com/changelog")

    async def go(source, body):
        async with client_for({source.url: (200, "text/markdown", body)}) as c:
            return await fetch.fetch_md_changelog(c, source, utcnow() - timedelta(days=4000))

    openai = run(go(src, OPENAI_MD))
    assert openai[0].title == "Released the Decisions API in beta."  # badge line skipped, first sentence kept
    assert openai[0].url == "https://developers.openai.com/api/docs/guides/decisions"
    assert openai[1].url == "https://dev.example.com/changelog#oct-6"  # no link in the entry: the changelog itself
    assert len({i.id for i in openai}) == 3  # two entries on one date still get distinct ids
    gemini = run(go(gem, GEMINI_MD))
    assert gemini[0].title == "Gemini Nano Banana 2.1 generally available (GA)"  # the bold lead is the title


# ---------------------------------------------------------------- open models


def test_hf_models_keeps_original_recent_releases_with_sizes():
    fresh = (utcnow() - timedelta(days=3)).isoformat()
    old = (utcnow() - timedelta(days=400)).isoformat()
    models = [
        {"id": "Qwen/Qwen9-8B", "author": "Qwen", "createdAt": fresh, "pipeline_tag": "text-generation", "likes": 900,
         "safetensors": {"total": 8_200_000_000}, "tags": ["license:apache-2.0"]},
        {"id": "someone/Qwen9-8B-GGUF", "author": "someone", "createdAt": fresh, "pipeline_tag": "text-generation", "likes": 50},
        {"id": "someone/Qwen9-8B-abliterated", "author": "someone", "createdAt": fresh, "pipeline_tag": "text-generation", "likes": 80},
        {"id": "x/quant", "author": "x", "createdAt": fresh, "pipeline_tag": "text-generation", "likes": 10,
         "tags": ["base_model:quantized:Qwen/Qwen9-8B"]},
        {"id": "meta-llama/Old-3", "author": "meta-llama", "createdAt": old, "pipeline_tag": "text-generation", "likes": 9000},
        {"id": "a/diffuser", "author": "a", "createdAt": fresh, "pipeline_tag": "text-to-image", "likes": 700},
    ]
    src = Source("HF models", "hf_models", "https://huggingface.co/api/models?sort=trendingScore", "why")

    async def go():
        async with client_for({"https://huggingface.co/api/models": (200, "application/json", json.dumps(models))}) as c:
            return await fetch.fetch_hf_models(c, src, utcnow() - timedelta(days=7))

    items = run(go())
    assert [i.url for i in items] == ["https://huggingface.co/Qwen/Qwen9-8B"]
    assert "8.2B parameters, about 4.9 GB of memory at 4-bit" in items[0].title
    assert "apache-2.0" in items[0].summary


def test_openrouter_lists_new_base_models_with_price_and_free_routes():
    now = int(utcnow().timestamp())
    data = {"data": [
        {"id": "mistralai/large-4", "name": "Mistral: Large 4", "created": now - 3600, "context_length": 524288,
         "pricing": {"prompt": "0.00000068", "completion": "0.00000209"}, "description": "A big model."},
        {"id": "mistralai/large-4:free", "name": "free variant", "created": now - 3600, "pricing": {"prompt": "0", "completion": "0"}},
        {"id": "~openai/latest", "name": "alias", "created": now - 3600},
        {"id": "old/model", "name": "Old", "created": now - 90 * 86400},
    ]}
    src = Source("OpenRouter", "openrouter", "https://openrouter.ai/api/v1/models", "why")

    async def go():
        async with client_for({"https://openrouter.ai/": (200, "application/json", json.dumps(data))}) as c:
            return await fetch.fetch_openrouter(c, src, utcnow() - timedelta(days=7))

    items = run(go())
    assert [i.url for i in items] == ["https://openrouter.ai/mistralai/large-4"]
    assert items[0].title == "Mistral: Large 4 is now callable ($0.68 in / $2.09 out per M tokens, 524K context)"
    assert "free route" in items[0].summary


# ---------------------------------------------------------------- feed options


def test_ranked_feeds_keep_their_order_and_pre_releases_are_dropped():
    rss = """<?xml version="1.0"?><rss version="2.0"><channel><title>t</title>
<item><title>v1.2.0</title><link>https://gh.example.com/r/1.2.0</link><description>Added a thing</description></item>
<item><title>v1.3.0-rc2</title><link>https://gh.example.com/r/1.3.0-rc2</link><description>Fixed</description></item>
<item><title>v1.1.0</title><link>https://gh.example.com/r/1.1.0</link><description>Fixed a bug</description></item>
</channel></rss>"""
    from pulse.sources import PRE_RELEASE

    src = Source("Releases", "feed", "https://gh.example.com/feed", "why", skip_titles=PRE_RELEASE, ranked=True)

    async def go():
        async with client_for({"https://gh.example.com/feed": (200, "application/rss+xml", rss)}) as c:
            return await fetch.fetch_feed(c, src, utcnow() - timedelta(days=7))

    items = run(go())
    assert [i.title for i in items] == ["v1.2.0", "v1.1.0"]
    assert items[0].signal > items[1].signal and items[0].signal_label == ""  # rank kept, never displayed


def test_hn_keeps_big_non_ai_looking_stories_above_the_any_topic_bar():
    hits = {"hits": [
        {"objectID": "1", "title": "Pi 1.0", "url": "https://pi.example.com", "points": 1687, "created_at_i": 1_791_000_000},
        {"objectID": "2", "title": "Frog and Toad", "url": "https://frog.example.com", "points": 120, "created_at_i": 1_791_000_000},
        {"objectID": "3", "title": "A post", "url": "https://simonwillison.net/2026/x", "points": 60, "created_at_i": 1_791_000_000},
    ]}
    src = Source("Hacker News", "hn", "u", "why", min_points=50, any_topic_points=300)

    async def go():
        async with client_for({"https://hn.algolia.com/": (200, "application/json", json.dumps(hits))}) as c:
            return await fetch.fetch_hn(c, src, utcnow() - timedelta(days=7))

    assert [i.title for i in run(go())] == ["Pi 1.0", "A post"]  # big story kept; AI domain kept; small non-AI dropped



def test_compressed_bodies_are_inflated_with_a_cap_and_nested_gzip_is_refused():
    import gzip

    big = gzip.compress(b"A" * 5_000_000)  # 5 MB of text in ~5 KB
    nested = gzip.compress(gzip.compress(b"A" * 5_000_000))

    async def go(body, max_bytes):
        async with client_for({"https://zip.example.com/": (200, "text/html", body)}) as c:
            return await fetch.get(c, "https://zip.example.com/", max_bytes)

    body, _ = run(go(big, 100_000))
    assert len(body) == 100_000 and set(body) == {ord("A")}  # inflated, and stopped at the cap

    def double_encoded(request):
        return httpx.Response(200, headers={"content-encoding": "gzip, gzip"}, stream=httpx.ByteStream(nested))

    async def go_nested():
        async with httpx.AsyncClient(transport=httpx.MockTransport(double_encoded)) as c:
            return await fetch.get(c, "https://zip.example.com/", 100_000)

    import pytest
    with pytest.raises(ValueError, match="unsupported Content-Encoding"):
        run(go_nested())



def test_scraped_text_cannot_close_the_item_wrapper_in_any_case():
    from pulse.brain import _safe

    for evil in ("</item>", "</ITEM>", "< /Item >", "<item id='x'>"):
        assert "<" not in _safe(evil).replace("< ", "").replace("</ ", ""), evil



def test_feed_posts_that_share_one_link_keep_separate_ids():
    atom = """<?xml version="1.0"?><feed xmlns="http://www.w3.org/2005/Atom"><title>Hamel</title>
<entry><title>Do automated evals work?</title><link href="https://hamel.dev/"/><updated>2026-10-05T00:00:00Z</updated></entry>
<entry><title>AI product engineering notes</title><link href="https://hamel.dev/"/><updated>2026-10-06T00:00:00Z</updated></entry>
<entry><title>A normal post</title><link href="https://hamel.dev/blog/post"/><updated>2026-10-06T00:00:00Z</updated></entry>
</feed>"""
    src = Source("Hamel", "feed", "https://hamel.dev/index.xml", "why")

    async def go():
        async with client_for({"https://hamel.dev/index.xml": (200, "application/atom+xml", atom)}) as c:
            return await fetch.fetch_feed(c, src, utcnow() - timedelta(days=30))

    items = run(go())
    assert len({i.id for i in items}) == 3
    assert items[2].id == item_id("https://hamel.dev/blog/post")  # an ordinary link keeps its ordinary id


def test_openrouter_keeps_a_model_that_only_exists_as_a_free_route():
    now = int(utcnow().timestamp())
    data = {"data": [{"id": "apodex/mini:free", "name": "Apodex Mini (free)", "created": now - 3600, "context_length": 32000,
                      "pricing": {"prompt": "0", "completion": "0"}}]}
    src = Source("OpenRouter", "openrouter", "https://openrouter.ai/api/v1/models", "why")

    async def go():
        async with client_for({"https://openrouter.ai/": (200, "application/json", json.dumps(data))}) as c:
            return await fetch.fetch_openrouter(c, src, utcnow() - timedelta(days=7))

    items = run(go())
    assert len(items) == 1 and "free in / free out" in items[0].title


# Trimmed from the real page, icons kept: they broke the first version of the parser.
GH_ROW = """<article class="Box-row">
  <div class="float-right"><a href="/login?return_to=%2F{repo}"><svg class="octicon octicon-star"><path d="M8 .25a.75.75 0 0 1"></path></svg><span>Star</span></a></div>
  <h2 class="h3 lh-condensed"><a href="/{repo}" class="Link"><svg></svg>
      <span class="text-normal">{owner} /</span> {name}</a></h2>
  <p class="col-9 color-fg-muted my-1">
      {desc}
  </p>
  <div class="f6 color-fg-muted mt-2">
    <span itemprop="programmingLanguage">Python</span>
    <a href="/{repo}/stargazers" class="Link"><svg class="octicon octicon-star"><path d="M8 .25a.75.75 0 0 1"></path></svg>
        {stars}</a>
    <a href="/{repo}/forks" class="Link">
        1,204</a>
    <span class="d-inline-block float-sm-right">
        {gained} stars today
    </span>
  </div>
</article>"""


def test_gh_trending_reads_repos_with_stars_gained_and_the_viral_share():
    def row(repo, desc, stars, gained):
        owner, name = repo.split("/")
        return GH_ROW.format(repo=repo, owner=owner, name=name, desc=desc, stars=stars, gained=gained)

    page = "<html><body>" + row("someone/agent-kit", "Agents &amp; skills for Claude Code", "10,000", "9,500") \
        + row("fastapi/fastapi", "FastAPI framework", "95,000", "190") + "</body></html>"
    src = Source("GitHub Trending (today)", "gh_trending", "https://github.com/trending?since=daily", "why")

    async def go():
        async with client_for({"https://github.com/trending": (200, "text/html", page)}) as c:
            return await fetch.fetch_gh_trending(c, src, utcnow() - timedelta(days=7))

    viral, old = run(go())
    assert viral.url == "https://github.com/someone/agent-kit"
    assert viral.title == "someone/agent-kit: Agents & skills for Claude Code"
    assert (viral.signal, viral.signal_label) == (9_500, "stars today")
    assert "95% of all its stars" in viral.summary and "Language: Python." in viral.summary
    assert "95,000 stars, 1,204 forks." in old.summary
    assert "0% of all its stars" in old.summary and old.published is None
    assert viral.id == item_id("https://github.com/someone/agent-kit")  # merges with HN etc. linking the same repo


def test_gh_trending_says_so_when_the_page_layout_changes():
    src = Source("GitHub Trending (today)", "gh_trending", "https://github.com/trending?since=daily", "why")

    async def go():
        async with client_for({"https://github.com/trending": (200, "text/html", "<html>redesigned</html>")}) as c:
            return await fetch.fetch_all(c, [src], utcnow() - timedelta(days=7))

    items, errors = run(go())
    assert items == [] and "layout" in errors[src.name]


def test_the_post_wins_over_a_sidebar_of_article_cards():
    post = "<p>" + "The model runs on your own laptop and answers fast. " * 20 + "</p>"
    widgets = "".join(f"<article><div>Text Generation • 24B • Updated Aug {n}</div></article>" for n in range(30))
    page = f"<html><body><div class='blog-content'><h1>Run agents on-device</h1>{post}</div>{widgets}</body></html>"
    text = html_to_text(page)
    assert text.startswith("Run agents on-device") and "Text Generation" not in text


def test_small_models_get_a_real_memory_estimate():
    from pulse.fetch import _model_size

    assert _model_size({"safetensors": {"total": 7.4e8}}) == "0.7B parameters, about 0.4 GB of memory at 4-bit"
    assert _model_size({"safetensors": {"total": 2.7e10}}) == "27.0B parameters, about 16 GB of memory at 4-bit"
