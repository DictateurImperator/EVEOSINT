import psycopg2
from .config import DB_CONFIG, SCHEMA


def t(name):
    return f"{SCHEMA}.{name}"


def db():
    return psycopg2.connect(
        dbname=DB_CONFIG["db_name"],
        user=DB_CONFIG["db_user"],
        password=DB_CONFIG["db_password"],
        host=DB_CONFIG["db_host"],
        port=DB_CONFIG["db_port"],
    )
