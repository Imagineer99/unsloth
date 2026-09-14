// SPDX-License-Identifier: AGPL-3.0-only
import {chromium,firefox,webkit} from 'playwright';
import {readFileSync,writeFileSync,mkdirSync} from 'node:fs';
import {fileURLToPath} from 'node:url';
import path from 'node:path';
const here=path.dirname(fileURLToPath(import.meta.url));
const variant=process.argv[2];
if(!['base','pr'].includes(variant))throw Error('Expected base or pr');
const names=(process.env.BROWSERS||'chromium,firefox,webkit').split(',');
const report={variant,node:process.version,platform:process.platform,browsers:[],cases:[],errors:[]};
mkdirSync(path.join(here,'results'),{recursive:true});
for(const name of names){
 let browser;
 try{
  const engine={chromium,firefox,webkit,chrome:chromium,msedge:chromium}[name];
  browser=await engine.launch({headless:true,...(['chrome','msedge'].includes(name)?{channel:name}:{})});
  report.browsers.push({name,version:browser.version()});
  const page=await browser.newPage();
  page.on('pageerror',e=>report.errors.push({name,error:String(e)}));
  await page.setContent('<div id="root"></div>');await page.addScriptTag({content:readFileSync(path.join(here,variant+'.js'),'utf8')});
  for(const eol of ['\n','\r\n','\r'])for(const prefix of ['', '> '])for(const chunks of ['suffix-together','suffix-separate'])for(const kind of ['ordinary','escaped-angle']){
   await page.evaluate(()=>window.reset());
   const start=`See [g][g].\n\n${prefix}[g]: <https://example.com/a${kind==='escaped-angle'?'\\>':''}`.replaceAll('\n',eol);
   const states=chunks==='suffix-together'?[start,start+'b>']:[start,start+'b',start+'b>'];
   const keys=[];
   for(const text of states){
    keys.push((await page.evaluate(text=>window.show(text),text)).key);
    await page.evaluate(()=>new Promise(resolve=>requestAnimationFrame(()=>requestAnimationFrame(resolve))));
    await page.waitForTimeout(30);
   }
   const expected=kind==='escaped-angle'?'https://example.com/a%3Eb':'https://example.com/ab';
   // Poll actual DOM. A setup error cannot satisfy this assertion.
   try{await page.waitForFunction(href=>[...document.querySelectorAll('a')].some(a=>a.textContent==='g'&&a.getAttribute('href')===href),expected,{timeout:800});}catch(e){if(e.name!=='TimeoutError')throw e;}
   const links=await page.locator('a').evaluateAll(as=>as.map(a=>({href:a.getAttribute('href'),text:a.textContent})));
   const pass=links.some(a=>a.text==='g'&&a.href===expected);
   const result={name,kind,eol:JSON.stringify(eol),prefix,chunks,pass,expected,keys,links,body:await page.locator('#root').innerText()};
   report.cases.push(result);console.log(`${pass?'PASS':'FAIL'} ${variant} ${name} ${kind} ${JSON.stringify({eol,prefix,chunks})}`);
  }
 }catch(e){report.errors.push({name,error:String(e)});console.error('SETUP_OR_RUNTIME_ERROR',name,String(e));}
 finally{await browser?.close();}
}
report.failed=report.cases.filter(c=>!c.pass).length;
report.status=report.errors.length?'setup_or_runtime_error':report.failed?'assertion_failure':'pass';
writeFileSync(path.join(here,'results',variant+'.json'),JSON.stringify(report,null,2));
console.log(JSON.stringify({variant,status:report.status,cases:report.cases.length,failed:report.failed,errors:report.errors.length}));
process.exitCode=report.errors.length?2:report.failed?1:0;
