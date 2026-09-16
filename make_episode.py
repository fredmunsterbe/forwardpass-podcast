#!/usr/bin/env python3
"""
Forward Pass — daily podcast episode maker (runs in GitHub Actions, open internet).

Flow:
  1. Find the newest AI_Daily_Brief_*.html in the Google Drive "Daily AI Brief" folder
     (read-only, via a Google service account).
  2. Turn it into a spoken ~6-minute script with OpenAI (news-anchor style, no URLs).
  3. Render the script to MP3 with OpenAI TTS (chunked to respect the 4096-char limit).
  4. Upload + publish the episode to Transistor (which feeds Spotify via RSS).

Env vars (set as GitHub Actions secrets):
  OPENAI_API_KEY        - your (rotated) OpenAI key
  TRANSISTOR_API_KEY    - Transistor > Account > Your API key
  TRANSISTOR_SHOW_ID    - numeric/string id of your show
  DRIVE_FOLDER_ID       - the "Daily AI Brief" folder id (1v2w8Q56LpXPAmi3gXqwmaX6NVgBr3z00)
  GOOGLE_SA_JSON        - the full service-account JSON (paste as a secret)
  TTS_VOICE             - optional, defaults to "onyx"
Deps:  pip install openai google-api-python-client google-auth requests
"""
import os, sys, io, re, json, datetime, tempfile
import requests
from openai import OpenAI
from google.oauth2 import service_account
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseDownload

OPENAI_API_KEY     = os.environ["OPENAI_API_KEY"]
TRANSISTOR_API_KEY = os.environ["TRANSISTOR_API_KEY"]
SHOW_ID            = os.environ["TRANSISTOR_SHOW_ID"]
DRIVE_FOLDER_ID    = os.environ["DRIVE_FOLDER_ID"]
VOICE              = os.environ.get("TTS_VOICE", "onyx")

client = OpenAI(api_key=OPENAI_API_KEY)

# ---------- 1. Pull the newest brief from Drive ----------
def newest_brief_html():
    creds = service_account.Credentials.from_service_account_info(
        json.loads(os.environ["GOOGLE_SA_JSON"]),
        scopes=["https://www.googleapis.com/auth/drive.readonly"])
    drive = build("drive", "v3", credentials=creds)
    q = (f"'{DRIVE_FOLDER_ID}' in parents and trashed=false "
         f"and name contains 'AI_Daily_Brief_' and mimeType='text/html'")
    res = drive.files().list(q=q, orderBy="createdTime desc", pageSize=1,
                             fields="files(id,name)").execute()
    files = res.get("files", [])
    if not files:
        sys.exit("No brief HTML found in the Drive folder.")
    f = files[0]
    buf = io.BytesIO()
    dl = MediaIoBaseDownload(buf, drive.files().get_media(fileId=f["id"]))
    done = False
    while not done:
        _, done = dl.next_chunk()
    print(f"Fetched {f['name']}")
    return f["name"], buf.getvalue().decode("utf-8", "ignore")

def html_to_text(html):
    html = re.sub(r"(?is)<style.*?</style>", " ", html)
    html = re.sub(r"(?is)<script.*?</script>", " ", html)
    text = re.sub(r"(?s)<[^>]+>", " ", html)
    text = re.sub(r"&amp;", "&", text); text = re.sub(r"&[a-z]+;", " ", text)
    return re.sub(r"\s+", " ", text).strip()

# ---------- 2. Brief -> spoken script ----------
def make_script(brief_text, date_label):
    prompt = f"""You are the voice of "The Forward Pass — AI Daily Brief", a ~6 minute
daily audio brief for NTT DATA Belgium. Turn the brief below into a spoken script.
Rules: conversational but authoritative news-anchor tone; NO URLs, NO citations,
NO markdown, no section numbers; spell figures naturally ("about 1.2 trillion dollars");
open with "From The Forward Pass, this is your AI Daily Brief for {date_label}.";
cover the fresh items and the NTT DATA Belgium takeaway; close with
"That's your brief. The full report, with every source, is in your inbox." Keep it
to roughly 850-950 words. Output ONLY the script text.

BRIEF:
{brief_text[:12000]}"""
    r = client.chat.completions.create(
        model="gpt-4o", messages=[{"role": "user", "content": prompt}],
        temperature=0.4)
    return r.choices[0].message.content.strip()

# ---------- 3. Script -> MP3 (chunked) ----------
def chunk(text, limit=3800):
    out, cur = [], ""
    for para in text.split("\n"):
        if len(cur) + len(para) + 1 > limit:
            if cur: out.append(cur); cur = ""
        cur += para + "\n"
    if cur.strip(): out.append(cur)
    return out

def synth(script, out_path):
    with open(out_path, "wb") as fh:
        for i, part in enumerate(chunk(script)):
            resp = client.audio.speech.create(
                model="gpt-4o-mini-tts", voice=VOICE, input=part,
                instructions="Crisp, warm financial-news anchor. Measured pace, clear enunciation.",
                response_format="mp3")
            fh.write(resp.content)
            print(f"  synth chunk {i+1} ok")
    print(f"MP3 written: {out_path} ({os.path.getsize(out_path)//1024} KB)")

# ---------- 4. Upload + publish to Transistor ----------
TB = "https://api.transistor.fm/v1"
H = {"x-api-key": TRANSISTOR_API_KEY}

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
    name, html = newest_brief_html()
    m = re.search(r"(\d{4}-\d{2}-\d{2})", name)
    d = datetime.date.fromisoformat(m.group(1)) if m else datetime.date.today()
    date_label = d.strftime("%A, %B %-d, %Y")
    script = make_script(html_to_text(html), date_label)
    print(f"Script: {len(script.split())} words")
    mp3 = os.path.join(tempfile.gettempdir(), f"forwardpass_{d.isoformat()}.mp3")
    synth(script, mp3)
    publish(mp3,
            title=f"AI Daily Brief — {date_label}",
            summary="The Forward Pass Fresh Desk: net-new AI, macro and Belgium, read for NTT DATA Belgium. Full report and sources by email.")
    print("Done.")
