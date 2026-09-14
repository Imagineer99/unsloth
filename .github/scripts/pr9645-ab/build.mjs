// SPDX-License-Identifier: AGPL-3.0-only
import {build} from 'esbuild';
import {execFileSync} from 'node:child_process';
import {readFileSync,writeFileSync,mkdirSync} from 'node:fs';
import path from 'node:path';
import {fileURLToPath} from 'node:url';
const here=path.dirname(fileURLToPath(import.meta.url));
const root=path.resolve(here,'../../..');
const frontend=path.join(root,'studio/frontend');
const base='a6cf5962b788c6a195e618d2ba0b55cb0855f7d4';
const pr='67b1a18aedf18766bec0931176c3d482a5b4e97c';
const file='studio/frontend/src/components/assistant-ui/streaming-render-schedule.ts';
const git=(...args)=>execFileSync('git',args,{cwd:root,encoding:'utf8'}).trim();
git('merge-base','--is-ancestor',base,'HEAD');
if(process.argv.includes('--pending-merge')){
 if(process.env.CI)throw Error('Pending merge mode is local preflight only');
 if(git('rev-parse','HEAD')!==base||git('rev-parse','MERGE_HEAD')!==pr)throw Error('Unexpected pending merge parents');
}else git('merge-base','--is-ancestor',pr,'HEAD');
const deps=['studio/frontend/package.json','studio/frontend/package-lock.json'];
if(git('diff',base,'HEAD','--',...deps))throw Error('A/B dependency trees differ');
const out=path.join(here,'results');mkdirSync(out,{recursive:true});
writeFileSync(path.join(out,'provenance.json'),JSON.stringify({base,pr,staging:git('rev-parse','HEAD'),node:process.version,platform:process.platform,changed:git('diff','--name-only',base,'HEAD')},null,2));
for(const variant of ['base','pr']){
 const source=variant==='base'?execFileSync('git',['show',`${base}:${file}`],{cwd:root,encoding:'utf8'}):readFileSync(path.join(root,file),'utf8');
 const temp=path.join(frontend,`pr9645-${variant}.ts`);writeFileSync(temp,source);
 await build({stdin:{contents:readFileSync(path.join(here,'entry.tsx'),'utf8').replace('__SCHEDULER__',`./pr9645-${variant}.ts`),resolveDir:frontend,loader:'tsx'},bundle:true,platform:'browser',format:'iife',outfile:path.join(here,`${variant}.js`)});
}
console.log('SETUP_OK pinned current-main A and merged-PR B built with identical dependencies');
