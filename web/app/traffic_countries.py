"""Local country lookup. Only ISO country codes leave this module, never IPs."""
from datetime import datetime, timedelta, timezone
import fcntl
import gzip
import ipaddress
import json
import os
from pathlib import Path
import re
import tempfile
import threading
import time
from urllib.error import HTTPError
from urllib.request import Request, urlopen

ROOT = Path(__file__).resolve().parents[2] / 'data' / 'public_traffic' / 'countries'
_lock = threading.RLock()
_reader = None
_signature = None


def status():
    try:
        value = json.loads((ROOT / 'status.json').read_text())
        return {key:value.get(key) for key in ('state','updated','month','bytes','error')}
    except (OSError, ValueError, AttributeError):
        return {'state':'missing'}


def _state(value):
    fd, name = tempfile.mkstemp(prefix='.status-', dir=ROOT)
    try:
        with os.fdopen(fd,'w') as out:
            json.dump(value,out)
        os.replace(name,ROOT/'status.json')
    finally:
        Path(name).unlink(missing_ok=True)


def country(address):
    """Use the ASGI client IP (Uvicorn's trusted proxy handling), not raw headers."""
    global _reader, _signature
    try:
        ip = ipaddress.ip_address(address)
        if not ip.is_global:
            return 'ZZ'
        path = ROOT/'country.mmdb'
        stat = path.stat()
        signature = (str(path),stat.st_ino,stat.st_mtime_ns)
        with _lock:
            if signature != _signature:
                import maxminddb
                new_reader = maxminddb.open_database(str(path))
                if _reader is not None:
                    _reader.close()
                _reader, _signature = new_reader, signature
            record = _reader.get(str(ip)) or {}
            code = record.get('country',{}).get('iso_code','ZZ')
        return code if isinstance(code,str) and re.fullmatch('[A-Z]{2}',code) else 'ZZ'
    except Exception:
        return 'ZZ'


def ensure_database(force=False):
    """Admin-only background download; monthly, bounded, atomic and cross-worker locked.

    Downloads a static DB-IP Lite file. No visitor address is ever sent out.
    A failed refresh preserves the last working database. No network on lookups.
    """
    try:
        ROOT.mkdir(parents=True,exist_ok=True,mode=0o700)
        with (ROOT/'download.lock').open('a') as lock:
            try:
                fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
            except BlockingIOError:
                return
            now = datetime.now(timezone.utc)
            current = now.strftime('%Y-%m')
            previous = status()
            if not force:
                if (ROOT/'country.mmdb').exists() and previous.get('month') == current:
                    return
                if time.time()-float(previous.get('updated') or 0)<3600:
                    return
            _state(dict(state='downloading',updated=time.time()))
            temporary = None
            try:
                import maxminddb
                # During monthly publication, the preceding release is still useful.
                months = [current,(now.replace(day=1)-timedelta(days=1)).strftime('%Y-%m')]
                for month in months:
                    url = f'https://download.db-ip.com/free/dbip-country-lite-{month}.mmdb.gz'
                    try:
                        response = urlopen(Request(url,headers={'User-Agent':'EVEOSINT country database updater'}),timeout=20)
                    except HTTPError as exc:
                        if exc.code == 404:
                            continue
                        raise
                    with response:
                        # Only the static dataset is downloaded; no visitor data in URL/headers.
                        compressed = response.read(16*1024*1024+1)
                    if len(compressed)>16*1024*1024:
                        raise ValueError('Download too large')
                    import io
                    with gzip.GzipFile(fileobj=io.BytesIO(compressed)) as archive:
                        body = archive.read(32*1024*1024+1)
                    if len(body)>32*1024*1024:
                        raise ValueError('Database too large')
                    fd,temporary = tempfile.mkstemp(prefix='.country-',suffix='.mmdb',dir=ROOT)
                    with os.fdopen(fd,'wb') as out:
                        out.write(body)
                    # Validate before replacing the last known working database.
                    with maxminddb.open_database(temporary) as database:
                        if database.metadata().ip_version not in (4,6):
                            raise ValueError('Invalid database')
                        database.get('8.8.8.8')
                    os.replace(temporary,ROOT/'country.mmdb')
                    temporary = None
                    _state(dict(state='ready',month=month,updated=time.time(),bytes=len(body)))
                    return
                raise ValueError('No country database available')
            except Exception:
                # Deliberately exclude exception strings/addresses from stored status.
                _state(dict(state='failed',updated=time.time(),error='Country database download failed. Retry from Admin.',
                            month=previous.get('month'),bytes=previous.get('bytes')))
            finally:
                if temporary:
                    Path(temporary).unlink(missing_ok=True)
    except OSError:
        return  # Geolocation never blocks serving/recording traffic.
