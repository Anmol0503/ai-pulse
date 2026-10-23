"""Where AI Pulse looks, and why each source earns its place.

Every endpoint below was fetched live on 2026-10-07 by a source sweep (5 category
agents + a gap-finding critic, 62 candidates). The "why" lines are what that sweep
found, not a guess. Sources it checked and dropped, with reasons, are at the bottom.

To add a source: append a Source(...) with a kind from fetch.FETCHERS.
`cap` is the most NEW items one pull takes from it, so one chatty source
can't crowd out the rest.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Source:
    name: str
    kind: str  # "feed" | "hn" | "hf_papers" | "sitemap" | "md_changelog" | "hf_models" | "openrouter" | "gh_trending"
    url: str  # the machine-readable endpoint
    why: str  # one line: what this catches that the others don't
    group: str = "Other"  # heading on the page's source list
    cap: int = 8  # the most NEW items one pull takes from this source
    homepage: str = ""  # md_changelog: the human page that items link to
    min_points: int = 0  # hn: ignore stories below this score
    any_topic_points: int = 0  # hn: above this score, keep a story even if the title doesn't look like AI
    path_prefix: str = ""  # sitemap: keep URLs containing this
    sitemap_hint: str = ""  # sitemap index: follow child sitemaps containing this
    skip_categories: tuple[str, ...] = ()  # feed: drop entries tagged with these categories
    prefer_lines: str = ""  # feed: regex; matching lines lead the summary (e.g. "Added ..." in release notes)
    skip_titles: str = ""  # feed: regex; drop entries whose title matches (e.g. release candidates)
    ranked: bool = False  # feed: entries arrive best-first (Reddit top, GitHub trending): keep that order
    max_bytes: int = 3_000_000  # the most one response may be


TOOLS = "APIs and tools you use"
LABS = "Labs"
OPEN = "Open models you can run"
RESEARCH = "Research, explained"
PEOPLE = "Practitioners"
VIDEO = "Talks and videos"
COMMUNITY = "Community and news"

RELEASE_LINES = r"^(added|new|changed|removed|deprecated|breaking|security|feat|introduc)"
PRE_RELEASE = r"(?i)-rc\d*\b|\balpha\b|\bbeta\b|\bdev\d*\b|nightly|preview"

SOURCES: list[Source] = [
    # ---- what changed in the APIs and tools you call ------------------------------------
    Source("Claude API release notes", "md_changelog", "https://platform.claude.com/docs/en/release-notes/overview.md",
           "The source of truth for Claude API changes: models, parameters, prices, deprecations. Shows launches before the news page does.",
           TOOLS, cap=8, homepage="https://platform.claude.com/docs/en/release-notes/overview"),
    Source("OpenAI API changelog", "md_changelog", "https://developers.openai.com/api/docs/changelog.md",
           "Every OpenAI API change, dated and tagged with the model or endpoint it touches.",
           TOOLS, cap=6, homepage="https://developers.openai.com/api/docs/changelog"),
    Source("Gemini API changelog", "md_changelog", "https://ai.google.dev/gemini-api/docs/changelog.md.txt",
           "Gemini has the most usable free API tier, so its model and access changes matter on a zero budget.",
           TOOLS, cap=5, homepage="https://ai.google.dev/gemini-api/docs/changelog"),
    Source("Claude Code releases", "feed", "https://github.com/anthropics/claude-code/releases.atom",
           "New flags, hooks and agent features in the tool you use daily. About 6 releases a week, mostly fixes.",
           TOOLS, cap=4, prefer_lines=RELEASE_LINES, skip_titles=PRE_RELEASE),
    Source("Ollama releases", "feed", "https://github.com/ollama/ollama/releases.atom",
           "What changed in the thing that runs models on your Mac.",
           TOOLS, cap=3, prefer_lines=RELEASE_LINES, skip_titles=PRE_RELEASE),
    Source("LangGraph releases", "feed", "https://github.com/langchain-ai/langgraph/releases.atom",
           "Changes to the framework you're learning. Mostly patch releases; triage drops those.",
           TOOLS, cap=2, prefer_lines=RELEASE_LINES, skip_titles=PRE_RELEASE),
    Source("Model Context Protocol blog", "feed", "https://blog.modelcontextprotocol.io/index.xml",
           "Changes to the MCP spec, which every agent tool (Claude Code included) now speaks.",
           TOOLS, cap=2),
    # ---- labs -------------------------------------------------------------------------------
    Source("claude.dev", "feed", "https://claude.dev/rss.xml",
           "Anthropic's blog for builders, mostly by the Claude Code team. Where their hands-on writing lives now.",
           LABS, cap=4),
    Source("OpenAI News", "feed", "https://openai.com/news/rss.xml",
           "OpenAI launches and research land here first. Noisy (~17 posts a week), so customer stories are dropped up front.",
           LABS, cap=10, skip_categories=("Story", "OpenAI Academy", "Global Affairs")),
    Source("Google DeepMind blog", "feed", "https://deepmind.google/blog/rss.xml",
           "Gemini and Gemma launches and DeepMind research, from the source.",
           LABS, cap=4),
    Source("Hugging Face blog", "feed", "https://huggingface.co/blog/feed.xml",
           "Open-model releases and practical guides from the hub everything open is hosted on.",
           LABS, cap=4),
    # ---- open models you can run ------------------------------------------------------------
    Source("Hugging Face trending models", "hf_models",
           "https://huggingface.co/api/models?sort=trendingScore&limit=60&expand[]=safetensors&expand[]=gguf"
           "&expand[]=createdAt&expand[]=likes&expand[]=pipeline_tag&expand[]=tags&expand[]=author",
           "New open-weight models people are actually downloading, with sizes, so you can tell if one fits in 24 GB.",
           OPEN, cap=6),
    Source("OpenRouter new models", "openrouter", "https://openrouter.ai/api/v1/models",
           "Every new model from every lab (Chinese labs included) with its price, context window and any free route.",
           OPEN, cap=6, max_bytes=4_000_000),
    Source("r/LocalLLaMA", "feed", "https://www.reddit.com/r/LocalLLaMA/top/.rss?t=week&limit=50",
           "Where 'does it run on a 24 GB Mac?' gets answered within hours of a release.",
           OPEN, cap=8, ranked=True),
    Source("Ollama blog", "feed", "https://ollama.com/blog/rss.xml",
           "New models and features for running locally on Apple Silicon.",
           OPEN, cap=2),
    # ---- research, explained ------------------------------------------------------------------
    Source("Hugging Face Papers", "hf_papers", "https://huggingface.co/api/daily_papers",
           "Research papers ranked by builders' upvotes: the top handful instead of 500 a day.",
           RESEARCH, cap=8),
    Source("Ahead of AI (Sebastian Raschka)", "feed", "https://magazine.sebastianraschka.com/feed",
           "Explains model architecture at the depth interviews probe: attention variants, reasoning, training.",
           RESEARCH, cap=2),
    Source("Interconnects (Nathan Lambert)", "feed", "https://www.interconnects.ai/feed",
           "Open models and post-training, by someone who trains models for a living.",
           RESEARCH, cap=2),
    Source("Deep (Learning) Focus", "feed", "https://cameronrwolfe.substack.com/feed",
           "Long, clear explainers of one LLM research topic at a time.",
           RESEARCH, cap=2),
    Source("Lil'Log (Lilian Weng)", "feed", "https://lilianweng.github.io/index.xml",
           "Rare posts, each a definitive survey of one topic. Worth knowing the moment one appears.",
           RESEARCH, cap=1),
    Source("Andrej Karpathy", "feed", "https://karpathy.bearblog.dev/feed/",
           "Low volume, high signal. His blog is the live outlet now; the YouTube channel has been quiet since Feb 2025.",
           RESEARCH, cap=1),
    Source("Artificial Analysis", "sitemap", "https://artificialanalysis.ai/sitemap.xml",
           "Independent speed, price and quality numbers for each notable launch: the check on a vendor's own chart.",
           RESEARCH, cap=3, path_prefix="artificialanalysis.ai/articles/", max_bytes=8_000_000),
    # ---- practitioners ------------------------------------------------------------------------
    Source("Simon Willison", "feed", "https://simonwillison.net/atom/entries/",
           "Ships code against every model he writes about, so he tells you whether a release changes what you can build.",
           PEOPLE, cap=6),
    Source("Latent Space", "feed", "https://www.latent.space/feed",
           "swyx's daily AINews recap plus practitioner interviews on agents and production systems.",
           PEOPLE, cap=3),
    Source("Hamel Husain", "feed", "https://hamel.dev/index.xml",
           "Evals, by the person who taught them to 700+ engineers. Evals are the skill that sets you apart.",
           PEOPLE, cap=2),
    Source("Eugene Yan", "feed", "https://eugeneyan.com/rss/",
           "A data scientist turned AI engineer: the move you're making, written up as it happens.",
           PEOPLE, cap=1),
    # ---- talks and videos ---------------------------------------------------------------------
    Source("AI Engineer (YouTube)", "feed", "https://www.youtube.com/feeds/videos.xml?channel_id=UCLKPca3kwwd-B59HNr-_lvA",
           "Conference talks by people running AI in production, failure modes included.",
           VIDEO, cap=4),
    Source("Dave Ebbelaar (YouTube)", "feed", "https://www.youtube.com/feeds/videos.xml?channel_id=UCn8ujwUInbJkBhffxqAPBVQ",
           "Applied, watchable AI engineering. One of the two best fits from your video roadmap.",
           VIDEO, cap=2),
    Source("Neural Breakdown with AVB (YouTube)", "feed", "https://www.youtube.com/feeds/videos.xml?channel_id=UC6MhHkSosYXAD-LTXBWyLMg",
           "Clear visual explainers of ML ideas. The other best fit from your video roadmap.",
           VIDEO, cap=2),
    # ---- community and news -------------------------------------------------------------------
    Source("Hacker News", "hn", "https://hn.algolia.com/api/v1/search",
           "AI stories engineers upvoted past 50 points, plus any story past 300 (product names like 'Pi 1.0' hide AI).",
           COMMUNITY, cap=25, min_points=50, any_topic_points=300),
    Source("GitHub Trending (today)", "gh_trending", "https://github.com/trending?since=daily",
           "The repos 'ranked #1 on GitHub' in reels, read from GitHub itself, with stars gained today so you can tell viral from established.",
           COMMUNITY, cap=5, max_bytes=2_000_000),
    Source("GitHub Trending (this week)", "gh_trending", "https://github.com/trending?since=weekly",
           "Catches the ones that peaked on a day you didn't pull. A repo in both lists shows up once.",
           COMMUNITY, cap=4, max_bytes=2_000_000),
    Source("Ars Technica AI", "feed", "https://arstechnica.com/ai/feed/",
           "Careful reporting for the headline layer: policy, lawsuits and launches, with less hype than most.",
           COMMUNITY, cap=4),
]

# Checked live on 2026-10-07 and left out, so nobody re-adds them by accident:
#   Anthropic News / Engineering: no feed; the listing pages need undocumented JSON scraping. Launches show up
#       in the Claude API release notes first, and builder posts moved to claude.dev.
#   OpenAI "RSS" linked from the changelog page (developers.openai.com/rss.xml): a TRAP. It's cookbook guides,
#       newest 2026-05-05, not the changelog.
#   llama.cpp releases: ~127 a week. Apple MLX core: too low-level. LM Studio hub: overlaps HF + Ollama.
#   AINews on news.smol.ai: moved to Latent Space. Karpathy's YouTube: silent since Feb 2025.
#   TechCrunch (~110/week, mostly funding), The Verge, Techmeme, Ben's Bites, Import AI, hnrss.org: overlap or noise.
#   r/LocalLLaMA JSON endpoint: HTTP 403 (the RSS works).
#   GitHubTrendingRSS (mshibanami.github.io): replaced 2026-10-24. No star counts, so triage couldn't tell a viral
#       repo from an old one, and it ran about a day behind github.com/trending.
#   India-specific, if you want them: ETtech AI https://economictimes.indiatimes.com/tech/artificial-intelligence/rssfeeds/119215726.cms
#       and MediaNama https://www.medianama.com/tag/artificial-intelligence/feed/ (both ~45 posts a week).
