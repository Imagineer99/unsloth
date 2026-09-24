# SPDX-License-Identifier: AGPL-3.0-only
import asyncio,io,json,pathlib,re,secrets,socket,wave,httpx,os,hashlib
from playwright.async_api import async_playwright,expect,Error
from urllib.parse import urlsplit
ROOT=pathlib.Path(os.environ.get('PR11075_ROOT',pathlib.Path(__file__).resolve().parent)).resolve()
OUT=ROOT/'ui-evidence'
PIN={'before':'f9bffe265889379126785d129700345a11f38b60','after':'9482694959cbc1df5cf5d32982f2c4ce56b2e6c9'}
def auth(meta):
 home=pathlib.Path(meta['home']);creds=home/'probe-login.json'
 if creds.exists():return json.loads(creds.read_text())
 with httpx.Client(trust_env=False) as c:
  url='http://127.0.0.1:'+str(meta['port'])
  pw=(home/'auth/.bootstrap_password').read_text().strip()
  r=c.post(url+'/api/auth/login',json={'username':'unsloth','password':pw});r.raise_for_status();data=r.json()
  r=c.post(url+'/api/auth/change-password',headers={'Authorization':'Bearer '+data['access_token']},json={'current_password':pw,'new_password':secrets.token_urlsafe(24)+'aA1!'});r.raise_for_status()
  data=r.json();creds.write_text(json.dumps(data));creds.chmod(0o600);return data
def wav():
 b=io.BytesIO()
 with wave.open(b,'wb') as w:w.setnchannels(1);w.setsampwidth(2);w.setframerate(16000);w.writeframes(b'\0\0'*1600)
 return b.getvalue()
async def main():
 sock=socket.socket(socket.AF_INET,socket.SOCK_DGRAM);sock.connect(('8.8.8.8',80));ip=sock.getsockname()[0];sock.close()
 facts={'plan':json.loads((ROOT/'scene-plan.json').read_text()),'browser':'chromium','sides':{},'checks':[]}
 async with async_playwright() as p:
  browser=await p.chromium.launch(args=['--use-fake-device-for-media-stream','--use-fake-ui-for-media-stream'])
  facts['browser_version']=browser.version
  for side in ['before','after']:
   meta=json.loads((OUT/side/'launch.json').read_text());assert meta['sha']==PIN[side]
   with httpx.Client(trust_env=False) as c:
    tree=pathlib.Path(meta['tree'])
    index=(tree/'studio/frontend/dist/index.html').read_text()
    asset=re.search(r'<script\b[^>]*\btype="module"[^>]*\bsrc="([^"]+\.js)"',index).group(1)
    local=(tree/'studio/frontend/dist'/asset.lstrip('/')).read_bytes()
    served=c.get('http://127.0.0.1:'+str(meta['port'])+asset);served.raise_for_status()
    assert hashlib.sha256(local).digest()==hashlib.sha256(served.content).digest()
    meta['entry_asset']=asset
    meta['asset_sha256']=hashlib.sha256(local).hexdigest()
    chat_files=[f for f in (tree/'studio/frontend/dist/assets').glob('chat-*.js') if re.fullmatch(r'chat-[A-Za-z0-9_-]{8}\.js',f.name)]
    assert len(chat_files)==1
    chat_file=chat_files[0]
    chat_asset='/assets/'+chat_file.name
    chat_response=c.get('http://127.0.0.1:'+str(meta['port'])+chat_asset);chat_response.raise_for_status()
    assert hashlib.sha256(chat_file.read_bytes()).digest()==hashlib.sha256(chat_response.content).digest()
    meta['chat_asset']=chat_asset
    meta['chat_asset_sha256']=hashlib.sha256(chat_response.content).hexdigest()
   data=auth(meta)
   with httpx.Client(trust_env=False) as c:
    r=c.post('http://127.0.0.1:'+str(meta['port'])+'/api/chat/threads',headers={'Authorization':'Bearer '+data['access_token']},json={'id':'dictate-evidence-destination','title':'Evidence destination','modelType':'base','modelId':'','createdAt':1700000000000})
    r.raise_for_status()
   facts['sides'][side]={'sha':meta['sha'],'port':meta['port'],'home':meta['home'],'entry_asset':meta['entry_asset'],'asset_sha256':meta['asset_sha256'],'chat_asset':meta['chat_asset'],'chat_asset_sha256':meta['chat_asset_sha256'],'surfaces':{}}
   for surface in ['main','compare']:
    ctx=await browser.new_context(viewport={'width':1200,'height':900},locale='en-GB',color_scheme='light')
    await ctx.add_init_script("localStorage.setItem('unsloth_auth_token',"+json.dumps(data['access_token'])+");localStorage.setItem('unsloth_auth_refresh_token',"+json.dumps(data['refresh_token'])+");localStorage.setItem('unsloth_voice_settings',JSON.stringify({state:{dictationEngine:'model',sttModel:'tiny',dictationLanguage:'en',sttDevice:'cpu'},version:1}));")
    page=await ctx.new_page();errors=[];requests=[];inference=[];mode={'value':'success','gate':asyncio.Event()}
    page.on('pageerror',lambda e:errors.append(str(e)))
    submit_paths={'/api/inference/chat-runs','/api/inference/chat/completions','/api/inference/generate/stream','/v1/chat/completions'}
    page.on('request',lambda r:inference.append(urlsplit(r.url).path) if r.method=='POST' and urlsplit(r.url).path in submit_paths else None)
    engine={'available':True,'loaded_model':'tiny','loading':False,'device':'cpu','keep_alive_seconds':300,'default_model':'tiny','models':['tiny'],'downloaded_models':['tiny'],'download':{'downloading':False,'model':None,'error':None,'bytes_done':None,'bytes_total':None}}
    async def audio_route(route):
     url=route.request.url
     if '/stt/status' in url:await route.fulfill(json={**engine,'gguf':engine,'transformers':engine})
     elif '/transcribe/raw' in url:
      requests.append({'url':url,'bytes':len(route.request.post_data_buffer or b'')})
      if mode['value']=='delay':
       await mode['gate'].wait()
       try:await route.fulfill(json={'text':'LATE TRANSCRIPT'})
       except Error:pass
      elif mode['value']=='failure':await route.fulfill(status=500,json={'detail':'Controlled retry test'})
      else:await route.fulfill(json={'text':'Controlled transcript.'})
     elif '/stt/load' in url or '/stt/unload' in url:await route.fulfill(json={'status':'ok','model':'tiny'})
     else:await route.continue_()
    await page.route('**/api/inference/audio/**',audio_route)
    url='http://'+ip+':'+str(meta['port'])
    await page.goto(url+'/chat',wait_until='domcontentloaded')
    await expect(page.get_by_role('button',name='Dictate',exact=True)).to_be_visible()
    if surface=='compare':
     await page.get_by_role('button',name='Tools and attachments',exact=True).click()
     await page.get_by_role('menuitem',name='More',exact=True).hover()
     await page.get_by_role('menuitem',name='Compare chat',exact=True).click()
     await page.wait_for_url(re.compile(r'.*[?&]compare='))
     await expect(page.get_by_placeholder('Send to both models...',exact=True)).to_be_visible()
    editor=page.locator('textarea:visible').first
    await editor.fill('Draft to keep.')
    await expect(editor).to_have_value('Draft to keep.')
    secure=await page.evaluate('window.isSecureContext');assert secure is False
    await page.get_by_role('button',name='Dictate',exact=True).click()
    dialog=page.get_by_role('dialog',name='Dictate with a recording',exact=True)
    async def shot(name):
     await page.screenshot(path=str(OUT/(side+'-'+surface+'-'+name+'.png')),animations='disabled')
    if side=='before':
     await expect(page.get_by_text('Voice typing needs a secure connection.',exact=True)).to_be_visible()
     await expect(dialog).to_have_count(0)
     await shot('entry')
     sf={'secure_context':False,'dialog_visible':False,'secure_connection_error':True,'draft':await editor.input_value(),'transcription_requests':len(requests)}
     facts['checks'].append(side+'/'+surface+': expected fallback assertion fails (dialog absent, secure-connection error visible)')
    else:
     await expect(dialog).to_be_visible()
     await expect(dialog.get_by_text('Ready on this Studio server',exact=True)).to_be_visible()
     await expect(dialog.get_by_text('Whisper Tiny',exact=True)).to_be_visible()
     await expect(dialog.get_by_text('English',exact=True)).to_be_visible()
     await shot('entry')
     sf={'secure_context':False,'dialog_visible':True,'secure_connection_error':False,'model':'Whisper Tiny','language':'English','readiness':'ready (controlled fixture)'}
     async def choose():
      async with page.expect_file_chooser() as chooser:
       await dialog.get_by_role('button',name='Choose recording',exact=True).click()
      await (await chooser.value).set_files({'name':'voice.wav','mimeType':'audio/wav','buffer':wav()})
     await choose()
     await expect(editor).to_have_value('Draft to keep. Controlled transcript.')
     assert len(requests)==1 and not inference
     assert all(x in requests[0]['url'] for x in ['model=tiny','language=en','device=cpu','engine=gguf'])
     await shot('success')
     facts['checks'].append(side+'/'+surface+': selected WAV appends once with captured settings and no send')
     mode['value']='failure'
     await page.get_by_role('button',name='Dictate',exact=True).click()
     await expect(dialog.get_by_text('Ready on this Studio server',exact=True)).to_be_visible()
     await choose()
     await expect(dialog.get_by_role('button',name='Retry transcription',exact=True)).to_be_visible()
     mode['value']='success'
     await expect(dialog.get_by_text('Ready on this Studio server',exact=True)).to_be_visible()
     await dialog.get_by_role('button',name='Retry transcription',exact=True).click()
     await expect(editor).to_have_value('Draft to keep. Controlled transcript. Controlled transcript.')
     facts['checks'].append(side+'/'+surface+': failed recording retained; explicit retry appends once')
     for action in ['cancel','navigate']:
      mode['value']='delay';mode['gate']=asyncio.Event()
      await page.get_by_role('button',name='Dictate',exact=True).click()
      await expect(dialog.get_by_text('Ready on this Studio server',exact=True)).to_be_visible()
      await choose()
      await expect(page.get_by_role('button',name='Cancel transcription',exact=True)).to_be_visible()
      if action=='cancel':await page.get_by_role('button',name='Cancel transcription',exact=True).click()
      else:
       await page.get_by_text('Evidence destination',exact=True).click()
       await page.wait_for_url(re.compile(r'.*thread=dictate-evidence-destination'))
       await expect(page.get_by_role('button',name='Dictate',exact=True)).to_be_visible()
       await editor.fill('Destination draft.')
      before=await editor.input_value()
      mode['gate'].set()
      await page.wait_for_timeout(250)
      await expect(editor).to_have_value(before)
      assert 'LATE TRANSCRIPT' not in await editor.input_value()
      facts['checks'].append(side+'/'+surface+': '+action+' fences delayed response')
     assert not inference,inference
     sf['transcription_requests']=len(requests);sf['chat_submission_requests']=len(inference)
    facts['sides'][side]['surfaces'][surface]=sf
    # Same real frontend/live adapter on localhost; Chromium supplies a synthetic microphone.
    mode['value']='success'
    await page.goto('http://127.0.0.1:'+str(meta['port'])+'/chat',wait_until='domcontentloaded')
    await expect(page.get_by_role('button',name='Dictate',exact=True)).to_be_visible()
    if surface=='compare':
     await page.get_by_role('button',name='Tools and attachments',exact=True).click()
     await page.get_by_role('menuitem',name='More',exact=True).hover()
     await page.get_by_role('menuitem',name='Compare chat',exact=True).click()
     await expect(page.get_by_placeholder('Send to both models...',exact=True)).to_be_visible()
    assert await page.evaluate('window.isSecureContext')
    await page.get_by_role('button',name='Dictate',exact=True).click()
    if surface=='main':await expect(page.get_by_label('Voice recording',exact=True)).to_be_visible()
    else:await expect(page.get_by_role('button',name='Stop dictation',exact=True)).to_be_visible()
    await expect(dialog).to_have_count(0)
    await page.keyboard.press('Escape')
    facts['checks'].append(side+'/'+surface+': localhost keeps live recording')
    sf['localhost_live_recording']=True
    assert not errors,errors
    await ctx.close()
    print('PASS',side,surface,flush=True)
   (OUT/'meta.json').write_text(json.dumps(facts,indent=2))
  await browser.close()
 assert facts['sides']['before']['chat_asset_sha256']!=facts['sides']['after']['chat_asset_sha256']
 facts['verified']=True
 facts['differences']={'main':{'dialog_visible':[False,True],'secure_connection_error':[True,False]},'compare':{'dialog_visible':[False,True],'secure_connection_error':[True,False]}}
 (OUT/'meta.json').write_text(json.dumps(facts,indent=2))
 print('PASS all',len(facts['checks']),'scenario checks',flush=True)
asyncio.run(main())
