/* Local selected-file listening only: no server audio storage or autoplay. */
'use strict';
class ScrunchMixListening {
 constructor(audio,status,urls=URL,timers={setTimeout:(fn,ms)=>globalThis.setTimeout(fn,ms),clearTimeout:key=>globalThis.clearTimeout(key)}){this.audio=audio;this.status=status;this.urls=urls;this.timers=timers;this.files=new Map();this.url=null;this.timer=null;this.generation=0;audio.onended=()=>{if(this.url){this.stop();this.status.textContent='Reached the end of the selected bounce.';}};audio.onerror=()=>{if(this.url){this.stop();this.status.textContent='The selected audio could not play. Your measurements are unchanged.';}};}
 remember(report,file){this.files.set(report.id,{file,duration:report.durationSeconds});while(this.files.size>2)this.files.delete(this.files.keys().next().value);}
 async rememberMatching(report,file,digest=bytes=>crypto.subtle.digest('SHA-256',bytes)){
  const generation=this.generation;
  const hash=Array.from(new Uint8Array(await digest(await file.arrayBuffer())),v=>v.toString(16).padStart(2,'0')).join('');
  if(generation!==this.generation||hash!==report.audioHash)return false;
  this.remember(report,file);return true;
 }
 available(id){return this.files.has(id);}
 stop(){this.generation++;if(this.timer!==null)this.timers.clearTimeout(this.timer);this.timer=null;this.audio.pause();this.audio.removeAttribute('src');this.audio.load();if(this.url)this.urls.revokeObjectURL(this.url);this.url=null;}
 async play(id,seconds,volume=.35){
  this.stop();const item=this.files.get(id),generation=this.generation;
  if(!item||!Number.isFinite(seconds)||seconds<0||seconds>=item.duration){this.status.textContent='Choose and analyse this bounce on this page before listening. No saved audio is available.';return false;}
  this.url=this.urls.createObjectURL(item.file);this.audio.src=this.url;this.audio.volume=Number.isFinite(volume)?Math.max(0,Math.min(.6,volume)):.35;this.audio.currentTime=seconds;
  this.status.textContent=`Playing up to eight seconds from ${Math.floor(seconds/60)}:${String(Math.floor(seconds%60)).padStart(2,'0')} on this Mac. Original file level; not a loudness-matched comparison.`;
  try{await this.audio.play();if(this.generation!==generation)return false;this.timer=this.timers.setTimeout(()=>{this.stop();this.status.textContent='Stopped. Select another moment to listen.';},Math.min(8,item.duration-seconds)*1000);return true;}
  catch(error){if(this.generation===generation){this.status.setAttribute?.('data-playback-error',String(error.name)+': '+String(error.message));this.stop();this.status.textContent='This browser could not play the selected audio. Your measurements are unchanged.';}return false;}
 }
 clear(){this.stop();this.files.clear();this.status.textContent='Local listening cleared. Your original audio file is unchanged.';}
}
if(typeof module!=='undefined'&&module.exports)module.exports=ScrunchMixListening;
else window.ScrunchMixListening=ScrunchMixListening;
