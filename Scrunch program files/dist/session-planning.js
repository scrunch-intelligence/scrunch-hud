// Read-only planning UI: no execution, rollback, or recovery-resolve endpoint.
(() => {
  const panel=document.createElement('details');panel.id='sessionPlanning';
  panel.innerHTML=`<summary>Session plans &amp; recovery</summary>
    <p>Review changes against the current Live Set. These previews do not change or play your music.</p>
    <button id="planRefresh">Read current session</button><p id="planStatus" role="status">Read the session to choose tracks.</p>
    <fieldset disabled id="planFields"><legend>Mixer preview</legend>
    <label>Track<select id="planTrack"></select></label>
    <label>Control<select id="planParameter"><option value="gainDb">Volume (dB)</option><option value="pan">Pan (−1 left to +1 right)</option></select></label>
    <label>Proposed value<input id="planValue" type="number" min="-70" max="0" step="0.1" value="-12"></label>
    <button id="planMixer">Preview mixer change</button>
    <h3>Group / return proposal</h3><label>Tracks for the group<select id="planMembers" multiple size="5"></select></label>
    <label>New group name<input id="planGroup" maxlength="100" placeholder="Optional, e.g. Rhythm"></label>
    <label>New return name<input id="planReturn" maxlength="100" placeholder="Optional, e.g. Room"></label>
    <p>A return starts without an effect or sends. No sound or routing changes occur.</p><button id="planArchitecture">Preview structure</button>
    <h3>Production feedback</h3><p>Attach one engineer comment to the Track selected above. These notes do not authorize changes.</p>
    <label>Feedback source<input id="feedbackSource" maxlength="200" placeholder="e.g. Mastering engineer’s notes"></label>
    <label>Comment<textarea id="feedbackText" maxlength="4000"></textarea></label>
    <label>Optional cue start (seconds)<input id="feedbackStart" type="number" min="0" max="86400" step="0.001"></label>
    <label>Optional cue end (seconds)<input id="feedbackEnd" type="number" min="0" max="86400" step="0.001"></label>
    <button id="feedbackSave">Save feedback association</button></fieldset>
    <div id="planResult" aria-live="polite"></div>
    <h3>Saved plans and recovery</h3><button id="planHistory">Read saved plans</button><div id="planHistoryList"></div>
    <details><summary>Technical details</summary><pre id="planTechnical"></pre></details>`;
  document.querySelector('#engineerWorkspace .workspace').append(panel);
  const el=id=>document.getElementById(id);let busy=false,observationFingerprint=null;
  async function request(path,body){
    const headers={};if(body){const r=await fetch('/api/state');if(!r.ok)throw Error('Cannot authenticate local request.');headers['X-Scrunch-Token']=(await r.json()).token;headers['Content-Type']='application/json';}
    const r=await fetch(path,{method:body?'POST':'GET',headers,body:body?JSON.stringify(body):undefined,signal:AbortSignal.timeout(30000)});
    const data=await r.json();if(!r.ok)throw Error(data.error||'Request failed.');return data;
  }
  function show(lines,data){el('planResult').replaceChildren(...lines.map(line=>{const p=document.createElement('p');p.textContent=line;return p;}));el('planTechnical').textContent=JSON.stringify(data,null,2);}
  async function run(work){if(busy)return;busy=true;el('planStatus').textContent='Reading and verifying…';try{await work();el('planStatus').textContent='Read complete. No Live changes made.';}catch(e){show([],{});el('planStatus').textContent='Could not complete: '+e.message;}finally{busy=false;}}
  el('planRefresh').onclick=()=>run(async()=>{
    observationFingerprint=null;el('planFields').disabled=true;el('planTrack').replaceChildren();el('planMembers').replaceChildren();
    const s=await request('/api/session/state');if(!s.available)throw Error(s.error||'Session unavailable.');
    for(const t of s.tracks){const option=new Option(t.name||'Unnamed track',t.id);el('planTrack').add(option);
      if(['audio','midi'].includes(t.type)&&!t.isGroup&&!t.groupId)el('planMembers').add(new Option(t.name||'Unnamed track',t.id));}
    observationFingerprint=s.fingerprint;el('planFields').disabled=!s.tracks.length;
    show(['Session: '+(s.session?.name||'Unnamed'),s.tracks.length+' tracks read.','Execution remains unavailable until routing, latency and rollback acceptance passes.'],s);
  });
  el('planParameter').onchange=()=>{const pan=el('planParameter').value==='pan';el('planValue').min=pan?'-1':'-70';el('planValue').max=pan?'1':'0';el('planValue').value=pan?'0':'-12';};
  el('planMixer').onclick=()=>run(async()=>{
    if(!el('planValue').checkValidity()||el('planValue').value==='')throw Error('Enter a value within the displayed control limits.');
    const p=await request('/api/session/preview',{observationFingerprint,actions:[{trackId:el('planTrack').value,parameter:el('planParameter').value,value:Number(el('planValue').value)}]});
    show([...p.actions.map(a=>`${a.track}: ${a.parameter==='gainDb'?'volume':'pan'} ${a.before} → ${a.after}${a.parameter==='gainDb'?' dB':''}.`),'Preview saved. No change executed; no approval has been assumed.'],p);
  });
  el('planArchitecture').onclick=()=>run(async()=>{
    const name=el('planGroup').value.trim(),room=el('planReturn').value.trim(),members=Array.from(el('planMembers').selectedOptions,o=>o.value);
    if(!name&&!room)throw Error('Name a group or return to preview.');if(name&&!members.length)throw Error('Select tracks for the proposed group.');
    const p=await request('/api/session/architecture-proposal',{observationFingerprint,groups:name?[{name,members}]:[],returns:room?[room]:[],sends:[]});
    show([...(name?[`Proposed group: ${name}, containing ${members.length} selected tracks.`]:[]),...(room?[`Proposed empty return: ${room}.`]:[]),'Saved for review only. No group, return, send or processor was created.'],p);
  });
  el('feedbackSave').onclick=()=>run(async()=>{
    const comment={text:el('feedbackText').value,trackId:el('planTrack').value};
    if(el('feedbackStart').value!==''||el('feedbackEnd').value!==''){
      if(el('feedbackStart').value===''||el('feedbackEnd').value==='')throw Error('Enter both cue start and end.');
      comment.startSeconds=Number(el('feedbackStart').value);comment.endSeconds=Number(el('feedbackEnd').value);
    }
    const report=await request('/api/session/feedback',{observationFingerprint,sourceLabel:el('feedbackSource').value,comments:[comment]});
    show(report.summary.split('\n'),report);
  });
  el('planHistory').onclick=()=>run(async()=>{
    const s=await request('/api/session/transactions');el('planHistoryList').replaceChildren();
    for(const item of s.transactions){const row=document.createElement('div'),p=document.createElement('p');p.textContent=item.status.replaceAll('-',' ')+': '+item.summary.join('; ');row.append(p);
      if(item.requiresRecovery&&item.status!=='unreadable'){const b=document.createElement('button');b.textContent='Inspect current values';b.onclick=()=>run(async()=>{
        const result=await request('/api/session/recovery-preview',{planId:item.id});
        show(['Recovery inspection only. The saved journal remains unresolved.','Original and current values are listed below; matching values alone do not prove restoration.'],result);
        for(const target of result.targets||[]){const p=document.createElement('p');p.textContent=`${target.track}: ${target.parameter==='gainDb'?'volume':'pan'} — original ${target.original}, planned ${target.planned}, current ${target.current??'unavailable'}. ${target.state.replaceAll('-',' ')}.`;el('planResult').append(p);}
      });row.append(b);}el('planHistoryList').append(row);}
    if(!s.transactions.length)el('planHistoryList').textContent='No saved mixer plans.';
    if(s.truncated)el('planHistoryList').append(document.createTextNode('Showing the most recent 100 records.'));
  });
})();
