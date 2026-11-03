const fs=require('node:fs'),path=require('node:path'),http=require('node:http'),assert=require('node:assert/strict');
const {chromium}=require('playwright');
const root=path.resolve(__dirname),manifest=JSON.parse(fs.readFileSync(path.join(root,'manifest.json'))),plans=JSON.parse(fs.readFileSync(path.join(root,'scene-plan.json')));
async function serve(name){const dir=path.join(root,`build-${name}`);const server=http.createServer((req,res)=>{const rel=decodeURIComponent(new URL(req.url,'http://localhost').pathname);const file=path.resolve(dir,'.'+rel);if(!file.startsWith(dir+path.sep)||!fs.existsSync(file)||!fs.statSync(file).isFile()){res.writeHead(404);res.end();return;}const types={'.html':'text/html','.js':'text/javascript','.css':'text/css','.json':'application/json','.woff2':'font/woff2','.svg':'image/svg+xml','.png':'image/png'};res.setHeader('Content-Type',types[path.extname(file)]||'application/octet-stream');fs.createReadStream(file).pipe(res)});await new Promise(r=>server.listen(0,'127.0.0.1',r));return {server,port:server.address().port,dir};}
(async()=>{const servers={};const browser=await chromium.launch({executablePath:process.env.CHROME_EXECUTABLE||undefined,headless:true});
try{
 for(const n of ['base','browser','titlebar','sidebar']) servers[n]=await serve(n);
 for(const scene of ['browser','titlebar','sidebar']){
  if(process.argv.length>2&&!process.argv.slice(2).includes(scene))continue;
  const output=path.join(root,scene);fs.mkdirSync(output,{recursive:true});
  const report={...plans[scene],base:manifest.base,head:manifest.scenes[scene].head,environment:{browser:await browser.version(),platform:process.platform,viewport:{width:1000,height:600},deviceScaleFactor:1},sides:{},cases:[]};
  for(const side of ['before','after']){
   const build=side==='before'?'base':scene,{port,dir}=servers[build],origin=`http://127.0.0.1:${port}`;
   const page=await browser.newPage({viewport:{width:1000,height:600},deviceScaleFactor:1});const errors=[];page.on('pageerror',e=>errors.push(e.message));
   await page.route('**/*',r=>new URL(r.request().url()).origin===origin?r.continue():r.abort());
   const provenance=await (await page.request.get(`${origin}/provenance.json`)).json();assert.equal(provenance.sha,side==='before'?report.base:report.head);report.sides[side]={...provenance,port,buildRoot:dir};
   await page.goto(`${origin}/work/glass-pr-evidence/index.html?scene=${scene}`);await page.waitForFunction(()=>window.setPinned&&window.modal&&window.setPeeking);
   if(scene==='sidebar') await page.waitForFunction(()=>window.scrollList);if(scene==='browser')await page.locator('.browser-tablist').waitFor();
   await page.evaluate(async()=>{await Promise.all([...new Set([...document.querySelectorAll('button,span,p,h2,div')].map(e=>getComputedStyle(e).font))].filter(Boolean).map(f=>document.fonts.load(f)));await document.fonts.ready});await page.locator('input').evaluateAll(es=>es.forEach(e=>e.blur()));
   const configs=[];
   if(scene==='titlebar')for(const dark of [true,false])for(const material of ['frosted','liquid-3d'])for(const mode of ['pinned','collapsed','peeking'])for(const tone of [null,'normal','strong','dense'])configs.push({dark,material,mode,tone});
   if(scene==='browser')for(const dark of [true,false])for(const material of ['frosted','liquid-3d',null])for(const documentMode of [false,true])configs.push({dark,material,mode:'pinned',tone:null,documentMode});
   if(scene==='sidebar')for(const dark of [true,false])for(const material of ['frosted','liquid','liquid-3d','macos',null])for(const bottom of [false,true])configs.push({dark,material,mode:'pinned',tone:null,bottom});
   for(const cfg of configs){
    await page.evaluate(c=>{const h=document.documentElement;h.classList.toggle('dark',c.dark);if(c.material)h.setAttribute('data-glass',c.material);else h.removeAttribute('data-glass');window.setPinned(c.mode==='pinned');window.setPeeking(c.mode==='peeking');window.modal(c.tone);if(c.documentMode!==undefined)window.documentMode(c.documentMode);if(c.bottom!==undefined)window.scrollList(c.bottom)},cfg);await page.waitForTimeout(250);
    const facts=await page.evaluate(scene=>{
     const bar=document.querySelector('[data-slot="window-titlebar"]'),nav=bar.querySelector('[data-wallpaper-chrome-controls]'),sidebar=document.querySelector('[data-slot="sidebar-inner"]'),button=nav.querySelector('button'),br=button.getBoundingClientRect();
     const result={navBackground:getComputedStyle(nav).backgroundColor,navBlur:getComputedStyle(nav).backdropFilter,navOpacity:getComputedStyle(nav).opacity,titlebarOverlay:getComputedStyle(bar,'::after').backgroundColor,sidebarTop:sidebar.getBoundingClientRect().top,controlsOpacity:getComputedStyle(bar.querySelector('[role="toolbar"][aria-label="Window controls"]')).opacity,navClickable:button.contains(document.elementFromPoint(br.x+br.width/2,br.y+br.height/2))};
     if(scene==='browser'){const panel=document.querySelector('#browser-host section'),body=panel.querySelector('.browser-panel-body'),before=getComputedStyle(panel,'::before'),tab=panel.querySelector('[role="tab"]'),rect=tab.getBoundingClientRect();Object.assign(result,{panelBackground:getComputedStyle(panel).backgroundColor,bodyBackground:getComputedStyle(body).backgroundColor,panelBlur:getComputedStyle(panel).backdropFilter,paintBlur:before.backdropFilter,paintTop:before.top,paintPointerEvents:before.pointerEvents,pageBackground:getComputedStyle(panel.querySelector('[data-browser-page]')).backgroundColor,document:panel.hasAttribute('data-browser-document'),tabClickable:tab.contains(document.elementFromPoint(rect.x+rect.width/2,rect.y+rect.height/2))});}
     if(scene==='sidebar'){const footer=document.querySelector('[data-slot="sidebar-footer"]'),fade=footer.querySelector('[aria-hidden="true"]'),scroller=document.querySelector('[data-slot="sidebar-content"]');Object.assign(result,{fadeDisplay:getComputedStyle(fade).display,fadeOpacity:getComputedStyle(fade).opacity,fadeVisible:fade.dataset.visible,footerTop:footer.getBoundingClientRect().top,scrollTop:scroller.scrollTop,hiddenRowsHeight:scroller.scrollHeight-scroller.clientHeight-scroller.scrollTop});}
     return result;
    },scene);
    assert.equal(facts.sidebarTop,0);assert.equal(facts.controlsOpacity,'1');assert(facts.navClickable);
    if(scene==='titlebar'&&side==='after'){assert.equal(facts.navBackground,'rgba(0, 0, 0, 0)');assert.equal(facts.navBlur,'none');assert.equal(facts.titlebarOverlay,'rgba(0, 0, 0, 0)');assert.equal(Number(facts.navOpacity),cfg.tone==='strong'?.6:cfg.tone==='dense'?.3:cfg.tone==='normal'?.7:1);}
    if(scene==='browser'){assert(facts.tabClickable);assert.equal(facts.panelBlur,'none');if(side==='after'&&cfg.material){assert.equal(facts.panelBackground,'rgba(0, 0, 0, 0)');assert.match(facts.paintBlur,/blur/);assert.equal(facts.paintTop,'34px');assert.equal(facts.paintPointerEvents,'none');assert.equal(facts.bodyBackground==='rgba(0, 0, 0, 0)',!cfg.documentMode);}if(!cfg.documentMode)assert.notEqual(facts.pageBackground,'rgba(0, 0, 0, 0)');}
    if(scene==='sidebar'){assert.equal(facts.fadeDisplay,side==='after'&&cfg.material?'none':'block');assert.equal(facts.fadeVisible,cfg.bottom?'false':'true');if(!cfg.material)assert.equal(facts.fadeOpacity,cfg.bottom?'0':'1');}
    const suffix=`${cfg.dark?'dark':'light'}-${cfg.material||'solid'}-${cfg.mode}-${cfg.tone||'none'}${scene==='browser'?'-'+(cfg.documentMode?'document':'web'):scene==='sidebar'?'-'+(cfg.bottom?'bottom':'overflow'):''}`;
    const file=`${side}-${suffix}.png`;await page.screenshot({path:path.join(output,file)});report.cases.push({side,file,...cfg,...facts});
   }
   assert.deepEqual(errors,[]);await page.close();
  }
  fs.writeFileSync(path.join(output,'meta.json'),JSON.stringify(report,null,2));console.log(`${scene}: ${report.cases.length} states passed`);
 }
}finally{await browser.close();for(const s of Object.values(servers))await new Promise(r=>s.server.close(r));}
})().catch(e=>{console.error(e);process.exit(1)});
