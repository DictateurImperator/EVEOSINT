"""Transient abuse detection. Normal requests never persist an address here."""
from collections import OrderedDict
import hmac
import ipaddress
import secrets
import threading
import time


# Observed abusive crawler sources, verified against Google's published crawler ranges.
GOOGLE_IPS = ('192.178.4.161', '192.178.4.162', '192.178.4.163')
GOOGLE_AGENTS = ('googlebot', 'googleother', 'google-inspectiontool', 'storebot-google', 'google-extended')


def blocked_crawler(address, agent):
    try:
        ip = ipaddress.ip_address(address)
        address = str(ip.ipv4_mapped if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped else ip)
    except (ValueError, TypeError):
        address = ''
    return address in GOOGLE_IPS or any(token in agent.lower() for token in GOOGLE_AGENTS)


def crawler_config(content, addresses=GOOGLE_IPS):
    import re
    content = re.sub(r'(?ms)^    # BEGIN EVEOSINT crawler block\n.*?^    # END EVEOSINT crawler block\n', '', content)
    rules = r'''    # BEGIN EVEOSINT crawler block
    set $eveosint_block_crawler 0;
    if ($http_user_agent ~* "(Googlebot|GoogleOther|Google-InspectionTool|Storebot-Google|Google-Extended)") { set $eveosint_block_crawler 1; }
    if ($uri = /robots.txt) { set $eveosint_block_crawler 0; }
    if ($eveosint_block_crawler = 1) { return 403; }
    location = /robots.txt {
        default_type text/plain;
        return 200 "User-agent: Googlebot\nUser-agent: GoogleOther\nUser-agent: Google-InspectionTool\nUser-agent: Storebot-Google\nUser-agent: Google-Extended\nDisallow: /\n\nUser-agent: *\nDisallow:\n";
    }
    # END EVEOSINT crawler block
'''
    ip_rules = ''.join('    if ($remote_addr = "'+str(ipaddress.ip_address(ip))+'") { set $eveosint_block_crawler 1; }\n' for ip in sorted(set(addresses)))
    rules = rules.replace('    if ($uri = /robots.txt)', ip_rules+'    if ($uri = /robots.txt)')
    result, count = re.subn(r'(?m)^([ \t]*server\s*\{)\n', lambda m: m[0] + rules, content)
    if not count:
        raise ValueError('No site server block found')
    return result


def logging_config(content):
    """Explicitly authorized incident-only logging for this site's vhosts."""
    import re
    content=re.sub(r'(?m)^\s*(?:access_log|error_log)\s+[^;\n]+;[ \t]*\n?', '', content)
    content=content.replace('    # EVEOSINT incident-only logging\n','')
    result,count=re.subn(r'(?m)^([ \t]*server\s*\{)',
        r'\1\n    # EVEOSINT incident-only logging\n    access_log off;\n    error_log /dev/null;',content)
    if not count:raise ValueError('No site server block found')
    return result


def install_logging_if_updater():
    """Only the normal updater invokes the existing checked privileged worker."""
    from pathlib import Path
    try:group=Path('/proc/self/cgroup').read_text()
    except OSError:return
    if '/eveosint-code-update.service' not in group:return
    apply_proxy_blocks(configured_addresses())


def configured_addresses():
    from .public_traffic import active_blocked_addresses
    return active_blocked_addresses()


def apply_proxy_blocks(addresses):
    """Confirm the checked Nginx reload before reporting a security change as applied."""
    from . import nginx_config as nginx
    content=nginx.load_nginx_config()
    updated=crawler_config(logging_config(content),addresses)
    if content==updated:return
    deadline=time.monotonic()+10
    while True:
        try:
            request_id=nginx.queue_nginx_save(updated,'Admin IP security blocks')
            break
        except nginx.NginxConfigError as error:
            if str(error)!='worker_busy' or time.monotonic()>=deadline:raise
            time.sleep(.1)
    while time.monotonic()<deadline:
        state=nginx._read_json(nginx.STATUS_FILE,{})
        if state.get('request_id')==request_id:
            if state.get('result')=='success':return
            if state.get('result')=='failed':raise RuntimeError('IP security configuration failed; previous configuration restored')
        time.sleep(.1)
    raise RuntimeError('IP security configuration was not confirmed; check Nginx worker status')


RATE_LIMIT = 300
WINDOW = 60
MAX_CLIENTS = 10000
_secret = secrets.token_bytes(32)
_clients = OrderedDict()
_lock = threading.Lock()


def abusive_rate(address):
    try:
        ip=ipaddress.ip_address(address)
    except (ValueError,TypeError):return False
    token=hmac.new(_secret,str(ip).encode(),'sha256').digest()
    now=time.monotonic()
    with _lock:
        start,count=_clients.pop(token,(now,0))
        if now-start>=WINDOW:start,count=now,0
        count+=1;_clients[token]=(start,count)
        while len(_clients)>MAX_CLIENTS:_clients.popitem(last=False)
        return count>RATE_LIMIT
