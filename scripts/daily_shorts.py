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
from googleapiclient.errors import HttpError


# ============================================================
# CONFIGURATION
# ============================================================

MODEL = "openrouter/free"

OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"

FEEDS = [
    ("NIH News Releases", "https://www.nih.gov/news-releases/feed.xml"),
    ("NIH Research Matters", "https://www.nih.gov/nih-research-matters/feed.xml"),
    ("NIH News in Health", "https://newsinhealth.nih.gov/rss"),
    ("ScienceDaily Health and Medicine", "https://www.sciencedaily.com/rss/health_medicine.xml"),
    ("BBC Health", "https://feeds.bbci.co.uk/news/health/rss.xml"),
    ("NPR Health", "https://feeds.npr.org/1128/rss.xml"),
]

HISTORY = Path(os.getenv("HISTORY_FILE", "data/historial_videos.csv"))
PENDING_FILE = Path(os.getenv("PENDING_FILE", "data/pendientes_publicacion.json"))
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


# ============================================================
# GENERAL HELPERS
# ============================================================

def require_env(*names):
    missing = [name for name in names if not os.getenv(name)]
    if missing:
        raise RuntimeError(
            "Missing GitHub secrets: " + ", ".join(missing)
        )


def clean_text(value):
    value = html.unescape(value or "")
    value = re.sub(r"<[^>]+>", " ", value)
    return re.sub(r"\s+", " ", value).strip()


def normalize(value):
    value = (value or "").lower()
    value = re.sub(r"[^a-z0-9 ]", " ", value)
    return re.sub(r"\s+", " ", value).strip()


def load_history():
    HISTORY.parent.mkdir(parents=True, exist_ok=True)

    if not HISTORY.exists():
        with HISTORY.open("w", newline="", encoding="utf-8") as file:
            writer = csv.DictWriter(file, fieldnames=HISTORY_FIELDS)
            writer.writeheader()

    with HISTORY.open(newline="", encoding="utf-8") as file:
        return list(csv.DictReader(file))


# ============================================================
# HEALTH RESEARCH FEEDS
# ============================================================

def fetch_feed(label, url):
    print(f"Reading research feed: {label}")

    request = urllib.request.Request(
        url,
        headers={"User-Agent": "TheHealthyYears404/1.0"}
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
        print(f"WARNING: Invalid feed: {label}")
        return []

    items = []

    # RSS
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

    # Atom
    namespace = {"atom": "http://www.w3.org/2005/Atom"}

    for node in root.findall(".//atom:entry", namespace):
        title = clean_text(
            node.findtext("atom:title", "", namespace)
        )

        link_node = node.find("atom:link", namespace)
        link = link_node.get("href", "") if link_node is not None else ""

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

    print(f"Found {len(items)} articles in {label}")
    return items


def get_research():
    articles = []

    for label, url in FEEDS:
        articles.extend(fetch_feed(label, url))

    unique = {article["url"]: article for article in articles}
    result = list(unique.values())

    print(f"Total unique research articles: {len(result)}")

    for article in result:
        print(f"- [{article['source']}] {article['title']}")

    if len(result) < 3:
        raise RuntimeError(
            "Not enough health research articles were retrieved. "
            "No videos will be uploaded."
        )

    return result[:60]


# ============================================================
# OPENROUTER - IMPROVED SHORTS SCRIPTWRITER
# ============================================================

def call_openrouter(feed_items, history):
    """Generate three research-based Shorts with stronger retention."""

    previous = [
        {
            "topic": row.get("topic", ""),
            "title": row.get("title", ""),
        }
        for row in history[-500:]
    ]

    prompt = f"""
You are the health researcher and YouTube Shorts scriptwriter
for The Healthy Years 404.

CHANNEL AND AUDIENCE:
- Channel: The Healthy Years 404.
- Audience: adults aged 50 and older, primarily in the United States.
- Write everything in natural, conversational American English.

GOAL:
Create exactly THREE distinct, useful, scientifically responsible
YouTube Shorts. Each video should make viewers want to keep watching
without using misleading clickbait or exaggerating medical findings.

TOPIC PRIORITIES:
Healthy aging after 50, protein and nutrition, joint and bone health,
heart health, sleep, brain health and memory, gut health, lung health,
blood pressure, mobility, balance and practical daily habits.

Choose topics with a recognizable problem, a useful takeaway and
research support in the supplied articles. Use different health
subjects and angles across the three videos.

RETENTION STRUCTURE FOR EACH SCRIPT:

1. FIRST 0-3 SECONDS:
Start with a direct hook: a relatable problem, surprising but accurate
finding, common mistake or useful question.
Do not greet viewers or introduce the channel.

2. BY SECOND 8:
Deliver the first useful tip or key fact. No long setup.

3. MIDDLE:
Give concise, practical explanations. Use short sentences
that sound natural when spoken aloud.

4. OPEN LOOP:
Early in the script, create honest curiosity about the final tip.
For example, say that the last habit is often overlooked.
The final tip must genuinely add value.

5. END:
Put the strongest or most overlooked takeaway near the end,
then finish with a very short call to action.
No long goodbye.

SCRIPT LENGTH:
- Aim for 95-110 spoken words per script.
- Acceptable range: 85-120 words.
- Target video duration: approximately 35-45 seconds.
- Prefer three useful takeaways when research supports them.
- Avoid repeated points, filler and long introductions.
- Write narration, not an article.

VISUAL PACING:
Provide 8-12 specific English Pexels search phrases.
Put them in the order they should appear in the video.
Make them relevant to the narration and suitable for visual
changes approximately every 3-5 seconds.

Prefer realistic footage of adults 50+, daily activities,
food, walking, exercise, sleep routines and relevant health visuals.
Do not use generic or unrelated visual phrases.

TITLES:
Create a clear, curiosity-driven English title, ideally 45-65 characters.
Use the benefit, problem or age group when appropriate.
The title must accurately represent the supplied research.

MEDICAL SAFETY AND ACCURACY:
- Use only factual claims supported by the supplied articles.
- Never invent studies, statistics, medical facts or source URLs.
- Distinguish association from causation.
- Do not present animal research as proven human benefit.
- Do not claim that a food, supplement or habit cures disease.
- Never advise viewers to stop or change prescribed medication.
- Explain uncertainty when evidence is preliminary.
- Offer responsible general education, not personal diagnosis.

Include this exact disclaimer in every description:
"This video is for educational purposes only and is not a substitute
for professional medical advice."

AVOID REPETITION:
Do not repeat previously published topics, titles or substantially
similar research angles. A new title does not make an old topic new.

Previously published videos:
{json.dumps(previous, ensure_ascii=False)}

AVAILABLE RESEARCH ARTICLES:
Use the supplied articles as the factual basis.
Every video must cite at least one exact URL from this list.
Do not invent or alter URLs.

{json.dumps(feed_items, ensure_ascii=False)}

OUTPUT:
Return one complete valid JSON object only.
No Markdown and no commentary.

Return exactly this structure with exactly three video objects:

{{
  "videos": [
    {{
      "topic": "Specific health topic",
      "title": "English YouTube Shorts title",
      "script": "Complete spoken English script of 85-120 words",
      "visual_terms": "8-12 English Pexels search phrases separated by commas",
      "description": "English description including the exact medical disclaimer and relevant hashtags",
      "tags": ["healthy aging", "health after 50", "wellness"],
      "source_urls": ["Exact article URL from the supplied articles"]
    }}
  ]
}}

Ensure every JSON string is properly escaped and the JSON is complete.
"""

    api_key = os.environ["OPENROUTER_API_KEY"]

    conversation = [
        {
            "role": "system",
            "content": (
                "You are a careful health researcher and expert YouTube Shorts "
                "scriptwriter. Write natural American English, prioritize a "
                "strong first-three-second hook, early practical value, visual "
                "pacing and scientific accuracy. Return valid JSON only."
            ),
        },
        {"role": "user", "content": prompt},
    ]

    last_error = None

    for attempt in range(1, 4):
        payload = {
            "model": MODEL,
            "messages": conversation,
            "temperature": 0.2,
            "max_tokens": 16000,
        }

        request = urllib.request.Request(
            OPENROUTER_URL,
            data=json.dumps(payload).encode("utf-8"),
            headers={
                "Content-Type": "application/json",
                "Authorization": "Bearer " + api_key,
                "HTTP-Referer": "https://github.com",
                "X-Title": "The Healthy Years 404",
            },
            method="POST",
        )

        print(f"OpenRouter request, attempt {attempt}/3...")

        try:
            with urllib.request.urlopen(request, timeout=240) as response:
                data = json.loads(response.read().decode("utf-8"))

        except urllib.error.HTTPError as exc:
            details = exc.read().decode("utf-8", errors="replace")
            raise RuntimeError(
                f"OpenRouter API error {exc.code}: {details[:2000]}"
            ) from exc

        except (urllib.error.URLError, TimeoutError) as exc:
            last_error = f"Network error: {exc}"
            print(last_error)
            if attempt < 3:
                continue
            break

        choices = data.get("choices", [])

        if not choices:
            last_error = (
                "OpenRouter returned no choices: "
                + json.dumps(data)[:1500]
            )
            continue

        content = choices[0].get("message", {}).get("content", "")

        if isinstance(content, list):
            content = "".join(
                part.get("text", "")
                for part in content
                if isinstance(part, dict)
            )

        text = str(content).strip()

        text = re.sub(
            r"^```(?:json)?\s*",
            "",
            text,
            flags=re.IGNORECASE
        )

        text = re.sub(r"\s*```$", "", text)

        first = text.find("{")
        last = text.rfind("}")

        try:
            if first < 0 or last <= first:
                raise ValueError("No complete JSON object found")

            result = json.loads(text[first:last + 1])

            if (
                not isinstance(result, dict)
                or not isinstance(result.get("videos"), list)
            ):
                raise ValueError("JSON must contain a videos list")

            if len(result["videos"]) < 3:
                raise ValueError("Fewer than three videos were returned")

            print(
                f"OpenRouter returned {len(result['videos'])} video scripts."
            )

            return result

        except (json.JSONDecodeError, ValueError) as exc:
            last_error = str(exc)

            print(
                f"Invalid OpenRouter response on attempt "
                f"{attempt}: {last_error}"
            )

            if attempt < 3:
                conversation = [
                    {
                        "role": "system",
                        "content": (
                            "Repair the supplied response and return "
                            "one complete valid JSON object only."
                        ),
                    },
                    {
                        "role": "user",
                        "content": (
                            "Fix this response so it is complete valid JSON "
                            "with exactly three video objects. Preserve factual "
                            "content and the required schema. Do not add "
                            "commentary.\n\n" + text[:14000]
                        ),
                    },
                ]

    raise RuntimeError(
        "OpenRouter did not provide valid JSON after 3 attempts. "
        f"Last error: {last_error}. No videos were uploaded."
    )


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

        if not all(video.get(key) for key in required):
            print("Rejected incomplete AI response.")
            continue

        topic = normalize(video["topic"])
        title = normalize(video["title"])

        if not topic or not title:
            continue

        if topic in old_topics:
            print(f"Repeated topic rejected: {topic}")
            continue

        if title in old_titles:
            print(f"Repeated title rejected: {title}")
            continue

        if topic in seen_topics or title in seen_titles:
            continue

        source_urls = video["source_urls"]

        if not isinstance(source_urls, list):
            continue

        valid_urls = [
            url for url in source_urls
            if url in allowed_urls
        ]

        if not valid_urls:
            print(
                f"Rejected without a supplied source: {title}"
            )
            continue

        word_count = len(video["script"].split())

        if word_count < 85 or word_count > 120:
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

    UPLOAD_DIR.mkdir(parents=True, exist_ok=True)

    task_folder = VIDEO_DIR / "storage/tasks"
    task_folder.mkdir(parents=True, exist_ok=True)

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

    destination.write_bytes(latest.read_bytes())

    print(f"Video file ready: {destination}")

    return destination


# ============================================================
# PENDING PUBLICATIONS
# ============================================================

def load_pending():
    """Load scripts waiting for YouTube upload."""

    if not PENDING_FILE.exists():
        return []

    try:
        data = json.loads(
            PENDING_FILE.read_text(encoding="utf-8")
        )

    except (json.JSONDecodeError, OSError) as exc:
        raise RuntimeError(
            f"Could not read pending publication file "
            f"{PENDING_FILE}: {exc}"
        ) from exc

    if not isinstance(data, list):
        raise RuntimeError(
            "Pending publication file must contain a JSON list."
        )

    return data


def save_pending(items):
    """Persist pending scripts atomically."""

    PENDING_FILE.parent.mkdir(parents=True, exist_ok=True)

    temporary = PENDING_FILE.with_suffix(
        PENDING_FILE.suffix + ".tmp"
    )

    temporary.write_text(
        json.dumps(items, ensure_ascii=False, indent=2),
        encoding="utf-8"
    )

    temporary.replace(PENDING_FILE)

    print(
        f"PENDING QUEUE SAVED: {len(items)} video(s) waiting."
    )


def is_upload_limit_error(exc):
    """Recognize YouTube's daily upload limit response."""

    details = str(exc)
    content = getattr(exc, "content", None)

    if content:
        if isinstance(content, bytes):
            details += " " + content.decode(
                "utf-8", errors="replace"
            )
        else:
            details += " " + str(content)

    return (
        "uploadLimitExceeded" in details
        or "number of videos they may upload" in details.lower()
    )


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
    video_url = f"https://www.youtube.com/watch?v={video_id}"

    print(f"SUCCESS: Published {item['title']}")
    print(f"YOUTUBE URL: {video_url}")

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

    print(f"HISTORY SAVED: {item['title']}")


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

    history = load_history()

    print(
        f"Loaded {len(history)} previous publications."
    )

    # Retry pending videos before researching new topics.
    pending = load_pending()

    if pending:
        videos = pending

        print(
            f"Found {len(videos)} pending video(s). "
            "They will be retried before researching new topics."
        )

    else:
        feed_items = get_research()

        result = call_openrouter(
            feed_items,
            history
        )

        videos = validate_videos(
            result,
            feed_items,
            history
        )

        print("\nTODAY'S SELECTED TOPICS:")

        for index, item in enumerate(videos, 1):
            print(
                f"{index}. {item['topic']} - {item['title']}"
            )

    # Save scripts before rendering and uploading.
    save_pending(videos)

    youtube = make_youtube_client()

    for index, item in enumerate(videos, 1):

        print("")
        print(
            f"PROCESSING VIDEO {index}/{len(videos)}"
        )

        try:
            video_path = render_video(
                item,
                index
            )

            video_id, video_url = upload_one(
                youtube,
                item,
                video_path
            )

        except HttpError as exc:

            remaining = videos[index - 1:]
            save_pending(remaining)

            if is_upload_limit_error(exc):

                print("\nYOUTUBE DAILY UPLOAD LIMIT REACHED.")

                print(
                    f"{len(remaining)} video(s) "
                    "have been saved for the next run."
                )

                print(
                    "The remaining videos will be retried later."
                )

                return

            print(
                "Upload failed. Current and remaining "
                "videos were saved for retry."
            )

            raise

        except Exception:

            save_pending(videos[index - 1:])
            raise

        append_history(
            item,
            video_id,
            video_url
        )

        # Remove successfully uploaded video from pending queue.
        save_pending(videos[index:])

    print("\n" + "=" * 60)
    print("ALL QUEUED SHORTS HAVE BEEN UPLOADED.")
    print("PUBLICATION HISTORY UPDATED.")
    print("PENDING QUEUE EMPTY.")
    print("=" * 60)


# ============================================================
# START THE PROGRAM
# ============================================================

if __name__ == "__main__":
    main()