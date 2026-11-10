"""Capture the real Unsloth UI. No substituted markup, injected CSS, or OS spoofing.

Windows: Edge; macOS: Playwright WebKit; Linux: native WebKitGTK MiniBrowser.
These are web-engine captures, not a claim of testing the complete Tauri shell.
"""
from __future__ import annotations
import asyncio
import hashlib
import http.server
import json
import os
import platform
import subprocess
import threading
import time
import urllib.request
from pathlib import Path

MODES = [('dark', 'liquid-3d'), ('light', 'liquid-3d'), ('dark', 'liquid'), ('dark', 'transparent'), ('dark', 'solid')]
VIEWPORT = (1440, 1000)
CLOCK = '''(() => { const RealDate=Date, start=RealDate.now(), fixed=1794308400000;
class TestDate extends RealDate { constructor(...args) { super(...(args.length ? args : [fixed+RealDate.now()-start])); }
static now() { return fixed+RealDate.now()-start; } } window.Date=TestDate; Math.random=()=>0.37; })();'''

FACTS = r'''() => {
  const root = document.documentElement;
  const style = (selector, pseudo) => {
    const el = document.querySelector(selector);
    if (!el) return null;
    const s = getComputedStyle(el, pseudo || null), r = el.getBoundingClientRect();
    return {content:s.content, backgroundImage:s.backgroundImage, backgroundColor:s.backgroundColor,
      boxShadow:s.boxShadow, backdropFilter:s.backdropFilter || s.webkitBackdropFilter,
      bounds:{x:r.x,y:r.y,width:r.width,height:r.height}};
  };
  return {ua:navigator.userAgent,platform:navigator.platform,dpr:devicePixelRatio,
    viewport:{width:innerWidth,height:innerHeight},
    rootAttributes:Object.fromEntries([...root.attributes].map(a=>[a.name,a.value])),
    greetingText:document.querySelector('.unsloth-welcome-title')?.textContent,
    greeting:style('.unsloth-welcome-greeting','::before'),
    header:style('.chat-wallpaper','::after'),
    settings:style('[data-settings-dialog]'),
    settingsBlur:style('[data-settings-dialog]','::before'),
    composer:style('.unsloth-composer-surface'),
    fontsReady:document.fonts.status,
    stylesheets:[...document.querySelectorAll('link[rel="stylesheet"]')].map(e=>e.href)};
}'''
READY = "document.querySelector('.unsloth-welcome-greeting') && document.querySelector('.unsloth-composer-surface') && document.fonts.status === 'loaded'"
OPEN_SETTINGS = r'''() => {
  const buttons = [...document.querySelectorAll('button[aria-label="Settings"]')];
  const button = buttons.find(b => b.getClientRects().length && getComputedStyle(b).visibility !== 'hidden');
  if (!button) throw Error('Visible Settings button missing');
  button.click();
}'''

def seed(theme, material):
    values = {
        'theme': theme, 'palette': 'standard',
        'unsloth_window_material': json.dumps({'state': {'material': material, 'style': 'liquid-3d', 'menus': True,
             'strength': {'light': 50, 'dark': 50}, 'lens': 'off', 'scope': 'all'}, 'version': 0}),
        'unsloth_user_profile': json.dumps({'state': {'displayName': '', 'nickname': '', 'showGreetingSloth': True}, 'version': 0}),
    }
    return f'for (const [k,v] of Object.entries({json.dumps(values)})) localStorage.setItem(k,v);'

def crop_shots(path, facts, out, name):
    from PIL import Image
    with Image.open(path) as im:
        vp = facts['viewport']
        sx, sy = im.width / vp['width'], im.height / vp['height']
        targets = {'header': (0, 0, vp['width'], 190)}
        if facts.get('greeting'):
            r = facts['greeting']['bounds']
            targets['greeting'] = (r['x'] - 55, r['y'] - 70, r['x'] + r['width'] + 55, r['y'] + r['height'] + 70)
        for scene, box in targets.items():
            x0,y0,x1,y1 = box
            box = (max(0, round(x0*sx)), max(0,round(y0*sy)), min(im.width,round(x1*sx)),min(im.height,round(y1*sy)))
            im.crop(box).save(out / f'{name}-{scene}.png')

def verify(facts, theme, material, side):
    attrs = facts['rootAttributes']
    assert ('dark' in attrs.get('class', '').split()) == (theme == 'dark'), facts
    if material == 'transparent':
        assert 'data-glass-clear' in attrs, facts
    elif material != 'solid':
        assert attrs.get('data-glass') == material, facts
    if material != 'solid':
        assert facts['header'] and facts['greeting'], 'Glass surfaces did not mount'
        assert (facts['header']['content'] == 'none') == (side == 'after'), facts['header']
        if theme == 'dark':
            assert (facts['greeting']['content'] == 'none') == (side == 'after'), facts['greeting']
    assert facts['fontsReady'] == 'loaded'

async def capture_playwright(base_url, init, browser_name, out, side):
    from playwright.async_api import async_playwright
    results = []
    async with async_playwright() as p:
        factory = p.chromium if platform.system() == 'Windows' else p.webkit
        options = {'channel': 'msedge'} if platform.system() == 'Windows' else {}
        browser = await factory.launch(headless=True, **options)
        for theme, material in MODES:
            context = await browser.new_context(viewport={'width': VIEWPORT[0], 'height': VIEWPORT[1]}, device_scale_factor=1,
                                                color_scheme=theme, locale='en-GB', timezone_id='Europe/London')
            await context.add_init_script(init + '\n' + seed(theme, material) + '\n' + CLOCK)
            page = await context.new_page()
            errors = []
            page.on('pageerror', lambda e: errors.append(str(e)))
            try:
                await page.goto(base_url + '/chat', wait_until='domcontentloaded')
                await page.wait_for_function(READY, timeout=90000)
                if material != 'solid':
                    await page.wait_for_selector('.chat-wallpaper[data-ready]', state='attached')
                await page.wait_for_timeout(1000)
                name = f'{theme}-{material}'
                facts = await page.evaluate(FACTS)
                verify(facts, theme, material, side)
                shot = out / f'{name}-chat.png'
                await page.screenshot(path=str(shot), animations='disabled')
                crop_shots(shot, facts, out, name)
                await page.evaluate(OPEN_SETTINGS)
                dialog = page.locator('[data-settings-dialog]')
                await dialog.wait_for(state='visible')
                await dialog.get_by_text('Display language', exact=True).wait_for(timeout=45000)
                await page.wait_for_timeout(750)
                facts['settingsOpen'] = await page.evaluate(FACTS)
                await page.screenshot(path=str(out / f'{name}-settings-context.png'), animations='disabled')
                await dialog.screenshot(path=str(out / f'{name}-settings.png'), animations='disabled')
                facts.update({'theme': theme, 'material': material, 'engine': browser.version, 'pageErrors': errors})
                assert not errors, errors
                results.append(facts)
            finally:
                await context.close()
        await browser.close()
    return results

def capture_gtk(base_url, init, out, side):
    from selenium import webdriver
    from selenium.webdriver.webkitgtk.service import Service
    from selenium.webdriver.support.ui import WebDriverWait
    from selenium.webdriver.common.by import By
    os.environ['WEBKIT_DISABLE_COMPOSITING_MODE'] = '1'
    results = []
    for theme, material in MODES:
        # Native WebKitGTK has no WebDriver preload-script API. A loopback proxy inserts
        # only the same auth/preference/test-clock bootstrap used by Playwright, before
        # the real application's scripts. It does not change the DOM, CSS, or app bundle.
        bootstrap = init + '\n' + seed(theme,material) + '\n' + CLOCK
        class Proxy(http.server.BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass
            def relay(self):
                body = self.rfile.read(int(self.headers.get('Content-Length',0))) or None
                headers = {k:v for k,v in self.headers.items() if k.lower() not in ('host','accept-encoding','content-length','connection')}
                request = urllib.request.Request(base_url+self.path,data=body,headers=headers,method=self.command)
                try:
                    response = urllib.request.urlopen(request,timeout=45)
                except urllib.error.HTTPError as error:
                    response = error
                payload=response.read()
                content_type=response.headers.get('Content-Type','')
                if 'text/html' in content_type:
                    payload=payload.replace(b'<head>',('<head><script>'+bootstrap+'</script>').encode(),1)
                self.send_response(response.status)
                for k,v in response.headers.items():
                    if k.lower() not in ('content-length','content-encoding','transfer-encoding','connection'):
                        self.send_header(k,v)
                self.send_header('Content-Length',str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)
            do_GET=relay
            do_POST=relay
            do_PUT=relay
            do_PATCH=relay
            do_DELETE=relay
        proxy=http.server.ThreadingHTTPServer(('127.0.0.1',0),Proxy)
        proxy_thread=threading.Thread(target=proxy.serve_forever,daemon=True)
        proxy_thread.start()
        driver = webdriver.WebKitGTK(service=Service('/usr/bin/WebKitWebDriver'))
        try:
            driver.set_window_size(*VIEWPORT)
            driver.get(f'http://127.0.0.1:{proxy.server_port}/chat')
            WebDriverWait(driver, 90).until(lambda d: d.execute_script('return !!(' + READY + ')'))
            width,height = driver.execute_script('return [outerWidth + 1440 - innerWidth, outerHeight + 1000 - innerHeight]')
            driver.set_window_size(width,height)
            if material != 'solid':
                WebDriverWait(driver, 30).until(lambda d: d.find_elements(By.CSS_SELECTOR, '.chat-wallpaper[data-ready]'))
            time.sleep(1)
            name = f'{theme}-{material}'
            facts = driver.execute_script('return (' + FACTS + ')()')
            verify(facts,theme,material,side)
            if material not in ('solid','transparent'):
                assert 'data-glass-cpu' in facts['rootAttributes'], 'Native WebKitGTK renderer path not active'
            shot = out / f'{name}-chat.png'
            driver.save_screenshot(str(shot))
            crop_shots(shot,facts,out,name)
            driver.execute_script('(' + OPEN_SETTINGS + ')()')
            WebDriverWait(driver,45).until(lambda d: d.execute_script("return document.querySelector('[data-settings-dialog]')?.textContent.includes('Display language')"))
            time.sleep(1)
            facts['settingsOpen'] = driver.execute_script('return (' + FACTS + ')()')
            driver.save_screenshot(str(out / f'{name}-settings-context.png'))
            driver.find_element(By.CSS_SELECTOR,'[data-settings-dialog]').screenshot(str(out / f'{name}-settings.png'))
            facts.update({'theme':theme,'material':material,'capabilities':driver.capabilities})
            results.append(facts)
        finally:
            driver.quit()
            proxy.shutdown()
            proxy.server_close()
    return results

async def capture_scenes(base_url, init, browser_name, out):
    side = os.environ['EVIDENCE_SIDE']
    source = subprocess.check_output(['git','rev-parse','HEAD'],text=True).strip()
    css_path = Path('studio/frontend/src/index.css')
    metadata = {'side':side,'sourceSha':source,'sourceCssSha256':hashlib.sha256(css_path.read_bytes()).hexdigest(),
                'os':platform.platform(),'baseUrl':base_url,'home':os.environ['UNSLOTH_STUDIO_HOME'],
                'captureScope':'Real Unsloth Web UI on host OS; not full Tauri shell',
                'renderer': {'Windows':'Microsoft Edge','Darwin':'Playwright WebKit (not Safari/WKWebView)',
                             'Linux':'Native WebKitGTK MiniBrowser, CPU compositing'}[platform.system()]}
    (out/'identity.json').write_text(json.dumps(metadata,indent=2))
    if platform.system() == 'Linux':
        # This synchronous driver runs on a separate thread, outside the asyncio loop.
        results = await asyncio.to_thread(capture_gtk,base_url,init,out,side)
    else:
        results = await capture_playwright(base_url,init,browser_name,out,side)
    metadata['scenes'] = results
    (out/'facts.json').write_text(json.dumps(metadata,indent=2))
    print(f'PASS captured {len(results)} actual UI configurations on {platform.system()} ({side})',flush=True)
