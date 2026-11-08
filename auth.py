import hashlib
import hmac
import json
import re
import secrets
import time
from urllib.parse import parse_qsl

from pydantic import ValidationError

from models import MaxUser


class InvalidInitData(ValueError):
    pass


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate parameter")
        result[key] = value
    return result


def validate_init_data(
    init_data: str, bot_token: str, max_age: int = 3600, *, now: int | None = None,
) -> MaxUser:
    """Validate the original window.WebApp.initData string before trusting its user."""
    if not bot_token or max_age <= 0:
        raise ValueError("MAX authentication is not configured")
    try:
        if not init_data or len(init_data) > 16384 or re.search(r"%(?![0-9a-fA-F]{2})", init_data):
            raise ValueError("Malformed init_data")
        params = _unique_object(parse_qsl(
            init_data, keep_blank_values=True, strict_parsing=True,
            encoding="utf-8", errors="strict", max_num_fields=64,
        ))
        received_hash = params.pop("hash", "")
        if not re.fullmatch(r"[0-9a-fA-F]{64}", received_hash):
            raise ValueError("Invalid hash")

        launch_params = "\n".join(f"{key}={value}" for key, value in sorted(params.items()))
        secret_key = hmac.digest(b"WebAppData", bot_token.encode("utf-8"), "sha256")
        expected_hash = hmac.digest(secret_key, launch_params.encode("utf-8"), "sha256")
        if not hmac.compare_digest(expected_hash, bytes.fromhex(received_hash)):
            raise ValueError("Invalid signature")

        auth_date = params.get("auth_date", "")
        if not re.fullmatch(r"[0-9]{1,12}", auth_date):
            raise ValueError("Invalid auth_date")
        issued_at = int(auth_date)
        current_time = int(time.time()) if now is None else now
        if issued_at > current_time + 30 or current_time - issued_at > max_age:
            raise ValueError("Expired or future init_data")

        user = json.loads(params["user"], object_pairs_hook=_unique_object)
        return MaxUser.model_validate(user)
    except (ValueError, KeyError, TypeError, UnicodeError, ValidationError) as error:
        raise InvalidInitData("Invalid init_data") from error


def parse_user_ids(value: str) -> frozenset[int]:
    """Parse MAX user IDs (window.WebApp.initData user.id) separated by commas or spaces."""
    ids = set()
    for item in re.split(r"[\s,]+", value.strip()):
        if not item:
            continue
        if not re.fullmatch(r"[1-9][0-9]{0,18}", item) or int(item) > 9223372036854775807:
            raise ValueError(f"Invalid MAX user ID in BETA_USER_IDS: {item!r}")
        ids.add(int(item))
    return frozenset(ids)


def new_session_token() -> str:
    return secrets.token_urlsafe(32)


def session_token_hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()
