#!/usr/bin/env python3
"""
Forward Pass — WEEKLY podcast episode maker (runs in GitHub Actions, open internet).

The weekly companion to make_episode.py. Same architecture (Drive -> GitHub Actions
-> OpenAI TTS -> Transistor -> Spotify), but it turns the full weekly issue of
"The Forward Pass" into a ~20-minute TWO-HOST conversation.

Flow:
  1. Find the newest English weekly issue HTML in the Google Drive
     "Weekly AI Journal" folder (read-only, via a Google service account).
     Naming: YYYY-MM-DD-the-forward-pass-issue-NNN.html  (FR / RUN-RECORD / longread
     / epoch / bound-volume files are excluded). Newest is chosen by ISSUE DATE
     (parsed from the filename), which is robust to Drive backfills/re-uploads.
  2. Freshness guard: skip if that issue's date is older than WEEKLY_FRESH_DAYS
     (default 6) unless FORCE=1 — so a skipped week is never re-voiced.
  3. Dedup guard: skip if the show already has an episode for this issue number
     (checked against Transistor) unless FORCE=1 — safe across re-runs/backfills.
  4. Turn the issue into a ~20-minute two-host script with OpenAI (host + analyst),
     honouring the paper's anti-hype house charter (no hype words, numbers over
     adjectives, only what's in the issue, no invented metrics/quotes, no URLs).
     A purpose-built script on Drive (<issue-basename>_pod.txt) is preferred if present.
  5. Render the script to MP3 with OpenAI TTS, one request per speaker turn using
     that speaker's voice, then stitch into a single MP3 (ffmpeg if available,
     else byte-concat).
  6. Upload + publish the episode to Transistor (same show as the daily by default).

Env vars (GitHub Actions secrets, shared with the daily unless noted):
  OPENAI_API_KEY            - OpenAI key
  TRANSISTOR_API_KEY        - Transistor account API key
  TRANSISTOR_SHOW_ID        - the show to publish into (same show as the daily by default)
  TRANSISTOR_SHOW_ID_WEEKLY - optional; a separate weekly show. Falls back to TRANSISTOR_SHOW_ID.
  GOOGLE_SA_JSON            - full service-account JSON (must have Viewer on the weekly folder)
  WEEKLY_DRIVE_FOLDER_ID    - optional; defaults to the "Weekly AI Journal" folder id below.

Repo Variables (Settings > Variables), all optional with sensible defaults:
  TTS_VOICE_A          - host voice   (default "marin")
  TTS_VOICE_B          - analyst voice(default "cedar")
  HOST_A_NAME          - host name    (default "Alex")
  HOST_B_NAME          - analyst name (default "Sam")
  WEEKLY_TARGET_MINUTES- target length (default "20")
  WEEKLY_MODEL         - script model (default "gpt-4o")
  WEEKLY_FRESH_DAYS    - freshness window in days (default "6")

Deps:  pip install openai google-api-python-client google-auth requests
"""
import os, sys, io, re, json, datetime, tempfile, subprocess, shutil
import requests
from openai import OpenAI
from google.oauth2 import service_account
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseDownload

# ---------- config ----------
OPENAI_API_KEY     = os.environ["OPENAI_API_KEY"]
TRANSISTOR_API_KEY = os.environ["TRANSISTOR_API_KEY"]
SHOW_ID            = os.environ.get("TRANSISTOR_SHOW_ID_WEEKLY") or os.environ["TRANSISTOR_SHOW_ID"]
# "Weekly AI Journal" Drive folder (holds the issue HTML files).
WEEKLY_FOLDER_ID   = os.environ.get("WEEKLY_DRIVE_FOLDER_ID") or "1YF23oNpM807KO-cQizSUjSDvl882J8H7"

VOICE_A       = os.environ.get("TTS_VOICE_A") or "marin"    # host
VOICE_B       = os.environ.get("TTS_VOICE_B") or "cedar"    # analyst
NAME_A        = os.environ.get("HOST_A_NAME") or "Alex"
NAME_B        = os.environ.get("HOST_B_NAME") or "Sam"
TARGET_MIN    = int(os.environ.get("WEEKLY_TARGET_MINUTES") or "20")
SCRIPT_MODEL  = os.environ.get("WEEKLY_MODEL") or "gpt-4o"
FRESH_DAYS    = int(os.environ.get("WEEKLY_FRESH_DAYS") or "6")
FORCE         = os.environ.get("FORCE", "").lower() in ("1", "true", "yes")

# ~150 spoken words/min; target the middle of a sensible band for TARGET_MIN.
TARGET_WORDS  = int(TARGET_MIN * 150)
WORDS_LO      = int(TARGET_WORDS * 0.92)
WORDS_HI      = int(TARGET_WORDS * 1.12)

client = OpenAI(api_key=OPENAI_API_KEY)

ISSUE_RE = re.compile(r"^(\d{4}-\d{2}-\d{2})-the-forward-pass-issue-(\d+)\.html$", re.I)

# ---------- 1. Drive: find + download the newest EN issue ----------
def get_drive():
    creds = service_account.Credentials.from_service_account_info(
        json.loads(os.environ["GOOGLE_SA_JSON"]),
        scopes=["https://www.googleapis.com/auth/drive.readonly"])
    return build("drive", "v3", credentials=creds)

def _download(drive, file_id):
    buf = io.BytesIO()
    dl = MediaIoBaseDownload(buf, drive.files().get_media(fileId=file_id))
    done = False
    while not done:
        _, done = dl.next_chunk()
    return buf.getvalue().decode("utf-8", "ignore")

def newest_issue(drive):
    """Return (date, issue_no, file_id, name) for the newest EN weekly issue.
    Newest is by the DATE in the filename (robust to Drive re-uploads); ties broken
    by createdTime. FR / RUN-RECORD / longread / epoch / bound-volume are excluded
    by the strict ISSUE_RE match."""
    q = (f"'{WEEKLY_FOLDER_ID}' in parents and trashed=false "
         f"and name contains 'the-forward-pass-issue-' and mimeType='text/html'")
    files, page = [], None
    while True:
        res = drive.files().list(q=q, orderBy="createdTime desc", pageSize=100,
                                 pageToken=page,
                                 fields="nextPageToken, files(id,name,createdTime)").execute()
        files.extend(res.get("files", []))
        page = res.get("nextPageToken")
        if not page:
            break
    best = None  # (date, createdTime, issue_no, id, name)
    for f in files:
        m = ISSUE_RE.match(f["name"])
        if not m:
            continue  # skips *-fr.html, *-RUN-RECORD.md, COMMISSIONED-*, epoch, bound-volume
        d = datetime.date.fromisoformat(m.group(1))
        key = (d, f.get("createdTime", ""))
        if best is None or key > (best[0], best[1]):
            best = (d, f.get("createdTime", ""), int(m.group(2)), f["id"], f["name"])
    if best is None:
        sys.exit("No English weekly issue HTML found in the Drive folder.")
    print(f"Newest issue: {best[4]}  (issue {best[2]}, dated {best[0]})")
    return best[0], best[2], best[3], best[4]

def fetch_pod_script(drive, basename):
    """Prefer a purpose-built two-host script if present: <basename>_pod.txt
    (expected already tagged with 'A:' / 'B:' turn prefixes)."""
    name = f"{basename}_pod.txt"
    q = f"'{WEEKLY_FOLDER_ID}' in parents and trashed=false and name='{name}'"
    res = drive.files().list(q=q, orderBy="createdTime desc", pageSize=1,
                             fields="files(id,name)").execute()
    files = res.get("files", [])
    if not files:
        return None
    print(f"Using purpose-built weekly script: {name}")
    return _download(drive, files[0]["id"]).strip()

def html_to_text(html):
    html = re.sub(r"(?is)<style.*?</style>", " ", html)
    html = re.sub(r"(?is)<script.*?</script>", " ", html)
    html = re.sub(r"(?is)<svg.*?</svg>", " ", html)   # drop inline SVG diagrams (not spoken)
    text = re.sub(r"(?s)<[^>]+>", " ", html)
    text = text.replace("&amp;", "&").replace("&nbsp;", " ")
    text = re.sub(r"&[a-z]+;", " ", text)
    return re.sub(r"\s+", " ", text).strip()

# ---------- 2. Issue -> two-host script ----------
def make_script(issue_text, issue_no, date_label):
    system = (
        "You write audio scripts for 'The Forward Pass', an anti-hype AI newsletter for "
        "AI leaders (engineers, CAIOs, CEOs) in Belgium and abroad, published by NTT DATA "
        "Belgium's editor. House charter you MUST obey: no hype; BANNED words "
        "game-changer / revolutionize / unleash / supercharge; numbers over adjectives; "
        "every claim must come from the issue provided (invent NOTHING — no metrics, no "
        "quotes, no companies, no dates that aren't in the text); attribute company "
        "self-claims as claims, not facts.")
    prompt = f"""Turn this week's issue (Issue {issue_no}, {date_label}) of The Forward Pass
into a natural, engaging TWO-HOST podcast conversation of about {TARGET_MIN} minutes
(roughly {WORDS_LO}-{WORDS_HI} spoken words total).

The two hosts:
- {NAME_A} = the HOST/anchor. Warm, sharp, drives the show: cold open, framing, crisp
  transitions between topics, pulls the "so what for an AI leader" out of {NAME_B}, and
  closes the show.
- {NAME_B} = the ANALYST. Deeper technical and strategic read; explains mechanisms plainly,
  lands the numbers, gives the boardroom implication. Occasionally pushes back or adds the
  contrarian second opinion the issue raises.

Make it a real conversation — they respond to each other, not two monologues. Substantial
turns (roughly 50-90 words each), not rapid ping-pong; about 30-50 turns total.

Cover, with editorial judgement (go DEEP on the ~8-10 most decision-relevant threads for an
AI leader; do NOT try to mention every section):
- a cold open + the week's thesis (From the Editor);
- the leadership dashboard / signal board read (which signals moved and what it means for you);
- Brussels (EU) and Silicon Valley (US) and their transatlantic read;
- the strongest of the five desk leads (frontier, research, policy & ethics, industry &
  economics, adoption & enterprise);
- one real production deployment dissected (results AND gaps);
- AI economics (cost per successful task, unit economics) where the issue covers it;
- the Long Read thesis and the Second Opinion counterpoint;
- the Watchlist / the Reckoning (what was graded hit or miss);
- close with the concrete moves from The Monday Brief and a sign-off.

Delivery rules: conversational and authoritative; NO URLs; NO markdown; NO section numbers;
spell figures naturally ("about one point two trillion dollars", "roughly forty percent");
gloss any jargon in five words the first time. Open with {NAME_A} saying, in their own words,
that this is The Forward Pass Weekly for {date_label}. Close with a clear sign-off that the
full illustrated edition, with every source, is in the reader's inbox.

OUTPUT FORMAT — exactly this, nothing else:
First line:  SUMMARY: <one or two sentences describing this episode for the show notes>
Then the dialogue, one turn per line, each line starting with 'A: ' (for {NAME_A}) or
'B: ' (for {NAME_B}). No blank lines, no names other than inside the spoken text, no stage
directions.

ISSUE CONTENT:
{issue_text[:100000]}"""
    r = client.chat.completions.create(
        model=SCRIPT_MODEL,
        messages=[{"role": "system", "content": system},
                  {"role": "user", "content": prompt}],
        temperature=0.6)
    return r.choices[0].message.content.strip()

def parse_script(raw):
    """Return (summary, [(speaker, text), ...]). speaker is 'A' or 'B'."""
    summary = ""
    turns = []
    for line in raw.splitlines():
        line = line.strip()
        if not line:
            continue
        if not summary and line.upper().startswith("SUMMARY:"):
            summary = line.split(":", 1)[1].strip()
            continue
        m = re.match(r"^(?:HOST\s*)?([AB])\s*[:\-–]\s*(.+)$", line, re.I)
        if m:
            turns.append([m.group(1).upper(), m.group(2).strip()])
        elif turns:
            turns[-1][1] += " " + line  # continuation of the previous turn
    # merge accidental consecutive same-speaker turns
    merged = []
    for sp, tx in turns:
        if merged and merged[-1][0] == sp:
            merged[-1][1] += " " + tx
        else:
            merged.append([sp, tx])
    return summary, merged

# ---------- 3. Script -> MP3 (per-turn voices, then stitch) ----------
def synth_turn(text, voice, out_path):
    style = (
        "Deliver like a seasoned public-radio host in a two-person conversation — warm, "
        "engaged and natural, never flat or robotic. Vary pitch and pace; lift into a new "
        "point, land firm emphasis on key numbers and company names, ease down on the "
        "takeaway. Sound like you're genuinely talking WITH your co-host, not reading.")
    # TTS hard limit is 4096 chars; turns are ~<700 chars, but guard anyway.
    resp = client.audio.speech.create(
        model="gpt-4o-mini-tts", voice=voice, input=text[:4000],
        instructions=style, response_format="mp3")
    with open(out_path, "wb") as fh:
        fh.write(resp.content)

def build_mp3(turns, out_path):
    tmpdir = tempfile.mkdtemp(prefix="fpw_")
    parts = []
    for i, (sp, tx) in enumerate(turns):
        voice = VOICE_A if sp == "A" else VOICE_B
        p = os.path.join(tmpdir, f"seg_{i:03d}.mp3")
        synth_turn(tx, voice, p)
        parts.append(p)
        if (i + 1) % 10 == 0 or i == len(turns) - 1:
            print(f"  synth {i+1}/{len(turns)} turns")
    # stitch: prefer ffmpeg concat (clean single file); fall back to byte-concat.
    if shutil.which("ffmpeg"):
        listf = os.path.join(tmpdir, "list.txt")
        with open(listf, "w") as fh:
            for p in parts:
                fh.write(f"file '{p}'\n")
        try:
            subprocess.run(["ffmpeg", "-y", "-f", "concat", "-safe", "0",
                            "-i", listf, "-c", "copy", out_path],
                           check=True, capture_output=True)
        except subprocess.CalledProcessError:
            subprocess.run(["ffmpeg", "-y", "-f", "concat", "-safe", "0",
                            "-i", listf, "-c:a", "libmp3lame", "-b:a", "128k", out_path],
                           check=True, capture_output=True)
    else:
        with open(out_path, "wb") as out:
            for p in parts:
                with open(p, "rb") as fh:
                    out.write(fh.read())
    print(f"MP3 written: {out_path} ({os.path.getsize(out_path)//1024} KB)")

# ---------- 4. Transistor: dedup check + publish ----------
TB = "https://api.transistor.fm/v1"
H = {"x-api-key": TRANSISTOR_API_KEY}

def already_published(issue_no):
    """True if the show already has a weekly episode for this issue number."""
    needle = f"issue {issue_no}".lower()
    page = 1
    while True:
        r = requests.get(f"{TB}/episodes", headers=H,
                         params={"show_id": SHOW_ID, "pagination[page]": page,
                                 "pagination[per]": 50}).json()
        data = r.get("data", [])
        for ep in data:
            title = (ep.get("attributes", {}).get("title") or "").lower()
            if "weekly" in title and needle in title:
                print(f"Already published: '{ep['attributes']['title']}' (episode {ep['id']}).")
                return True
        meta = r.get("meta", {})
        if not data or page >= int(meta.get("totalPages", page)):
            return False
        page += 1

def publish(mp3_path, title, summary):
    fn = os.path.basename(mp3_path)
    au = requests.get(f"{TB}/episodes/authorize_upload", headers=H,
                      params={"filename": fn}).json()["data"]["attributes"]
    with open(mp3_path, "rb") as fh:
        requests.put(au["upload_url"], data=fh,
                     headers={"Content-Type": "audio/mpeg"}).raise_for_status()
    ep = requests.post(f"{TB}/episodes", headers=H, data={
        "episode[show_id]": SHOW_ID, "episode[title]": title,
        "episode[summary]": summary, "episode[audio_url]": au["audio_url"]}).json()
    eid = ep["data"]["id"]
    requests.patch(f"{TB}/episodes/{eid}/publish", headers=H,
                   data={"episode[status]": "published"}).raise_for_status()
    print(f"Published Transistor episode {eid}: {title}")

# ---------- main ----------
if __name__ == "__main__":
    drive = get_drive()
    d, issue_no, file_id, name = newest_issue(drive)
    basename = name[:-5] if name.lower().endswith(".html") else name

    # freshness guard
    try:
        from zoneinfo import ZoneInfo
        today = datetime.datetime.now(ZoneInfo("Europe/Brussels")).date()
    except Exception:
        today = datetime.date.today()
    age = (today - d).days
    if age > FRESH_DAYS and not FORCE:
        print(f"Newest issue is {age} days old (> {FRESH_DAYS}); skipping. Set FORCE=1 to override.")
        sys.exit(0)

    # dedup guard
    if not FORCE and already_published(issue_no):
        print("Skipping (episode already exists). Set FORCE=1 to override.")
        sys.exit(0)

    date_label = d.strftime("%A, %B %-d, %Y")

    # prefer a purpose-built script; else generate from the issue HTML
    raw = fetch_pod_script(drive, basename)
    if not raw:
        print("No purpose-built script on Drive; generating a two-host script from the issue.")
        raw = make_script(html_to_text(_download(drive, file_id)), issue_no, date_label)

    summary, turns = parse_script(raw)
    words = sum(len(t.split()) for _, t in turns)
    print(f"Script: {len(turns)} turns, ~{words} words "
          f"(~{round(words/150)} min at 150 wpm)")
    if len(turns) < 6:
        sys.exit("Parsed too few turns — aborting rather than publishing a broken episode.")

    mp3 = os.path.join(tempfile.gettempdir(), f"forwardpass_weekly_issue{issue_no}_{d.isoformat()}.mp3")
    build_mp3(turns, mp3)

    if not summary:
        summary = ("The Forward Pass Weekly: a two-host read of the week in AI for AI leaders. "
                   "Full illustrated edition and every source in your inbox.")
    publish(mp3,
            title=f"The Forward Pass Weekly — Issue {issue_no} — {d.strftime('%B %-d, %Y')}",
            summary=summary)
    print("Done.")
