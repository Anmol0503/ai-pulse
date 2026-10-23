# AI Pulse

Press one button. Get what's new in AI from 32 sources, sorted into three piles,
with the few that matter to you explained in plain English. Then the page ends:
**"You're caught up."**

- **Learn** (at most 5): a short card in plain words. What happened, the idea, a
  picture from your own work (SQL, ETL, sklearn), why it matters to you, and one line
  to remember. The term, an interview line, how sure it is and a 15-minute thing to
  try sit behind "Go deeper".
- **Know**: one line each. Big enough that you should have heard of it.
- **Skipped**: everything else, each with the reason it was safe to skip.
  (This is the Ignore Ledger from `06-staying-current.md`, written for you.)

## Run it

Needs [uv](https://docs.astral.sh/uv/) (`brew install uv`). Then:

```bash
cd ai-pulse
uv run pulse          # first run installs dependencies, then opens http://127.0.0.1:8765
```

Or double-click **`start.command`** in Finder. Press **Pull latest** whenever you
want. Each pull shows only what's new since the last one.

Choose who explains with the menu next to the button:

| Engine | How it runs | Trade-off |
|---|---|---|
| **Claude (your plan)** | `claude -p`, Claude Code's headless mode, with every tool off. Haiku sorts, Opus writes the cards | Best explanations. Uses your plan's usage; no API key, no bill. |
| **Local (free, less accurate)** | `qwen2.5:7b-instruct` through Ollama on your Mac | Free, private, offline. Weaker at judging what matters, and it invents details: on its first test card it claimed the Claude API is free. The page labels its cards. |

## On your phone

**https://anmol0503.github.io/ai-pulse/**. Bookmark it, or Share, then Add to Home Screen
for an app icon.

A GitHub Action (`.github/workflows/pulse.yml`) pulls every morning at 7:30 (India time)
and publishes the result there. For a pull right now, tap **Pull now** on the page: it
opens the workflow on GitHub. Tap **Run workflow**, then reload the page in about three
minutes.

It runs on your Claude plan through a token, and reads your profile from a secret:

```bash
claude setup-token                                              # opens the browser, prints a token
gh secret set CLAUDE_CODE_OAUTH_TOKEN -R Anmol0503/ai-pulse     # paste it when asked
gh secret set PULSE_PROFILE -R Anmol0503/ai-pulse < profile.md  # again whenever you edit profile.md
```

**What's public:** the site and this code. Anyone with the link can read the digests,
including lines like "your 24 GB Mac". **What isn't:** `profile.md` (git ignores it; the
cloud reads the `PULSE_PROFILE` secret) and your token.

**How the cloud remembers what you've seen:** after each run, `deploy.sh` force-pushes the
site to the `gh-pages` branch and the seen-list to the `pulse-data` branch, one commit each,
so no history piles up. Your Mac and the cloud keep separate seen-lists.

**If it stops:** GitHub emails you when a run fails, and the page says why. The usual cause
is an expired token: run the first two commands again. If GitHub ever pauses the schedule
(it does that to quiet repos after 60 days), turn it back on from the Actions tab.

## What one pull does

```
32 sources ──fetch──► ~3,000 items ──new to you?──► ~110 (7-day first pull)
   │ 3 s, all at once             (seen-list in data/pulse.db)
   ▼
skim     read the first lines of title-only links (Hacker News, DeepMind)   ~2 s
triage   Haiku sorts every item: learn / know / skip, score 1-10            ~40 s
merge    one call folds "same story, different source" into one item        ~6 s
budget   YOUR rule in pulse/budget.py picks at most 5 to explain           instant
read     fetch the full article for those 5                                 ~2 s
explain  Opus writes the plain cards                                       ~60 s
```

Measured on 2026-10-08: a 7-day first pull took **73 s** and would have cost
**$0.11** at API prices (7 model calls, 72k tokens in, 30k out). On your plan,
that's usage, not a bill. Daily pulls are a fraction of that.

**Why two passes?** Sorting needs a glance at every item, so it uses a fast model
on ~700 characters each. Explaining needs the full article, but only for five
items, so it uses a stronger one. One strong model reading everything in full
would cost several times more for the same five cards. This **router → worker**
shape is how you keep an LLM pipeline's cost proportional to what matters.

## How the 5 are picked: `pulse/budget.py`

Triage can mark a dozen items "learn". You get five, most important first: ranked by
triage's 1–10 score, then by how many of your three questions the item passed, then by
how new it is. No source gets more than 2 of the 5 unless there's nothing else, so a
week of changelog lines can't push out the week's big story. An item with too little
real text to explain (a talk with only a title) gets no card; the next one steps in.

It's a short function and it's yours: change the rule, save, press Pull (it reloads on
every pull), and check the basics still hold:

```bash
uv run pytest tests/test_budget.py -v
```

You can rank on `v.score`, `v.hits`, `v.signal` (HN points, HF upvotes), `v.published`
and `v.source`. Careful with `signal`: 300 Hacker News points and 300 Hugging Face
upvotes are not the same amount of excitement.

## Make it yours

- **`profile.md`**: who you are and your three questions. Triage and the explainer
  read it on every pull. Edit it when it stops being true.
- **`pulse/sources.py`**: the 32 sources, each with a one-line reason it's there,
  plus the ones checked and dropped, so they don't creep back. Add a source by
  appending a `Source(...)`. `cap` limits how many new items one source can bring
  per pull. Reloaded on every pull, like `budget.py`.

## Decisions that came from measuring, not guessing

- **The cards are written for a newcomer, and tested on one.** The first version was
  ~250 words in 8 parts, with up to 11 unexplained AI words per card. Now it's ~95
  words in 5 parts, with the rest behind "Go deeper". On 8 real items, two blind
  newcomer readers (a data scientist new to AI; a second-language reader) preferred
  Opus's cards 7 times out of 8 and understood them better (4.5 vs 3.5 out of 5), and
  Opus used 0.4 unexplained AI words per card to Sonnet's 1.4. Pictures from your own
  work (a cached warehouse query, an sklearn cutoff) were the ones they remembered.
- **No source, no card.** When a source was thin, both models filled the gaps with
  confident guesses. A fact checker caught it, so thin items no longer get explained,
  and the writer must keep the source's strength of claim ("can help" stays "can help").

- **Triage runs at low effort.** On one batch of 30, Haiku 5.5 took 29 s at low effort
  and 52 s at medium, and picked the same three "learn" items both times.
  `MAX_THINKING_TOKENS=0` stopped working the day Haiku 5.5 shipped (it thinks
  adaptively and ignores manual budgets). The app's first pull headlined that exact
  change: *"Haiku 4.5 code can break on Haiku 5.5 due to thinking changes."*
- **API changelogs are read from their Markdown copies** (`.../overview.md`,
  `changelog.md`, `changelog.md.txt`). The Claude one listed Haiku 5.5 before the
  news page did. The "RSS" link on OpenAI's changelog page is a trap: it's cookbook
  guides, last updated in May.
- **Hacker News at 50+ points, or 300+ whatever the title says.** At 150 points it
  missed niche items that matter to you; at 300, titles like "Pi 1.0" hide AI
  stories from any keyword filter.
- **Nothing is marked as read until a pull finishes.** If Claude is rate-limited
  while sorting, the next pull picks the same items up again. If it fails while
  explaining, the item shows with its headline now and is explained on the next pull.
- **Scraped text can't take control.** Claude runs with every tool switched off, the
  prompts treat items as data, and the page renders all scraped text as plain
  text. The server only answers on localhost, refuses cross-site requests and can't
  be framed, so another website can't spend your plan.

## Files

```
pulse/sources.py   where it looks, and why            pulse/brain.py     prompts, both engines, triage/merge/explain
pulse/fetch.py     7 source kinds -> Items             pulse/budget.py    YOUR rule
pulse/pipeline.py  one pull, start to finish           pulse/store.py     SQLite: pulls + every item shown
pulse/server.py    FastAPI + live progress (SSE)       pulse/static/      the page
pulse/publish.py   one pull -> static site (the cloud)  deploy.sh          site -> gh-pages, seen-list -> pulse-data
profile.md         who it's for (example: profile.example.md)  tests/     54 tests: uv run pytest
```

## When something goes wrong

- **"The `claude` command isn't on PATH"**: install Claude Code, or switch to Local.
- **"Ollama isn't running"**: open the Ollama app. Model missing: `ollama pull qwen2.5:7b-instruct`.
- **A source "didn't answer"**: the page names it. Its items arrive on a later pull.
  Reddit rate-limits anonymous readers hardest.
- **Start over**: stop AI Pulse (Ctrl+C in its Terminal window), delete `data/pulse.db`,
  start it again. The next pull looks back 7 days.
