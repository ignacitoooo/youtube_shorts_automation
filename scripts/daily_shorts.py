
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

Audience: adults over 50.
All titles, scripts and descriptions must be in US English.

Use the supplied official NIH research articles.
Choose exactly THREE distinct topics for three YouTube Shorts.

Do not repeat any previous topic, angle or practical advice.
Do not invent medical facts, statistics or sources.
Do not promise cures or guaranteed prevention.
Use cautious, evidence-aware language.

Each script must contain 90-125 spoken words.
Each video must have at least one source URL copied exactly
from the supplied articles.

PREVIOUSLY PUBLISHED:
{json.dumps(previous, ensure_ascii=False)}

RESEARCH ARTICLES:
{json.dumps(feed_items, ensure_ascii=False)}

Return ONLY one complete JSON object.
Do not use Markdown fences.
Do not add explanations before or after the JSON.
Do not stop before closing all brackets.

Required structure:

{{
  "videos": [
    {{
      "topic": "topic description",
      "title": "Short title",
      "script": "Complete spoken script",
      "visual_terms": "Pexels search phrases separated by commas",
      "description": "Description and medical disclaimer with hashtags",
      "tags": ["tag one", "tag two"],
      "source_urls": ["exact source URL"]
    }}
  ]
}}
"""

    payload = {
        "model": "openrouter/free",
        "messages": [
            {
                "role": "system",
                "content": (
                    "Return valid JSON only. "
                    "Never invent research sources. "
                    "Complete every JSON bracket."
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
                "Bearer " + os.environ["OPENROUTER_API_KEY"]
            ),
            "HTTP-Referer": "https://github.com",
            "X-Title": "The Healthy Years 404"
        },
        method="POST"
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
            "utf-8", errors="replace"
        )
        raise RuntimeError(
            f"OpenRouter API error {exc.code}: {details[:2000]}"
        ) from exc

    choices = data.get("choices", [])

    if not choices:
        raise RuntimeError(
            "OpenRouter returned no choices: "
            + json.dumps(data)[:2000]
        )

    text = choices[0].get(
        "message", {}
    ).get("content", "")

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

    # Extract the JSON object if the model adds text around it.
    start = text.find("{")
    end = text.rfind("}")

    if start == -1 or end == -1 or end <= start:
        raise RuntimeError(
            "OpenRouter did not return a complete JSON object. "
            "Response: " + text[:2500]
        )

    json_text = text[start:end + 1]

    try:
        result = json.loads(json_text)

    except json.JSONDecodeError as exc:
        raise RuntimeError(
            f"Invalid JSON from OpenRouter: {exc}. "
            "Response: " + text[:2500]
        ) from exc

    if not isinstance(result, dict):
        raise RuntimeError("The AI response is not a JSON object.")

    return result
