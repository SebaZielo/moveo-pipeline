# MOVEO pipeline

The daily work that used to run as Claude scheduled tasks, now on **GitHub Actions** (the computer that runs it) and **Supabase** (where the data lives). Nothing here needs Claude to run.

| Before (Claude task) | Now (step in `.github/workflows/daily.yml`) |
|---|---|
| Date check, step 2a: cleanup | `cleanup`: listings that are over are deleted; anything with a reaction and open-ended places are never deleted |
| Harvester (+ organizer directory, Don't Forget list) | `harvest` |
| Date check, part 2: blog reader | `blog` |
| Date check, part 1 | `datecheck` (`pipeline/datecheck.py`) |
| Map | `map` |
| Artwork | `artwork`: pictures go to Supabase Storage, bucket `covers` |
| Website sync | `sync` (only when the repository variable `WEBSITE_SYNC` is `on`) |

It runs every morning at 05:35 Buenos Aires time, and in about 2½ hours it does what the old tasks did.

The scripts in `pipeline/legacy/` are the same programs the Claude tasks ran. `pipeline/run.py` hands them the data as folders of JSON files, exactly as before, and writes back only what changed.

## Where the data is

Supabase table `public.app_docs` has the columns `(collection, id, data)`. It holds the same documents the Claude app database held: `events`, `reactions`, `settings`, `harvest/state`, `directory`, `dirgeo`, `locations`, `future`, plus `runs/<date>`, a report for each day.

The website keeps its own tables (`events`, `venues`, …). The `sync` step fills them from here.

## The few judgment calls

Writing a one-line description, fixing a genre, and dropping kids-only, online-only or non-events are judgment calls. Two ways to handle them:

- **With a Claude API key** (secret `ANTHROPIC_API_KEY`): Claude Haiku 4.5 reviews the day's new listings in batches. It is capped at 700 items a day by default (`LLM_MAX_ITEMS`). At $1 per million input tokens and $5 per million output tokens, that comes to cents per day, roughly $3–10 a month.
- **Without a key:** simple rules do it.
  - They drop obvious kids-only, online-only and private items.
  - The description becomes "Live music at <venue>".
  - New blog postings are only added when their original page was found.

What no longer happens automatically is the self-repair. When a site changes its layout, its source shows 0 in the daily report. To fix it, open a Claude Code session on this repo and ask it to fix that source. It edits the script here and commits.

## Daily use

- **See what happened:** Actions tab → latest "MOVEO daily" run. The summary lists every step's numbers.
- **Run now, or test:** Actions → MOVEO daily → **Run workflow**.
  - Choose the steps, e.g. `datecheck` or `blog`.
  - Tick **dry run** to see what it would change without writing anything.
- **Failures:** if a step fails, the run turns red and GitHub emails you. The other steps still run.
- **Pause everything:** Actions → MOVEO daily → "…" → Disable workflow.

## Settings

Secrets: Settings → Secrets and variables → Actions → Secrets.

| Secret | |
|---|---|
| `SUPABASE_URL` | `https://<project>.supabase.co` |
| `SUPABASE_SERVICE_KEY` | Supabase → Project Settings → API keys → secret / service_role key |
| `ANTHROPIC_API_KEY` | optional, see above |

Variables: same page, Variables tab.

| Variable | |
|---|---|
| `WEBSITE_SYNC` | `on` once the Claude website-sync task is paused (until then leave `off`) |
| `MOVEO_MODEL` | optional, default `claude-haiku-4-5-20251001` |

## Cost

GitHub Actions is free and unlimited for **public** repositories. The code holds no secrets; secrets stay in GitHub's encrypted settings, and the data stays in Supabase.

A **private** repository gets 2,000 free minutes a month. This pipeline needs about 4,500, so it would cost about $20 a month on GitHub's per-minute rate. You could also shorten the time limits: `HARVEST_SECONDS`, `BLOG_MINUTES`, `DATECHECK_MINUTES`, `ARTWORK_MINUTES`.

## Local test (no network needed)

```
pip install -r requirements.txt
python -m tests.test_offline          # date-check decisions; prints FAILS 0
```
