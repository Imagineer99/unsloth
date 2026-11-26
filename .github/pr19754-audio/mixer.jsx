import React from "react";
import {createRoot} from "react-dom/client";
import {StemMixer} from "./src/features/audio/components/stem-mixer.tsx";
async function main(){
 const stems=await Promise.all(["vocals","bass","drums","other"].map(async(role,i)=>({
   role, label:role, clipId:role,src:URL.createObjectURL(await(await fetch(i+".wav")).blob()),
   durationS:180,peaks:[.1,.7,.3,.5]
 })));
 createRoot(document.getElementById("root")).render(<StemMixer active groupId="fixture" title="Separation" stems={stems} sendTargets={[]} onSend={()=>{}} onDownloadStem={()=>{}} onDownloadAll={()=>{}}/>);
}
main();
