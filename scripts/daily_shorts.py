#!/usr/bin/env python3

import csv
import datetime as dt
import html
import json
import os
import re
import subprocess
import sys
import time
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
# GEMINI + GROK COLLABORATION
# ============================================================

GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-3.8-flash")
GROK_MODEL = os.getenv("GROK_MODEL", "grok-4.7")


def post_json(url, payload, headers, timeout=300, max_attempts=5):
    request_data = json.dumps(payload).encode("utf-8")
    retryable_statuses = {429, 500, 502, 503, 504}
    for attempt in range(1, max_attempts + 1):
        request = urllib.request.Request(
            url,
            data=request_data,
            headers={"Content-Type": "application/json", **headers},
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                return json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")
            if exc.code not in retryable_statuses or attempt == max_attempts:
                raise RuntimeError(
                    f"AI provider HTTP {exc.code} after {attempt} attempt(s): {detail[:1200]}"
                ) from exc
            wait_seconds = min(60, 5 * (2 ** (attempt - 1)))
            print(
                f"AI provider returned HTTP {exc.code}; "
                f"retrying in {wait_seconds}s ({attempt}/{max_attempts - 1})..."
            )
            time.sleep(wait_seconds)


def gemini_text(prompt):
    key = os.environ["GEMINI_API_KEY"]
    url = (
        "https://generativelanguage.googleapis.com/v1beta/models/"
        + GEMINI_MODEL + ":generateContent"
    )
    data = post_json(
        url,
        {
            "contents": [{"parts": [{"text": prompt}]}],
            "generationConfig": {"responseMimeType": "application/json"},
        },
        {"x-goog-api-key": key},
        timeout=300,
    )
    candidates = data.get("candidates", [])
    if not candidates:
        raise RuntimeError("Gemini returned no candidate.")
    return "".join(
        part.get("text", "")
        for part in candidates[0].get("content", {}).get("parts", [])
        if isinstance(part, dict)
    ).strip()


def grok_review(draft, articles):
    key = os.environ["XAI_API_KEY"]
    prompt = (
        "Act as a rigorous health-content editor. Review the draft below against "
        "the supplied source articles. Flag unsupported medical claims, causal "
        "overstatements, unsafe advice, repetition, weak hooks and unclear wording. "
        "Do not invent facts or sources. Return concise actionable feedback only.\n\n"
        "ARTICLES:\n" + json.dumps(articles, ensure_ascii=False)[:18000]
        + "\n\nDRAFT:\n" + json.dumps(draft, ensure_ascii=False)
    )
    data = post_json(
        "https://api.x.ai/v1/responses",
        {
            "model": GROK_MODEL,
            "input": [
                {"role": "system", "content": "You are a cautious medical-content reviewer. Never invent evidence."},
                {"role": "user", "content": prompt},
            ],
            "store": False,
            "max_output_tokens": 5000,
        },
        {"Authorization": "Bearer " + key},
        timeout=600,
    )
    texts = []
    for item in data.get("output", []):
        for part in item.get("content", []):
            if part.get("type") == "output_text":
                texts.append(part.get("text", ""))
    result = "\n".join(texts).strip()
    if not result:
        raise RuntimeError("Grok returned an empty review.")
    return result


def call_openrouter(feed_items, history):
    """Compatibility name: Gemini drafts, Grok critiques, Gemini revises."""
    previous = [
        {"topic": row.get("topic", ""), "title": row.get("title", "")}
        for row in history[-500:]
    ]
    prompt = f"""
You write evidence-based YouTube Shorts for adults 50+, in natural American English.
Create exactly THREE distinct scripts, each 85-120 spoken words, with a strong
first-3-second hook, practical value early, accurate claims, and 8-12 ordered
Pexels search phrases. Avoid medical diagnosis, cures, invented statistics,
unsupported causal claims, and advice to change medication. Use only supplied
articles and exact source URLs. Include this disclaimer in every description:
"This video is for educational purposes only and is not a substitute for professional medical advice."
Each object must contain topic,title,script,visual_terms,description,tags,source_urls.
Avoid these prior topics/titles: {json.dumps(previous, ensure_ascii=False)}
Research articles: {json.dumps(feed_items, ensure_ascii=False)}
Return JSON only in the shape {{"videos":[...]}}.
"""
    print("Gemini is drafting three health Shorts...")
    draft_text = gemini_text(prompt)
    try:
        draft = json.loads(draft_text)
    except json.JSONDecodeError as exc:
        raise RuntimeError("Gemini did not return valid JSON.") from exc
    if not isinstance(draft, dict) or not isinstance(draft.get("videos"), list):
        raise RuntimeError("Gemini response must contain a videos list.")

    if os.getenv("GROK_REVIEW_ENABLED", "false").strip().lower() in {"1", "true", "yes"}:
        require_env("XAI_API_KEY")
        print("Grok is reviewing medical accuracy, clarity and retention...")
        review = grok_review(draft, feed_items)
    else:
        review = (
            "Grok review skipped: GROK_REVIEW_ENABLED is false. "
            "Do not treat this as an independent medical review."
        )
        print("Grok review is disabled to prevent unexpected API charges.")

    print("Gemini is preparing the final scripts...")
    final_prompt = (
        "Revise this draft using the reviewer feedback when available. Keep exactly three videos "
        "and the required fields topic,title,script,visual_terms,description,tags,source_urls. "
        "Use only exact source URLs present in the articles. Every script must be "
        "85-120 words. Do not introduce unsupported claims. Return JSON only.\n\n"
        "ARTICLES:\n" + json.dumps(feed_items, ensure_ascii=False)
        + "\n\nDRAFT:\n" + json.dumps(draft, ensure_ascii=False)
        + "\n\nGROK REVIEW:\n" + review
    )
    final_text = gemini_text(final_prompt)
    try:
        result = json.loads(final_text)
    except json.JSONDecodeError as exc:
        raise RuntimeError("Gemini's final revision was not valid JSON.") from exc
    if not isinstance(result, dict) or not isinstance(result.get("videos"), list):
        raise RuntimeError("Gemini final response must contain a videos list.")
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

    visual_terms = item["visual_terms"]
    if isinstance(visual_terms, list):
        visual_terms = ", ".join(str(term) for term in visual_terms if str(term).strip())
    elif not isinstance(visual_terms, str):
        raise RuntimeError(
            f"visual_terms must be a string or list of strings, got {type(visual_terms).__name__}"
        )

    command = [
        sys.executable,
        "cli.py",

        "--video-script",
        item["script"],

        "--video-terms",
        visual_terms,

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
    print("DAILY RESEARCH - GEMINI + GROK - YOUTUBE SHORTS")
    print("=" * 60)

    dry_run = os.getenv("DRY_RUN", "true").strip().lower() in {"1", "true", "yes"}
    require_env("GEMINI_API_KEY", "PEXELS_API_KEY")
    if not dry_run:
        require_env("YT_CLIENT_ID", "YT_CLIENT_SECRET", "YT_REFRESH_TOKEN")

    print("DRY RUN ENABLED: videos will be rendered but NOT uploaded." if dry_run else "LIVE MODE: videos may be published publicly.")

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

    youtube = None if dry_run else make_youtube_client()

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

            if dry_run:
                print(f"DRY RUN: rendered locally, not uploaded: {video_path}")
                continue

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
    print("ALL QUEUED SHORTS HAVE BEEN RENDERED (DRY RUN; NOT UPLOADED)." if dry_run else "ALL QUEUED SHORTS HAVE BEEN UPLOADED.")
    print("PUBLICATION HISTORY UPDATED.")
    print("PENDING QUEUE EMPTY.")
    print("=" * 60)


# ============================================================
# START THE PROGRAM
# ============================================================

if __name__ == "__main__":
    main()