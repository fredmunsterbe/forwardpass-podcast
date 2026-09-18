# The Forward Pass — Daily Podcast · Operations Runbook

**Owner:** Fred Munster (munster.fred@gmail.com) · **Product:** AI Cure Newsroom
**Last updated:** 2026-09-18 · **Status:** Live

This document describes the fully automated pipeline that turns the daily
**AI Daily Brief** into an audio episode published to **Spotify**. It covers the
architecture, configuration, day-to-day operations, troubleshooting, and recovery.

---

## 1. What it does (one paragraph)

Every morning, the existing **BAGEHOT** scheduled task writes the day's brief (HTML + PDF)
**and a purpose-built spoken script** to a Google Drive folder. A **GitHub Actions** job then
picks up that script, renders it to audio with **OpenAI text-to-speech**, and publishes it as an
episode to **Transistor** (the podcast host). Transistor's RSS feed is registered with **Spotify**,
so the new episode appears in the show automatically. No human action is required day to day.

---

## 2. Architecture

![Forward Pass daily podcast pipeline](podcast_architecture.png)

```
06:30 Brussels          ~07:10 Brussels
┌───────────────┐       ┌──────────────────────────────────────────────┐
│ BAGEHOT task  │       │ GitHub Actions: "Daily AI Brief Podcast"       │
│ (Claude cloud │       │  make_episode.py                               │
│  scheduled    │       │   1. read newest brief + script from Drive     │
│  task)        │       │   2. (today-only guard)                        │
│               │       │   3. OpenAI TTS  → MP3                          │
│  writes to    │──────▶│   4. upload + publish → Transistor             │
│  Google Drive │ Drive │                                                │
│  "Daily AI    │       └───────────────┬────────────────────────────────┘
│   Brief"      │                       │ RSS
│  - .html      │                       ▼
│  - .pdf       │                ┌──────────────┐      ┌───────────┐
│  - _script.txt│                │  Transistor  │─────▶│  Spotify  │
└───────────────┘                │  (host+RSS)  │ feed │  (show)   │
                                 └──────────────┘      └───────────┘
```

**Why two separate systems?** The Claude cloud sandbox that runs BAGEHOT has locked-down
network egress — it can reach Google/Notion/Gmail/Slack but **not** OpenAI or Transistor.
So audio generation and publishing must run somewhere with open internet: GitHub Actions.
Google Drive is the hand-off point between the two.

**Why podcast-first (not a Spotify playlist)?** Spotify does not allow uploading an MP3 into a
playlist. Self-published audio reaches Spotify only as a **show** via an RSS feed. Episodes can
later be added to a playlist manually if desired, but the show is the reliable delivery path.

---

## 3. Components & accounts

| Component | What it is | Where |
|---|---|---|
| **BAGEHOT task** | Claude scheduled task that writes the brief + script to Drive | Claude app → scheduled tasks · trigger id `trig_016dhi4hRzBsUq65bya5DoVk` · cron `30 4 * * *` UTC |
| **Google Drive folder** | "Daily AI Brief" — holds `.html`, `.pdf`, `_script.txt` | folder id `1v2w8Q56LpXPAmi3gXqwmaX6NVgBr3z00` |
| **Google service account** | Read-only Drive access for the runner | Google Cloud Console → IAM → Service Accounts |
| **GitHub repo** | Holds the runner + workflow | private repo `forwardpass-podcast` |
| **GitHub Actions** | Runs the pipeline daily | repo → Actions → "Daily AI Brief Podcast" |
| **OpenAI** | Script polish + TTS | api.openai.com (key in GitHub secret) |
| **Transistor** | Podcast host + RSS feed | transistor.fm |
| **Spotify** | Public/observer surface | Spotify for Creators |

---

## 4. Daily timeline

| Time (Brussels) | Actor | Action |
|---|---|---|
| 06:30 | BAGEHOT | Publishes brief HTML + PDF to Drive; writes `AI_Daily_Brief_<date>_script.txt` (STEP 2d); stages Gmail + Slack drafts |
| 07:10 | GitHub Actions (cron `10 5 * * *` UTC) | Runs `make_episode.py` |
| 07:10–07:13 | make_episode.py | Reads script from Drive → OpenAI TTS → MP3 → publishes to Transistor |
| within minutes–hours | Transistor → Spotify | New episode ingested into the show |

---

## 5. Repository contents

```
forwardpass-podcast/
├── make_episode.py                 # the runner (see §6)
└── .github/workflows/
    └── daily-podcast.yml           # schedule + secrets wiring
```

### 6. make_episode.py — what it does, step by step
1. **get_drive()** — authenticates to Google Drive read-only using the service-account JSON.
2. **newest_brief_html()** — finds the newest `AI_Daily_Brief_*.html` in the folder.
3. **today-only guard** — if that brief's date isn't today (Europe/Brussels), it exits without
   publishing, so a skipped BAGEHOT day never produces a duplicate. Override with `FORCE=1`.
4. **fetch_script_text()** — prefers BAGEHOT's `AI_Daily_Brief_<date>_script.txt`. If missing,
   falls back to **make_script()**, which asks GPT to turn the brief HTML into a spoken script.
5. **synth()** — OpenAI `gpt-4o-mini-tts` renders the script to MP3, chunked to stay under the
   4096-character per-request limit, with a dynamic "news-anchor" delivery instruction.
6. **publish()** — Transistor `authorize_upload` → PUT the MP3 → create episode → publish.

---

## 7. Configuration

### GitHub → Settings → Secrets and variables → Actions

**Secrets** (never printed; values known only to you):

| Secret | Value |
|---|---|
| `OPENAI_API_KEY` | OpenAI API key (rotate periodically) |
| `TRANSISTOR_API_KEY` | Transistor account API key |
| `TRANSISTOR_SHOW_ID` | Numeric show id (`curl -s https://api.transistor.fm/v1/shows -H "x-api-key: KEY"`) |
| `DRIVE_FOLDER_ID` | `1v2w8Q56LpXPAmi3gXqwmaX6NVgBr3z00` |
| `GOOGLE_SA_JSON` | Full service-account JSON (entire file contents) |

**Variables:**

| Variable | Value | Notes |
|---|---|---|
| `TTS_VOICE` | `marin` | Current voice. Options: `marin`, `cedar`, `verse`, `ballad`, `sage`, `coral`, `onyx`, `nova`, `ash`, `alloy`, `shimmer`, `echo`, `fable` |

### Schedule
`daily-podcast.yml` → `on.schedule.cron: "10 5 * * *"` (UTC) → ~07:10 Brussels. Keep it ~30–40 min
after the BAGEHOT run (`30 4 * * *` UTC).

---

## 8. Common operations

**Change the voice**
Repo → Settings → Variables → edit `TTS_VOICE`. No code change or redeploy. Takes effect next run.

**Test / publish on demand**
Repo → Actions → "Daily AI Brief Podcast" → **Run workflow**. Tick **force** to publish even if the
newest brief isn't today's (useful for testing off-hours).

**Change the audio style / length**
Two levers:
- *Delivery* (energy, pacing): the `instructions=` string in `synth()` inside `make_episode.py`.
- *Words* (script itself): **STEP 2d** in the BAGEHOT task prompt (word count, tone, intro/outro).
  Edit the scheduled task's prompt in the Claude app.

**Change the schedule**
Edit the cron in `daily-podcast.yml` and commit.

**Rotate the OpenAI key**
Create a new key in the OpenAI dashboard → update the `OPENAI_API_KEY` secret → revoke the old key.
Recommended every ~90 days, and immediately if a key is ever exposed.

**Update the runner code**
Edit `make_episode.py` in the repo (web editor or git) and commit — the next scheduled run uses it.

---

## 9. Monitoring & verification

- **Did it run?** Repo → Actions tab. Green = success. Each run's log shows: `Fetched …html`,
  `Using BAGEHOT spoken script …` (or the HTML fallback), `synth chunk N ok`,
  `Published Transistor episode <id>`.
- **Episode live?** Transistor dashboard → the show → episode list.
- **On Spotify?** Spotify for Creators, or the public show page (after first-show approval).
- **Skipped day?** Log says `Newest brief is <date> (not today …); skipping.` — expected when
  BAGEHOT didn't publish that day.

---

## 10. Troubleshooting

| Symptom (in the Actions log) | Cause | Fix |
|---|---|---|
| `Invalid value: ''. Supported values are: …` on `voice` | `TTS_VOICE` variable empty/unset | Set `TTS_VOICE` to a valid voice; code also falls back to `onyx` |
| `No brief HTML found in the Drive folder` | Folder not shared with the service account, or wrong `DRIVE_FOLDER_ID` | Share "Daily AI Brief" with the SA email (Viewer); check the secret |
| `HttpError 403` from Google | Drive API not enabled, or SA lacks access | Enable Drive API; re-share the folder |
| `Newest brief is <date>…; skipping` | Today-only guard: no brief for today | Expected if BAGEHOT skipped; use **force** to override |
| Transistor `401`/`403` | Bad/expired `TRANSISTOR_API_KEY` or wrong `TRANSISTOR_SHOW_ID` | Re-copy the key and show id |
| OpenAI `401` | Bad/rotated `OPENAI_API_KEY` | Update the secret |
| Episode published but not on Spotify | Show not yet approved, or ingestion delay | Confirm the RSS is submitted in Transistor → Distribution; wait (minutes–hours) |
| Voice sounds flat | Voice choice / instruction | Try `marin`/`cedar`/`verse`; strengthen `synth()` instruction; shorten STEP 2d sentences |

**Note on the Claude sandbox:** it cannot reach OpenAI or Transistor (locked egress). This is by
design — never try to move the TTS/publish steps into the BAGEHOT task; they must stay in GitHub Actions.

---

## 11. Cost (approximate)

| Item | Cost |
|---|---|
| OpenAI (script polish + TTS, ~6–7 min/day) | a few cents to ~$0.30/day |
| Transistor host | per plan (~$19/mo tier) |
| GitHub Actions (private repo, ~3 min/day) | within free minutes |
| Spotify | free |

---

## 12. Recovery & rollback

- **Pause the whole thing:** Repo → Actions → "Daily AI Brief Podcast" → **⋯ → Disable workflow**.
  (BAGEHOT keeps producing the brief; only the podcast stops.)
- **Unpublish a bad episode:** delete/unpublish it in the Transistor dashboard; it drops from the
  RSS feed and (after re-ingestion) from Spotify.
- **Revert code:** restore a previous commit of `make_episode.py` in GitHub.
- **Turn off the script upgrade only:** remove STEP 2d from the BAGEHOT prompt — the runner then
  auto-falls back to generating the script from the HTML.

---

## 13. Reference IDs (non-secret)

- Drive folder "Daily AI Brief": `1v2w8Q56LpXPAmi3gXqwmaX6NVgBr3z00`
- BAGEHOT task (trigger): `trig_016dhi4hRzBsUq65bya5DoVk` · cron `30 4 * * *` UTC
- Podcast workflow cron: `10 5 * * *` UTC (~07:10 Brussels)
- Slack channel (#all-ai-cure): `C0B9MTMUQCC`
- Email draft recipients: munster.fred@gmail.com, frederic.munster@nttdata.com, alec.boyle@nttdata.com, pawel.andre@nttdata.com

> Secrets (API keys, service-account JSON) live **only** in GitHub Actions secrets — never in this
> document, the repo, or chat. If a key is exposed, rotate it immediately (§8).
