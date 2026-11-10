// Layout-only shell: existing nodes/listeners are moved, never duplicated.
(()=>{
 const $=id=>document.getElementById(id),make=(tag,id)=>{const n=document.createElement(tag);if(id)n.id=id;return n;};
 const top=make('div','shellHeader'),content=make('div','shellContent'),bottom=make('footer','shellControls');
 top.append(document.querySelector('body>header'),document.querySelector('.workspace-tabs'));document.body.prepend(top,content,bottom);
 for(const id of ['engineerWorkspace','analyticsWorkspace','referenceWorkspace','sessionWorkspace'])content.append($(id));
 const statusRow=make('div','commandStatus');statusRow.append($('inputStatus'));const modeLabel=document.querySelector('label[for=engineerMode]');modeLabel.textContent='Mode';statusRow.append(modeLabel,$('engineerMode'));
 bottom.append(statusRow,$('commandBar'),$('voiceMonitor'),$('cancelVoice'));
 const ab=$('refPlayA').parentElement;ab.id='pinnedAB';bottom.prepend(ab);ab.append($('refPlayback'));
 const midi=document.querySelector('#sessionWorkspace .transport');midi.id='pinnedMidi';bottom.prepend(midi);
 // One shared comparison workspace, whichever comparison tab is active.
 const comparison=document.querySelector('.reference-workspace');
 const reference=$('referenceWorkspace');
 const profile=make('details','profilePreparation');const profileSummary=make('summary');profileSummary.textContent='Master profile and blind review';profile.append(profileSummary,$('preproof'));reference.append(profile);
 const referenceIntro=make('details','referenceNotes');const referenceSummary=make('summary');referenceSummary.textContent='Technical details';referenceIntro.append(referenceSummary);
 for(const n of [...reference.children])if(n!==profile&&n!==referenceIntro)referenceIntro.append(n);reference.append(referenceIntro);
 const setup=$('analyticsPanel-setup'),controls=make('div','comparisonControls');controls.className='comparisonControls';
 for(const n of [...setup.children])if(n!==$('refWave'))controls.append(n);setup.append(controls);controls.prepend(recoveryPanel);setup.classList.add('visualComparison');
 // Keep the complete existing warning/explanation text, collapsed and available.
 earsDetails.append(earsStatus);earsControls.classList.add('compactTapControls');
 const info=make('details','analyticsNotes'),sum=make('summary');sum.textContent='Technical details';info.append(sum);comparison.append(info);
 for(const n of [...comparison.children])if(n.tagName==='P'&&n.id!=='refStatus')info.append(n);const fullStatus=make('p');info.append(fullStatus);const copyStatus=()=>{fullStatus.textContent=$('refStatus').textContent;$('refStatus').title=$('refStatus').textContent};new MutationObserver(copyStatus).observe($('refStatus'),{childList:true,characterData:true,subtree:true});copyStatus();
 function collapseProse(scope){
  let details=null;
  for(const p of [...scope.querySelectorAll('p')]){
   if(p.closest('details')||p.closest('#engineerChat')||p.closest('#resourceConversation')||p.getAttribute('role')==='status'||p.id==='refPlayback')continue;
   if(p.textContent.length<110)continue;
   if(!details){details=make('details');const s=make('summary');s.textContent='Technical details · guidance';details.append(s);scope.append(details)}details.append(p);
  }
 }
 collapseProse(controls);collapseProse($('preproof'));collapseProse($('sessionWorkspace'));
 const sources=make('details','referenceSources'),sourcesTitle=make('summary');sourcesTitle.textContent='Load reference files';sources.append(sourcesTitle,$('referenceDrop'),$('refFiles').parentElement);controls.append(sources);
 const choiceRow=$('refA').parentElement.parentElement,cueRow=$('refCueA').parentElement.parentElement;choiceRow.classList.add('sourceChoices');cueRow.classList.add('cueFields');controls.prepend(choiceRow,cueRow);
 for(const p of [...controls.querySelectorAll(':scope > p')])info.append(p);
 const tapOptions=make('details','liveReferenceOptions'),tapTitle=make('summary');tapTitle.textContent='Live reference options';tapOptions.append(tapTitle);for(const b of [...controls.children])if(b.tagName==='BUTTON')tapOptions.append(b);controls.append(tapOptions);

 // Session's roll and takes precede setup/recovery, so useful displays are first.
 const session=$('sessionWorkspace'),rollhead=session.querySelector('.rollhead'),roll=$('roll'),takes=session.querySelector('aside');
 const work=make('div','sessionDisplays');const notes=make('section');notes.append(rollhead,roll);work.append(notes,takes);session.prepend(work);
 const secondary=make('details','sessionSetup');const ss=make('summary');ss.textContent='Session setup, source and recovery';secondary.append(ss);for(const n of [...session.children])if(n!==work&&n!==secondary)secondary.append(n);session.append(secondary);
 function selected(id){const isAB=['tabAnalytics','tabReference'].includes(id);ab.hidden=!isAB;midi.hidden=id!=='tabSession';if(isAB){const target=$(id==='tabReference'?'referenceWorkspace':'analyticsWorkspace');target.prepend(comparison);if(id==='tabReference')$('analyticsTab-setup').click()}if(id==='tabEngineer')scrollEngineerToLatest();else content.scrollTop=0;}
 for(const id of ['tabEngineer','tabAnalytics','tabReference','tabSession']){const b=$(id),click=b.onclick,key=b.onkeydown;b.onclick=e=>{click?.(e);selected(id)};b.onkeydown=e=>{key?.(e);const active=document.querySelector('.workspace-tabs [aria-selected=true]');selected(active.id)}}selected(document.querySelector('.workspace-tabs [aria-selected=true]').id);

 // Compact composer, with distinct recording discard, finish and transport stop.
 const bar=$('commandBar'),settings=$('settingsButton');
 document.querySelector('body #shellHeader header').append(settings);
 settings.textContent='☰';settings.title='Settings and memory';settings.setAttribute('aria-label','Settings and memory');
 bar.prepend($('inputStatus'),$('cancelVoice'));bar.append($('engineerMode'),$('dictate'),$('sendEngineer'));
 statusRow.remove();$('cancelVoice').textContent='×';$('cancelVoice').title='Discard dictation';$('cancelVoice').setAttribute('aria-label','Discard dictation');
 $('sendEngineer').textContent='↑';$('sendEngineer').title='Send to engineer';$('sendEngineer').setAttribute('aria-label','Send to engineer');
 $('stop').title='Stop playback and response';$('engineerMode').setAttribute('aria-label','Engineer mode');
 const micLabel=()=>{const active=$('dictate').classList.contains('recording');$('dictate').setAttribute('aria-label',active?'Stop dictation and review':'Start dictation');$('dictate').title=active?'Stop dictation and review':'Start dictation';};
 new MutationObserver(micLabel).observe($('dictate'),{attributes:true,attributeFilter:['class']});micLabel();
 $('voiceCommands').checked=false;$('voiceCommands').closest('label')?.setAttribute('hidden','');
 // Keep each comparison tab's working view: Analytics opens Spectrum, Reference A/B.
 const previousAnalytics=$('tabAnalytics').onclick;$('tabAnalytics').onclick=e=>{previousAnalytics(e);$('analyticsTab-spectrum').click();};
 // No section candidate is eligible for the session header.
 const key=make('span','sessionKey');key.textContent='Key —';$('hostTempo').after(key);
 const show=value=>{const r=window.ScrunchHeaderKey(value);key.textContent=r.label;key.title=r.reason;};
 document.addEventListener('scrunch-session-analysis',e=>show(e.detail?.available?e.detail.overallKey:null));
 show(null);
})();

// BEGIN observation rules — pure, locally evaluated measurements; no model/DAW actions.
function createListeningObserver(){
 let source=null,lastWindow=null,baseline=null,pending=null,count=0,lastSpoken=-Infinity,started=false,peakAnnounced=false;
 return function(j,now){
  if(j.streamId!==source){source=j.streamId;lastWindow=null;baseline=null;pending=null;count=0;lastSpoken=-Infinity;started=false;peakAnnounced=false;}
  if(j.state!=='live'||!j.result?.measurement){pending=null;count=0;baseline=null;return null;}
  const id=j.result.windowEndedAt;if(!id||id===lastWindow)return null;lastWindow=id;
  const m=j.result.measurement,l=m.integratedLUFS,p=m.truePeakDbTP;
  if(!Number.isFinite(l)||l< -60){pending=null;count=0;baseline=null;return null;}
  if(baseline===null)baseline=l;
  if(Number.isFinite(p)&&p<=-1)peakAnnounced=false;
  const change=l-baseline;
  const kind=Number.isFinite(p)&&p>0&&!peakAnnounced?'peak':!started?'initial':Math.abs(change)>=3?(change>0?'louder':'quieter'):null;
  if(!kind){pending=null;count=0;return null;}
  count=kind===pending?count+1:1;pending=kind;
  if(count<3||now-lastSpoken<30000)return null;
  started=true;if(kind==='peak')peakAnnounced=true;lastSpoken=now;baseline=l;count=0;
  const text=kind==='peak'?`The audio I'm hearing has repeatedly exceeded zero dB true peak. The latest three-second measurement is ${p.toFixed(1)} dBTP. Check the level before the output.`:kind==='initial'?`I'm listening. The audio currently measures ${l.toFixed(1)} LUFS over the last three seconds. I'll flag sustained level changes.`:`The audio has become about ${Math.abs(change).toFixed(1)} loudness units ${kind} than my previous observation. This measures a level change; it doesn't establish the cause.`;
  return {id:source+':'+id+':'+kind,text,kind,evidence:{windowEndedAt:id,integratedLUFS:l,truePeakDbTP:p},basis:'Local measurement rule; not a model interpretation'};
 };
}
// END observation rules
(()=>{
 const observe=createListeningObserver(),section=document.createElement('section');section.id='listeningStatus';
 section.innerHTML='<span class="listeningPulse" aria-hidden="true"></span><strong>Not listening</strong><span class="listeningNote">Connect to hear observations.</span>';
 document.querySelector('.compactTapControls').after(section);
 const log=document.createElement('div');log.id='listeningObservations';document.querySelector('.engineer-panel').append(log);
 const entries=[];let deferred=null;
 function deliver(){if(!deferred||busy||recording||window.speechSynthesis?.speaking||document.hidden)return;const e=deferred;deferred=null;
  if(Date.now()-e.receivedAt>5000)return;
  const line=document.createElement('div');line.className='message';line.textContent='Listening · '+e.text;
  const details=document.createElement('details'),summary=document.createElement('summary'),body=document.createElement('pre');summary.textContent='Technical details';body.textContent=JSON.stringify(e,null,2);details.append(summary,body);line.append(details);log.append(line);entries.push(line);if(entries.length>20)entries.shift().remove();
  // Existing user-controlled voice and mute preference; never cancel a current reply.
  speakReply(e.text);
 }
 document.addEventListener('scrunch-listening',event=>{
  const j=event.detail,live=j.state==='live';section.dataset.listening=String(live);section.querySelector('strong').textContent=live?'Listening':j.streamId?'Waiting for audio':'Not listening';
  section.querySelector('.listeningNote').textContent=live?'Level observations appear in Engineer. Track identity is not confirmed.':'Connect, then play audio in Live.';
  for(const c of Object.values(earsCanvases))c.hidden=true;
  if(!live)deferred=null;
  const e=observe(j,Date.now());if(e)deferred={...e,receivedAt:Date.now()};deliver();
 });
})();

/* Push to talk: hold Space to speak, let go to send. A studio-style listening panel with a
   segmented level meter replaces the waveform. Built on the existing dictation (#dictate), so
   live transcription, the Gemini fallback and the engineer route are unchanged. */
(()=>{
 const $=id=>document.getElementById(id);
 const dictate=$('dictate'),input=$('engineerInput'),send=$('sendEngineer');
 if(!dictate||!input||!send)return;
 const SEGMENTS=24,HOLD_MS=220;
 const panel=document.createElement('div');panel.id='pttPanel';panel.hidden=true;
 panel.innerHTML='<div class="ptt-mic"><svg viewBox="0 0 24 24" aria-hidden="true"><rect x="9" y="3" width="6" height="11" rx="3"/><path d="M5.5 11a6.5 6.5 0 0 0 13 0M12 17.5V21M8.5 21h7"/></svg><span>MIC</span></div>'+
  '<div class="ptt-body"><div class="ptt-top"><strong id="pttState">LISTENING</strong><em id="pttMode">PTT</em></div>'+
  '<div class="ptt-meter" aria-hidden="true">'+Array.from({length:SEGMENTS},()=>'<i></i>').join('')+'</div>'+
  '<small id="pttHint">Release Space to send</small></div>';
 const bar=dictate.closest('.commandBar')||dictate.parentElement;
 bar.parentElement.insertBefore(panel,bar);
 const cells=[...panel.querySelectorAll('.ptt-meter i')];
 const recording=()=>dictate.classList.contains('recording');

 // Level meter from its own analyser on the microphone (dictation keeps its own stream).
 let meter=null;
 async function meterOn(){
  try{const stream=await navigator.mediaDevices.getUserMedia({audio:true});const ctx=new AudioContext();
   const an=ctx.createAnalyser();an.fftSize=1024;ctx.createMediaStreamSource(stream).connect(an);
   const buf=new Float32Array(an.fftSize);let peak=0;
   const draw=()=>{if(!meter)return;an.getFloatTimeDomainData(buf);let s=0;for(const x of buf)s+=x*x;
    const db=20*Math.log10(Math.sqrt(s/buf.length)+1e-9);const lit=Math.max(0,Math.min(SEGMENTS,Math.round((db+60)/60*SEGMENTS)));
    peak=Math.max(lit,peak-0.35);cells.forEach((c,i)=>{c.className=i<lit?(i>=SEGMENTS-3?'on red':i>=SEGMENTS-8?'on amber':'on'):(i===Math.round(peak)-1?'peak':'');});
    meter.raf=requestAnimationFrame(draw);};
   meter={stream,ctx,raf:0};draw();
  }catch(e){}
 }
 function meterOff(){if(!meter)return;cancelAnimationFrame(meter.raf);meter.stream.getTracks().forEach(t=>t.stop());meter.ctx.close();meter=null;cells.forEach(c=>c.className='');}
 function show(state,hint){panel.hidden=false;$('pttState').textContent=state;$('pttHint').textContent=hint;}
 function hide(){panel.hidden=true;meterOff();}

 // After dictation stops, send as soon as the words are in and the engineer is free.
 function sendWhenReady(){
  show('SENDING','Transcribing…');const t0=Date.now();
  const tick=()=>{
   if(recording())return setTimeout(tick,100);
   const text=input.value.trim();
   if(text&&!send.disabled){hide();send.click();return;}
   if(Date.now()-t0>20000){hide();return;}
   setTimeout(tick,120);};
  setTimeout(tick,150);
 }
 new MutationObserver(()=>{if(recording()){show('LISTENING',ptt.active?'Release Space to send':'Press the mic to send');if(!meter)meterOn();}}).observe(dictate,{attributes:true,attributeFilter:['class']});
 // The mic button also sends when it stops (John rarely edits dictated text).
 dictate.addEventListener('click',()=>{if(recording()){meterOff();sendWhenReady();}},true);

 // Space: a tap types a space; holding it talks.
 const ptt={timer:0,active:false,before:null,field:null};
 const typing=el=>el&&(el.tagName==='TEXTAREA'||(el.tagName==='INPUT'&&/^(text|search|)$/.test(el.type))||el.isContentEditable);
 document.addEventListener('keydown',e=>{
  if(e.code!=='Space'||e.metaKey||e.ctrlKey||e.altKey)return;
  if(ptt.active){e.preventDefault();return;}
  if(e.repeat){if(ptt.timer)e.preventDefault();return;}
  const field=typing(document.activeElement)?document.activeElement:null;
  if(!field)e.preventDefault();
  if(recording()||dictate.disabled)return;
  ptt.field=field;ptt.before=field?field.value:null;
  ptt.timer=setTimeout(()=>{ptt.timer=0;ptt.active=true;
   if(ptt.field&&ptt.before!==null)ptt.field.value=ptt.before;   // remove spaces typed while holding
   dictate.click();},HOLD_MS);
 },true);
 document.addEventListener('keyup',e=>{
  if(e.code!=='Space')return;
  if(ptt.timer){clearTimeout(ptt.timer);ptt.timer=0;return;}
  if(!ptt.active)return;
  e.preventDefault();ptt.active=false;
  if(recording())dictate.click();   // the click listener stops the meter and sends
 },true);
 window.addEventListener('blur',()=>{if(ptt.active){ptt.active=false;if(recording())dictate.click();}});
 input.placeholder='Hold Space to talk, or type. '+(input.placeholder||'');
})();
