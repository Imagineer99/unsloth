"""Pinned real-app before/after evidence. No route mocks or source injection.
Private JSON: {before:{url,home,worktree,sha,pid,access_token,refresh_token?,api_key},after:{...}}.
Each home must contain .uidiff_sha. Sources must remain clean at the pinned SHA.
"""
from __future__ import annotations
import argparse, asyncio, hashlib, json, os, re, subprocess, sys, time
from pathlib import Path
from urllib.parse import urlsplit
import httpx, psutil
from PIL import Image, ImageDraw, ImageFont
from playwright.async_api import async_playwright, expect

STAGE='startup'
SECRETS=[]

def digest(data): return hashlib.sha256(data).hexdigest()
def run(*args,cwd=None): return subprocess.check_output(args,cwd=cwd,text=True).strip()
def scrub(value):
    text=str(value)
    for secret in SECRETS:
        if secret: text=text.replace(secret,'[REDACTED]')
    return text

def identity(side):
    global STAGE
    STAGE='identity-'+side['sha'][:8]
    root=Path(side['worktree']).resolve();home=Path(side['home']).resolve()
    assert run('git','rev-parse','HEAD',cwd=root)==side['sha'],'worktree SHA mismatch'
    assert not run('git','diff','--name-only',cwd=root),'tracked working-tree edits invalidate evidence'
    assert (home/'.uidiff_sha').read_text().strip()==side['sha'],'home build stamp mismatch'
    proc=psutil.Process(int(side['pid']))
    cwd=Path(proc.cwd()).resolve();assert cwd==root or root in cwd.parents,'server cwd outside worktree'
    port=urlsplit(side['url']).port
    assert port,'server port missing'
    identity_method='direct-process-environment-and-listener'
    try:
        env=proc.environ()
        assert Path(env.get('UNSLOTH_STUDIO_HOME','')).resolve()==home,'server home mismatch'
        assert any(c.status==psutil.CONN_LISTEN and c.laddr.port==port for c in proc.net_connections(kind='inet')),'PID is not listening on expected port'
    except (psutil.AccessDenied, NotImplementedError):
        assert sys.platform in ('win32','darwin'),'Linux identity must remain strict'
        record=json.loads(Path(side['launch_record_path']).read_text(encoding='utf-8-sig'))
        for key in ('pid','sha','url'):
            assert str(record[key])==str(side[key]),'launcher record mismatch: '+key
        for key in ('home','worktree'):
            assert Path(record[key]).resolve()==Path(side[key]).resolve(),'launcher path mismatch: '+key
        assert side.get('studio_root_id'),'native fallback needs known per-home server root id'
        identity_method='owner-runner-record-plus-health-root-and-assets'
    start_token=Path('/proc/'+str(proc.pid)+'/stat').read_text().rsplit(')',1)[1].split()[19] if sys.platform.startswith('linux') else str(proc.create_time())
    return {'sha':side['sha'],'worktree':str(root),'home':str(home),'pid':proc.pid,'port':port,'process_start_token':start_token,'process_cwd':str(cwd),'home_stamp_verified':True,'identity_method':identity_method}

async def verify_assets(client,side):
    if side.get('studio_root_id'):
        health=await client.get('/api/health');health.raise_for_status()
        assert health.json().get('studio_root_id')==side['studio_root_id'],'responding server root id mismatch'
    dist=Path(side['worktree'])/'studio/frontend/dist'
    local=(dist/'index.html').read_text();response=await client.get('/')
    response.raise_for_status()
    assets=set(re.findall(r'["\x27](/assets/[^"\x27]+)["\x27]',local))
    served=set(re.findall(r'["\x27](/assets/[^"\x27]+)["\x27]',response.text))
    assert assets and assets==served,'served index asset list differs from pinned build'
    entry=re.findall(r'<script[^>]+src=["\x27]([^"\x27]+)',local)
    css=[a for a in assets if a.endswith('.css')]
    verified={}
    for url in entry+css:
        if not url.startswith('/assets/'): continue
        r=await client.get(url);r.raise_for_status()
        expected=(dist/url.lstrip('/')).read_bytes()
        assert digest(r.content)==digest(expected),'served asset hash mismatch'
        verified[url]=digest(expected)
    assert verified,'no entry assets verified'
    return {'local_index_sha256':digest(local.encode()),'served_entry_asset_hashes':verified,'index_asset_count':len(assets)}

async def rpc(client,side,method,params=None):
    res=await client.post('/mcp/',headers={'Authorization':'Bearer '+side['api_key'],'Accept':'application/json, text/event-stream'},json={'jsonrpc':'2.0','id':1,'method':method,'params':params or {}})
    assert res.status_code==200,'MCP HTTP status '+str(res.status_code)
    try: data=res.json()
    except ValueError:
        frames=[json.loads(line[5:].strip()) for line in res.text.splitlines() if line.startswith('data:')]
        assert frames,'MCP SSE result missing';data=frames[-1]
    assert 'result' in data,'MCP JSON-RPC error'
    return data['result']

async def open_settings(page,tab):
    await page.goto('/chat',wait_until='domcontentloaded')
    await page.get_by_role('button',name='Settings',exact=True).first.wait_for(timeout=60000)
    await page.get_by_role('button',name='Settings',exact=True).first.click()
    await expect(page.locator('[data-settings-dialog]')).to_be_visible(timeout=30000)
    await page.locator('[data-testid="settings-tab-'+tab+'"]').click()

async def secrets_absent(page):
    text=await page.locator('body').inner_text()
    values=await page.locator('input,textarea').evaluate_all('(els)=>els.map(e=>e.value).join("\\n")')
    assert all(not secret or (secret not in text and secret not in values) for secret in SECRETS),'credential appeared in capture surface'

async def shoot(page,path):
    await secrets_absent(page)
    await page.evaluate('document.fonts.ready')
    dialog=page.locator('[data-settings-dialog]')
    box=await dialog.bounding_box();assert box and box['width']>800,'unexpected dialog crop'
    await dialog.screenshot(path=str(path),animations='disabled')
    return {'path':str(path),'sha256':digest(path.read_bytes()),'clip':box,'pixels':list(Image.open(path).size)}

async def drive(browser,side,label,out):
    global STAGE
    ident=identity(side)
    async with httpx.AsyncClient(base_url=side['url'],timeout=30,follow_redirects=True,trust_env=False) as client:
        assets=await verify_assets(client,side)
        headers={'Authorization':'Bearer '+side['access_token']}
        initial=await client.get('/api/settings/mcp-access',headers=headers)
        assert initial.status_code==200,'owner settings session failed'
        payload={'enabled':True}
        if label=='after':payload['disabled_tools']=[]
        saved=await client.put('/api/settings/mcp-access',headers=headers,json=payload)
        assert saved.status_code==200,'could not enable MCP in isolated home'
        before_tools=(await rpc(client,side,'tools/list'))['tools']
        assert len(before_tools)==23 and 'chat' in [t['name'] for t in before_tools],'unexpected starting tool catalog'
        ctx=await browser.new_context(base_url=side['url'],viewport={'width':1440,'height':1100},device_scale_factor=1,color_scheme='light',reduced_motion='reduce',locale='en-GB')
        seed={'unsloth_auth_token':side['access_token'],'theme':'light','palette':'standard','unsloth_locale':'en'}
        if side.get('refresh_token'):seed['unsloth_refresh_token']=side['refresh_token']
        await ctx.add_init_script('for(const [k,v] of Object.entries('+json.dumps(seed)+')) localStorage.setItem(k,v);')
        page=await ctx.new_page();errors=[]
        page.on('pageerror',lambda e:errors.append(scrub(str(e))))
        STAGE='open-settings-'+label
        await open_settings(page,'api-keys' if label=='before' else 'mcp')
        assert not await page.locator('html').evaluate('(e)=>e.classList.contains("dark")'),'theme differs from plan'
        mcp_tab=page.locator('[data-testid="settings-tab-mcp"]')
        access=page.get_by_role('switch',name='Allow agent connections',exact=True)
        await expect(access).to_have_attribute('aria-checked','true')
        shots=[]
        if label=='before':
            assert await mcp_tab.count()==0,'baseline unexpectedly has MCP tab'
            assert await page.locator('[data-mcp-tool]').count()==0,'baseline unexpectedly has per-tool rows'
            await page.locator('[data-settings-label="Agent access (MCP)"]').evaluate('''(e)=>{ const panel=e.closest('main').querySelector('div.hover-scrollbar'); for(let p=e.parentElement;p&&p!==document.body;p=p.parentElement) if(p!==panel)p.scrollTop=0; panel.scrollTop=panel.scrollHeight; }''')
            settings=(await client.get('/api/settings/mcp-access',headers=headers)).json()
            assert 'tools' not in settings,'baseline response unexpectedly has tool settings'
            listing=before_tools;chat_error=None
        else:
            await expect(mcp_tab).to_be_visible()
            await expect(page.locator('[data-mcp-tool]')).to_have_count(23)
            shots.append(await shoot(page,out/'after-overview.png'))
            chat=page.locator('[data-mcp-tool="chat"] [role="switch"]')
            await expect(chat).to_have_attribute('aria-checked','true')
            STAGE='disable-chat-through-real-ui'
            await chat.click();await expect(chat).to_have_attribute('aria-checked','false')
            await expect(chat).to_be_enabled()
            settings=(await client.get('/api/settings/mcp-access',headers=headers)).json()
            assert [t['name'] for t in settings['tools'] if not t['enabled']]==['chat'],'backend settings did not persist UI toggle'
            listing=(await rpc(client,side,'tools/list'))['tools']
            assert len(listing)==22 and 'chat' not in [t['name'] for t in listing],'actual MCP listing did not hide chat'
            chat_error=await rpc(client,side,'tools/call',{'name':'chat','arguments':{'prompt':'This call must be refused before execution.'}})
            assert chat_error.get('isError') is True and '[tool_disabled]' in json.dumps(chat_error),'disabled call not refused'
            await page.locator('[data-mcp-tool="chat"]').evaluate('''(e)=>{ const panel=e.closest('main').querySelector('div.hover-scrollbar'); for(let p=e.parentElement;p&&p!==document.body;p=p.parentElement) if(p!==panel)p.scrollTop=0; panel.scrollTop+=e.getBoundingClientRect().top-panel.getBoundingClientRect().top-panel.clientHeight/2; }''')
            await expect(chat).to_have_attribute('aria-checked','false')
        STAGE='capture-'+label
        shots.append(await shoot(page,out/(label+'.png')))
        facts={'settings_tab':'API' if label=='before' else 'MCP','mcp_tab_present':bool(await mcp_tab.count()),'rendered_tool_rows':await page.locator('[data-mcp-tool]').count(),'advertised_tools':len(listing),'advertised_names':[t['name'] for t in listing],'chat_enabled':label=='before','disabled_call_refused':None if label=='before' else True,'settings_response_has_tools':'tools' in settings,'theme':'light','uncaught_js_errors':errors}
        assert not errors,'uncaught JavaScript error during scene'
        ident_after=identity(side)
        if ident_after!=ident:
            (out/(label+'-identity-change.json')).write_text(json.dumps({'start':ident,'end':ident_after},indent=2))
        assert ident_after==ident,'process identity changed during capture'
        await ctx.close()
        return {'identity':ident,'build':assets,'facts':facts,'shots':shots}

def compose(out,data):
    images=[Image.open(out/(label+'.png')).convert('RGB') for label in ('before','after')]
    width=max(im.width for im in images);pad=24;header=92;gap=24
    canvas=Image.new('RGB',(width+2*pad,sum(im.height+header for im in images)+3*pad),(239,242,246))
    draw=ImageDraw.Draw(canvas)
    font_path=next((p for p in ['/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf','C:/Windows/Fonts/arial.ttf','/System/Library/Fonts/Supplemental/Arial.ttf'] if Path(p).exists()),None)
    font=ImageFont.truetype(font_path,25) if font_path else ImageFont.load_default(size=25)
    small=ImageFont.truetype(font_path,18) if font_path else ImageFont.load_default(size=18)
    y=pad
    for label,im in zip(('before','after'),images):
        title=('BEFORE — Settings > API' if label=='before' else 'AFTER — Settings > MCP; chat switched off')
        f=data[label]['facts'];detail=f"{data[label]['identity']['sha'][:10]}  |  {f['rendered_tool_rows']} tool rows in DOM  |  {f['advertised_tools']} tools advertised"
        draw.text((pad,y+8),title,font=font,fill=(20,28,38));draw.text((pad,y+46),detail,font=small,fill=(56,66,79))
        canvas.paste(im,(pad,y+header));y+=header+im.height+gap
    canvas.save(out/'comparison.png')

async def main():
    parser=argparse.ArgumentParser();parser.add_argument('--config',required=True,type=Path);parser.add_argument('--out',required=True,type=Path);parser.add_argument('--engine',choices=['chromium','firefox','webkit'],default='chromium');parser.add_argument('--channel',choices=['chrome','msedge']);args=parser.parse_args()
    cfg=json.loads(args.config.read_text(encoding='utf-8-sig'));out=args.out.resolve();out.mkdir(parents=True,exist_ok=True)
    expectation=json.loads((Path(__file__).parent/'expectation.json').read_text(encoding='utf-8-sig'))
    assert cfg['before']['sha']==expectation['before_sha'] and cfg['after']['sha']==expectation['after_sha'],'config refs differ from original plan'
    assert cfg['before']['home']!=cfg['after']['home'] and cfg['before']['pid']!=cfg['after']['pid'] and cfg['before']['url']!=cfg['after']['url'],'sides are not isolated'
    for side in cfg.values():
        if isinstance(side,dict):SECRETS.extend(side.get(k,'') for k in ['access_token','refresh_token','api_key'])
    async with async_playwright() as pw:
        kwargs={'headless':True}
        if args.channel:
            assert args.engine=='chromium','channel requires Chromium';kwargs['channel']=args.channel
        browser=await getattr(pw,args.engine).launch(**kwargs)
        data={}
        try:
            for label in ('before','after'):
                data[label]=await drive(browser,cfg[label],label,out)
                print(label+' verified: '+json.dumps(data[label]['facts']),flush=True)
            diffs={key:{'before':data['before']['facts'].get(key),'after':data['after']['facts'].get(key)} for key in sorted(set(data['before']['facts'])|set(data['after']['facts'])) if data['before']['facts'].get(key)!=data['after']['facts'].get(key)}
            assert diffs,'facts unexpectedly identical'
            meta={'pr':14348,'scene':expectation['scene'],'original_expectation':expectation,'browser':{'engine':args.engine,'channel':args.channel,'version':browser.version},'environment':{'os':sys.platform,'python':sys.version.split()[0]},**data,'fact_differences':diffs,'verification':'Automated identity, state, API and secret checks passed; composite still requires visual inspection.'}
            (out/'meta.json').write_text(json.dumps(meta,indent=2))
            compose(out,data)
            print('Saved comparison.png and meta.json; visual inspection required.',flush=True)
        finally:await browser.close()

if __name__=='__main__':
    try:asyncio.run(main())
    except Exception as exc:
        print('FAILED at '+STAGE+': '+type(exc).__name__+': '+scrub(exc),file=sys.stderr)
        sys.exit(1)
