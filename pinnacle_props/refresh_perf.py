"""Credential-free phase timings and current resident memory for refresh diagnosis."""
from contextlib import contextmanager
from pathlib import Path
import json,os,sys,time,uuid,subprocess
from datetime import datetime,timezone

def resident_mb():
    try:
        if sys.platform.startswith('linux'):
            return int(Path('/proc/self/statm').read_text().split()[1])*os.sysconf('SC_PAGE_SIZE')/1048576
        return int(subprocess.check_output(['ps','-o','rss=','-p',str(os.getpid())],timeout=1))/1024
    except Exception:return None

class RefreshPerf:
    def __init__(self,root,report=None,*,clock=time.monotonic):
        self.root=Path(root);self.report=report or (lambda text:None);self.clock=clock;self.phases={}
        from .bounded_io import SHEETS
        self.initial_calls=SHEETS.total;self.started=clock()
    def caption(self):return ' · '.join(f'{name} {value:.1f} s' for name,value in self.phases.items())
    @contextmanager
    def phase(self,name):
        start=self.clock();self.report((self.caption()+' · ' if self.phases else '')+name+'…')
        try:yield
        finally:
            self.phases[name]=self.phases.get(name,0)+self.clock()-start
            self.report(self.caption())
    def finish(self,status):
        from .bounded_io import SHEETS
        with SHEETS.lock:
            calls=SHEETS.total-self.initial_calls
            minute_calls=sum(self.clock()-t<60 for t in SHEETS.calls)
        record=dict(at=datetime.now(timezone.utc).isoformat(),status=status,phases=self.phases,
            total_seconds=self.clock()-self.started,rss_mb=resident_mb(),
            sheet_calls=calls,sheet_calls_last_minute=minute_calls)
        target=self.root/'perf';target.mkdir(parents=True,exist_ok=True)
        (target/(str(uuid.uuid4())+'.json')).write_text(json.dumps(record,sort_keys=True))
        self.report(self.caption()+f" · Sheets calls {record['sheet_calls']}")
        return record


def logged_refresh(fn):
    """CLI/scheduled scans get the same diagnostics as explicitly instrumented UI scans."""
    from functools import wraps
    @wraps(fn)
    def run(*args,**kwargs):
        if kwargs.get('perf') is not None:return fn(*args,**kwargs)
        from .settings import ROOT
        perf=RefreshPerf(kwargs.get('data_dir',ROOT/'data'))
        kwargs['perf']=perf;status='failed'
        try:
            result=fn(*args,**kwargs);status='complete';return result
        finally:perf.finish(status)
    return run
