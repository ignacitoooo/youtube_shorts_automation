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


def post_json(url, payload, headers, timeout=300, max_attempts=6):
    """POST JSON with bounded retries for temporary provider/network failures."""
    request_data = json.dumps(payload).encode("utf-8")
    retryable_statuses = {408, 429, 500, 502, 503, 504}
    last_error = None

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
            last_error = exc
            if exc.code not in retryable_statuses or attempt == max_attempts:
                raise RuntimeError(
                    f"AI provider HTTP {exc.code} after {attempt} attempt(s): {detail[:1200]}"
                ) from exc

            retry_after = exc.headers.get("Retry-After")
            try:
                wait_seconds = float(retry_after) if retry_after else min(90, 10 * (2 ** (attempt - 1)))
            except (TypeError, ValueError):
                wait_seconds = min(90, 10 * (2 ** (attempt - 1)))

        except (urllib.error.URLError, TimeoutError) as exc:
            last_error = exc
            if attempt == max_attempts:
                raise RuntimeError(
                    f"AI provider network error after {attempt} attempts: {exc}"
                ) from exc
            wait_seconds = min(90, 10 * (2 ** (attempt - 1)))

        print(
            f"Temporary AI provider error; retrying in {wait_seconds:g}s "
            f"({attempt}/{max_attempts - 1})..."
        )
        time.sleep(wait_seconds)

    raise RuntimeError(f"AI provider request failed: {last_error}")


def gemini_text(prompt):
    """Use the configured Gemini model, then an official Flash-Lite fallback on capacity errors."""
    key = os.environ["GEMINI_API_KEY"]
    fallback_models = [
        model.strip()
        for model in os.getenv("GEMINI_FALLBACK_MODELS", "gemini-3.5-flash-lite").split(",")
        if model.strip() and model.strip() != GEMINI_MODEL
    ]
    models = [GEMINI_MODEL] + fallback_models
    payload = {
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {"responseMimeType": "application/json"},
    }
    last_error = None

    for index, model in enumerate(models):
        url = (
            "https://generativelanguage.googleapis.com/v1beta/models/"
            + model + ":generateContent"
        )
        try:
            print(f"Requesting Gemini model: {model}")
            data = post_json(
                url,
                payload,
                {"x-goog-api-key": key},
                timeout=300,
                max_attempts=3,
            )
            candidates = data.get("candidates", [])
            if not candidates:
                raise RuntimeError(f"Gemini model {model} returned no candidate.")
            text = "".join(
                part.get("text", "")
                for part in candidates[0].get("content", {}).get("parts", [])
                if isinstance(part, dict)
            ).strip()
            if not text:
                raise RuntimeError(f"Gemini model {model} returned empty text.")
            print(f"Gemini response received from {model}.")
            return text
        except RuntimeError as exc:
            last_error = exc
            retryable_capacity_error = any(
                marker in str(exc) for marker in ("HTTP 429", "HTTP 500", "HTTP 502", "HTTP 503", "HTTP 504")
            )
            if not retryable_capacity_error or index == len(models) - 1:
                raise
            print(
                f"Gemini model {model} is unavailable; switching to "
                f"{models[index + 1]} without using a paid provider."
            )
            time.sleep(10)

    raise RuntimeError(f"All configured Gemini models failed: {last_error}")


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


def parse_gemini_json(text, stage):
    """Parse strict JSON while tolerating Markdown fences and surrounding prose."""
    cleaned = (text or "").strip()
    if cleaned.startswith("```"):
        cleaned = re.sub(r"^\`\`\`(?:json)?\\s*", "", cleaned, flags=re.IGNORECASE)
        cleaned = re.sub(r"\\s*\`\`\`$", "", cleaned)
    try:
        value = json.loads(cleaned)
    except json.JSONDecodeError:
        start = cleaned.find("{")
        end = cleaned.rfind("}")
        if start < 0 or end <= start:
            raise RuntimeError(f"Gemini {stage} response did not contain a JSON object.")
        try:
            value = json.loads(cleaned[start:end + 1])
        except json.JSONDecodeError as exc:
            raise RuntimeError(f"Gemini {stage} response was not valid JSON.") from exc
    if not isinstance(value, dict):
        raise RuntimeError(f"Gemini {stage} response must be a JSON object.")
    return value


def ensure_content_shape(value, stage):
    if not isinstance(value.get("videos"), list) or len(value["videos"]) != 3:
        raise RuntimeError(f"Gemini {stage} response must contain exactly three videos.")
    if not isinstance(value.get("long_video"), dict):
        raise RuntimeError(f"Gemini {stage} response must contain a long_video object.")
    return value


def repair_content_json(raw_text, articles, stage):
    repair_prompt = (
        "Convert the response below into valid JSON only. Preserve its factual content and "
        "source URLs; do not add facts. Required exact shape: "
        '{"videos":[THREE objects with topic,title,script,visual_terms,description,tags,source_urls],'
        '"long_video":{"topic","title","script","visual_terms","description","tags","source_urls"}}. '
        "The long_video script must be 600-750 words. Return no Markdown fences or commentary.\n\n"
        "Allowed source articles and URLs:\n" + json.dumps(articles, ensure_ascii=False)
        + "\n\nMalformed response:\n" + (raw_text or "")[:24000]
    )
    print(f"Requesting one JSON-format repair for Gemini {stage} response...")
    repaired = parse_gemini_json(gemini_text(repair_prompt), stage + " repaired")
    return ensure_content_shape(repaired, stage + " repaired")


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
Also create exactly ONE horizontal long-form video:
- 600-750 spoken words, approximately five minutes.
- A distinct topic not used by any Short.
- A clear opening, 3-5 developed sections, practical takeaways and concise conclusion.
- Evidence-based health education for adults 50+; no diagnosis, cure claims, invented statistics or medication changes.
- Use only supplied articles and exact source URLs.
- Include 12-18 ordered Pexels visual search phrases, description and tags.
- Required long_video fields: topic,title,script,visual_terms,description,tags,source_urls.
Return JSON only in the shape {{"videos":[...],"long_video":{{...}}}}.
"""
    print("Gemini is drafting three health Shorts...")
    draft_text = gemini_text(prompt)
    try:
        draft = ensure_content_shape(parse_gemini_json(draft_text, "draft"), "draft")
    except RuntimeError:
        draft = repair_content_json(draft_text, feed_items, "draft")

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
        "Revise this draft using the reviewer feedback when available. Keep exactly three Shorts "
        "and exactly one long_video. Shorts must be 85-120 words. The long_video must be "
        "600-750 words for approximately five minutes of narration, with fields "
        "topic,title,script,visual_terms,description,tags,source_urls. Use only exact source URLs "
        "present in supplied articles. Do not introduce unsupported claims. Return JSON only.\n\n"
        "ARTICLES:\n" + json.dumps(feed_items, ensure_ascii=False)
        + "\n\nDRAFT:\n" + json.dumps(draft, ensure_ascii=False)
        + "\n\nGROK REVIEW:\n" + review
    )
    final_text = gemini_text(final_prompt)
    try:
        result = ensure_content_shape(parse_gemini_json(final_text, "final"), "final")
    except RuntimeError:
        result = repair_content_json(final_text, feed_items, "final")
    return result


# ============================================================
# CHECK NEW TOPICS
# ============================================================

def validate_long_video(result, feed_items, history, shorts):
    item = result.get("long_video")
    required = ["topic", "title", "script", "visual_terms", "description", "source_urls", "tags"]
    if not isinstance(item, dict) or not all(item.get(key) for key in required):
        raise RuntimeError("Gemini did not provide a complete long_video object.")

    words = len(re.findall(r"\b[\w'-]+\b", item["script"]))
    if not 600 <= words <= 750:
        raise RuntimeError(f"Long video script has {words} words; expected 600-750.")

    allowed_urls = {article["url"] for article in feed_items}
    sources = item["source_urls"]
    if not isinstance(sources, list) or not sources or not set(sources).issubset(allowed_urls):
        raise RuntimeError("Long video contains missing or unverified source URLs.")

    topic = normalize(item["topic"])
    short_topics = {normalize(short.get("topic", "")) for short in shorts}
    old_topics = {normalize(row.get("topic", "")) for row in history}
    if topic in short_topics or topic in old_topics:
        raise RuntimeError("Long video topic duplicates a Short or a previous publication.")

    terms = item["visual_terms"]
    if isinstance(terms, str):
        terms = [term.strip() for term in terms.split(",") if term.strip()]
    if not isinstance(terms, list) or len(terms) < 8:
        raise RuntimeError("Long video must include at least 8 visual search phrases.")
    item["visual_terms"] = terms
    print(f"Validated long video: {item['title']} ({words} words)")
    return item


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

def render_video(item, index, aspect="9:16", label="Short"):

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
        aspect,

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
        f"Generating {label} {index}: {item['title']}"
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

    long_video = None
    if pending:
        videos = pending

        print(
            f"Found {len(videos)} pending Short(s). "
            "They will be retried, while a fresh long-form video is generated."
        )

        # A pending Shorts queue must never suppress the daily horizontal video.
        feed_items = get_research()
        fresh_result = call_openrouter(feed_items, history)
        fresh_shorts = validate_videos(fresh_result, feed_items, history)
        long_video = validate_long_video(
            fresh_result, feed_items, history, fresh_shorts
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
        long_video = validate_long_video(result, feed_items, history, videos)

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
    print("ALL SHORTS AND LONG VIDEO RENDERED (DRY RUN; NOT UPLOADED)." if dry_run and long_video is not None else ("ALL QUEUED SHORTS HAVE BEEN RENDERED (DRY RUN; NOT UPLOADED)." if dry_run else "ALL QUEUED VIDEOS HAVE BEEN UPLOADED."))
    if long_video is None:
        raise RuntimeError("No long-form video was generated; refusing to report a complete run.")

    print("\\nPROCESSING HORIZONTAL LONG-FORM VIDEO")
    long_path = render_video(long_video, 4, aspect="16:9", label="Long video")
    if not os.path.isfile(long_path) or os.path.getsize(long_path) == 0:
        raise RuntimeError(f"Long-form render did not produce a valid MP4: {long_path}")
    if dry_run:
        print(f"DRY RUN: long video rendered locally, not uploaded: {long_path}")
    else:
        long_id, long_url = upload_one(youtube, long_video, long_path)
        append_history(long_video, long_id, long_url)

    print("PUBLICATION HISTORY UPDATED.")
    print("PENDING QUEUE EMPTY.")
    print("=" * 60)


# ============================================================
# START THE PROGRAM
# ============================================================

if __name__ == "__main__":
    main()