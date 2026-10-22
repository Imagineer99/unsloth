"""Actual UI read/modify/write concurrency probe. No synthetic API data.
Each racing GET is fetched from the actual server, then both unchanged responses
are delivered together. PUT bodies are produced by the shipped frontend and sent
to the actual server. A failed invariant exits 1; harness/setup errors exit 2.
Private config is one head-server.json. Requires sibling capture_mcp.py helpers.
"""
from __future__ import annotations
import argparse, asyncio, json, sys, time
from pathlib import Path
import httpx
from playwright.async_api import async_playwright, expect
from capture_mcp import identity, verify_assets, rpc, open_settings, scrub, SECRETS

async def main(args):
    config=json.loads(args.config.read_text(encoding='utf-8-sig'));side=config.get('after',config)
    SECRETS.extend(side.get(k,'') for k in ('access_token','refresh_token','api_key'))
    target=args.out.resolve();out=target.parent if target.suffix=='.json' else target;out.mkdir(parents=True,exist_ok=True)
    report={'head_sha':side['sha'],'source':'built frontend with actual owner-authenticated backend and MCP','synthetic_api_responses':False,'engine':args.engine,'channel':args.channel,'sequential':None,'concurrent_rounds':[]}
    started=identity(side);report['identity']=started
    headers={'Authorization':'Bearer '+side['access_token']}
    async with httpx.AsyncClient(base_url=side['url'],timeout=30,trust_env=False) as client:
        report['served_build']=await verify_assets(client,side)
        async def state():
            r=await client.get('/api/settings/mcp-access',headers=headers);r.raise_for_status()
            return sorted(t['name'] for t in r.json()['tools'] if not t['enabled'])
        async def reset(names):
            r=await client.put('/api/settings/mcp-access',headers=headers,json={'enabled':True,'disabled_tools':names});r.raise_for_status()
            assert await state()==sorted(names),'reset did not persist'
        async def listed():
            return sorted(t['name'] for t in (await rpc(client,side,'tools/list'))['tools'])
        async with async_playwright() as pw:
            launch={'headless':True}
            if args.channel:
                assert args.engine=='chromium','channel requires Chromium';launch['channel']=args.channel
            browser=await getattr(pw,args.engine).launch(**launch)
            report['browser_version']=browser.version
            async def windows():
                contexts=[];pages=[];errors=[]
                for label in ('A','B'):
                    context=await browser.new_context(base_url=side['url'],viewport={'width':1440,'height':1100},color_scheme='light',reduced_motion='reduce',locale='en-GB')
                    await context.add_init_script('for(const[k,v]of Object.entries('+json.dumps({'unsloth_auth_token':side['access_token'],'unsloth_refresh_token':side.get('refresh_token',''),'theme':'light','unsloth_locale':'en'})+'))localStorage.setItem(k,v);')
                    page=await context.new_page();page.on('pageerror',lambda e:errors.append(scrub(e)))
                    await open_settings(page,'mcp')
                    for name in ('chat','cancel'):
                        await expect(page.locator('[data-mcp-tool="'+name+'"] [role="switch"]')).to_have_attribute('aria-checked','true')
                    contexts.append(context);pages.append(page)
                return contexts,pages,errors
            async def toggle(page,name,require_disabled=True):
                control=page.locator('[data-mcp-tool="'+name+'"] [role="switch"]')
                async with page.expect_response(lambda response: response.url.endswith('/api/settings/mcp-access') and response.request.method=='PUT',timeout=30000) as pending:
                    await control.click()
                response=await pending.value
                assert response.status==200,'actual tool-save PUT failed'
                await response.finished()
                # An overlapping save can already appear in this PUT response.
                # Judge that outcome from persisted state rather than timing out
                # while waiting for an overwritten switch to remain disabled.
                if require_disabled:
                    await expect(control).to_have_attribute('aria-checked','false',timeout=30000)
                await expect(control).to_be_enabled(timeout=30000)
                return {'tool':name,'status':response.status}
            try:
                # The author's latest change is expected to fix sequential stale windows.
                await reset([])
                contexts,pages,errors=await windows()
                try:
                    await toggle(pages[0],'chat');first=await state();assert first==['chat']
                    await expect(pages[1].locator('[data-mcp-tool="chat"] [role="switch"]')).to_have_attribute('aria-checked','true')
                    await toggle(pages[1],'cancel');final=await state();names=await listed()
                    passed=final==['cancel','chat'] and 'chat' not in names and 'cancel' not in names
                    report['sequential']={'status':'PASS' if passed else 'FAIL','after_A':first,'after_B':final,'expected_disabled':['cancel','chat'],'chat_advertised':'chat' in names,'cancel_advertised':'cancel' in names,'uncaught_js_errors':errors}
                    assert not errors,'uncaught JavaScript errors in sequential scenario'
                finally:
                    for context in contexts:await context.close()
                for index in range(args.rounds):
                    await reset([])
                    contexts,pages,errors=await windows()
                    barrier=asyncio.Event();reads={};puts=[];events=[];route_errors=[];zero=time.monotonic()
                    def record(kind,label,**facts):events.append({'event':kind,'window':label,'elapsed_ms':round((time.monotonic()-zero)*1000,2),**facts})
                    def handler_for(label):
                        async def handler(route):
                            try:
                                request=route.request
                                if request.method=='GET':
                                    assert label not in reads,'unexpected second GET in racing save'
                                    # Real request first; no mutation or fabricated response body.
                                    response=await route.fetch()
                                    assert response.status==200,'actual fresh GET failed'
                                    data=await response.json()
                                    disabled=sorted(t['name'] for t in data['tools'] if not t['enabled'])
                                    reads[label]={'status':response.status,'disabled':disabled,'tool_count':len(data['tools'])}
                                    record('actual_GET_completed_and_held',label,disabled=disabled)
                                    if len(reads)==2:barrier.set()
                                    await asyncio.wait_for(barrier.wait(),timeout=20)
                                    assert reads['A']['disabled']==reads['B']['disabled']==[],'barrier did not capture two real all-on states'
                                    record('unchanged_GET_delivered',label)
                                    await route.fulfill(response=response)
                                elif request.method=='PUT':
                                    payload=request.post_data_json
                                    puts.append({'window':label,'payload':payload})
                                    record('frontend_PUT_sent_to_actual_server',label,disabled_tools=payload.get('disabled_tools'))
                                    await route.continue_()
                                else:await route.continue_()
                            except Exception as exc:
                                route_errors.append(scrub(exc));barrier.set()
                                await route.abort('failed')
                        return handler
                    try:
                        for label,page in zip(('A','B'),pages):
                            await page.route('**/api/settings/mcp-access',handler_for(label))
                        save_results=await asyncio.wait_for(asyncio.gather(toggle(pages[0],'chat',False),toggle(pages[1],'cancel',False)),timeout=45)
                        assert not route_errors,'route scheduling error: '+str(route_errors)
                        assert set(reads)=={'A','B'} and len(puts)==2,'missing fresh GET or frontend PUT'
                        final=await state();names=await listed()
                        passed=final==['cancel','chat'] and 'chat' not in names and 'cancel' not in names
                        report['concurrent_rounds'].append({'round':index+1,'status':'PASS' if passed else 'FAIL','assertion':'Overlapping saves of different tool switches must preserve both disabled choices','actual_GETs':reads,'actual_frontend_PUTs':puts,'actual_PUT_responses':save_results,'events':events,'expected_disabled':['cancel','chat'],'actual_disabled':final,'chat_advertised':'chat' in names,'cancel_advertised':'cancel' in names,'uncaught_js_errors':errors,'real_responses_delayed_only':True})
                        assert not errors,'uncaught JavaScript errors in race scenario'
                    finally:
                        barrier.set()
                        for context in contexts:await context.close()
                report['server_identity_unchanged']=started==identity(side)
                assert report['server_identity_unchanged'],'server identity changed'
                report['status']='PASS' if report['sequential']['status']=='PASS' and all(r['status']=='PASS' for r in report['concurrent_rounds']) else 'FAIL'
            except Exception as exc:
                report['status']='HARNESS_ERROR';report['error']=scrub(exc)
            finally:
                await reset(['chat']);report['restored_final_disabled']=await state()
                await browser.close()
    filename='concurrent-save-'+(args.channel or args.engine)+'.json'
    (target if target.suffix=='.json' else out/filename).write_text(json.dumps(report,indent=2)+'\n')
    print(json.dumps(report,indent=2))
    return 0 if report['status']=='PASS' else 1 if report['status']=='FAIL' else 2

if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('--config',required=True,type=Path);parser.add_argument('--out',required=True,type=Path);parser.add_argument('--engine',choices=['chromium','firefox','webkit'],default='chromium');parser.add_argument('--channel',choices=['chrome','msedge']);parser.add_argument('--rounds',type=int,default=3);args=parser.parse_args()
    if not 1<=args.rounds<=10:parser.error('--rounds must be 1..10')
    try:sys.exit(asyncio.run(main(args)))
    except Exception as exc:print('HARNESS_ERROR '+scrub(exc),file=sys.stderr);sys.exit(2)
