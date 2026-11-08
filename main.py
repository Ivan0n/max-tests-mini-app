import argparse
import json
import os
import re
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from functools import wraps

import time
import uuid

from flask import Flask, Response, g, jsonify, request
from flask_cors import CORS
import psycopg
from pydantic import ValidationError

from sqlconnector import (
    create_auth_session, get_account_by_session, get_beta_contact_url, get_beta_status, get_test,
    get_test_by_path, log_event,
    get_authored_tests, get_last_result, get_test_result, get_user_results, new_test,
    revoke_auth_session, save_test_result, upsert_account,test_path_exists,get_all_test_results_by_url,
    get_student_result, delete_test, update_test, count_test_results,
    save_image, get_image_meta, count_recent_images, images_owned_by,
)
import storage
from models import CheckTest, CreateTest, DEFAULT_QUESTION_TYPE, MaxAuthRequest
from controller import (
    LANDING_CSP, _reject_constant, dashboard_statistics, generate_url_path, is_correct_answer,
    test_landing_page, validate_answer,
)
from auth import InvalidInitData, new_session_token, parse_user_ids, session_token_hash, validate_init_data
from max_bot import START_PARAM_RE, MaxAPIError, MaxClient, test_share_text


MAX_PATH_ATTEMPTS = 5

def dev_mode_from_env() -> bool:
    """DEV_MODE=1 is the Docker equivalent of `py main.py -dev` (compose.dev.yaml for frontend
    developers). The production compose.yaml never passes this variable."""
    return os.getenv("DEV_MODE", "").strip().lower() in ("1", "true", "yes", "on")


DEV_MODE_WARNING = ("DEV mode: MAX init_data and Bearer authentication checks are disabled; "
                    "all requests use the shared development account")

app = Flask(__name__)
app.config.update(
    DEV_MODE=dev_mode_from_env(),
    MAX_BOT_TOKEN=os.getenv("MAX_BOT_TOKEN", ""),
    MAX_INIT_DATA_MAX_AGE=int(os.getenv("MAX_INIT_DATA_MAX_AGE", "3600")),
    AUTH_SESSION_TTL=int(os.getenv("AUTH_SESSION_TTL", "86400")),
    # Closed beta: when non-empty, only these MAX user IDs can sign in and use the API.
    BETA_USER_IDS=parse_user_ids(os.getenv("BETA_USER_IDS", "")),
    # Public origin of the site, e.g. https://example.com: share links become <PUBLIC_URL>/test/<code>.
    PUBLIC_URL=os.getenv("PUBLIC_URL", "").strip().rstrip("/"),
)
if app.config["DEV_MODE"]:
    app.logger.warning(DEV_MODE_WARNING + " (DEV_MODE environment variable)")
if app.config["MAX_INIT_DATA_MAX_AGE"] <= 0 or app.config["AUTH_SESSION_TTL"] <= 0:
    raise ValueError("MAX_INIT_DATA_MAX_AGE and AUTH_SESSION_TTL must be positive seconds")
if app.config["PUBLIC_URL"] and not re.fullmatch(r"https?://[A-Za-z0-9.-]+(:[0-9]{1,5})?", app.config["PUBLIC_URL"]):
    raise ValueError("PUBLIC_URL must be an origin like https://example.com, without a path")

CORS(
    app,
    resources=[r"/api/.*", r"/t/.*"],
    origins=[
        origin.strip()
        for origin in os.getenv(
            "CORS_ORIGINS",
            "http://localhost:3000,http://localhost:5173,"
            "http://127.0.0.1:3000,http://127.0.0.1:5173",
        ).split(",")
        if origin.strip()
    ],
    methods=["GET", "POST", "PUT", "DELETE", "OPTIONS"],
    allow_headers=["Content-Type", "Authorization"],
    supports_credentials=False,
    max_age=600,
    vary_header=True,
)


_max_client = None


def max_client() -> MaxClient:
    """Bot API client for sending messages; created on first use and reused per worker."""
    global _max_client
    if _max_client is None:
        _max_client = MaxClient(app.config["MAX_BOT_TOKEN"])
    return _max_client


def get_development_account():
    return upsert_account({
        "id": -1,
        "first_name": "Разработчик",
        "last_name": None,
        "language_code": "ru",
        "photo_url": None,
    })


def has_beta_access(max_user_id: int, db_enabled: bool = False, db_invited: bool = False) -> bool:
    """Closed beta is on when BETA_USER_IDS is set or it is switched on in the admin panel
    (app_settings.beta_enabled). Then only IDs from BETA_USER_IDS or beta_invites get in."""
    allowed = app.config["BETA_USER_IDS"]
    if not allowed and not db_enabled:
        return True
    return max_user_id in allowed or db_invited


def _log_user_event(event: str, details: dict) -> None:
    user = g.current_user
    if app.config["DEV_MODE"]:
        return  # the shared -dev account is not a real user
    log_event(event, max_user_id=user.get("max_user_id"), account_id=user.get("id"),
              first_name=user.get("first_name"), last_name=user.get("last_name"), details=details)


def beta_access_denied(max_user_id: int):
    """403 for people without an invite. The mini app shows their (signature-verified) MAX ID
    so they can send it to the admin, plus the admin's contact link from the admin panel."""
    try:
        contact_url = get_beta_contact_url()
    except psycopg.Error:
        contact_url = None
    return jsonify(error="Beta access denied", max_user_id=max_user_id, contact_url=contact_url), 403, {
        "Cache-Control": "no-store"}


def require_auth(handler):
    @wraps(handler)
    def authenticated(*args, **kwargs):
        if app.config["DEV_MODE"]:
            g.current_user = get_development_account()
            g.session_token_hash = None
            return handler(*args, **kwargs)
        parts = request.headers.get("Authorization", "").split()
        if (len(parts) != 2 or parts[0].lower() != "bearer"
                or not re.fullmatch(r"[A-Za-z0-9_-]{43}", parts[1])):
            return jsonify(error="Unauthorized"), 401, {"WWW-Authenticate": "Bearer", "Cache-Control": "no-store"}
        token_hash = session_token_hash(parts[1])
        account = get_account_by_session(token_hash)
        if account is None:
            return jsonify(error="Unauthorized"), 401, {"WWW-Authenticate": "Bearer", "Cache-Control": "no-store"}
        # Re-checked on every request, so revoking an invite locks out existing sessions at once.
        beta_enabled = account.pop("beta_enabled", False)
        beta_invited = account.pop("beta_invited", False)
        if not has_beta_access(account.get("max_user_id"), beta_enabled, beta_invited):
            return beta_access_denied(account.get("max_user_id"))
        g.current_user = account
        g.session_token_hash = token_hash
        return handler(*args, **kwargs)
    return authenticated


@app.after_request
def prevent_auth_caching(response):
    if (app.config["DEV_MODE"] or request.path.startswith("/api/auth/")
            or request.headers.get("Authorization")):
        response.headers["Cache-Control"] = "no-store"
    return response


@app.route("/api/auth/max", methods=["POST"])
def authenticate_max():
    if app.config["DEV_MODE"]:
        account = get_development_account()
    else:
        try:
            payload = json.loads(request.get_data(cache=False), parse_constant=_reject_constant)
        except (ValueError, UnicodeDecodeError):
            return jsonify(error="Invalid JSON"), 400
        try:
            body = MaxAuthRequest.model_validate(payload)
        except ValidationError as error:
            return jsonify(
                error="Validation failed",
                details=error.errors(include_url=False, include_input=False, include_context=False),
            ), 400
        if not app.config["MAX_BOT_TOKEN"]:
            return jsonify(error="MAX authentication is not configured"), 503
        try:
            user = validate_init_data(
                body.init_data, app.config["MAX_BOT_TOKEN"], app.config["MAX_INIT_DATA_MAX_AGE"],
            )
        except InvalidInitData:
            return jsonify(error="Invalid init_data"), 401
        if not has_beta_access(user.id, *get_beta_status(user.id)):
            # "Без инвайта" in the admin panel: who tried to open the mini app during the closed beta.
            log_event("beta_denied", max_user_id=user.id, first_name=user.first_name, last_name=user.last_name,
                      details={"photo_url": user.photo_url} if user.photo_url else None)
            return beta_access_denied(user.id)

        account = upsert_account(user.model_dump())
        log_event("login", max_user_id=account["max_user_id"], account_id=account["id"],
                  first_name=account["first_name"], last_name=account["last_name"])
    token = new_session_token()
    if app.config["DEV_MODE"]:
        expires_at = datetime.now(timezone.utc) + timedelta(seconds=app.config["AUTH_SESSION_TTL"])
    else:
        expires_at = create_auth_session(
            account["id"], session_token_hash(token), app.config["AUTH_SESSION_TTL"],
        )
    return jsonify(
        access_token=token, token_type="Bearer", expires_in=app.config["AUTH_SESSION_TTL"],
        expires_at=expires_at.isoformat(), user=account,
    ), 200


@app.route("/api/auth/me", methods=["GET"])
@require_auth
def current_user():
    return jsonify(user=g.current_user), 200


@app.route("/api/auth/logout", methods=["POST"])
@require_auth
def logout():
    if g.session_token_hash is not None:
        revoke_auth_session(g.session_token_hash)
    return "", 204

@app.route("/api/status", methods=["GET"])
def status():
    return jsonify({
        "status": "ok",
        "message": "Flask server is running"
    })

def _read_test_body():
    """(CreateTest, data dict, None) for a valid create/update body, or (None, None, error response)."""
    body = request.get_data(cache=False)

    #Проверка на дурака, синтаксис JSON
    try:
        payload = json.loads(body, parse_constant=_reject_constant)
    except (ValueError, UnicodeDecodeError):
        return None, None, (jsonify(error="Invalid JSON"), 400)
    #структура и типы
    try:
        body = CreateTest.model_validate(payload)
    except ValidationError as e:
        return None, None, (jsonify(
            error="Validation failed",
            details=e.errors(include_url=False, include_input=False, include_context=False),
        ), 400)

    data = body.data.model_dump(exclude_unset=True)
    for question in data["questions"]:
        question.setdefault("type", DEFAULT_QUESTION_TYPE)
    error = _store_images(data, g.current_user["id"])
    if error is not None:
        return None, None, error
    return body, data, None


def _store_images(data, owner_id):
    """Keep task images in a stable form: our uploads as /api/images/<id> (only the author's own),
    other pictures as plain http(s) links. Returns an error response or None."""
    uploaded = []
    for question in data["questions"]:
        image = question.get("image")
        if image is None:
            question.pop("image", None)
            continue
        src = image.get("src") if isinstance(image, dict) else None
        alt = image.get("alt") if isinstance(image, dict) else None
        alt = alt.strip()[:500] if isinstance(alt, str) and alt.strip() else "Изображение к заданию"
        image_id = storage.internal_image_id(src)
        if image_id is not None:
            uploaded.append(image_id)
            question["image"] = {"src": f"/api/images/{image_id}", "alt": alt}
        elif isinstance(src, str) and len(src) <= 2048 and re.match(r"^(?:https?://|/(?!/))[^\s]*$", src):
            question["image"] = {"src": src, "alt": alt}
        else:
            return jsonify(error="Unsupported image source", id=question.get("id")), 400
    unknown = sorted(set(uploaded) - images_owned_by(sorted(set(uploaded)), owner_id))
    if unknown:
        return jsonify(error="Unknown image", ids=unknown), 400
    return None


def _present_images(data):
    """Swap stored /api/images/<id> for signed absolute links the browser can load in <img>."""
    if not storage.configured():
        return data
    base = app.config["PUBLIC_URL"] or request.host_url.rstrip("/")
    for question in data.get("questions") or []:
        image = question.get("image")
        image_id = storage.internal_image_id(image.get("src")) if isinstance(image, dict) else None
        if image_id is not None:
            question["image"] = {**image, "src": base + storage.signed_path(image_id)}
    return data


def _grade(questions):
    """Grader for stored answers (JSON object with string keys) against the given questions."""
    def grade(answers):
        good = 0
        for question in questions:
            answer = answers.get(str(question["id"]))
            good += answer is not None and validate_answer(question, answer) is None and is_correct_answer(question, answer)
        return good, len(questions) - good
    return grade


def _structure_change(old_questions, new_questions):
    """Why the edit is not allowed, or None. Saved attempts must stay valid: the same tasks in the
    same order, each with its type and number of options."""
    if len(old_questions) != len(new_questions):
        return "Questions cannot be added or removed"
    for old, new in zip(old_questions, new_questions):
        if old["id"] != new["id"]:
            return "Questions cannot be reordered"
        if old.get("type", DEFAULT_QUESTION_TYPE) != new["type"]:
            return "Question type cannot be changed"
        if len(old.get("options") or []) != len(new.get("options") or []):
            return "Number of options cannot be changed"
    return None


@app.route('/api/test/create-test', methods=["POST"])
@require_auth
def create_test():
    body, data, error = _read_test_body()
    if error is not None:
        return error
    #logica men

    for _ in range(MAX_PATH_ATTEMPTS):
        gen_path = generate_url_path()
        test_id = new_test(gen_path, body.title, data, g.current_user["id"])
        if test_id is not None:
            _log_user_event("test_created", {"path": gen_path, "title": body.title,
                                             "questions": len(data["questions"])})
            return jsonify(status="ok", id=test_id, path=gen_path), 201
        

    return jsonify(error = "Сервер не смог создать эксклюзивный путь"), 500

def test_view(test, url_path, result=None, viewer_id=None):
    view = deepcopy(test)
    author_id = view.pop("author_id", None)
    view["url_path"] = url_path
    # Lets the frontend show the author's "share" button without exposing account ids.
    view["is_author"] = author_id is not None and author_id == viewer_id
    view["solved"] = result is not None
    view["result"] = None
    grades = []
    for question in view["data"]["questions"]:
        question.setdefault("type", DEFAULT_QUESTION_TYPE)
        question.pop("answer", None)
        if result is None:
            question.pop("correct_answer", None)
            question.pop("explanation", None)
            question.pop("is_correct", None)
            continue
        answer = result["answers"].get(question["id"])
        correct = answer is not None and is_correct_answer(question, answer)
        question.update(answer=answer, is_correct=correct, explanation=question.get("explanation") or "")
        grades.append({
            "id": question["id"], "answer": answer, "correct": correct,
            "correct_answer": question["correct_answer"], "explanation": question["explanation"],
        })
    _present_images(view["data"])
    if result is not None:
        view["result"] = {
            "id": result["id"], "score": result["good"], "total": result["good"] + result["bad"],
            "answers": result["answers"], "results": grades, "finished_at": result["created_at"],
            "duration_seconds": result.get("duration_seconds"),
        }
        if result.get("student") is not None:
            view["result"]["student"] = result["student"]
    return view


@app.route('/api/test/<url_path>', methods=["GET"])
@require_auth
def get_current_test(url_path):
    test = get_test(url_path)
    if test is None:
        return jsonify(error="Test not found"), 404
    result_id = request.args.get("result", type=int)
    if "result" in request.args:
        if result_id is None or not 1 <= result_id <= 9223372036854775807:
            return jsonify(error="Invalid result id"), 400
        result = get_test_result(g.current_user["id"], url_path, result_id)
        if result is None and test.get("author_id") == g.current_user["id"]:
            # The author opens a student's attempt from the test's results list.
            result = get_student_result(url_path, result_id)
        if result is None:
            return jsonify(error="Result not found"), 404
    else:
        result = get_last_result(g.current_user["id"], url_path)
    return jsonify(test_view(test, url_path, result, g.current_user["id"])), 200


IMAGE_UPLOADS_PER_HOUR = 60


@app.route('/api/images', methods=["POST"])
@require_auth
def upload_image():
    """Multipart field "image": PNG, JPEG or WebP up to 5 MB. Stored as WebP without metadata."""
    if not storage.configured():
        return jsonify(error="Image storage is not configured"), 503
    if (request.content_length or 0) > storage.MAX_UPLOAD_BYTES + 64 * 1024:
        return jsonify(error="Image is too large", max_bytes=storage.MAX_UPLOAD_BYTES), 413
    if count_recent_images(g.current_user["id"], 3600) >= IMAGE_UPLOADS_PER_HOUR:
        return jsonify(error="Too many uploads, try again later"), 429
    upload = request.files.get("image")
    if upload is None:
        return jsonify(error="Send the file in the multipart field \"image\""), 400
    raw = upload.stream.read(storage.MAX_UPLOAD_BYTES + 1)
    if len(raw) > storage.MAX_UPLOAD_BYTES:
        return jsonify(error="Image is too large", max_bytes=storage.MAX_UPLOAD_BYTES), 413
    try:
        data, width, height = storage.prepare_image(raw)
    except storage.InvalidImage as error:
        return jsonify(error=str(error)), 400
    image_id = uuid.uuid4().hex
    try:
        storage.put_image(image_id, data)
    except Exception as error:  # noqa: BLE001 — any storage failure is a 502 for the client
        app.logger.warning("Cannot store an image: %s", error)
        return jsonify(error="Image storage is unavailable"), 502
    save_image(image_id, g.current_user["id"], storage.object_key(image_id), storage.CONTENT_TYPE,
               len(data), width, height)
    _log_user_event("image_uploaded", {"id": image_id, "bytes": len(data), "width": width, "height": height})
    base = app.config["PUBLIC_URL"] or request.host_url.rstrip("/")
    return jsonify(id=image_id, src=base + storage.signed_path(image_id), width=width, height=height), 201


@app.route('/api/images/<image_id>', methods=["GET"])
def get_image(image_id):
    """No Bearer token here (an <img> cannot send one): the signed link is the permission."""
    if not storage.configured():
        return jsonify(error="Image storage is not configured"), 503
    expires, signature = request.args.get("exp"), request.args.get("sig")
    if not storage.verify(image_id, expires, signature):
        return jsonify(error="Invalid or expired image link"), 403
    meta = get_image_meta(image_id)
    if meta is None:
        return jsonify(error="Image not found"), 404
    try:
        data = storage.get_image(meta["object_key"])
    except Exception as error:  # noqa: BLE001
        app.logger.warning("Cannot read an image: %s", error)
        return jsonify(error="Image storage is unavailable"), 502
    max_age = max(0, min(int(expires) - int(time.time()), 86400))
    return Response(data, mimetype=meta["content_type"], headers={
        "Cache-Control": f"private, max-age={max_age}, immutable",
        "Content-Disposition": "inline",
        "X-Content-Type-Options": "nosniff",
        "Content-Security-Policy": "default-src 'none'; sandbox",
        "Cross-Origin-Resource-Policy": "cross-origin",
    })


@app.route('/api/test/<url_path>/edit', methods=["GET"])
@require_auth
def get_test_for_edit(url_path):
    """The author's full test, with correct answers, to load into the editor."""
    test = get_test(url_path)
    if test is None:
        return jsonify(error="Test not found"), 404
    if test.get("author_id") != g.current_user["id"]:
        return jsonify(error="Only the test author can edit it"), 403
    data = deepcopy(test["data"])
    for question in data["questions"]:
        question.setdefault("type", DEFAULT_QUESTION_TYPE)
    _present_images(data)
    return jsonify(path=url_path, title=test["title"], data=data,
                   results_count=count_test_results(test["id"])), 200


@app.route('/api/test/<url_path>', methods=["PUT"])
@require_auth
def update_test_api(url_path):
    """Edit the author's test in place: same link, same tasks; saved attempts are re-graded."""
    test = get_test(url_path)
    if test is None:
        return jsonify(error="Test not found"), 404
    if test.get("author_id") != g.current_user["id"]:
        return jsonify(error="Only the test author can edit it"), 403
    body, data, error = _read_test_body()
    if error is not None:
        return error
    reason = _structure_change(test["data"]["questions"], data["questions"])
    if reason is not None:
        return jsonify(error=reason, code="structure_changed"), 409
    regraded = update_test(url_path, g.current_user["id"], body.title, data, _grade(data["questions"]))
    if regraded is None:
        return jsonify(error="Test not found"), 404
    _log_user_event("test_updated", {"path": url_path, "title": body.title, "regraded": regraded})
    return jsonify(status="ok", id=test["id"], path=url_path, regraded=regraded), 200


@app.route('/api/test/<url_path>', methods=["DELETE"])
@require_auth
def delete_test_api(url_path):
    """Only the author deletes a test; its results go with it and the link stops working."""
    test = get_test(url_path)
    if test is None:
        return jsonify(error="Test not found"), 404
    if test.get("author_id") != g.current_user["id"]:
        return jsonify(error="Only the test author can delete it"), 403
    removed = delete_test(url_path, g.current_user["id"])
    if removed is None:
        return jsonify(error="Test not found"), 404
    _log_user_event("test_deleted", {"path": url_path, "title": test.get("title"), "results": removed})
    return "", 204


@app.route('/api/test/<url_path>/all-results', methods=["GET"])
@require_auth
def get_all_test_results_api(url_path: str):
    test = get_test(url_path)
    if test is None:
        return jsonify(error="Test not found"), 404

    if test["author_id"] != g.current_user["id"]:
        return jsonify(error="Forbidden: only test author can view all results"), 403

    results = get_all_test_results_by_url(url_path)

    _log_user_event(
        "test_results_prosmotr",
        {"path": url_path, "title": test.get("title"), "results_count": len(results)}
    )

    return jsonify(
        test=url_path,
        title=test.get("title"),
        total_results=len(results),
        results=results
    ), 200


@app.route('/t/<url_path>', methods=["GET"])
def get_test_api(url_path):
    test = get_test(url_path)

    if test is None:
        return jsonify({"error": "Test not found"}), 404

    test = deepcopy(test)
    test.pop("author_id", None)
    for question in test["data"]["questions"]:
        question.setdefault("type", DEFAULT_QUESTION_TYPE)
        question.pop("answer", None)
        question.pop("correct_answer", None)
        question.pop("explanation", None)
        question.pop("is_correct", None)
    _present_images(test["data"])

    return jsonify(test), 200

@app.route('/api/dashboard', methods=["GET"])
@require_auth
def get_dashboard():
    tests = get_authored_tests(g.current_user["id"])
    results = get_user_results(user_id=g.current_user["id"])
    return jsonify(statistics=dashboard_statistics(tests, results), tests=tests, results=results), 200


@app.route('/api/results', methods=["GET"])
@require_auth
def get_results():
    test_path = request.args.get("test")  # необязательный параметр
    results = get_user_results(user_id=g.current_user["id"], test_path=test_path)
    return jsonify(results=results), 200
    
@app.route('/api/test/<url_path>/results', methods=["GET"])
@require_auth
def get_test_results(url_path: str):
    test = get_test_by_path(url_path)
    if test is None:
        return jsonify(error="Test not found"), 404

    results = get_user_results(user_id=g.current_user["id"], test_path=url_path)
    return jsonify(test=url_path, results=results), 200


@app.route('/api/test/<url_path>/share', methods=["GET"])
@require_auth
def share_test(url_path: str):
    """Text and link for WebApp.shareMaxContent({text, link}); sends nothing itself.

    The link is <PUBLIC_URL>/test/<code>: a page on our domain that gives the chat a test
    preview and opens the mini app. Without PUBLIC_URL it is the raw max.ru deep link.
    """
    test = get_test_by_path(url_path)
    if test is None:
        return jsonify(error="Test not found"), 404
    if not START_PARAM_RE.fullmatch(url_path):
        return jsonify(error="Test path cannot be passed to the mini app"), 400
    if app.config["PUBLIC_URL"]:
        link = f"{app.config['PUBLIC_URL']}/test/{url_path}"
    else:
        if not app.config["MAX_BOT_TOKEN"]:
            return jsonify(error="Sharing through MAX is not available"), 503
        try:
            link = max_client().app_link(url_path)
        except MaxAPIError as error:
            app.logger.warning("Cannot resolve the bot for a share link: %s", error)
            return jsonify(error="MAX API is unavailable"), 502
        if link is None:
            return jsonify(error="Sharing through MAX is not available"), 503
    data = test["data"]
    text = test_share_text(test["title"], len(data.get("questions") or []),
                           subject=data.get("subject"), author=data.get("author_name"))
    return jsonify(text=text, link=link), 200


@app.route('/test/<url_path>', methods=["GET"])
def test_landing(url_path: str):
    """Public share page: chat preview tags, then a hop to https://max.ru/<bot>?startapp=<code>."""
    test = get_test_by_path(url_path) if START_PARAM_RE.fullmatch(url_path) else None
    app_link = None
    if test is not None and app.config["MAX_BOT_TOKEN"]:
        try:
            app_link = max_client().app_link(url_path)
        except MaxAPIError as error:
            app.logger.warning("Cannot resolve the bot for a landing page: %s", error)
    page_url = f"{app.config['PUBLIC_URL'] or request.host_url.rstrip('/')}/test/{url_path}"
    html = test_landing_page(test, page_url, app_link)
    return html, 200 if test is not None else 404, {
        "Content-Type": "text/html; charset=utf-8",
        "Content-Security-Policy": LANDING_CSP,
        "Cache-Control": "public, max-age=300",
        "X-Robots-Tag": "noindex",
        "Referrer-Policy": "no-referrer",
    }


@app.route('/api/test/<url_path>/chek-test', methods = ["POST"])
@require_auth
def cheker_test(url_path: str):
    body = request.get_data(cache= False)

    try: 
        payload = json.loads(body, parse_constant=_reject_constant)
    except (ValueError, UnicodeDecodeError):
        return jsonify(error="Invalid JSON"), 400

    try:
        body = CheckTest.model_validate(payload)
    except ValidationError as e:
        return jsonify(
            error="Validation failed",
            details=e.errors(include_url=False, include_input=False, include_context=False),
        ), 400
    
    test = get_test_by_path(url_path)

    if test is None:
        return jsonify(error="Test not found"), 404


    questions = {q["id"]: q for q in test["data"]["questions"]}
    answers = {a.id: a.answer for a in body.answers}

    unknown = set(answers) - set(questions)
    if unknown:
        return jsonify(error="Unknown question ids", ids=sorted(unknown)), 400

    missing = set(questions) - set(answers)
    if missing:
        return jsonify(error="Not all questions answered", ids=sorted(missing)), 400
    
    for qid, given in answers.items():
        error = validate_answer(questions[qid], given)
        if error is not None:
            return jsonify(error), 400

    score = 0
    total = len(questions)

    for qid, q in questions.items():
        is_correct = is_correct_answer(q, answers[qid])
        score += is_correct

    result_id = save_test_result(
        test_id=test["id"],
        user_id=g.current_user["id"],
        answers=answers,
        good=score,
        bad=total - score,
        duration_seconds=body.elapsed_seconds,
    )
    _log_user_event("test_completed", {"path": url_path, "title": test.get("title"), "good": score, "bad": total - score})
 
    solved_test = test_view(test, url_path, {
        "id": result_id, "answers": answers, "good": score, "bad": total - score,
        "created_at": datetime.now(timezone.utc).isoformat(), "duration_seconds": body.elapsed_seconds,
    }, g.current_user["id"])
    return jsonify(**solved_test["result"], test=solved_test), 201


"""
@app.route('/api/test/chek-result/<test_id>', methods=["POST"])
def chek_results(test_id):
    file_name = os.path.join(JSON_DIR, f"{test_id}.json")

    with open(file_name, "r", encoding="utf-8") as f:
        test_data = json.load(f)

    data = request.get_json()

    answers = data["answers"]
    keys = test_data["keys"]

    good = 0
    bad = 0

    for i in range(len(keys)):
        for ex in keys[i]:
            if answers[i][ex] == keys[i][ex]:
                good += 1
            else:
                bad += 1

    return f"Good: {good}, Bad: {bad}"
"""

def main(argv=None):
    parser = argparse.ArgumentParser(description="Запуск API тестов MAX")
    parser.add_argument(
        "-dev", "--dev", action="store_true",
        help="отключить авторизацию MAX и Bearer для локальной разработки",
    )
    args = parser.parse_args(argv)
    app.config["DEV_MODE"] = args.dev or dev_mode_from_env()
    if app.config["DEV_MODE"]:
        app.logger.warning(DEV_MODE_WARNING)
    app.run(host="0.0.0.0", port=8000, debug=True)


if __name__ == "__main__":
    main()
