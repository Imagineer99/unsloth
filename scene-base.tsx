import React,{useEffect,useState,useRef,useCallback} from "react";
import {createRoot} from "react-dom/client";
import {createPortal} from "react-dom";
import "../../src/index.css";
import {cn} from "../../src/lib/utils";
import {TooltipProvider} from "../../src/components/ui/tooltip";
import {Sidebar,SidebarProvider,SidebarHeader,SidebarContent,SidebarFooter,useSidebar} from "../../src/components/ui/sidebar";
import {useSidebarPin} from "../../src/hooks/use-sidebar-pin";
import {WindowTitlebar} from "../../src/components/tauri/window-titlebar";
import {BrowserPanel} from "../../src/features/browser/browser-panel";
import {useBrowserStore} from "../../src/features/browser/store";
useBrowserStore.getState().newTab();
const scene=new URLSearchParams(location.search).get('scene')||'titlebar';
function Controls(){const sidebar=useSidebar();useEffect(()=>{window.setPeeking=sidebar.setPeeking},[sidebar.setPeeking]);return null;}
function FooterScene(){
 const scrollRef=useRef<HTMLDivElement|null>(null),fadeRef=useRef<HTMLDivElement|null>(null);
 const showUpdateCard=false;
   const syncFade = useCallback((el: HTMLDivElement) => {
    const fade = fadeRef.current;
    if (!fade) return;
    const hidden = el.scrollHeight - el.scrollTop - el.clientHeight;
    const visible = String(hidden > parseFloat(getComputedStyle(el).paddingBottom) + 1);
    if (fade.dataset.visible !== visible) fade.dataset.visible = visible;
  }, []);

 useEffect(()=>{if(scrollRef.current)syncFade(scrollRef.current);window.scrollList=(bottom:boolean)=>{const el=scrollRef.current!;el.scrollTop=bottom?el.scrollHeight:0;syncFade(el)}},[syncFade]);
 return <><SidebarContent ref={scrollRef} onScroll={e=>syncFade(e.currentTarget)} className="gap-0 overflow-y-auto min-h-0 pb-2">
 {Array.from({length:30},(_,i)=><div key={i} style={{height:40,minHeight:40,padding:'10px 18px',fontSize:14,whiteSpace:'nowrap',overflow:'hidden',textOverflow:'ellipsis'}}>Example conversation {String(i+1).padStart(2,'0')}</div>)}
 </SidebarContent><SidebarFooter className="relative p-3" style={{height:66,minHeight:66}}>
         <div
          ref={fadeRef}
          aria-hidden="true"
          className={cn(
            // from-[8px] holds the fade opaque across the hard clip, matching the list's pb-2.
            "pointer-events-none absolute start-0 end-[var(--sidebar-rail,0px)] bottom-full bg-gradient-to-t from-[var(--sidebar-surface)] from-[8px] to-[rgb(from_var(--sidebar-surface)_r_g_b/0)] transition-opacity duration-200",
            showUpdateCard ? "h-7" : "h-10",
            // data-visible is written by syncFade, not React.
            "opacity-0 data-[visible=true]:opacity-100",
          )}
        />
 <div style={{display:'flex',alignItems:'center',gap:12}}><div style={{background:'#12b58e',borderRadius:'50%',width:36,height:36,display:'grid',placeItems:'center',color:'#fff'}}>U</div><div><strong>Unsloth</strong><div style={{fontSize:12,opacity:.65}}>Example profile</div></div><span style={{marginLeft:'auto',fontSize:20}}>⚙</span></div>
 </SidebarFooter></>;
}
function App(){
 const pin=useSidebarPin();const [tone,modal]=useState<string|null>(null);
 useEffect(()=>{window.setPinned=pin.setPinned;window.modal=modal;window.documentMode=(enabled:boolean)=>{const state=useBrowserStore.getState();state.updateTab(state.activeTabId!,{documentType:enabled?'application/pdf':null})}},[pin.setPinned]);
 return <TooltipProvider><SidebarProvider {...pin} data-wallpaper-workspace="" className="relative h-screen overflow-hidden">
 <div className="chat-wallpaper" data-ready="" style={{background:scene==='browser'?'linear-gradient(135deg,#285c52,#294a67)':'#28685d'}}/>
 <Sidebar collapseToZero collapsible="icon"><SidebarHeader className="relative px-5" style={{paddingTop:50,paddingBottom:12}}><h2 style={{fontSize:21,fontWeight:600}}>Unsloth</h2>{scene!=='sidebar'&&<><p>New chat</p><p>Model hub</p><p>Library</p></>}</SidebarHeader>{scene==='sidebar'&&<FooterScene/>}</Sidebar>
 <Controls/><main style={{position:'relative',padding:'86px 32px'}}><h2 style={{fontSize:17}}>Example conversation</h2><p style={{marginTop:16,opacity:.65}}>Controlled component scene</p></main>
 {scene==='browser'&&<div id="browser-host" style={{position:'absolute',left:450,right:0,top:0,bottom:0}}><BrowserPanel active={false}/></div>}
 </SidebarProvider><WindowTitlebar showSidebarSurface/>
 {tone&&createPortal(<><div data-slot="dialog-overlay" data-viewport-backdrop="true" data-viewport-backdrop-tone={tone==='normal'?undefined:tone} data-state="open" style={{position:'fixed',inset:0,zIndex:50,pointerEvents:'none',background:`rgb(0 0 0 / ${tone==='strong'?.4:tone==='dense'?.7:.3})`}}/><div role="dialog" aria-label="Settings" style={{position:'fixed',top:240,left:340,width:480,padding:24,borderRadius:24,background:'var(--background)',zIndex:60}}><h2>Settings</h2><p style={{opacity:.65,marginTop:12}}>Appearance</p></div></>,document.body)}
 </TooltipProvider>
}
createRoot(document.getElementById('root')!).render(<App/>);
