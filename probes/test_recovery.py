# SPDX-License-Identifier: AGPL-3.0-only
import json
import os
import pytest
import typer
from types import SimpleNamespace
import unsloth_cli.commands.start as s

def test_transient_polling_failure(monkeypatch):
    h=SimpleNamespace(elapsed=0,polls=0,failures=0,shutdowns=[])
    h.server=SimpleNamespace(poll=lambda:None)
    def sleep(seconds):h.elapsed+=seconds
    def http(*args,**kwargs):
        h.polls+=1
        if h.polls==1:
            h.failures+=1
            raise TimeoutError('temporary progress endpoint failure')
        return {'downloaded_bytes':h.polls*1024}
    monkeypatch.setattr(s,'time',SimpleNamespace(monotonic=lambda:h.elapsed,sleep=sleep))
    monkeypatch.setattr(s,'_http_json',http)
    monkeypatch.setattr(s,'_log_tail',lambda *a,**k:
        'UNSLOTH_START_API_KEY: sk-unsloth-test\n'+('Model loaded: org/model\n' if h.elapsed>=2000 else ''))
    monkeypatch.setattr(s,'_studio_healthy',lambda *a,**k:True)
    monkeypatch.setattr(s.subprocess,'Popen',lambda *a,**k:h.server)
    monkeypatch.setattr(s.atexit,'register',lambda *a,**k:None)
    monkeypatch.setattr(s,'_auto_served_server',None)
    monkeypatch.setattr(s,'_shutdown_server',h.shutdowns.append)
    try:
        server=s._start_studio_server('http://127.0.0.1:8888','org/model',s.LoadOptions())
    except typer.Exit:
        assert os.environ['PROBE_ARM']=='base'
        assert h.elapsed==900
        assert h.failures==1
        assert h.polls==1
        assert h.shutdowns==[h.server]
        print(json.dumps({'arm':'base','outcome':'poller-retired-after-one-error',
            'elapsed':h.elapsed,'polls':h.polls,'shutdowns':len(h.shutdowns)}))
        pytest.xfail('Confirmed baseline abandons polling after its first error')
    assert os.environ['PROBE_ARM']=='head', 'Baseline unexpectedly recovered'
    assert server is h.server
    assert h.elapsed>900
    assert h.failures==1
    assert h.polls>=1000
    assert h.shutdowns==[]
    print(json.dumps({'arm':'head','outcome':'polling-recovered-and-loaded',
        'elapsed':h.elapsed,'polls':h.polls,'shutdowns':0}))
