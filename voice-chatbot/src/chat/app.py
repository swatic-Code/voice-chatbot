"""POST /chat: message -> OpenRouter LLM -> Polly MP3 -> S3 (pre-signed URL) -> optional SNS email."""
import json
import logging
import os
import re
import urllib.error
import urllib.request
import uuid

import boto3
from botocore.config import Config

logger = logging.getLogger()
logger.setLevel(logging.INFO)

AUDIO_BUCKET = os.environ.get("AUDIO_BUCKET", "")
TOPIC_ARN = os.environ.get("TOPIC_ARN", "")
OPENROUTER_API_KEY = os.environ.get("OPENROUTER_API_KEY", "")
MODEL_ID = os.environ.get("MODEL_ID", "openai/gpt-4o-mini")
POLLY_VOICE = os.environ.get("POLLY_VOICE", "Joanna")

OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"
MAX_MESSAGE_CHARS = 500
MAX_HISTORY = 6
POLLY_MAX_CHARS = 2900  # Polly limit is 3000 billed characters
URL_TTL_SECONDS = 3600
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
SYSTEM_PROMPT = (
    "You are a friendly voice assistant. Answer in at most three short sentences "
    "of plain spoken English. No markdown, lists, or emojis."
)

s3 = boto3.client("s3", config=Config(signature_version="s3v4", s3={"addressing_style": "virtual"}))
polly = boto3.client("polly")
sns = boto3.client("sns")


class LLMError(Exception):
    """Raised when the OpenRouter call fails or returns an unusable response."""


def respond(status, body):
    return {"statusCode": status, "headers": {"Content-Type": "application/json"}, "body": json.dumps(body)}


def parse_request(event):
    """Validate the request body. Raises ValueError with a user-safe message."""
    try:
        body = json.loads(event.get("body") or "{}")
    except json.JSONDecodeError:
        raise ValueError("Request body must be valid JSON.")
    if not isinstance(body, dict):
        raise ValueError("Request body must be a JSON object.")

    message = body.get("message")
    if not isinstance(message, str) or not message.strip():
        raise ValueError("'message' is required.")
    message = message.strip()
    if len(message) > MAX_MESSAGE_CHARS:
        raise ValueError(f"'message' must be at most {MAX_MESSAGE_CHARS} characters.")

    history = []
    for item in (body.get("history") or [])[-MAX_HISTORY:]:
        if (
            isinstance(item, dict)
            and item.get("role") in ("user", "assistant")
            and isinstance(item.get("content"), str)
        ):
            history.append({"role": item["role"], "content": item["content"][:MAX_MESSAGE_CHARS * 2]})

    email = body.get("email")
    if email:
        email = str(email).strip().lower()
        if not EMAIL_RE.match(email):
            raise ValueError("'email' is not a valid address.")
    return message, history, email or None


def ask_llm(message, history):
    payload = {
        "model": MODEL_ID,
        "max_tokens": 200,
        "messages": [{"role": "system", "content": SYSTEM_PROMPT}, *history, {"role": "user", "content": message}],
    }
    req = urllib.request.Request(
        OPENROUTER_URL,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Authorization": f"Bearer {OPENROUTER_API_KEY}", "Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            data = json.loads(resp.read())
        text = data["choices"][0]["message"]["content"].strip()
    except urllib.error.HTTPError as exc:
        raise LLMError(f"OpenRouter returned HTTP {exc.code}") from exc
    except (urllib.error.URLError, TimeoutError, KeyError, IndexError, AttributeError, json.JSONDecodeError) as exc:
        raise LLMError(f"OpenRouter call failed: {type(exc).__name__}") from exc
    if not text:
        raise LLMError("OpenRouter returned an empty reply")
    return text


def synthesize(text):
    """Convert text to MP3 with Polly, store it in S3, return a pre-signed GET URL."""
    result = polly.synthesize_speech(
        Text=text[:POLLY_MAX_CHARS], OutputFormat="mp3", VoiceId=POLLY_VOICE, Engine="neural"
    )
    key = f"audio/{uuid.uuid4()}.mp3"
    s3.put_object(Bucket=AUDIO_BUCKET, Key=key, Body=result["AudioStream"].read(), ContentType="audio/mpeg")
    return s3.generate_presigned_url(
        "get_object", Params={"Bucket": AUDIO_BUCKET, "Key": key}, ExpiresIn=URL_TTL_SECONDS
    )


def notify(email, question, reply, audio_url):
    lines = [f"You asked: {question}", "", f"Reply: {reply}"]
    if audio_url:
        lines += ["", f"Listen (link valid for {URL_TTL_SECONDS // 60} minutes): {audio_url}"]
    sns.publish(
        TopicArn=TOPIC_ARN,
        Subject="Your voice chatbot reply",
        Message="\n".join(lines),
        MessageAttributes={"email": {"DataType": "String", "StringValue": email}},
    )


def lambda_handler(event, context):
    request_id = getattr(context, "aws_request_id", "local")
    try:
        message, history, email = parse_request(event)
    except ValueError as exc:
        logger.warning(json.dumps({"request_id": request_id, "event": "bad_request", "reason": str(exc)}))
        return respond(400, {"error": str(exc)})

    try:
        reply = ask_llm(message, history)
    except LLMError:
        logger.exception("LLM call failed request_id=%s", request_id)
        return respond(502, {"error": "The AI service is unavailable. Please try again."})

    audio_url = None
    try:
        audio_url = synthesize(reply)
    except Exception:  # degrade gracefully: still return the text reply
        logger.exception("Audio generation failed request_id=%s", request_id)

    notified = False
    if email:
        try:
            notify(email, message, reply, audio_url)
            notified = True
        except Exception:
            logger.exception("SNS publish failed request_id=%s", request_id)

    logger.info(json.dumps({
        "request_id": request_id, "event": "chat_ok", "message_chars": len(message),
        "reply_chars": len(reply), "audio": bool(audio_url), "notified": notified, "model": MODEL_ID,
    }))
    return respond(200, {"reply": reply, "audio_url": audio_url, "notified": notified})
