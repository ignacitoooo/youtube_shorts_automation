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
    # Try independent stable Flash variants when Google reports temporary
    # capacity errors. The environment variable can override this ordered list.
    fallback_models = [
        model.strip()
        for model in os.getenv(
            "GEMINI_FALLBACK_MODELS",
            "gemini-3.5-flash-lite,gemini-3.7-flash"
        ).split(",")
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
                # Keep a slow provider from consuming most of the workflow runtime.
                # Two bounded attempts are followed by the configured fallback model.
                timeout=180,
                max_attempts=2,
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
            retryable_provider_error = any(
                marker in str(exc).lower()
                for marker in (
                    "http 408", "http 429", "http 500", "http 502", "http 503", "http 504",
                    "network error", "timed out", "timeout", "urlopen error",
                )
            )
            if not retryable_provider_error or index == len(models) - 1:
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
    """Parse Gemini JSON, tolerating fences, prose, unquoted keys and trailing commas."""
    cleaned = (text or "").strip()
    if cleaned.startswith("```"):
        cleaned = re.sub(r"^```(?:json)?\s*", "", cleaned, flags=re.IGNORECASE)
        cleaned = re.sub(r"\s*```$", "", cleaned)

    start = cleaned.find("{")
    end = cleaned.rfind("}")
    if start < 0 or end <= start:
        raise RuntimeError(f"Gemini {stage} response did not contain a JSON object.")

    candidate = cleaned[start:end + 1]
    # Gemini occasionally emits JavaScript-style object keys or trailing commas.
    # Quote only bare property names immediately following an object/array delimiter.
    candidate = re.sub(
        r'([{,]\s*)([A-Za-z_][A-Za-z0-9_]*)\s*:',
        r'\1"\2":',
        candidate,
    )
    candidate = re.sub(r",\s*([}\]])", r"\1", candidate)

    try:
        value = json.loads(candidate)
    except json.JSONDecodeError as exc:
        snippet = candidate[max(0, exc.pos - 100):exc.pos + 100].replace("\n", " ")
        raise RuntimeError(
            f"Gemini {stage} response was not valid JSON "
            f"(line {exc.lineno}, column {exc.colno}; near: {snippet!r})."
        ) from exc

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
EDITORIAL FOCUS FOR UPCOMING VIDEOS: prioritize fresh, closely related angles on
nutrition and healthy aging, especially protein intake, amino-acid balance, muscle
preservation, dietary patterns, and metabolic aging. Prefer at least TWO of the
three Shorts to explore distinct angles in this cluster when the supplied research
articles genuinely support them; the horizontal video may also cover a distinct
related angle. Do NOT repeat the already-covered headline "eating less protein
could slow aging" or the prior angle about protein source and testosterone. Choose
new, specific questions such as how protein needs change with age, balancing
muscle preservation with metabolic health, or what evidence says about amino-acid
quality. If the supplied articles do not support a claim or angle, choose another
well-supported healthy-aging topic instead. Never invent research or force a
protein angle without a relevant supplied source.
Create exactly THREE distinct scripts, each 85-120 spoken words, with a strong
first-3-second hook, practical value early, accurate claims, and 8-12 ordered
Pexels search phrases. Use a visually concrete, natural documentary style: real
people, food, kitchens, gardens, everyday activity and close-up details where
relevant; avoid generic stock-footage concepts and avoid relying on one repeated
visual motif. Avoid medical diagnosis, cures, invented statistics,
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
        "Preserve the requested editorial focus on fresh nutrition-and-healthy-aging angles, "
        "especially protein, amino-acid balance, muscle preservation and metabolic aging, "
        "when the supplied verified articles support them. Do not repeat the previously covered "
        "general low-protein-aging or protein-source/testosterone angles. Keep the visual direction "
        "documentary and concrete, using varied real-life scenes relevant to each script. "
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

def repair_long_video_script(item, feed_items, valid_urls):
    """Append a source-grounded continuation; never rely on a full-script rewrite."""
    articles_by_url = {
        article["url"]: article
        for article in feed_items
        if article.get("url") in valid_urls
    }
    if not articles_by_url or not valid_urls:
        raise RuntimeError(
            f"Cannot repair long video without verified supplied sources: {item.get('title', 'untitled')}"
        )

    script = item["script"].strip()
    for attempt in range(1, 3):
        current_words = len(re.findall(r"\b[\w'-]+\b", script))
        if 600 <= current_words <= 750:
            item["script"] = script
            item["source_urls"] = list(valid_urls)
            print(f"Long video script repaired: {current_words} words; verified sources preserved.")
            return item
        if current_words > 750:
            raise RuntimeError(
                f"Long video script has {current_words} words after repair; expected 600-750. Nothing will be uploaded."
            )

        # Aim for a modest addition. The previous 120-word minimum caused
        # overshoots when Gemini returned a much longer continuation than requested.
        target_addition = max(40, min(120, 680 - current_words))
        prompt = (
            "Continue the narration below. Do NOT rewrite or summarize the existing script. "
            "Return JSON only with exactly one string field named continuation. "
            f"Write approximately {target_addition} new words, as 1-2 coherent paragraphs, "
            "that can be appended directly to the end of the existing narration. "
            f"The completed combined script should be about 650-690 words. "
            f"Keep the continuation under {max(1, 750 - current_words)} words; "
            "the completed script must never exceed 750 words. "
            "Add useful explanation, context, practical implications and a natural conclusion. "
            "Use ONLY facts explicitly supported by the verified source articles below. "
            "Do not invent statistics, diagnoses, cures, causal claims or medication advice. "
            "Do not repeat existing points or add, remove or change sources. "
            "If the sources do not support more detail, explain relevant limitations rather than inventing facts.\n\n"
            "TITLE: " + item["title"] + "\n"
            "TOPIC: " + item["topic"] + "\n"
            "VERIFIED SOURCE ARTICLES:\n"
            + json.dumps(list(articles_by_url.values()), ensure_ascii=False)
            + "\n\nEXISTING SCRIPT (continue from its ending):\n" + script
        )
        print(
            f"Requesting evidence-based long-video continuation ({attempt}/2); "
            f"current length: {current_words} words."
        )
        repaired = parse_gemini_json(
            gemini_text(prompt),
            f"Long video continuation {attempt}"
        )
        continuation = repaired.get("continuation")
        if not isinstance(continuation, str) or not continuation.strip():
            print("Gemini returned no usable continuation.")
            continue

        # Enforce the hard word limit locally: Gemini can exceed its requested
        # length substantially. Keep only complete sentences that fit the budget.
        max_additional_words = 750 - current_words
        sentences = re.split(r"(?<=[.!?])\s+", continuation.strip())
        accepted_parts = []
        accepted_count = 0
        for sentence in sentences:
            sentence_count = len(re.findall(r"\b[\w'-]+\b", sentence))
            if accepted_count + sentence_count > max_additional_words:
                break
            accepted_parts.append(sentence)
            accepted_count += sentence_count

        bounded_continuation = " ".join(accepted_parts).strip()
        if not bounded_continuation:
            print("Gemini continuation did not fit the remaining word budget.")
            continue

        script = script + "\n\n" + bounded_continuation
        new_count = len(re.findall(r"\b[\w'-]+\b", script))
        print(f"Combined long-video script now has {new_count} words after enforcing the 750-word limit.")
        if 600 <= new_count <= 750:
            item["script"] = script
            item["source_urls"] = list(valid_urls)
            print("Long video continuation accepted; verified sources preserved.")
            return item

    final_count = len(re.findall(r"\b[\w'-]+\b", script))
    raise RuntimeError(
        f"Gemini could not produce a 600-750 word long video after two continuation attempts; "
        f"final count: {final_count}. Nothing will be uploaded."
    )

def validate_long_video(result, feed_items, history, shorts):
    item = result.get("long_video")
    # Topic is classification metadata; use the title as a fallback if omitted.
    if isinstance(item, dict) and not item.get("topic") and item.get("title"):
        item["topic"] = item["title"].strip()
        print(f"Derived missing long-video topic from title: {item['title']}")
    required = ["topic", "title", "script", "visual_terms", "description", "source_urls", "tags"]
    if not isinstance(item, dict) or not all(item.get(key) for key in required):
        raise RuntimeError("Gemini did not provide a complete long_video object.")

    allowed_urls = {article["url"] for article in feed_items}
    short_topics = {normalize(short.get("topic", "")) for short in shorts}
    old_topics = {normalize(row.get("topic", "")) for row in history}
    excluded_topics = short_topics | old_topics

    # Gemini can occasionally ignore the distinct-topic instruction. Ask it for
    # a genuinely different long-form concept instead of aborting the workflow.
    for attempt in range(1, 3):
        topic = normalize(item.get("topic", ""))
        if topic and topic not in excluded_topics:
            break
        print(f"Long video topic duplicates a Short or previous publication; requesting a new topic ({attempt}/2).")
        prompt = (
            "Create ONE replacement horizontal YouTube health video for adults 50+ in natural American English. "
            "Its topic AND title must be clearly distinct from every excluded topic/title below and from one another. "
            "Do not merely rephrase an excluded topic. Choose a different evidence-based health subject supported "
            "by the supplied articles. Return JSON only with one object named long_video containing exactly these "
            "fields: topic,title,script,visual_terms,description,tags,source_urls. "
            "The spoken script must be 600-750 words. Include 12-18 useful ordered Pexels visual search phrases. "
            "Use only exact source URLs from the supplied articles. Do not invent facts, statistics, diagnoses, "
            "cures, causal claims or medication advice. Include the educational medical disclaimer in description.\n\n"
            "EXCLUDED TOPICS AND TITLES:\n" + json.dumps({
                "topics": sorted(excluded_topics),
                "previous_publications": [
                    {"topic": row.get("topic", ""), "title": row.get("title", "")}
                    for row in history[-500:]
                ],
                "shorts": [{"topic": s.get("topic", ""), "title": s.get("title", "")} for s in shorts],
            }, ensure_ascii=False)
            + "\n\nSUPPLIED SOURCE ARTICLES:\n"
            + json.dumps(feed_items, ensure_ascii=False)
        )
        replacement = parse_gemini_json(gemini_text(prompt), f"replacement long video {attempt}")
        candidate = replacement.get("long_video", replacement)
        if not isinstance(candidate, dict) or not all(candidate.get(key) for key in required):
            raise RuntimeError("Gemini did not provide a complete replacement long_video object.")
        item = candidate
    topic = normalize(item.get("topic", ""))
    if not topic or topic in excluded_topics:
        raise RuntimeError("Gemini could not generate a distinct long video topic after two attempts.")

    sources = item["source_urls"]
    if not isinstance(sources, list) or not sources or not set(sources).issubset(allowed_urls):
        raise RuntimeError("Long video contains missing or unverified source URLs.")

    words = len(re.findall(r"\b[\w'-]+\b", item["script"]))
    if not 600 <= words <= 750:
        print(f"Long video script has {words} words; requesting Gemini expansion.")
        item = repair_long_video_script(item, feed_items, sources)
        words = len(re.findall(r"\b[\w'-]+\b", item["script"]))

    if not 600 <= words <= 750:
        raise RuntimeError(f"Long video script has {words} words; expected 600-750.")

    terms = item["visual_terms"]
    if isinstance(terms, str):
        terms = [term.strip() for term in terms.split(",") if term.strip()]
    if not isinstance(terms, list) or len(terms) < 8:
        raise RuntimeError("Long video must include at least 8 visual search phrases.")
    item["visual_terms"] = terms
    print(f"Validated long video: {item['title']} ({words} words)")
    return item


def repair_short_script(video, feed_items, valid_urls):
    """Regenerate an out-of-range Short script without changing its verified sources."""
    articles_by_url = {
        article["url"]: article
        for article in feed_items
        if article.get("url") in valid_urls
    }
    if not articles_by_url:
        raise RuntimeError(
            f"Cannot repair Short without a verified supplied source: {video.get('title', 'untitled')}"
        )

    original_script = video["script"]
    for attempt in range(1, 3):
        prompt = (
            "Rewrite ONLY the spoken script for this evidence-based YouTube Short. "
            "Return JSON only as an object with exactly one string field named script. "
            "The script must contain 90-105 whitespace-separated words (never fewer than 85 "
            "or more than 120), sound natural when narrated, and retain a strong hook. "
            "Use only facts supported by the supplied source article(s); do not invent "
            "statistics, diagnoses, cures, causal claims, or medication advice. "
            "Do not add or change sources.\n\n"
            "TITLE: " + video["title"] + "\n"
            "TOPIC: " + video["topic"] + "\n"
            "VERIFIED SOURCE ARTICLES:\n"
            + json.dumps(list(articles_by_url.values()), ensure_ascii=False)
            + "\n\nCURRENT SCRIPT:\n" + original_script
        )
        print(
            f"Repairing Short script ({attempt}/2): {video['title']}"
        )
        repaired = parse_gemini_json(
            gemini_text(prompt),
            f"Short script repair {attempt}"
        )
        candidate = repaired.get("script")
        if not isinstance(candidate, str) or not candidate.strip():
            print("Gemini script repair returned no usable script.")
            continue

        word_count = len(candidate.split())
        if 85 <= word_count <= 120:
            video["script"] = candidate.strip()
            video["source_urls"] = list(valid_urls)
            print(
                f"Short script repaired and validated: {word_count} words."
            )
            return video

        print(
            f"Repaired script still has {word_count} words; "
            "it will be retried."
        )

    raise RuntimeError(
        f"Gemini could not produce an 85-120 word Short after two repairs: "
        f"{video.get('title', 'untitled')}. Nothing will be uploaded."
    )


def validate_videos(result, feed_items, history, target_count=3):
    """Accept three unique, source-backed Shorts; ask Gemini for replacements when needed."""
    videos = result.get("videos", [])
    if not isinstance(videos, list):
        raise RuntimeError("The AI response does not contain a valid videos list.")

    allowed_urls = {item["url"] for item in feed_items}
    old_topics = {normalize(row.get("topic", "")) for row in history}
    old_titles = {normalize(row.get("title", "")) for row in history}

    accepted = []
    seen_topics = set()
    seen_titles = set()
    rejected_topics = set()
    rejected_titles = set()

    def inspect_candidates(candidates):
        for video in candidates:
            if len(accepted) >= target_count:
                break
            if not isinstance(video, dict):
                continue
            # Topic, title, narration and traceable sources are essential.
            # Metadata can be safely completed locally if Gemini omits it.
            # Topic is classification metadata, not narration or source evidence.
            # Derive it from the generated title if Gemini omits it, rather than
            # discarding an otherwise complete, source-backed Short.
            if not video.get("topic") and video.get("title"):
                video["topic"] = video["title"].strip()
                print(f"Derived missing topic from title: {video['title']}")

            # A fallback Gemini model may omit source_urls even when the script
            # was drafted from the supplied research. Recover citations in one
            # bounded metadata-only call; exact-URL allowlist validation remains
            # authoritative and rejects invented or unrelated sources.
            if (
                not video.get("source_urls")
                and video.get("title")
                and video.get("script")
            ):
                source_prompt = (
                    'Select the exact source URL(s) from the supplied articles that directly '
                    'support this Short. Return JSON only: {"source_urls":[...]}. '
                    'Copy URLs verbatim; never invent URLs. If none directly supports it, '
                    'return an empty array. Do not rewrite the title or script.\\n\\n'
                    'TITLE: ' + str(video["title"]) + '\\nSCRIPT: '
                    + str(video["script"]) + '\\nARTICLES: '
                    + json.dumps(feed_items, ensure_ascii=False)
                )
                try:
                    source_result = parse_gemini_json(
                        gemini_text(source_prompt), "Short source metadata repair"
                    )
                    proposed_sources = source_result.get("source_urls")
                    if isinstance(proposed_sources, list):
                        video["source_urls"] = proposed_sources
                        print("Recovered Short source metadata; exact-URL validation will follow.")
                except Exception as exc:
                    print(f"Short source metadata repair failed: {exc}")

            essential = ["topic", "title", "script", "source_urls"]
            missing = [key for key in essential if not video.get(key)]
            if missing:
                print(
                    "Rejected AI candidate; missing essential fields: "
                    + ", ".join(missing)
                )
                continue

            if not video.get("visual_terms"):
                video["visual_terms"] = [
                    f"{video['topic']} healthy aging",
                    f"{video['topic']} lifestyle",
                    "older adults everyday activities",
                    "healthy active lifestyle"
                ]
                print(f"Filled missing visual_terms locally: {video.get('title', 'untitled')}")

            if not video.get("description"):
                video["description"] = (
                    f"{video['title']}\\n\\n"
                    "This video is for educational purposes only and is not a substitute "
                    "for professional medical advice."
                )
                print(f"Filled missing description locally: {video.get('title', 'untitled')}")

            if not isinstance(video.get("tags"), list):
                video["tags"] = []

            topic = normalize(video["topic"])
            title = normalize(video["title"])
            if not topic or not title:
                continue

            if topic in old_topics or topic in seen_topics:
                print(f"Repeated topic rejected: {topic}")
                rejected_topics.add(topic)
                continue
            if title in old_titles or title in seen_titles:
                print(f"Repeated title rejected: {title}")
                rejected_titles.add(title)
                continue

            source_urls = video["source_urls"]
            if not isinstance(source_urls, list):
                print(f"Rejected invalid source list: {title}")
                continue
            valid_urls = [url for url in source_urls if url in allowed_urls]
            if not valid_urls:
                print(f"Rejected without a supplied source: {title}")
                continue

            word_count = len(video["script"].split())
            if word_count < 85 or word_count > 120:
                print(f"Short script has {word_count} words; requesting a Gemini repair: {title}")
                video = repair_short_script(video, feed_items, valid_urls)
                word_count = len(video["script"].split())

            if word_count < 85 or word_count > 120:
                raise RuntimeError(
                    f"Short script remains outside 85-120 words after repair: {title}. Nothing will be uploaded."
                )

            video["source_urls"] = valid_urls
            accepted.append(video)
            seen_topics.add(topic)
            seen_titles.add(title)
            print(f"Accepted unique Short: {title} ({word_count} words)")

    inspect_candidates(videos)

    # Gemini sometimes ignores the exclusion list. Generate fresh replacements
    # instead of weakening duplicate-topic/title or source-verification rules.
    for attempt in range(1, 3):
        missing = target_count - len(accepted)
        if missing <= 0:
            break

        excluded = {
            "previous_topics": sorted(old_topics | seen_topics | rejected_topics),
            "previous_titles": sorted(old_titles | seen_titles | rejected_titles),
        }
        prompt = (
            "Create exactly " + str(missing) + " replacement YouTube Shorts in natural American English "
            "for adults 50+. These are replacements for rejected duplicate ideas. "
            "Every replacement must use a genuinely NEW topic and a NEW title not present in the exclusion lists. "
            "Each spoken script must be 85-120 words, ideally 90-105. "
            "Use only facts in the supplied articles and exact supplied source URLs. "
            "Do not invent statistics, diagnoses, cures, causal claims or medication advice. "
            "Include a strong opening, practical value, 8-12 ordered Pexels visual search phrases, "
            "description, tags and source_urls. Include the educational medical disclaimer in each description. "
            "Return JSON only in the shape {\"videos\":[...]}.\n\n"
            "EXCLUSIONS:\n" + json.dumps(excluded, ensure_ascii=False)
            + "\n\nSUPPLIED SOURCE ARTICLES:\n"
            + json.dumps(feed_items, ensure_ascii=False)
        )
        print(f"Requesting {missing} unique replacement Short(s) from Gemini ({attempt}/2)...")
        generated_text = gemini_text(prompt)
        try:
            generated = parse_gemini_json(generated_text, f"replacement Shorts {attempt}")
        except RuntimeError:
            generated = repair_content_json(generated_text, feed_items, f"replacement Shorts {attempt}")

        replacement_videos = generated.get("videos", [])
        if not isinstance(replacement_videos, list) or not replacement_videos:
            print("Gemini returned no usable replacement Shorts.")
            continue

        before_count = len(accepted)
        inspect_candidates(replacement_videos)
        if len(accepted) == before_count:
            print("No new valid Shorts accepted in this replacement attempt.")

    if len(accepted) != target_count:
        raise RuntimeError(
            f"Only {len(accepted)} of {target_count} requested valid unique Shorts passed validation after two replacement attempts. "
            "Duplicate and source protections remain enabled. Nothing will be uploaded."
        )

    return accepted


# ============================================================
# GENERATE VIDEO WITH MONEYPRINTERTURBO
# ============================================================

def validate_mp4(path, expected_aspect):
    """Use ffprobe to reject corrupt, empty, or incorrectly oriented renders."""
    if not path.is_file() or path.stat().st_size < 1024:
        raise RuntimeError(f"Rendered MP4 is missing or unexpectedly small: {path}")

    probe = subprocess.run(
        [
            "ffprobe", "-v", "error",
            "-show_entries", "format=duration",
            "-show_entries", "stream=codec_type,width,height",
            "-of", "json", str(path),
        ],
        capture_output=True,
        text=True,
        check=True,
    )
    metadata = json.loads(probe.stdout)
    video_streams = [
        stream for stream in metadata.get("streams", [])
        if stream.get("codec_type") == "video"
    ]
    if not video_streams:
        raise RuntimeError(f"Rendered MP4 has no video stream: {path}")

    width = int(video_streams[0].get("width", 0))
    height = int(video_streams[0].get("height", 0))
    duration = float(metadata.get("format", {}).get("duration", 0))
    if width <= 0 or height <= 0 or duration <= 0:
        raise RuntimeError(f"Rendered MP4 has invalid dimensions or duration: {path}")

    ratio = width / height
    if expected_aspect == "9:16":
        if ratio >= 1:
            raise RuntimeError(f"Expected vertical 9:16 video, got {width}x{height}.")
        if duration > 180:
            raise RuntimeError(f"Short exceeds YouTube Shorts 3-minute limit: {duration:.1f}s.")
    elif expected_aspect == "16:9":
        if ratio <= 1.2:
            raise RuntimeError(f"Expected horizontal 16:9 video, got {width}x{height}.")
        if not 180 <= duration <= 480:
            raise RuntimeError(
                f"Horizontal video duration {duration:.1f}s is outside the 3-8 minute safety window."
            )
    else:
        raise RuntimeError(f"Unsupported expected aspect ratio: {expected_aspect}")

    print(
        f"MP4 validated: {path.name}, {width}x{height}, "
        f"{duration:.1f}s, aspect {expected_aspect}"
    )
    return True


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
    validate_mp4(destination, aspect)

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
    consecutive_network_failures = 0

    while response is None:
        try:
            # Reuse the same resumable request after a transport interruption.
            # This lets YouTube report the committed byte range instead of
            # creating a second videos.insert request.
            status, response = request.next_chunk()
            consecutive_network_failures = 0
        except OSError as exc:
            consecutive_network_failures += 1
            if consecutive_network_failures > 3:
                print(
                    "YouTube upload transport failed after 3 retries: "
                    f"{exc}"
                )
                raise

            delay = min(5 * (2 ** (consecutive_network_failures - 1)), 20)
            print(
                "Transient network error during YouTube upload; "
                f"retrying the existing resumable session in {delay}s "
                f"(retry {consecutive_network_failures}/3): {exc}"
            )
            time.sleep(delay)
            continue

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
    print("DAILY RESEARCH - GEMINI - YOUTUBE SHORTS + LONG VIDEO")
    print("=" * 60)

    dry_run = os.getenv("DRY_RUN", "true").strip().lower() in {"1", "true", "yes"}
    require_env("GEMINI_API_KEY", "PEXELS_API_KEY")
    if not dry_run:
        require_env("YT_CLIENT_ID", "YT_CLIENT_SECRET", "YT_REFRESH_TOKEN")

    print("DRY RUN ENABLED: videos will be rendered but NOT uploaded." if dry_run else "LIVE MODE: videos may be published publicly.")

    history = load_history()
    print(f"Loaded {len(history)} previous publications.")

    # One durable queue for every format. Legacy queue entries without a
    # format marker are treated as Shorts for backwards compatibility.
    pending = load_pending()
    videos = list(pending)
    retry_pending_only = (
        os.getenv("RETRY_PENDING_ONLY", "false").strip().lower()
        in {"1", "true", "yes"}
    )

    if retry_pending_only:
        if not videos:
            raise RuntimeError(
                "RETRY_PENDING_ONLY is enabled but the pending queue is empty."
            )
        print(
            "RETRY_PENDING_ONLY enabled: processing the existing queue "
            "without researching or generating additional videos."
        )
    else:
        # A pending horizontal video must never suppress the daily Shorts batch.
        # Top up the queue independently by format: keep pending items, add Shorts
        # until there are three, and add one horizontal video only if none is queued.
        pending_shorts = [
            item for item in videos
            if item.get("_video_format", "short") == "short"
        ]
        has_pending_long = any(
            item.get("_video_format", "short") == "long"
            for item in videos
        )

        if videos:
            print(f"Found {len(videos)} pending video(s); retrying them first.")
        if len(pending_shorts) < 3 or not has_pending_long:
            feed_items = get_research()
            result = call_openrouter(feed_items, history)

            missing_shorts = max(0, 3 - len(pending_shorts))
            if missing_shorts:
                print(
                    f"Queue contains {len(pending_shorts)} pending Short(s); "
                    f"generating {missing_shorts} additional Short(s)."
                )
                new_shorts = validate_videos(
                    result, feed_items, history, target_count=missing_shorts
                )
                for item in new_shorts:
                    queued = dict(item)
                    queued["_video_format"] = "short"
                    videos.append(queued)

            if not has_pending_long:
                print("No horizontal video is pending; generating one.")
                current_shorts = [
                    item for item in videos
                    if item.get("_video_format", "short") == "short"
                ]
                long_video = validate_long_video(
                    result, feed_items, history, current_shorts
                )
                queued_long = dict(long_video)
                queued_long["_video_format"] = "long"
                videos.append(queued_long)

    if len(videos) == 0:
        raise RuntimeError("No videos available to process.")

    save_pending(videos)
    youtube = None if dry_run else make_youtube_client()
    queued_videos = list(videos)

    for index, item in enumerate(queued_videos, 1):
        video_format = item.get("_video_format", "short")
        is_long = video_format == "long"
        aspect = "16:9" if is_long else "9:16"
        label = "Long video" if is_long else "Short"

        print("")
        print(f"PROCESSING VIDEO {index}/{len(videos)} ({label})")

        try:
            video_path = render_video(
                item,
                index,
                aspect=aspect,
                label=label,
            )

            if dry_run:
                print(f"DRY RUN: rendered and validated, not uploaded: {video_path}")
                continue

            video_id, video_url = upload_one(youtube, item, video_path)
            append_history(item, video_id, video_url)

            # Remove each successful upload immediately; preserve all remaining
            # videos, including a horizontal video, if a later upload fails.
            save_pending(queued_videos[index:])

        except HttpError as exc:
            remaining = queued_videos[index - 1:]
            save_pending(remaining)

            if is_upload_limit_error(exc):
                print("\\nYOUTUBE DAILY UPLOAD LIMIT REACHED.")
                print(f"{len(remaining)} video(s) saved for the next run.")
                return

            print("YouTube upload failed; current and remaining videos were queued.")
            raise

        except Exception:
            save_pending(queued_videos[index - 1:])
            raise

    if dry_run:
        print("\\nDRY RUN COMPLETE: all queued videos were rendered and validated; none were uploaded.")
        print("The complete queue has been preserved for a later live run.")
    else:
        save_pending([])
        print("\\nALL QUEUED VIDEOS WERE UPLOADED AND RECORDED.")
    print("=" * 60)


# ============================================================
# START THE PROGRAM
# ============================================================

if __name__ == "__main__":
    main()