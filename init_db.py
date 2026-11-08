from pathlib import Path

import psycopg

import storage
from database import connection_kwargs


def initialize_database(connection) -> None:
    schema_dir = Path(__file__).resolve().parent / "sql_requests"
    scripts = ("schema.sql", "auth_schema.sql", "test_schema.sql", "test_results.sql", "beta_schema.sql",
               "activity_schema.sql", "images_schema.sql")

    for filename in scripts:
        connection.execute((schema_dir / filename).read_text(encoding="utf-8"))

    # Old versions wrote arbitrary author/user IDs without creating accounts.
    # Allocate new accounts above those IDs instead of assigning their data
    # to the first real MAX user. Existing account ownership is preserved.
    connection.execute("LOCK TABLE accounts IN SHARE ROW EXCLUSIVE MODE")
    largest_id = connection.execute(
        """
        SELECT GREATEST(
            COALESCE((SELECT MAX(id) FROM accounts), 0),
            COALESCE((SELECT MAX(author_id) FROM tests), 0),
            COALESCE((SELECT MAX(user_id) FROM test_results), 0)
        )
        """
    ).fetchone()[0]
    next_id = connection.execute(
        "SELECT nextval(pg_get_serial_sequence('accounts', 'id'))"
    ).fetchone()[0]
    if largest_id >= next_id:
        connection.execute(
            "SELECT setval(pg_get_serial_sequence('accounts', 'id'), %s)", (largest_id,),
        )


def main() -> None:
    with psycopg.connect(**connection_kwargs()) as connection:
        initialize_database(connection)

    if storage.configured():
        storage.ensure_bucket()
        print(f"Image storage is ready: bucket {storage.bucket()}", flush=True)

    print("Database tables are ready: accounts, auth_sessions, tests, test_results, beta_invites, app_settings, "
          "activity_log", flush=True)


if __name__ == "__main__":
    main()
