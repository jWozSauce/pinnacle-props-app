import json
from pinnacle_props.live_usage import since


def test_manifest_status_deduplicates_cache_recovery_and_keeps_unknowns(tmp_path):
    folder=tmp_path/'closes/nba/day';folder.mkdir(parents=True)
    def write(name,rows):
        (folder/name).write_text(json.dumps({'accounting':{'policy':'account-floor-v1','requests':rows}}))
    row={'request_id':'one','credits':5,'at':'2026-10-21T20:00:00Z'}
    write('one.json',[row]);write('recovered.json',[dict(row,cached=True)])
    write('failed.job.json',[{'request_id':'unknown','credits':None,'at':row['at']}])
    result=since(tmp_path,'2026-10-21T19:00:00Z')
    assert result['credits']==5 and result['unknown_charges']==1 and not result['complete']
    write('resolved.json',[{'request_id':'unknown','credits':2,'at':row['at']}])
    result=since(tmp_path,'2026-10-21T19:00:00Z')
    assert result['credits']==7 and result['complete']
    assert since(tmp_path,'2026-10-21T21:00:00Z')['credits']==0


def test_manifest_status_never_reads_or_writes_ledger(tmp_path):
    ledger=tmp_path/'odds_budget.json';ledger.write_text('unreadable retired ledger')
    assert since(tmp_path,'2026-10-21T19:00:00Z')['credits']==0
    assert ledger.read_text()=='unreadable retired ledger'


def test_unrelated_import_json_is_not_a_bad_manifest(tmp_path):
    folder=tmp_path/'imports';folder.mkdir()
    (folder/'preview.json').write_text('[{"row":1}]')
    assert since(tmp_path,'2026-10-21T19:00:00Z')['complete']


def test_offline_manifest_is_reported_without_materialization(tmp_path,monkeypatch):
    from pathlib import Path
    from types import SimpleNamespace
    folder=tmp_path/'closes';folder.mkdir();path=folder/'offline.json';path.write_text('{}')
    original=Path.stat
    def stat(p,*a,**kw):
        value=original(p,*a,**kw)
        return SimpleNamespace(st_mode=value.st_mode,st_flags=0x40000000) if p==path else value
    monkeypatch.setattr(Path,'stat',stat)
    summaries=folder/'manifests';summaries.mkdir();(summaries/'legacy.json').write_text('not a run receipt')
    report=since(tmp_path,'2026-10-21T19:00:00Z')
    assert report['issues']==['closes/offline.json'] and not report['complete']
