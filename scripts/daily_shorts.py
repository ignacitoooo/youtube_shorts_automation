
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


# ============================================================
# CONFIGURATION
# ============================================================

MODEL = "openrouter/free"

OPENROUTER_URL = (
    "https://openrouter.ai/api/v1/chat/completions"
)

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

HISTORY = Path(
    os.getenv(
        "HISTORY_FILE",
        "data/historial_videos.csv"
    )
)

VIDEO_DIR = Path(
    os.getenv(
        "VIDEO_WORKDIR",
        "MoneyPrinterTurbo"
    )
)

UPLOAD_DIR = VIDEO_DIR / "upload_queue"

HISTORY_FIELDS = [
    "date_utc",
    "topic",
    "title",
    "video_id",
    "video_url",
    "source_urls",
]


# ============================================================
# GENERAL HELPERS
# ============================================================

def require_env(*names):
    missing = [
        name
        for name in names
        if not os.getenv(name)
    ]

    if missing:
        raise RuntimeError(
            "Missing GitHub secrets: "
            + ", ".join(missing)
        )


def clean_text(value):
    value = html.unescape(value or "")
    value = re.sub(r"<[^>]+>", " ", value)

    return re.sub(
        r"\s+",
        " ",
        value
    ).strip()


def normalize(value):
    value = (value or "").lower()

    value = re.sub(
        r"[^a-z0-9 ]",
        " ",
        value
    )

    return re.sub(
        r"\s+",
        " ",
        value
    ).strip()


def load_history():
    HISTORY.parent.mkdir(
        parents=True,
        exist_ok=True
    )

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
        return list(
            csv.DictReader(file)
        )


# ============================================================
# NIH RESEARCH
# ============================================================

def fetch_feed(label, url):
    print(f"Reading research feed: {label}")

    request = urllib.request.Request(
        url,
        headers={
            "User-Agent": "TheHealthyYears404/1.0"
        }
    )

    try:
        with urllib.request.urlopen(
            request,
            timeout=30
        ) as response:
            raw = response.read()

    except Exception as exc:
        print(
            f"WARNING: Could not read {label}: {exc}"
        )
        return []

    try:
        root = ET.fromstring(raw)

    except ET.ParseError:
        print(
            f"WARNING: Invalid feed: {label}"
        )
        return []

    items = []

    # RSS feeds
    for node in root.findall(".//item"):
        title = clean_text(
            node.findtext("title", "")
        )

        link = clean_text(
            node.findtext("link", "")
        )

        summary = clean_text(
            node.findtext("description", "")
        )

        published = clean_text(
            node.findtext("pubDate", "")
        )

        if title and link:
            items.append({
                "source": label,
                "title": title,
                "url": link,
                "published": published,
                "summary": summary[:1800],
            })

    # Atom feeds
    namespace = {
        "atom": "http://www.w3.org/2005/Atom"
    }

    for node in root.findall(
        ".//atom:entry",
        namespace
    ):
        title = clean_text(
            node.findtext(
                "atom:title",
                "",
                namespace
            )
        )

        link_node = node.find(
            "atom:link",
            namespace
        )

        link = (
            link_node.get("href", "")
            if link_node is not None
            else ""
        )

        summary = clean_text(
            node.findtext(
                "atom:summary",
                "",
                namespace
            )
        )

        published = clean_text(
            node.findtext(
                "atom:published",
                "",
                namespace
            )
        )

        if title and link:
            items.append({
                "source": label,
                "title": title,
                "url": link,
                "published": published,
                "summary": summary[:1800],
            })

    print(
        f"Found {len(items)} articles in {label}"
    )

    return items


def get_research():
    articles = []

    for label, url in FEEDS:
        articles.extend(
            fetch_feed(label, url)
        )

    unique = {}

    for article in articles:
        unique[article["url"]] = article

    result = list(unique.values())

    print(
        f"Total unique research articles: {len(result)}"
    )

    if len(result) < 3:
        raise RuntimeError(
            "Not enough NIH research articles were retrieved. "
            "No videos will be uploaded."
        )

    return result[:60]


# ============================================================
# OPENROUTER
# ============================================================

def call_openrouter(feed_items, history):

    previous = [
        {
            "topic": row.get("topic", ""),
            "title": row.get("title", ""),
        }
        for row in history[-500:]
    ]

    prompt = f"""
You are the health researcher and scriptwriter
for The Healthy Years 404.

AUDIENCE:
Adults over 50 interested in healthy aging.

LANGUAGE:
All titles, scripts and descriptions must be
written in natural American English.

TASK:
Study the official NIH research articles below.
Choose exactly THREE genuinely different topics
for three separate YouTube Shorts.

TOPIC AREAS:
Heart health, sleep, nutrition, brain health,
memory, bone health, joint mobility, hearing,
vision, balance, fall prevention, mental wellbeing,
physical activity and healthy aging.

RESEARCH RULES:
- Base each video on the supplied research.
- Do not invent studies, statistics or medical facts.
- Do not present animal studies as proven human benefits.
- Do not promise cures or guaranteed prevention.
- Explain uncertainty honestly.
- Give practical, low-risk information.
- Include an educational disclaimer in each description.

VERY IMPORTANT: AVOID REPEATING CONTENT.

The publication history below contains topics
already published by this channel.

Do not repeat:
- An existing topic.
- A very similar topic.
- The same practical advice with a different title.
- The same research angle.

A new title does NOT make an old topic new.

Choose three different subjects with different
practical takeaways.

If three genuinely different topics are not supported,
return fewer than three. Never invent topics.

PREVIOUSLY PUBLISHED:
{json.dumps(previous, ensure_ascii=False)}

OFFICIAL NIH RESEARCH ARTICLES:
{json.dumps(feed_items, ensure_ascii=False)}

Return ONLY valid JSON.
Do not use Markdown code fences.
Do not add text outside the JSON object.

Use exactly this structure:

{{
  "videos": [
    {{
      "topic": "short topic description",
      "title": "YouTube Shorts title under 70 characters",
      "script": "Complete spoken English script of 90 to 125 words",
      "visual_terms": "5 to 8 Pexels search phrases separated by commas",
      "description": "English description with medical disclaimer and hashtags",
      "tags": ["tag one", "tag two"],
      "source_urls": ["exact article URL from the supplied articles"]
    }}
  ]
}}
"""

    payload = {
        "model": MODEL,
        "messages": [
            {
                "role": "system",
                "content": (
                    "You are a careful health research assistant. "
                    "Return valid JSON only. "
                    "Never invent medical research or source URLs. "
                    "Do not repeat previously published topics."
                )
            },
            {
                "role": "user",
                "content": prompt
            }
        ],
        "temperature": 0.3,
        "max_tokens": 12000
    }

    request = urllib.request.Request(
        OPENROUTER_URL,
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            "Authorization": (
                "Bearer "
                + os.environ["OPENROUTER_API_KEY"]
            ),
            "HTTP-Referer": "https://github.com",
            "X-Title": "The Healthy Years 404"
        },
        method="POST"
    )

    print(
        "Sending research and history to OpenRouter..."
    )

    try:
        with urllib.request.urlopen(
            request,
            timeout=240
        ) as response:
            data = json.loads(
                response.read().decode("utf-8")
            )

    except urllib.error.HTTPError as exc:
        details = exc.read().decode(
            "utf-8",
            errors="replace"
        )

        raise RuntimeError(
            f"OpenRouter API error {exc.code}: "
            f"{details[:2000]}"
        ) from exc

    choices = data.get("choices", [])

    if not choices:
        raise RuntimeError(
            "OpenRouter returned no choices: "
            + json.dumps(data)[:2000]
        )

    text = choices[0].get(
        "message",
        {}
    ).get(
        "content",
        ""
    )

    if isinstance(text, list):
        text = "".join(
            part.get("text", "")
            for part in text
            if isinstance(part, dict)
        )

    text = str(text).strip()

    text = re.sub(
        r"^```(?:json)?\s*",
        "",
        text,
        flags=re.IGNORECASE
    )

    text = re.sub(
        r"\s*```$",
        "",
        text
    )

    start = text.find("{")
    end = text.rfind("}")

    if start == -1 or end == -1 or end <= start:
        raise RuntimeError(
            "OpenRouter did not return a complete JSON object: "
            + text[:2500]
        )

    json_text = text[start:end + 1]

    try:
        result = json.loads(json_text)

    except json.JSONDecodeError as exc:
        raise RuntimeError(
            f"Invalid JSON from OpenRouter: {exc}. "
            + text[:2500]
        ) from exc

    if not isinstance(result, dict):
        raise RuntimeError(
            "OpenRouter response is not a JSON object."
        )

    return result


# ============================================================
# CHECK NEW TOPICS
# ============================================================

def validate_videos(result, feed_items, history):

    videos = result.get("videos", [])

    if not isinstance(videos, list):
        raise RuntimeError(
            "The AI response does not contain a valid videos list."
        )

    if len(videos) < 3:
        raise RuntimeError(
            "The AI found fewer than three new topics. "
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
            "source_urls"
        ]

        if not all(
            video.get(key)
            for key in required
        ):
            print(
                "Rejected incomplete AI response."
            )
            continue

        topic = normalize(video["topic"])
        title = normalize(video["title"])

        if not topic or not title:
            continue

        if topic in old_topics:
            print(
                f"Repeated topic rejected: {topic}"
            )
            continue

        if title in old_titles:
            print(
                f"Repeated title rejected: {title}"
            )
            continue

        if (
            topic in seen_topics
            or title in seen_titles
        ):
            continue

        source_urls = video["source_urls"]

        if not isinstance(source_urls, list):
            continue

        valid_urls = [
            url
            for url in source_urls
            if url in allowed_urls
        ]

        if not valid_urls:
            print(
                f"Rejected without a supplied source: {title}"
            )
            continue

        word_count = len(
            video["script"].split()
        )

        if word_count < 70 or word_count > 160:
            print(
                f"Rejected script with {word_count} words: {title}"
            )
            continue

        video["source_urls"] = valid_urls

        accepted.append(video)

        seen_topics.add(topic)
        seen_titles.add(title)

        if len(accepted) == 3:
            break

    if len(accepted) != 3:
        raise RuntimeError(
            f"Only {len(accepted)} valid new videos passed "
            "the checks. Nothing will be uploaded."
        )

    return accepted


# ============================================================
# GENERATE VIDEO WITH MONEYPRINTERTURBO
# ============================================================

def render_video(item, index):

    UPLOAD_DIR.mkdir(
        parents=True,
        exist_ok=True
    )

    task_folder = VIDEO_DIR / "storage/tasks"

    task_folder.mkdir(
        parents=True,
        exist_ok=True
    )

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
        if (
            path.is_file()
            and path.resolve() not in before
        )
    ]

    if not candidates:
        raise RuntimeError(
            "MoneyPrinterTurbo did not create a new MP4."
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

    destination.write_bytes(
        latest.read_bytes()
    )

    print(
        f"Video file ready: {destination}"
    )

    return destination


# ============================================================
# YOUTUBE
# ============================================================

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
        scopes=scopes
    )

    credentials.refresh(Request())

    return build(
        "youtube",
        "v3",
        credentials=credentials,
        cache_discovery=False
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

            "defaultLanguage": "en"
        },

        "status": {
            "privacyStatus": "public",
            "selfDeclaredMadeForKids": False
        }
    }

    media = MediaFileUpload(
        str(video_path),
        mimetype="video/mp4",
        resumable=True
    )

    request = youtube.videos().insert(
        part="snippet,status",
        body=body,
        media_body=media,
        notifySubscribers=False
    )

    response = None

    while response is None:

        status, response = request.next_chunk()

        if status:
            print(
                f"Uploading {item['title']}: "
                f"{int(status.progress() * 100)}%"
            )

    video_id = response["id"]

    video_url = (
        f"https://www.youtube.com/watch?v={video_id}"
    )

    print(
        f"SUCCESS: Published {item['title']}"
    )

    print(
        f"YOUTUBE URL: {video_url}"
    )

    return video_id, video_url


# ============================================================
# SAVE HISTORY AFTER EACH SUCCESSFUL UPLOAD
# ============================================================

def append_history(item, video_id, video_url):

    new_row = {
        "date_utc": dt.datetime.now(
            dt.timezone.utc
        ).isoformat(timespec="seconds"),

        "topic": item["topic"],

        "title": item["title"],

        "video_id": video_id,

        "video_url": video_url,

        "source_urls": " ".join(
            item["source_urls"]
        )
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
        f"HISTORY SAVED: {item['title']}"
    )


# ============================================================
# MAIN PROGRAM
# ============================================================

def main():

    print("=" * 60)
    print("THE HEALTHY YEARS 404")
    print("DAILY RESEARCH AND YOUTUBE SHORTS AUTOMATION")
    print("=" * 60)

    require_env(
        "OPENROUTER_API_KEY",
        "PEXELS_API_KEY",
        "YT_CLIENT_ID",
        "YT_CLIENT_SECRET",
        "YT_REFRESH_TOKEN"
    )

    # 1. Read publication history
    history = load_history()

    print(
        f"Loaded {len(history)} previous publications."
    )

    # 2. Research health topics
    feed_items = get_research()

    # 3. Ask OpenRouter for new scripts
    result = call_openrouter(
        feed_items,
        history
    )

    # 4. Reject repeated or invalid topics
    videos = validate_videos(
        result,
        feed_items,
        history
    )

    print("")
    print("TODAY'S SELECTED TOPICS:")

    for index, item in enumerate(videos, 1):
        print(
            f"{index}. {item['topic']} "
            f"- {item['title']}"
        )

    # 5. Connect to YouTube
    youtube = make_youtube_client()

    # 6. Generate and upload one at a time
    for index, item in enumerate(videos, 1):

        print("")
        print(
            f"PROCESSING VIDEO {index}/3"
        )

        video_path = render_video(
            item,
            index
        )

        video_id, video_url = upload_one(
            youtube,
            item,
            video_path
        )

        # Save immediately after successful upload.
        append_history(
            item,
            video_id,
            video_url
        )

    print("")
    print("=" * 60)
    print("ALL THREE SHORTS HAVE BEEN UPLOADED.")
    print("PUBLICATION HISTORY UPDATED.")
    print("=" * 60)


# ============================================================
# START THE PROGRAM
# ============================================================

if __name__ == "__main__":
    main()
