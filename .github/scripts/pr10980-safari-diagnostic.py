# SPDX-License-Identifier: AGPL-3.0-only
"""Cold-start diagnostics, then the unchanged native Safari interaction suite.

--local-chromium validates the diagnostic plumbing only; it is not Safari proof.
"""
import argparse
import json
import os
from pathlib import Path
import runpy
import subprocess
import sys
import time
import urllib.request

parser=argparse.ArgumentParser()
parser.add_argument('--repo',required=True)
parser.add_argument('--local-chromium',action='store_true')
args=parser.parse_args()
repo=Path(args.repo).resolve()
os.chdir(repo)
sys.path.insert(0,str(repo/'tests/studio'))
from _playwright_robust import stop_process

out=repo/'temp/queue-validation/compatibility'
out.mkdir(parents=True,exist_ok=True)
config=repo/'studio/frontend/temp/pr10980-startup/vite.config.ts'
config.parent.mkdir(parents=True,exist_ok=True)
injection="""window.__reviewStartupErrors=[];
window.addEventListener('error',e=>window.__reviewStartupErrors.push({kind:'error',message:e.message,filename:e.filename,line:e.lineno}));
window.addEventListener('unhandledrejection',e=>window.__reviewStartupErrors.push({kind:'rejection',message:String(e.reason)}));"""
config.write_text("import {mergeConfig} from 'vite';\nimport original from '../../vite.config';\nexport default mergeConfig(original,{plugins:[{name:'startup-diagnostic',transformIndexHtml:{order:'pre',handler(){return [{tag:'script',injectTo:'head-prepend',children:"+json.dumps(injection)+"}];}}}],server:{host:'127.0.0.1',strictPort:true}});\n")
report={'mode':'local Chromium plumbing check' if args.local_chromium else 'native Safari','sha':subprocess.check_output(['git','rev-parse','HEAD'],text=True).strip(),'initial_10s_has_rows':False,'eventual_has_rows':False,'interaction_suite_passed':False}
assert report['sha']=='564e9d2ff48fb343cc701a501ad04fc56df3efb7'
log=(out/'startup-vite.log').open('w')
proc=subprocess.Popen(['node','node_modules/vite/bin/vite.js','--config','temp/pr10980-startup/vite.config.ts','--host','127.0.0.1','--port','5423'],cwd=repo/'studio/frontend',stdout=log,stderr=subprocess.STDOUT,start_new_session=True)
url='http://127.0.0.1:5423/smoke-prompt-queue-actions.html'
snapshot="""return {url:location.href,readyState:document.readyState,rows:[...document.querySelectorAll('[data-queue-item-id]')].map(e=>e.dataset.queueItemId),errors:window.__reviewStartupErrors||[],root:document.getElementById('root')?.innerHTML.slice(0,2500),resources:performance.getEntriesByType('resource').map(e=>({name:e.name,duration:e.duration,transferSize:e.transferSize}))};"""
try:
    deadline=time.monotonic()+30
    while True:
        try:
            with urllib.request.urlopen(url,timeout=2) as response: assert response.status==200
            break
        except Exception:
            if proc.poll() is not None or time.monotonic()>deadline: raise
            time.sleep(.2)
    if args.local_chromium:
        from playwright.sync_api import sync_playwright,TimeoutError
        with sync_playwright() as pw:
            browser=pw.chromium.launch(headless=True)
            try:
                page=browser.new_page(viewport={'width':1100,'height':900})
                started=time.monotonic()
                page.goto(url,wait_until='domcontentloaded')
                report['browser_version']=browser.version
                try:
                    page.wait_for_function("document.querySelectorAll('[data-queue-item-id]').length===3",timeout=10000)
                    report['initial_10s_has_rows']=True
                except TimeoutError:
                    report['at_10s']=page.evaluate('()=>{'+snapshot+'}')
                    page.screenshot(path=str(out/'startup-at-10s.png'))
                page.wait_for_function("document.querySelectorAll('[data-queue-item-id]').length===3",timeout=80000)
                report['eventual_has_rows']=True
                report['startup_seconds']=round(time.monotonic()-started,3)
                report['final']=page.evaluate('()=>{'+snapshot+'}')
            finally: browser.close()
    else:
        from selenium import webdriver
        from selenium.webdriver.support.ui import WebDriverWait
        from selenium.common.exceptions import TimeoutException
        driver=webdriver.Safari()
        try:
            driver.set_page_load_timeout(90)
            driver.set_window_size(1100,900)
            report['browser_version']=driver.capabilities.get('browserVersion')
            started=time.monotonic()
            driver.get(url)
            ready=lambda d:d.execute_script("return [...document.querySelectorAll('[data-queue-item-id]')].map(e=>e.dataset.queueItemId)")==['q0','q1','q2']
            try:
                WebDriverWait(driver,10).until(ready)
                report['initial_10s_has_rows']=True
            except TimeoutException:
                report['at_10s']=driver.execute_script(snapshot)
                driver.save_screenshot(str(out/'startup-at-10s.png'))
            WebDriverWait(driver,80).until(ready)
            report['eventual_has_rows']=True
            report['startup_seconds']=round(time.monotonic()-started,3)
            report['final']=driver.execute_script(snapshot)
        finally:
            try:
                report['last_state']=driver.execute_script(snapshot)
                driver.save_screenshot(str(out/'startup-last-state.png'))
            finally: driver.quit()
        # Reuse the diagnosed server; execute the pinned interaction assertions unchanged.
        module=runpy.run_path(str(repo/'tests/studio/selenium_composer_safari.py'))
        module['main'].__globals__.update(start_vite=lambda port:proc,stop_process=lambda p:None)
        module['main']()
        report['interaction_suite_passed']=True
except Exception as error:
    report['error']=repr(error)
    raise
finally:
    (out/'startup-diagnostic.json').write_text(json.dumps(report,indent=2)+'\n')
    print(json.dumps({k:v for k,v in report.items() if k not in ('final','at_10s','last_state')},indent=2))
    stop_process(proc)
    log.close()
