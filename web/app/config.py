from pathlib import Path
import json

CONFIG_PATH = Path.home() / "eveosint" / "config" / "db.json"
JOBS_CONFIG_PATH = Path.home() / "eveosint" / "config" / "jobs.json"

SCHEMA = "web"
SESSION_COOKIE = "eveosint_session"
SESSION_DAYS = 7
BASE_DIR = Path(__file__).resolve().parent.parent
TEMPLATES_DIR = BASE_DIR / "templates"

# Whitelist volontairement courte. Si un service n'existe pas sur ton serveur,
# il sera affiché comme indisponible au lieu d'inventer un état.
SYSTEM_SERVICES = [
    "eveosint-web",
    "nginx",
    "postgresql",
]


def load_db_config():
    with CONFIG_PATH.open("r", encoding="utf-8") as f:
        return json.load(f)


DB_CONFIG = load_db_config()
