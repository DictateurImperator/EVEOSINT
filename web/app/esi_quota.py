"""Web callers share the on-host ESI budget with daily/live jobs."""
import sys
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parents[2] / 'scripts'
if str(SCRIPTS) not in sys.path:sys.path.insert(0,str(SCRIPTS))
from esi_global_quota import wait_for_slot
