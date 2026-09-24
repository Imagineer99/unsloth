# SPDX-License-Identifier: AGPL-3.0-only
"""Pinned, CPU-only Studio UI A/B runner. Never uploads homes or server logs."""
import json,os,pathlib,shutil,signal,subprocess,sys,time
import httpx
REPO=pathlib.Path.cwd().resolve()
ROOT=REPO/'temp/pr11075-http-dictate-ab'
SCRIPTS=REPO/'.github/scripts/pr11075'
PINS={'base':'f9bffe265889379126785d129700345a11f38b60','head':'9482694959cbc1df5cf5d32982f2c4ce56b2e6c9'}
def run(args,cwd=REPO,log=None,timeout=600):
 if log:
  with open(log,'w') as stream:
   subprocess.run(args,cwd=cwd,env=ENV,stdout=stream,stderr=subprocess.STDOUT,check=True,timeout=timeout)
 else:subprocess.run(args,cwd=cwd,env=ENV,check=True,timeout=timeout)
ROOT.mkdir(parents=True,exist_ok=False)
BROWSER_TMP=REPO/'temp/pw';BROWSER_TMP.mkdir(parents=True,exist_ok=True)
ENV=dict(os.environ,PR11075_ROOT=str(ROOT),TMPDIR=str(BROWSER_TMP),TEMP=str(BROWSER_TMP),TMP=str(BROWSER_TMP))
ART=pathlib.Path(os.environ['STUDIO_ARTIFACT_DIR']).resolve()
ART.mkdir(parents=True,exist_ok=True)
try:
 run(['git','fetch','https://github.com/unslothai/unsloth.git',*PINS.values()])
 for name,sha in PINS.items():
  run(['git','worktree','add','--detach',str(ROOT/name),sha])
  actual=subprocess.check_output(['git','rev-parse','HEAD'],cwd=ROOT/name,text=True).strip()
  assert actual==sha
  front=ROOT/name/'studio/frontend'
  run(['npm','ci','--ignore-scripts','--no-audit','--no-fund'],cwd=front,log=ROOT/(name+'-install.log'))
  run(['npm','run','build'],cwd=front,log=ROOT/(name+'-build.log'))
 shutil.copyfile(SCRIPTS/'scene-plan.json',ROOT/'scene-plan.json')
 for side in ['before','after']:
  run([sys.executable,str(SCRIPTS/'launch_side.py'),side])
  meta=json.loads((ROOT/'ui-evidence'/side/'launch.json').read_text())
  deadline=time.monotonic()+60
  with httpx.Client(trust_env=False) as c:
   while True:
    try:
     if c.get('http://127.0.0.1:'+str(meta['port'])+'/',timeout=2).status_code==200:break
    except httpx.HTTPError:pass
    if time.monotonic()>deadline:raise RuntimeError(side+' Studio did not start; setup failure, not A/B evidence')
    time.sleep(.5)
 run([sys.executable,str(SCRIPTS/'studio_ab_probe.py')],log=ROOT/'results.log',timeout=180)
 print((ROOT/'results.log').read_text(),flush=True)
 run([sys.executable,str(SCRIPTS/'compose_evidence.py')])
finally:
 # Upload allow-list: never publish Studio auth state, browser storage, environment, or backend logs.
 evidence=ROOT/'ui-evidence'
 names=['meta.json','main-comparison.png','compare-comparison.png',
        'before-main-entry.png','after-main-entry.png','before-compare-entry.png','after-compare-entry.png',
        'after-main-success.png','after-compare-success.png']
 for name in names:
  if (evidence/name).is_file():shutil.copyfile(evidence/name,ART/name)
 for name in ['scene-plan.json','results.log']:
  if (ROOT/name).is_file():shutil.copyfile(ROOT/name,ART/name)
 for side in ['before','after']:
  file=evidence/side/'launch.json'
  if file.exists():
   meta=json.loads(file.read_text())
   pathlib.Path(meta['home']).resolve().relative_to(ROOT)
   try:
    if os.getpgid(meta['pid'])==meta['pid']:os.killpg(meta['pid'],signal.SIGTERM)
   except ProcessLookupError:pass
