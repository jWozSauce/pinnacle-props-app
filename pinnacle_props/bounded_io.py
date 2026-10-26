"""Bound caller waits without retrying uncertain writes or touching UI off-thread."""
from collections import deque
import threading
import time

class WaitUnavailable(ValueError):pass

class Deadline:
    def __init__(self, seconds, *, limit=40, clock=time.monotonic):
        self.seconds=seconds;self.limit=limit;self.clock=clock
        self.lock=threading.Lock();self.pending=None;self.cooldown=0.;self.calls=deque();self.total=0

    def call(self, fn):
        with self.lock:
            now=self.clock()
            while self.calls and now-self.calls[0]>=60:self.calls.popleft()
            if now<self.cooldown or (self.pending is not None and not self.pending.is_set()):
                raise WaitUnavailable('External call still unresolved; retry later')
            if len(self.calls)>=self.limit:raise WaitUnavailable('Local request budget reached; retry later')
            self.calls.append(now);self.total+=1
            done=threading.Event();self.pending=done;result=[]
            def run():
                try:result.append((True,fn()))
                except Exception as exc:result.append((False,exc))
                finally:done.set()
            # Only the transport runs here. No Streamlit, cache, outbox or retry code.
            threading.Thread(target=run,daemon=True,name='bounded-transport').start()
        if not done.wait(self.seconds):
            with self.lock:self.cooldown=self.clock()+60
            raise WaitUnavailable('External call timed out; acknowledgement uncertain')
        ok,value=result[0]
        if not ok:raise value
        return value

SHEETS=Deadline(10,limit=40)
PUBLIC_SHEET=Deadline(10,limit=20)
FREE_FEED=Deadline(5,limit=120)


def sheet_client(auth,session=None):
    from gspread.http_client import HTTPClient
    class BoundedHTTPClient(HTTPClient):
        def request(self,*args,**kwargs):
            parent=super().request
            return SHEETS.call(lambda:parent(*args,**kwargs))
    return BoundedHTTPClient(auth,session)
