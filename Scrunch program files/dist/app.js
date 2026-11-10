// Producer views read a completed comparison; navigation never initiates DSP.
function bindTabs(pairs){const buttons=pairs.map(([b])=>document.getElementById(b));function select(i){pairs.forEach(([b,p],j)=>{const el=document.getElementById(b);el.setAttribute('aria-selected',String(i===j));el.tabIndex=i===j?0:-1;document.getElementById(p).hidden=i!==j;});}buttons.forEach((b,i)=>{b.onclick=()=>select(i);b.onkeydown=e=>{let n;if(e.key==='ArrowRight')n=(i+1)%buttons.length;else if(e.key==='ArrowLeft')n=(i+buttons.length-1)%buttons.length;else if(e.key==='Home')n=0;else if(e.key==='End')n=buttons.length-1;else return;e.preventDefault();select(n);buttons[n].focus();};});select(0);}
bindTabs([['tabEngineer','engineerWorkspace'],['tabAnalytics','analyticsWorkspace']]);
const analytics=document.querySelector('.reference-workspace'),panels={},nav=document.createElement('nav');nav.className='analytics-tabs';nav.setAttribute('role','tablist');nav.setAttribute('aria-label','Audio analysis views');
const sections=[['setup','A/B Listening'],['spectrum','Spectrum'],['stereo','Stereo'],['metering','Metering']];
for(const [id,label] of sections){const b=document.createElement('button');b.id='analyticsTab-'+id;b.textContent=label;b.setAttribute('role','tab');b.setAttribute('aria-controls','analyticsPanel-'+id);nav.append(b);const p=document.createElement('section');p.id='analyticsPanel-'+id;p.setAttribute('role','tabpanel');p.setAttribute('aria-labelledby',b.id);panels[id]=p;}
const children=Array.from(analytics.children),referenceStatusElement=document.getElementById('refStatus'),technicalDetails=document.getElementById('refTechnical').parentElement;
const deep=document.createElement('details');deep.className='deep-analysis';const sum=document.createElement('summary');sum.textContent='Deep Analysis · capture processed Live output';deep.append(sum);
let beforeFiles=true;
for(const el of children){if(el.tagName==='H2'||el===referenceStatusElement||el===technicalDetails)continue;if(el.id==='refEnglish'){el.hidden=true;technicalDetails.append(el);}else if(el.id==='refSpectrum')panels.spectrum.append(el);else if(beforeFiles && !el.contains(document.getElementById('refFiles'))){deep.append(el);}else{beforeFiles=false;panels.setup.append(el);}}
panels.setup.prepend(deep);
const drop=document.createElement('div');drop.id='referenceDrop';drop.className='reference-drop';drop.textContent='Drop one or two WAV / AIFF references here';panels.setup.insertBefore(drop,deep.nextSibling);
analytics.append(...Object.values(panels));
const playback=document.getElementById('refPlayA').parentElement;playback.classList.add('ab-switches');const wave=document.getElementById('refWave');wave.height=280;panels.setup.insertBefore(wave,deep);panels.setup.insertBefore(playback,deep);
const note=document.createElement('p');note.className='hint';note.textContent='Deep Analysis: captured cues and matched playback. LIVE EARS: current rolling input, labelled separately.';analytics.append(nav,note,referenceStatusElement,playback,document.getElementById("refPlayback"),...Object.values(panels),technicalDetails);
bindTabs(sections.map(([id])=>['analyticsTab-'+id,'analyticsPanel-'+id]));
const modes=document.createElement('div');modes.className='row';let spectrumMode='both',analyticsReport=null;
for(const [mode,label] of [['live','A only'],['reference','B only'],['both','A + B']]){const b=document.createElement('button');b.textContent=label;b.setAttribute('aria-pressed',String(mode===spectrumMode));b.onclick=()=>{spectrumMode=mode;for(const other of modes.children)other.setAttribute('aria-pressed',String(other===b));if(analyticsReport)drawReference(analyticsReport);};modes.append(b);}panels.spectrum.prepend(modes);
for(const [id,label] of [['stereo','Stereo spread and correlation by frequency'],['metering','A and B loudness and dynamics']]){const c=document.createElement('canvas');c.id='analytics-'+id;c.hidden=true;c.width=1000;c.height=id==='stereo'?420:480;c.setAttribute('aria-label',label);panels[id].append(c);}
function renderAnalytics(r){analyticsReport=r;document.getElementById('analytics-stereo').hidden=false;document.getElementById('analytics-metering').hidden=false;drawStereo(r);drawMeters(r);}
function drawStereo(r){const c=document.getElementById('analytics-stereo'),x=c.getContext('2d');x.clearRect(0,0,c.width,c.height);x.font='13px sans-serif';x.fillStyle='#c9d0d1';x.fillText('Side-energy spread by frequency · centre = mono; edges = side-dominant',20,22);const bands=r.matched.live.bands;for(let i=0;i<bands.length;i++){const xx=65+i*(870/bands.length),w=800/bands.length;for(const [j,side,color] of [[0,'live','#ed913d'],[1,'reference','#66b9db']]){const b=r.matched[side].bands[i],cy=112+j*155;x.strokeStyle='#41494a';x.beginPath();x.moveTo(xx,cy-55);x.lineTo(xx,cy+55);x.stroke();if(b.sideFraction!==null){const h=Math.sqrt(b.sideFraction)*55;x.fillStyle=color;x.globalAlpha=.7;x.fillRect(xx-w*.32,cy-h,w*.64,Math.max(2,h*2));x.globalAlpha=1;}x.fillStyle='#c9d0d1';x.textAlign='center';x.fillText((b.lowHz<1000?b.lowHz:b.lowHz/1000+'k')+'–'+(b.highHz<1000?b.highHz:b.highHz/1000+'k'),xx,cy+78);if(b.correlation!==null){x.fillStyle=b.correlation<0?'#e77373':color;x.fillRect(xx-w*.32,cy+88,w*.64*(b.correlation+1)/2,5);}}}x.textAlign='left';x.fillStyle='#ed913d';x.fillText('A',20,115);x.fillStyle='#66b9db';x.fillText('B',20,270);x.fillStyle='#c9d0d1';x.fillText('Small bars: correlation −1 to +1. Red indicates negative correlation. Snapshot of selected cues.',20,405);}
function drawMeters(r){const c=document.getElementById('analytics-metering'),x=c.getContext('2d');x.clearRect(0,0,c.width,c.height);x.font='14px sans-serif';const audioA=document.getElementById('refAudioA'),audioB=document.getElementById('refAudioB'),playing=!audioA.paused?audioA:!audioB.paused?audioB:null;const t=playing?playing.currentTime:r.durationSeconds;for(const [j,side,color] of [[0,'live','#ed913d'],[1,'reference','#66b9db']]){const m=r.matched[side],hist=m.loudnessHistory;const h=hist.filter(h=>h.seconds<=t).at(-1)||{};const rows=[['Integrated',m.integratedLUFS,'LUFS'],['Short-term',h.shortTermLUFS,'LUFS'],['Momentary',h.momentaryLUFS,'LUFS'],['True peak (whole cue)',m.truePeakDbTP,'dBTP'],['RMS left',m.rmsDbFS[0],'dBFS'],['RMS right',m.rmsDbFS[1],'dBFS'],['Crest (whole cue)',m.crestFactorDb,'dB']];const ox=25+j*500;x.fillStyle=color;x.fillText(j?'B · Reference':'A · Live',ox,25);rows.forEach(([label,v,unit],i)=>{const y=65+i*39;x.fillStyle='#c9d0d1';x.fillText(label,ox,y);x.fillStyle='#333b3c';x.fillRect(ox+185,y-13,190,15);if(typeof v==='number'){x.fillStyle=color;x.fillRect(ox+185,y-13,190*Math.max(0,Math.min(1,(label.startsWith('Crest')?v/24:(v+60)/60))),15);}x.fillStyle='#eee';x.fillText(typeof v==='number'?v.toFixed(1)+' '+unit:'Unavailable',ox+385,y);});x.strokeStyle=color;x.beginPath();let start=true;for(const p of hist){if(p.shortTermLUFS===null)continue;const xx=ox+(p.seconds/r.durationSeconds)*450,yy=415-Math.max(0,Math.min(60,p.shortTermLUFS+60));if(start){x.moveTo(xx,yy);start=false;}else x.lineTo(xx,yy);}x.stroke();x.strokeStyle='#eee';const cursor=ox+450*t/r.durationSeconds;x.beginPath();x.moveTo(cursor,345);x.lineTo(cursor,420);x.stroke();}x.fillStyle='#c9d0d1';x.fillText((playing?'Playback cursor':'End-of-cue snapshot')+' · '+t.toFixed(2)+' s · short-term history above; not a live input meter',25,458);}
setInterval(()=>{if(analyticsReport&&!panels.metering.hidden)drawMeters(analyticsReport);},100);

const $=id=>document.getElementById(id);let state,token,selected=null,busy=false;
$('engineerMode').value='act';localStorage.setItem('scrunchEngineerMode','act');
$('engineerMode').hidden=true;
document.querySelector('label[for="engineerMode"]').hidden=true;
const workLabel=node('label','Work on'),workSelect=document.createElement('select');workSelect.id='engineerWorkspaceChoice';
// Always visible and explicit (John, 27 Sep): no Automatic, so a note never lands in the wrong engineer unseen.
workSelect.add(new Option('Mixing','mix'));workSelect.add(new Option('Sound design','sound'));workSelect.add(new Option('Session and MIDI','session'));
workSelect.value=['mix','sound','session'].includes(localStorage.getItem('scrunchWorkspace'))?localStorage.getItem('scrunchWorkspace'):'mix';workLabel.append(workSelect);
const learnLabel=node('label',''),learnInput=document.createElement('input');learnInput.type='checkbox';learnInput.id='engineerLearn';
learnLabel.className='sound-learn';workLabel.className='sound-workspace';
learnInput.checked=localStorage.getItem('scrunchLearn')==='1';learnLabel.append(learnInput,document.createTextNode(' Teaching mode'));
$('engineerInput').before(workLabel,learnLabel);
const soundButtons=node('div','','row');soundButtons.id='soundActions';
// Revert | Wrong way | Go | Save | ?. Revert and Save act at once; the rest go to the AI engineer, which decides.
for(const [label,words,cls,tip] of [['Revert','revert','sound-revert','Undo that change'],
  ['Wrong way','wrong way','sound-wrongway','Go the other direction'],
  ['Go','go','sound-go','Keep going, or try a new idea after Revert'],
  ['Save',null,'sound-save','Save this sound as a preset'],
  ['?','explain','sound-explain','Explain what changed and why']]){
 const b=button(label,()=>{if(words)return sendEngineer(words,'sound',words==='revert'?'revert':undefined);const name=prompt('Save this sound as:');if(name&&name.trim())return sendEngineer('save this sound as '+name.trim(),'sound','save');});
 b.className=cls;b.title=tip;b.setAttribute('aria-label',tip);soundButtons.append(b);
}
$('engineerInput').after(soundButtons);
const presetStatus=node('output','','hint');presetStatus.id='presetScanStatus';presetStatus.setAttribute('role','status');presetStatus.hidden=true;
soundButtons.before(presetStatus);
const keepPreset=button('Keep',()=>sendEngineer('keep this preset','sound','keep-preset'));keepPreset.id='keepPreset';keepPreset.title='Stop scanning and keep this preset';keepPreset.hidden=true;soundButtons.prepend(keepPreset);
async function pollPresetScan(){
 try{
  const response=await fetch('/api/preset-scan');if(!response.ok)return;
  const scan=await response.json();keepPreset.hidden=!scan.scanning;presetStatus.hidden=!scan.scanning&&!scan.error;
  presetStatus.textContent=scan.error?'Preset scan stopped: '+scan.error:scan.scanning?[scan.track,scan.preset||'Scanning presets'].filter(Boolean).join(' · '):'';
 }catch(e){presetStatus.textContent='Preset scan status unavailable.';presetStatus.hidden=false;}
 finally{setTimeout(pollPresetScan,1000);}
}
pollPresetScan();
function soundWorkspace(){const sound=workSelect.value==='sound';soundButtons.hidden=!sound;learnLabel.hidden=!sound;
 if(sound&&!localStorage.getItem('scrunchSoundModeInitialized')){$('engineerMode').value='act';localStorage.setItem('scrunchSoundModeInitialized','1');localStorage.setItem('scrunchEngineerMode','act');}
 $('engineerInput').placeholder=workSelect.value==='mix'?'Mix note for the mix engineer (paste it here). Add bars or a time if it is about one place.':'Tell your engineer what you want: a track, a sound, the session.';}
workSelect.onchange=()=>{localStorage.setItem('scrunchWorkspace',workSelect.value);soundWorkspace();cancelReply();};
learnInput.onchange=()=>{localStorage.setItem('scrunchLearn',learnInput.checked?'1':'0');if(!learnInput.checked)cancelReply();};
soundWorkspace();
$('readReplies').checked=false;
$('readReplies').parentElement.lastChild.textContent=' Speak teaching summaries';
$('voiceCommands').closest('label').hidden=true;
function orb(mode,label){$('engineerOrb').dataset.state=mode;document.body.dataset.orb=mode;$('orbLabel').textContent=label;$('engineerOrb').style.setProperty('--voice',0);}
$('orbMotion').checked=localStorage.getItem('scrunchOrbMotion')!=='off';
function orbMotion(){$('engineerOrb').classList.toggle('still',!$('orbMotion').checked);localStorage.setItem('scrunchOrbMotion',$('orbMotion').checked?'on':'off');}
$('orbMotion').onchange=orbMotion;orbMotion();
const status=text=>$('status').textContent=text;
async function request(path,body){orb(path==='transcribe'?'working':path==='engineer'?'working':'connecting',path==='transcribe'?'Transcribing…':path==='engineer'?'Engineer working…':'Sending request…');const r=await fetch('/api/'+path,{method:'POST',headers:{'Content-Type':'application/json','X-Scrunch-Token':token},body:JSON.stringify(body)});const data=await r.json();if(!r.ok){orb('error','Request failed');throw Error(data.error||'Request failed');}orb(data.failed?'error':'done',data.failed?'Could not complete request':'Response received');return data;}
async function refresh(){state=await(await fetch('/api/state')).json();token=state.token;render();renderEngineerVariant();showEngineerReference(state.referenceTarget);if($('refRefine'))$('refRefine').checked=state.referenceRefine===true;}
function showEngineerReference(t){const el=$('refEngineerStatus');if(!el)return;el.textContent=t?`Engineer's reference: ${t.name}, from ${t.cueSeconds} s for ${t.durationSeconds} s (${t.part}). With Live playing, ask "make it sit like ${t.part}".`:'No reference set for the engineer.';}
function node(tag,text,cls){const n=document.createElement(tag);n.textContent=text;if(cls)n.className=cls;return n;}
function scrollEngineerToLatest(){requestAnimationFrame(()=>{if($('engineerWorkspace').hidden)return;const area=$('shellContent')||$('engineerChat');area.scrollTop=area.scrollHeight;});}
new MutationObserver(scrollEngineerToLatest).observe($('engineerChat'),{childList:true,subtree:true});
window.addEventListener('resize',scrollEngineerToLatest);
function button(text,fn){const b=node('button',text);b.onclick=()=>run(fn);return b;}
async function run(fn){if(busy)return;busy=true;document.querySelectorAll('button:not(#stop):not(#refStop):not(#closeSettings)').forEach(b=>b.disabled=true);try{await fn();await refresh();}catch(e){await refresh().catch(()=>{});orb('error','Request failed');status(e.message);$('settingsStatus').textContent=e.message;}finally{busy=false;document.querySelectorAll('button').forEach(b=>b.disabled=false);}}
function render(){const old=$('provider').value;const profiles=$('profiles').value;const engineerChoice=$('engineerModel').value;$('engineerModel').replaceChildren();for(const p of state.providers)$('engineerModel').add(new Option(p.name+' · '+p.model,p.id));$('engineerModel').value=engineerChoice||state.engineerProvider||state.providers[0]?.id||'';$('provider').replaceChildren(new Option('Choose a model',''));$('profiles').replaceChildren(new Option('New provider profile',''));for(const p of state.providers){$('provider').add(new Option(p.name+' · '+p.model,p.id));$('profiles').add(new Option(p.name,p.id));}$('provider').value=old||state.midiProvider||state.providers[0]?.id||'';$('profiles').value=profiles;$('memory').value=state.memory;
 const retainedProfile=$('inlineProfile');$('engineerChat').replaceChildren();for(const m of (state.engineerChat||[]))$('engineerChat').append(node('div',m.text,'message '+(m.role==='user'?'user':'')));if(!(state.engineerChat||[]).length)$('engineerChat').append(node('p','Tell me what you want to work on.'));if(retainedProfile)$('engineerChat').append(retainedProfile);$('engineerChat').scrollTop=$('engineerChat').scrollHeight;
 const src=state.source;$('sourceInfo').textContent=src?`${src.trackName} · ${src.notes.length} notes · ${src.length/4} bars · ${src.tempo} BPM`:'Your original stays the reference across every model and every take.';
 const attempts=state.attempts.filter(a=>a.source.hash===src?.hash&&(a.source.sessionId||'lab')===(src?.sessionId||'lab'));if(!attempts.some(a=>a.id===selected))selected=attempts.at(-1)?.id;
 $('takes').replaceChildren();for(const a of [...attempts].reverse()){const box=node('article','',`take ${a.id===selected?'selected':''}`);box.append(node('span',a.status,'tag'),node('h3',`Take ${attempts.indexOf(a)+1} · ${a.model}`),node('p',`${a.metrics.unchangedPercent}% note positions/pitches/lengths retained · ${a.metrics.noteCount} notes`));const actions=node('div','','row');actions.append(button('Show MIDI',async()=>{selected=a.id;render();$('roll').scrollIntoView({behavior:'smooth',block:'center'});}),button('Accept',()=>feedback(a,'accepted')),button('Reject',()=>feedback(a,'rejected')));box.append(actions);if(a.workflowError)box.append(node('p',a.workflowError));const link=node('a','Download MIDI');link.href='/midi/'+a.id;link.download='Scrunch-'+a.id.slice(0,6)+'.mid';box.append(document.createElement('br'),link);$('takes').append(box);}
 $('conversation').replaceChildren();for(const a of attempts.slice(-3)){ $('conversation').append(node('div',a.prompt,'message user'),node('div',a.reply,'message'));if(a.feedback)$('conversation').append(node('div',a.feedback,'message user'));}
 const current=attempts.find(a=>a.id===selected);$('comparison').textContent=current?`${current.metrics.unchangedPercent}% retained · ${current.status}`:'Fixed original reference';if(current?.metrics.warnings.length)$('conversation').append(node('div',current.metrics.warnings.join('\n'),'message'));draw(src?.notes||[],current?.notes||[],src?.length||16);
}
function draw(original,variation,length){const c=$('roll'),ctx=c.getContext('2d');ctx.clearRect(0,0,c.width,c.height);const all=[...original,...variation];const min=Math.min(48,...all.map(n=>n.p))-2,max=Math.max(60,...all.map(n=>n.p))+2;ctx.strokeStyle='#283236';for(let i=0;i<=length;i++){ctx.beginPath();ctx.moveTo(i/length*c.width,0);ctx.lineTo(i/length*c.width,c.height);ctx.stroke();}for(const [notes,color]of[[original,'#7b949f'],[variation,'#f39a45']]){ctx.fillStyle=color;ctx.globalAlpha=notes===original?.65:.9;for(const n of notes)ctx.fillRect(n.t/length*c.width,(max-n.p)/(max-min)*185+8,Math.max(3,n.d/length*c.width),5);}ctx.globalAlpha=1;}
async function feedback(a,statusValue){await request('feedback',{id:a.id,status:statusValue,feedback:$('prompt').value.trim()});status(statusValue==='accepted'?'Accepted. Your choice is saved.':'Rejected. Feedback stays with this take and follows model switches.');}
$('connect').onclick=()=>run(async()=>{const r=await request('connect',{});$('sessionConnection').textContent=r.name+' · '+r.tempo+' BPM';$('clips').replaceChildren(new Option('Choose an Arrangement clip',''));for(const c of r.clips)$('clips').add(new Option(`${c.trackIndex+1}: ${c.trackName} / ${c.name}`,c.id));const original=r.clips.find(c=>c.trackName.includes('Supersaw')&&c.name.includes('ORIGINAL'));if(original)$('clips').value=original.id;status('Connected. Choose the original phrase.');});
$('load').onclick=()=>run(async()=>{await request('source',{id:$('clips').value});selected=null;status('Original loaded. Tell your engineer what to change.');});
async function generate(mode){status(mode==='rehearsal'?'Running local rehearsal…':'Generating one take… no automatic paid retries.');const previous=state.attempts.filter(a=>a.source.hash===state.source?.hash&&(a.source.sessionId||'lab')===(state.source?.sessionId||'lab')).at(-1);const a=await request('generate-and-audition',{provider:$('provider').value,prompt:$('prompt').value||previous?.feedback||previous?.prompt||'Make a variation of the original.',mode});selected=a.id;$('prompt').value='';status('New take placed below the original and auditioned through Ableton. Accept to keep it, or Reject to replace it on the next generation.');}
$('generate').onclick=()=>run(()=>generate('provider'));$('rehearsal').onclick=()=>run(()=>generate('rehearsal'));
$('regenerate').onclick=()=>run(()=>generate('provider'));
$('original').onclick=()=>run(async()=>{await request('audition',{});status('Playing the original through its Ableton instrument.');});
$('variation').onclick=()=>run(async()=>{if(!selected)throw Error('Generate a variation first.');await request('audition',{id:selected});status('Playing the selected variation through its copied instrument.');});
$('stop').onclick=async()=>{cancelReply();try{await request('stop',{});status('Stopped. Previous track mute/solo settings restored.');}catch(e){status(e.message);}};
const voiceSection=document.createElement('section');
voiceSection.innerHTML=`<h2>Voice</h2><p>Choose Scrunch’s speaking voice. Your engineer stays the same.</p><label>Voice service<select id="voiceService"><option value="openai">OpenAI</option><option disabled>ElevenLabs — not connected yet</option></select></label><label>Voice API key<input id="voiceApiKey" type="password" autocomplete="off" placeholder="Paste your OpenAI key here" data-1p-ignore data-lpignore="true"></label><label>Voice<select id="cloudVoice"><option value="marin">Marin</option><option value="cedar">Cedar</option><option value="coral">Coral</option><option value="alloy">Alloy</option><option value="nova">Nova</option><option value="sage">Sage</option><option value="shimmer">Shimmer</option></select></label><button id="saveTestVoice" type="button">Save & Test Voice</button><p id="cloudVoiceStatus" role="status">Paste a key to get started. Saved securely on this Mac.</p><p>AI-generated speech. Testing plays a short greeting and uses your voice service credit.</p>`;
$('profileForm').after(voiceSection);
$('profileForm').before(voiceSection);
voiceSection.prepend($('profileForm').querySelector('.topline'));
voiceSection.querySelector('h2').textContent='Settings';
$('speechVoice').closest('label').hidden=true;
const keySavedStatus=document.createElement('p');keySavedStatus.setAttribute('role','status');
$('voiceApiKey').closest('label').after(keySavedStatus);
function showSavedVoiceKey(saved){
 keySavedStatus.textContent=saved?'Voice key saved on this Mac. The field stays blank to keep it private.':'No voice key saved yet.';
 $('voiceApiKey').placeholder=saved?'Key saved — paste here only to replace it':'Paste your OpenAI key here';
}
$('settingsButton').addEventListener('click',()=>showSavedVoiceKey(!!state.voiceSettings?.hasKey));
$('previewVoice').hidden=true;
$('settingsButton').onclick=()=>{const v=state.voiceSettings;if(v){$('cloudVoice').value=v.voice;$('cloudVoiceStatus').textContent=v.hasKey?'Key saved. Leave the key field blank to keep it.':'Paste your voice API key.';}$('settings').showModal();$('settings').scrollTop=0;};
$('closeSettings').onclick=()=>{$('voiceApiKey').value='';$('settings').close();};
$('saveTestVoice').onclick=async()=>{
 const button=$('saveTestVoice');button.disabled=true;
 try{
  $('cloudVoiceStatus').textContent='Saving voice settings…';
  await request('voice-settings',{service:$('voiceService').value,voice:$('cloudVoice').value,key:$('voiceApiKey').value});
  $('voiceApiKey').value='';
  showSavedVoiceKey(true);
  $('cloudVoiceStatus').textContent='Key saved. Creating your voice greeting…';
  await playVoice('Hello John. I’m your Scrunch voice. Let’s make some music.');
  $('cloudVoiceStatus').textContent='Voice test is playing.';
  await refresh();
 }catch(e){$('cloudVoiceStatus').textContent=e.message;}
 finally{$('voiceApiKey').value='';button.disabled=false;}
};
const geminiModels=[['gemini-3.1-pro-preview','Gemini 3.1 Pro (Preview)'],['gemini-3.8-flash','Gemini 3.8 Flash'],['gemini-2.5-flash','Gemini 2.5 Flash']];
const engineerVariantLabel=node('label','Gemini model'),engineerVariant=document.createElement('select');
engineerVariant.id='engineerVariant';engineerVariant.setAttribute('aria-label','Gemini model');engineerVariantLabel.append(engineerVariant);$('engineerModel').closest('label').after(engineerVariantLabel);
function renderEngineerVariant(){
 const p=state?.providers.find(p=>p.id===$('engineerModel').value);engineerVariantLabel.hidden=p?.kind!=='gemini';
 engineerVariant.replaceChildren();if(p?.kind!=='gemini')return;
 const choices=geminiModels.some(([id])=>id===p.model)?geminiModels:[[p.model,p.model],...geminiModels];
 for(const [id,label] of choices)engineerVariant.add(new Option(label,id));engineerVariant.value=p.model;
}
$('engineerModel').addEventListener('change',renderEngineerVariant);
engineerVariant.onchange=()=>run(async()=>{
 const p=state.providers.find(p=>p.id===$('engineerModel').value),model=engineerVariant.value;
 if(!p||p.kind!=='gemini')throw Error('Choose your saved Gemini provider first.');
 await request('provider',{id:p.id,name:p.name,kind:p.kind,model,endpoint:p.endpoint,key:''});
 await refresh();renderEngineerVariant();
 if($('profiles').value===p.id)$('model').value=model;
 $('settingsStatus').textContent='Saved '+model+'. Your existing Gemini key is retained.';
});
function providerFields(){const kind=$('kind').value;$('endpointField').hidden=kind!=='compatible';$('endpoint').disabled=kind!=='compatible';$('modelSuggestions').replaceChildren();if(kind==='gemini')for(const [id] of geminiModels)$('modelSuggestions').append(new Option(id,id));}
$('kind').onchange=providerFields;
providerFields();
$('profiles').onchange=()=>{const p=state.providers.find(p=>p.id===$('profiles').value);for(const [id,key]of[['profileName','name'],['kind','kind'],['model','model'],['endpoint','endpoint']])$(id).value=p?.[key]||(id==='kind'?'openai':'');$('key').value='';providerFields();$('settingsStatus').textContent=p?.hasKey?'A key is saved in this Mac’s Keychain.':'';};
$('profileForm').onsubmit=e=>{e.preventDefault();run(async()=>{const saved=await request('provider',{id:$('profiles').value,name:$('profileName').value,kind:$('kind').value,model:$('model').value,endpoint:$('endpoint').value,key:$('key').value});$('key').value='';await refresh();$('profiles').value=saved.id;$('provider').value=saved.id;$('settingsStatus').textContent=saved.hasKey?'Saved. Your API key is stored in this Mac’s Keychain.':'Profile saved. Add an API key before generating.';status('Provider ready for a live test.');});};
$('saveMemory').onclick=()=>run(async()=>{await request('memory',{memory:$('memory').value});$('settingsStatus').textContent='Preferences saved.';});
let lastTranscriptionMs=null;
let recording=null;
function updateVoiceMeter(r,level){
 $('engineerOrb').style.setProperty('--voice',Math.min(1,level*12));
 const heights=r.levels;heights.push(Math.max(3,Math.min(44,level*500)));heights.shift();
 Array.from($('voiceWave').children).forEach((bar,i)=>bar.style.height=heights[i]+'px');
 const seconds=Math.min(60,Math.floor((performance.now()-r.started)/1000));
 $('voiceElapsed').textContent=Math.floor(seconds/60)+':'+String(seconds%60).padStart(2,'0')+' / 1:00';
}
function hideVoiceMeter(){$('voiceMonitor').hidden=true;orb('ready','Ready');}

function wavBytes(chunks,rate){
 const count=chunks.reduce((n,c)=>n+c.length,0),buffer=new ArrayBuffer(44+count*2),v=new DataView(buffer);
 const str=(o,s)=>{for(let i=0;i<s.length;i++)v.setUint8(o+i,s.charCodeAt(i));};
 str(0,'RIFF');v.setUint32(4,36+count*2,true);str(8,'WAVE');str(12,'fmt ');v.setUint32(16,16,true);v.setUint16(20,1,true);v.setUint16(22,1,true);v.setUint32(24,rate,true);v.setUint32(28,rate*2,true);v.setUint16(32,2,true);v.setUint16(34,16,true);str(36,'data');v.setUint32(40,count*2,true);
 let offset=44;for(const c of chunks)for(const x of c){const value=Math.max(-1,Math.min(1,x));v.setInt16(offset,value<0?value*32768:value*32767,true);offset+=2;}
 return new Blob([buffer],{type:'audio/wav'});
}
let replyTimer=null,activeSpeech=null;
function cancelReply(){
 clearTimeout(replyTimer);
 if(activeSpeech?.kind==='audio'){activeSpeech.element.pause();activeSpeech.element.currentTime=0;}
 if(activeSpeech?.kind==='utterance'){window.speechSynthesis?.cancel();}
 activeSpeech=null;
 if($('engineerOrb').dataset.state==='speaking')orb('ready','Ready');
}
function trackSpeech(speech){
 const record={kind:'utterance',element:speech};
 activeSpeech=record;
 const current=()=>activeSpeech===record;
 speech.onstart=()=>{if(current())orb('speaking','Speaking');};
 speech.onresume=speech.onstart;
 speech.onpause=()=>{if(current()&&$('engineerOrb').dataset.state==='speaking')orb('ready','Speech paused');};
 speech.onend=()=>{if(current()){activeSpeech=null;if($('engineerOrb').dataset.state==='speaking')orb('ready','Ready');}};
 speech.onerror=()=>{if(current()){activeSpeech=null;orb('error','Speech interrupted');}};
}

function trackCloudSpeech(audio){
 const record={kind:'audio',element:audio};
 activeSpeech=record;
 const current=()=>activeSpeech===record;
 audio.onplay=()=>{if(current())orb('speaking','Speaking');};
 audio.onpause=()=>{if(current()&&$('engineerOrb').dataset.state==='speaking')orb('ready','Speech paused');};
 audio.onended=()=>{if(current()){activeSpeech=null;if($('engineerOrb').dataset.state==='speaking')orb('ready','Ready');}};
 audio.onerror=()=>{if(current()){activeSpeech=null;orb('error','Speech interrupted');}};
}

async function playVoice(text){
 const payload=await request('speak',{text,speed:Number($('speechRate').value)||1});
 if(!payload.audio)throw Error('Voice service returned no audio.');
 cancelReply();
 const audio=new Audio(`data:${payload.mime||'audio/mpeg'};base64,${payload.audio}`);
 audio.volume=0.7;trackCloudSpeech(audio);await audio.play();
}
async function speakReply(text,delay=0,providerId=''){
 cancelReply();
 if(!text||!$('readReplies').checked)return;
 const playCloud=async()=>{
 try{
    await playVoice(text);
  }catch(e){
    $('cloudVoiceStatus').textContent=e.message;
    status('Voice unavailable: '+e.message);
    return;
  }
 }
 replyTimer=setTimeout(playCloud,delay);
}
async function sendEngineer(text,workspace,soundButton){
 const sentAt=performance.now();
 text=text.trim();if(!text)throw Error('Tell your engineer what you want.');
 if($('engineerMode').value==='act'&&/^(stop|stop playback)[.!]?$/i.test(text)){cancelReply();await request('stop',{});status('Playback stopped.');return;}
 $('engineerChat').append(node('div',text,'message user'));const pending=node('div','Engineer working…','message pending');pending.setAttribute('role','status');$('engineerChat').append(pending);scrollEngineerToLatest();status('Your engineer is working…');
 cancelReply();
 const result=await request('engineer',{text,requestId:crypto.randomUUID(),engineerProvider:$('engineerModel').value,midiProvider:$('provider').value,selected,audioTrack:$('audioTracks').value,mode:$('engineerMode').value,workspace:workspace||workSelect.value,learn:learnInput.checked,button:soundButton});
 const timing=result.timings;if(timing)$('latencyInfo').textContent=(lastTranscriptionMs===null?'':`Voice ${(lastTranscriptionMs/1000).toFixed(1)}s · `)+`Interpret ${(timing.interpretationMs/1000).toFixed(1)}s (${timing.route}${timing.modelCalls?', '+timing.modelCalls+' AI':''}${timing.listenMs?', listen '+(timing.listenMs/1000).toFixed(1)+'s':''}) · Execute ${(timing.executionMs/1000).toFixed(1)}s · Request ${((performance.now()-sentAt)/1000).toFixed(1)}s`;lastTranscriptionMs=null;
 selected=result.selected;$('provider').value=result.midiProvider;if(!result.failed)$('engineerInput').value='';pending.textContent=result.text;pending.classList.remove('pending');scrollEngineerToLatest();
 status(result.playing?(result.seconds?'Auditioning one phrase.':'Loop playing until Stop.'):'');orb(result.failed?'error':'done',result.failed?'Needs your answer':result.playing?'Playing in Ableton':'Ready');
 const providerId=$('engineerModel').value;
 if(result.workspace==='sound'&&learnInput.checked&&result.speechText)speakReply(result.speechText,0,providerId);
}
$('sendEngineer').onclick=()=>run(()=>sendEngineer($('engineerInput').value));
$('engineerInput').onkeydown=e=>{if(e.key==='Enter'&&(e.metaKey||e.ctrlKey)){e.preventDefault();$('sendEngineer').click();}};
function loadVoices(){
 const choice=localStorage.getItem('scrunchSpeechVoice')||'';
 $('speechVoice').replaceChildren(new Option('System default',''));
 for(const v of window.speechSynthesis?.getVoices()||[])$('speechVoice').add(new Option(v.name+' · '+v.lang,v.voiceURI));
 $('speechVoice').value=choice;
}
$('speechRate').value=localStorage.getItem('scrunchSpeechRate')||'1';
$('speechRate').onchange=()=>localStorage.setItem('scrunchSpeechRate',$('speechRate').value);
$('speechVoice').onchange=()=>localStorage.setItem('scrunchSpeechVoice',$('speechVoice').value);
if(window.speechSynthesis)window.speechSynthesis.addEventListener('voiceschanged',loadVoices);
loadVoices();
$('previewVoice').onclick=()=>speakReply('Hello John. Your Scrunch Engineer is ready.');
$('readReplies').onchange=()=>{if(!$('readReplies').checked){cancelReply();}};
async function handleVoice(text){
 $('voiceTranscript').textContent=text?'Heard: '+text:'No intelligible speech detected. Nothing executed.';
 if(!text)return;$('engineerInput').value=text;$('engineerInput').focus();$('engineerInput').scrollTop=$('engineerInput').scrollHeight;scrollEngineerToLatest();
 status('Edit if needed, then press Send.');
}
async function finishVoice(cancel=false){
 const r=recording;if(!r)return;recording=null;clearTimeout(r.timer);hideVoiceMeter();
 if(r.live){try{r.live.stop();}catch(e){}await new Promise(res=>setTimeout(res,350));}
 r.processor.disconnect();r.source.disconnect();r.gain.disconnect();r.stream.getTracks().forEach(t=>t.stop());await r.context.close();
 $('dictate').classList.remove('recording');$('dictate').textContent='🎙 Speak';$('cancelVoice').hidden=true;
 busy=false;document.querySelectorAll('button').forEach(b=>b.disabled=false);
 if(cancel){status('Recording discarded.');return;}
 const heard=(r.liveText||'').trim();
 if(heard){lastTranscriptionMs=0;await handleVoice(heard);orb('done','Ready');return;}
 if(r.energy<0.003){orb('error','No microphone signal');status('No microphone signal detected. Check the microphone input and try again.');return;}
 await run(async()=>{
  status('Transcribing with Gemini…');
  const blob=wavBytes(r.chunks,r.context.sampleRate);
  const encoded=await new Promise((resolve,reject)=>{const f=new FileReader();f.onload=()=>resolve(f.result.split(',')[1]);f.onerror=reject;f.readAsDataURL(blob);});
  const result=await request('transcribe',{provider:r.provider,mime:'audio/wav',audio:encoded});lastTranscriptionMs=result.transcriptionMs??null;await handleVoice(result.text);
 });
}
$('dictate').onclick=async()=>{
 if(recording){await finishVoice();return;}if(busy)return;cancelReply();
 const profile=state.providers.find(p=>p.id===$('provider').value&&p.kind==='gemini')||state.providers.find(p=>p.kind==='gemini');
 if(!profile){status('Save a Gemini profile in Settings to enable voice.');return;}
 busy=true;document.querySelectorAll('button:not(#stop):not(#refStop)').forEach(b=>b.disabled=true);
 let stream,context;
 try{
  if(!navigator.mediaDevices?.getUserMedia)throw Error('Microphone capture is unavailable in this browser.');
  stream=await navigator.mediaDevices.getUserMedia({audio:{channelCount:1,echoCancellation:true,noiseSuppression:true},video:false});
  context=new AudioContext({sampleRate:16000});await context.resume();
  const source=context.createMediaStreamSource(stream),processor=context.createScriptProcessor(2048,1,1),gain=context.createGain();gain.gain.value=0;
  const r={stream,context,source,processor,gain,chunks:[],energy:0,provider:profile.id};recording=r;
  r.started=performance.now();r.levels=Array(40).fill(3);
  $('voiceWave').replaceChildren(...r.levels.map(()=>{const bar=document.createElement('span');bar.style.height='3px';return bar;}));
  $('voiceElapsed').textContent='0:00 / 1:00';$('voiceMonitor').hidden=false;orb('listening','Listening');
  processor.onaudioprocess=e=>{if(recording!==r)return;const chunk=new Float32Array(e.inputBuffer.getChannelData(0));r.chunks.push(chunk);let sum=0;for(const x of chunk)sum+=x*x;const level=Math.sqrt(sum/chunk.length);r.energy=Math.max(r.energy,level);updateVoiceMeter(r,level);};
  source.connect(processor);processor.connect(gain);gain.connect(context.destination);
  r.timer=setTimeout(()=>finishVoice(),60000);
  const SR=window.SpeechRecognition||window.webkitSpeechRecognition;
  if(SR){try{const live=new SR();live.lang='en-GB';live.continuous=true;live.interimResults=true;let done='';
   live.onresult=e=>{let interim='';for(let i=e.resultIndex;i<e.results.length;i++){const t=e.results[i][0].transcript;if(e.results[i].isFinal)done+=t+' ';else interim+=t;}r.liveText=(done+interim).trim();$('engineerInput').value=r.liveText;};
   live.onerror=()=>{};live.start();r.live=live;}catch(e){}}
  $('dictate').disabled=false;$('dictate').classList.add('recording');$('dictate').textContent='Finish speaking';$('cancelVoice').hidden=false;$('cancelVoice').disabled=false;
  status('Recording — stop to review, or × to discard.');
 }catch(e){hideVoiceMeter();stream?.getTracks().forEach(t=>t.stop());if(context)await context.close().catch(()=>{});busy=false;document.querySelectorAll('button').forEach(b=>b.disabled=false);orb('error','Microphone unavailable');status('Microphone: '+e.message);}
};
$('cancelVoice').onclick=()=>finishVoice(true);
window.addEventListener('pagehide',()=>{if(recording){recording.stream.getTracks().forEach(t=>t.stop());recording.context.close();}});
if(document.modelContext?.registerTool){document.modelContext.registerTool({name:'read_scrunch_state',description:'Read source and takes without editing Ableton.',inputSchema:{type:'object',properties:{},additionalProperties:false},annotations:{readOnlyHint:true},execute:async()=>{await refresh();return{source:state.source,attempts:state.attempts.map(a=>({id:a.id,status:a.status,model:a.model,metrics:a.metrics}))};}});}
refresh().catch(e=>status(e.message));

async function loadCheckpoints(){const r=await(await fetch('/api/checkpoints')).json();$('checkpoints').replaceChildren();for(const m of r.checkpoints){const o=new Option(new Date(m.created).toLocaleString()+' · '+m.reason+(m.verified?'':' · damaged'),m.id);o.disabled=!m.verified;$('checkpoints').add(o);}$('checkpointStatus').textContent=r.checkpoints.length+' local checkpoints found.';}
$('listCheckpoints').onclick=()=>run(loadCheckpoints);
$('makeCheckpoint').onclick=()=>run(async()=>{await request('checkpoint',{});await loadCheckpoints();});
$('makeCheckpoint').closest('section').querySelector('p').textContent='Manual recovery copies only. Automatic backups are off.';
$('restoreCheckpoint').onclick=()=>run(async()=>{if(!$('checkpoints').value)throw Error('Choose a verified checkpoint first.');await request('revert',{id:$('checkpoints').value});await loadCheckpoints();$('checkpointStatus').textContent='Restored copy opened and MIDI verified.';});

function technical(container,data){const details=node('details');details.append(node('summary','Show technical details'));details.append(node('pre',JSON.stringify(data,null,2)));container.append(details);}
$('scanInstruments').onclick=()=>run(async()=>{const r=await fetch('/api/instruments');if(!r.ok)throw Error('Instrument scan failed');const data=await r.json(),box=$('instrumentReport');box.replaceChildren();for(const d of data.instruments){box.append(node('p',d.track.name+' — '+d.instrument+': '+(d.parameters.map(p=>p.raw.name+(p.midiCC===null?' (CC not reported)':' (CC '+p.midiCC+')')).join(', ')||'No exposed controls')+'. Writes untested.'));}for(const e of data.errors)box.append(node('p','Read failed: '+e.error));technical(box,data);});
async function refreshAudio(){try{const response=await fetch('/api/analysis/session');if(!response.ok)throw Error('Cannot read session audio');const data=await response.json(),choice=$('audioTracks').value;document.dispatchEvent(new CustomEvent('scrunch-session-analysis',{detail:data}));$('audioTracks').replaceChildren(new Option('All audio tracks',''));for(const t of data.tracks||[])$('audioTracks').add(new Option(t.name,t.id));$('audioTracks').value=choice;const clips=(data.tracks||[]).flatMap(t=>t.clips);const errors=clips.filter(c=>c.error||c.analysis?.deepError);$('ambientStatus').textContent=!data.available?'Audio analysis unavailable: '+data.error:errors.length?errors.map(c=>c.name+': '+(c.error||'Background RMS failed: '+c.analysis.deepError)).join(' • '):clips.length?clips.filter(c=>c.status==='complete').length+' of '+clips.length+' audio files measured. Ask “Show measurements”.':'No audio clips in this Live Set. MIDI instruments need rendered audio.';$('ambientStatus').classList.toggle('analysis-error',!data.available||errors.length>0);$('audioTechnical').textContent=JSON.stringify(data,null,2);if(document.activeElement!==$('sidecarDirectory'))$('sidecarDirectory').placeholder=data.sidecarDirectory||'.scrunch/analysis';}catch(e){document.dispatchEvent(new CustomEvent('scrunch-session-analysis',{detail:null}));$('ambientStatus').textContent='Audio analysis unavailable: '+e.message;$('ambientStatus').classList.add('analysis-error');}}
$('saveSidecarDirectory').onclick=()=>run(async()=>{const r=await request('analysis/storage',{directory:$('sidecarDirectory').value});$('storageStatus').textContent=r.message;});
refreshAudio();setInterval(refreshAudio,5000);

let referenceReportId='',refPlaying=null,refSeekOffset=0,referencePlayRequest=0;
function optionsFor(id,rows,label){const el=$(id),old=el.value;el.replaceChildren(...rows.map(r=>new Option(label(r),r.id)));if(rows.some(r=>r.id===old))el.value=old;}
async function refreshReferences(){try{const s=await(await fetch('/api/reference/state')).json();if(s.error){$('refStatus').textContent=s.error.includes('saved Live Set location')?"Live’s saved project location is unavailable. Deep Analysis is paused; listening remains available.":s.error;$('refTechnical').textContent=JSON.stringify({error:s.error},null,2);return;}optionsFor('refTarget',s.targets,r=>r.name);optionsFor('refA',s.captures,r=>r.trackName+' · '+r.lengthBars+' bars');optionsFor('refB',s.references,r=>r.name);$('refStatus').textContent=['failed','interrupted'].includes(s.job.status)?'Failed: '+s.job.error:s.job.status==='running'?'Capture/comparison in progress…':s.job.result?.message?.split('\n')[0]||'Choose an output and reference.';if(s.reports.length){const id=s.reports.at(-1).id;if(id!==referenceReportId){const r=await(await fetch('/api/reference/report/'+id)).json();$('refTechnical').textContent=JSON.stringify(r,null,2);$('refEnglish').textContent=r.summary||'Comparison ready. Ask your engineer “Show comparison”.';$('refPlayback').textContent='Preview ready: '+r.liveSource.trackName+' vs '+r.referenceSource.name+'. Live cue '+r.manualCues.liveSeconds+' s; reference cue '+r.manualCues.referenceSeconds+' s.';$('refAudioA').src='/api/reference/audio/'+id+'/A';$('refAudioB').src='/api/reference/audio/'+id+'/B';drawReference(r);referenceReportId=id;}}}catch(e){$('refStatus').textContent='Reference comparison unavailable: '+e.message;}}
function drawReference(r){renderAnalytics(r);const c=$('refSpectrum'),x=c.getContext('2d'),left=48,right=c.width-15,top=30,bottom=c.height-28;const fx=f=>left+Math.log10(f/20)/3*(right-left),fy=db=>top+Math.max(0,Math.min(1,(-db-10)/100))*(bottom-top);x.clearRect(0,0,c.width,c.height);x.font='11px sans-serif';for(const f of [20,60,200,1000,4000,10000,20000]){const xx=fx(f);x.strokeStyle='#343e40';x.beginPath();x.moveTo(xx,top);x.lineTo(xx,bottom);x.stroke();x.fillStyle='#bbc5c7';x.textAlign='center';x.fillText(f>=1000?f/1000+'k':String(f),xx,c.height-9);}for(const db of [-10,-30,-50,-70,-90,-110]){const yy=fy(db);x.strokeStyle='#343e40';x.beginPath();x.moveTo(left,yy);x.lineTo(right,yy);x.stroke();x.fillStyle='#bbc5c7';x.textAlign='right';x.fillText(String(db),left-7,yy+4);}for(const [side,color] of [['live','#ed913d'],['reference','#66b9db']]){if(spectrumMode!=='both'&&spectrumMode!==side)continue;const sp=r.matched[side].spectrum;x.strokeStyle=color;x.beginPath();let first=true;for(let i=0;i<sp.hz.length;i++){if(sp.hz[i]<20||sp.hz[i]>20000)continue;const xx=fx(sp.hz[i]),yy=fy(sp.powerDbPerHz[i]);if(first){x.moveTo(xx,yy);first=false;}else x.lineTo(xx,yy);}x.stroke();}x.textAlign='left';x.fillStyle='#ddd';x.fillText('Power density · dB/Hz',left,15);x.fillStyle='#ed913d';x.fillText('A · Live',300,15);x.fillStyle='#66b9db';x.fillText('B · Reference',400,15);drawWave(r);}
function drawWave(r){const w=$('refWave'),z=w.getContext('2d');z.clearRect(0,0,w.width,w.height);const max=Math.max(.001,...r.matched.live.waveform.max.map(Math.abs),...r.matched.live.waveform.min.map(Math.abs),...r.matched.reference.waveform.max.map(Math.abs),...r.matched.reference.waveform.min.map(Math.abs));for(const [j,side,color] of [[0,'live','#ed913d'],[1,'reference','#66b9db']]){const a=r.matched[side].waveform;z.strokeStyle=color;z.beginPath();for(let i=0;i<a.min.length;i++){const xx=i/a.min.length*w.width,mid=65+j*140;z.moveTo(xx,mid-a.min[i]*50/max);z.lineTo(xx,mid-a.max[i]*50/max);}z.stroke();z.fillStyle=color;z.fillText(j?'B · Reference':'A · Live',8,18+j*140);}const active=!$('refAudioA').paused?$('refAudioA'):!$('refAudioB').paused?$('refAudioB'):null;if(active){const xx=active.currentTime/r.durationSeconds*w.width;z.strokeStyle='#fff';z.beginPath();z.moveTo(xx,0);z.lineTo(xx,w.height);z.stroke();}z.fillStyle='#bfc8c9';z.fillText('0 s · click to seek · shared display zoom (audio unchanged)',8,w.height-8);z.fillText(r.durationSeconds.toFixed(2)+' s',w.width-55,w.height-8);}
$('refWave').onclick=e=>{if(!analyticsReport)return;const rect=e.currentTarget.getBoundingClientRect(),time=Math.max(0,Math.min(analyticsReport.durationSeconds,(e.clientX-rect.left)/rect.width*analyticsReport.durationSeconds));refSeekOffset=time;for(const id of ['refAudioA','refAudioB'])if($(id).readyState>0)$(id).currentTime=time;drawWave(analyticsReport);};
setInterval(()=>{if(analyticsReport&&!panels.setup.hidden)drawWave(analyticsReport);},100);
async function uploadReferences(files){if(files.length<1||files.length>2)throw Error('Choose one or two references.');for(const file of files){if(!/\.(wav|aif|aiff)$/i.test(file.name))throw Error('References must be WAV or AIFF.');const r=await fetch('/api/reference/upload',{method:'POST',headers:{'X-Scrunch-Token':token,'X-File-Name':encodeURIComponent(file.name),'Content-Type':'application/octet-stream'},body:file});const result=await r.json();if(!r.ok)throw Error(result.error);$('refStatus').textContent=result.message;}await refreshReferences();}
$('refFiles').onchange=()=>run(()=>uploadReferences(Array.from($('refFiles').files)));
$('referenceDrop').ondragover=e=>{e.preventDefault();e.dataTransfer.dropEffect='copy';};$('referenceDrop').ondrop=e=>{e.preventDefault();run(()=>uploadReferences(Array.from(e.dataTransfer.files)));};
$('refCapture').onclick=()=>run(async()=>{await request('reference/capture',{target:$('refTarget').value,bar:Number($('refBar').value),bars:Number($('refBars').value)});await refreshReferences();});
$('refForEngineer').onclick=()=>run(async()=>{try{if(!$('refB').value)throw Error('Choose a reference first.');const t=await request('reference-target',{referenceId:$('refB').value,cueSeconds:Number($('refCueB').value),durationSeconds:Number($('refDuration').value),part:$('refPart').value.trim()||'the lead'});showEngineerReference(t);}catch(e){$('refEngineerStatus').textContent='Not set: '+e.message;throw e;}});
$('refSearch').onclick=()=>run(async()=>{$('refSearchStatus').textContent='Planning (offline renders), then measuring on the synth…';$('refSearchPlan').hidden=true;try{const r=await request('search',{requestId:crypto.randomUUID()});$('refSearchPlan').textContent=r.say;$('refSearchPlan').hidden=false;$('refSearchStatus').textContent=r.result?'Done: plan and measured result below.':'Plan only.';}catch(e){$('refSearchStatus').textContent='Search not run: '+e.message;throw e;}});
$('refSearchUndo').onclick=()=>run(async()=>{const r=await request('search/undo',{});$('refSearchStatus').textContent='Search moves: '+r.status;});
$('refRefine').onchange=()=>run(async()=>{const r=await request('reference-refine',{on:$('refRefine').checked});$('refRefine').checked=r.referenceRefine===true;});
$('refCompare').onclick=()=>run(async()=>{await request('reference/compare',{capture:$('refA').value,reference:$('refB').value,liveCue:Number($('refCueA').value),referenceCue:Number($('refCueB').value),duration:Number($('refDuration').value)});await refreshReferences();});
function stopReference(){referencePlayRequest++;for(const id of ['refAudioA','refAudioB'])$(id).pause();refPlaying=null;}
async function playReference(side){if(!referenceReportId)throw Error('Make a comparison first.');const offset=refPlaying?$(refPlaying).currentTime:refSeekOffset;stopReference();const playbackRequest=referencePlayRequest;const id='refAudio'+side,a=$(id);a.currentTime=Number.isFinite(a.duration)&&offset>=a.duration?0:offset;a.volume=.5;await a.play();if(playbackRequest!==referencePlayRequest){a.pause();return;}refPlaying=id;$('refPlayback').textContent='Playing '+side+' from the last completed comparison.';}
function preview(side){playReference(side).catch(e=>{if(e.name!=='AbortError')$('refPlayback').textContent='Preview failed: '+e.message;});}$('refPlayA').onclick=()=>preview('A');$('refPlayB').onclick=()=>preview('B');$('refStop').onclick=()=>{stopReference();refSeekOffset=0;$('refPlayback').textContent='A/B preview stopped.';};
refreshReferences();setInterval(refreshReferences,5000);

for(const id of ['refAudioA','refAudioB'])$(id).onended=()=>{refPlaying=null;refSeekOffset=0;$('refPlayback').textContent='A/B preview finished.';};
$('stop').addEventListener('click',stopReference);

// Recovery is a human-controlled workflow, independent of Live connectivity.
const recoveryPanel=document.createElement('details');recoveryPanel.className='deep-analysis';
const recoveryTitle=document.createElement('summary'),recoveryMessage=document.createElement('p'),recoverySettings=document.createElement('dl'),recoveryNote=document.createElement('textarea'),recoveryAck=document.createElement('input'),recoveryAckLabel=document.createElement('label'),recoveryButtons=document.createElement('div');
recoveryNote.placeholder='Describe what you checked or why you are resolving without verification';recoveryAck.type='checkbox';recoveryAckLabel.append(recoveryAck,document.createTextNode(' I reviewed the saved settings and understand this decision is recorded.'));
recoveryPanel.append(recoveryTitle,recoveryMessage,recoverySettings,recoveryNote,recoveryAckLabel,recoveryButtons);analytics.prepend(recoveryPanel);let recoveryRevision;
async function refreshRecovery(){try{const r=await fetch('/api/export-recovery');if(!r.ok)throw Error('Recovery status unavailable');const j=await r.json();if(recoveryRevision!==j.revision){recoveryAck.checked=false;recoveryNote.value='';}recoveryRevision=j.revision;recoveryPanel.open=j.pending||j.status==='user-resolved-unverified';recoveryTitle.textContent=j.pending?'Export settings need recovery':'Export recovery · '+({'restored':'settings restored','manual-verified':'manually checked','user-resolved-unverified':'resolved without verification','clear':'no pending restoration'}[j.status]||'review needed');recoveryMessage.textContent=j.pending?(j.error||'Restore these original settings in Live, then record your verification, or explicitly resolve without verification. Force resolve does not restore settings.'):(j.status==='user-resolved-unverified'?'Resolved by user without verified restoration.':'No restoration is pending.');recoverySettings.replaceChildren();for(const s of j.settings||[]){const term=document.createElement('dt'),value=document.createElement('dd');term.textContent=s.label;value.textContent=s.value;recoverySettings.append(term,value);}for(const el of [recoveryNote,recoveryAckLabel,recoveryButtons])el.hidden=!j.pending;}catch(e){recoveryMessage.textContent=e.message;}}
for(const [action,label] of [['retry','Retry automatic restoration'],['manual-verified','I restored and checked these settings'],['user-resolved-unverified','Force resolve · restoration unverified']]){const b=document.createElement('button');b.textContent=label;b.onclick=async()=>{b.disabled=true;try{await request('export-recovery',{action,revision:recoveryRevision,acknowledge:recoveryAck.checked,reason:recoveryNote.value});await refreshRecovery();}catch(e){recoveryMessage.textContent=e.message;}finally{b.disabled=false;}};recoveryButtons.append(b);}
refreshRecovery();setInterval(refreshRecovery,10000);

const earsControls=document.createElement('div'),earsSelect=document.createElement('select'),earsStart=document.createElement('button'),earsStop=document.createElement('button'),earsStatus=document.createElement('p'),earsDetails=document.createElement('details'),earsSummary=document.createElement('summary'),earsRaw=document.createElement('pre');
earsSelect.setAttribute('aria-label','Audio source');earsStart.textContent='Connect';earsStop.textContent='Disconnect';earsSummary.textContent='Live Ears technical details';earsDetails.append(earsSummary,earsRaw);earsControls.append(earsSelect,earsStart,earsStop,earsStatus,earsDetails);analytics.insertBefore(earsControls,nav);const earsCanvases={};
for(const id of ['spectrum','stereo','metering']){const c=document.createElement('canvas');c.width=1000;c.height=360;c.hidden=true;c.setAttribute('aria-label','Live Ears '+id);panels[id].prepend(c);earsCanvases[id]=c;}
earsStart.onclick=async()=>{try{await request('live-ears/start',{streamId:earsSelect.value});}catch(e){earsStatus.textContent='Could not connect or disconnect audio. See technical details.';earsRaw.textContent=e.message;}};earsStop.onclick=async()=>{try{await request('live-ears/stop',{});}catch(e){earsStatus.textContent='Could not connect or disconnect audio. See technical details.';earsRaw.textContent=e.message;}};
function drawEars(j){const m=j.result?.measurement;for(const [id,c] of Object.entries(earsCanvases)){c.hidden=!j.streamId;c.dataset.windowEndedAt=j.result?.windowEndedAt||'';delete c.dataset.hostDrawLatency;const x=c.getContext('2d');x.clearRect(0,0,1000,360);x.font='14px sans-serif';x.fillStyle='#ed913d';x.fillText('LIVE EARS · last 3 seconds · '+j.state,25,25);if(!m){x.fillText('Waiting for audio',25,65);continue;}if(id==='spectrum'){x.strokeStyle='#ed913d';x.beginPath();m.spectrum.hz.forEach((f,i)=>{if(f<20)return;const xx=35+930*Math.log(f/20)/Math.log(1200),yy=300-Math.max(0,Math.min(110,m.spectrum.powerDbPerHz[i]+110))*2.3;i&&f>21?x.lineTo(xx,yy):x.moveTo(xx,yy);});x.stroke();x.fillText('20 Hz                         200 Hz                        2 kHz                         20 kHz',35,335);}else if(id==='stereo'){m.bands.forEach((b,i)=>{const xx=65+i*82;x.fillStyle='#596165';x.fillRect(xx,65,1,210);if(b.sideFraction!==null){const h=Math.sqrt(b.sideFraction)*100;x.fillStyle='#ed913d';x.fillRect(xx-23,170-h,46,2*h);}x.fillStyle='#ddd';x.fillText((b.lowHz/1000).toFixed(2)+'k',xx-25,300);x.fillText(b.correlation===null?'—':b.correlation.toFixed(2),xx-25,325);});x.fillText('Side energy around mono centre · correlation shown below each band',25,48);}else{const h=m.loudnessHistory.at(-1)||{};[['Rolling integrated',m.integratedLUFS,'LUFS'],['Short-term',h.shortTermLUFS,'LUFS'],['Momentary',h.momentaryLUFS,'LUFS'],['True peak (3 s)',m.truePeakDbTP,'dBTP'],['RMS left',m.rmsDbFS[0],'dBFS'],['RMS right',m.rmsDbFS[1],'dBFS'],['Crest (3 s)',m.crestFactorDb,'dB']].forEach(([label,v,unit],i)=>{const y=65+i*43;x.fillStyle='#ddd';x.fillText(label,25,y);x.fillStyle='#394244';x.fillRect(210,y-15,540,20);if(typeof v==='number'){x.fillStyle='#ed913d';x.fillRect(210,y-15,540*Math.max(0,Math.min(1,label.startsWith('Crest')?v/24:(v+60)/60)),20);}x.fillStyle='#ddd';x.fillText(typeof v==='number'?v.toFixed(1)+' '+unit:'Unavailable',775,y);});}}}
let earsPolling=false,earsLastResponse=0;async function refreshEars(){if(earsPolling)return;earsPolling=true;const requestStarted=performance.now(),controller=new AbortController(),deadline=setTimeout(()=>controller.abort(),2000);try{const r=await fetch('/api/live-ears',{signal:controller.signal});if(!r.ok)throw Error('Live Ears status unavailable');const j=await r.json(),selected=earsSelect.value;earsLastResponse=performance.now();earsSelect.replaceChildren();for(const stream of j.streams){const o=document.createElement('option');o.value=stream;o.textContent='Audio '+(j.streams.indexOf(stream)+1)+' · track not identified';earsSelect.append(o);}if(j.streams.includes(selected))earsSelect.value=selected;earsStart.disabled=!j.streams.length;earsStatus.textContent=j.error?'Listening unavailable. See technical details.':j.streamId?'Connected · track and position in the effects chain not confirmed.':'Choose audio and Connect.';earsRaw.textContent=JSON.stringify(j,null,2);drawEars(j);document.dispatchEvent(new CustomEvent('scrunch-listening',{detail:j}));
const received=performance.now(),age=j.result?.hostBufferStartToStatusMs,windowId=String(j.result?.windowEndedAt||'');
if(typeof age==='number'&&Number.isFinite(age))requestAnimationFrame(()=>{
 const drawn=performance.now();
 for(const c of Object.values(earsCanvases))if(!c.hidden&&c.dataset.windowEndedAt===windowId)c.dataset.hostDrawLatency=JSON.stringify({lowerMs:age+drawn-received,upperMs:age+drawn-requestStarted,basis:'host buffer start to browser animation frame after canvas drawing; network interval bounded; not physical display or hardware audio latency'});
});}catch(e){earsStatus.textContent=e.name==='AbortError'?'Audio connection timed out. Reconnect to continue.':e.message;drawEars({state:'unavailable',streamId:true});document.dispatchEvent(new CustomEvent('scrunch-listening',{detail:{state:'unavailable',streamId:null}}));}finally{clearTimeout(deadline);earsPolling=false;}}
refreshEars();setInterval(refreshEars,500);
setInterval(()=>{if(earsLastResponse&&performance.now()-earsLastResponse>2500){drawEars({state:'unavailable',streamId:true});document.dispatchEvent(new CustomEvent('scrunch-listening',{detail:{state:'unavailable',streamId:null}}));earsStatus.textContent='Audio connection lost. Reconnect to continue.';}},250);

const earsRef=document.createElement('button');earsRef.textContent='Compare';earsRef.onclick=async()=>{try{const r=await request('live-ears/reference',{reference:$('refB').value,cueSeconds:Number($('refCueB').value)});earsStatus.textContent='Reference cue set: '+r.reference.name;}catch(e){earsStatus.textContent='Could not connect or disconnect audio. See technical details.';earsRaw.textContent=e.message;}};panels.setup.append(earsRef);
const earsClearRef=document.createElement('button');earsClearRef.textContent='Clear live reference comparison';earsClearRef.onclick=async()=>{try{await request('live-ears/reference',{clear:true});earsStatus.textContent='Live reference comparison cleared; live measurements continue.';}catch(e){earsStatus.textContent='Could not connect or disconnect audio. See technical details.';earsRaw.textContent=e.message;}};panels.setup.append(earsClearRef);
if(location.hash==='#analytics')document.getElementById('tabAnalytics').click();
