# forwardpass-podcast

Automation that turns **The Forward Pass** into audio on Spotify. Two pipelines, one repo,
shared secrets. Both run in **GitHub Actions** (open internet — the Claude cloud sandbox that
writes the briefs/issues can't reach OpenAI or Transistor), read their source from **Google
Drive**, render speech with **OpenAI TTS**, and publish to **Transistor**, whose RSS feed reaches
Spotify.

| Pipeline | Source | Output | Runner | Schedule (UTC) |
|---|---|---|---|---|
| **Daily** | newest `AI_Daily_Brief_*.html` (Drive) | ~6-min single-anchor brief | `make_episode.py` · `daily-podcast.yml` | `10 5 * * *` |
| **Weekly** | newest `*-the-forward-pass-issue-NNN.html` (Drive) | **~20-min two-host** conversation | `make_weekly_episode.py` · `weekly-podcast.yml` | `15 6 * * 1`, `15 6 * * 2` |

The weekly publishes to the **same Transistor show** as the daily by default (set
`TRANSISTOR_SHOW_ID_WEEKLY` to split them).

## Setup

Secrets live in **Settings → Secrets and variables → Actions**. Full details, configuration
tables, testing and troubleshooting are in **`FORWARD_PASS_PODCAST_RUNBOOK.md`** — start there.

Shared secrets: `OPENAI_API_KEY`, `TRANSISTOR_API_KEY`, `TRANSISTOR_SHOW_ID`, `GOOGLE_SA_JSON`.
Daily also uses `DRIVE_FOLDER_ID`. The weekly reads the "Weekly AI Journal" folder (id defaulted
in the script) — **share that folder (Viewer) with the same service-account email** the daily uses.

## Test

Repo → **Actions** → pick a workflow → **Run workflow**. Tick **force** to publish even when the
source isn't fresh or was already published (needed for off-cadence testing).

> Secrets never live in this repo, its history, or chat. Rotate any exposed key immediately.
