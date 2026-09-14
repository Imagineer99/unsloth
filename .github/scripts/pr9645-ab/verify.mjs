// SPDX-License-Identifier: AGPL-3.0-only
import assert from 'node:assert/strict';
import {readFileSync,appendFileSync} from 'node:fs';
const read=name=>JSON.parse(readFileSync(new URL('./results/'+name+'.json',import.meta.url),'utf8'));
const a=read('base'),b=read('pr');
const names=(process.env.BROWSERS||'chromium,firefox,webkit').split(',');
assert.equal(a.errors.length,0);assert.equal(b.errors.length,0);
assert.equal(a.status,'pass');assert.equal(b.status,'assertion_failure');
assert.equal(a.cases.length,names.length*24);assert.equal(b.cases.length,a.cases.length);
for(const name of names){
 assert.equal(a.cases.filter(c=>c.name===name&&!c.pass).length,0);
 assert.equal(b.cases.filter(c=>c.name===name&&c.kind==='ordinary'&&!c.pass).length,0);
 assert.equal(b.cases.filter(c=>c.name===name&&c.kind==='escaped-angle'&&!c.pass).length,12);
}
const summary=`CONFIRMED: current-main A passes ${a.cases.length}/${a.cases.length}; merged PR B fails ${b.failed} escaped-angle assertions; all ordinary-link controls pass. No setup/runtime errors.\n`;
console.log(summary);if(process.env.GITHUB_STEP_SUMMARY)appendFileSync(process.env.GITHUB_STEP_SUMMARY,summary);
