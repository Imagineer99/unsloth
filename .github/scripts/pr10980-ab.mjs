// SPDX-License-Identifier: AGPL-3.0-only
// Targeted production-function A/B. No model/network transport is executed.
import assert from 'node:assert/strict';
import {readFileSync, mkdirSync, writeFileSync} from 'node:fs';
import {resolve} from 'node:path';
import {pathToFileURL} from 'node:url';
import {createRequire} from 'node:module';
import {execFileSync} from 'node:child_process';

const [baseArg,headArg,outArg]=process.argv.slice(2);
if(!baseArg||!headArg||!outArg) throw new Error('Usage: node pr10980-ab.mjs BASE_TREE HEAD_TREE OUTPUT_DIR');
const roots={base:resolve(baseArg),head:resolve(headArg)};
const out=resolve(outArg);mkdirSync(out,{recursive:true});
const expected={base:'1811677f024a88b6cbe6d36e7d3d91b27c2c0190',head:'564e9d2ff48fb343cc701a501ad04fc56df3efb7'};
const require=createRequire(`${roots.head}/studio/frontend/package.json`);
const ts=require('typescript');
const rows=[];
async function check(side,name,fn){try{const facts=await fn();rows.push({side,name,pass:true,...facts});}catch(error){rows.push({side,name,pass:false,error:String(error),actual:error.actual,expected:error.expected});}console.log(JSON.stringify(rows.at(-1)));}
const transpile=code=>ts.transpileModule(code,{compilerOptions:{target:ts.ScriptTarget.ES2022,module:ts.ModuleKind.None}}).outputText;
for(const side of ['base','head']){
  const tree=roots[side];
  assert.equal(execFileSync('git',['rev-parse','HEAD'],{cwd:tree,encoding:'utf8'}).trim(),expected[side]);
  const load=p=>import(pathToFileURL(`${tree}/studio/frontend/src/features/chat/${p}`).href);
  const boundary=await load('utils/prompt-queue-model-boundary.ts');
  const {chatModelLifecycleGate:gate}=await load('utils/model-lifecycle-gate.ts');
  const {snapshotQueuedChatRunSettings}=await load('utils/queued-chat-run-settings.ts');
  const {parseExternalModelId}=await load('external-providers.ts');
  const stop=await load('utils/prompt-queue-user-stop.ts');
  const source=ts.createSourceFile('thread.tsx',readFileSync(`${tree}/studio/frontend/src/components/assistant-ui/thread.tsx`,'utf8'),ts.ScriptTarget.Latest,true,ts.ScriptKind.TSX);
  const functions=['startPromptQueue','steerPromptQueueTarget','steerPromptQueueItem','findPromptQueueRunByItemId','pausePromptQueueRun','resumePromptQueueRun','getPromptQueueRunsForThreadIds','getActivePromptQueueItem','createQueuedPrompt','getPromptQueueTargetIds','getPromptQueueRunTargetIds','promptQueueRunMatchesThreadIds','findPromptQueueRunByTarget','findPromptQueueRunByThreadIds','isPromptQueueTargetRunning','isActivePromptQueueItem','dispatchQueuedPrompt','isPromptQueueRunReadyToDispatch','handlePromptQueueRunState','isPromptQueueRunTargetRunning','advancePromptQueue','handlePromptQueueRunFailed','retainPendingPromptQueueItemsAfterFailure'];
  const declarations=source.statements.filter(n=>ts.isFunctionDeclaration(n)&&functions.includes(n.name?.text)).map(n=>n.getText(source));
  const js=transpile(declarations.join('\n'));
  function world(){
    const runs=new Map(),appended=[],noop=()=>{};let serial=0,modelLoading=false;
    const deps={promptQueueRuns:runs,promptQueueRunOrder:[],promptQueueActiveRunIds:new Set(),promptQueueDispatchingRunIds:new Set(),compactIds:ids=>[...new Set(ids.filter(Boolean))],createPromptQueueRunId:()=>`r${++serial}`,createPromptQueueItemId:()=>`i${++serial}`,...stop,steeringInsertionIndex:(items,index)=>Math.min(items.length,Math.max(0,index)+(items[Math.max(0,index)]?.dispatched?1:0)),cancelPreStreamRunForThreadIds:noop,useChatRuntimeStore:{getState:()=>({runningByThreadId:{},modelLoading})},syncPromptQueueUI:noop,ensurePromptQueueSubscription:noop,requestPromptQueuePump:noop,requestPromptQueuePumpIfReady:noop,clearPromptQueueRetryTimer:noop,schedulePromptQueueTargetStatePoll:noop,scheduleQueuedPromptDispatch:noop,PROMPT_QUEUE_DISPATCH_RETRY_MS:500,PROMPT_QUEUE_INDEXING_RETRY_MS:500,deletePromptQueueRun:run=>runs.delete(run.id),toast:{info:noop},discardQueuedChatRunSettingsForThread:noop,targetHasIndexingDocuments:async()=>false,appendQueuedPrompt:(_run,item)=>{appended.push(item.prompt);item.dispatched=true;}};
    const engine=new Function(...Object.keys(deps),`${js}\nreturn {startPromptQueue,dispatchQueuedPrompt,handlePromptQueueRunState,handlePromptQueueRunFailed,resumePromptQueueRun,isPromptQueueRunReadyToDispatch};`)(...Object.values(deps));
    return {...engine,runs,appended,setLoading:v=>modelLoading=v,run:()=>[...runs.values()][0]};
  }
  function target(id,local=true,running=false){return {usesLocalModel:local,running,getRunningThreadIds:()=>[id],getDocumentThreadId:()=>id,isRunning(){return this.running;},researchStarted:()=>false,cancelActiveRun(){this.running=false;},cancel(){this.running=false;},complete(){},consumeDeepResearch(){}};}
  let factory;
  function visit(n){if(ts.isVariableDeclaration(n)&&n.name.getText(source)==='createPromptQueueTarget')factory=n.initializer.arguments[0];ts.forEachChild(n,visit);}visit(source);assert.ok(factory);
  async function queuedTarget(checkpoint,incoming){
    let snapshot;
    const state={params:{checkpoint,temperature:.4},activeGgufVariant:null,loadingModelPick:incoming?{id:incoming}:null,modelLoading:true,ragEnabled:false,incognito:false,hydratePersistedSettings:async()=>{}};
    const deps={aui:{threads:()=>({}),threadListItem:()=>({getState:()=>({id:'chat',remoteId:'chat'})})},referenceThreadId:'chat',chatHistoryClearBoundary:{capture:()=>0},promptQueueTargetMountedRef:{current:true},indexingActiveRef:{current:false},useChatRuntimeStore:{getState:()=>state},compactIds:ids=>[...new Set(ids.filter(Boolean))],snapshotQueuedChatRunSettings:(...args)=>(snapshot=snapshotQueuedChatRunSettings(...args)),parseExternalModelId,hasPreStreamRunReservation:()=>false};
    const result=await new Function(...Object.keys(deps),transpile(`return (${factory.getText(source)});`))(...Object.values(deps))();
    return {result,snapshot};
  }
  await check(side,'loading acceptance and ordered dispatch',async()=>{
    const lease=gate.tryAcquire('loading');assert.notEqual(lease,null);
    try{
      const w=world(),t=target('chat',true,true);w.setLoading(true);
      const prompts=['first','second','third'];
      const accepted=prompts.filter(()=>!boundary.shouldAbortPendingQueueForModelBoundary({capturedGeneration:boundary.localPromptQueueModelBoundary.capture(),usesLocalModel:true,modelLoading:true}));
      assert.equal(accepted.length,side==='head'?3:0);
      if(accepted.length){
        w.startPromptQueue(accepted,t,true);const run=w.run();
        await w.dispatchQueuedPrompt(run,run.items[0]);assert.deepEqual(w.appended,[]);
        w.setLoading(false);w.handlePromptQueueRunState(run,{});assert.equal(run.index,-1);
        t.running=false;w.handlePromptQueueRunState(run,{});
        for(const prompt of prompts){const item=run.items[run.index];assert.equal(item.prompt,prompt);await w.dispatchQueuedPrompt(run,item);t.running=true;w.handlePromptQueueRunState(run,{});t.running=false;w.handlePromptQueueRunState(run,{});}
        assert.deepEqual(w.appended,prompts);assert.equal(w.runs.size,0);
      }
      return {accepted:accepted.length,appended:w.appended,negativeControl:side==='base'};
    }finally{gate.release(lease);}
  });
  await check(side,'new hosted selection during local load preserves its provider',async()=>{
    const selected='external::new-provider::explicitly-selected-model';
    const {result,snapshot}=await queuedTarget(selected,'incoming-local');
    assert.equal(snapshot.params.checkpoint,selected,'Explicit hosted selection must remain the queued model');
    assert.equal(result.usesLocalModel,false);
    return {queuedCheckpoint:snapshot.params.checkpoint,usesLocalModel:result.usesLocalModel};
  });
  await check(side,'initial load failure retains accepted follow-ups',async()=>{
    const w=world();w.startPromptQueue(['recover one','recover two'],target('chat'),true);
    w.handlePromptQueueRunFailed('chat');
    assert.equal(w.runs.size,side==='head'?1:0);
    if(side==='head'){assert.equal(w.run().paused,true);assert.deepEqual(w.run().items.map(i=>i.prompt),['recover one','recover two']);w.resumePromptQueueRun(['chat']);await w.dispatchQueuedPrompt(w.run(),w.run().items[0]);assert.deepEqual(w.appended,['recover one']);}
    return {retained:side==='head'?2:0,negativeControl:side==='base'};
  });
  if(side==='head'){
    await check(side,'local failure blocks local work and preserves external work',async()=>{
      const w=world();w.setLoading(true);w.startPromptQueue(['local next'],target('local'));w.startPromptQueue(['hosted next'],target('hosted',false));
      const [local,external]=[...w.runs.values()];w.handlePromptQueueRunFailed(undefined,true);w.setLoading(false);
      await w.dispatchQueuedPrompt(local,local.items[0]);assert.deepEqual(w.appended,[]);
      await w.dispatchQueuedPrompt(external,external.items[0]);assert.deepEqual(w.appended,['hosted next']);
      w.resumePromptQueueRun(['local']);await w.dispatchQueuedPrompt(local,local.items[0]);assert.deepEqual(w.appended,['hosted next','local next']);
      return {appended:w.appended};
    });
    await check(side,'outgoing hosted to incoming local still defers',async()=>{
      const {result,snapshot}=await queuedTarget('external::outgoing::model','incoming-local');
      assert.equal(snapshot.params.checkpoint,'');assert.equal(result.usesLocalModel,true);return {usesLocalModel:true,queuedCheckpoint:''};
    });
  }
}
const report={revisions:expected,scope:'Actual production functions; controlled stores, scheduling and transport; no inference',results:rows};
writeFileSync(`${out}/ab-results.json`,JSON.stringify(report,null,2)+'\n');
process.exitCode=rows.some(r=>!r.pass)?1:0;
