#!/usr/bin/env python3
"""PR 10623: full Studio A/B against a loopback-only deterministic provider.

No API credentials are needed. Evidence describes a fixture, not model inference.
"""
import argparse
import hashlib
import json
import os
import re
import secrets
import signal
import shutil
import socket
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import httpx
from playwright.sync_api import sync_playwright, expect

BEFORE = '178e1722f5445c7260be1b932c1502426675ba36'
AFTER = 'f458f1d0e9200282953ba2470801e886643f212e'
REJECTION = 'The min_p and logit_bias sampling parameters are not yet supported with speculative decoding.'
MODEL = 'minp-proof'


class Fixture(BaseHTTPRequestHandler):
    def log_message(self, *_): pass
    def do_GET(self):
        data={'object':'list','data':[{'id':MODEL,'object':'model','owned_by':'fixture','max_model_len':4096}]}
        self.reply(200,json.dumps(data),'application/json')
    def reply(self,status,text,kind):
        raw=text.encode();self.send_response(status);self.send_header('Content-Type',kind)
        self.send_header('Content-Length',str(len(raw)));self.end_headers();self.wfile.write(raw)
    def do_POST(self):
        body=json.loads(self.rfile.read(int(self.headers.get('Content-Length','0'))))
        self.server.requests.append(body)
        rejected=body.get('min_p',self.server.default_min_p)>1e-5
        if rejected:
            error={'error':{'message':REJECTION,'type':'BadRequestError','code':400}}
            if body.get('stream'):self.reply(200,'data: '+json.dumps(error)+'\n\ndata: [DONE]\n\n','text/event-stream')
            else:self.reply(400,json.dumps(error),'application/json')
        else:
            content='MINP_FIXTURE_OK'
            if body.get('stream'):
                chunks=[{'id':'proof','object':'chat.completion.chunk','choices':[{'index':0,'delta':{'role':'assistant','content':content},'finish_reason':None}]},
                        {'id':'proof','object':'chat.completion.chunk','choices':[{'index':0,'delta':{},'finish_reason':'stop'}],'usage':{'prompt_tokens':8,'completion_tokens':4,'total_tokens':12}}]
                self.reply(200,''.join('data: '+json.dumps(x)+'\n\n' for x in chunks)+'data: [DONE]\n\n','text/event-stream')
            else:self.reply(200,json.dumps({'choices':[{'message':{'role':'assistant','content':content},'finish_reason':'stop'}]}),'application/json')


def free_port():
    with socket.socket() as sock:sock.bind(('127.0.0.1',0));return sock.getsockname()[1]


def wait_until(fn,timeout=30):
    end=time.monotonic()+timeout
    while time.monotonic()<end:
        if fn():return
        time.sleep(.1)
    raise AssertionError('condition did not become true')


def run_side(args,label,tree,pw,fixture):
    expected=BEFORE if label=='before' else AFTER
    actual=subprocess.check_output(['git','-C',str(tree),'rev-parse','HEAD'],text=True).strip()
    assert actual==expected,(actual,expected)
    side=args.output/label;side.mkdir(parents=True,exist_ok=False)
    home=side/'home';home.mkdir();port=free_port();base=f'http://127.0.0.1:{port}'
    password=secrets.token_urlsafe(24)+'Aa1!'
    env=dict(os.environ,UNSLOTH_STUDIO_HOME=str(home),UNSLOTH_STUDIO_PASSWORD=password,
             UNSLOTH_STUDIO_DISABLE_DEVICE_PROBE='1',HF_HOME=str(side/'hf'),HF_HUB_CACHE=str(side/'hf/hub'),
             HF_XET_CACHE=str(side/'hf/xet'),XDG_CACHE_HOME=str(side/'cache'),TMPDIR=str(side),
             UNSLOTH_ALLOW_CPU='1',UNSLOTH_IS_PRESENT='1',DO_NOT_TRACK='1')
    frontend=tree/'studio/frontend/dist'
    assert (frontend/'index.html').is_file()
    facts={'label':label,'sha':actual,'home':str(home),'port':port,'browser':args.browser,'fixture':True,
           'bundle_sha256':hashlib.sha256((frontend/'index.html').read_bytes()).hexdigest()}
    log=(side/'server.log').open('w')
    proc=subprocess.Popen([sys.executable,str(tree/'studio/backend/run.py'),'--host','127.0.0.1','--port',str(port),
                           '--frontend',str(frontend),'--no-cloudflare','--disable-tools'],cwd=tree,env=env,stdout=log,stderr=subprocess.STDOUT,start_new_session=True)
    browser=None;page=None
    try:
        def healthy():
            if proc.poll() is not None:raise RuntimeError(f'{label} server exited {proc.returncode}; see server.log')
            try:return httpx.get(base+'/api/health',timeout=2).status_code==200
            except httpx.HTTPError:return False
        wait_until(healthy,120)
        auth=httpx.post(base+'/api/auth/login',json={'username':'unsloth','password':password},timeout=15)
        auth.raise_for_status();tokens=auth.json();assert not tokens.get('must_change_password')
        headers={'Authorization':'Bearer '+tokens['access_token']}
        fixture.requests.clear();fixture.default_min_p=0
        provider={'id':'minp-proof-provider','providerType':'vllm','name':'vLLM proof fixture','baseUrl':f'http://127.0.0.1:{fixture.server_port}/v1','models':[MODEL]}
        saved=httpx.post(base+'/api/providers/',headers=headers,json={'provider_type':'vllm','display_name':provider['name'],'base_url':provider['baseUrl'],'models':[MODEL],'available_models':[MODEL]},timeout=15)
        saved.raise_for_status();provider['id']=saved.json()['id']
        seed={'unsloth_auth_token':tokens['access_token'],'unsloth_refresh_token':tokens.get('refresh_token',''),
              'unsloth_chat_external_providers':json.dumps([provider]),'unsloth_chat_external_provider_keys':'{}',
              'unsloth_chat_connections_enabled':'true','unsloth_chat_settings_imported_to_studio_db':'true'}
        browser=getattr(pw,args.browser).launch(headless=True)
        context=browser.new_context(viewport={'width':1440,'height':1000},device_scale_factor=1,color_scheme='light')
        # Only seed once: reload must use persisted state rather than replacing it.
        context.add_init_script('if(!sessionStorage.getItem("proof-seeded")){for(const [k,v] of Object.entries('+json.dumps(seed)+'))localStorage.setItem(k,v);sessionStorage.setItem("proof-seeded","1")}')
        page=context.new_page();errors=[];page.on('pageerror',lambda e:errors.append(str(e)))
        page.goto(base+'/chat',wait_until='domcontentloaded')
        page.locator('form:has(textarea) textarea').first.wait_for(timeout=45000)
        (side/'initial-dom.txt').write_text(page.locator('body').inner_text())
        # Select the exact fixture model through the shipped model picker.
        trigger=page.locator('[data-testid="model-picker-trigger"]:visible').first
        if not trigger.count():trigger=page.get_by_role('button',name=re.compile('Select model',re.I)).first
        trigger.click(timeout=15000)
        page.get_by_text('Connected',exact=True).last.click(timeout=15000)
        page.get_by_text(MODEL,exact=True).last.click(timeout=15000)
        page.wait_for_timeout(500)
        (side/'selected-dom.txt').write_text(page.locator('body').inner_text())
        # Settings is a per-chat side panel, not the global Settings dialog.
        opener=page.get_by_role('button',name='Open run settings',exact=True)
        if opener.count():opener.click()
        number=page.locator('input[aria-label="Min P"]:visible').first
        if not number.count():
            button=page.get_by_role('button',name=re.compile('Run settings',re.I)).first
            button.click()
        if not number.count():
            page.get_by_role('button',name=re.compile('Sampling',re.I)).first.click()
        expect(number).to_be_visible(timeout=15000)
        mode=page.locator('[aria-label="Min P mode"]:visible').first
        if label=='after':expect(mode).to_have_text('Server default',timeout=15000)
        facts.update({'provider':'vllm','model':MODEL,'minP':float(number.input_value()),'mode':' '.join(mode.inner_text().split()) if mode.count() else None,'input_disabled':number.is_disabled()})
        assert facts['minP']==0.01
        assert facts['input_disabled']==(label=='after')
        assert (facts['mode']=='Server default') if label=='after' else facts['mode'] is None
        page.wait_for_timeout(400)
        page.screenshot(path=str(side/'settings-full.png'))
        box=number.bounding_box();assert box and box['x']<1440
        page.screenshot(path=str(side/'settings.png'),clip={'x':1040,'y':0,'width':400,'height':1000})

        def send(text):
            prior=len(fixture.requests)
            box=page.locator('form:has(textarea) textarea').first
            box.fill(text);box.press('Enter')
            # Playwright waits pump page events so the async request can finish.
            page.wait_for_function('() => !document.querySelector("form textarea")?.value',timeout=15000)
            end=time.monotonic()+30
            while len(fixture.requests)==prior and time.monotonic()<end:page.wait_for_timeout(100)
            assert len(fixture.requests)>prior,'no request reached loopback provider'
            page.wait_for_timeout(1500)
            return fixture.requests[-1]

        body=send('Reply with the proof marker.')
        facts['fresh_request_sampling']={k:body[k] for k in ['min_p','temperature','top_p','top_k','repetition_penalty','presence_penalty'] if k in body}
        if label=='before':
            assert body.get('min_p')==0.01
            expect(page.get_by_role('button',name='Set Min P to 0',exact=True)).to_have_count(0)
            facts['fresh_result']='speculative rejection; no targeted action'
        else:
            assert 'min_p' not in body
            expect(page.get_by_text('MINP_FIXTURE_OK',exact=True).first).to_be_visible(timeout=10000)
            facts['fresh_result']='success'
            mode.click();page.get_by_role('option',name='Custom',exact=True).click()
            expect(number).to_be_enabled();number.fill('0.2');number.press('Enter')
            send('Exercise speculative decoding recovery.')
            action=page.get_by_role('button',name='Set Min P to 0',exact=True)
            expect(action).to_be_visible(timeout=3000)
            page.screenshot(path=str(side/'recovery.png'))
            action.click();expect(number).to_have_value('0')
            expect(mode).to_have_text('Custom')
            prior=len(fixture.requests)
            page.get_by_role('button',name='Retry',exact=True).click()
            end=time.monotonic()+30
            while len(fixture.requests)==prior and time.monotonic()<end:page.wait_for_timeout(100)
            assert len(fixture.requests)>prior,'manual Retry did not reach provider'
            retry=fixture.requests[-1]
            page.wait_for_timeout(1500)
            assert retry.get('min_p')==0
            expect(page.get_by_text('MINP_FIXTURE_OK',exact=True).last).to_be_visible(timeout=10000)
            facts['recovery_retry_min_p']=retry['min_p']
            # Stale recovery action must not clobber the later edit.
            number.fill('0.2');number.press('Enter');send('Create a stale recovery action.')
            expect(action).to_be_visible(timeout=3000)
            number.fill('0.3');number.press('Enter');action.click()
            expect(number).to_have_value('0.3');facts['stale_action_preserved']=0.3
            # Persist zero and verify on a real reload of the current chat.
            number.fill('0');number.press('Enter');page.wait_for_timeout(1200)
            page.reload(wait_until='domcontentloaded')
            page.get_by_text('Reply with the proof marker.',exact=True).first.click(timeout=15000)
            opener=page.get_by_role('button',name='Open run settings',exact=True)
            if opener.count():opener.click()
            expect(page.locator('input[aria-label="Min P"]:visible').first).to_have_value('0',timeout=15000)
            facts['reload_min_p']=0
        facts['page_errors']=errors
        # WebKit serializes Error.message with an Error: prefix.
        assert all(e in (REJECTION, 'Error: '+REJECTION) for e in errors),errors
        (side/'requests.json').write_text(json.dumps(fixture.requests,indent=2))
        facts['settings_api']=httpx.get(base+'/api/chat/settings',headers=headers).json()
        facts['passed']=True
        return facts
    except Exception:
        if page:
            page.screenshot(path=str(side/'failure.png'))
            (side/'failure-dom.txt').write_text(page.locator('body').inner_text())
        raise
    finally:
        (side/'facts.json').write_text(json.dumps(facts,indent=2))
        if browser:browser.close()
        if proc.poll() is None:
            os.killpg(proc.pid,signal.SIGTERM)
            try:proc.wait(timeout=15)
            except subprocess.TimeoutExpired:os.killpg(proc.pid,signal.SIGKILL);proc.wait()
        log.close()


def main():
    parser=argparse.ArgumentParser();parser.add_argument('--before',type=Path,required=True);parser.add_argument('--after',type=Path,required=True)
    parser.add_argument('--output',type=Path,required=True);parser.add_argument('--browser',default='chromium',choices=['chromium','firefox','webkit'])
    parser.add_argument('--only',choices=['before','after'])
    args=parser.parse_args();args.output=args.output.resolve();args.output.mkdir(parents=True,exist_ok=False)
    fixture=ThreadingHTTPServer(('127.0.0.1',0),Fixture);fixture.requests=[];fixture.default_min_p=0
    threading.Thread(target=fixture.serve_forever,daemon=True).start();facts=[]
    try:
        with sync_playwright() as pw:
            for label,tree in [('before',args.before.resolve()),('after',args.after.resolve())]:
                if args.only and label!=args.only:continue
                facts.append(run_side(args,label,tree,pw,fixture));print('PASS '+label,flush=True)
        (args.output/'meta.json').write_text(json.dumps({'scene':'vllm-min-p-server-default','expect':'BEFORE sends numeric Min P; AFTER delegates by omission and supports guarded explicit-zero recovery.','facts':facts},indent=2))
        public=args.output/'public';public.mkdir()
        clean=[]
        for fact in facts:
            clean.append({k:v for k,v in fact.items() if k not in ('home','settings_api')})
            for name in ['settings.png','settings-full.png','recovery.png']:
                source=args.output/fact['label']/name
                if source.exists():shutil.copyfile(source,public/(fact['label']+'-'+name))
        (public/'facts.json').write_text(json.dumps({'fixture':'Loopback deterministic vLLM contract fixture; no model inference','facts':clean},indent=2))
    finally:fixture.shutdown();fixture.server_close()

if __name__=='__main__':main()
