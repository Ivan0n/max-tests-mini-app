"""Small MAX chat bot for local development and testing."""

import json
import logging
import os
import re
import signal
import ssl
from pathlib import Path
from threading import Event
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from dotenv import load_dotenv


API_URL = "https://platform-api2.max.ru"
MESSAGES_PATH = Path(__file__).parent / "messages.json"
# MAX allows at most 3 buttons in a row that contains an open_app button.
OPEN_APP_ROW_LIMIT = 3
# Start parameter rules from https://dev.max.ru/docs/webapps/introduction
START_PARAM_RE = re.compile(r"[A-Za-z0-9_-]{1,512}")
logger = logging.getLogger(__name__)


def _valid_button_text(button):
    text = button.get("text")
    return isinstance(text, str) and bool(text.strip()) and len(text) <= 128


def _is_open_app(button):
    return isinstance(button, dict) and button.get("type") == "open_app"


def _validate_open_app_button(button):
    """{"type": "open_app", "text": ..., "payload": optional start_param for initData}."""
    payload = button.get("payload")
    return (set(button) <= {"type", "text", "payload"} and _valid_button_text(button)
            and (payload is None or isinstance(payload, str) and START_PARAM_RE.fullmatch(payload)))


def _one_line(value, limit):
    """Collapse user-entered text to one trimmed line for the invite message."""
    if not isinstance(value, str):
        return ""
    value = " ".join(value.split())
    return value if len(value) <= limit else value[:limit - 1] + "…"


def test_share_text(title, question_count, subject=None, author=None):
    """Text shared with WebApp.shareMaxContent({text, link}); MAX appends the link after it."""
    lines = [f"📝 {_one_line(title, 200)}"]
    if _one_line(subject, 100):
        lines.append(f"📚 {_one_line(subject, 100)}")
    if _one_line(author, 200):
        lines.append(f"👤 Составитель: {_one_line(author, 200)}")
    lines += [f"📋 Заданий: {question_count}", "", "Пройти тест 👇"]
    return "\n".join(lines)


MESSAGE_FORMAT_KEY = "format"
MESSAGE_FORMATS = ("markdown", "html")
# In the greeting text this is replaced with a link to the user's own profile.
USER_PLACEHOLDER = "[Имя Фамилия](max://user/user_id)"
_MARKDOWN_SPECIAL = re.compile(r"([\\`*_{}\[\]()#+\-.!|~<>])")


class Messages(dict):
    """Menu messages from messages.json plus the optional text format."""
    format = None


def personalize(text, user=None):
    """Fill USER_PLACEHOLDER with the user's name as a link to their profile."""
    if USER_PLACEHOLDER not in text:
        return text
    user = user if isinstance(user, dict) else {}
    name = " ".join(part.strip() for part in (user.get("first_name"), user.get("last_name"))
                    if isinstance(part, str) and part.strip())
    name = name or (user.get("name") if isinstance(user.get("name"), str) else "") or "друг"
    user_id = user.get("user_id")
    if type(user_id) is not int or user_id <= 0:
        return text.replace(USER_PLACEHOLDER, name)
    safe_name = _MARKDOWN_SPECIAL.sub(lambda match: "\\" + match.group(1), name)
    return text.replace(USER_PLACEHOLDER, f"[{safe_name}](max://user/{user_id})")


def load_messages(path=MESSAGES_PATH):
    """Validate editable copy and all menu links before connecting to MAX."""
    try:
        with Path(path).open(encoding="utf-8") as source:
            messages = json.load(source)
    except (OSError, ValueError) as error:
        raise ValueError(f"Cannot load messages.json: {error}") from None
    if not isinstance(messages, dict) or "start" not in messages:
        raise ValueError("messages.json must contain a start message")
    # Optional top-level "format": how MAX renders every message text ("markdown" or "html").
    text_format = messages.pop(MESSAGE_FORMAT_KEY, None)
    if text_format not in (None, *MESSAGE_FORMATS):
        raise ValueError(f"messages.json: format must be one of {', '.join(MESSAGE_FORMATS)}")
    messages = Messages(messages)
    messages.format = text_format
    for key, message in messages.items():
        if not isinstance(key, str) or not 1 <= len(key) <= 1024 or not isinstance(message, dict):
            raise ValueError("Invalid message in messages.json")
        text = message.get("text")
        rows = message.get("buttons", [])
        if isinstance(rows, list):
            # A button written without its own [ ] is a row of one button.
            rows = message["buttons"] = [[row] if isinstance(row, dict) else row for row in rows]
        if not isinstance(text, str) or not text.strip() or len(text) > 4000:
            raise ValueError(f"messages.json: {key}.text must contain 1–4000 characters")
        if not isinstance(rows, list) or len(rows) > 30:
            raise ValueError(f"messages.json: {key}.buttons must contain at most 30 rows")
        for row_index, row in enumerate(rows):
            if not isinstance(row, list) or not 1 <= len(row) <= 7:
                raise ValueError(f"messages.json: each button row in {key} must contain 1–7 buttons")
            if any(map(_is_open_app, row)) and len(row) > OPEN_APP_ROW_LIMIT:
                raise ValueError(
                    f"messages.json: a row with open_app in {key} must contain at most "
                    f"{OPEN_APP_ROW_LIMIT} buttons"
                )
            for button_index, button in enumerate(row):
                location = f"{key}.buttons[{row_index}][{button_index}]"
                if _is_open_app(button):
                    if not _validate_open_app_button(button):
                        raise ValueError(
                            f"messages.json: invalid open_app button at {location}; payload may "
                            "contain only 1–512 Latin letters, digits, _ and -"
                        )
                    continue
                if (not isinstance(button, dict) or set(button) != {"text", "payload"}
                        or not _valid_button_text(button)
                        or not isinstance(button["payload"], str)):
                    raise ValueError(f"messages.json: invalid button at {location}")
                if button["payload"] not in messages:
                    raise ValueError(
                        f"messages.json: {location}.payload refers to unknown message {button['payload']!r}"
                    )
    return messages


def needs_mini_app(messages, key):
    return any(_is_open_app(button) for row in messages[key].get("buttons", []) for button in row)


def keyboard_button(button, mini_app):
    if _is_open_app(button):
        # mini_app names the bot whose app opens: {"contact_id": ..., "web_app": ...}.
        return {**button, **(mini_app or {})}
    return {"type": "callback", **button}


def message_body(messages, key, mini_app=None, user=None):
    message = messages[key]
    rows = message.get("buttons", [])
    body = {
        "text": personalize(message["text"], user),
        "attachments": [{
            "type": "inline_keyboard",
            "payload": {"buttons": [
                [keyboard_button(button, mini_app) for button in row] for row in rows
            ]},
        }] if rows else [],
    }
    if getattr(messages, "format", None):
        body["format"] = messages.format
    return body


class MaxAPIError(RuntimeError):
    def __init__(self, message, status=None):
        super().__init__(message)
        self.status = status

    @property
    def retryable(self):
        return self.status is None or self.status == 429 or self.status >= 500


class MaxClient:
    def __init__(self, token, messages_path=MESSAGES_PATH):
        if not token.strip():
            raise ValueError("Set MAX_BOT_TOKEN in the backend .env")
        self.token = token.strip()
        self.messages = load_messages(messages_path)
        self.mini_app = None
        self.ssl_context = ssl.create_default_context()
        self.ssl_context.load_verify_locations(
            cafile=Path(__file__).parent / "certs" / "russian_trusted_root_ca.pem",
        )

    def request(self, method, path, *, params=None, body=None, timeout=10):
        url = API_URL + path
        if params:
            url += "?" + urlencode(params)
        headers = {"Authorization": self.token, "Accept": "application/json"}
        data = None
        if body is not None:
            data = json.dumps(body).encode("utf-8")
            headers["Content-Type"] = "application/json"
        request = Request(url, data=data, headers=headers, method=method)
        try:
            with urlopen(request, timeout=timeout, context=self.ssl_context) as response:
                result = json.load(response)
        except HTTPError as error:
            status = error.code
            error.close()
            raise MaxAPIError(f"MAX API returned HTTP {status}", status) from None
        except (URLError, OSError, ValueError):
            # Never log request headers, the token, or raw responses with user data.
            raise MaxAPIError("MAX API connection or JSON response error") from None
        if not isinstance(result, dict):
            raise MaxAPIError("MAX API returned an invalid JSON object")
        return result

    def subscriptions(self):
        result = self.request("GET", "/subscriptions")
        if not isinstance(result.get("subscriptions"), list):
            raise MaxAPIError("MAX API returned invalid subscriptions")
        return result["subscriptions"]

    def updates(self, marker):
        params = {"timeout": 30, "types": "bot_started,message_created,message_callback"}
        if marker is not None:
            params["marker"] = marker
        result = self.request("GET", "/updates", params=params, timeout=40)
        if (not isinstance(result.get("updates"), list)
                or type(result.get("marker")) not in (int, type(None))):
            raise MaxAPIError("MAX API returned invalid updates")
        return result

    def load_mini_app(self):
        """Identify this bot via GET /me so open_app buttons launch its own mini app."""
        if self.mini_app is None:
            bot = self.request("GET", "/me")
            bot_id, username = bot.get("user_id"), bot.get("username")
            if type(bot_id) is not int or bot_id <= 0:
                raise MaxAPIError("MAX API returned invalid bot info")
            mini_app = {"contact_id": bot_id}
            if isinstance(username, str) and username.strip():
                mini_app["web_app"] = username.strip()
            self.mini_app = mini_app
        return self.mini_app

    def message(self, key, user=None):
        mini_app = self.load_mini_app() if needs_mini_app(self.messages, key) else None
        return message_body(self.messages, key, mini_app, user)

    def app_link(self, start_param):
        """Deep link that opens this bot's mini app with initDataUnsafe.start_param = start_param.

        Format from https://dev.max.ru/docs/webapps/introduction. None if the bot has no username.
        """
        if not isinstance(start_param, str) or not START_PARAM_RE.fullmatch(start_param):
            raise ValueError("Invalid mini app start parameter")
        username = self.load_mini_app().get("web_app")
        return f"https://max.ru/{username}?startapp={start_param}" if username else None

    def greet(self, user_id, user=None):
        self.request("POST", "/messages", params={"user_id": user_id}, body=self.message("start", user))

    def answer_callback(self, callback_id, key, user=None):
        body = {}
        if key is not None:
            if key not in self.messages:
                key = "start"
            body["message"] = self.message(key, user)
        self.request("POST", "/answers", params={"callback_id": callback_id}, body=body)


def human_user_id(sender):
    if not isinstance(sender, dict) or sender.get("is_bot") is not False:
        return None
    user_id = sender.get("user_id")
    return user_id if type(user_id) is int and user_id > 0 else None


def handle_update(client, update):
    """Show the start menu and handle its buttons in private conversations."""
    if not isinstance(update, dict):
        return
    if update.get("update_type") == "message_callback":
        callback = update.get("callback")
        if not isinstance(callback, dict) or human_user_id(callback.get("user")) is None:
            return
        callback_id = callback.get("callback_id")
        if not isinstance(callback_id, str) or not callback_id.strip():
            return
        message = update.get("message")
        if message is None:
            # MAX may deliver a click after the original message was deleted.
            client.answer_callback(callback_id, None)
            return
        if not isinstance(message, dict):
            return
        recipient = message.get("recipient")
        if not isinstance(recipient, dict) or recipient.get("chat_type") != "dialog":
            return
        key = callback.get("payload")
        client.answer_callback(callback_id, key if isinstance(key, str) else "start", user=callback["user"])
        return
    sender = start_sender(update)
    if sender is not None:
        client.greet(human_user_id(sender), user=sender)


def start_sender(update):
    """The human who pressed "Начать" or sent /start in a private chat, otherwise None."""
    if not isinstance(update, dict):
        return None
    if update.get("update_type") == "bot_started":
        sender = update.get("user")
    elif update.get("update_type") == "message_created":
        message = update.get("message")
        if not isinstance(message, dict):
            return None
        recipient = message.get("recipient")
        body = message.get("body")
        if (not isinstance(recipient, dict) or recipient.get("chat_type") != "dialog"
                or not isinstance(body, dict)):
            return None
        text = body.get("text")
        if not isinstance(text, str) or text.split(maxsplit=1)[:1] != ["/start"]:
            return None
        sender = message.get("sender")
    else:
        return None
    return sender if human_user_id(sender) is not None else None


def record_start(sender, update_type):
    """Activity log entry for the admin panel ("Логи" → "Без инвайта")."""
    from sqlconnector import log_event  # opens the DB pool only in the real bot process

    def text(value):
        return value if isinstance(value, str) else None

    details = {"via": "start_button" if update_type == "bot_started" else "start_command"}
    if text(sender.get("username")):
        details["username"] = sender["username"]
    log_event("bot_started", source="bot", max_user_id=sender["user_id"], first_name=text(sender.get("first_name")),
              last_name=text(sender.get("last_name")), details=details)


def with_retry(action, stop):
    delay = 1
    while not stop.is_set():
        try:
            return action()
        except MaxAPIError as error:
            if not error.retryable:
                raise
            logger.warning("%s; retry in %ss", error, delay)
            stop.wait(delay)
            delay = min(delay * 2, 30)
    return None


def run_bot(client, stop, on_start=None):
    subscriptions = with_retry(client.subscriptions, stop)
    if stop.is_set():
        return
    if subscriptions:
        raise ValueError("This bot already has a webhook. Long Polling requires a bot without webhooks.")
    # Resolve the bot ID once before handling events, so the first greeting waits for nothing.
    with_retry(client.load_mini_app, stop)
    if stop.is_set():
        return
    logger.info("MAX bot is listening for Start, /start and menu buttons")
    marker = None
    while not stop.is_set():
        batch = with_retry(lambda: client.updates(marker), stop)
        if stop.is_set():
            return
        for update in batch["updates"]:
            sender = start_sender(update) if on_start is not None else None
            try:
                with_retry(lambda: handle_update(client, update), stop)
            except MaxAPIError as error:
                if error.status == 401:
                    raise
                # A removed dialog or a user who blocked the bot must not block the queue.
                logger.warning("Cannot reply to one update: %s", error)
            finally:
                # Recorded once per update, after the reply: a slow or broken database must never
                # delay the greeting.
                if sender is not None:
                    try:
                        on_start(sender, update.get("update_type"))
                    except Exception as error:  # noqa: BLE001 — the log must never stop the bot
                        logger.warning("Cannot record a bot start: %s", error)
            if stop.is_set():
                return
        # Acknowledge only after the whole batch; retrying a send keeps its position.
        marker = batch["marker"]


def main():
    load_dotenv()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    stop = Event()
    for signum in (signal.SIGINT, signal.SIGTERM):
        signal.signal(signum, lambda *_: stop.set())
    try:
        run_bot(MaxClient(os.getenv("MAX_BOT_TOKEN", "")), stop, on_start=record_start)
    except (ValueError, MaxAPIError) as error:
        logger.error("%s", error)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
