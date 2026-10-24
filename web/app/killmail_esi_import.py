"""On-demand single-killmail import from a validated CCP ESI capability URL."""
import importlib
import json
import re
import sys
from contextlib import closing
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import requests
from .db import db

ROOT = Path(__file__).resolve().parents[2]
USER_AGENT = 'EVEOSINT-KillmailImport/1.0 (github.com/DictateurImperator/EVEOSINT)'
MAX_RESPONSE = 2 * 1024 * 1024


class ImportError(ValueError):
    pass


def parse_link(value):
    if not isinstance(value, str) or len(value) > 1024:
        raise ImportError('Lien ESI invalide.')
    try:
        u = urlsplit(value.strip())
        path = re.fullmatch(r'/(?:latest|legacy|dev|v[0-9]+)/killmails/([1-9][0-9]{0,18})/([a-fA-F0-9]{40})/?', u.path)
        if u.scheme != 'https' or u.netloc.lower() != 'esi.evetech.net' or u.fragment or not path:
            raise ValueError()
        query = parse_qs(u.query, keep_blank_values=True, strict_parsing=True)
        if set(query) - {'datasource'} or query.get('datasource', ['tranquility']) != ['tranquility']:
            raise ValueError()
        ident = int(path[1])
        if ident > 2**63 - 1:
            raise ValueError()
    except ValueError as exc:
        raise ImportError('Colle un lien HTTPS ESI complet contenant le numéro et le hash du killmail, sur Tranquility.') from exc
    return ident, path[2].lower()


def fetch_killmail(ident, kmhash):
    # Never fetch the user-supplied URL itself or follow redirects.
    url = f'https://esi.evetech.net/latest/killmails/{ident}/{kmhash}/'
    try:
        with requests.Session() as session:
            session.headers.update({'User-Agent': USER_AGENT, 'Accept': 'application/json'})
            from .esi_quota import wait_for_slot
            wait_for_slot()
            with session.get(url, params={'datasource': 'tranquility'}, timeout=(3, 8),
                             stream=True, allow_redirects=False) as response:
                if response.status_code == 404:
                    raise ImportError('Killmail introuvable : vérifie le numéro et le hash du lien ESI.')
                if response.status_code in (420, 429):
                    raise ImportError('ESI limite les requêtes. Réessaie plus tard.')
                if response.status_code != 200:
                    raise ImportError('ESI ne répond pas correctement. Aucun killmail importé.')
                body = bytearray()
                for chunk in response.iter_content(65536):
                    body.extend(chunk)
                    if len(body) > MAX_RESPONSE:
                        raise ImportError('Réponse ESI trop volumineuse.')
                payload = json.loads(body)
                modified = response.headers.get('Last-Modified')
    except (requests.RequestException, ValueError) as exc:
        if isinstance(exc, ImportError):
            raise
        raise ImportError('Impossible de récupérer un killmail valide depuis ESI.') from exc
    if not isinstance(payload, dict) or payload.get('killmail_id', ident) != ident:
        raise ImportError('La réponse ESI ne correspond pas au killmail demandé.')
    try:
        stamp = datetime.fromisoformat(payload['killmail_time'].replace('Z', '+00:00'))
        if stamp.tzinfo is None or stamp > datetime.now(timezone.utc):
            raise ValueError()
        victim = payload['victim']
        if not isinstance(victim, dict) or not isinstance(payload['attackers'], list):
            raise ValueError()
        if type(payload['solar_system_id']) is not int or payload['solar_system_id'] <= 0 or type(victim['ship_type_id']) is not int or victim['ship_type_id'] <= 0:
            raise ValueError()
    except (KeyError, ValueError, TypeError, AttributeError) as exc:
        raise ImportError('Réponse ESI incomplète ou date invalide.') from exc
    payload = dict(payload, killmail_id=ident, killmail_hash=kmhash)
    if modified:
        try:
            payload['http_last_modified'] = parsedate_to_datetime(modified)
        except (ValueError, TypeError):
            pass
    return payload, stamp


def importer_module():
    if str(ROOT) not in sys.path:
        sys.path.insert(0, str(ROOT))
    return importlib.import_module('scripts.sync_killmails')


def import_link(value):
    ident, kmhash = parse_link(value)
    payload, stamp = fetch_killmail(ident, kmhash)
    importer = importer_module()
    month = stamp.astimezone(timezone.utc).date().replace(day=1)
    with closing(db()) as conn:
        try:
            with conn.cursor() as cur:
                cur.execute("SET LOCAL statement_timeout='8s'; SET LOCAL lock_timeout='3s'")
                cur.execute("SELECT to_regclass('rawkm.killmails'),to_regclass('rawkm.killmail_attackers'),to_regclass('rawkm.killmail_items')")
                if not all(cur.fetchone()):
                    raise ImportError('Le stockage des killmails doit être initialisé depuis les jobs Admin.')
                cur.execute('SELECT to_regclass(%s),to_regclass(%s),to_regclass(%s)',
                            tuple('rawkm.' + importer.partition_name(name, month) for name in ('killmails', 'killmail_attackers', 'killmail_items')))
                has_partitions = all(cur.fetchone())
            if not has_partitions:
                importer.ensure_month_partitions(conn, month, month)
            with conn.cursor() as cur:
                cur.execute("SET LOCAL statement_timeout='8s'; SET LOCAL lock_timeout='3s'")
            added = importer.insert_killmail(conn, payload, skip_existing=True)
            if added:
                importer.mark_months_dirty(conn, [month])
            conn.commit()
        except Exception:
            conn.rollback()
            raise
    return {'status': 'imported' if added else 'existing', 'killmail_id': ident,
            'killmail_time': stamp.astimezone(timezone.utc).isoformat(),
            'victim_character_id': payload['victim'].get('character_id'),
            'victim_ship_type_id': payload['victim']['ship_type_id'],
            'attackers': len(payload['attackers']), 'url': f'/kill/{ident}'}
