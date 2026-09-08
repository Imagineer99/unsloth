# SPDX-License-Identifier: AGPL-3.0-only
import os
import sys
import tempfile
import faulthandler
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
arm = sys.argv[1]
assert arm in ('base','head')
os.environ['PROBE_ARM'] = arm
sys.path.insert(0,str(ROOT/'temp'/arm))
scratch=ROOT/'temp'/('scratch-'+arm)
scratch.mkdir(parents=True,exist_ok=True)
os.environ['TMPDIR']=os.environ['TMP']=os.environ['TEMP']=str(scratch)
tempfile.tempdir=str(scratch)
import pytest
class Counts:
    def __init__(self): self.passed=0; self.xfailed=0
    def pytest_runtest_logreport(self,report):
        if report.when=='call':
            self.passed += report.passed
            self.xfailed += report.skipped and hasattr(report,'wasxfail')
counts=Counts()
faulthandler.dump_traceback_later(90,exit=True)
code=pytest.main(['-p','no:cacheprovider','-q','-s','-rx',
    str(ROOT/'probes/test_process_http.py'),str(ROOT/'probes/test_recovery.py'),
    '--basetemp='+str(scratch/'pytest')],plugins=[counts])
faulthandler.cancel_dump_traceback_later()
assert code==0, f'Test failure: {code}'
assert (counts.passed,counts.xfailed)==((1,3) if arm=='base' else (4,0))
print(json.dumps({'arm':arm,'passed':counts.passed,'confirmed_baseline_failures':counts.xfailed}))
