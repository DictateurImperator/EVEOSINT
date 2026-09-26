import calendar
import csv
import hashlib
import html
import io
import json
import re
import threading
import zipfile
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, datetime
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import urljoin, urlparse

import requests

from .db import db, t


ARTICLE_BASE = "https://www.eveonline.com/news/view"
USER_AGENT = "EVEOSINT-MER-Scanner/1.1"
FIRST_MER_MONTH = date(2016, 2, 1)
MAX_WORKERS = 8
HTTP_TIMEOUT = (5, 12)

MER_ARCHIVE_DIR = Path.home() / "eveosint" / "data" / "mer" / "archive"
MER_KILL_DUMP_DIR = Path.home() / "eveosint" / "data" / "mer" / "dumps" / "kill"
MER_LOG_FILE = Path.home() / "eveosint" / "data" / "logs" / "mer.log"
DOWNLOAD_CHUNK_SIZE = 1024 * 1024
_download_lock = threading.Lock()
_analyze_lock = threading.Lock()
_import_kills_lock = threading.Lock()
_map_alliance_ids_lock = threading.Lock()
_match_killmails_lock = threading.Lock()
_log_lock = threading.Lock()


def _mer_log(message):
    """Log MER append-only. Une erreur de log ne doit jamais casser le traitement."""
    try:
        MER_LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
        line = f"{datetime.now().isoformat(timespec='seconds')} {message}\n"

        with _log_lock:
            with MER_LOG_FILE.open("a", encoding="utf-8") as handle:
                handle.write(line)
                handle.flush()
    except Exception:
        pass

KNOWN_ARCHIVE_HOSTS = (
    "content.eveonline.com",
    "cdn1.eveonline.com",
    "web.ccpgamescdn.com",
    "monthly-economic-report.s3.eu-west-1.amazonaws.com",
)

# Exceptions historiques confirmées dans les publications officielles CCP.
# Elles servent de garde-fou en plus du parsing générique des anciens liens.
ARTICLE_URL_OVERRIDES = {
    date(2021, 12, 1): (
        "https://www.eveonline.com/news/view/"
        "updated-monthly-economic-report-december-2021"
    ),
}


PINNED_MER_ZIP_URLS = {
    date(2016, 2, 1): "https://cdn1.eveonline.com/community/quant/EVEOnline_MER_Feb2016.zip",
    date(2016, 3, 1): "https://content.eveonline.com/www/newssystem/media/70162/1/EVEOnline_MER_Mar2016.zip",
    date(2016, 4, 1): "https://content.eveonline.com/www/newssystem/media/70258/1/EVEOnline_MER_Apr2016.zip",
    date(2016, 5, 1): "https://content.eveonline.com/www/newssystem/media/70343/1/EVEOnline_MER_May2016.zip",
    date(2016, 6, 1): "https://content.eveonline.com/www/newssystem/media/70401/1/EVEOnline_MER_June2016.zip",
    date(2016, 7, 1): "https://content.eveonline.com/www/newssystem/media/70454/1/EVEOnline_MER_July2016.zip",
    date(2016, 8, 1): "https://content.eveonline.com/www/newssystem/media/70511/1/EVEOnline_MER_Aug2016.zip",
    date(2016, 9, 1): "https://content.eveonline.com/www/newssystem/media/70577/1/EVEOnline_MER_Sep2016.zip",
    date(2016, 10, 1): "https://content.eveonline.com/www/newssystem/media/70687/1/EVEOnline_MER_Oct2016.zip",
    date(2016, 11, 1): "https://content.eveonline.com/www/newssystem/media/70766/1/EVEOnline_MER_Nov2016.zip",
    date(2016, 12, 1): "https://content.eveonline.com/www/newssystem/media/71808/1/EVEOnline_MER_Dec2016_v1.1.zip",
    date(2017, 1, 1): "https://cdn1.eveonline.com/community/MER/EVEOnline_MER_Jan2017.zip",
    date(2017, 2, 1): "https://cdn1.eveonline.com/community/MER/EVEOnline_MER_Feb2017.zip",
    date(2017, 3, 1): "https://cdn1.eveonline.com/community/MER/EVEOnline_MER_Mar2017.zip",
    date(2017, 4, 1): "https://cdn1.eveonline.com/community/MER/EVEOnline_MER_Apr2017.zip",
    date(2017, 5, 1): "https://cdn1.eveonline.com/community/MER/EVEOnline_MER_May2017.zip",
    date(2017, 6, 1): "https://cdn1.eveonline.com/community/MER/EVEOnline_MER_Jun2017.zip",
    date(2017, 7, 1): "https://cdn1.eveonline.com/community/MER/EVEOnline_MER_Jul2017.zip",
    date(2017, 8, 1): "https://cdn1.eveonline.com/community/MER/EVEOnline_MER_Aug17.zip",
    date(2017, 9, 1): "https://cdn1.eveonline.com/community/MER/EVEOnline_MER_Sep2017.zip",
    date(2017, 10, 1): "https://content.eveonline.com/www/newssystem/media/73479/1/EVEOnline_MER_Oct2017.zip",
    date(2017, 11, 1): "https://content.eveonline.com/www/newssystem/media/73542/1/EVEOnline_MER_Nov2017.zip",
    date(2017, 12, 1): "https://content.eveonline.com/www/newssystem/media/73589/1/EVEOnline_MER_Dec2017.zip",
    date(2018, 1, 1): "http://web.ccpgamescdn.com/newssystem/media/73619/1/EVEOnline_MER_Jan2018.zip",
    date(2018, 2, 1): "http://web.ccpgamescdn.com/newssystem/media/73619/1/EVEOnline_MER_Feb2018.zip",
    date(2018, 3, 1): "http://web.ccpgamescdn.com/newssystem/media/73619/1/EVEOnline_MER_Mar2018.zip",
    date(2018, 4, 1): "http://content.eveonline.com/www/newssystem/media/73592/1/EVEOnline_MER_Apr2018.zip",
    date(2018, 5, 1): "https://web.ccpgamescdn.com/newssystem/media/73619/1/EVEOnline_MER_May2018.zip",
    date(2018, 6, 1): "https://web.ccpgamescdn.com/newssystem/media/73619/1/EVEOnline_MER_Jun2018.zip",
    date(2018, 7, 1): "http://web.ccpgamescdn.com/newssystem/media/73619/1/EVEOnline_MER_Jul2018.zip",
    date(2018, 8, 1): "https://web.ccpgamescdn.com/aws/community/EVEOnline_MER_Aug2018.zip",
    date(2018, 9, 1): "https://web.ccpgamescdn.com/aws/community/EVEOnline_MER_Sep2018.zip",
    date(2018, 10, 1): "https://web.ccpgamescdn.com/aws/community/EVEOnline_MER_Oct2018.zip",
    date(2018, 11, 1): "https://web.ccpgamescdn.com/aws/community/EVEOnline_MER_Nov2018.zip",
    date(2018, 12, 1): "https://web.ccpgamescdn.com/aws/community/EVEOnline_MER_Dec2018.zip",
    date(2019, 1, 1): "https://web.ccpgamescdn.com/aws/community/EVEOnline_MER_Jan2019.zip",
    date(2019, 2, 1): "https://web.ccpgamescdn.com/aws/community/EVEOnline_MER_Feb2019.zip",
    date(2019, 3, 1): "https://web.ccpgamescdn.com/aws/community/EVEOnline_MER_Mar2019.zip",
    date(2019, 4, 1): "https://web.ccpgamescdn.com/aws/community/EVEOnline_MER_Apr2019.zip",
    date(2019, 5, 1): "https://web.ccpgamescdn.com/aws/community/EVEOnline_MER_May2019.zip",
    date(2019, 6, 1): "https://web.ccpgamescdn.com/aws/community/EVEOnline_MER_Jun2019.zip",
    date(2019, 7, 1): "https://web.ccpgamescdn.com/aws/community/EVEOnline_MER_Jul2019.zip",
    date(2019, 8, 1): "https://web.ccpgamescdn.com/aws/community/EVEOnline_MER_Aug2019.zip",
    date(2019, 9, 1): "https://web.ccpgamescdn.com/aws/community/EVEOnline_MER_Sep2019.zip",
    date(2019, 10, 1): "https://web.ccpgamescdn.com/aws/community/EVEOnline_MER_Oct2019.zip",
    date(2019, 11, 1): "https://web.ccpgamescdn.com/aws/community/EVEOnline_MER_Nov2019.zip",
    date(2019, 12, 1): "https://web.ccpgamescdn.com/aws/community/EVEOnline_MER_Dec2019b.zip",
    date(2020, 1, 1): "https://web.ccpgamescdn.com/aws/community/EVEOnline_MER_Jan2020.zip",
    date(2020, 2, 1): "https://web.ccpgamescdn.com/aws/community/EVEOnline_MER_Feb2020.zip",
    date(2020, 3, 1): "https://web.ccpgamescdn.com/aws/community/EVEOnline_MER_Mar2020.zip",
    date(2020, 4, 1): "https://web.ccpgamescdn.com/aws/community/EVEOnline_MER_Apr2020.zip",
    date(2020, 5, 1): "https://web.ccpgamescdn.com/aws/community/EVEOnline_MER_May2020.zip",
    date(2020, 6, 1): "https://web.ccpgamescdn.com/aws/community/EVEOnline_MER_Jun2020.zip",
    date(2020, 7, 1): "https://web.ccpgamescdn.com/aws/community/EVEOnline_MER_Jul2020.zip",
    date(2020, 8, 1): "https://web.ccpgamescdn.com/aws/community/EVEOnline_MER_Aug2020.zip",
    date(2020, 9, 1): "https://web.ccpgamescdn.com/aws/community/EVEOnline_MER_Sep2020.zip",
    date(2020, 10, 1): "https://web.ccpgamescdn.com/aws/community/EVEOnline_MER_Oct2020.zip",
    date(2020, 11, 1): "https://web.ccpgamescdn.com/aws/community/EVEOnline_MER_Nov2020.zip",
    date(2020, 12, 1): "https://web.ccpgamescdn.com/aws/community/EVEOnline_MER_Dec2020.zip",
    date(2021, 1, 1): "https://web.ccpgamescdn.com/aws/community/EVEOnline_MER_Jan2021.zip",
    date(2021, 2, 1): "https://web.ccpgamescdn.com/aws/community/EVEOnline_MER_Feb2021.zip",
    date(2021, 3, 1): "https://web.ccpgamescdn.com/aws/community/EVEOnline_MER_Mar2021.zip",
    date(2021, 4, 1): "https://web.ccpgamescdn.com/aws/community/EVEOnline_MER_Apr2021.zip",
    date(2021, 5, 1): "https://web.ccpgamescdn.com/aws/community/EVEOnline_MER_May2021.zip",
    date(2021, 6, 1): "https://web.ccpgamescdn.com/aws/community/EVEOnline_MER_Jun2021.zip",
    date(2021, 7, 1): "https://web.ccpgamescdn.com/aws/community/EVEOnline_MER_Jul2021.zip",
    date(2021, 8, 1): "https://web.ccpgamescdn.com/aws/community/EVEOnline_MER_Aug2021.zip",
    date(2021, 9, 1): "https://web.ccpgamescdn.com/aws/community/EVEOnline_MER_Sept2021.zip",
    date(2021, 10, 1): "https://web.ccpgamescdn.com/aws/community/EVEOnline_MER_Oct2021.zip",
    date(2021, 11, 1): "https://web.ccpgamescdn.com/aws/community/EVEOnline_MER_Nov2021.zip",
    date(2021, 12, 1): "https://web.ccpgamescdn.com/aws/community/EVEOnline_MER_Dec2021_Updated.zip",
    date(2022, 1, 1): "https://web.ccpgamescdn.com/aws/community/January_2022_MER.zip",
    date(2022, 2, 1): "https://web.ccpgamescdn.com/aws/community/EVEOnline_MER_Feb2022.zip",
    date(2022, 3, 1): "https://web.ccpgamescdn.com/aws/community/EVEOnline_MER_Mar2022.zip",
    date(2022, 4, 1): "https://web.ccpgamescdn.com/aws/community/EVEOnline_MER_Apr2022.zip",
    date(2022, 5, 1): "https://web.ccpgamescdn.com/aws/community/EVEOnline_MER_May2022.zip",
    date(2022, 6, 1): "https://web.ccpgamescdn.com/aws/community/EVEOnline_MER_Jun2022.zip",
    date(2022, 7, 1): "https://web.ccpgamescdn.com/aws/community/EVEOnline_MER_Jul2022.zip",
    date(2022, 8, 1): "https://web.ccpgamescdn.com/aws/community/EVEOnline_MER_Aug2022.zip",
    date(2022, 9, 1): "https://web.ccpgamescdn.com/aws/community/EVEOnline_MER_Sep2022-Updated.zip",
    date(2022, 10, 1): "https://web.ccpgamescdn.com/aws/community/EVEOnline_MER_Oct2022-updated.zip",
    date(2022, 11, 1): "https://web.ccpgamescdn.com/aws/community/EVEOnline_MER_Nov2022.zip",
    date(2022, 12, 1): "https://web.ccpgamescdn.com/aws/community/EVEOnline_MER_Dec2022.zip",
    date(2023, 1, 1): "https://web.ccpgamescdn.com/aws/community/EVEOnline_MER_Jan2023.zip",
    date(2023, 2, 1): "https://web.ccpgamescdn.com/aws/community/EVEOnline_MER_Feb2023.zip",
    date(2023, 3, 1): "https://web.ccpgamescdn.com/aws/community/EVEOnline_MER_Mar2023.zip",
    date(2023, 4, 1): "https://web.ccpgamescdn.com/aws/community/EVEOnline_MER_Apr2023.zip",
    date(2023, 5, 1): "https://web.ccpgamescdn.com/aws/community/EVEOnline_MER_May2023.zip",
    date(2023, 6, 1): "https://web.ccpgamescdn.com/aws/community/EVEOnline_MER_Jun2023.zip",
    date(2023, 7, 1): "https://web.ccpgamescdn.com/aws/community/EVEOnline_MER_Jul2023.zip",
    date(2023, 8, 1): "https://web.ccpgamescdn.com/aws/community/EVEOnline_MER_Aug2023.zip",
    date(2023, 9, 1): "https://web.ccpgamescdn.com/aws/community/EVEOnline_MER_Sep2023.zip",
    date(2023, 10, 1): "https://web.ccpgamescdn.com/aws/community/EVEOnline_MER_Oct2023.zip",
    date(2023, 11, 1): "https://web.ccpgamescdn.com/aws/community/EVEOnline_MER_Nov2023.zip",
    date(2023, 12, 1): "https://web.ccpgamescdn.com/aws/community/EVEOnline_MER_Dec2023.zip",
    date(2024, 1, 1): "https://web.ccpgamescdn.com/aws/community/EVEOnline_MER_Jan2024.zip",
    date(2024, 2, 1): "https://web.ccpgamescdn.com/aws/community/EVEOnline_MER_Feb2024.zip",
    date(2024, 3, 1): "https://web.ccpgamescdn.com/aws/community/EVEOnline_MER_Mar2024.zip",
    date(2024, 4, 1): "https://web.ccpgamescdn.com/aws/community/EVEOnline_MER_Apr2024.zip",
    date(2024, 5, 1): "https://web.ccpgamescdn.com/aws/community/EVEOnline_MER_May2024.zip",
    date(2024, 6, 1): "https://web.ccpgamescdn.com/aws/community/EVEOnline_MER_Jun2024.zip",
    date(2024, 7, 1): "https://web.ccpgamescdn.com/aws/community/EVEOnline_MER_Jul2024.zip",
    date(2024, 8, 1): "https://web.ccpgamescdn.com/aws/community/EVEOnline_MER_Aug2024.zip",
    date(2024, 9, 1): "https://web.ccpgamescdn.com/aws/community/EVEOnline_MER_Sep2024.zip",
    date(2024, 10, 1): "https://web.ccpgamescdn.com/aws/community/EVEOnline_MER_Oct2024.zip",
    date(2024, 11, 1): "https://web.ccpgamescdn.com/aws/community/EVEOnline_MER_Nov2024.zip",
    date(2024, 12, 1): "https://web.ccpgamescdn.com/aws/community/EVEOnline_MER_Dec2024.zip",
    date(2025, 1, 1): "https://web.ccpgamescdn.com/aws/community/EVEOnline_MER_Jan2025.zip",
    date(2025, 2, 1): "https://web.ccpgamescdn.com/aws/community/EVEOnline_MER_Feb2025.zip",
    date(2025, 3, 1): "https://web.ccpgamescdn.com/aws/community/EVEOnline_MER_Mar2025.zip",
    date(2025, 4, 1): "https://web.ccpgamescdn.com/aws/community/EVEOnline_MER_Apr2025.zip",
    date(2025, 5, 1): "https://web.ccpgamescdn.com/aws/community/EVEOnline_MER_May2025.zip",
    date(2025, 6, 1): "https://web.ccpgamescdn.com/aws/community/EVEOnline_MER_Jun2025.zip",
    date(2025, 7, 1): "https://web.ccpgamescdn.com/aws/community/EVEOnline_MER_Jul2025.zip",
    date(2025, 8, 1): "https://web.ccpgamescdn.com/aws/community/EVEOnline_MER_202508.zip",
    date(2025, 9, 1): "https://web.ccpgamescdn.com/aws/community/EVEOnline_MER_202509.zip",
    date(2025, 10, 1): "https://web.ccpgamescdn.com/aws/community/EVEOnline_MER_202510.zip",
    date(2025, 11, 1): "https://web.ccpgamescdn.com/aws/community/EVEOnline_MER_202511.zip",
    date(2025, 12, 1): "https://web.ccpgamescdn.com/aws/community/EVEOnline_MER_202512.zip",







}

LAST_PINNED_MER_MONTH = max(PINNED_MER_ZIP_URLS)


# Quelques anciens articles n'utilisent pas le nom complet du mois.
ARTICLE_MONTH_SLUGS = {
    1: ("january", "jan"),
    2: ("february", "feb"),
    3: ("march", "mar"),
    4: ("april", "apr"),
    5: ("may",),
    6: ("june", "jun"),
    7: ("july", "jul"),
    8: ("august", "aug"),
    9: ("september", "sept", "sep"),
    10: ("october", "oct"),
    11: ("november", "nov"),
    12: ("december", "dec"),
}


class _LinkParser(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.links = []

    def handle_starttag(self, tag, attrs):
        if tag.lower() != "a":
            return

        href = dict(attrs).get("href")
        if href:
            self.links.append(href)


def _previous_month():
    today = date.today()

    if today.month == 1:
        return date(today.year - 1, 12, 1)

    return date(today.year, today.month - 1, 1)


def _month_range(start, end):
    current = start

    while current <= end:
        yield current

        if current.month == 12:
            current = date(current.year + 1, 1, 1)
        else:
            current = date(current.year, current.month + 1, 1)


def _next_month(month):
    if month.month == 12:
        return date(month.year + 1, 1, 1)

    return date(month.year, month.month + 1, 1)


def _article_candidates(month):
    candidates = []

    override = ARTICLE_URL_OVERRIDES.get(month)
    if override:
        candidates.append(override)

    candidates.extend(
        f"{ARTICLE_BASE}/monthly-economic-report-{slug}-{month.year}"
        for slug in ARTICLE_MONTH_SLUGS[month.month]
    )

    return list(dict.fromkeys(candidates))


def _normalize_archive_link(raw_link, base_url):
    if not raw_link:
        return None

    value = html.unescape(raw_link.strip())
    value = value.replace("\\u0026", "&").replace("\\/", "/")

    if value.startswith("//"):
        return "https:" + value

    lower = value.lower()

    for host in KNOWN_ARCHIVE_HOSTS:
        if lower.startswith(host + "/"):
            return "https://" + value

    if lower.startswith(("http://", "https://")):
        return value

    return urljoin(base_url, value)


def _extract_zip_urls(page_html, base_url):
    """
    Lit uniquement les liens .zip réellement présents dans la page.
    """
    parser = _LinkParser()
    parser.feed(page_html)

    urls = []

    for href in parser.links:
        url = _normalize_archive_link(href, base_url)

        if (
            url
            and urlparse(url).path.lower().endswith(".zip")
        ):
            urls.append(url)

    cleaned_html = html.unescape(page_html)
    cleaned_html = cleaned_html.replace("\\u0026", "&").replace("\\/", "/")

    for match in re.finditer(
        r'https?://[^"\'<>\s]+?\.zip(?:\?[^"\'<>\s]*)?',
        cleaned_html,
        flags=re.IGNORECASE,
    ):
        urls.append(match.group(0))

    hosts_pattern = "|".join(
        re.escape(host)
        for host in KNOWN_ARCHIVE_HOSTS
    )

    for match in re.finditer(
        rf'(?<!://)(?:{hosts_pattern})/[^"\'<>\s]+?\.zip'
        rf'(?:\?[^"\'<>\s]*)?',
        cleaned_html,
        flags=re.IGNORECASE,
    ):
        urls.append("https://" + match.group(0))

    result = []

    for raw_url in urls:
        url = _normalize_archive_link(
            raw_url,
            base_url,
        )

        if (
            url
            and urlparse(url).path.lower().endswith(".zip")
        ):
            result.append(url)

    return list(dict.fromkeys(result))


def _zip_matches_month(url, month):
    filename = Path(urlparse(url).path).name.lower()
    compact = re.sub(r"[^a-z0-9]", "", filename)

    yyyy = f"{month.year:04d}"
    yy = f"{month.year % 100:02d}"
    mm = f"{month.month:02d}"

    # EVEOnline_MER_202508.zip
    if f"{yyyy}{mm}" in compact:
        return 1000

    for token in ARTICLE_MONTH_SLUGS[month.month]:
        token = re.sub(
            r"[^a-z0-9]",
            "",
            token.lower(),
        )

        # Apr2017 / April2017 / 2017Apr
        if (
            f"{token}{yyyy}" in compact
            or f"{yyyy}{token}" in compact
        ):
            return 950

        # Aug17 / Sep17
        if (
            f"{token}{yy}" in compact
            or f"{yy}{token}" in compact
        ):
            return 900

        # January_2022_MER.zip
        if token in compact and yyyy in compact:
            return 850

    return 0


def _fetch_mer_article(month):
    errors = []

    for article_url in _article_candidates(month):
        try:
            response = requests.get(
                article_url,
                headers={"User-Agent": USER_AGENT},
                timeout=HTTP_TIMEOUT,
                allow_redirects=True,
            )
        except requests.RequestException as exc:
            errors.append(f"{article_url}: {exc}")
            continue

        if response.status_code != 200:
            errors.append(
                f"{article_url}: HTTP {response.status_code}"
            )
            continue

        lower_html = response.text.lower()

        if (
            "monthly economic report" not in lower_html
            or str(month.year) not in lower_html
        ):
            errors.append(
                f"{article_url}: contenu MER non reconnu"
            )
            continue

        return {
            "url": response.url.split("?", 1)[0],
            "html": response.text,
            "base_url": response.url,
        }, errors

    return None, errors


def _scan_month(month):
    """
    RECUPERATION DES LIENS UNIQUEMENT.

    - page MER du mois ;
    - page MER du mois suivant pour récupérer une éventuelle correction ;
    - choix du lien ZIP dont le NOM correspond au mois demandé ;
    - stockage EXACT du href trouvé.

    Aucun HEAD.
    Aucun téléchargement de ZIP.
    Aucun contrôle du contenu du ZIP.
    Aucun lien ZIP hardcodé.
    """
    target_article, target_errors = _fetch_mer_article(month)

    if not target_article:
        return {
            "month": month,
            "article_url": None,
            "zip_url": None,
            "zip_http_status": None,
            "zip_size_bytes": None,
            "archive_filename": None,
            "scan_status": "article_missing",
            "last_error": (
                " | ".join(target_errors[-3:])
                if target_errors
                else "Article introuvable."
            ),
        }

    candidate_pages = [
        (0, month, target_article),
    ]

    following_month = _next_month(month)

    if following_month <= date.today().replace(day=1):
        next_article, _ = _fetch_mer_article(
            following_month
        )

        if next_article:
            candidate_pages.append(
                (
                    1,
                    following_month,
                    next_article,
                )
            )

    candidates = []

    for page_priority, source_month, article in candidate_pages:
        for zip_url in _extract_zip_urls(
            article["html"],
            article["base_url"],
        ):
            month_score = _zip_matches_month(
                zip_url,
                month,
            )

            if month_score <= 0:
                continue

            candidates.append({
                "url": zip_url,
                "score": month_score,
                "page_priority": page_priority,
                "source_month": source_month,
                "source_article": article["url"],
            })

    if not candidates:
        _mer_log(
            f"[SCAN] {month:%Y-%m} NO_ZIP "
            f"article={target_article['url']}"
        )

        return {
            "month": month,
            "article_url": target_article["url"],
            "zip_url": None,
            "zip_http_status": None,
            "zip_size_bytes": None,
            "archive_filename": None,
            "scan_status": "zip_missing",
            "last_error": (
                "Article trouvé mais aucun lien ZIP "
                "correspondant au mois détecté."
            ),
        }

    # Une archive du mois republiée dans le MER suivant est prioritaire.
    candidates.sort(
        key=lambda item: (
            -item["score"],
            -item["page_priority"],
            item["url"],
        )
    )

    selected = candidates[0]
    zip_url = selected["url"]

    _mer_log(
        f"[SCAN] {month:%Y-%m} "
        f"ARTICLE={target_article['url']} "
        f"ZIP={zip_url} "
        f"ZIP_SOURCE={selected['source_article']}"
    )

    return {
        "month": month,
        "article_url": target_article["url"],
        "zip_url": zip_url,
        "zip_http_status": None,
        "zip_size_bytes": None,
        "archive_filename": (
            Path(urlparse(zip_url).path).name
            or None
        ),
        "scan_status": "ok",
        "last_error": None,
    }


def scan_all_mer():
    """
    Catalogue MER stable.

    2016-02 -> 2026-07 :
        liens ZIP figés dans PINNED_MER_ZIP_URLS.
        Ils ne sont JAMAIS recalculés depuis les pages.

    Après 2025-12 :
        zone dynamique recalculée depuis les pages MER.
        Cela permet de tester 2026 avec la logique future.
    """
    end_month = _previous_month()

    pinned_months = [
        month
        for month in sorted(PINNED_MER_ZIP_URLS)
        if month <= end_month
    ]

    _mer_log(
        f"[SCAN] PINNED_START months={len(pinned_months)} "
        f"through={LAST_PINNED_MER_MONTH:%Y-%m}"
    )

    # 1. Réinjecte les liens vérifiés, sans toucher aux fichiers locaux.
    with db() as conn:
        with conn.cursor() as cur:
            for month in pinned_months:
                zip_url = PINNED_MER_ZIP_URLS[month]
                archive_filename = (
                    Path(urlparse(zip_url).path).name
                    or None
                )

                article_candidates = _article_candidates(month)
                default_article_url = (
                    article_candidates[0]
                    if article_candidates
                    else None
                )

                cur.execute(
                    f"""
                    INSERT INTO {t('mer_catalog')} (
                        month,
                        article_url,
                        zip_url,
                        zip_http_status,
                        zip_size_bytes,
                        archive_filename,
                        scan_status,
                        last_error,
                        scanned_at,
                        updated_at
                    )
                    VALUES (
                        %s, %s, %s, NULL, NULL, %s,
                        'ok', NULL, NOW(), NOW()
                    )
                    ON CONFLICT (month)
                    DO UPDATE SET
                        article_url = COALESCE(
                            {t('mer_catalog')}.article_url,
                            EXCLUDED.article_url
                        ),
                        zip_url = EXCLUDED.zip_url,
                        zip_http_status = NULL,
                        zip_size_bytes = NULL,
                        archive_filename = EXCLUDED.archive_filename,
                        scan_status = 'ok',
                        last_error = NULL,
                        scanned_at = NOW(),
                        updated_at = NOW();
                    """,
                    (
                        month,
                        default_article_url,
                        zip_url,
                        archive_filename,
                    ),
                )

                _mer_log(
                    f"[SCAN] PINNED {month:%Y-%m} ZIP={zip_url}"
                )

        conn.commit()

    # 2. Nouveaux mois seulement, après la zone figée.
    future_months = []

    start_future = _next_month(LAST_PINNED_MER_MONTH)

    if start_future <= end_month:
        future_months = list(
            _month_range(
                start_future,
                end_month,
            )
        )

    if future_months:
        # Zone non figée : on la rescane volontairement.
        # Cela permet de tester 2026 avec exactement la logique
        # qui servira pour les prochains mois.
        months_to_scan = future_months

        _mer_log(
            f"[SCAN] DYNAMIC months={len(months_to_scan)} "
            f"from={months_to_scan[0]:%Y-%m} "
            f"to={months_to_scan[-1]:%Y-%m}"
        )

        results = []

        if months_to_scan:
            with ThreadPoolExecutor(
                max_workers=MAX_WORKERS
            ) as executor:
                future_map = {
                    executor.submit(
                        _scan_month,
                        month,
                    ): month
                    for month in months_to_scan
                }

                for future in as_completed(future_map):
                    month = future_map[future]

                    try:
                        result = future.result()
                    except Exception as exc:
                        result = {
                            "month": month,
                            "article_url": None,
                            "zip_url": None,
                            "zip_http_status": None,
                            "zip_size_bytes": None,
                            "archive_filename": None,
                            "scan_status": "scan_error",
                            "last_error": str(exc),
                        }

                    results.append(result)

            with db() as conn:
                with conn.cursor() as cur:
                    for row in results:
                        cur.execute(
                            f"""
                            INSERT INTO {t('mer_catalog')} (
                                month,
                                article_url,
                                zip_url,
                                zip_http_status,
                                zip_size_bytes,
                                archive_filename,
                                scan_status,
                                last_error,
                                scanned_at,
                                updated_at
                            )
                            VALUES (
                                %s, %s, %s, NULL, NULL, %s,
                                %s, %s, NOW(), NOW()
                            )
                            ON CONFLICT (month)
                            DO UPDATE SET
                                article_url = COALESCE(
                                    {t('mer_catalog')}.article_url,
                                    EXCLUDED.article_url
                                ),
                                zip_url = EXCLUDED.zip_url,
                                archive_filename = EXCLUDED.archive_filename,
                                scan_status = EXCLUDED.scan_status,
                                last_error = EXCLUDED.last_error,
                                scanned_at = NOW(),
                                updated_at = NOW();
                            """,
                            (
                                row["month"],
                                row["article_url"],
                                row["zip_url"],
                                row["archive_filename"],
                                row["scan_status"],
                                row["last_error"],
                            ),
                        )

                conn.commit()

    # 3. Résumé réel du catalogue.
    with db() as conn:
        with conn.cursor() as cur:
            cur.execute(
                f"""
                SELECT
                    COUNT(*),
                    COUNT(article_url),
                    COUNT(zip_url),
                    COUNT(*) FILTER (
                        WHERE scan_status = 'ok'
                    )
                FROM {t('mer_catalog')}
                WHERE month >= %s
                  AND month <= %s;
                """,
                (
                    FIRST_MER_MONTH,
                    end_month,
                ),
            )

            total, articles, zips, ok = cur.fetchone()

    summary = {
        "months": total or 0,
        "articles": articles or 0,
        "zips": zips or 0,
        "ok": ok or 0,
        "errors": (total or 0) - (ok or 0),
    }

    _mer_log(
        f"[SCAN] PINNED_END "
        f"months={summary['months']} "
        f"zips={summary['zips']} "
        f"errors={summary['errors']}"
    )

    return summary


def list_mer_catalog():
    with db() as conn:
        with conn.cursor() as cur:
            cur.execute(
                f"""
                SELECT
                    month,
                    article_url,
                    zip_url,
                    zip_http_status,
                    zip_size_bytes,
                    archive_filename,
                    scan_status,
                    local_path,
                    kill_dump_state,
                    economic_dump_state,
                    last_error,
                    scanned_at,
                    archive_csv_count,
                    kill_dump_candidate_count,
                    kill_dump_member,
                    kill_dump_path,
                    kill_dump_columns,
                    kill_dump_column_count,
                    kill_dump_schema_hash,
                    kill_dump_size_bytes,
                    kill_dump_analyzed_at
                FROM {t('mer_catalog')}
                ORDER BY month DESC;
                """
            )

            rows = cur.fetchall()

    result = []

    for row in rows:
        local_path = row[7]
        local_present = bool(local_path and Path(local_path).is_file())

        result.append({
            "month": row[0].strftime("%Y-%m"),
            "article_url": row[1],
            "zip_url": row[2],
            "zip_http_status": row[3],
            "zip_size_bytes": row[4],
            "archive_filename": row[5],
            "scan_status": row[6],
            "local_path": local_path,
            "local": local_present,
            "kill_dump_state": row[8] or "unknown",
            "economic_dump_state": row[9] or "unknown",
            "last_error": row[10],
            "scanned_at": row[11],
            "archive_csv_count": row[12],
            "kill_dump_candidate_count": row[13],
            "kill_dump_member": row[14],
            "kill_dump_path": row[15],
            "kill_dump_columns": row[16] or [],
            "kill_dump_column_count": row[17],
            "kill_dump_schema_hash": row[18],
            "kill_dump_size_bytes": row[19],
            "kill_dump_analyzed_at": row[20],
        })

    schema_hashes = sorted({
        row["kill_dump_schema_hash"]
        for row in result
        if row["kill_dump_schema_hash"]
    })
    schema_group_by_hash = {
        value: index + 1
        for index, value in enumerate(schema_hashes)
    }

    for row in result:
        schema_hash = row.get("kill_dump_schema_hash")
        row["kill_schema_group"] = (
            schema_group_by_hash.get(schema_hash)
            if schema_hash
            else None
        )

    return result


def get_mer_catalog_summary():
    with db() as conn:
        with conn.cursor() as cur:
            cur.execute(
                f"""
                SELECT
                    COUNT(*),
                    COUNT(article_url),
                    COUNT(zip_url),
                    MAX(scanned_at),
                    COUNT(*) FILTER (WHERE kill_dump_state = 'present'),
                    COUNT(*) FILTER (WHERE kill_dump_state = 'absent'),
                    COUNT(DISTINCT kill_dump_schema_hash)
                        FILTER (WHERE kill_dump_schema_hash IS NOT NULL)
                FROM {t('mer_catalog')};
                """
            )
            (
                total,
                articles,
                zips,
                last_scan_at,
                kill_present,
                kill_absent,
                kill_schema_count,
            ) = cur.fetchone()

    return {
        "total": total or 0,
        "articles": articles or 0,
        "zips": zips or 0,
        "last_scan_at": last_scan_at,
        "kill_present": kill_present or 0,
        "kill_absent": kill_absent or 0,
        "kill_schema_count": kill_schema_count or 0,
    }



def _archive_host_allowed(url):
    host = (urlparse(url).hostname or "").lower()

    return (
        host in KNOWN_ARCHIVE_HOSTS
        or host.endswith(".eveonline.com")
        or host.endswith(".ccpgamescdn.com")
    )


def _safe_archive_filename(zip_url, stored_filename=None):
    candidate = stored_filename or Path(urlparse(zip_url).path).name
    candidate = Path(candidate or "").name

    if not candidate.lower().endswith(".zip"):
        raise ValueError(f"Nom d'archive invalide: {candidate!r}")

    if candidate in {"", ".", ".."}:
        raise ValueError("Nom d'archive vide ou invalide")

    return candidate


def _zip_is_valid(path):
    try:
        if not path.is_file() or path.stat().st_size <= 0:
            return False

        with zipfile.ZipFile(path, "r") as archive:
            return archive.testzip() is None

    except (OSError, zipfile.BadZipFile):
        return False


def _download_catalog_row(row):
    """
    Règle absolue :
      - prend exactement mer_catalog.zip_url ;
      - ZIP physique présent => rien ;
      - ZIP physique absent => GET de ce zip_url et sauvegarde.

    Aucune analyse du contenu ici.
    """
    month = row["month"]
    zip_url = row["zip_url"]

    if not zip_url:
        return {
            "month": month,
            "status": "skipped",
            "error": "Aucun zip_url.",
        }

    filename = Path(urlparse(zip_url).path).name

    if not filename:
        return {
            "month": month,
            "status": "failed",
            "error": "Nom de fichier absent dans zip_url.",
        }

    year_dir = MER_ARCHIVE_DIR / f"{month.year:04d}"
    year_dir.mkdir(
        parents=True,
        exist_ok=True,
    )

    final_path = year_dir / filename
    partial_path = year_dir / f"{filename}.part"

    _mer_log(
        f"[DOWNLOAD] {month:%Y-%m} "
        f"URL={zip_url} FILE={final_path}"
    )

    # Présence physique = aucune requête réseau.
    if final_path.is_file():
        size = final_path.stat().st_size

        with db() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    f"""
                    UPDATE {t('mer_catalog')}
                    SET
                        archive_filename = %s,
                        local_path = %s,
                        last_error = NULL,
                        updated_at = NOW()
                    WHERE month = %s;
                    """,
                    (
                        filename,
                        str(final_path),
                        month,
                    ),
                )

            conn.commit()

        return {
            "month": month,
            "status": "already_present",
            "path": str(final_path),
            "bytes": size,
        }

    if partial_path.exists():
        partial_path.unlink()

    try:
        # EXACTEMENT le lien enregistré dans mer_catalog.zip_url.
        with requests.get(
            zip_url,
            headers={"User-Agent": USER_AGENT},
            stream=True,
            timeout=(10, 300),
        ) as response:
            response.raise_for_status()

            _mer_log(
                f"[DOWNLOAD] {month:%Y-%m} "
                f"HTTP status={response.status_code} "
                f"requested={zip_url} final={response.url}"
            )

            with partial_path.open("wb") as output:
                for chunk in response.iter_content(
                    DOWNLOAD_CHUNK_SIZE
                ):
                    if chunk:
                        output.write(chunk)

        partial_path.replace(final_path)
        final_size = final_path.stat().st_size

        with db() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    f"""
                    UPDATE {t('mer_catalog')}
                    SET
                        archive_filename = %s,
                        local_path = %s,
                        zip_http_status = 200,
                        zip_size_bytes = %s,
                        last_error = NULL,
                        updated_at = NOW()
                    WHERE month = %s;
                    """,
                    (
                        filename,
                        str(final_path),
                        final_size,
                        month,
                    ),
                )

            conn.commit()

        _mer_log(
            f"[DOWNLOAD] {month:%Y-%m} "
            f"DOWNLOADED bytes={final_size} "
            f"path={final_path}"
        )

        return {
            "month": month,
            "status": "downloaded",
            "path": str(final_path),
            "bytes": final_size,
        }

    except Exception as exc:
        if partial_path.exists():
            partial_path.unlink()

        with db() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    f"""
                    UPDATE {t('mer_catalog')}
                    SET
                        last_error = %s,
                        updated_at = NOW()
                    WHERE month = %s;
                    """,
                    (
                        str(exc),
                        month,
                    ),
                )

            conn.commit()

        _mer_log(
            f"[DOWNLOAD] {month:%Y-%m} "
            f"FAILED url={zip_url} error={exc}"
        )

        return {
            "month": month,
            "status": "failed",
            "error": str(exc),
        }


def _catalog_rows_for_download(month=None):
    with db() as conn:
        with conn.cursor() as cur:
            if month is None:
                cur.execute(
                    f"""
                    SELECT
                        month,
                        zip_url
                    FROM {t('mer_catalog')}
                    WHERE zip_url IS NOT NULL
                    ORDER BY month ASC;
                    """
                )
            else:
                cur.execute(
                    f"""
                    SELECT
                        month,
                        zip_url
                    FROM {t('mer_catalog')}
                    WHERE month = %s
                      AND zip_url IS NOT NULL;
                    """,
                    (month,),
                )

            rows = cur.fetchall()

    return [
        {
            "month": row[0],
            "zip_url": row[1],
        }
        for row in rows
    ]


def reset_mer_downloads():
    """
    Remise à zéro EXCEPTIONNELLE des téléchargements MER locaux.

    Supprime uniquement :
      - ~/eveosint/data/mer/archive/**/*.zip + .part
      - ~/eveosint/data/mer/dumps/kill/**/*.csv + .part

    Conserve intégralement :
      - le catalogue MER ;
      - article_url / zip_url ;
      - les métadonnées de scan ;
      - le reste d'EVEOSINT.
    """
    if not _download_lock.acquire(blocking=False):
        return {
            "busy": True,
            "deleted_archives": 0,
            "deleted_kill_dumps": 0,
        }

    if not _analyze_lock.acquire(blocking=False):
        _download_lock.release()
        return {
            "busy": True,
            "deleted_archives": 0,
            "deleted_kill_dumps": 0,
        }

    try:
        _mer_log("[RESET_DOWNLOADS] START")

        deleted_archives = 0
        deleted_kill_dumps = 0

        if MER_ARCHIVE_DIR.is_dir():
            for path in MER_ARCHIVE_DIR.rglob("*"):
                if (
                    path.is_file()
                    and (
                        path.suffix.lower() == ".zip"
                        or path.name.lower().endswith(".zip.part")
                    )
                ):
                    path.unlink()
                    deleted_archives += 1

        if MER_KILL_DUMP_DIR.is_dir():
            for path in MER_KILL_DUMP_DIR.rglob("*"):
                if (
                    path.is_file()
                    and (
                        path.suffix.lower() == ".csv"
                        or path.name.lower().endswith(".csv.part")
                    )
                ):
                    path.unlink()
                    deleted_kill_dumps += 1

        with db() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    f"""
                    UPDATE {t('mer_catalog')}
                    SET
                        local_path = NULL,
                        kill_dump_state = 'unknown',
                        economic_dump_state = 'unknown',
                        archive_csv_count = NULL,
                        kill_dump_candidate_count = NULL,
                        kill_dump_member = NULL,
                        kill_dump_path = NULL,
                        kill_dump_columns = NULL,
                        kill_dump_column_count = NULL,
                        kill_dump_schema_hash = NULL,
                        kill_dump_size_bytes = NULL,
                        kill_dump_analyzed_at = NULL,
                        last_error = NULL,
                        updated_at = NOW();
                    """
                )

            conn.commit()

        _mer_log(
            f"[RESET_DOWNLOADS] END "
            f"archives={deleted_archives} "
            f"kill_dumps={deleted_kill_dumps}"
        )

        return {
            "busy": False,
            "deleted_archives": deleted_archives,
            "deleted_kill_dumps": deleted_kill_dumps,
        }

    finally:
        _analyze_lock.release()
        _download_lock.release()


def download_mer_archives(month=None):
    """
    Télécharge les ZIP MER catalogués.
    Si month est fourni, ne traite que ce mois.
    Retourne immédiatement avec busy=True si un téléchargement est déjà lancé.
    """
    if not _download_lock.acquire(blocking=False):
        _mer_log("[DOWNLOAD] BUSY")
        return {
            "busy": True,
            "processed": 0,
            "downloaded": 0,
            "already_present": 0,
            "failed": 0,
        }

    try:
        rows = _catalog_rows_for_download(month=month)

        _mer_log(
            f"[DOWNLOAD] START target="
            f"{month.strftime('%Y-%m') if month else 'ALL'} "
            f"rows={len(rows)}"
        )

        stats = {
            "busy": False,
            "processed": 0,
            "downloaded": 0,
            "already_present": 0,
            "failed": 0,
        }

        for row in rows:
            row_month = row["month"]
            _mer_log(
                f"[DOWNLOAD] {row_month:%Y-%m} START "
                f"url={row.get('zip_url') or '-'} "
                f"expected_size={row.get('zip_size_bytes')}"
            )

            result = _download_catalog_row(row)
            stats["processed"] += 1

            status = result["status"]

            if status == "downloaded":
                stats["downloaded"] += 1
            elif status == "already_present":
                stats["already_present"] += 1
            elif status == "failed":
                stats["failed"] += 1

            _mer_log(
                f"[DOWNLOAD] {row_month:%Y-%m} "
                f"status={status} "
                f"bytes={result.get('bytes')} "
                f"path={result.get('path') or '-'} "
                f"error={result.get('error') or '-'}"
            )

        _mer_log(
            f"[DOWNLOAD] END processed={stats['processed']} "
            f"downloaded={stats['downloaded']} "
            f"already_present={stats['already_present']} "
            f"failed={stats['failed']}"
        )

        return stats

    finally:
        _download_lock.release()



KILL_HEADER_HINTS = {
    "kill_datetime",
    "killmail_time",
    "kill_solarsystem_id",
    "solar_system_id",
    "victim_ship_type_id",
    "victim_corporation_id",
    "victim_alliance_id",
    "killer_ship_type_id",
    "killer_corporation_id",
    "killer_alliance_id",
    "attacker_ship_type_id",
    "attacker_corporation_id",
    "attacker_alliance_id",
}


def _read_csv_header(archive, member_name):
    with archive.open(member_name, "r") as raw:
        text = io.TextIOWrapper(
            raw,
            encoding="utf-8-sig",
            newline="",
            errors="replace",
        )
        reader = csv.reader(text)
        row = next(reader, [])

    return [
        str(value).strip()
        for value in row
    ]


def _sample_csv_rows(archive, member_name, limit=25):
    rows = []

    with archive.open(member_name, "r") as raw:
        text = io.TextIOWrapper(
            raw,
            encoding="utf-8-sig",
            newline="",
            errors="replace",
        )
        reader = csv.reader(text)

        # Header
        next(reader, None)

        for row in reader:
            rows.append(row)
            if len(rows) >= limit:
                break

    return rows


def _kill_candidate_score(member_name, columns):
    basename = Path(member_name).name.lower()
    normalized_columns = {
        str(value).strip().lower()
        for value in columns
        if str(value).strip()
    }

    score = 0

    if "kill" in basename:
        score += 100

    if "kill_dump" in basename or "killdump" in basename:
        score += 100

    score += 10 * len(normalized_columns & KILL_HEADER_HINTS)

    if any(value.startswith("victim_") for value in normalized_columns):
        score += 20

    if any(
        value.startswith(("killer_", "attacker_"))
        for value in normalized_columns
    ):
        score += 20

    if any(
        value in normalized_columns
        for value in ("kill_datetime", "killmail_time", "killtime")
    ):
        score += 30

    return score


def _rows_match_month(rows, month):
    month_tokens = (
        f"{month:%Y-%m}-",
        f"{month:%Y/%m}/",
        f"{month:%Y.%m}.",
    )

    for row in rows:
        for value in row:
            value = str(value)

            if any(token in value for token in month_tokens):
                return True

    return False


def _archive_kill_candidates(archive_path, month):
    """
    Cherche le kill dump directement dans un ZIP MER LOCAL.

    Aucun téléchargement.
    Priorité aux noms CCP connus :
      - Killdump.csv
      - kill_dump.csv
    """
    candidates = []
    inspected_members = []

    try:
        with zipfile.ZipFile(archive_path, "r") as archive:
            members = [
                info
                for info in archive.infolist()
                if not info.is_dir()
            ]

            inspected_members = [
                info.filename
                for info in members
            ]

            exact_names = {
                "killdump.csv",
                "kill_dump.csv",
            }

            kill_infos = [
                info
                for info in members
                if Path(info.filename).name.lower() in exact_names
            ]

            # Fallback uniquement si CCP change encore le nom.
            if not kill_infos:
                kill_infos = [
                    info
                    for info in members
                    if (
                        info.filename.lower().endswith(".csv")
                        and "kill" in Path(
                            info.filename
                        ).name.lower()
                    )
                ]

            for info in kill_infos:
                member_name = info.filename

                try:
                    columns = _read_csv_header(
                        archive,
                        member_name,
                    )
                except Exception as exc:
                    _mer_log(
                        f"[ANALYZE] {month:%Y-%m} "
                        f"HEADER_ERROR archive={archive_path} "
                        f"member={member_name} error={exc}"
                    )
                    continue

                try:
                    sample_rows = _sample_csv_rows(
                        archive,
                        member_name,
                        limit=100,
                    )
                except Exception as exc:
                    _mer_log(
                        f"[ANALYZE] {month:%Y-%m} "
                        f"SAMPLE_ERROR archive={archive_path} "
                        f"member={member_name} error={exc}"
                    )
                    sample_rows = []

                month_match = _rows_match_month(
                    sample_rows,
                    month,
                )

                _mer_log(
                    f"[ANALYZE] {month:%Y-%m} "
                    f"KILL_MEMBER archive={archive_path} "
                    f"member={member_name} "
                    f"columns={len(columns)} "
                    f"month_match={month_match}"
                )

                candidates.append({
                    "archive_path": str(archive_path),
                    "member_name": member_name,
                    "columns": columns,
                    "score": 1000 if month_match else 100,
                    "size_bytes": info.file_size,
                    "month_match": month_match,
                    "archive_member_count": len(members),
                })

    except Exception as exc:
        return [], [], str(exc)

    candidates.sort(
        key=lambda value: (
            0 if value["month_match"] else 1,
            -value["score"],
            value["member_name"].lower(),
        )
    )

    return candidates, inspected_members, None


def _local_archive_candidates(month, stored_local_path):
    """
    Liste les ZIP DEJA PRESENTS localement pour le mois.

    Le chemin BDD passe en premier, puis les autres ZIP du répertoire
    de l'année. Cela répare notamment une mauvaise association BDD
    mois -> ZIP sans rien télécharger.
    """
    result = []
    seen = set()

    def add(path):
        if not path:
            return

        path = Path(path)

        try:
            key = str(path.resolve())
        except Exception:
            key = str(path)

        if key in seen:
            return

        if path.is_file() and path.suffix.lower() == ".zip":
            seen.add(key)
            result.append(path)

    add(stored_local_path)

    year_dir = MER_ARCHIVE_DIR / f"{month.year:04d}"

    if year_dir.is_dir():
        # D'abord les noms qui ressemblent au mois recherché.
        all_zips = sorted(
            year_dir.glob("*.zip"),
            key=lambda path: path.name.lower(),
        )

        month_names = ARTICLE_MONTH_SLUGS.get(month.month, ())

        def month_name_score(path):
            compact = re.sub(
                r"[^a-z0-9]",
                "",
                path.name.lower(),
            )

            score = 0

            if f"{month.year:04d}{month.month:02d}" in compact:
                score += 100

            for token in month_names:
                token_compact = re.sub(
                    r"[^a-z0-9]",
                    "",
                    token.lower(),
                )
                if f"{token_compact}{month.year}" in compact:
                    score += 100
                    break

            return score

        all_zips.sort(
            key=lambda path: (
                -month_name_score(path),
                path.name.lower(),
            )
        )

        for path in all_zips:
            add(path)

    return result


def _schema_hash(columns):
    normalized = sorted(
        str(value).strip().lower()
        for value in columns
        if str(value).strip()
    )

    payload = "\0".join(normalized).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _extract_kill_dump_from_path(
    archive_path,
    member_name,
    month,
):
    destination_dir = MER_KILL_DUMP_DIR / f"{month.year:04d}"
    destination_dir.mkdir(parents=True, exist_ok=True)

    final_path = destination_dir / f"{month:%Y-%m}.csv"
    partial_path = destination_dir / f"{month:%Y-%m}.csv.part"

    if partial_path.exists():
        partial_path.unlink()

    try:
        with zipfile.ZipFile(archive_path, "r") as archive:
            with archive.open(member_name, "r") as source:
                with partial_path.open("wb") as target:
                    while True:
                        chunk = source.read(DOWNLOAD_CHUNK_SIZE)
                        if not chunk:
                            break

                        target.write(chunk)

        if not partial_path.is_file() or partial_path.stat().st_size <= 0:
            raise RuntimeError("Kill dump extrait vide.")

        partial_path.replace(final_path)

        _mer_log(
            f"[EXTRACT] {month:%Y-%m} "
            f"archive={archive_path} "
            f"member={member_name} "
            f"-> {final_path}"
        )

        return final_path

    except Exception:
        if partial_path.exists():
            partial_path.unlink()
        raise


def _analyze_catalog_row(row):
    month = row["month"]
    stored_local_path = row.get("local_path")

    archive_paths = _local_archive_candidates(
        month,
        stored_local_path,
    )

    if not archive_paths:
        result = {
            "month": month,
            "archive_csv_count": 0,
            "candidate_count": 0,
            "state": "error",
            "member_name": None,
            "dump_path": None,
            "columns": [],
            "schema_hash": None,
            "size_bytes": None,
            "error": "Aucune archive ZIP locale trouvée.",
            "resolved_archive_path": None,
        }
    else:
        all_candidates = []
        total_csv_count = 0
        diagnostics = []

        for archive_path in archive_paths:
            candidates, members, archive_error = (
                _archive_kill_candidates(
                    archive_path,
                    month,
                )
            )

            csv_count = sum(
                1
                for name in members
                if name.lower().endswith(".csv")
            )
            total_csv_count += csv_count

            if archive_error:
                diagnostics.append(
                    f"{archive_path.name}: ERROR {archive_error}"
                )
                _mer_log(
                    f"[ANALYZE] {month:%Y-%m} "
                    f"archive={archive_path} "
                    f"ERROR={archive_error}"
                )
                continue

            _mer_log(
                f"[ANALYZE] {month:%Y-%m} "
                f"archive={archive_path} "
                f"members={len(members)} "
                f"csv={csv_count} "
                f"kill_candidates={len(candidates)}"
            )

            if not candidates:
                # Log utile uniquement en cas de raté : on voit enfin
                # ce que contient réellement le vieux ZIP.
                preview = " | ".join(members[:40])
                _mer_log(
                    f"[ANALYZE] {month:%Y-%m} "
                    f"NO_KILL_IN_ARCHIVE={archive_path} "
                    f"members_preview={preview}"
                )

            all_candidates.extend(candidates)

            # Dès qu'on a un candidat avec preuve du mois, inutile de
            # continuer à ouvrir tous les ZIP de l'année.
            if any(
                candidate["month_match"]
                for candidate in candidates
            ):
                break

        all_candidates = [
            value
            for value in all_candidates
            if value["month_match"]
        ]

        all_candidates.sort(
            key=lambda value: (
                -value["score"],
                value["archive_path"].lower(),
                value["member_name"].lower(),
            )
        )

        if not all_candidates:
            result = {
                "month": month,
                "archive_csv_count": total_csv_count,
                "candidate_count": 0,
                "state": "absent",
                "member_name": None,
                "dump_path": None,
                "columns": [],
                "schema_hash": None,
                "size_bytes": None,
                "error": (
                    "Kill dump introuvable dans les ZIP locaux de "
                    f"{month.year}."
                ),
                "resolved_archive_path": None,
            }

        else:
            selected = all_candidates[0]

            # Si plusieurs candidats ont exactement la même priorité,
            # on refuse de deviner.
            same_rank = [
                value
                for value in all_candidates
                if value["score"] == selected["score"]
            ]

            if len(same_rank) > 1:
                result = {
                    "month": month,
                    "archive_csv_count": total_csv_count,
                    "candidate_count": len(all_candidates),
                    "state": "multiple",
                    "member_name": None,
                    "dump_path": None,
                    "columns": [],
                    "schema_hash": None,
                    "size_bytes": None,
                    "error": (
                        "Plusieurs kill dumps locaux équivalents: "
                        + ", ".join(
                            f"{Path(value['archive_path']).name}"
                            f"::{value['member_name']}"
                            for value in same_rank
                        )
                    ),
                    "resolved_archive_path": None,
                }

            else:
                resolved_archive_path = Path(
                    selected["archive_path"]
                )

                dump_path = _extract_kill_dump_from_path(
                    resolved_archive_path,
                    selected["member_name"],
                    month,
                )

                result = {
                    "month": month,
                    "archive_csv_count": total_csv_count,
                    "candidate_count": len(all_candidates),
                    "state": "present",
                    "member_name": selected["member_name"],
                    "dump_path": str(dump_path),
                    "columns": selected["columns"],
                    "schema_hash": _schema_hash(
                        selected["columns"]
                    ),
                    "size_bytes": selected["size_bytes"],
                    "error": None,
                    "resolved_archive_path": str(
                        resolved_archive_path
                    ),
                }

                if (
                    not stored_local_path
                    or Path(stored_local_path)
                    != resolved_archive_path
                ):
                    _mer_log(
                        f"[ANALYZE] {month:%Y-%m} "
                        f"LOCAL_PATH_REPAIRED "
                        f"old={stored_local_path or '-'} "
                        f"new={resolved_archive_path}"
                    )

    with db() as conn:
        with conn.cursor() as cur:
            cur.execute(
                f"""
                UPDATE {t('mer_catalog')}
                SET
                    local_path = COALESCE(%s, local_path),
                    archive_csv_count = %s,
                    kill_dump_candidate_count = %s,
                    kill_dump_state = %s,
                    kill_dump_member = %s,
                    kill_dump_path = %s,
                    kill_dump_columns = %s::jsonb,
                    kill_dump_column_count = %s,
                    kill_dump_schema_hash = %s,
                    kill_dump_size_bytes = %s,
                    kill_dump_analyzed_at = NOW(),
                    last_error = %s,
                    updated_at = NOW()
                WHERE month = %s;
                """,
                (
                    result.get("resolved_archive_path"),
                    result.get("archive_csv_count"),
                    result.get("candidate_count"),
                    result["state"],
                    result.get("member_name"),
                    result.get("dump_path"),
                    json.dumps(result.get("columns") or []),
                    len(result.get("columns") or []),
                    result.get("schema_hash"),
                    result.get("size_bytes"),
                    result.get("error"),
                    month,
                ),
            )

        conn.commit()

    return result


def _catalog_rows_for_analysis(month=None):
    with db() as conn:
        with conn.cursor() as cur:
            if month is None:
                cur.execute(
                    f"""
                    SELECT
                        month,
                        local_path
                    FROM {t('mer_catalog')}
                    WHERE local_path IS NOT NULL
                    ORDER BY month ASC;
                    """
                )
            else:
                cur.execute(
                    f"""
                    SELECT
                        month,
                        local_path
                    FROM {t('mer_catalog')}
                    WHERE month = %s
                      AND local_path IS NOT NULL;
                    """,
                    (month,),
                )

            rows = cur.fetchall()

    return [
        {
            "month": row[0],
            "local_path": row[1],
        }
        for row in rows
    ]


def analyze_mer_archives(month=None):
    """
    Analyse les ZIP MER déjà téléchargés.

    - détecte le CSV de kills ;
    - extrait le dump sélectionné vers
      ~/eveosint/data/mer/dumps/kill/YYYY/YYYY-MM.csv ;
    - mémorise son chemin interne dans le ZIP ;
    - mémorise ses colonnes et un hash de structure ;
    - ne touche pas aux données économiques.
    """
    if not _analyze_lock.acquire(blocking=False):
        _mer_log("[ANALYZE] BUSY")
        return {
            "busy": True,
            "processed": 0,
            "present": 0,
            "absent": 0,
            "multiple": 0,
            "failed": 0,
        }

    try:
        rows = _catalog_rows_for_analysis(month=month)

        _mer_log(
            f"[ANALYZE] START LOCAL_ONLY target="
            f"{month.strftime('%Y-%m') if month else 'ALL'} "
            f"rows={len(rows)}"
        )

        stats = {
            "busy": False,
            "processed": 0,
            "present": 0,
            "absent": 0,
            "multiple": 0,
            "failed": 0,
        }

        for row in rows:
            row_month = row["month"]

            _mer_log(
                f"[ANALYZE] {row_month:%Y-%m} START "
                f"archive={row.get('local_path') or '-'}"
            )

            result = _analyze_catalog_row(row)
            stats["processed"] += 1

            state = result["state"]

            if state == "present":
                stats["present"] += 1
            elif state == "absent":
                stats["absent"] += 1
            elif state == "multiple":
                stats["multiple"] += 1
            else:
                stats["failed"] += 1

            _mer_log(
                f"[ANALYZE] {row_month:%Y-%m} "
                f"state={state} "
                f"csv={result.get('archive_csv_count')} "
                f"candidates={result.get('candidate_count')} "
                f"member={result.get('member_name') or '-'} "
                f"columns={len(result.get('columns') or [])} "
                f"schema={result.get('schema_hash') or '-'} "
                f"error={result.get('error') or '-'}"
            )

        _mer_log(
            f"[ANALYZE] END processed={stats['processed']} "
            f"present={stats['present']} "
            f"absent={stats['absent']} "
            f"multiple={stats['multiple']} "
            f"failed={stats['failed']}"
        )

        return stats

    finally:
        _analyze_lock.release()


# ============================================================
# MER KILL DUMP IMPORT
# ============================================================

MER_DATA_SCHEMA = "mer"
MER_KILL_TABLE = "killmails"
MER_KILL_IMPORT_TABLE = "kill_dump_imports"

# Hashes calculés par _schema_hash() sur les en-têtes CCP.
MER_KILL_SCHEMA_CAMEL_HASH = (
    "5995e1dde64ab0a19edb7bf6adb7efc66afe3090d4ab38a988a3a05437df34de"
)
MER_KILL_SCHEMA_SNAKE_HASH = (
    "c32da52a475c3e30254912e247bebd8c30b423122ec625837da50e7fc1c55689"
)
MER_KILL_SCHEMA_14_HASH = (
    "7107ed42a135b3d44737c2c183306d062e04d338bdfeb49e452c259b6d2a9246"
)

MER_KILL_SCHEMA_MAPPINGS = {
    MER_KILL_SCHEMA_CAMEL_HASH: {
        "kill_datetime": "killTime",
        "solar_system_id": "solarSystemID",
        "solar_system_name": "solarSystemName",
        "region_id": "regionID",
        "region_name": "regionName",
        "victim_ship_type_id": "destroyedShipTypeID",
        "victim_ship_type_name": "destroyedShipType",
        "victim_ship_group_name": "destroyedShipGroup",
        "victim_corporation_id": "victimCorporationID",
        "victim_corporation_name": "victimCorp",
        "victim_alliance_id": None,
        "victim_alliance_name": "victimAlliance",
        "killer_ship_type_id": None,
        "killer_corporation_id": "finalCorporationID",
        "killer_corporation_name": "finalCorp",
        "killer_alliance_id": None,
        "killer_alliance_name": "finalAlliance",
        "ccp_isk_lost": "iskLost",
        "ccp_isk_destroyed": "iskDestroyed",
        "zkb_isk_lost": None,
        "zkb_isk_destroyed": None,
    },
    MER_KILL_SCHEMA_SNAKE_HASH: {
        "kill_datetime": "kill_datetime",
        "solar_system_id": "solarsystem_id",
        "solar_system_name": "solarsystem_name",
        "region_id": "region_id",
        "region_name": "region_name",
        "victim_ship_type_id": "victim_ship_type_id",
        "victim_ship_type_name": "victim_ship_type_name",
        "victim_ship_group_name": "victim_ship_group_name",
        "victim_corporation_id": "victim_corporation_id",
        "victim_corporation_name": "victim_corporation_name",
        "victim_alliance_id": None,
        "victim_alliance_name": "victim_alliance_name",
        "killer_ship_type_id": None,
        "killer_corporation_id": "killer_corporation_id",
        "killer_corporation_name": "killer_corporation_name",
        "killer_alliance_id": None,
        "killer_alliance_name": "killer_alliance_name",
        "ccp_isk_lost": "isk_lost",
        "ccp_isk_destroyed": "isk_destroyed",
        "zkb_isk_lost": None,
        "zkb_isk_destroyed": None,
    },
    MER_KILL_SCHEMA_14_HASH: {
        "kill_datetime": "kill_datetime",
        "solar_system_id": "kill_solarsystem_id",
        "solar_system_name": None,
        "region_id": None,
        "region_name": None,
        "victim_ship_type_id": "victim_ship_type_id",
        "victim_ship_type_name": None,
        "victim_ship_group_name": None,
        "victim_corporation_id": "victim_corporation_id",
        "victim_corporation_name": None,
        "victim_alliance_id": "victim_alliance_id",
        "victim_alliance_name": None,
        "killer_ship_type_id": "killer_ship_type_id",
        "killer_corporation_id": "killer_corporation_id",
        "killer_corporation_name": None,
        "killer_alliance_id": "killer_alliance_id",
        "killer_alliance_name": None,
        "ccp_isk_lost": "ccp_isk_lost",
        "ccp_isk_destroyed": "ccp_isk_destroyed",
        "zkb_isk_lost": "zkb_isk_lost",
        "zkb_isk_destroyed": "zkb_isk_destroyed",
    },
}

MER_KILL_TARGET_COLUMNS = (
    "kill_datetime",
    "solar_system_id",
    "solar_system_name",
    "region_id",
    "region_name",
    "victim_ship_type_id",
    "victim_ship_type_name",
    "victim_ship_group_name",
    "victim_corporation_id",
    "victim_corporation_name",
    "victim_alliance_id",
    "victim_alliance_name",
    "killer_ship_type_id",
    "killer_corporation_id",
    "killer_corporation_name",
    "killer_alliance_id",
    "killer_alliance_name",
    "ccp_isk_lost",
    "ccp_isk_destroyed",
    "zkb_isk_lost",
    "zkb_isk_destroyed",
)

MER_KILL_ID_COLUMNS = {
    "solar_system_id",
    "region_id",
    "victim_ship_type_id",
    "victim_corporation_id",
    "victim_alliance_id",
    "killer_ship_type_id",
    "killer_corporation_id",
    "killer_alliance_id",
}

MER_KILL_NUMERIC_COLUMNS = {
    "ccp_isk_lost",
    "ccp_isk_destroyed",
    "zkb_isk_lost",
    "zkb_isk_destroyed",
}


def _mer_data_table(table_name):
    return f'{MER_DATA_SCHEMA}.{table_name}'


def _quote_ident(value):
    return '"' + str(value).replace('"', '""') + '"'


def _sha256_file(path):
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _ensure_mer_kill_tables(conn):
    """
    Crée uniquement le stockage MER kill.

    mer.killmails est partitionnée par kill_datetime, comme rawkm.killmails.
    Les partitions mensuelles sont créées à la demande au moment de l'import.
    """
    with conn.cursor() as cur:
        cur.execute(f"CREATE SCHEMA IF NOT EXISTS {MER_DATA_SCHEMA};")

        cur.execute(f"""
            CREATE TABLE IF NOT EXISTS {_mer_data_table(MER_KILL_TABLE)} (
                source_month DATE NOT NULL,
                source_row BIGINT NOT NULL,
                kill_datetime TIMESTAMPTZ NOT NULL,
                solar_system_id BIGINT,
                solar_system_name TEXT,
                region_id BIGINT,
                region_name TEXT,
                victim_ship_type_id BIGINT,
                victim_ship_type_name TEXT,
                victim_ship_group_name TEXT,
                victim_corporation_id BIGINT,
                victim_corporation_name TEXT,
                victim_alliance_id BIGINT,
                victim_alliance_name TEXT,
                killer_ship_type_id BIGINT,
                killer_corporation_id BIGINT,
                killer_corporation_name TEXT,
                killer_alliance_id BIGINT,
                killer_alliance_name TEXT,
                resolved_km BIGINT[],
                resolved_km_ambiguous BOOLEAN NOT NULL DEFAULT FALSE,
                ccp_isk_lost NUMERIC,
                ccp_isk_destroyed NUMERIC,
                zkb_isk_lost NUMERIC,
                zkb_isk_destroyed NUMERIC,
                PRIMARY KEY (kill_datetime, source_month, source_row)
            ) PARTITION BY RANGE (kill_datetime);
        """)

        # MER -> killmail(s) existant(s).
        cur.execute(f"""
            ALTER TABLE {_mer_data_table(MER_KILL_TABLE)}
            ADD COLUMN IF NOT EXISTS resolved_km BIGINT[];
        """)

        cur.execute(f"""
            ALTER TABLE {_mer_data_table(MER_KILL_TABLE)}
            ADD COLUMN IF NOT EXISTS resolved_km_ambiguous
                BOOLEAN NOT NULL DEFAULT FALSE;
        """)

        # Si une ancienne colonne homonyme existe avec le mauvais type,
        # on la remet au type final attendu. Aucun ancien résultat n'est conservé.
        cur.execute(f"""
            DO $$
            BEGIN
                IF EXISTS (
                    SELECT 1
                    FROM information_schema.columns
                    WHERE table_schema = '{MER_DATA_SCHEMA}'
                      AND table_name = '{MER_KILL_TABLE}'
                      AND column_name = 'resolved_km'
                      AND data_type <> 'ARRAY'
                ) THEN
                    ALTER TABLE {_mer_data_table(MER_KILL_TABLE)}
                    ALTER COLUMN resolved_km TYPE BIGINT[]
                    USING NULL::BIGINT[];
                END IF;
            END
            $$;
        """)

        cur.execute(f"""
            CREATE TABLE IF NOT EXISTS {_mer_data_table(MER_KILL_IMPORT_TABLE)} (
                month DATE PRIMARY KEY,
                source_path TEXT NOT NULL,
                source_size_bytes BIGINT NOT NULL,
                source_sha256 TEXT NOT NULL,
                source_schema_hash TEXT NOT NULL,
                status TEXT NOT NULL,
                rows_imported BIGINT NOT NULL DEFAULT 0,
                started_at TIMESTAMPTZ,
                finished_at TIMESTAMPTZ,
                error TEXT,
                updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
            );
        """)

        # Indexes partitionnés : PostgreSQL crée/attache les index enfants
        # pour chaque partition mensuelle.
        cur.execute(f"""
            CREATE INDEX IF NOT EXISTS mer_killmails_match_strict_idx
            ON {_mer_data_table(MER_KILL_TABLE)}
                (
                    kill_datetime,
                    solar_system_id,
                    victim_ship_type_id,
                    victim_corporation_id,
                    killer_corporation_id
                );
        """)
        cur.execute(f"""
            CREATE INDEX IF NOT EXISTS mer_killmails_victim_corp_idx
            ON {_mer_data_table(MER_KILL_TABLE)} (victim_corporation_id);
        """)
        cur.execute(f"""
            CREATE INDEX IF NOT EXISTS mer_killmails_victim_alliance_idx
            ON {_mer_data_table(MER_KILL_TABLE)} (victim_alliance_id);
        """)
        cur.execute(f"""
            CREATE INDEX IF NOT EXISTS mer_killmails_killer_corp_idx
            ON {_mer_data_table(MER_KILL_TABLE)} (killer_corporation_id);
        """)
        cur.execute(f"""
            CREATE INDEX IF NOT EXISTS mer_killmails_killer_alliance_idx
            ON {_mer_data_table(MER_KILL_TABLE)} (killer_alliance_id);
        """)
        cur.execute(f"""
            CREATE INDEX IF NOT EXISTS mer_kill_dump_imports_status_idx
            ON {_mer_data_table(MER_KILL_IMPORT_TABLE)} (status, month);
        """)


def _ensure_mer_kill_partition(conn, month):
    month = month.replace(day=1)
    month_next = _next_month(month)
    partition_name = f"killmails_{month.year}_{month.month:02d}"

    with conn.cursor() as cur:
        cur.execute(f"""
            CREATE TABLE IF NOT EXISTS {MER_DATA_SCHEMA}.{partition_name}
            PARTITION OF {_mer_data_table(MER_KILL_TABLE)}
            FOR VALUES FROM (%s) TO (%s);
        """, (month.isoformat(), month_next.isoformat()))


def _catalog_rows_for_kill_import(month=None):
    with db() as conn:
        with conn.cursor() as cur:
            if month is None:
                cur.execute(f"""
                    SELECT
                        month,
                        kill_dump_path,
                        kill_dump_schema_hash,
                        kill_dump_columns
                    FROM {t('mer_catalog')}
                    WHERE kill_dump_state = 'present'
                      AND kill_dump_path IS NOT NULL
                    ORDER BY month ASC;
                """)
            else:
                cur.execute(f"""
                    SELECT
                        month,
                        kill_dump_path,
                        kill_dump_schema_hash,
                        kill_dump_columns
                    FROM {t('mer_catalog')}
                    WHERE month = %s
                      AND kill_dump_state = 'present'
                      AND kill_dump_path IS NOT NULL;
                """, (month,))

            rows = cur.fetchall()

    return [
        {
            "month": row[0],
            "kill_dump_path": row[1],
            "kill_dump_schema_hash": row[2],
            "kill_dump_columns": row[3] or [],
        }
        for row in rows
    ]


def _text_file_decodes(path, encoding):
    decoder = __import__("codecs").getincrementaldecoder(encoding)(errors="strict")

    try:
        with path.open("rb") as handle:
            while True:
                chunk = handle.read(1024 * 1024)
                if not chunk:
                    break
                decoder.decode(chunk, final=False)
            decoder.decode(b"", final=True)
        return True
    except UnicodeDecodeError:
        return False


def _detect_kill_dump_encoding(path):
    # MER récents : UTF-8. Plusieurs anciens dumps CCP sont Windows-1252.
    # Validation complète en streaming pour éviter de découvrir un octet invalide
    # au milieu d'un COPY massif. Aucun errors=replace : on préserve les noms.
    if _text_file_decodes(path, "utf-8-sig"):
        return "utf-8-sig"
    if _text_file_decodes(path, "cp1252"):
        return "cp1252"
    return "latin-1"


def _read_kill_dump_header(path, encoding):
    with path.open("r", encoding=encoding, newline="") as handle:
        reader = csv.reader(handle)
        header = next(reader, None)

    if not header:
        raise RuntimeError(f"Empty MER kill dump: {path}")

    header = [str(value).strip() for value in header]

    if any(not value for value in header):
        raise RuntimeError(f"MER kill dump has an empty column name: {path}")

    if len(header) != len(set(header)):
        raise RuntimeError(f"MER kill dump has duplicate columns: {path}")

    return header


def _stage_text_expr(source_column):
    if source_column is None:
        return "NULL"
    return f"NULLIF(BTRIM({_quote_ident(source_column)}), '')"


def _stage_id_expr(source_column):
    if source_column is None:
        return "NULL"

    col = _quote_ident(source_column)
    return (
        f"CASE WHEN BTRIM({col}) ~ '^[0-9]+$' "
        f"AND BTRIM({col})::NUMERIC > 0 "
        f"THEN BTRIM({col})::BIGINT ELSE NULL END"
    )


def _stage_numeric_expr(source_column):
    if source_column is None:
        return "NULL"

    col = _quote_ident(source_column)
    return (
        f"CASE WHEN BTRIM({col}) ~ '^[+-]?[0-9]+([.][0-9]+)?$' "
        f"THEN BTRIM({col})::NUMERIC ELSE NULL END"
    )


def _stage_datetime_expr(source_column):
    if source_column is None:
        raise RuntimeError("MER schema has no kill datetime column")

    col = _quote_ident(source_column)
    # Les kill times MER sont publiés en UTC sans suffixe de timezone.
    return f"(NULLIF(BTRIM({col}), '')::TIMESTAMP AT TIME ZONE 'UTC')"


def _normalized_stage_select(mapping):
    expressions = []

    for target_column in MER_KILL_TARGET_COLUMNS:
        source_column = mapping.get(target_column)

        if target_column == "kill_datetime":
            expression = _stage_datetime_expr(source_column)
        elif target_column in MER_KILL_ID_COLUMNS:
            expression = _stage_id_expr(source_column)
        elif target_column in MER_KILL_NUMERIC_COLUMNS:
            expression = _stage_numeric_expr(source_column)
        else:
            expression = _stage_text_expr(source_column)

        expressions.append(f"{expression} AS {_quote_ident(target_column)}")

    return ",\n                    ".join(expressions)


def _get_kill_import_state(conn, month):
    with conn.cursor() as cur:
        cur.execute(f"""
            SELECT status, source_sha256
            FROM {_mer_data_table(MER_KILL_IMPORT_TABLE)}
            WHERE month = %s;
        """, (month,))
        return cur.fetchone()


def _mark_kill_import_running(
    conn,
    *,
    month,
    source_path,
    source_size_bytes,
    source_sha256,
    source_schema_hash,
):
    with conn.cursor() as cur:
        cur.execute(f"""
            INSERT INTO {_mer_data_table(MER_KILL_IMPORT_TABLE)} (
                month,
                source_path,
                source_size_bytes,
                source_sha256,
                source_schema_hash,
                status,
                rows_imported,
                started_at,
                finished_at,
                error,
                updated_at
            )
            VALUES (%s,%s,%s,%s,%s,'running',0,NOW(),NULL,NULL,NOW())
            ON CONFLICT (month)
            DO UPDATE SET
                source_path = EXCLUDED.source_path,
                source_size_bytes = EXCLUDED.source_size_bytes,
                source_sha256 = EXCLUDED.source_sha256,
                source_schema_hash = EXCLUDED.source_schema_hash,
                status = 'running',
                rows_imported = 0,
                started_at = NOW(),
                finished_at = NULL,
                error = NULL,
                updated_at = NOW();
        """, (
            month,
            str(source_path),
            source_size_bytes,
            source_sha256,
            source_schema_hash,
        ))
    conn.commit()


def _mark_kill_import_failed(conn, month, error):
    with conn.cursor() as cur:
        cur.execute(f"""
            UPDATE {_mer_data_table(MER_KILL_IMPORT_TABLE)}
            SET
                status = 'failed',
                finished_at = NOW(),
                error = %s,
                updated_at = NOW()
            WHERE month = %s;
        """, (str(error), month))
    conn.commit()


def _import_mer_kill_dump_row(row):
    month = row["month"]
    source_path = Path(row["kill_dump_path"])

    if not source_path.is_file():
        raise RuntimeError(f"MER kill dump missing on disk: {source_path}")

    source_encoding = _detect_kill_dump_encoding(source_path)
    header = _read_kill_dump_header(source_path, source_encoding)
    actual_schema_hash = _schema_hash(header)
    catalog_schema_hash = row.get("kill_dump_schema_hash")

    mapping = MER_KILL_SCHEMA_MAPPINGS.get(actual_schema_hash)
    if mapping is None:
        raise RuntimeError(
            f"Unsupported MER kill schema {actual_schema_hash} "
            f"for {month:%Y-%m}"
        )

    if catalog_schema_hash and catalog_schema_hash != actual_schema_hash:
        raise RuntimeError(
            f"MER kill schema changed since analysis for {month:%Y-%m}: "
            f"catalog={catalog_schema_hash} file={actual_schema_hash}"
        )

    source_size_bytes = source_path.stat().st_size
    source_sha256 = _sha256_file(source_path)

    if source_encoding != "utf-8-sig":
        _mer_log(
            f"[KILL_IMPORT] {month:%Y-%m} source_encoding={source_encoding}"
        )

    with db() as conn:
        _ensure_mer_kill_tables(conn)
        conn.commit()

        state = _get_kill_import_state(conn, month)
        if state and state[0] == "success":
            if state[1] == source_sha256:
                return {
                    "state": "skipped",
                    "rows_imported": 0,
                    "source_sha256": source_sha256,
                }

            # Un mois déjà importé n'est jamais réinjecté automatiquement.
            # Cela évite tout doublon si CCP remplace le fichier a posteriori.
            return {
                "state": "changed",
                "rows_imported": 0,
                "source_sha256": source_sha256,
            }

        _mark_kill_import_running(
            conn,
            month=month,
            source_path=source_path,
            source_size_bytes=source_size_bytes,
            source_sha256=source_sha256,
            source_schema_hash=actual_schema_hash,
        )

        try:
            stage_columns_sql = ",\n                ".join(
                f"{_quote_ident(column)} TEXT"
                for column in header
            )

            with conn.cursor() as cur:
                cur.execute("DROP TABLE IF EXISTS mer_kill_stage;")
                cur.execute(f"""
                    CREATE TEMP TABLE mer_kill_stage (
                        source_row BIGINT GENERATED ALWAYS AS IDENTITY,
                        {stage_columns_sql}
                    ) ON COMMIT DROP;
                """)

                copy_columns = ", ".join(
                    _quote_ident(column)
                    for column in header
                )

                with source_path.open(
                    "r",
                    encoding=source_encoding,
                    newline="",
                ) as source_handle:
                    cur.copy_expert(
                        f"""
                        COPY mer_kill_stage ({copy_columns})
                        FROM STDIN
                        WITH (FORMAT CSV, HEADER TRUE)
                        """,
                        source_handle,
                    )

                cur.execute("SELECT COUNT(*) FROM mer_kill_stage;")
                source_rows = int(cur.fetchone()[0] or 0)

                datetime_expression = _stage_datetime_expr(
                    mapping["kill_datetime"]
                )

                cur.execute(f"""
                    SELECT COUNT(*)
                    FROM mer_kill_stage
                    WHERE NULLIF(
                        BTRIM({_quote_ident(mapping['kill_datetime'])}),
                        ''
                    ) IS NULL;
                """)
                missing_datetime = int(cur.fetchone()[0] or 0)
                if missing_datetime:
                    raise RuntimeError(
                        f"{month:%Y-%m}: {missing_datetime} rows without kill datetime"
                    )

                # Création des partitions réellement nécessaires d'après les
                # timestamps du fichier, pas seulement d'après le mois MER.
                cur.execute(f"""
                    SELECT DISTINCT
                        date_trunc('month', {datetime_expression})::DATE
                    FROM mer_kill_stage
                    ORDER BY 1;
                """)
                partition_months = [
                    partition_row[0]
                    for partition_row in cur.fetchall()
                    if partition_row[0] is not None
                ]

            for partition_month in partition_months:
                _ensure_mer_kill_partition(conn, partition_month)

            normalized_select = _normalized_stage_select(mapping)

            with conn.cursor() as cur:
                cur.execute(f"""
                    INSERT INTO {_mer_data_table(MER_KILL_TABLE)} (
                        source_month,
                        source_row,
                        {', '.join(MER_KILL_TARGET_COLUMNS)}
                    )
                    SELECT
                        %s::DATE AS source_month,
                        source_row,
                        {normalized_select}
                    FROM mer_kill_stage;
                """, (month,))
                inserted_rows = int(cur.rowcount or 0)

                if inserted_rows != source_rows:
                    raise RuntimeError(
                        f"{month:%Y-%m}: source_rows={source_rows} "
                        f"inserted_rows={inserted_rows}"
                    )

                cur.execute(f"""
                    UPDATE {_mer_data_table(MER_KILL_IMPORT_TABLE)}
                    SET
                        status = 'success',
                        rows_imported = %s,
                        finished_at = NOW(),
                        error = NULL,
                        updated_at = NOW()
                    WHERE month = %s;
                """, (inserted_rows, month))

            conn.commit()

            return {
                "state": "imported",
                "rows_imported": inserted_rows,
                "source_sha256": source_sha256,
            }

        except Exception as exc:
            conn.rollback()
            _mark_kill_import_failed(conn, month, exc)
            raise


def import_mer_kill_dumps(month=None):
    """
    Importe les kill dumps MER analysés dans mer.killmails.

    - une seule table normalisée superset ;
    - NULL quand un schéma source ne fournit pas le champ ;
    - bounty/faction volontairement ignorés ;
    - partitionnement mensuel par kill_datetime ;
    - COPY PostgreSQL pour l'import massif ;
    - mer.kill_dump_imports mémorise les fichiers déjà traités ;
    - un fichier déjà importé avec le même SHA256 est SKIP ;
    - un fichier déjà importé mais modifié n'est jamais réimporté automatiquement.
    """
    if not _import_kills_lock.acquire(blocking=False):
        _mer_log("[KILL_IMPORT] BUSY")
        return {
            "busy": True,
            "processed": 0,
            "imported": 0,
            "skipped": 0,
            "changed": 0,
            "failed": 0,
            "rows_imported": 0,
        }

    try:
        rows = _catalog_rows_for_kill_import(month=month)

        stats = {
            "busy": False,
            "processed": 0,
            "imported": 0,
            "skipped": 0,
            "changed": 0,
            "failed": 0,
            "rows_imported": 0,
        }

        _mer_log(
            f"[KILL_IMPORT] START target="
            f"{month.strftime('%Y-%m') if month else 'ALL'} "
            f"files={len(rows)}"
        )

        # Crée les tables même si aucun dump n'est encore importable.
        with db() as conn:
            _ensure_mer_kill_tables(conn)
            conn.commit()

        for row in rows:
            row_month = row["month"]
            stats["processed"] += 1

            try:
                result = _import_mer_kill_dump_row(row)
                state = result["state"]

                if state == "imported":
                    stats["imported"] += 1
                    stats["rows_imported"] += result["rows_imported"]
                elif state == "skipped":
                    stats["skipped"] += 1
                elif state == "changed":
                    stats["changed"] += 1

                _mer_log(
                    f"[KILL_IMPORT] {row_month:%Y-%m} "
                    f"state={state} "
                    f"rows={result.get('rows_imported', 0)}"
                )

            except Exception as exc:
                stats["failed"] += 1
                _mer_log(
                    f"[KILL_IMPORT] {row_month:%Y-%m} "
                    f"FAILED={exc}"
                )

        _mer_log(
            f"[KILL_IMPORT] END processed={stats['processed']} "
            f"imported={stats['imported']} "
            f"skipped={stats['skipped']} "
            f"changed={stats['changed']} "
            f"failed={stats['failed']} "
            f"rows={stats['rows_imported']}"
        )

        return stats

    finally:
        _import_kills_lock.release()


# ============================================================
# MER ALLIANCE IDS FROM EXISTING LOCAL CORPORATION HISTORY
# ============================================================

def enrich_mer_entity_ids():
    """
    Pour toutes les lignes de mer.killmails :

      victim_corporation_id + kill_datetime
          -> entities.corporation_alliance_history
          -> victim_alliance_id

      killer_corporation_id + kill_datetime
          -> entities.corporation_alliance_history
          -> killer_alliance_id

    Lecture seule de l'historique local existant.
    Aucune API.
    Aucun refresh.
    Aucune écriture hors de mer.killmails.
    """
    if not _map_alliance_ids_lock.acquire(blocking=False):
        _mer_log("[ALLIANCE_HISTORY] BUSY")
        return {"busy": True}

    try:
        _mer_log("[ALLIANCE_HISTORY] START")

        with db() as conn:
            with conn.cursor() as cur:
                cur.execute(f"""
                    UPDATE {_mer_data_table(MER_KILL_TABLE)} AS m
                    SET
                        victim_alliance_id = (
                            SELECT h.alliance_id
                            FROM entities.corporation_alliance_history h
                            WHERE h.corporation_id = m.victim_corporation_id
                              AND h.start_date <= m.kill_datetime
                              AND (
                                  h.end_date IS NULL
                                  OR m.kill_datetime < h.end_date
                              )
                            ORDER BY h.start_date DESC, h.record_id DESC
                            LIMIT 1
                        ),
                        killer_alliance_id = (
                            SELECT h.alliance_id
                            FROM entities.corporation_alliance_history h
                            WHERE h.corporation_id = m.killer_corporation_id
                              AND h.start_date <= m.kill_datetime
                              AND (
                                  h.end_date IS NULL
                                  OR m.kill_datetime < h.end_date
                              )
                            ORDER BY h.start_date DESC, h.record_id DESC
                            LIMIT 1
                        );
                """)
                rows = int(cur.rowcount or 0)

            conn.commit()

        _mer_log(f"[ALLIANCE_HISTORY] END rows={rows}")

        return {
            "busy": False,
            "rows": rows,
        }

    except Exception as exc:
        _mer_log(f"[ALLIANCE_HISTORY] FAILED={exc}")
        raise

    finally:
        _map_alliance_ids_lock.release()


# ============================================================
# MER -> EXISTING RAW KILLMAIL MATCHING
# ============================================================

def match_mer_killmails():
    """
    Matching strict incrémental MER -> rawkm.

    Les lignes avec resolved_km déjà renseigné sont ignorées.
    Les raw killmail IDs déjà utilisés par ces lignes sont réservés et ne
    peuvent pas être attribués à une nouvelle ligne MER.

    La clé de matching est directement composée de TOUS les critères :
      - kill_datetime exact à la seconde
      - solar_system_id
      - victim_ship_type_id
      - victim_corporation_id
      - killer_corporation_id du final_blow

    Il n'y a plus de logique "clé principale puis tie-break".

    Règle 1 -> 1 :
      un raw killmail ne peut être considéré comme un match unique que s'il
      n'est candidat que pour UNE seule ligne MER.

    Si un raw killmail est candidat pour plusieurs lignes MER, toutes les
    lignes MER concernées sont marquées ambiguës.

    resolved_km :
      NULL       -> aucun candidat
      {id}       -> un candidat
      {id,id...} -> plusieurs candidats

    resolved_km_ambiguous :
      FALSE -> le rattachement est réellement unique 1 -> 1
      TRUE  -> plusieurs candidats pour la ligne MER, ou au moins un raw
               killmail candidat est partagé par plusieurs lignes MER

    Lecture : rawkm.*
    Ecriture : mer.killmails.resolved_km et resolved_km_ambiguous uniquement.
    Aucun appel API / HTTP.
    """
    if not _match_killmails_lock.acquire(blocking=False):
        _mer_log("[KILL_MATCH] BUSY")
        return {"busy": True}

    try:
        _mer_log("[KILL_MATCH] START STRICT_1TO1 INCREMENTAL")

        with db() as conn:
            _ensure_mer_kill_tables(conn)
            conn.commit()

            with conn.cursor() as cur:
                cur.execute("""
                    SELECT child.relname
                    FROM pg_inherits i
                    JOIN pg_class parent
                      ON parent.oid = i.inhparent
                    JOIN pg_namespace parent_ns
                      ON parent_ns.oid = parent.relnamespace
                    JOIN pg_class child
                      ON child.oid = i.inhrelid
                    JOIN pg_namespace child_ns
                      ON child_ns.oid = child.relnamespace
                    WHERE parent_ns.nspname = %s
                      AND parent.relname = %s
                      AND child_ns.nspname = %s
                    ORDER BY child.relname;
                """, (
                    MER_DATA_SCHEMA,
                    MER_KILL_TABLE,
                    MER_DATA_SCHEMA,
                ))
                partitions = [str(row[0]) for row in cur.fetchall()]

            totals = {
                "rows": 0,
                "matched_unique": 0,
                "ambiguous": 0,
                "unresolved": 0,
                "shared_raw_mer_rows": 0,
            }

            # Les rattachements déjà trouvés ne sont jamais rejoués.
            # Leurs raw killmail IDs sont réservés afin qu'un nouveau MER
            # ne puisse pas reprendre un kill déjà rattaché.
            with conn.cursor() as cur:
                cur.execute(f"""
                    CREATE TEMP TABLE tmp_claimed_raw_kills
                    ON COMMIT PRESERVE ROWS
                    AS
                    SELECT DISTINCT UNNEST(resolved_km) AS killmail_id
                    FROM {_mer_data_table(MER_KILL_TABLE)}
                    WHERE resolved_km IS NOT NULL;
                """)
                cur.execute("""
                    CREATE UNIQUE INDEX tmp_claimed_raw_kills_idx
                    ON tmp_claimed_raw_kills (killmail_id);
                """)
                cur.execute("ANALYZE tmp_claimed_raw_kills;")

            conn.commit()

            for partition_name in partitions:
                match = re.fullmatch(
                    rf"{re.escape(MER_KILL_TABLE)}_(\d{{4}})_(\d{{2}})",
                    partition_name,
                )
                if match is None:
                    raise RuntimeError(
                        f"Unexpected MER partition name: {partition_name}"
                    )

                year = match.group(1)
                month = match.group(2)

                partition_table = (
                    f"{MER_DATA_SCHEMA}.{_quote_ident(partition_name)}"
                )
                raw_km_name = f"killmails_{year}_{month}"
                raw_atk_name = f"killmail_attackers_{year}_{month}"
                raw_km_table = f"rawkm.{_quote_ident(raw_km_name)}"
                raw_atk_table = f"rawkm.{_quote_ident(raw_atk_name)}"

                # Les tables rawkm sont elles aussi mensuelles.
                with conn.cursor() as cur:
                    cur.execute(
                        """
                        SELECT
                            to_regclass(%s) IS NOT NULL,
                            to_regclass(%s) IS NOT NULL;
                        """,
                        (
                            f"rawkm.{raw_km_name}",
                            f"rawkm.{raw_atk_name}",
                        ),
                    )
                    raw_km_exists, raw_atk_exists = cur.fetchone()

                if not raw_km_exists or not raw_atk_exists:
                    with conn.cursor() as cur:
                        cur.execute(f"""
                            SELECT COUNT(*)
                            FROM {partition_table}
                            WHERE resolved_km IS NULL;
                        """)
                        rows = int(cur.fetchone()[0] or 0)

                    conn.commit()

                    totals["rows"] += rows
                    totals["unresolved"] += rows

                    _mer_log(
                        f"[KILL_MATCH] partition={partition_name} "
                        f"rows={rows} unique=0 ambiguous=0 "
                        f"shared_raw_mer_rows=0 unresolved={rows} "
                        f"raw_partition_missing=1"
                    )
                    continue

                with conn.cursor() as cur:
                    # Une ligne = une relation candidate MER <-> rawkm.
                    # Tous les critères sont obligatoires dès cette étape.
                    cur.execute(f"""
                        CREATE TEMP TABLE tmp_mer_kill_candidates
                        ON COMMIT DROP
                        AS
                        SELECT
                            m.kill_datetime,
                            m.source_month,
                            m.source_row,
                            km.killmail_id,
                            km.killmail_time
                        FROM {partition_table} AS m
                        JOIN {raw_km_table} AS km
                          ON km.killmail_time = m.kill_datetime
                         AND km.solar_system_id = m.solar_system_id
                         AND km.victim_ship_type_id = m.victim_ship_type_id
                         AND km.victim_corporation_id =
                             m.victim_corporation_id
                        WHERE m.resolved_km IS NULL
                          AND NOT EXISTS (
                            SELECT 1
                            FROM tmp_claimed_raw_kills AS claimed
                            WHERE claimed.killmail_id = km.killmail_id
                          )
                          AND EXISTS (
                            SELECT 1
                            FROM {raw_atk_table} AS a
                            WHERE a.killmail_id = km.killmail_id
                              AND a.killmail_time = km.killmail_time
                              AND a.final_blow IS TRUE
                              AND (
                                  a.corporation_id = m.killer_corporation_id
                                  OR (
                                      a.corporation_id IS NULL
                                      AND a.faction_id = m.killer_corporation_id
                                  )
                              )
                        );
                    """)

                    cur.execute("""
                        CREATE INDEX tmp_mer_kill_candidates_mer_idx
                        ON tmp_mer_kill_candidates
                            (kill_datetime, source_month, source_row);
                    """)

                    cur.execute("""
                        CREATE INDEX tmp_mer_kill_candidates_raw_idx
                        ON tmp_mer_kill_candidates
                            (killmail_id, killmail_time);
                    """)

                    cur.execute("ANALYZE tmp_mer_kill_candidates;")

                    # raw_claimers = nombre de lignes MER qui revendiquent
                    # ce même killmail raw.
                    #
                    # Une ligne MER n'est réellement "unique" que si :
                    #   - elle a exactement 1 candidat
                    #   - ce candidat n'appartient qu'à elle.
                    cur.execute(f"""
                        WITH candidate_stats AS (
                            SELECT
                                c.kill_datetime,
                                c.source_month,
                                c.source_row,
                                c.killmail_id,
                                c.killmail_time,
                                COUNT(*) OVER (
                                    PARTITION BY
                                        c.killmail_id,
                                        c.killmail_time
                                ) AS raw_claimers
                            FROM tmp_mer_kill_candidates AS c
                        ),
                        per_mer AS (
                            SELECT
                                kill_datetime,
                                source_month,
                                source_row,
                                ARRAY_AGG(
                                    killmail_id
                                    ORDER BY killmail_id
                                ) AS candidate_ids,
                                COUNT(*) AS candidate_count,
                                BOOL_OR(raw_claimers > 1) AS has_shared_raw
                            FROM candidate_stats
                            GROUP BY
                                kill_datetime,
                                source_month,
                                source_row
                        )
                        UPDATE {partition_table} AS m
                        SET
                            resolved_km = p.candidate_ids,
                            resolved_km_ambiguous = (
                                p.candidate_count > 1
                                OR p.has_shared_raw
                            )
                        FROM per_mer AS p
                        WHERE m.kill_datetime = p.kill_datetime
                          AND m.source_month = p.source_month
                          AND m.source_row = p.source_row;
                    """)

                    # Stats finales basées sur la vraie règle 1 -> 1.
                    cur.execute(f"""
                        WITH shared_mer AS (
                            SELECT DISTINCT
                                c.kill_datetime,
                                c.source_month,
                                c.source_row
                            FROM tmp_mer_kill_candidates AS c
                            JOIN (
                                SELECT
                                    killmail_id,
                                    killmail_time
                                FROM tmp_mer_kill_candidates
                                GROUP BY
                                    killmail_id,
                                    killmail_time
                                HAVING COUNT(*) > 1
                            ) AS shared
                              ON shared.killmail_id = c.killmail_id
                             AND shared.killmail_time = c.killmail_time
                        ),
                        touched AS (
                            SELECT
                                m.resolved_km,
                                m.resolved_km_ambiguous
                            FROM {partition_table} AS m
                            WHERE m.resolved_km IS NULL
                               OR EXISTS (
                                    SELECT 1
                                    FROM tmp_mer_kill_candidates AS c
                                    WHERE c.kill_datetime = m.kill_datetime
                                      AND c.source_month = m.source_month
                                      AND c.source_row = m.source_row
                               )
                        )
                        SELECT
                            COUNT(*) AS rows_total,
                            COUNT(*) FILTER (
                                WHERE resolved_km IS NOT NULL
                                  AND CARDINALITY(resolved_km) = 1
                                  AND resolved_km_ambiguous IS FALSE
                            ) AS matched_unique,
                            COUNT(*) FILTER (
                                WHERE resolved_km_ambiguous IS TRUE
                            ) AS ambiguous,
                            COUNT(*) FILTER (
                                WHERE resolved_km IS NULL
                            ) AS unresolved,
                            (
                                SELECT COUNT(*)
                                FROM shared_mer
                            ) AS shared_raw_mer_rows
                        FROM touched;
                    """)

                    row = cur.fetchone()

                    cur.execute("""
                        INSERT INTO tmp_claimed_raw_kills (killmail_id)
                        SELECT DISTINCT killmail_id
                        FROM tmp_mer_kill_candidates
                        ON CONFLICT (killmail_id) DO NOTHING;
                    """)

                conn.commit()

                rows = int(row[0] or 0)
                unique = int(row[1] or 0)
                ambiguous = int(row[2] or 0)
                unresolved = int(row[3] or 0)
                shared_raw_mer_rows = int(row[4] or 0)

                totals["rows"] += rows
                totals["matched_unique"] += unique
                totals["ambiguous"] += ambiguous
                totals["unresolved"] += unresolved
                totals["shared_raw_mer_rows"] += shared_raw_mer_rows

                _mer_log(
                    f"[KILL_MATCH] partition={partition_name} "
                    f"rows={rows} "
                    f"unique={unique} "
                    f"ambiguous={ambiguous} "
                    f"shared_raw_mer_rows={shared_raw_mer_rows} "
                    f"unresolved={unresolved}"
                )

        _mer_log(
            f"[KILL_MATCH] END STRICT_1TO1 "
            f"rows={totals['rows']} "
            f"unique={totals['matched_unique']} "
            f"ambiguous={totals['ambiguous']} "
            f"shared_raw_mer_rows={totals['shared_raw_mer_rows']} "
            f"unresolved={totals['unresolved']}"
        )

        return {
            "busy": False,
            **totals,
        }

    except Exception as exc:
        _mer_log(f"[KILL_MATCH] FAILED={exc}")
        raise

    finally:
        _match_killmails_lock.release()

