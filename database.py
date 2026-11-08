import os

from dotenv import load_dotenv

load_dotenv()


def connection_kwargs() -> dict:
    return {
        "dbname": os.getenv("DB_NAME"),
        "user": os.getenv("DB_LOGIN"),
        "password": os.getenv("DB_PASS"),
        "host": os.getenv("DB_IP"),
        "port": int(os.getenv("DB_PORT", "5432")),
        "connect_timeout": 5,
    }
