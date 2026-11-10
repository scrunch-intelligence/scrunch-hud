const $=id=>document.getElementById(id);
let token='',running=false,frozen=false,hidden=new Set(),lanes=[],timer=null,analyseAt=0;

const PALETTE=['#4ad991','#5cc8ff','#ffcf5c','#ff8a5c','#c08bff','#ff6b9d','#7ce0d3','#a3b1c6'];
const colourFor=(lane,i)=>lane.color||PALETTE[i%PALETTE.length];

// A kick peaking at -20 dBFS is a tenth of full scale: drawn against +-1 it is a few pixels and
// looks like a blip. Each lane is fitted to its own recent peak instead, held for a few seconds
// so the picture does not jump about between hits, and never magnified past 60x.
const MAX_GAIN=60,GAIN_DECAY=0.97,FLOOR=0.002;
const peaks={};
function gainFor(lane){
  const mode=$('gain').value;
  const peak=Math.max(FLOOR,...(lane.max||[0]).map(Math.abs),...(lane.min||[0]).map(Math.abs));
  const held=peaks[lane.stream]||peak;
  peaks[lane.stream]=Math.max(peak,held*GAIN_DECAY);
  if(mode!=='auto')return parseFloat(mode);
  return Math.min(MAX_GAIN,0.92/peaks[lane.stream]);
}

async function api(path,body){
  const r=await fetch('/api/scope/'+path,{method:'POST',headers:{'Content-Type':'application/json',
    'X-Scrunch-Token':token},body:JSON.stringify(body||{})});
  const d=await r.json();
  if(!r.ok)throw Error(d.error||'Scope error');
  return d;
}
function say(text,bad){$('status').textContent=text;$('status').className=bad?'bad':'';}

function laneRow(lane,i){
  const el=document.createElement('div');
  el.className='lane'+(hidden.has(lane.stream)?' off':'');
  el.innerHTML=`<div class="meta">
      <div class="name"><span class="swatch" style="background:${colourFor(lane,i)}"></span><span>${lane.name}</span></div>
      <div class="numbers" data-num></div>
      <div class="flag" data-flag></div>
      <div class="buttons"><button data-show>${hidden.has(lane.stream)?'Show':'Hide'}</button>
        <button data-solo>Only</button><button data-name title="name this lane">Name</button></div>
    </div><canvas data-canvas></canvas>`;
  el.querySelector('[data-show]').onclick=()=>{
    hidden.has(lane.stream)?hidden.delete(lane.stream):hidden.add(lane.stream);draw(lanes,true)};
  el.querySelector('[data-name]').onclick=async()=>{
    const label=prompt('Which track is this tap on?',lane.name);
    if(label===null)return;
    try{await api('name',{stream:lane.stream,label});await api('lanes');}catch(e){say(e.message,true)}
  };
  el.querySelector('[data-solo]').onclick=()=>{
    hidden=new Set(lanes.map(l=>l.stream).filter(s=>s!==lane.stream));draw(lanes,true)};
  return el;
}

function ensureRows(list){
  const box=$('lanes');
  if(box.childElementCount!==list.length||box.dataset.keys!==list.map(l=>l.stream).join()){
    box.innerHTML='';box.dataset.keys=list.map(l=>l.stream).join();
    list.forEach((lane,i)=>box.appendChild(laneRow(lane,i)));
    fillPickers(list);
  }
  return [...box.children];
}

function paint(canvas,lane,colour,height,gain){
  const dpr=window.devicePixelRatio||1;
  const w=canvas.clientWidth||600;
  if(canvas.width!==Math.round(w*dpr)||canvas.height!==Math.round(height*dpr)){
    canvas.width=Math.round(w*dpr);canvas.height=Math.round(height*dpr);
  }
  const g=canvas.getContext('2d');
  g.setTransform(dpr,0,0,dpr,0,0);
  g.clearRect(0,0,w,height);
  const mid=height/2;
  g.strokeStyle='#2a2f3a';g.lineWidth=1;
  g.beginPath();g.moveTo(0,mid+.5);g.lineTo(w,mid+.5);g.stroke();
  const lo=lane.min||[],hi=lane.max||[];
  if(!lo.length)return;
  const step=w/lo.length;
  g.strokeStyle=colour;g.lineWidth=Math.max(1,step*0.9);
  g.beginPath();
  const clamp=v=>Math.max(-1,Math.min(1,v*gain));
  for(let i=0;i<lo.length;i++){
    const x=i*step+step/2;
    const top=mid-clamp(hi[i])*(mid-2);
    const bottom=mid-clamp(lo[i])*(mid-2);
    g.moveTo(x,Math.min(top,bottom));g.lineTo(x,Math.max(top,bottom)+.5);
  }
  g.stroke();
}

function draw(list,structureOnly){
  lanes=list;
  const rows=ensureRows(list);
  const height=parseInt($('height').value,10);
  list.forEach((lane,i)=>{
    const row=rows[i];if(!row)return;
    row.className='lane'+(hidden.has(lane.stream)?' off':'');
    row.querySelector('[data-show]').textContent=hidden.has(lane.stream)?'Show':'Hide';
    const numbers=[];
    if(lane.peakDbFS!==undefined)numbers.push('peak '+lane.peakDbFS.toFixed(1)+' dB');
    if(lane.rmsDbFS!==undefined)numbers.push('rms '+lane.rmsDbFS.toFixed(1));
    if(lane.correlation!==null&&lane.correlation!==undefined)numbers.push('φ '+lane.correlation.toFixed(2));
    row.querySelector('[data-num]').textContent=numbers.join(' · ');
    const flag=row.querySelector('[data-flag]');
    const notes=[];
    if(!lane.live)notes.push('no audio');
    if(lane.namedFromLive===false)notes.push('not matched to a track');
    if(lane.dropped)notes.push(lane.dropped+' dropped');
    if(lane.error)notes.push(lane.error);
    if(lane.correlation!==null&&lane.correlation!==undefined&&lane.correlation<-0.2)notes.push('out of phase');
    flag.textContent=notes.join(' · ');
    flag.className='flag'+(lane.error||(lane.correlation<-0.2)?' bad':'');
    const canvas=row.querySelector('[data-canvas]');
    if(hidden.has(lane.stream)){canvas.getContext('2d').clearRect(0,0,canvas.width,canvas.height);return}
    const gain=gainFor(lane);
    if(gain>1.5)numbers.push('×'+gain.toFixed(gain<10?1:0));
    row.querySelector('[data-num]').textContent=numbers.join(' · ');
    if(!structureOnly||lane.min)paint(canvas,lane,colourFor(lane,i),height,gain);
  });
  $('empty').hidden=list.length>0;
  $('pair').hidden=list.length<2;
}

function fillPickers(list){
  for(const id of ['a','b']){
    const sel=$(id),was=sel.value;
    sel.innerHTML=list.map(l=>`<option value="${l.stream}">${l.name}</option>`).join('');
    if(list.some(l=>l.stream===was))sel.value=was;
    else if(id==='b'&&list.length>1)sel.value=list[1].stream;
  }
}

async function tick(){
  if(frozen||!running)return;
  try{
    const now=Date.now();
    const analyse=now-analyseAt>400;
    if(analyse)analyseAt=now;
    const d=await api('frame',{seconds:parseFloat($('seconds').value),columns:Math.min(1200,
      Math.round(($('lanes').clientWidth-176)||600)),analyse});
    draw(d.lanes);
    say(d.lanes.length+' lane'+(d.lanes.length===1?'':'s')+(d.aligned?' · aligned on Live’s clock':' · not aligned'));
  }catch(e){say(e.message,true)}
}

function bar(value,width,colour){
  return `<span class="bar" style="width:${Math.max(2,width)}px;background:${colour}"></span>`;
}

function verdict(d){
  if(!d.measurable)return `<div class="headline dim">${d.why}</div>`;
  const low=d.lowCorrelation;
  const phase=low===null?'<span class="dim">not enough low end to judge</span>':
    low>0.5?`<span class="good">low end is together (${low.toFixed(2)})</span>`:
    low>0?`<span class="warn">low end only loosely together (${low.toFixed(2)})</span>`:
    `<span class="bad">low end is fighting (${low.toFixed(2)}) — this is what disappears in mono</span>`;
  const t=d.transient;
  const hit=!t?'<span class="dim">no clear transients</span>':
    Math.abs(t.lagMs)<3?`<span class="good">they hit together (${t.lagMs} ms)</span>`:
    `<span class="warn">${d.tracks[1]} lands ${t.lagMs} ms ${t.lagMs>0?'after':'before'} ${d.tracks[0]}</span>`;
  const rows=d.masking.filter(m=>m.overlapping).map(m=>
    `<tr><td>${m.name}</td><td>${m.lowHz}–${m.highHz} Hz</td>
     <td>${m.aDbFS} dB</td><td>${m.bDbFS} dB</td>
     <td>${Math.abs(m.differenceDb)<3?'<span class="warn">level, fighting</span>':
        (m.differenceDb>0?d.tracks[0]:d.tracks[1])+' on top by '+Math.abs(m.differenceDb)+' dB'}</td></tr>`).join('');
  return `<div class="headline">Below ${d.belowHz} Hz: ${phase}</div>
    <div class="headline">Transients: ${hit}</div>
    ${rows?`<table><tr><th>band</th><th></th><th>${d.tracks[0]}</th><th>${d.tracks[1]}</th><th>who wins</th></tr>${rows}</table>`
          :'<div class="dim">They are not both loud in any one band right now.</div>'}`;
}

$('run').onclick=async()=>{
  try{
    if(running){await api('stop');running=false;$('run').textContent='Start';$('run').classList.remove('on');say('stopped')}
    else{const d=await api('start');running=true;$('run').textContent='Stop';$('run').classList.add('on');draw(d.lanes)}
  }catch(e){say(e.message,true)}
};
$('freeze').onclick=()=>{frozen=!frozen;$('freeze').classList.toggle('on',frozen);
  say(frozen?'frozen — the picture is held, the audio keeps running':'running')};
$('identify').onclick=async()=>{
  if(!confirm('To hear which lane is which, Scrunch switches each instrument off for about a third '
    +'of a second. An instrument switched off mid-note can be left with a note stuck on — a drone '
    +'that only reloading the Set clears. Safer with the music stopped and the parts short. '
    +'Use the Name button instead if you would rather not risk it. Go ahead?'))return;
  try{
    say('listening…');
    const d=await api('identify',{acceptHungNoteRisk:true});
    draw(d.lanes);
    const named=d.named.map(n=>n.track+' ('+n.fellBy+' dB)').join(', ');
    say(d.named.length?'named by listening: '+named:'could not tell any of them apart');
    if(d.unclear.length)say(($('status').textContent)+' · unclear: '+d.unclear.map(u=>u.track).join(', '));
  }catch(e){say(e.message,true)}
};
$('height').onchange=()=>draw(lanes);
$('compare').onclick=async()=>{
  try{
    const d=await api('relationship',{a:$('a').value,b:$('b').value,seconds:parseFloat($('seconds').value)});
    $('verdict').innerHTML=verdict(d);
  }catch(e){$('verdict').innerHTML='<div class="bad">'+e.message+'</div>'}
};

(async()=>{
  try{
    token=(await(await fetch('/api/state')).json()).token;
    const d=await api('lanes');
    running=d.running;$('run').textContent=running?'Stop':'Start';$('run').classList.toggle('on',running);
    draw(d.lanes);
    say(d.lanes.length?d.lanes.length+' tap'+(d.lanes.length===1?'':'s')+' found':'no taps yet');
  }catch(e){say(e.message,true)}
  timer=setInterval(tick,50);
})();
