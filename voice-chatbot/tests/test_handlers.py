import importlib.util
import json
import os
import pathlib
from unittest import mock

os.environ.setdefault("AWS_DEFAULT_REGION", "us-east-1")
os.environ.setdefault("AWS_ACCESS_KEY_ID", "test")
os.environ.setdefault("AWS_SECRET_ACCESS_KEY", "test")

ROOT = pathlib.Path(__file__).resolve().parent.parent


def load(name, rel):
    spec = importlib.util.spec_from_file_location(name, ROOT / rel)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


chat = load("chat_app", "src/chat/app.py")
subscribe = load("subscribe_app", "src/subscribe/app.py")


def ev(body):
    return {"body": json.dumps(body)}


def test_chat_rejects_empty_message():
    assert chat.lambda_handler(ev({"message": "  "}), None)["statusCode"] == 400


def test_chat_rejects_bad_json_and_long_message():
    assert chat.lambda_handler({"body": "{oops"}, None)["statusCode"] == 400
    assert chat.lambda_handler(ev({"message": "x" * 501}), None)["statusCode"] == 400


def test_chat_rejects_bad_email():
    assert chat.lambda_handler(ev({"message": "hi", "email": "nope"}), None)["statusCode"] == 400


def test_chat_success_with_notification():
    with mock.patch.object(chat, "ask_llm", return_value="Hello!"), \
         mock.patch.object(chat, "synthesize", return_value="https://audio"), \
         mock.patch.object(chat, "notify") as n:
        res = chat.lambda_handler(ev({"message": "hi", "email": "A@b.com"}), None)
    body = json.loads(res["body"])
    assert res["statusCode"] == 200 and body["reply"] == "Hello!" and body["audio_url"] == "https://audio"
    assert body["notified"] is True
    n.assert_called_once_with("a@b.com", "hi", "Hello!", "https://audio")


def test_chat_returns_502_when_llm_fails():
    with mock.patch.object(chat, "ask_llm", side_effect=chat.LLMError("boom")):
        assert chat.lambda_handler(ev({"message": "hi"}), None)["statusCode"] == 502


def test_chat_degrades_when_polly_fails():
    with mock.patch.object(chat, "ask_llm", return_value="Hello!"), \
         mock.patch.object(chat, "synthesize", side_effect=RuntimeError("polly down")):
        res = chat.lambda_handler(ev({"message": "hi"}), None)
    assert res["statusCode"] == 200 and json.loads(res["body"])["audio_url"] is None


def test_subscribe_validates_and_subscribes():
    assert subscribe.lambda_handler(ev({"email": "bad"}), None)["statusCode"] == 400
    with mock.patch.object(subscribe, "sns") as sns_mock:
        res = subscribe.lambda_handler(ev({"email": "Me@x.com"}), None)
    assert res["statusCode"] == 200
    kwargs = sns_mock.subscribe.call_args.kwargs
    assert kwargs["Endpoint"] == "me@x.com"
    assert json.loads(kwargs["Attributes"]["FilterPolicy"]) == {"email": ["me@x.com"]}
