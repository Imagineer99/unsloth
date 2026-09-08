// SPDX-License-Identifier: AGPL-3.0-only
// Copyright 2026-present the Unsloth AI Inc. team. All rights reserved.
import { register } from 'node:module';
import { pathToFileURL } from 'node:url';
const root = process.argv[2];
register(pathToFileURL(`${root}/studio/frontend/tests/bundler-resolver.mjs`));
const { exampleModelOptions, resolveExampleModel } = await import(pathToFileURL(`${root}/studio/frontend/src/features/settings/lib/example-model.ts`));
let total=0, failures=[];
for (const kind of ['nonGGUF','standaloneGGUF','withheld','legacyGGUF','multiGGUF']) {
 for (const loaded of [false,true]) for (const keylessOnly of [false,true]) for (const autoSwitch of [false,true]) for(const selected of [false,true]) {
  const row={id:'org/model',loaded};
  if(kind==='withheld')Object.assign(row,{quant:'Q4_K_M',quants:[]});
  if(kind==='legacyGGUF')Object.assign(row,{quant:'Q4_K_M'});
  if(kind==='multiGGUF')Object.assign(row,{quant:'Q4_K_M',quants:['Q4_K_M','Q8_0']});
  const catalog=[row];
  const input={catalog,options:exampleModelOptions(catalog),keylessOnly,autoSwitch,checkpoint:null,ggufVariant:null,picked:selected?'org/model':null};
  total++;
  const expected=loaded || (!keylessOnly && autoSwitch);
  try {
    const result=resolveExampleModel(input);
    if(result.servable!==expected) failures.push({kind,loaded,keylessOnly,autoSwitch,selected,expected,result});
  }catch(e){ failures.push({kind,loaded,keylessOnly,autoSwitch,selected,expected,error:String(e)}); }
 }
}
console.log(JSON.stringify({total,passed:total-failures.length,failures},null,2));
process.exitCode=failures.length?1:0;
