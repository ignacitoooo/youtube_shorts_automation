
#!/usr/bin/env python3

import csv
import datetime as dt
import html
import json
import os
import re
import subprocess
import sys
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET
from pathlib import Path

from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload


# Gemini model. If Google changes model availability, this can be
# overridden with the GEMINI_MODEL GitHub Actions variable.
MODEL = os.getenv("GEMINI_MODEL", "gemini-2.5-flash")

FEEDS = [
    (
        "NIH News Releases",
        "https://www.nih.gov/news-releases/feed.xml",
    ),
    (
        "NIH Research Matters",
        "https://www.nih.gov/nih-research-matters/feed.xml",
    ),
]

HISTORY = Path(os.getenv("HISTORY_FILE", "data/historial_videos.csv"))
VIDEO_DIR = Path(os.getenv("VIDEO_WORKDIR", "MoneyPrinterTurbo"))
UPLOAD_DIR = VIDEO_DIR / "upload_queue"

HISTORY_FIELDS = [
    "date_utc",
    "topic",
    "title",
    "video_id",
    "video_url",
    "source_urls",
]


def require_env(*names):
    missing = [name for name in names if not os.getenv(name)]
    if missing:
        raise SystemExit(
            "Missing GitHub secrets: " + ", ".join(missing)
        )


def clean_text(value):
    value = html.unescape(value or "")
    value = re.sub(r"<[^>]+>", " ", value)
    return re.sub(r"\s+", " ", value).strip()


def fetch_feed(label, url):
    request = urllib.request.Request(
        url,
        headers={
            "User-Agent": "TheHealthyYears404/1.0"
        },
    )

    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            raw = response.read()
    except Exception as exc:
        print(f"WARNING: Could not read {label}: {exc}")
        return []

    try:
        root = ET.fromstring(raw)
    except ET.ParseError:
        print(f"WARNING: Invalid RSS feed: {label}")
        return []

    items = []

    # RSS format
    for node in root.findall(".//item"):
        title = clean_text(node.findtext("title", ""))
        link = clean_text(node.findtext("link", ""))
        summary = clean_text(node.findtext("description", ""))
        published = clean_text(node.findtext("pubDate", ""))

        if title and link:
            items.append({
                "source": label,
                "title": title,
                "url": link,
                "published": published,
                "summary": summary[:1800],
            })

    # Atom format
    namespace = {
        "atom": "http://www.w3.org/2005/Atom"
    }

    for node in root.findall(".//atom:entry", namespace):
        title = clean_text(
            node.findtext("atom:title", "", namespace)
        )

        link_node = node.find("atom:link", namespace)
        link = (
            link_node.get("href", "")
            if link_node is not None
            else ""
        )

        summary = clean_text(
            node.findtext("atom:summary", "", namespace)
        )

        published = clean_text(
            node.findtext("atom:published", "", namespace)
        )

        if title and link:
            items.append({
                "source": label,
                "title": title,
                "url": link,
                "published": published,
                "summary": summary[:1800],
            })

    return items


def load_history():
    HISTORY.parent.mkdir(parents=True, exist_ok=True)

    if not HISTORY.exists():
        with HISTORY.open(
            "w",
            newline="",
            encoding="utf-8"
        ) as file:
            writer = csv.DictWriter(
                file,
                fieldnames=HISTORY_FIELDS
            )
            writer.writeheader()

    with HISTORY.open(
        newline="",
        encoding="utf-8"
    ) as file:
        return list(csv.DictReader(file))


def call_gemini(feed_items, history):
    previous = [
        {
            "topic": row.get("topic", ""),
            "title": row.get("title", ""),
        }
        for row in history[-500:]
    ]

    prompt = f"""
You are the health content researcher and scriptwriter
for the YouTube channel "The Healthy Years 404".

AUDIENCE:
Adults over 50 interested in healthy aging.

LANGUAGE:
All titles, scripts and descriptions must be in natural US English.

YOUR TASK:
Study the recent official research and health news supplied below.
Choose THREE different, useful topics for three separate YouTube Shorts.

Topics can include:
- Healthy aging
- Heart health
- Sleep
- Nutrition
- Brain health
- Memory
- Bone health
- Joint mobility
- Hearing and vision
- Balance and fall prevention
- Mental wellbeing
- Physical activity
- Preventive health

RESEARCH RULES:
Use the supplied articles as your research sources.
Prefer recent and relevant information.
Do not invent research or statistics.
Do not present animal studies as proven human benefits.
Do not promise cures or guaranteed prevention.
Use cautious language such as "may support" when appropriate.
Include practical, low-risk advice.
If evidence is preliminary, make that clear.

VERY IMPORTANT: AVOID REPEATING CONTENT.

The publication history below contains videos already published.
Do not repeat their topics, angles, or practical advice.
Changing the title does not make an old topic new.
Avoid close variations of previously covered subjects.

If three genuinely different topics cannot be found,
return fewer than three. Never invent topics to fill the quota.

OFFICIAL RESEARCH FEEDS:
{json.dumps(feed_items, ensure_ascii=False)}

PREVIOUSLY PUBLISHED VIDEOS:
{json.dumps(previous, ensure_ascii=False)}

RETURN ONLY VALID JSON, using this structure:

{{
  "videos": [
    {{
      "topic": "short topic description",
      "title": "YouTube Shorts title under 70 characters",
      "script": "Complete spoken English script, around 90 to 125 words",
      "visual_terms": "5 to 8 Pexels search phrases in English, separated by commas",
      "description": "English description, educational disclaimer and hashtags",
      "tags": ["tag one", "tag two"],
      "source_urls": [
        "Exact article URL copied from the supplied research feeds"
      ]
    }}
  ]
}}
"""

    payload = {
        "contents": [
            {
                "role": "user",
                "parts": [{"text": prompt}],
            }
        ],
        "generationConfig": {
            "temperature": 0.5,
            "responseMimeType": "application/json",
            "maxOutputTokens": 8192,
        },
    }

    endpoint = (
        "https://generativelanguage.googleapis.com/v1beta/models/"
        + MODEL
        + ":generateContent"
    )

    request = urllib.request.Request(
        endpoint,
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            "x-goog-api-key": os.environ["GEMINI_API_KEY"],
        },
        method="POST",
    )

    try:
        with urllib.request.urlopen(
            request,
            timeout=180
        ) as response:
            data = json.loads(response.read().decode("utf-8"))

    except urllib.error.HTTPError as exc:
        details = exc.read().decode(
            "utf-8",
            errors="replace"
        )
        raise RuntimeError(
            f"Gemini API error {exc.code}: {details[:1500]}"
        )

    parts = (
        data.get("candidates", [{}])[0]
        .get("content", {})
        .get("parts", [])
    )

    text = "".join(
        part.get("text", "")
        for part in parts
    ).strip()

    if not text:
        raise RuntimeError("Gemini returned an empty response.")

    try:
        return json.loads(text)
    except json.JSONDecodeError:
        raise RuntimeError(
            "Gemini did not return valid JSON: " + text[:1000]
        )


def normalize(value):
    value = (value or "").lower()
    value = re.sub(r"[^a-z0-9 ]", " ", value)
    return re.sub(r"\s+", " ", value).strip()


def validate_videos(result, feed_items, history):
    videos = result.get("videos", [])

    if not isinstance(videos, list) or len(videos) < 3:
        raise RuntimeError(
            "Fewer than three new topics were found. "
            "No videos will be uploaded today."
        )

    allowed_urls = {
        item["url"]
        for item in feed_items
    }

    old_topics = {
        normalize(row.get("topic", ""))
        for row in history
    }

    old_titles = {
        normalize(row.get("title", ""))
        for row in history
    }

    accepted = []
    seen_topics = set()
    seen_titles = set()

    for video in videos:
        required = [
            "topic",
            "title",
            "script",
            "visual_terms",
            "description",
            "source_urls",
        ]

        if not all(video.get(key) for key in required):
            continue

        topic = normalize(video["topic"])
        title = normalize(video["title"])

        if not topic or not title:
            continue

        if topic in old_topics or title in old_titles:
            continue

        if topic in seen_topics or title in seen_titles:
            continue

        valid_urls = [
            url for url in video["source_urls"]
            if url in allowed_urls
        ]

        if not valid_urls:
            continue

        word_count = len(video["script"].split())

        if word_count < 70 or word_count > 160:
            continue

        video["source_urls"] = valid_urls

        accepted.append(video)
        seen_topics.add(topic)
        seen_titles.add(title)

        if len(accepted) == 3:
            break

    if len(accepted) != 3:
        raise RuntimeError(
            f"Only {len(accepted)} valid new scripts passed validation. "
            "No videos will be uploaded today."
        )

    return accepted


def render_video(item, index):
    UPLOAD_DIR.mkdir(
        parents=True,
        exist_ok=True
    )

    task_folder = VIDEO_DIR / "storage/tasks"

    before = {
        path.resolve()
        for path in task_folder.rglob("*.mp4")
    }

    command = [
        sys.executable,
        "cli.py",
        "--video-script",
        item["script"],
        "--video-terms",
        item["visual_terms"],
        "--video-language",
        "en-US",
        "--video-source",
        "pexels",
        "--video-aspect",
        "9:16",
        "--video-fit-mode",
        "cover",
        "--video-clip-duration",
        "3",
        "--video-concat-mode",
        "sequential",
        "--video-transition-mode",
        "fade-in",
        "--match-materials-to-script",
        "--voice-name",
        "en-US-GuyNeural",
        "--voice-rate",
        "1.05",
        "--subtitle-enabled",
        "--subtitle-position",
        "two_thirds_bottom",
        "--subtitle-display-mode",
        "word_by_word",
        "--subtitle-animation",
        "pop_spring",
        "--font-size",
        "68",
        "--text-fore-color",
        "#FFE45C",
        "--stroke-color",
        "#000000",
        "--stroke-width",
        "3",
        "--video-count",
        "1",
        "--bgm-type",
        "none",
    ]

    print(
        f"Generating video {index}/3: {item['title']}"
    )

    subprocess.run(
        command,
        cwd=VIDEO_DIR,
        check=True
    )

    candidates = [
        path
        for path in task_folder.rglob("*.mp4")
        if path.is_file() and path.resolve() not in before
    ]

    if not candidates:
        raise RuntimeError(
            "No new MP4 was created by MoneyPrinterTurbo."
        )

    latest = max(
        candidates,
        key=lambda path: path.stat().st_mtime
    )

    safe_name = re.sub(
        r"[^a-z0-9]+",
        "-",
        item["title"].lower()
    ).strip("-")[:55]

    destination = UPLOAD_DIR / (
        f"{index:02d}-{safe_name}.mp4"
    )

    destination.write_bytes(latest.read_bytes())

    return destination


def make_youtube_client():
    scopes = [
        "https://www.googleapis.com/auth/youtube.upload"
    ]

    credentials = Credentials(
        token=None,
        refresh_token=os.environ["YT_REFRESH_TOKEN"],
        token_uri="https://oauth2.googleapis.com/token",
        client_id=os.environ["YT_CLIENT_ID"],
        client_secret=os.environ["YT_CLIENT_SECRET"],
        scopes=scopes,
    )

    credentials.refresh(Request())

    return build(
        "youtube",
        "v3",
        credentials=credentials,
        cache_discovery=False,
    )


def upload_one(youtube, item, video_path):
    tags = item.get("tags", [])

    if not isinstance(tags, list):
        tags = []

    body = {
        "snippet": {
            "title": item["title"][:100],
            "description": item["description"][:5000],
            "tags": [
                str(tag)[:30]
                for tag in tags[:20]
            ],
            "categoryId": "26",
            "defaultLanguage": "en",
        },
        "status": {
            "privacyStatus": "public",
            "selfDeclaredMadeForKids": False,
        },
    }

    media = MediaFileUpload(
        str(video_path),
        mimetype="video/mp4",
        resumable=True,
    )

    request = youtube.videos().insert(
        part="snippet,status",
        body=body,
        media_body=media,
        notifySubscribers=False,
    )

    response = None

    while response is None:
        status, response = request.next_chunk()

        if status:
            print(
                f"{item['title']}: "
                f"{int(status.progress() * 100)}% uploaded"
            )

    video_id = response["id"]

    return (
        video_id,
        f"https://www.youtube.com/watch?v={video_id}"
    )


def append_history(item, video_id, video_url):
    new_row = {
        "date_utc": dt.datetime.now(
            dt.timezone.utc
        ).isoformat(timespec="seconds"),
        "topic": item["topic"],
        "title": item["title"],
        "video_id": video_id,
        "video_url": video_url,
        "source_urls": " ".join(item["source_urls"]),
    }

    exists = (
        HISTORY.exists()
        and HISTORY.stat().st_size > 0
    )

    with HISTORY.open(
        "a",
        newline="",
        encoding="utf-8"
    ) as file:
        writer = csv.DictWriter(
            file,
            fieldnames=HISTORY_FIELDS
        )

        if not exists:
            writer.writeheader()

        writer.writerow(new_row)

    print(
        f"HISTORY SAVED: {item['title']} | {video_url}"
    )


def main():
    require_env(
        "GEMINI_API_KEY",
        "PEXELS_API_KEY",
        "YT_CLIENT_ID",
        "YT_CLIENT_SECRET",
        "YT_REFRESH_TOKEN",
    )

    history = load_history()

    feed_items = []

    for label, url in FEEDS:
        feed_items.extend(
            fetch_feed(label, url)
        )

    unique_items = {}

    for item in feed_items:
        unique_items[item["url"]] = item

    feed_items = list(unique_items.values())[:60]

    if len(feed_items) < 3:
        raise RuntimeError(
            "Not enough research articles could be retrieved "
            "from the NIH feeds. No videos will be uploaded."
        )

    print(
        f"Loaded {len(history)} previous publications."
    )

    print(
        f"Retrieved {len(feed_items)} research articles."
    )

    result = call_gemini(
        feed_items,
        history
    )

    videos = validate_videos(
        result,
        feed_items,
        history
    )

    print("Today's selected topics:")

    for index, item in enumerate(videos, 1):
        print(
            f"{index}. {item['topic']} - {item['title']}"
        )

    youtube = make_youtube_client()

    for index, item in enumerate(videos, 1):
        video_path = render_video(
            item,
            index
        )

        video_id, video_url = upload_one(
            youtube,
            item,
            video_path
        )

        # Save each successful upload immediately.
        append_history(
            item,
            video_id,
            video_url
        )

    print(
        "All three new Shorts have been uploaded publicly."
    )


if __name__ == "__main__":
    main()
