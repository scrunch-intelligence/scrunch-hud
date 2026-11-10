const $=id=>document.getElementById(id);let token,knobs=[],sel=null,playing=false,sweeping=false,proposal=null;
function say(t,err){$('status').textContent=t||'';$('status').className=err?'err':''}
async function api(action,body){const r=await fetch('/api/coach/'+action,{method:'POST',headers:{'Content-Type':'application/json','X-Scrunch-Token':token},body:JSON.stringify(body||{})});const d=await r.json();if(!r.ok)throw Error(d.error||'Request failed');return d}
async function showLearned(){try{const d=await api('learned');
  const box=$('learned');if(!box)return;
  box.innerHTML=d.lines&&d.lines.length?('<b>What Scrunch has picked up from you</b><br>'+d.lines.map(l=>'· '+l).join('<br>')):'';
}catch(e){}}
async function load(){const d=await(await fetch('/api/coach/knobs')).json();if(d.error)throw Error(d.error);showLearned();$('session').textContent=(d.preset||d.session)+' · '+d.plugin+(d.family?' · ★ = try first for a '+d.family.toLowerCase():'');knobs=d.knobs;showInstances(d.instances);draw()}
function showInstances(list){
  const box=$('instances');if(!box)return;
  if(!list||list.length<2){box.innerHTML='';return}                 // one instrument: nothing to choose
  box.innerHTML='<small>This Set has '+list.length+' of them — working on: </small>'+list.map(i=>
    '<button class="inst'+(i.inUse?' on':'')+'" data-d="'+i.device+'"'+(i.knobsExposed?'':' disabled title="knobs not exposed in this Set"')+'>'
    +(i.trackName||i.device)+'</button>').join(' ');
  for(const b of box.querySelectorAll('button.inst'))b.onclick=()=>act(async()=>{
    await api('use',{device:b.dataset.d});await load();return {}},'Switched instrument');
}
function draw(){const q=$('filter').value.toLowerCase();$('list').innerHTML='';
  const order=[...knobs].sort((x,y)=>(x.suggested||999)-(y.suggested||999));
  for(const k of order){if(q&&!(k.label+' '+(k.lesson||'')).toLowerCase().includes(q))continue;
    const el=document.createElement('div');el.className='knob'+(sel&&sel.parameter===k.parameter?' sel':'')+(k.lesson?'':' nolesson');
    el.innerHTML='<span></span><small></small>';el.children[0].textContent=(k.suggested?'★ ':'')+k.label;el.children[1].textContent=k.display||'';
    el.onclick=()=>choose(k);$('list').appendChild(el)}}
function choose(k){sel=k;$('title').textContent=k.label;$('value').textContent=k.display||k.value;$('lesson').textContent=k.lesson||'No lesson written for this knob yet.';draw()}
function show(r){if(r.now||r.display){$('value').textContent=r.now||r.display}if(r.lesson)$('lesson').textContent=r.lesson}
async function act(fn,label){try{say(label+'…');const r=await fn();show(r);say(r.question||r.says||label);await refreshValue();return r}catch(e){say(e.message,true)}}
async function refreshValue(){try{const d=await(await fetch('/api/coach/knobs')).json();if(d.knobs){knobs=d.knobs;if(sel){sel=knobs.find(k=>k.parameter===sel.parameter)||sel;if(!sweeping)$('value').textContent=sel.display}draw()}}catch(e){}}
function side(which,display){$(which==='A'?'aval':'bval').textContent=display||'';
  $('a').classList.toggle('playing',which==='A');$('b').classList.toggle('playing',which==='B')}
function need(){if(!sel)throw Error('Pick a knob first.')}
const moves={
  play:()=>act(async()=>{const r=playing?await api('halt'):await api('play');playing=!!r.playing;return r},'Playback'),
  stop:()=>act(async()=>{const r=await api('stop');sweeping=false;return r},"That's it"),
  up:()=>act(async()=>{need();const r=await api('start',{parameter:sel.parameter,direction:'up'});sweeping=true;return r},'Sweeping up — hit Enter when it sounds right'),
  down:()=>act(async()=>{need();const r=await api('start',{parameter:sel.parameter,direction:'down'});sweeping=true;return r},'Sweeping down — hit Enter when it sounds right'),
  back:()=>act(()=>api('back'),'Back a bit'),
  other:()=>act(async()=>{const r=await api('other');sweeping=true;return r},'Going the other way — Enter to stop'),
  ab:()=>act(()=>{need();$('aval').textContent=sel.display||'A';$('bval').textContent='…';const span=sel.max-sel.min,b=sel.value<(sel.min+span/2)?Math.min(sel.max,sel.value+span*.25):Math.max(sel.min,sel.value-span*.25);return api('ab',{parameter:sel.parameter,a:sel.value,b})},'Playing A — press B to compare').then(r=>{if(r)side('A',r.display)}),
  a:()=>act(async()=>{const r=await api('side',{side:'A'});side('A',r.display);return r},'A'),
  b:()=>act(async()=>{const r=await api('side',{side:'B'});side('B',r.display);return r},'B'),
  undo:()=>act(()=>api('undo'),'Undone'),
  keep:()=>act(async()=>{const r=await api('keep',{words:$('words').value});$('words').value='';return {now:r.to.display}},'Kept and remembered'),
};
function setMode(){const learn=$('learn').checked;document.body.classList.toggle('engineer',!learn);
  try{localStorage.setItem('coachLearn',learn?'1':'0')}catch(e){}}
$('learn').addEventListener('change',setMode);
try{$('learn').checked=localStorage.getItem('coachLearn')!=='0'}catch(e){$('learn').checked=true}
setMode();
function showMove(r){const box=$('moved');box.innerHTML='';
  if(r.says){const reply=document.createElement('div');reply.textContent=r.says;box.appendChild(reply)}
  if(r.question){const question=document.createElement('div');question.textContent=r.question;box.appendChild(question);return}
  if(r.reverted){box.innerHTML='<small>Reverted to where the patch was.</small>';return}
  const head=document.createElement('div');head.textContent=(r.move||'')+' · '+Math.round((r.amount||0)*100)+'%';box.appendChild(head);
  for(const k of r.knobs||[]){const el=document.createElement('div');el.innerHTML='<b></b> <span></span><small></small>';
    el.children[0].textContent=k.label;el.children[1].textContent=(k.was||'')+' → '+(k.now||'');
    el.children[2].textContent=' — '+k.why;box.appendChild(el)}}
Object.assign(moves,{
  go:()=>act(async()=>{const words=$('ask').value.trim();if(!words)throw Error('Describe the sound you want.');const r=await api('instruct',{words});
    if(r.proposal){proposal=r.proposal;$('moved').innerHTML='';
      const d=document.createElement('div');d.textContent=r.says;
      const b=document.createElement('button');b.textContent='Rebuild ('+r.proposal.seconds+'s)';
      b.onclick=()=>act(async()=>{const done=await api('effect',{slot:proposal.slot,effect:proposal.effect});
        proposal=null;await load();$('moved').innerHTML='<small>'+done.effect+' is now in slot '+done.slot+'. '+done.note+'</small>';return {}},'Rebuilt');
      $('moved').appendChild(d);$('moved').appendChild(b);return r}
    showMove(r);return r},'Change applied'),
  more:()=>act(async()=>{const r=await api('steer',{direction:'more'});showMove(r);return r},'More'),
  less:()=>act(async()=>{const r=await api('steer',{direction:'less'});showMove(r);return r},'Less'),
  wrong:()=>act(async()=>{const r=await api('steer',{direction:'wrong direction'});showMove(r);return r},'Turned around'),
  revert:()=>act(async()=>{const r=await api('steer',{direction:'revert'});showMove(r);return r},'Reverted'),
  levels:()=>act(async()=>{const r=await api('match-level');
    $('moved').insertAdjacentHTML('beforeend','<small>'+(r.matched?'Levels matched':'Still '+r.louderByDb+' dB apart')+' — volume was '+r.volumeFrom+'</small>');return {}},'Level matched'),
  check:()=>act(async()=>{const v=await api('verify');
    const bits=[v.result==='achieved'?'Confirmed in the audio':v.result==='no change'?'The audio barely moved':v.result==='wrong way'?'The audio went the other way — put back':'Not measurable'];
    if(v.measured)bits.push(v.measured.what==='brightnessHz'?('brightness '+v.measured.before+' → '+v.measured.after+' Hz'):('low end '+v.measured.before+' → '+v.measured.after+' dB'));
    if(v.loudnessChangeLU!==null&&v.loudnessChangeLU!==undefined)bits.push((v.loudnessChangeLU>0?'+':'')+v.loudnessChangeLU+' LU');
    $('moved').insertAdjacentHTML('beforeend','<small>'+bits.join(' · ')+'</small>');return {}},'Listening…'),
  save:()=>act(async()=>{
    const name=prompt('Name this sound. It is saved into the plug-in\u2019s own User folder, so it shows up in its preset browser.');
    if(!name)return {};
    const r=await api('save-sound',{name});
    $('moved').insertAdjacentHTML('beforeend','<small>'+r.say+' ('+(r.knobsMatched||r.settingsCarried)+' settings checked against what is playing)</small>');
    return {}},'Saved'),
  keepmove:()=>act(async()=>{await api('keep-move',{words:$('ask').value});$('moved').innerHTML='<small>Kept.</small>';showLearned();return {}},'Kept and remembered'),
});
for(const id of Object.keys(moves))$(id).onclick=moves[id];
$('ask').addEventListener('keydown',e=>{if(e.key==='Enter'){e.preventDefault();moves.go()}});
$('filter').oninput=draw;
let soundTimer;$('soundq').oninput=()=>{clearTimeout(soundTimer);soundTimer=setTimeout(findSounds,250)};
async function findSounds(){try{const d=await(await fetch('/api/coach/presets?q='+encodeURIComponent($('soundq').value))).json();if(d.error)throw Error(d.error);
  $('sounds').innerHTML='';for(const p of d.presets){const el=document.createElement('div');el.className='sound';
    el.innerHTML='<span class="heart"></span><b></b><small></small>';el.children[0].textContent=p.liked?'♥ ':'';el.children[1].textContent=p.name;el.children[2].textContent=p.subtype+(p.notes?' — '+p.notes.slice(0,90):'');
    el.onclick=()=>switchTo(p);$('sounds').appendChild(el)}}catch(e){say(e.message,true)}}
async function switchTo(p){if(playing)await api('halt').catch(()=>{});playing=false;sweeping=false;sel=null;
  say('Loading '+p.name+' into the JUP — about 15 seconds…');try{await api('switch',{path:p.path});await load();$('title').textContent=p.name;$('value').textContent='—';$('lesson').textContent=p.notes||'Loaded. Press Space to hear it, then pick a knob.';say('Loaded '+p.name)}catch(e){say(e.message,true)}}
findSounds();
const keys={' ':'play','Enter':'stop','ArrowUp':'up','ArrowDown':'down','ArrowLeft':'back','o':'other','t':'ab','a':'a','b':'b','u':'undo','l':'levels','v':'check',
             '+':'more','=':'more','-':'less','w':'wrong','r':'revert','k':'keepmove','s':'save'};
document.addEventListener('keydown',e=>{if(e.target.tagName==='INPUT'&&!(e.key==='Enter'&&e.target.id==='words'))return;
  const m=e.target.id==='words'?'keep':keys[e.key.length===1?e.key.toLowerCase():e.key];if(m){e.preventDefault();moves[m]()}});
setInterval(()=>{if(sweeping)fetch('/api/coach/knobs').then(r=>r.json()).then(d=>{if(d.knobs&&sel){const k=d.knobs.find(x=>x.parameter===sel.parameter);if(k)$('value').textContent=k.display}}).catch(()=>{})},400);
(async()=>{try{token=(await(await fetch('/api/state')).json()).token;await load()}catch(e){$('session').textContent='';say(e.message,true)}})();
