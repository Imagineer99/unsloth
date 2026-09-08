// SPDX-License-Identifier: AGPL-3.0-only
// Copyright 2026-present the Unsloth AI Inc. team. All rights reserved.
import {useEffect, useState, type CSSProperties} from 'react';
import {createRoot} from 'react-dom/client';
import {invoke} from '@tauri-apps/api/core';
import {AppearanceTab} from './src/features/settings/tabs/appearance-tab';
import {Navbar} from './src/components/navbar';
import {SidebarProvider, SidebarInset} from './src/components/ui/sidebar';
import {applyCustomizationToDocument, useAppearanceCustomStore} from './src/features/settings/stores/appearance-custom-store';
import {useTheme, setTheme} from './src/features/settings/stores/theme-store';
import './src/index.css';
/* SIDE_IMPORTS */
const SIDE = /* SIDE_MANIFEST */;
/* PRODUCTION_MAC_STYLE */
/* PRODUCTION_EFFECT */
const delay = (ms: number) => new Promise(resolve => setTimeout(resolve, ms));
async function until(check: () => boolean | Promise<boolean>, message: string) {
  for (let i=0; i<100; i++) {if (await check()) return; await delay(100);}
  throw new Error(message);
}
const assert = (condition: unknown, message: string) => {if (!condition) throw new Error(message);};
let setScene: (scene: string) => void;
function Scene() {
  const [scene, update] = useState('appearance'); setScene = update;
  return <><AppearanceCustomizationEffect/><div className="h-dvh" style={MAC_NATIVE_CHROME_STYLE}>
    {scene === 'appearance' ? <main data-probe-scene="appearance" className="h-full overflow-auto" style={{padding:'calc(var(--studio-mac-titlebar-height,34px) + 24px) 24px 24px'}}><AppearanceTab/></main>
    : <SidebarProvider pinned={false} setPinned={()=>{}} togglePinned={()=>{}} className="h-[calc(100dvh-var(--studio-titlebar-height,0px))] min-h-0 overflow-hidden"><SidebarInset><Navbar/><div data-probe-scene="toolbar" className="pt-14 md:pt-0"> </div></SidebarInset></SidebarProvider>}
  </div></>;
}
async function show(scene: string) {
  setScene(scene); await until(()=>!!document.querySelector(`[data-probe-scene="${scene}"]`),'Scene did not render');
  await document.fonts.ready; await delay(250);
}
const scaleInput = () => document.querySelector<HTMLInputElement>('input[aria-label="Interface scale"]');
async function zoom(expected: number) {
  await until(async()=>{
    const native:any = await invoke('native_geometry');
    return Math.abs(native.page_zoom-expected)<0.001 && Math.abs(innerWidth*expected-native.view_width)<2;
  }, 'Native zoom or viewport did not match requested scale');
}
async function changeScale(value: number) {
  await show('appearance');
  const input=scaleInput(); assert(input,'Interface scale missing');
  input!.focus();
  Object.getOwnPropertyDescriptor(HTMLInputElement.prototype,'value')!.set!.call(input,String(value));
  input!.dispatchEvent(new Event('input',{bubbles:true}));
  await delay(30); input!.blur();
  await until(()=>scaleInput()?.value===String(value),'Scale input did not commit');
  await zoom(value/100);
}
function facts() {
  const button=document.querySelector('[data-sidebar="trigger"], button[aria-label="Expand sidebar"]');
  const rect=button?.getBoundingClientRect();
  return {side:SIDE.side,sha:SIDE.sha,scene:document.querySelector('[data-probe-scene]')?.getAttribute('data-probe-scene'),
    viewport:{width:innerWidth,height:innerHeight,dpr:devicePixelRatio},
    scale_control:!!scaleInput(),input:scaleInput()?.value??null,
    stored_scale:localStorage.getItem('unsloth_interface_scale'),
    sidebar_button:rect?{x:rect.x,y:rect.y,width:rect.width,height:rect.height}:null};
}
async function shot(label: string) {await delay(200); return invoke('capture',{label,facts:facts()});}
async function run() {
  await until(()=>!!document.querySelector('[data-probe-scene]'),'Initial scene missing');
  if (sessionStorage.getItem('pr9666-phase')==='reload') {
    await zoom(1.25); assert(scaleInput()?.value==='125','Reload did not restore 125%');
    await shot('reload125');
    const reset=Array.from(document.querySelectorAll('button')).find(b=>b.textContent?.trim()==='Reset customization');
    assert(reset,'Reset customization missing'); reset!.click(); await zoom(1);
    await until(()=>scaleInput()?.value==='100','Reset input incorrect');
    await shot('reset100'); await show('toolbar'); await shot('toolbar100');
    await invoke('finish',{result:{status:'complete',side:SIDE.side,sha:SIDE.sha,reload125:true,reset100:true}});return;
  }
  await zoom(1); assert(!!scaleInput()===(SIDE.side==='head'),'Incorrect control visibility');
  await shot('appearance100'); await show('toolbar'); await shot('toolbar100');
  if (SIDE.side==='base') {
    await invoke('finish',{result:{status:'complete',side:SIDE.side,sha:SIDE.sha}});return;
  }
  for(const pct of [125,50,200]) {
    await changeScale(pct); await shot(`appearance${pct}`); await show('toolbar'); await shot(`toolbar${pct}`);
  }
  await changeScale(125); sessionStorage.setItem('pr9666-phase','reload'); location.reload();
}
async function boot() {
  setTheme('light');
  /* PRODUCTION_SCALE_BOOT */
  createRoot(document.getElementById('root')!).render(<Scene/>);
  await run();
}
boot().catch(async error=>{
  await invoke('finish',{result:{status:'harness_or_feature_failure',side:SIDE.side,sha:SIDE.sha,error:String(error)}});
});
