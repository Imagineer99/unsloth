// SPDX-License-Identifier: AGPL-3.0-only
import React from 'react';
import {createRoot} from 'react-dom/client';
import {flushSync} from 'react-dom';
import {Streamdown} from 'streamdown';
import * as scheduler from '__SCHEDULER__';
const root=createRoot(document.getElementById('root')!);
const components={a:({node,...props}:any)=><a {...props}/>};
let cache:any;
(window as any).reset=()=>{cache=new scheduler.IncrementalMarkdownCache();flushSync(()=>root.render(null));};
(window as any).show=(text:string)=>{
 const r=cache.update(text);
 const key=scheduler.markdownRenderKey(text);
 flushSync(()=>root.render(<Streamdown key={cache.renderGeneration+':'+key} mode="streaming" components={components} animated={{duration:0,stagger:0}} parseIncompleteMarkdown={false} parseMarkdownIntoBlocksFn={r.parseMarkdownIntoBlocks} isAnimating>{r.markdown}</Streamdown>));
 return {key,scope:scheduler.markdownRenderScope(text)};
};
