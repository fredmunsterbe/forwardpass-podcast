#!/usr/bin/env python3
"""
Forward Pass — WEEKLY podcast episode maker (runs in GitHub Actions, open internet).

The weekly companion to make_episode.py. Same architecture (Drive -> GitHub Actions
-> OpenAI TTS -> Transistor -> Spotify), but it turns the full weekly issue of
"The Forward Pass" into a ~20-minute TWO-HOST conversation, with an episode
description (show notes) and chapters.

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
  4. Turn the issue into a ~20-minute two-host script with OpenAI. The script is
     built in 5 SEGMENTS (open / bureaus / desks / deep / close) and stitched, so it
     reliably reaches full length; each segment also yields one CHAPTER. It honours
     the paper's anti-hype house charter. A purpose-built script on Drive
     (<issue-basename>_pod.txt) is preferred if present (no auto-chapters in that case).
  5. Render the script to MP3 with OpenAI TTS, one request per speaker turn using
     that speaker's voice, measure each segment's real duration, stitch into a single
     MP3 (ffmpeg), and embed ID3 chapter marks (mutagen).
  6. Upload + publish to Transistor with a title, a short summary, and a full HTML
     description that includes the chapter list with timestamps. Same show as the daily
     by default.

Env vars (GitHub Actions secrets, shared with the daily unless noted):
  OPENAI_API_KEY            - OpenAI key
  TRANSISTOR_API_KEY        - Transistor account API key
  TRANSISTOR_SHOW_ID        - the show to publish into (same show as the daily by default)
  TRANSISTOR_SHOW_ID_WEEKLY - optional; a separate weekly show. Falls back to TRANSISTOR_SHOW_ID.
  GOOGLE_SA_JSON            - full service-account JSON (must have Viewer on the weekly folder)
  WEEKLY_DRIVE_FOLDER_ID    - optional; defaults to the "Weekly AI Journal" folder id below.

Repo Variables (Settings > Variables), all optional with sensible defaults:
  TTS_VOICE_A / TTS_VOICE_B - host / analyst voices (defaults "marin" / "cedar")
  HOST_A_NAME / HOST_B_NAME - host / analyst names (defaults "Alex" / "Sam")
  WEEKLY_TARGET_MINUTES     - target length (default "20")
  WEEKLY_MODEL              - script model (default "gpt-4o")
  WEEKLY_FRESH_DAYS         - freshness window in days (default "6")

Deps:  pip install openai google-api-python-client google-auth requests mutagen
"""
import os, sys, io, re, json, html, datetime, tempfile, subprocess, shutil
import requests
from openai import OpenAI
from google.oauth2 import service_account
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseDownload
from mutagen.mp3 import MP3
from mutagen.id3 import ID3, CHAP, CTOC, TIT2, CTOCFlags, ID3NoHeaderError

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

TARGET_WORDS  = int(TARGET_MIN * 150)   # ~150 spoken words/min

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
    """(date, issue_no, file_id, name) for the newest EN weekly issue. Newest is by the
    DATE in the filename (robust to Drive re-uploads); FR / RUN-RECORD / longread / epoch /
    bound-volume are excluded by the strict ISSUE_RE match."""
    q = (f"'{WEEKLY_FOLDER_ID}' in parents and trashed=false "
         f"and name contains 'the-forward-pass-issue-' and mimeType='text/html'")
    files, page = [], None
    while True:
        res = drive.files().list(q=q, orderBy="createdTime desc", pageSize=100, pageToken=page,
                                 fields="nextPageToken, files(id,name,createdTime)").execute()
        files.extend(res.get("files", []))
        page = res.get("nextPageToken")
        if not page:
            break
    best = None
    for f in files:
        m = ISSUE_RE.match(f["name"])
        if not m:
            continue
        d = datetime.date.fromisoformat(m.group(1))
        key = (d, f.get("createdTime", ""))
        if best is None or key > (best[0], best[1]):
            best = (d, f.get("createdTime", ""), int(m.group(2)), f["id"], f["name"])
    if best is None:
        sys.exit("No English weekly issue HTML found in the Drive folder.")
    print(f"Newest issue: {best[4]}  (issue {best[2]}, dated {best[0]})")
    return best[0], best[2], best[3], best[4]

def fetch_pod_script(drive, basename):
    """Prefer a purpose-built script if present: <basename>_pod.txt (tagged 'A:' / 'B:')."""
    name = f"{basename}_pod.txt"
    q = f"'{WEEKLY_FOLDER_ID}' in parents and trashed=false and name='{name}'"
    res = drive.files().list(q=q, orderBy="createdTime desc", pageSize=1,
                             fields="files(id,name)").execute()
    files = res.get("files", [])
    if not files:
        return None
    print(f"Using purpose-built weekly script: {name}")
    return _download(drive, files[0]["id"]).strip()

def html_to_text(html_s):
    html_s = re.sub(r"(?is)<style.*?</style>", " ", html_s)
    html_s = re.sub(r"(?is)<script.*?</script>", " ", html_s)
    html_s = re.sub(r"(?is)<svg.*?</svg>", " ", html_s)   # drop inline SVG diagrams (not spoken)
    text = re.sub(r"(?s)<[^>]+>", " ", html_s)
    text = text.replace("&amp;", "&").replace("&nbsp;", " ")
    text = re.sub(r"&[a-z]+;", " ", text)
    return re.sub(r"\s+", " ", text).strip()

# ---------- 2. Issue -> two-host script (SEGMENTED; each segment = one chapter) ----------
SYSTEM = (
    "You write audio scripts for 'The Forward Pass', an anti-hype AI newsletter for AI leaders "
    "(engineers, CAIOs, CEOs) in Belgium and abroad, published by NTT DATA Belgium's editor. "
    "House charter you MUST obey: no hype; BANNED words game-changer / revolutionize / unleash / "
    "supercharge; numbers over adjectives; every claim must come from the issue provided (invent "
    "NOTHING — no metrics, no quotes, no companies, no dates that aren't in the text); attribute "
    "company self-claims as claims, not facts.")

# (default chapter title, topics, weight-of-total-words, is_first, is_last)
SEGMENTS = [
    ("The week's thesis & the dashboard",
     "Open the show — {A} welcomes listeners to The Forward Pass Weekly for {date}, then the two of "
     "you lay out the week's thesis (the editor's note) and read the leadership dashboard / signal "
     "board: which directional signals moved, and what each one means for an AI leader.",
     0.20, True, False),
    ("Brussels & Silicon Valley",
     "The Brussels (EU) desk and the Silicon Valley (US) desk, and the transatlantic read between "
     "them: where model capability and regulatory obligation are each heading, and who they "
     "ultimately land on.",
     0.20, False, False),
    ("The desks that mattered",
     "The strongest desk leads of the week across frontier models, research, policy & ethics, "
     "industry & economics, and enterprise adoption — go deep on the two or three that matter most "
     "for a leader rather than listing all of them.",
     0.22, False, False),
    ("Deployment, economics & the Long Read",
     "One real production deployment dissected (the results AND the gaps), the AI-economics angle "
     "(cost per successful task, unit economics) where the issue covers it, then the Long Read's "
     "central thesis and the Second Opinion's counterpoint.",
     0.22, False, False),
    ("The Watchlist & the Monday Brief",
     "The Watchlist and the Reckoning (which prior calls were graded a hit or a miss), then close "
     "on the concrete moves from The Monday Brief and a clear sign-off that the full illustrated "
     "edition, with every source, is in the reader's inbox.",
     0.16, False, True),
]

def _segment_prompt(i, deftitle, topics, seg_words, is_first, is_last, issue_no, date_label, body):
    topics = topics.format(A=NAME_A, date=date_label)
    pos = ("This is the OPENING of the episode." if is_first else
           "This CONTINUES an in-progress conversation — do NOT re-introduce the hosts or re-welcome "
           "listeners; pick up naturally from where the last topic left off.")
    endr = ("End the WHOLE episode here with a clear sign-off." if is_last else
            "Do NOT sign off or say goodbye — more of the show follows.")
    header = 'Begin with one line "TITLE: <a specific 3-6 word chapter title for this segment>".'
    if is_first:
        header += ('\nThen one line "SUMMARY: <one or two sentences describing the whole episode for '
                   'the show notes>".')
    return f"""You are writing SEGMENT {i+1} of {len(SEGMENTS)} of a SINGLE continuous
~{TARGET_MIN}-minute two-host podcast episode for The Forward Pass Weekly (Issue {issue_no},
{date_label}).

The two hosts: {NAME_A} = the host/anchor; {NAME_B} = the analyst (deeper technical and strategic
read, lands the numbers, gives the boardroom implication, and sometimes pushes back).

THIS SEGMENT covers ONLY: {topics}

Write about {seg_words} words of natural back-and-forth for THIS segment — substantial turns of
roughly 60-90 words each (about {max(6, seg_words//75)} turns), a real conversation in which they
respond to each other, not two monologues. {pos} {endr}

Rules: conversational and authoritative; obey the house charter (invent nothing — only what's in
the issue below; no hype/banned words; numbers over adjectives; attribute company self-claims);
NO URLs, NO markdown, NO section numbers; spell figures naturally ("about one point two trillion
dollars"); gloss any jargon in five words on first use.
{header}
Then the dialogue, EACH turn on its own line, starting with exactly 'A: ' (for {NAME_A}) or 'B: '
(for {NAME_B}). Use ONLY 'A:' and 'B:' as line prefixes — never names — and alternate speakers.

ISSUE CONTENT:
{body}"""

def generate_segments(issue_text, issue_no, date_label):
    """Return (summary, [(chapter_title, [ [speaker, text], ... ]), ...])."""
    body = issue_text[:90000]
    summary = ""
    segs = []
    for i, (deftitle, topics, weight, is_first, is_last) in enumerate(SEGMENTS):
        seg_words = max(300, round(TARGET_WORDS * weight))
        prompt = _segment_prompt(i, deftitle, topics, seg_words, is_first, is_last,
                                 issue_no, date_label, body)
        r = client.chat.completions.create(
            model=SCRIPT_MODEL,
            messages=[{"role": "system", "content": SYSTEM}, {"role": "user", "content": prompt}],
            temperature=0.6, max_tokens=1800)
        txt = r.choices[0].message.content.strip()
        title = deftitle
        for line in txt.splitlines():
            u = line.strip()
            if u.upper().startswith("TITLE:"):
                title = (u.split(":", 1)[1].strip() or deftitle)[:70]
            if is_first and not summary and u.upper().startswith("SUMMARY:"):
                summary = u.split(":", 1)[1].strip()
        turns = parse_turns(txt, min_turns=4)
        wc = sum(len(t.split()) for _, t in turns)
        print(f"  segment {i+1}/{len(SEGMENTS)} '{title}': {len(turns)} turns, ~{wc} words")
        if turns:
            segs.append((title, turns))
    if not summary:
        summary = ("The week in AI for AI leaders — the signals that moved, Brussels and Silicon "
                   "Valley, the desks that mattered, and the moves to make.")
    return summary, segs

def parse_turns(text, min_turns=4):
    """Parse tagged dialogue into [[speaker,text],...] with speaker in {'A','B'}. Recognises
    'A:'/'B:'/'HOST A:'/host names; if the strict parse is short (tag drift), re-parses
    permissively (any 'Word: text' line, alternating), so length is never lost."""
    name_a, name_b = NAME_A.strip().lower(), NAME_B.strip().lower()
    known = {"a": "A", "b": "B", "host a": "A", "host b": "B",
             "hosta": "A", "hostb": "B", name_a: "A", name_b: "B"}
    tag_re = re.compile(r"^\**\s*([A-Za-z][A-Za-z ]{0,14})\**\s*[:–-]\s*(.+)$")

    def run(strict):
        turns, last = [], "B"
        for line in text.splitlines():
            line = line.strip()
            if not line:
                continue
            up = line.upper()
            if up.startswith("SUMMARY:") or up.startswith("TITLE:"):
                continue
            m = tag_re.match(line)
            sp = None
            if m:
                sp = known.get(m.group(1).strip().lower())
                if sp is None and not strict:
                    sp = "A" if last == "B" else "B"
            if sp:
                turns.append([sp, m.group(2).strip()]); last = sp
            elif turns:
                turns[-1][1] += " " + line
        merged = []
        for sp, tx in turns:
            if merged and merged[-1][0] == sp:
                merged[-1][1] += " " + tx
            else:
                merged.append([sp, tx])
        return merged

    t = run(strict=True)
    if len(t) < min_turns:
        a = run(strict=False)
        if len(a) > len(t):
            t = a
    return t

def extract_summary(raw):
    for line in raw.splitlines():
        if line.strip().upper().startswith("SUMMARY:"):
            return line.split(":", 1)[1].strip()
    return ""

# ---------- 3. Script -> MP3 (per-turn voices), measure chapters, stitch, embed ----------
def synth_turn(text, voice, out_path):
    style = (
        "Deliver like a seasoned public-radio host in a two-person conversation — warm, engaged "
        "and natural, never flat or robotic. Vary pitch and pace; lift into a new point, land firm "
        "emphasis on key numbers and company names, ease down on the takeaway. Sound like you're "
        "genuinely talking WITH your co-host, not reading.")
    resp = client.audio.speech.create(
        model="gpt-4o-mini-tts", voice=voice, input=text[:4000],
        instructions=style, response_format="mp3")
    with open(out_path, "wb") as fh:
        fh.write(resp.content)

def _dur_ms(path):
    try:
        return int(MP3(path).info.length * 1000)
    except Exception:
        return 0

def build_mp3(segments, out_path):
    """Synthesise every turn, measuring each segment's duration; stitch to one MP3.
    Returns chapters = [[title, start_ms, end_ms], ...] (empty for untitled segments)."""
    tmpdir = tempfile.mkdtemp(prefix="fpw_")
    parts, chapters = [], []
    t_ms, idx = 0, 0
    total_turns = sum(len(t) for _, t in segments)
    for title, turns in segments:
        seg_start = t_ms
        for sp, tx in turns:
            voice = VOICE_A if sp == "A" else VOICE_B
            p = os.path.join(tmpdir, f"seg_{idx:03d}.mp3"); idx += 1
            synth_turn(tx, voice, p)
            parts.append(p)
            t_ms += _dur_ms(p)
            if idx % 10 == 0 or idx == total_turns:
                print(f"  synth {idx}/{total_turns} turns")
        if title and title != "__nochapter__":
            chapters.append([title, seg_start, t_ms])
    # stitch: prefer ffmpeg concat (clean single file); fall back to byte-concat.
    if shutil.which("ffmpeg"):
        listf = os.path.join(tmpdir, "list.txt")
        with open(listf, "w") as fh:
            for p in parts:
                fh.write(f"file '{p}'\n")
        try:
            subprocess.run(["ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", listf,
                            "-c", "copy", out_path], check=True, capture_output=True)
        except subprocess.CalledProcessError:
            subprocess.run(["ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", listf,
                            "-c:a", "libmp3lame", "-b:a", "128k", out_path], check=True,
                           capture_output=True)
    else:
        with open(out_path, "wb") as out:
            for p in parts:
                with open(p, "rb") as fh:
                    out.write(fh.read())
    total = _dur_ms(out_path) or t_ms
    if chapters:
        chapters[-1][2] = max(chapters[-1][2], total)   # clamp last chapter to real end
    print(f"MP3 written: {out_path} ({os.path.getsize(out_path)//1024} KB, ~{round(total/60000)} min)")
    return chapters

def embed_chapters(path, chapters):
    """Embed ID3 CHAP/CTOC chapter frames so podcast apps (and Transistor) show chapters."""
    if not chapters:
        return
    try:
        tags = ID3(path)
    except ID3NoHeaderError:
        tags = ID3()
    ids = []
    for i, (title, start_ms, end_ms) in enumerate(chapters):
        cid = f"chp{i}"
        ids.append(cid)
        tags.add(CHAP(element_id=cid, start_time=int(start_ms), end_time=int(end_ms),
                      start_offset=0xFFFFFFFF, end_offset=0xFFFFFFFF,
                      sub_frames=[TIT2(encoding=3, text=[title])]))
    tags.add(CTOC(element_id="toc", flags=CTOCFlags.TOP_LEVEL | CTOCFlags.ORDERED,
                  child_element_ids=ids, sub_frames=[TIT2(encoding=3, text=["Chapters"])]))
    tags.save(path)
    print(f"Embedded {len(chapters)} chapters.")

def fmt_ts(ms):
    s = int(ms // 1000)
    h, rem = divmod(s, 3600)
    m, sec = divmod(rem, 60)
    return f"{h}:{m:02d}:{sec:02d}" if h else f"{m}:{sec:02d}"

def build_description(summary, chapters, issue_no, date_label):
    parts = [f"<p>{html.escape(summary)}</p>"]
    if chapters:
        items = "".join(f"<li>{fmt_ts(s)} — {html.escape(t)}</li>" for t, s, e in chapters)
        parts.append("<p><strong>In this episode</strong></p>\n<ul>" + items + "</ul>")
    parts.append(
        f"<p>The full illustrated edition of Issue {issue_no} ({html.escape(date_label)}), with "
        f"every source, is in your inbox. This is an AI-generated audio edition of The Forward Pass "
        f"— an anti-hype read on the week in AI for AI leaders.</p>")
    return "\n".join(parts)

# ---------- 4. Transistor: dedup check + publish ----------
TB = "https://api.transistor.fm/v1"
H = {"x-api-key": TRANSISTOR_API_KEY}

def already_published(issue_no):
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

def publish(mp3_path, title, summary, description):
    fn = os.path.basename(mp3_path)
    au = requests.get(f"{TB}/episodes/authorize_upload", headers=H,
                      params={"filename": fn}).json()["data"]["attributes"]
    with open(mp3_path, "rb") as fh:
        requests.put(au["upload_url"], data=fh,
                     headers={"Content-Type": "audio/mpeg"}).raise_for_status()
    ep = requests.post(f"{TB}/episodes", headers=H, data={
        "episode[show_id]": SHOW_ID, "episode[title]": title,
        "episode[summary]": summary, "episode[description]": description,
        "episode[audio_url]": au["audio_url"]}).json()
    eid = ep["data"]["id"]
    requests.patch(f"{TB}/episodes/{eid}/publish", headers=H,
                   data={"episode[status]": "published"}).raise_for_status()
    print(f"Published Transistor episode {eid}: {title}")

# ---------- main ----------
if __name__ == "__main__":
    drive = get_drive()
    d, issue_no, file_id, name = newest_issue(drive)
    basename = name[:-5] if name.lower().endswith(".html") else name

    try:
        from zoneinfo import ZoneInfo
        today = datetime.datetime.now(ZoneInfo("Europe/Brussels")).date()
    except Exception:
        today = datetime.date.today()
    age = (today - d).days
    if age > FRESH_DAYS and not FORCE:
        print(f"Newest issue is {age} days old (> {FRESH_DAYS}); skipping. Set FORCE=1 to override.")
        sys.exit(0)

    if not FORCE and already_published(issue_no):
        print("Skipping (episode already exists). Set FORCE=1 to override.")
        sys.exit(0)

    date_label = d.strftime("%A, %B %-d, %Y")

    raw_pod = fetch_pod_script(drive, basename)
    if raw_pod:
        # purpose-built script: one block, no auto-chapters (add them by hand if desired)
        summary = extract_summary(raw_pod) or (
            "The Forward Pass Weekly — a two-host read of the week in AI for AI leaders.")
        segments = [("__nochapter__", parse_turns(raw_pod, min_turns=12))]
    else:
        print("No purpose-built script on Drive; generating a two-host script from the issue.")
        summary, segments = generate_segments(html_to_text(_download(drive, file_id)),
                                              issue_no, date_label)

    turns_total = sum(len(t) for _, t in segments)
    words = sum(len(x.split()) for _, t in segments for _, x in t)
    print(f"Script: {turns_total} turns, ~{words} words (~{round(words/150)} min at 150 wpm)")
    if turns_total < 6:
        sys.exit("Parsed too few turns — aborting rather than publishing a broken episode.")

    mp3 = os.path.join(tempfile.gettempdir(),
                       f"forwardpass_weekly_issue{issue_no}_{d.isoformat()}.mp3")
    chapters = build_mp3(segments, mp3)
    embed_chapters(mp3, chapters)

    description = build_description(summary, chapters, issue_no, date_label)
    publish(mp3,
            title=f"The Forward Pass Weekly — Issue {issue_no} — {d.strftime('%B %-d, %Y')}",
            summary=summary, description=description)
    print("Done.")
