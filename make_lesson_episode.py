#!/usr/bin/env python3
"""
Forward Pass — DAILY LESSON podcast episode maker (runs in GitHub Actions, open internet).

The audio companion to MONTESSORI's Daily Lesson (the ~20-25 min learning paper that teaches
the Daily Brief's Concept of the Day to AI champions). Same architecture as the other
runners (Drive -> GitHub Actions -> OpenAI TTS -> Transistor -> Spotify), but the episode is a
~12-15 minute TWO-VOICE TUTORIAL: voice A is the tutor, voice B is a curious AI champion who
asks the questions a listener would ask. Each part of the lesson is one chapter.

Flow:
  1. Find the newest lesson HTML in the Google Drive "Daily AI Lessons" folder
     (read-only, via the same service account as the daily/weekly).
     Naming: YYYY-MM-DD-lesson-NNN-<concept>.html   (NNN = concept number).
  2. Today-only guard (Europe/Brussels): skip if the newest lesson isn't today's, unless FORCE.
  3. Dedup guard: skip if the show already has an episode titled "... Lesson #N ...", unless FORCE.
     Safe to run several times a day (the workflow does, to survive late lessons and DST).
  4. Script: prefer MONTESSORI's purpose-built spoken script on Drive,
     <lesson-basename>_script.txt, in the special-edition format:
         TITLE: <episode title>          (optional)
         SUMMARY: <one or two sentences> (optional; used as the Transistor summary)
         ## CHAPTER: <chapter title>     (starts a new ID3 chapter)
         A: <tutor's line>
         B: <learner's line>
     If the script is missing:
       - before LESSON_FALLBACK_HOUR (Brussels, default 12) -> skip and wait for the next run
         (the lesson task writes the HTML a minute or two before the script);
       - from that hour on (or with FORCE) -> generate the two-voice script from the lesson
         HTML with OpenAI, so a day is never lost.
  5. Render with OpenAI TTS (one request per turn, each speaker in its own voice and delivery),
     stitch with ffmpeg, embed ID3 chapters (mutagen).
  6. Upload + publish to Transistor with a summary and an HTML description (chapter list with
     timestamps). Same show as the daily by default.

Env (GitHub Actions secrets/vars, shared with the other runners unless noted):
  OPENAI_API_KEY, TRANSISTOR_API_KEY, TRANSISTOR_SHOW_ID, GOOGLE_SA_JSON
  TRANSISTOR_SHOW_ID_LESSON  optional separate show (falls back to TRANSISTOR_SHOW_ID)
  LESSON_DRIVE_FOLDER_ID     optional; defaults to the "Daily AI Lessons" folder id below
                             (the folder must be shared Viewer with the service account)
  TTS_VOICE_A / TTS_VOICE_B  tutor / learner voices (defaults "marin" / "cedar", as the weekly)
  LESSON_TUTOR_NAME / LESSON_LEARNER_NAME  spoken names in a generated script (Maria / Sam)
  LESSON_MODEL               model for the fallback script (default gpt-4o)
  LESSON_TARGET_MINUTES      fallback script length (default 14; generated chapter by chapter)
  LESSON_FALLBACK_HOUR       see step 4 (default 12)
  FEEDBACK_EMAIL             default theforwardpasschannel@gmail.com
  FORCE                      "true" to bypass the today-only, dedup and wait-for-script guards
  OUT_DIR                    optional; where the MP3 is written (the workflow keeps it 14 days)

Deps: pip install openai google-api-python-client google-auth requests mutagen ; apt ffmpeg
"""
import os, sys, io, re, json, html, datetime, tempfile, subprocess
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
SHOW_ID            = os.environ.get("TRANSISTOR_SHOW_ID_LESSON") or os.environ["TRANSISTOR_SHOW_ID"]
LESSON_FOLDER_ID   = os.environ.get("LESSON_DRIVE_FOLDER_ID") or "1KzKPeDBOMe6fjAoIw-UctbhzVysKQ97e"
VOICE_A            = os.environ.get("TTS_VOICE_A") or "marin"     # the tutor
VOICE_B            = os.environ.get("TTS_VOICE_B") or "cedar"     # the curious champion
TUTOR_NAME         = os.environ.get("LESSON_TUTOR_NAME") or "Maria"
LEARNER_NAME       = os.environ.get("LESSON_LEARNER_NAME") or "Sam"
MODEL              = os.environ.get("LESSON_MODEL") or "gpt-4o"
TARGET_MIN         = int(os.environ.get("LESSON_TARGET_MINUTES") or 14)
FALLBACK_HOUR      = int(os.environ.get("LESSON_FALLBACK_HOUR") or 12)
FORCE              = os.environ.get("FORCE", "").lower() in ("1", "true", "yes")
FEEDBACK_EMAIL     = os.environ.get("FEEDBACK_EMAIL") or "theforwardpasschannel@gmail.com"
MAX_CHARS          = 1800          # short TTS requests keep the reading faithful (API max 4096)

LESSON_RE = re.compile(r"^(\d{4}-\d{2}-\d{2})-lesson-(\d{1,4})-(.+)\.html$", re.I)

client = OpenAI(api_key=OPENAI_API_KEY)
TB = "https://api.transistor.fm/v1"
H = {"x-api-key": TRANSISTOR_API_KEY}

STYLE = {
    "A": ("You are a warm, patient teacher explaining an idea to a smart colleague who is not a "
          "specialist. Clear, friendly and unhurried; land gentle emphasis on key terms and "
          "numbers; a short beat after each new idea so it can sink in. Never lecture-y or flat. "
          "Read the text exactly as written."),
    "B": ("You are a curious, quick-witted business professional learning something new and "
          "enjoying it. Natural, conversational, genuinely interested; questions rise at the end; "
          "reactions sound real, not scripted. Read the text exactly as written."),
}

def spoken_email(addr):
    local, _, domain = addr.partition("@")
    local = {"theforwardpasschannel": "the forward pass channel"}.get(local, local)
    return f"{local} at {domain.replace('.', ' dot ')}"

FEEDBACK_LINE = (f"Questions about today's lesson, or a concept you'd like us to teach next? Write "
                 f"to us at {spoken_email(FEEDBACK_EMAIL)}. We read every message.")

def brussels_now():
    try:
        from zoneinfo import ZoneInfo
        return datetime.datetime.now(ZoneInfo("Europe/Brussels"))
    except Exception:
        return datetime.datetime.utcnow()

# ---------- Drive ----------
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

def newest_lesson(drive):
    """Newest lesson by the DATE in its filename (robust to re-uploads), then concept number."""
    q = (f"'{LESSON_FOLDER_ID}' in parents and trashed=false "
         f"and name contains '-lesson-' and mimeType='text/html'")
    files, token = [], None
    while True:
        res = drive.files().list(q=q, pageSize=100, pageToken=token,
                                 fields="nextPageToken, files(id,name)").execute()
        files += res.get("files", [])
        token = res.get("nextPageToken")
        if not token:
            break
    cands = []
    for f in files:
        m = LESSON_RE.match(f["name"])
        if m:
            cands.append((m.group(1), int(m.group(2)), f))
    if not cands:
        sys.exit("No lesson HTML found in the Daily AI Lessons folder. Is it shared (Viewer) "
                 "with the service account?")
    date_s, num, f = max(cands, key=lambda c: (c[0], c[1]))
    print(f"Newest lesson: {f['name']}")
    return datetime.date.fromisoformat(date_s), num, f

def fetch_lesson_script(drive, basename):
    name = f"{basename}_script.txt"
    q = f"'{LESSON_FOLDER_ID}' in parents and trashed=false and name='{name}'"
    files = drive.files().list(q=q, orderBy="createdTime desc", pageSize=1,
                               fields="files(id,name)").execute().get("files", [])
    if not files:
        return None
    print(f"Using MONTESSORI's spoken script: {name}")
    return _download(drive, files[0]["id"])

# ---------- lesson HTML -> text ----------
def html_to_text(s):
    s = re.sub(r"(?is)<(style|script|svg)\b.*?</\1>", " ", s)
    s = re.sub(r"(?is)<(h[1-4]|p|li|dt|dd|tr|figcaption|summary)\b", r"\n<\1", s)
    s = re.sub(r"(?s)<[^>]+>", " ", s)
    s = html.unescape(s)
    s = re.sub(r"[ \t\r\f\v]+", " ", s)
    return re.sub(r"\n\s*", "\n", s).strip()

def lesson_meta(h):
    def grab(pat):
        m = re.search(pat, h, re.I | re.S)
        return html_to_text(m.group(1)) if m else ""
    return {
        "h1": grab(r"<h1[^>]*>(.*?)</h1>"),
        "dek": grab(r'<p class="dek"[^>]*>(.*?)</p>'),
        "eyebrow": grab(r'class="eyebrow"[^>]*>(.*?)</'),
        "title_tag": grab(r"<title>(.*?)</title>"),
    }

def lesson_body(h):
    """The teaching sections, without the sources list (numbered refs are not for the ear)."""
    body = re.split(r'(?i)<h2[^>]*id="s16"', h)[0]
    return html_to_text(body)

def concept_title(meta, slug):
    t = meta["title_tag"]
    m = re.search(r"Lesson\s*#\s*\d+\s*[—:-]\s*(.+)$", t)
    if m:
        return m.group(1).strip()
    return slug.replace("-", " ").strip().capitalize()

# ---------- script parsing (same format as the special editions) ----------
def parse_script(text):
    """-> (title, summary, [(chapter_title, [[speaker, text], ...]), ...])"""
    title = summary = ""
    chapters, cur = [], None
    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            continue
        if line.upper().startswith("TITLE:"):
            title = line.split(":", 1)[1].strip(); continue
        if line.upper().startswith("SUMMARY:"):
            summary = line.split(":", 1)[1].strip(); continue
        m = re.match(r"^##\s*CHAPTER:\s*(.+)$", line, re.I)
        if m:
            cur = (m.group(1).strip(), []); chapters.append(cur); continue
        m = re.match(r"^\**([AB])\**\s*:\s*(.+)$", line)
        if m:
            if cur is None:
                cur = ("The lesson", []); chapters.append(cur)
            cur[1].append([m.group(1), m.group(2).strip()])
        elif cur and cur[1]:
            cur[1][-1][1] += " " + line              # continuation line
    chapters = [(t, turns) for t, turns in chapters if turns]
    return title, summary, chapters

def ensure_feedback(chapters):
    """Every episode invites questions by email: if the script never mentions the address,
    the tutor says the feedback line just before the very last turn."""
    blob = " ".join(t for _, turns in chapters for _, t in turns).lower()
    if "gmail" in blob or "forward pass channel" in blob or not chapters:
        return chapters
    turns = chapters[-1][1]
    turns.insert(max(len(turns) - 1, 0), ["A", FEEDBACK_LINE])
    return chapters

def words_of(chapters):
    return sum(len(t.split()) for _, turns in chapters for _, t in turns)

# ---------- fallback: lesson HTML -> two-voice script ----------
# Generated ONE CHAPTER PER CALL and stitched (as the weekly does): asked for ~2,000 words in a
# single call, models stop at ~500 (the first real run, 2026-10-08, came out at ~3.5 minutes).
CHAPTER_PLAN = [
    ("Why this matters to you",
     "A concrete moment from an AI champion's working week where this concept decides something. "
     "B recognises the situation; A says what today's lesson will make clear (one sentence, no "
     "list of what's coming)."),
    ("The idea and the words you need",
     "The idea in one plain sentence, then in a short paragraph. Then the 4-6 words a listener "
     "needs, each glossed in speech with an everyday example; B checks understanding by saying one "
     "back in their own words."),
    ("How it works, step by step",
     "Walk through the mechanism step by step, describing the lesson's main picture in words so a "
     "listener can see it. Then the lesson's analogy and exactly where it breaks. B asks 'but why' "
     "at least once."),
    ("The worked example",
     "Go through the lesson's worked example slowly with its real numbers, said naturally; say "
     "which numbers are illustrative. B does part of the arithmetic or reasoning out loud."),
    ("In the wild, myths and where it breaks",
     "The lesson's real cases (what happened, why the concept explains it), two or three myths "
     "B has heard and A corrects, and the honest limits, risks and costs (what is established "
     "versus still debated)."),
    ("What to do with it",
     "Decisions this affects; two questions to ask a vendor or your team; the 'say it in a "
     "meeting' line; one check-yourself question that B answers aloud and A confirms; where to "
     "keep learning (name the 'start here' pick and one university course, in words, no URLs)."),
]

def _chapter_prompt(i, title, brief, per, meta, body, num, date_label, ctitle, prev_tail):
    first, last = i == 0, i == len(CHAPTER_PLAN) - 1
    opening = (f'Start with A saying exactly: "From The Forward Pass, this is the Daily Lesson for '
               f'{date_label}. Concept number {num}: {ctitle}."\n') if first else \
              "Do NOT greet or re-introduce the show; continue naturally from the previous chapter.\n"
    closing = (f'Near the end A says exactly: "{FEEDBACK_LINE}"\nThen end with A saying exactly: '
               f'"That\'s today\'s lesson. The full version, with every diagram and source, is in '
               f'your inbox."\n') if last else "Do NOT wrap up or say goodbye; the lesson continues.\n"
    cont = f"\nTHE PREVIOUS CHAPTER ENDED WITH:\n{prev_tail}\n" if prev_tail else ""
    return f"""You write chapter {i+1} of 6 of the audio edition of "The Daily Lesson" from The
Forward Pass: a two-voice tutorial teaching today's Concept of the Day to AI champions —
business and tech people who know the basics but are not data scientists. Voice A is
{TUTOR_NAME}, the tutor (warm, patient). Voice B is {LEARNER_NAME}, a curious AI champion who
asks what a listener would ask, pushes back once and sometimes sums up in their own words.
Teach — do not summarise, and do not repeat what earlier chapters already explained.

THIS CHAPTER: "{title}" — {brief}
LENGTH: about {per} words (at least {int(per*0.85)}), 10-20 turns.
{opening}{closing}FORMAT (strict): only lines "A: <text>" or "B: <text>", one turn per line. No
chapter heading, no blank speaker names, no stage directions, no markdown.
RULES: plain spoken English; gloss technical terms; say figures naturally ("about two dollars
per million words"); use only facts, numbers, cases and analogies that are in the lesson below —
invent nothing; no URLs, citations or section numbers; never name the publisher's employer.
{cont}
LESSON — {meta['eyebrow']}
TITLE: {meta['h1']}
DEK: {meta['dek']}

{body[:45000]}"""

def generate_script(meta, body, num, date_label, ctitle):
    per = int(TARGET_MIN * 150 / len(CHAPTER_PLAN))
    out, prev_tail = [f"SUMMARY: {meta['dek'] or ctitle}"], ""
    for i, (title, brief) in enumerate(CHAPTER_PLAN):
        text = ""
        for attempt in range(2):            # one retry if a chapter comes back far too short
            r = client.chat.completions.create(
                model=MODEL, temperature=0.5, max_tokens=min(4000, per * 3 + 400),
                messages=[{"role": "user", "content": _chapter_prompt(
                    i, title, brief, per, meta, body, num, date_label, ctitle, prev_tail)}])
            text = r.choices[0].message.content.strip()
            turns = [l for l in text.splitlines() if re.match(r"^\**[AB]\**\s*:", l.strip())]
            n = sum(len(l.split()) for l in turns)
            if n >= per * 0.6:
                break
            print(f"  chapter {i+1}: only {n} words, retrying")
        print(f"  chapter {i+1} '{title}': {n} words")
        out += [f"## CHAPTER: {title}"] + turns
        prev_tail = "\n".join(turns[-3:])
    return "\n".join(out)

# ---------- audio ----------
def merge_and_split(turns):
    merged = []
    for sp, tx in turns:
        if merged and merged[-1][0] == sp and len(merged[-1][1]) + len(tx) + 1 <= MAX_CHARS:
            merged[-1][1] += " " + tx
        else:
            merged.append([sp, tx])
    out = []
    for sp, tx in merged:
        if len(tx) <= MAX_CHARS:
            out.append([sp, tx]); continue
        buf = ""
        for s in re.split(r"(?<=[.!?])\s+", tx):
            if buf and len(buf) + len(s) + 1 > MAX_CHARS:
                out.append([sp, buf]); buf = s
            else:
                buf = f"{buf} {s}".strip()
        if buf:
            out.append([sp, buf])
    return out

def synth(text, speaker, path):
    voice = VOICE_A if speaker == "A" else VOICE_B
    for attempt in range(3):
        try:
            r = client.audio.speech.create(model="gpt-4o-mini-tts", voice=voice, input=text,
                                           instructions=STYLE[speaker], response_format="mp3")
            with open(path, "wb") as fh:
                fh.write(r.content)
            return
        except Exception as e:                      # transient API errors: retry
            print(f"  TTS retry {attempt+1}: {e}")
    raise RuntimeError("TTS failed 3 times")

def dur_ms(path):
    try:
        return int(MP3(path).info.length * 1000)
    except Exception:
        return 0

def build_mp3(chapters, out_path):
    tmp = tempfile.mkdtemp(prefix="fpl_")
    parts, marks, t, idx = [], [], 0, 0
    plan = [(title, merge_and_split(turns)) for title, turns in chapters]
    total = sum(len(x) for _, x in plan)
    for title, turns in plan:
        start = t
        for sp, tx in turns:
            p = os.path.join(tmp, f"t{idx:04d}.mp3"); idx += 1
            synth(tx, sp, p)
            parts.append(p); t += dur_ms(p)
            if idx % 10 == 0 or idx == total:
                print(f"  synth {idx}/{total}")
        marks.append([title, start, t])
    listf = os.path.join(tmp, "list.txt")
    with open(listf, "w") as fh:
        fh.writelines(f"file '{p}'\n" for p in parts)
    try:
        subprocess.run(["ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", listf, "-c", "copy",
                        out_path], check=True, capture_output=True)
    except (subprocess.CalledProcessError, FileNotFoundError):
        subprocess.run(["ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", listf,
                        "-c:a", "libmp3lame", "-b:a", "128k", out_path], check=True,
                       capture_output=True)
    real = dur_ms(out_path) or t
    marks[-1][2] = max(marks[-1][2], real)
    print(f"MP3: {out_path} ({os.path.getsize(out_path)//1024} KB, ~{round(real/60000)} min)")
    return marks

def embed_chapters(path, marks):
    try:
        tags = ID3(path)
    except ID3NoHeaderError:
        tags = ID3()
    ids = []
    for i, (title, s, e) in enumerate(marks):
        ids.append(f"chp{i}")
        tags.add(CHAP(element_id=f"chp{i}", start_time=int(s), end_time=int(e),
                      start_offset=0xFFFFFFFF, end_offset=0xFFFFFFFF,
                      sub_frames=[TIT2(encoding=3, text=[title])]))
    tags.add(CTOC(element_id="toc", flags=CTOCFlags.TOP_LEVEL | CTOCFlags.ORDERED,
                  child_element_ids=ids, sub_frames=[TIT2(encoding=3, text=["Chapters"])]))
    tags.save(path)

def ts(ms):
    s = int(ms // 1000); h, r = divmod(s, 3600); m, sec = divmod(r, 60)
    return f"{h}:{m:02d}:{sec:02d}" if h else f"{m}:{sec:02d}"

def description(summary, marks, meta):
    out = [f"<p>{html.escape(summary)}</p>"]
    if meta.get("eyebrow"):
        out.append(f"<p><em>{html.escape(meta['eyebrow'].title())}</em></p>")
    if marks:
        out.append("<p><strong>Chapters</strong></p>\n<ul>" + "".join(
            f"<li>{ts(s)} — {html.escape(t)}</li>" for t, s, e in marks) + "</ul>")
    out.append("<p>The Daily Lesson from The Forward Pass teaches the Concept of the Day to AI "
               "champions — no maths required. Read by two AI-generated voices. The full written "
               "lesson, with diagrams, exercises and every source, goes out by email.</p>")
    out.append(f"<p>Questions or a concept you'd like taught next? Write to "
               f"<a href=\"mailto:{FEEDBACK_EMAIL}\">{FEEDBACK_EMAIL}</a>.</p>")
    return "\n".join(out)

# ---------- Transistor ----------
def already_published(num):
    needle = re.compile(rf"lesson\s*#\s*{num}\b", re.I)
    page = 1
    while True:
        r = requests.get(f"{TB}/episodes", headers=H, params={
            "show_id": SHOW_ID, "pagination[page]": page, "pagination[per]": 50})
        r.raise_for_status()
        j = r.json()
        for ep in j.get("data", []):
            title = ep.get("attributes", {}).get("title") or ""
            if needle.search(title):
                print(f"Already on the show: '{title}' (episode {ep['id']}).")
                return True
        if not j.get("data") or page >= int(j.get("meta", {}).get("totalPages", page)):
            return False
        page += 1

def publish(mp3_path, title, summary, desc):
    au = requests.get(f"{TB}/episodes/authorize_upload", headers=H,
                      params={"filename": os.path.basename(mp3_path)})
    au.raise_for_status()
    a = au.json()["data"]["attributes"]
    with open(mp3_path, "rb") as fh:
        requests.put(a["upload_url"], data=fh,
                     headers={"Content-Type": "audio/mpeg"}).raise_for_status()
    r = requests.post(f"{TB}/episodes", headers=H, data={
        "episode[show_id]": SHOW_ID, "episode[title]": title,
        "episode[summary]": summary, "episode[description]": desc,
        "episode[audio_url]": a["audio_url"]})
    r.raise_for_status()
    eid = r.json()["data"]["id"]
    requests.patch(f"{TB}/episodes/{eid}/publish", headers=H,
                   data={"episode[status]": "published"}).raise_for_status()
    print(f"Published Transistor episode {eid}: {title}")

# ---------- main ----------
if __name__ == "__main__":
    drive = get_drive()
    d, num, f = newest_lesson(drive)
    basename = f["name"][:-5]
    now = brussels_now()
    if d != now.date() and not FORCE:
        print(f"Newest lesson is {d} (not today {now.date()}); skipping. Set FORCE=1 to override.")
        sys.exit(0)
    if not FORCE and already_published(num):
        print("Skipping (lesson episode already exists). Set FORCE=1 to override.")
        sys.exit(0)

    h = _download(drive, f["id"])
    meta = lesson_meta(h)
    slug = LESSON_RE.match(f["name"]).group(3)
    ctitle = concept_title(meta, slug)
    date_label = d.strftime("%A, %B %-d, %Y")

    raw = fetch_lesson_script(drive, basename)
    if not raw:
        if now.hour < FALLBACK_HOUR and not FORCE:
            print(f"No spoken script yet ({basename}_script.txt); MONTESSORI may still be writing. "
                  f"Skipping — a later run today will pick it up (fallback from {FALLBACK_HOUR}:00).")
            sys.exit(0)
        print("No spoken script on Drive; generating the two-voice script from the lesson HTML.")
        raw = generate_script(meta, lesson_body(h), num, date_label, ctitle)

    s_title, summary, chapters = parse_script(raw)
    chapters = ensure_feedback(chapters)
    n_words = words_of(chapters)
    print(f"{len(chapters)} chapters, {sum(len(t) for _, t in chapters)} turns, "
          f"{n_words} words (~{round(n_words/150)} min)")
    if n_words < 300:
        sys.exit("Script looks empty/broken — aborting.")
    if n_words < TARGET_MIN * 150 * 0.6:
        print(f"WARNING: script is short ({n_words} words, ~{round(n_words/150)} min) against a "
              f"~{TARGET_MIN}-min target; publishing anyway.")
    summary = summary or meta["dek"] or f"Concept #{num}: {ctitle}."

    out_dir = os.environ.get("OUT_DIR") or tempfile.gettempdir()
    os.makedirs(out_dir, exist_ok=True)
    mp3 = os.path.join(out_dir, f"forwardpass_lesson_{num:03d}_{d.isoformat()}.mp3")
    marks = build_mp3(chapters, mp3)
    embed_chapters(mp3, marks)
    title = f"Daily AI Lesson #{num} — {ctitle}"     # dedup key: "Lesson #N"
    publish(mp3, title, summary, description(summary, marks, meta))
    print("Done.")
