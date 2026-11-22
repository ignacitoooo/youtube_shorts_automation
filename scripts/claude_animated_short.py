#!/usr/bin/env python3
"""Claude-authored cartoon Short, locally rendered with mandatory narration/audio."""
import html
import json
import os
import re
import subprocess
import time
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET
from pathlib import Path

import cairosvg
import edge_tts
from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "artifacts" / "claude_short"
HISTORY = ROOT / "data" / "animated_short_history.json"
MODEL = os.getenv("CLAUDE_MODEL", "claude-sonnet-5-5")
VOICE = os.getenv("CLAUDE_TTS_VOICE", "en-US-GuyNeural")
W, H = 720, 1280
FEEDS = [
    ("NIH", "https://www.nih.gov/news-events/news-releases/rss.xml"),
    ("ScienceDaily", "https://www.sciencedaily.com/rss/health_medicine.xml"),
    ("NPR Health", "https://feeds.npr.org/1128/rss.xml"),
]

def run(cmd):
    subprocess.run(cmd, check=True)

def get_sources():
    found = []
    for label, url in FEEDS:
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "TheHealthyYears/1.0"})
            with urllib.request.urlopen(req, timeout=25) as response:
                root = ET.fromstring(response.read())
            for item in root.findall(".//item")[:20]:
                title = html.unescape((item.findtext("title") or "").strip())
                link = (item.findtext("link") or "").strip()
                desc = re.sub(r"<[^>]+>", " ", item.findtext("description") or "")
                if title and link:
                    found.append({"publisher": label, "title": title, "url": link, "summary": desc[:500]})
        except Exception as exc:
            print(f"Source feed warning ({label}): {exc}")
    if len(found) < 3:
        raise RuntimeError("Could not retrieve enough source articles; animated Short cancelled.")
    return found[:45]

def claude_storyboard(sources, previous_titles):
    key = os.environ["ANTHROPIC_API_KEY"]
    prompt = f"""
Create one original, evidence-based YouTube Short for The Healthy Years, an English-language healthy-aging channel for adults 50+.
Use only the supplied article facts and cite one or more exact supplied URLs. Do not diagnose, promise cures, invent statistics,
or give medication advice. Make it a human-centered mini-story starring John: a friendly 65-year-old man with grey hair,
blue shirt and brown trousers. A human trainer/nutritionist may appear. Never personify organs.
Visual style: 2D illustrated human cartoon, thick dark outlines, expressive faces, colorful simple environments, clear visual
storytelling, not photorealistic, not 3D medical CGI, no stock-footage instructions.
Return JSON only with keys: topic,title,description,tags,source_urls,scenes.
Exactly 8 scenes. Each scene has: narration (natural spoken English), caption (max 7 words), setting (one of stairs,home,
park,gym,kitchen,clinic), action (short visual instruction), emotion (one of worried,curious,hopeful,confident).
Narration total must be 75-95 words, designed for 35-40 seconds. Scene 1 must be a compelling question/problem in first 2 seconds.
Scene 8 must resolve the opening story and include a natural follow CTA. No repeated topic from this list:
{json.dumps(previous_titles)}
Supplied research articles:
{json.dumps(sources, ensure_ascii=False)}
"""
    payload = {"model": MODEL, "max_tokens": 3500, "temperature": 0.5,
               "system": "You are a careful health editor and cartoon-short showrunner. Return valid JSON only.",
               "messages": [{"role": "user", "content": prompt}]}
    request = urllib.request.Request(
        "https://api.anthropic.com/v1/messages",
        data=json.dumps(payload).encode(),
        headers={"content-type": "application/json", "x-api-key": key,
                 "anthropic-version": "2026-10-21"},
        method="POST",
    )
    for attempt in range(5):
        try:
            with urllib.request.urlopen(request, timeout=240) as response:
                data = json.loads(response.read())
            text = "".join(x.get("text", "") for x in data.get("content", []) if x.get("type") == "text")
            start, end = text.find("{"), text.rfind("}")
            if start < 0 or end <= start:
                raise RuntimeError("Claude did not return a JSON object.")
            return json.loads(text[start:end + 1])
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode(errors="replace")
            if exc.code not in (408, 429, 500, 502, 503, 504) or attempt == 4:
                raise RuntimeError(f"Claude API HTTP {exc.code}: {detail[:1000]}") from exc
            wait = min(60, 8 * (2 ** attempt))
            print(f"Claude temporary HTTP {exc.code}; retrying in {wait}s.")
            time.sleep(wait)
    raise RuntimeError("Claude API failed after retries.")

PALETTES = {
    "stairs": ("#dcecff", "#b7d9ff"), "home": ("#fff0d9", "#f6d5a8"),
    "park": ("#dff4dc", "#a9d99f"), "gym": ("#e8e2ff", "#c7b8ff"),
    "kitchen": ("#fff0e6", "#ffd0ad"), "clinic": ("#e4f3f4", "#b8dfe2"),
}

def esc(s):
    return html.escape(str(s or ""))[:160]

def person(x, y, scale=1.0, trainer=False, pose="standing"):
    shirt = "#e59b45" if trainer else "#3478c8"
    hair = "#55545a" if not trainer else "#4b3028"
    head = f'<circle cx="{x}" cy="{y}" r="{36*scale}" fill="#f1c49e" stroke="#252535" stroke-width="7"/>'
    eyes = (f'<circle cx="{x-12*scale}" cy="{y-4*scale}" r="3.8" fill="#252535"/>'
            f'<circle cx="{x+12*scale}" cy="{y-4*scale}" r="3.8" fill="#252535"/>')
    smile = f'<path d="M{x-12*scale} {y+12*scale} Q{x} {y+23*scale} {x+13*scale} {y+10*scale}" fill="none" stroke="#252535" stroke-width="4" stroke-linecap="round"/>'
    hairshape = f'<path d="M{x-35*scale} {y-8*scale} Q{x-35*scale} {y-53*scale} {x+8*scale} {y-42*scale} Q{x+34*scale} {y-39*scale} {x+35*scale} {y-12*scale} Q{x+8*scale} {y-30*scale} {x-35*scale} {y-8*scale}" fill="{hair}"/>'
    torso_y = y + 42*scale
    body = f'<path d="M{x} {torso_y} L{x} {torso_y+125*scale}" stroke="{shirt}" stroke-width="{62*scale}" stroke-linecap="round"/>'
    arms = f'<path d="M{x-5*scale} {torso_y+25*scale} L{x-58*scale} {torso_y+92*scale} M{x+5*scale} {torso_y+25*scale} L{x+60*scale} {torso_y+88*scale}" stroke="#f1c49e" stroke-width="{18*scale}" stroke-linecap="round"/>'
    legs = f'<path d="M{x} {torso_y+125*scale} L{x-34*scale} {torso_y+220*scale} M{x} {torso_y+125*scale} L{x+35*scale} {torso_y+220*scale}" stroke="#795b45" stroke-width="{23*scale}" stroke-linecap="round"/>'
    shoes = f'<path d="M{x-49*scale} {torso_y+222*scale} h34 M{x+20*scale} {torso_y+222*scale} h34" stroke="#292936" stroke-width="14" stroke-linecap="round"/>'
    return f'<g>{body}{arms}{legs}{shoes}{head}{hairshape}{eyes}{smile}</g>'

def scene_svg(scene, index):
    setting = scene.get("setting", "park")
    top, bottom = PALETTES.get(setting, PALETTES["park"])
    caption = esc(scene.get("caption", "KEEP MOVING"))
    action = esc(scene.get("action", "John takes a positive step"))
    emotion = scene.get("emotion", "hopeful")
    # Distinct illustrated environment elements, while keeping John a human protagonist.
    decor = {
        "stairs": '<path d="M60 930 h160 v-90 h150 v-90 h150 v-90 h150" fill="none" stroke="#6b7280" stroke-width="34"/>',
        "home": '<rect x="70" y="620" width="580" height="250" rx="32" fill="#fffaf0" stroke="#343443" stroke-width="9"/><rect x="120" y="680" width="170" height="130" rx="12" fill="#a7d9ef"/>',
        "park": '<circle cx="120" cy="580" r="100" fill="#73b96c"/><circle cx="610" cy="590" r="120" fill="#83c778"/><path d="M0 990 Q360 850 720 990" fill="#92c986"/>',
        "gym": '<rect x="100" y="700" width="520" height="22" rx="10" fill="#55566b"/><path d="M180 700 v-100 M540 700 v-100" stroke="#55566b" stroke-width="18"/><path d="M140 600 h80 M500 600 h80" stroke="#252535" stroke-width="26"/>',
        "kitchen": '<rect x="70" y="700" width="580" height="200" rx="24" fill="#fffaf0" stroke="#343443" stroke-width="9"/><circle cx="230" cy="690" r="55" fill="#f4bd4e"/><circle cx="370" cy="690" r="48" fill="#8bc77b"/>',
        "clinic": '<rect x="500" y="560" width="120" height="180" rx="16" fill="#fff" stroke="#343443" stroke-width="8"/><path d="M560 590 v120 M520 650 h80" stroke="#59a9a5" stroke-width="18"/>',
    }.get(setting, "")
    john = person(270, 650, 1.12, False, "standing")
    second = person(505, 700, .88, True) if index in (3, 4, 5) else ""
    return f'''<svg xmlns="http://www.w3.org/2000/svg" width="{W}" height="{H}" viewBox="0 0 {W} {H}">
    <defs><linearGradient id="bg" x2="0" y2="1"><stop stop-color="{top}"/><stop offset="1" stop-color="{bottom}"/></linearGradient></defs>
    <rect width="720" height="1280" fill="url(#bg)"/>{decor}
    <text x="360" y="150" text-anchor="middle" font-family="DejaVu Sans" font-size="27" font-weight="bold" fill="#343443">{esc(action).upper()}</text>
    <ellipse cx="360" cy="1030" rx="260" ry="42" fill="#343443" opacity=".12"/>
    {john}{second}
    <rect x="35" y="1060" width="650" height="150" rx="26" fill="#252535"/>
    <text x="360" y="1150" text-anchor="middle" font-family="DejaVu Sans" font-size="43" font-weight="bold" fill="#fff" stroke="#252535" stroke-width="1">{caption}</text>
    <text x="360" y="1248" text-anchor="middle" font-family="DejaVu Sans" font-size="20" fill="#343443">THE HEALTHY YEARS</text>
    </svg>'''

def validate_story(story, sources):
    required = ("topic", "title", "description", "tags", "source_urls", "scenes")
    if not all(story.get(k) for k in required):
        raise RuntimeError("Claude storyboard is missing required fields.")
    if not isinstance(story["scenes"], list) or len(story["scenes"]) != 8:
        raise RuntimeError("Claude must return exactly eight scenes.")
    allowed_urls = {x["url"] for x in sources}
    urls = [u for u in story["source_urls"] if u in allowed_urls]
    if not urls:
        raise RuntimeError("Claude storyboard has no verifiable supplied source URL.")
    narration = [str(s.get("narration", "")).strip() for s in story["scenes"]]
    if any(not x for x in narration):
        raise RuntimeError("A scene has no narration; refusing silent/partial video.")
    words = len(" ".join(narration).split())
    if not 65 <= words <= 100:
        raise RuntimeError(f"Animated Short narration has {words} words; expected 65-100.")
    for scene in story["scenes"]:
        if scene.get("setting") not in PALETTES:
            scene["setting"] = "park"
    story["source_urls"] = urls
    story["script"] = " ".join(narration)
    return story

async def make_voice(text, path):
    communicate = edge_tts.Communicate(text, VOICE, rate="+5%")
    await communicate.save(str(path))

def probe(path):
    data = json.loads(subprocess.check_output([
        "ffprobe", "-v", "error", "-show_entries",
        "format=duration:stream=codec_type,width,height",
        "-of", "json", str(path)
    ], text=True))
    streams = data.get("streams", [])
    if not any(x.get("codec_type") == "video" for x in streams):
        raise RuntimeError("Final MP4 has no video stream.")
    if not any(x.get("codec_type") == "audio" for x in streams):
        raise RuntimeError("Final MP4 has no audio stream. Upload blocked.")
    video = next(x for x in streams if x.get("codec_type") == "video")
    if int(video["width"]) >= int(video["height"]):
        raise RuntimeError("Animated Short must be vertical.")
    duration = float(data["format"]["duration"])
    if not 20 <= duration <= 40:
        raise RuntimeError(f"Final Short duration {duration:.1f}s is outside 20-40s.")
    return duration

def upload(path, story):
    creds = Credentials(
        token=None, refresh_token=os.environ["YT_REFRESH_TOKEN"],
        token_uri="https://oauth2.googleapis.com/token",
        client_id=os.environ["YT_CLIENT_ID"], client_secret=os.environ["YT_CLIENT_SECRET"],
        scopes=["https://www.googleapis.com/auth/youtube.upload"])
    creds.refresh(Request())
    youtube = build("youtube", "v3", credentials=creds, cache_discovery=False)
    body = {"snippet": {"title": story["title"][:100],
                        "description": (story["description"] + "\n\nSources:\n" + "\n".join(story["source_urls"]))[:5000],
                        "tags": [str(x)[:30] for x in story["tags"][:20]],
                        "categoryId": "26", "defaultLanguage": "en"},
            "status": {"privacyStatus": "public", "selfDeclaredMadeForKids": False}}
    req = youtube.videos().insert(part="snippet,status", body=body,
        media_body=MediaFileUpload(str(path), mimetype="video/mp4", resumable=True),
        notifySubscribers=False)
    response = None
    while response is None:
        _, response = req.next_chunk()
    return response["id"]

def main():
    for name in ("ANTHROPIC_API_KEY", "YT_CLIENT_ID", "YT_CLIENT_SECRET", "YT_REFRESH_TOKEN"):
        if not os.getenv(name):
            raise RuntimeError(f"Missing GitHub secret: {name}")
    OUT.mkdir(parents=True, exist_ok=True)
    sources = get_sources()
    previous = json.loads(HISTORY.read_text()) if HISTORY.exists() else []
    story = validate_story(claude_storyboard(sources, [x.get("title", "") for x in previous[-60:]]), sources)
    (OUT / "storyboard.json").write_text(json.dumps(story, indent=2), encoding="utf-8")
    import asyncio
    voice_path = OUT / "voice.mp3"
    asyncio.run(make_voice(story["script"], voice_path))
    audio_duration = float(subprocess.check_output([
        "ffprobe", "-v", "error", "-show_entries", "format=duration",
        "-of", "default=noprint_wrappers=1:nokey=1", str(voice_path)], text=True).strip())
    weights = [max(1, len(s["narration"].split())) for s in story["scenes"]]
    total = sum(weights)
    clip_paths = []
    for i, scene in enumerate(story["scenes"]):
        png = OUT / f"scene_{i+1:02d}.png"
        cairosvg.svg2png(bytestring=scene_svg(scene, i).encode(), write_to=str(png), output_width=W, output_height=H)
        seconds = audio_duration * weights[i] / total
        clip = OUT / f"clip_{i+1:02d}.mp4"
        run(["ffmpeg", "-y", "-loglevel", "error", "-loop", "1", "-i", str(png),
             "-t", f"{seconds:.3f}", "-vf",
             "zoompan=z='min(zoom+0.0005,1.06)':d=1:s=720x1280:fps=30,format=yuv420p",
             "-an", "-c:v", "libx264", "-preset", "veryfast", str(clip)])
        clip_paths.append(clip)
    concat = OUT / "concat.txt"
    concat.write_text("".join(f"file '{p.as_posix()}'\n" for p in clip_paths), encoding="utf-8")
    silent_video = OUT / "picture.mp4"
    run(["ffmpeg", "-y", "-loglevel", "error", "-f", "concat", "-safe", "0",
         "-i", str(concat), "-c", "copy", str(silent_video)])
    final = OUT / "healthy-years-claude-short.mp4"
    run(["ffmpeg", "-y", "-loglevel", "error", "-i", str(silent_video), "-i", str(voice_path),
         "-map", "0:v:0", "-map", "1:a:0", "-c:v", "copy", "-c:a", "aac",
         "-b:a", "192k", "-shortest", "-movflags", "+faststart", str(final)])
    duration = probe(final)
    print(f"Audio/video validated: {final} ({duration:.1f}s).")
    video_id = upload(final, story)
    previous.append({"title": story["title"], "video_id": video_id, "duration": duration,
                     "source_urls": story["source_urls"]})
    HISTORY.parent.mkdir(parents=True, exist_ok=True)
    HISTORY.write_text(json.dumps(previous[-500:], indent=2), encoding="utf-8")
    print(f"Published animated Short: https://www.youtube.com/watch?v={video_id}")

if __name__ == "__main__":
    main()
