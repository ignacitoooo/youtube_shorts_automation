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

The publication history below contains topics already
published by this channel. Do not repeat an existing
or very similar topic, advice, or research angle.
A new title does NOT make an old topic new.

If three genuinely different topics are not supported,
return fewer than three. Never invent topics.

PREVIOUSLY PUBLISHED:
{json.dumps(previous, ensure_ascii=False)}

OFFICIAL NIH RESEARCH ARTICLES:
{json.dumps(feed_items, ensure_ascii=False)}

OUTPUT RULES:
Return ONE complete, syntactically valid JSON object only.
Do not use Markdown fences or commentary.
Use double quotes for every JSON key and string.
Escape quotation marks and newlines inside strings.
Do not add trailing commas.
Do not stop before closing every bracket and brace.

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

    api_key = os.environ["OPENROUTER_API_KEY"]

    conversation = [
        {
            "role": "system",
            "content": (
                "You are a careful health research assistant. "
                "Return one complete valid JSON object only. "
                "Never invent medical research or source URLs. "
                "Do not repeat previously published topics."
            ),
        },
        {
            "role": "user",
            "content": prompt,
        },
    ]

    last_error = None

    for attempt in range(1, 4):

        payload = {
            "model": MODEL,
            "messages": conversation,
            "temperature": 0.1,
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

        except (urllib.error.URLError, TimeoutError) as exc:
            last_error = f"Network error: {exc}"
            print(last_error)

            if attempt == 3:
                break

            continue

        choices = data.get("choices", [])

        if not choices:
            last_error = (
                "OpenRouter returned no choices: "
                + json.dumps(data)[:1500]
            )

            if attempt == 3:
                break

            conversation.extend([
                {
                    "role": "assistant",
                    "content": "",
                },
                {
                    "role": "user",
                    "content": (
                        "Your previous response was empty or invalid. "
                        "Return the complete JSON object now, "
                        "following the exact schema. "
                        "Do not add any explanation."
                    ),
                },
            ])

            continue

        message = choices[0].get("message", {})
        content = message.get("content", "")

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

        text = re.sub(
            r"\s*```$",
            "",
            text
        )

        first = text.find("{")
        last = text.rfind("}")

        try:
            if first < 0 or last <= first:
                raise json.JSONDecodeError(
                    "No complete JSON object found",
                    text,
                    0
                )

            result = json.loads(
                text[first:last + 1]
            )

            if not isinstance(result, dict):
                raise ValueError(
                    "JSON must be an object"
                )

            if not isinstance(result.get("videos"), list):
                raise ValueError(
                    "JSON must contain a videos list"
                )

            if not result["videos"]:
                raise ValueError(
                    "The videos list is empty"
                )

            print(
                "OpenRouter returned valid JSON with "
                f"{len(result['videos'])} video entries."
            )

            return result

        except (json.JSONDecodeError, ValueError) as exc:

            last_error = str(exc)

            print(
                f"Invalid OpenRouter JSON on attempt "
                f"{attempt}: {last_error}"
            )

            if attempt < 3:

                conversation = [
                    {
                        "role": "system",
                        "content": (
                            "You repair JSON. Return only one "
                            "complete valid JSON object. "
                            "Preserve the supplied content and schema. "
                            "Do not use Markdown."
                        ),
                    },
                    {
                        "role": "user",
                        "content": (
                            "The following response is invalid JSON. "
                            "Correct its syntax, preserve the intended "
                            "content, and return the complete object "
                            "with a videos array. Do not truncate it "
                            "and do not add commentary.\n\n"
                            + text[:14000]
                        ),
                    },
                ]

    raise RuntimeError(
        "OpenRouter did not provide valid JSON after 3 attempts. "
        f"Last error: {last_error}. No videos were uploaded."
    )
