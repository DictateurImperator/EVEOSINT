"""Per-tab subscriptions: replacing a selection cancels only abandoned source work."""
from collections import OrderedDict
from contextvars import ContextVar
import re
import threading
import time

_LOCK=threading.RLock()
_CLIENTS=OrderedDict()
CURRENT=ContextVar('personal_group_scan',default=None)


class Cancelled(Exception):pass
class StaleSelection(ValueError):pass


def select(client,generation,signature):
    if not re.fullmatch(r'[a-f0-9]{32}',client or ''):raise ValueError('Invalid scan client.')
    generation=int(generation)
    if not 0<generation<2**53:raise ValueError('Invalid scan generation.')
    cancelled=[]
    with _LOCK:
        now=time.monotonic()
        item=_CLIENTS.get(client)
        if item and (generation<item['generation'] or
                     generation==item['generation'] and (signature!=item['signature'] or item.get('closed'))):
            raise StaleSelection('This selection was replaced.')
        for old,expired in list(_CLIENTS.items()):
            if now-expired['at']>120:
                _CLIENTS.pop(old);cancelled+=_detach(old,expired)
        item=_CLIENTS.get(client)
        if not item or generation>item['generation']:
            if item:cancelled+=_detach(client,item)
            item=dict(generation=generation,signature=signature,control=None,at=now)
            _CLIENTS[client]=item
        item['at']=now;_CLIENTS.move_to_end(client)
        while len(_CLIENTS)>256:
            old,expired=_CLIENTS.popitem(last=False);cancelled+=_detach(old,expired)
    for control in cancelled:control.cancel()
    return client,generation


def _detach(client,item):
    control=item.get('control')
    if not control:return []
    control.viewers.discard((client,item['generation']))
    if control.viewers or control.finished:return []
    control.cancelled.set()
    return [control]


def leave(client,generation):
    cancelled=[]
    with _LOCK:
        item=_CLIENTS.get(client)
        if item and item['generation']==generation:
            cancelled=_detach(client,item);item['control']=None;item['closed']=True
    for control in cancelled:control.cancel()


class Control:
    def __init__(self):
        self.viewers=set();self.cancelled=threading.Event();self.connection=None;self.finished=False

    def subscribe(self):
        viewer=CURRENT.get()
        with _LOCK:
            self.check()
            if viewer:
                client,generation=viewer;item=_CLIENTS.get(client)
                if not item or item['generation']!=generation or item.get('closed'):raise StaleSelection('This selection was replaced.')
                item['control']=self
            self.viewers.add(viewer)
        self.check()

    def check(self):
        if self.cancelled.is_set():raise Cancelled()

    def bind(self,connection):
        with _LOCK:self.connection=connection
        self.check()

    def unbind(self):
        with _LOCK:self.connection=None;self.finished=True

    def cancel(self):
        if self.finished:return
        self.cancelled.set()
        with _LOCK:connection=self.connection
        if connection is not None:
            try:connection.cancel()
            except Exception:pass # The worker may already have closed its connection.
