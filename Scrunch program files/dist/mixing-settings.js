/* Standalone Mixing preferences. No generation, playback or automatic paid retry. */
'use strict';
const el=id=>document.getElementById(id);
let state=null,busy=false;
function status(id,words){el(id).textContent=words;}
function lock(value){busy=value;document.querySelectorAll('button').forEach(b=>b.disabled=value);}
function providerFields(){el('endpoint-label').hidden=el('provider-kind').value!=='compatible';}
function selectProfile(){
 const p=state?.providers.find(p=>p.id===el('profile').value);
 for(const [id,k] of [['profile-name','name'],['model','model'],['endpoint','endpoint']])el(id).value=p?.[k]||'';
 el('provider-kind').value=p?.kind||'openai';el('key').value='';providerFields();
 status('profile-status',p?.hasKey?'Key saved in this Mac’s Keychain. Leave blank to keep it.':'Create a profile using your own provider account.');
}
function setupReadiness(saved){
 const profiles=saved.providers||[];
 const engineer=profiles.find(p=>p.id===saved.engineerProvider);
 const chat=!!engineer&&(engineer.kind==='compatible'||engineer.hasKey===true);
 const voice=saved.voiceSettings?.hasKey===true;
 const microphone=saved.voiceEngine==='openai'?voice:saved.voiceEngine==='gemini'&&profiles.some(p=>p.kind==='gemini'&&p.hasKey===true);
 return [
  ['Track measurements',true,'Work locally without an AI key.'],
  ['AI conversation',chat,chat?'Configured; send your first question in Mixing.':'Save a provider profile with its key, then select it for Mixing.'],
  ['Speak your question',!!microphone,microphone?'Transcription is configured; press Speak in Mixing.':'Choose OpenAI with a saved voice key, or Gemini with a saved Gemini key.'],
  ['Spoken replies',voice,voice?'Voice is configured; enable Voice replies in Mixing.':'Save an OpenAI voice key below.']
 ];
}
function renderReadiness(){
 const box=el('setup-readiness');box.replaceChildren();
 for(const [name,ready,detail] of setupReadiness(state)){
  const row=document.createElement('p');row.textContent=`${ready?'✓':'○'} ${name}: ${detail}`;box.append(row);
 }
}
async function refresh(){
 const r=await fetch('/api/state');if(!r.ok)throw Error('Settings could not reconnect. Reload before saving.');
 state=await r.json();el('profile').replaceChildren(new Option('New profile',''));el('engineer').replaceChildren();
 for(const p of state.providers||[]){el('profile').add(new Option(p.name,p.id));el('engineer').add(new Option(`${p.name} · ${p.model}`,p.id));}
 el('engineer').value=state.engineerProvider||state.providers?.[0]?.id||'';
 el('choose-engineer').disabled=!state.providers?.length;
 el('voice').value=state.voiceSettings?.voice||'marin';
 el('transcriber').value=state.voiceEngine==='gemini'?'gemini':'openai';
 status('voice-status',state.voiceSettings?.hasKey?'A voice key is saved on this Mac.':'No voice key saved. Local bounce measurements are ready without it.');
 renderReadiness();
}
async function post(path,body){
 const r=await fetch(path,{method:'POST',headers:{'Content-Type':'application/json','X-Scrunch-Token':state.token},body:JSON.stringify(body)});
 const result=await r.json();if(!r.ok)throw Error(result.error||'Settings were not saved.');return result;
}
async function save(id,fn){
 if(busy||!state)return;lock(true);status(id,'Saving…');
 try{await fn();}catch(e){status(id,e.message+' No automatic retry was made.');}
 finally{lock(false);el('choose-engineer').disabled=!state?.providers?.length;el('key').value='';el('voice-key').value='';}
}
el('provider-kind').onchange=providerFields;el('profile').onchange=selectProfile;
el('profile-form').onsubmit=e=>{e.preventDefault();save('profile-status',async()=>{
 const saved=await post('/api/provider',{id:el('profile').value,name:el('profile-name').value,kind:el('provider-kind').value,model:el('model').value,endpoint:el('endpoint').value,key:el('key').value});
 await refresh();el('profile').value=saved.id;selectProfile();status('profile-status','Profile saved. Choose it below for Mixing. No question was sent.');
});};
el('engineer-form').onsubmit=e=>{e.preventDefault();save('engineer-status',async()=>{await post('/api/mixing-demo/provider',{id:el('engineer').value});await refresh();status('engineer-status','Mixing engineer selected.');});};
el('voice-form').onsubmit=e=>{e.preventDefault();save('voice-status',async()=>{await post('/api/voice-settings',{service:'openai',voice:el('voice').value,key:el('voice-key').value});await refresh();status('voice-status','Voice settings saved. Switch Voice replies on in Mixing when wanted.');});};
el('transcriber-form').onsubmit=e=>{e.preventDefault();save('transcriber-status',async()=>{await post('/api/voice-settings',{transcriber:el('transcriber').value});await refresh();status('transcriber-status','Transcription service selected. Press Speak in Mixing to record a question.');});};
window.addEventListener('pagehide',()=>{el('key').value='';el('voice-key').value='';});
refresh().catch(e=>status('profile-status',e.message));

el('check-midi').onclick=async()=>{
 const button=el('check-midi');button.disabled=true;status('midi-readiness','Checking installed models…');
 try{
  const r=await fetch('/api/mixing-demo/readiness');const result=await r.json();if(!r.ok)throw Error(result.error||'Readiness check unavailable.');
  const midi=result.midi;if(!midi||typeof midi.audioToMidi!=='boolean'||typeof midi.humToMidi!=='boolean')throw Error('MIDI readiness is not reported by this installation.');
  status('midi-readiness',`Audio → MIDI: ${midi.audioToMidi?'model files installed':'not installed; run setup to install the MIDI model'}. Humming → MIDI: ${midi.humToMidi?'runtime installed':'not installed; run setup'}. ${midi.basis||'Presence check only; a transcription has not been tested.'}`);
 }catch(e){status('midi-readiness',e.message);}
 finally{button.disabled=false;}
};
