"""Bounded request concurrency and shared rate-limit control; no scheduler."""
from concurrent.futures import ThreadPoolExecutor, as_completed
from contextlib import contextmanager
from contextvars import copy_context
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
import math
import threading
import time


def workers(config,kind,override=None):
    value=config.get('request_workers',{}).get(kind,{'scans':6,'closes':4,'historical':3}[kind]) if override is None else override
    if type(value) is not int or not 1<=value<=10:raise ValueError('Request workers must be an integer from 1 to 10')
    return value


def retry_after(value,default=2.):
    try:delay=float(value)
    except (ValueError,TypeError):
        try:delay=(parsedate_to_datetime(value)-datetime.now(timezone.utc)).total_seconds()
        except (ValueError,TypeError,OverflowError):return default
    return max(0.,delay) if math.isfinite(delay) else default


class Pool:
    def __init__(self,limit):
        if type(limit) is not int or not 1<=limit<=10:raise ValueError('Request workers must be an integer from 1 to 10')
        self.initial=self.limit=limit
        self.condition=threading.Condition(threading.RLock())
        self.active=0;self.reservations=set();self.pause_until=0.;self.stopped=False
        self.rate_limit=None;self._locks={}

    def check(self):
        if self.stopped:
            from .odds_api import BudgetStop
            raise BudgetStop('Request pool stopped; no further request sent')

    def stop(self):
        with self.condition:self.stopped=True;self.condition.notify_all()

    @contextmanager
    def identity(self,path):
        with self.condition:lock=self._locks.setdefault(str(path),threading.RLock())
        with lock:yield

    @contextmanager
    def slot(self):
        with self.condition:
            while True:
                self.check();delay=self.pause_until-time.monotonic()
                if delay<=0 and self.active<self.limit:break
                self.condition.wait(timeout=max(.01,delay) if delay>0 else None)
            self.active+=1
        try:yield
        finally:
            with self.condition:self.active-=1;self.condition.notify_all()

    def wait_send(self):
        with self.condition:
            while self.pause_until>time.monotonic():
                self.check();self.condition.wait(timeout=self.pause_until-time.monotonic())
            self.check()

    def backoff(self,value,status):
        delay=retry_after(value)
        with self.condition:
            self.pause_until=max(self.pause_until,time.monotonic()+delay)
            self.limit=max(1,self.limit//2)
            if self.rate_limit is None:
                self.rate_limit=dict(at=datetime.now(timezone.utc).isoformat(),occurrences=0,
                                     initial_workers=self.initial)
            self.rate_limit.update(occurrences=self.rate_limit['occurrences']+1,workers=self.limit,
                                   retry_after_seconds=delay,status=status)
            self.condition.notify_all()

    def owns(self,rid):
        with self.condition:return rid in self.reservations

    def claim(self,rid):
        with self.condition:self.reservations.add(rid)

    def release(self,rid):
        with self.condition:self.reservations.discard(rid)

    def map(self,items,fn,progress=None):
        """Finish in-flight work after failure; preserve each input slot and its error."""
        items=list(items);results=[None]*len(items);errors=[None]*len(items);completed=0
        def execute(item):
            self.check()
            return fn(item)
        with ThreadPoolExecutor(max_workers=self.initial,thread_name_prefix='odds') as executor:
            futures={executor.submit(copy_context().run,execute,item):i for i,item in enumerate(items)}
            for future in as_completed(futures):
                index=futures[future]
                try:results[index]=future.result()
                except Exception as exc:
                    errors[index]=exc;self.stop()
                completed+=1
                if progress:progress(completed,len(items))
        return results,errors


def staged(pool,items,fn,before=lambda:None,width=None,progress=None):
    """Check gates between bounded cohorts; consumers also check each ordered result."""
    items=list(items);position=0
    while position<len(items):
        before()
        size=min(pool.initial,width() if width else pool.initial,len(items)-position)
        if size<1:raise ValueError('Staged cohort must contain at least one item')
        values,errors=pool.map(items[position:position+size],fn,progress=progress)
        for item,value,error in zip(items[position:position+size],values,errors):
            yield item,value,error
        if any(errors):return
        position+=size
