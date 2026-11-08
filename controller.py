import base64
import hashlib
import secrets
import string
from html import escape

from models import DEFAULT_QUESTION_TYPE

def generate_url_path(size=12):
    chars = string.ascii_lowercase + string.digits
    return "".join(secrets.choice(chars) for _ in range(size))


def plural(count: int, forms: tuple[str, str, str]) -> str:
    """Russian plural: (1 задание, 2 задания, 5 заданий)."""
    tail = count % 100
    form = forms[2] if 11 <= tail <= 14 else forms[0] if count % 10 == 1 else (
        forms[1] if 2 <= count % 10 <= 4 else forms[2])
    return f"{count} {form}"


# The only script on the landing page is static, so the CSP allows exactly it by hash.
_LANDING_SCRIPT = "location.replace(document.getElementById('open').href);"
_LANDING_SCRIPT_HASH = base64.b64encode(hashlib.sha256(_LANDING_SCRIPT.encode()).digest()).decode()
LANDING_CSP = ("default-src 'none'; style-src 'unsafe-inline'; "
               f"script-src 'sha256-{_LANDING_SCRIPT_HASH}'; base-uri 'none'; form-action 'none'")

_LANDING_STYLE = (
    ":root{color-scheme:light dark;font-family:system-ui,-apple-system,'Segoe UI',Roboto,sans-serif}"
    "body{margin:0;min-height:100vh;display:grid;place-items:center;background:Canvas;color:CanvasText}"
    "main{max-width:420px;padding:32px 24px;text-align:center}"
    "h1{margin:0 0 8px;font-size:22px}p{margin:0 0 24px;opacity:.7;line-height:1.4}"
    "a{display:inline-block;padding:14px 28px;border-radius:14px;background:#07f;color:#fff;"
    "text-decoration:none;font-weight:600}"
)


def test_landing_page(test: dict | None, page_url: str, app_link: str | None) -> str:
    """Public page behind a share link: Open Graph tags for the chat preview, then a
    redirect to https://max.ru/<bot>?startapp=<code>, which opens the mini app on the test.

    Served with 200 (not an HTTP redirect) so link previews use these tags, not max.ru's.
    """
    if test is None:
        heading, description, title = "Тест не найден", "Проверьте ссылку или попросите прислать её ещё раз.", "Тест не найден"
    else:
        data = test.get("data") or {}
        details = [data.get("subject"), plural(len(data.get("questions") or []), ("задание", "задания", "заданий"))]
        if data.get("author_name"):
            details.append(f"Составитель: {data['author_name']}")
        heading = " ".join(str(test["title"]).split())
        description = " · ".join(" ".join(str(item).split()) for item in details if item)
        title = f"{heading} — тест в MAX"
    meta = ""
    if test is not None:
        meta = "".join(
            f'<meta property="{name}" content="{escape(value)}">'
            for name, value in (("og:type", "website"), ("og:site_name", "Тесты в MAX"),
                                ("og:title", heading), ("og:description", description), ("og:url", page_url))
        )
    if test is not None and app_link:
        action = (f'<a id="open" href="{escape(app_link)}">Открыть тест в MAX</a>'
                  f"<script>{_LANDING_SCRIPT}</script>")
    elif test is not None:
        action = "<p>Не удалось открыть MAX. Попробуйте позже.</p>"
    else:
        action = ""
    return ("<!doctype html><html lang=\"ru\"><head><meta charset=\"utf-8\">"
            "<meta name=\"viewport\" content=\"width=device-width, initial-scale=1\">"
            f"<title>{escape(title)}</title><meta name=\"description\" content=\"{escape(description)}\">"
            f"<meta name=\"robots\" content=\"noindex\">{meta}<style>{_LANDING_STYLE}</style></head>"
            f"<body><main><h1>{escape(heading)}</h1><p>{escape(description)}</p>{action}</main></body></html>")


def dashboard_statistics(tests, results):
    correct = sum(result.get("good") or 0 for result in results)
    incorrect = sum(result.get("bad") or 0 for result in results)
    answered = correct + incorrect
    return {
        "created_tests": len(tests),
        "test_completions": sum(test["completions"] for test in tests),
        "completed_tests": len({result["url_path"] for result in results}),
        "attempts": len(results),
        "correct_answers": correct,
        "incorrect_answers": incorrect,
        "answered_questions": answered,
        "accuracy": round(100 * correct / answered) if answered else None,
    }

def _reject_constant(name: str):
    raise ValueError(f"Недопустимая константа JSON: {name}") # Python принимает NaN и Infinity, а JSON и PostgreSQL их не принимают


def validate_answer(question: dict, answer: int | list[int] | str) -> dict | None:
    question_type = question.get("type", DEFAULT_QUESTION_TYPE)
    expected_types = {
        "radio button": (int, "integer"),
        "chek box": (list, "array of integers"),
        "text": (str, "string"),
    }
    if question_type not in expected_types:
        return {"error": "Unsupported question type", "id": question["id"]}

    expected_type, description = expected_types[question_type]
    if type(answer) is not expected_type:
        return {"error": "Invalid answer type", "id": question["id"], "expected": description}

    if question_type != "text":
        indices = answer if isinstance(answer, list) else [answer]
        if any(index >= len(question["options"]) for index in indices):
            return {"error": "Answer index out of range", "id": question["id"]}
    return None


def is_correct_answer(question: dict, answer: int | list[int] | str) -> bool:
    question_type = question.get("type", DEFAULT_QUESTION_TYPE)
    correct_answer = question["correct_answer"]
    if question_type == "chek box":
        return set(answer) == set(correct_answer)
    if question_type == "text":
        return answer.strip().casefold() == correct_answer.strip().casefold()
    return answer == correct_answer

