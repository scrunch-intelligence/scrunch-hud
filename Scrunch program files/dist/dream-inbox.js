const $=id=>document.getElementById(id);let token='';
const el=(t,txt,cls)=>{const e=document.createElement(t);if(txt!=null)e.textContent=txt;if(cls)e.className=cls;return e};
async function post(path,body){const r=await fetch(path,{method:'POST',headers:{'Content-Type':'application/json','X-Scrunch-Token':token},body:JSON.stringify(body)});const d=await r.json();if(!r.ok)throw Error(d.error||'Request failed');return d}
function sel(opts,val){const s=el('select');opts.forEach(([v,l])=>{const o=el('option',l);o.value=v;s.append(o)});s.value=String(val);return s}
function card(t){const c=el('section');c.append(el('h2',t.name));
  c.append(el('p',`${t.by?t.by+' · ':''}${t.noteCount} notes · ${t.seconds}s · ${t.tempo.bpm} bpm (${t.tempo.source}) · ${new Date(t.created).toLocaleString()}`,'muted'));
  const a=el('audio');a.controls=true;a.preload='none';a.src='/api/dream/audio/'+t.id;c.append(a);
  if(t.status==='placed'){c.append(el('p',`In Live at bar ${t.placed.bar} (${t.placed.mode} timing, ${t.placed.notes} notes, read back).`));return c}
  const f=el('form');const bar=el('input');bar.type='number';bar.min=1;bar.value=1;
  const mode=sel([['pulse','Fit to Set tempo (player’s beat → Set beats)'],['time','Keep exact timing']],'pulse');
  const sug=t.suggested||{octave:0,quantize:''};
  const oct=sel([[2,'Octave +2'],[1,'Octave +1'],[0,'Octave 0'],[-1,'Octave −1'],[-2,'Octave −2']],sug.octave);
  const q=sel([['','No quantize'],['1/16','1/16'],['1/8','1/8'],['1/4','1/4']],sug.quantize);const snap=el('input');snap.type='checkbox';snap.checked=sug.snap!==false;
  const lab=(txt,x)=>{const l=el('label',txt);l.append(x);return l};
  f.append(lab('Bar ',bar),lab('Timing ',mode),lab('',oct),lab('',q));if(t.key)f.append(lab(`In key (${t.key.name}) `,snap));const go=el('button','Place in Live');const del=el('button','Remove');del.type='button';f.append(go,del);
  if(t.suggested&&(t.suggested.octave||t.suggested.quantize))c.append(el('p',`Player suggested: octave ${t.suggested.octave>0?'+':''}${t.suggested.octave}${t.suggested.quantize?', quantize '+t.suggested.quantize:''}.`,'muted'));
  f.onsubmit=async e=>{e.preventDefault();go.disabled=true;$('status').textContent='Placing in Live…';
    try{const r=await post('/api/dream/place',{id:t.id,bar:+bar.value,mode:mode.value,octave:+oct.value,quantize:q.value,snap:snap.checked});$('status').textContent=`Placed "${r.name}" at bar ${r.placed.bar}: ${r.placed.notes} notes, read back.`;load()}
    catch(err){$('status').textContent=err.message;go.disabled=false}};
  del.onclick=async()=>{if(!confirm('Remove this take and its files?'))return;await post('/api/dream/delete',{id:t.id});load()};
  c.append(f);return c}
async function load(){try{const {takes}=await(await fetch('/api/dream/takes')).json();$('takes').replaceChildren(...takes.map(card));$('status').textContent=takes.length?'':'No takes yet. Send one from the phone.'}catch(e){$('status').textContent=e.message}}
$('refresh').onclick=load;
(async()=>{token=(await(await fetch('/api/state')).json()).token;load()})();
