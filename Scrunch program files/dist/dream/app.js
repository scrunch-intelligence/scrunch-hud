const $=id=>document.getElementById(id);
let velem=null,picked=new Set(),countIn=null,KEY='',take=null,voice=null,rec=null,chunks=[],clickTimer=null,tick=null;
try{const u=new URL(location.href),k=u.searchParams.get('k');if(k){localStorage.setItem('dreamKey',k);history.replaceState(null,'','/')}KEY=localStorage.getItem('dreamKey')||'';
  $('who').value=localStorage.getItem('dreamWho')||''}catch(e){}
$('pair').hidden=!!KEY;
$('who').onchange=()=>{try{localStorage.setItem('dreamWho',$('who').value)}catch(e){}};
if('serviceWorker' in navigator)navigator.serviceWorker.register('/sw.js').catch(()=>{});
const say=t=>$('status').textContent=t;
async function api(path,opt={}){opt.headers=Object.assign({'X-Dream-Key':KEY},opt.headers||{});const r=await fetch(path,opt);
  if(!r.ok){let m='Request failed';try{m=(await r.json()).error||m}catch(e){}throw Error(m)}return r}
const GRID={'1/16':.25,'1/8':.5,'1/4':1};
function view(){ // what the phone plays: raw notes + the preview corrections (the raw take on the Mac is unchanged)
  if(!take)return[];const sc=$('snap').checked&&take.key?new Set(take.key.scale):null,oct=+$('oct').value,g=GRID[$('q').value],bpm=take.tempo.bpm,ph=take.tempo.firstBeatSeconds,spb=60/bpm;
  return take.notes.map(n=>{let s=n.s,e=n.e;if(g){const q=x=>ph+Math.round((x-ph)/(g*spb))*g*spb;s=Math.max(0,q(s));e=Math.max(s+g*spb,q(e))}let p=n.p;if(sc&&!sc.has(((p%12)+12)%12)){const up=sc.has((p+1)%12),dn=sc.has((p+11)%12);p=up&&(!dn||(n.cents||0)>0)?p+1:dn?p-1:p}return{p:p+12*oct,s,e,v:n.v,support:n.support,moved:p!==n.p}})}
function draw(){const c=$('roll'),r=c.getBoundingClientRect(),dpr=devicePixelRatio||1;c.width=r.width*dpr;c.height=160*dpr;const x=c.getContext('2d');x.scale(dpr,dpr);x.clearRect(0,0,r.width,160);
  const ns=view();if(!ns.length){x.fillStyle='#9a94ad';x.fillText('No notes found',12,80);return}
  const lo=Math.min(...ns.map(n=>n.p))-2,hi=Math.max(...ns.map(n=>n.p))+2,T=Math.max(take.seconds,...ns.map(n=>n.e)),W=r.width,H=160,ph=(H-10)/(hi-lo+1);
  x.strokeStyle='#2a2639';const spb=60/take.tempo.bpm;for(let t=take.tempo.firstBeatSeconds;t<T;t+=spb){x.beginPath();x.moveTo(t/T*W,0);x.lineTo(t/T*W,H);x.stroke()}
  ns.forEach(n=>{x.globalAlpha=n.support==null?1:.35+.65*n.support;x.fillStyle=`hsl(${(n.p%12)*30},70%,62%)`;const r=[n.s/T*W,5+(hi-n.p)*ph,Math.max(2,(n.e-n.s)/T*W),Math.max(3,ph-1)];x.fillRect(...r);if(n.moved){x.strokeStyle='#fff';x.lineWidth=1.5;x.strokeRect(...r)}})}
const NAMES=['C','C#','D','D#','E','F','F#','G','G#','A','A#','B'];
async function open(id){say('Loading…');DreamSynth.stop();if(velem){velem.pause();velem=null}take=await(await api('/api/takes/'+id)).json();voice=null;$('speed').value=Math.round(take.tempo.bpm);$('take').hidden=false;$('name').value=take.name;
  if(take.suggested){$('oct').value=take.suggested.octave;$('q').value=take.suggested.quantize;$('snap').checked=take.suggested.snap!==false}else $('snap').checked=true;
  const t=take.tempo,lo=take.notes.length?Math.min(...take.notes.map(n=>n.p)):0;
  $('info').textContent=`${take.notes.length} notes · ${t.bpm} bpm (${t.source==='click'?'click':'found from your timing'}) · ${take.seconds}s · lowest ${NAMES[lo%12]}${Math.floor(lo/12)-2}${take.key?` · key ${take.key.name} (guess)`:''}${take.status!=='phone'?' · sent to Scrunch':''}${take.combined?` · combined from ${take.combined.method}; faint notes = fewer versions agreed`:''}`;
  const hasVoice=!take.combined;$('orig').hidden=!hasVoice;$('both').hidden=!hasVoice;$('rep').hidden=!hasVoice;
  $('send').disabled=take.status!=='phone';$('send').textContent=take.status==='phone'?'Send to Scrunch':'Sent ✓';$('del').hidden=take.status!=='phone';draw();say('')}
async function voiceBuf(){if(voice)return voice;const c=DreamSynth.audio(),b=await(await api(`/api/takes/${take.id}/audio`)).arrayBuffer();
  voice=await new Promise((ok,no)=>c.decodeAudioData(b,ok,no));const d=voice.getChannelData(0);let s=0,n=0;for(let i=0;i<d.length;i+=4){if(Math.abs(d[i])>.01){s+=d[i]*d[i];n++}}
  voice.gainMatch=Math.min(8,.12/Math.sqrt(s/Math.max(1,n)||1e-4));return voice}   // volume-match the voice to the synth level
const rate=()=>{const v=+$('speed').value;return take&&v>=40&&v<=240?v/take.tempo.bpm:1};
async function ready(){const k=$('sound').value;if(DreamSynth.INST[k].dir){say('Loading '+DreamSynth.INST[k].label+'…');await DreamSynth.load(k);say('')}}
{const sel=$('sound');sel.replaceChildren();Object.entries(DreamSynth.INST).forEach(([k,v])=>{const o=document.createElement('option');o.value=k;o.textContent=v.label;sel.append(o)});
 try{sel.value=localStorage.getItem('dreamSound')||'piano'}catch(e){}sel.onchange=()=>{try{localStorage.setItem('dreamSound',sel.value)}catch(e){};ready()}}
async function voicePlay(gainScale){ // the voice at the chosen speed, pitch kept (browser time-stretch)
  const b=await voiceBuf();if(!velem){const blob=await(await api(`/api/takes/${take.id}/audio`)).blob();velem=new Audio(URL.createObjectURL(blob));
    const g=DreamSynth.audio().createGain();velem.gainNode=g;DreamSynth.audio().createMediaElementSource(velem).connect(g).connect(DreamSynth.out)}
  velem.gainNode.gain.value=b.gainMatch*gainScale;velem.preservesPitch=true;velem.webkitPreservesPitch=true;velem.playbackRate=rate();velem.currentTime=0;await velem.play()}
function stopAll(){DreamSynth.stop();if(velem)velem.pause()}
$('play').onclick=async()=>{stopAll();await ready();DreamSynth.play($('sound').value,view(),.08,rate())};
$('orig').onclick=async()=>{stopAll();await voicePlay(1)};
$('both').onclick=async()=>{stopAll();await ready();await voicePlay(.7);DreamSynth.play($('sound').value,view(),.02,rate())};
$('stop').onclick=stopAll;
$('speedReset').onclick=()=>{if(take)$('speed').value=Math.round(take.tempo.bpm)};
async function combine(ids){say('Combining…');try{const t=await(await api('/api/combine',{method:'POST',body:JSON.stringify({ids})})).json();picked.clear();await open(t.id);list();
  say(`Combined ${t.combined.versions} versions: ${t.notes.length} notes; ${Math.round(t.combined.meanSupport*100)}% agreement on average.`)}catch(e){say(e.message)}}
$('rep').onclick=()=>take&&combine([take.id]);
$('comb').onclick=()=>combine([...picked]);
['oct','q','snap'].forEach(i=>$(i).onchange=draw);addEventListener('resize',()=>take&&draw());
$('name').onchange=async()=>{if(take)await api(`/api/takes/${take.id}/rename`,{method:'POST',body:JSON.stringify({name:$('name').value})});list()};
$('send').onclick=async()=>{try{await api(`/api/takes/${take.id}/send`,{method:'POST',body:JSON.stringify({octave:+$('oct').value,quantize:$('q').value,snap:$('snap').checked})});await open(take.id);list();say('Sent. John will see it in Scrunch.')}catch(e){say(e.message)}};
$('del').onclick=async()=>{if(!confirm('Delete this take?'))return;await api(`/api/takes/${take.id}/delete`,{method:'POST',body:'{}'});take=null;$('take').hidden=true;list()};
$('dl').onclick=async()=>{const b=await(await api(`/api/takes/${take.id}/midi?octave=${$('oct').value}&quantize=${encodeURIComponent($('q').value)}&bpm=${+$('speed').value||''}&snap=${$('snap').checked?1:0}`)).blob();
  const a=document.createElement('a');a.href=URL.createObjectURL(b);a.download=(take.name||'dream')+'.mid';a.click();setTimeout(()=>URL.revokeObjectURL(a.href),5000)};
async function list(){if(!KEY)return;try{const {takes}=await(await api('/api/takes')).json();const ul=$('list');ul.replaceChildren();
  takes.forEach(t=>{const li=document.createElement('li'),a=document.createElement('span'),b=document.createElement('span'),cb=document.createElement('input');cb.type='checkbox';cb.checked=picked.has(t.id);
    cb.onclick=e=>{e.stopPropagation();cb.checked?picked.add(t.id):picked.delete(t.id);$('comb').hidden=picked.size<2};li.append(cb);a.className='n';a.textContent=t.name;const sm=document.createElement('small');sm.textContent=` ${t.noteCount} notes · ${t.tempo.bpm} bpm`;a.append(sm);
    b.className='s';b.textContent=t.status==='phone'?'':t.status==='placed'?'in Ableton':'sent';li.append(a,b);li.onclick=()=>open(t.id);ul.append(li)});$('comb').hidden=picked.size<2}catch(e){say(e.message)}}
function pickType(){for(const t of['audio/webm;codecs=opus','audio/mp4','audio/webm','audio/ogg'])if(window.MediaRecorder&&MediaRecorder.isTypeSupported&&MediaRecorder.isTypeSupported(t))return t;return''}
async function start(){if(!KEY)return say('Pair this phone first.');DreamSynth.stop();
  let stream;try{stream=await navigator.mediaDevices.getUserMedia({audio:{echoCancellation:false,noiseSuppression:false,autoGainControl:true}})}
  catch(e){return say(location.protocol!=='https:'&&location.hostname!=='localhost'?'The microphone needs the https Dream link.':'Microphone blocked: allow it in the browser settings.')}
  const type=pickType();rec=new MediaRecorder(stream,type?{mimeType:type}:{});chunks=[];rec.ondataavailable=e=>e.data.size&&chunks.push(e.data);
  rec.onstop=()=>{stream.getTracks().forEach(t=>t.stop());upload(new Blob(chunks,{type:rec.mimeType||type||'audio/mp4'}))};
  const useClick=$('click').checked,bpm=+$('bpm').value,c=DreamSynth.audio();
  if(useClick){const spb=60/bpm;let t=c.currentTime+.1,i=0;for(let k=0;k<4;k++)DreamSynth.click(t+k*spb,k===0); // one bar count-in
    const begin=t+4*spb;countIn=setTimeout(()=>{countIn=null;rec.start(250);go()},(begin-c.currentTime-.02)*1000);let nx=begin;
    clickTimer=setInterval(()=>{while(nx<c.currentTime+.3){DreamSynth.click(nx,i%4===0);nx+=spb;i++}},100);say('Count-in…')}
  else{rec.start(250);go()}}
function go(){const t0=Date.now();$('rec').classList.add('on');$('rec').textContent='■';say('Recording — hum away');
  tick=setInterval(()=>{const s=(Date.now()-t0)/1000;$('timer').textContent=`${Math.floor(s/60)}:${String(Math.floor(s%60)).padStart(2,'0')}`;if(s>180)stop()},200)}
function stop(){if(countIn){clearTimeout(countIn);countIn=null;rec.onstop=null;rec.stream.getTracks().forEach(t=>t.stop());say('')}clearInterval(clickTimer);clearInterval(tick);clickTimer=tick=null;DreamSynth.stop();$('rec').classList.remove('on');$('rec').textContent='●';$('timer').textContent='Tap to hum';if(rec&&rec.state!=='inactive')rec.stop()}
async function upload(blob){say('Listening to your take…');const h={'Content-Type':blob.type.split(';')[0],'X-Who':encodeURIComponent($('who').value||'')};
  if($('click').checked)h['X-Click-Bpm']=$('bpm').value;
  try{const t=await(await api('/api/takes',{method:'POST',headers:h,body:blob})).json();await open(t.id);list();say(t.notes.length?'Here it is. Pick a sound and play.':'No notes heard — try humming a bit louder, closer to the phone.')}catch(e){say('Could not transcribe: '+e.message)}}
$('rec').onclick=()=>rec&&rec.state==='recording'||clickTimer?stop():start();
list();
