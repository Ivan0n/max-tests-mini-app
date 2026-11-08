import atexit
import logging

from psycopg_pool import ConnectionPool
import json 
from datetime import datetime, timedelta, timezone

from database import connection_kwargs


pool = ConnectionPool(
    conninfo="",
    kwargs=connection_kwargs(),
    min_size=1,
    max_size=5,
    open=True
)
atexit.register(pool.close)


def _account_from_row(row) -> dict:
    return dict(zip(
        ("id", "max_user_id", "first_name", "last_name", "language_code", "photo_url"),
        row,
    ))

def test_path_exists(url_path: str) -> bool:
    with pool.connection() as conn:
        row = conn.execute(
            "SELECT EXISTS (SELECT 1 FROM tests WHERE url_path = %s)",
            (url_path,),
        ).fetchone()
    return bool(row[0])


def upsert_account(user: dict) -> dict:
    with pool.connection() as conn:
        row = conn.execute(
            """
            INSERT INTO accounts (user_id, first_name, last_name, language_code, photo_url)
            VALUES (%s, %s, %s, %s, %s)
            ON CONFLICT (user_id) DO UPDATE SET
                first_name = EXCLUDED.first_name,
                last_name = EXCLUDED.last_name,
                language_code = EXCLUDED.language_code,
                photo_url = EXCLUDED.photo_url,
                updated_at = NOW()
            RETURNING id, user_id, first_name, last_name, language_code, photo_url
            """,
            (user["id"], user["first_name"], user["last_name"], user["language_code"], user["photo_url"]),
        ).fetchone()
    return _account_from_row(row)


def create_auth_session(account_id: int, token_hash: str, ttl_seconds: int) -> datetime:
    expires_at = datetime.now(timezone.utc) + timedelta(seconds=ttl_seconds)
    with pool.connection() as conn:
        conn.execute("DELETE FROM auth_sessions WHERE expires_at <= NOW()")
        conn.execute(
            "INSERT INTO auth_sessions (token_hash, account_id, expires_at) VALUES (%s, %s, %s)",
            (token_hash, account_id, expires_at),
        )
    return expires_at


_BETA_ENABLED_SQL = (
    "COALESCE((SELECT value = 'true'::jsonb FROM app_settings WHERE key = 'beta_enabled'), FALSE)"
)


def get_account_by_session(token_hash: str) -> dict | None:
    """Session's account plus the closed-beta state, in one query.

    The extra keys beta_enabled / beta_invited are internal: callers must pop them
    before exposing the account.
    """
    with pool.connection() as conn:
        row = conn.execute(
            f"""
            SELECT a.id, a.user_id, a.first_name, a.last_name, a.language_code, a.photo_url,
                {_BETA_ENABLED_SQL},
                EXISTS (SELECT 1 FROM beta_invites AS b WHERE b.max_user_id = a.user_id)
            FROM auth_sessions AS s
            JOIN accounts AS a ON a.id = s.account_id
            WHERE s.token_hash = %s AND s.expires_at > NOW()
            """,
            (token_hash,),
        ).fetchone()
    if row is None:
        return None
    return {**_account_from_row(row[:6]), "beta_enabled": bool(row[6]), "beta_invited": bool(row[7])}


def get_beta_status(max_user_id: int) -> tuple[bool, bool]:
    """(closed beta switched on in the admin panel, this MAX user is invited)."""
    with pool.connection() as conn:
        row = conn.execute(
            f"""
            SELECT {_BETA_ENABLED_SQL},
                EXISTS (SELECT 1 FROM beta_invites WHERE max_user_id = %s)
            """,
            (max_user_id,),
        ).fetchone()
    return bool(row[0]), bool(row[1])


def get_beta_contact_url() -> str | None:
    """Admin's contact link shown to people without an invite (set in the admin panel)."""
    with pool.connection() as conn:
        row = conn.execute(
            "SELECT value #>> '{}' FROM app_settings WHERE key = 'beta_contact_url' AND jsonb_typeof(value) = 'string'"
        ).fetchone()
    url = row[0] if row else None
    return url if isinstance(url, str) and url.startswith("https://") and len(url) <= 500 else None


def revoke_auth_session(token_hash: str) -> None:
    with pool.connection() as conn:
        conn.execute("DELETE FROM auth_sessions WHERE token_hash = %s", (token_hash,))

def new_test(url_path: str, title: str, data: dict, author_id: int) -> int | None:
    with pool.connection() as conn:
        row = conn.execute(
            """
            INSERT INTO tests (url_path, title, data, author_id)
            VALUES (%s, %s, %s, %s)
            ON CONFLICT (url_path) DO NOTHING
            RETURNING id
            """,
            (url_path, title, json.dumps(data), author_id),
        ).fetchone()
    return None if row is None else row[0]

def get_test(url_path: str):
    with pool.connection() as conn:
        row = conn.execute(
            """
            SELECT id, url_path, title, data, author_id
            FROM tests
            WHERE url_path = %s
            """,
            (url_path,)
        ).fetchone()

        if row is None:
            return None

        return {
            "id": row[0],
            "url_path": row[1],
            "title": row[2],
            "data": row[3],
            "author_id": row[4],
        }

def get_test_by_path(url_path: str) -> dict | None:
    with pool.connection() as conn:
        row = conn.execute(
            "SELECT id, title, data FROM tests WHERE url_path = %s",
            (url_path,),
        ).fetchone()
    if row is None:
        return None
    return {"id": row[0], "title": row[1], "data": row[2]}

def save_test_result(
    test_id: int,
    user_id: int,
    answers: dict[int, int | list[int] | str],
    good: int,
    bad: int,
    duration_seconds: int | None = None,
) -> int:
    with pool.connection() as conn:
        row = conn.execute(
            """
            INSERT INTO test_results (test_id, user_id, answers, good, bad, duration_seconds)
            VALUES (%s, %s, %s, %s, %s, %s)
            RETURNING id
            """,
            (test_id, user_id, json.dumps(answers), good, bad, duration_seconds),
        ).fetchone()

        return row[0]

def get_authored_tests(user_id: int) -> list[dict]:
    with pool.connection() as conn:
        rows = conn.execute(
            """
            SELECT t.id, t.url_path, t.title, COALESCE(t.data->>'subject', ''),
                CASE WHEN jsonb_typeof(t.data::jsonb->'questions') = 'array'
                    THEN jsonb_array_length(t.data::jsonb->'questions') ELSE 0 END,
                COALESCE(r.completions, 0)
            FROM tests AS t
            LEFT JOIN (
                SELECT test_id, COUNT(*) AS completions FROM test_results GROUP BY test_id
            ) AS r ON r.test_id = t.id
            WHERE t.author_id = %s
            ORDER BY t.id DESC
            """,
            (user_id,),
        ).fetchall()
    return [dict(zip(("id", "url_path", "title", "subject", "question_count", "completions"), row)) for row in rows]


def get_user_results(user_id: int, test_path: str | None = None) -> list[dict]:
    sql = """
        SELECT r.id, t.url_path, t.title, r.answers, r.good, r.bad, r.created_at,
            COALESCE(t.data->>'subject', ''), r.duration_seconds
        FROM test_results AS r
        JOIN tests AS t ON t.id = r.test_id
        WHERE r.user_id = %s
    """
    params: list = [user_id]

    if test_path is not None:
        sql += " AND t.url_path = %s"
        params.append(test_path)

    sql += " ORDER BY r.created_at DESC, r.id DESC"

    with pool.connection() as conn:
        rows = conn.execute(sql, params).fetchall()

    return [
        {
            "id": row[0],
            "url_path": row[1],
            "title": row[2],
            "answers": {int(k): v for k, v in row[3].items()},
            "good": row[4],
            "bad": row[5],
            "created_at": row[6].isoformat(),
            "subject": row[7],
            "duration_seconds": row[8],
        }
        for row in rows
    ]

def _result_from_row(row):
    if row is None:
        return None
    return {
        "id": row[0],
        "answers": {int(k): v for k, v in row[1].items()},
        "good": row[2], "bad": row[3], "created_at": row[4].isoformat(),
        "duration_seconds": row[5],
    }


def get_test_result(user_id: int, test_path: str, result_id: int) -> dict | None:
    with pool.connection() as conn:
        row = conn.execute(
            """
            SELECT r.id, r.answers, r.good, r.bad, r.created_at, r.duration_seconds
            FROM test_results AS r JOIN tests AS t ON t.id = r.test_id
            WHERE r.user_id = %s AND t.url_path = %s AND r.id = %s
            """,
            (user_id, test_path, result_id),
        ).fetchone()
    return _result_from_row(row)


def get_last_result(user_id: int, test_path: str) -> dict | None:
    with pool.connection() as conn:
        row = conn.execute(
            """
            SELECT r.id, r.answers, r.good, r.bad, r.created_at, r.duration_seconds
            FROM test_results AS r
            JOIN tests AS t ON t.id = r.test_id
            WHERE r.user_id = %s AND t.url_path = %s
            ORDER BY r.created_at DESC, r.id DESC
            LIMIT 1
            """,
            (user_id, test_path),
        ).fetchone()

    return _result_from_row(row)


_log = logging.getLogger(__name__)


def log_event(event: str, *, source: str = "app", max_user_id: int | None = None, account_id: int | None = None,
              first_name: str | None = None, last_name: str | None = None, details: dict | None = None) -> None:
    """Append to activity_log for the admin panel's "Логи". Never raises: losing a log line
    must not break a login, a test submission or the bot."""
    try:
        with pool.connection() as conn:
            conn.execute(
                """
                INSERT INTO activity_log (source, event, max_user_id, account_id, first_name, last_name, details)
                VALUES (%s, %s, %s, %s, %s, %s, %s)
                """,
                (source, event, max_user_id, account_id, (first_name or None) and first_name[:255],
                 (last_name or None) and last_name[:255], json.dumps(details or {}, ensure_ascii=False, default=str)),
            )
    except Exception as error:  # noqa: BLE001 — logging is best effort by design
        _log.warning("activity_log write failed for %s: %s", event, error)

def _parse_answers(raw_answers: dict | None) -> dict[int, any]:
    if not raw_answers:
        return {}

    return {
        int(question_id): answer_value
        for question_id, answer_value in raw_answers.items()
    }
def get_all_test_results_by_url(url_path: str) -> list[dict]:
    # test_results.user_id is accounts.id (the internal account), not the MAX user id.
    with pool.connection() as conn:
        rows = conn.execute(
            """
            SELECT r.id, r.user_id, a.first_name, a.last_name, a.photo_url,
                   r.answers, r.good, r.bad, r.created_at, r.duration_seconds
            FROM test_results AS r
            JOIN tests AS t ON t.id = r.test_id
            LEFT JOIN accounts AS a ON a.id = r.user_id
            WHERE t.url_path = %s
            ORDER BY r.created_at DESC, r.id DESC
            """,
            (url_path,),
        ).fetchall()

    results = []

    for (result_id, user_id, first_name, last_name, photo_url, answers, good, bad, created_at,
         duration_seconds) in rows:
        results.append({
            "id": result_id,
            "user_id": user_id,
            "first_name": first_name,
            "last_name": last_name,
            "photo_url": photo_url,
            "answers": _parse_answers(answers),
            "good": good,
            "bad": bad,
            "created_at": created_at.isoformat(),
            "duration_seconds": duration_seconds,
        })

    return results


def save_image(image_id: str, owner_id: int, object_key: str, content_type: str, size_bytes: int,
               width: int, height: int) -> None:
    with pool.connection() as conn:
        conn.execute(
            """
            INSERT INTO images (id, owner_id, object_key, content_type, size_bytes, width, height)
            VALUES (%s, %s, %s, %s, %s, %s, %s)
            """,
            (image_id, owner_id, object_key, content_type, size_bytes, width, height),
        )


def get_image_meta(image_id: str) -> dict | None:
    with pool.connection() as conn:
        row = conn.execute("SELECT object_key, content_type FROM images WHERE id = %s", (image_id,)).fetchone()
    return None if row is None else {"object_key": row[0], "content_type": row[1]}


def count_recent_images(owner_id: int, seconds: int) -> int:
    with pool.connection() as conn:
        return conn.execute(
            "SELECT count(*) FROM images WHERE owner_id = %s AND created_at > NOW() - make_interval(secs => %s)",
            (owner_id, seconds),
        ).fetchone()[0]


def images_owned_by(image_ids: list[str], owner_id: int) -> set[str]:
    if not image_ids:
        return set()
    with pool.connection() as conn:
        rows = conn.execute(
            "SELECT id FROM images WHERE owner_id = %s AND id = ANY(%s)", (owner_id, list(image_ids)),
        ).fetchall()
    return {row[0] for row in rows}


def count_test_results(test_id: int) -> int:
    with pool.connection() as conn:
        return conn.execute("SELECT count(*) FROM test_results WHERE test_id = %s", (test_id,)).fetchone()[0]


def update_test(url_path: str, author_id: int, title: str, data: dict, grade) -> int | None:
    """Replace the author's test and re-grade every saved attempt against the new answers.

    grade(answers) -> (good, bad), where answers is the stored JSON object. Returns how many
    results were re-graded, or None when there is no such test of this author.
    """
    with pool.connection() as conn:
        with conn.transaction():
            row = conn.execute(
                "SELECT id FROM tests WHERE url_path = %s AND author_id = %s FOR UPDATE",
                (url_path, author_id),
            ).fetchone()
            if row is None:
                return None
            test_id = row[0]
            conn.execute("UPDATE tests SET title = %s, data = %s WHERE id = %s", (title, json.dumps(data), test_id))
            results = conn.execute(
                "SELECT id, answers FROM test_results WHERE test_id = %s FOR UPDATE", (test_id,),
            ).fetchall()
            for result_id, answers in results:
                good, bad = grade(answers or {})
                conn.execute("UPDATE test_results SET good = %s, bad = %s WHERE id = %s", (good, bad, result_id))
    return len(results)


def delete_test(url_path: str, author_id: int) -> int | None:
    """Delete the author's test with all its results. Returns how many results went with it,
    or None when there is no such test of this author."""
    with pool.connection() as conn:
        with conn.transaction():
            row = conn.execute(
                "SELECT id FROM tests WHERE url_path = %s AND author_id = %s FOR UPDATE",
                (url_path, author_id),
            ).fetchone()
            if row is None:
                return None
            # The foreign key cascades too; deleting explicitly keeps the count for the log.
            removed = conn.execute("DELETE FROM test_results WHERE test_id = %s", (row[0],)).rowcount
            conn.execute("DELETE FROM tests WHERE id = %s", (row[0],))
    return removed


def get_student_result(test_path: str, result_id: int) -> dict | None:
    """Any user's result of this test, with who took it. Only for the test's author: the caller checks."""
    with pool.connection() as conn:
        row = conn.execute(
            """
            SELECT r.id, r.answers, r.good, r.bad, r.created_at, r.duration_seconds,
                   a.first_name, a.last_name, a.photo_url
            FROM test_results AS r
            JOIN tests AS t ON t.id = r.test_id
            LEFT JOIN accounts AS a ON a.id = r.user_id
            WHERE t.url_path = %s AND r.id = %s
            """,
            (test_path, result_id),
        ).fetchone()
    result = _result_from_row(row)
    if result is not None:
        result["student"] = {"first_name": row[6], "last_name": row[7], "photo_url": row[8]}
    return result