"""POST /subscribe: subscribe an email to the SNS topic, filtered so it only receives its own replies."""
import json
import logging
import os
import re

import boto3

logger = logging.getLogger()
logger.setLevel(logging.INFO)

TOPIC_ARN = os.environ.get("TOPIC_ARN", "")
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
sns = boto3.client("sns")


def respond(status, body):
    return {"statusCode": status, "headers": {"Content-Type": "application/json"}, "body": json.dumps(body)}


def lambda_handler(event, context):
    try:
        body = json.loads(event.get("body") or "{}")
    except json.JSONDecodeError:
        return respond(400, {"error": "Request body must be valid JSON."})
    email = str(body.get("email", "")).strip().lower() if isinstance(body, dict) else ""
    if not EMAIL_RE.match(email):
        return respond(400, {"error": "A valid 'email' is required."})

    try:
        sns.subscribe(
            TopicArn=TOPIC_ARN,
            Protocol="email",
            Endpoint=email,
            Attributes={"FilterPolicy": json.dumps({"email": [email]})},
        )
    except Exception:
        logger.exception("SNS subscribe failed")
        return respond(502, {"error": "Could not create the subscription. Please try again."})
    logger.info(json.dumps({"event": "subscribe_requested"}))
    return respond(200, {"message": "Check your inbox and confirm the subscription."})
