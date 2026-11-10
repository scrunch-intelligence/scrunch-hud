// Functional desktop composition. No DAW mutation or model call on layout/navigation.
(()=>{
 const el=id=>document.getElementById(id),make=(tag,id,text)=>{const n=document.createElement(tag);if(id)n.id=id;if(text)n.textContent=text;return n;};
 const nav=document.querySelector('.workspace-tabs');
 const session=make('main','sessionWorkspace');session.hidden=true;session.setAttribute('role','tabpanel');session.setAttribute('aria-labelledby','tabSession');
 const reference=make('main','referenceWorkspace');reference.hidden=true;reference.setAttribute('role','tabpanel');reference.setAttribute('aria-labelledby','tabReference');reference.append(make('h2',null,'Reference listening'),make('p',null,'Choose references and compare manually aligned sections in A/B Listening.'));
 const jump=make('button',null,'Open A/B Listening');jump.onclick=()=>{el('tabAnalytics').click();el('analyticsTab-setup').click();};reference.append(jump);
 for(const [id,label,panel] of [['tabReference','Reference',reference],['tabSession','Session',session]]){const b=make('button',id,label);b.setAttribute('role','tab');b.setAttribute('aria-controls',panel.id);nav.append(b);document.body.insertBefore(panel,el('settings'));}
 const connection=el('connection');connection.setAttribute('role','status');connection.dataset.health='red';connection.textContent='Checking connections…';
 nav.append(connection);const transport=make('span','hostTempo','— BPM · —');nav.append(transport);
 const connectButton=el('connect');connectButton.textContent='Connect Ableton';nav.insertBefore(connectButton,connection);
 const earsBadge=make('span','earsStatusBadge','Ears off');earsBadge.title='Audio analysis of Ableton playback';nav.insertBefore(earsBadge,transport);
 const workspace=document.querySelector('#engineerWorkspace .workspace');const panel=document.querySelector('.engineer-panel');
 const legacy=make('span','sessionConnection','Read the current Live Set');session.append(make('h2',null,'Session'),legacy);
 for(const n of [...workspace.children])if(n!==panel)session.append(n);
 session.append(document.querySelector('#engineerWorkspace aside'));session.append(el('sessionPlanning'));
 const healthDetails=make('details');healthDetails.append(make('summary',null,'System resources and connection checks'));const raw=make('pre','systemResources','Waiting for heartbeat');healthDetails.append(raw);session.append(healthDetails);
 new MutationObserver(()=>{try{const h=JSON.parse(raw.textContent),live=h.checks.ableton;const connected=live?.ok&&live?.fresh;connection.dataset.health=connected?'green':'red';connection.textContent=connected?'Ableton connected':'Ableton disconnected';connectButton.textContent=connected?'Reconnect Ableton':'Connect Ableton';earsBadge.textContent=h.checks.tap?.ok?'Ears on':'Ears off';}catch{}}).observe(raw,{childList:true});
 const bar=make('div','commandBar');bar.append(el('dictate'),el('engineerInput'),el('sendEngineer'),el('settingsButton'));el('stop').hidden=true;panel.append(bar);
 const indicator=el('engineerOrb');indicator.replaceChildren();indicator.innerHTML='<div class="tapDiscPlane"><div class="tapDisc"></div></div><svg class="tapHead" viewBox="0 0 1289 1221"><image href="/thinking-head.png" width="1289" height="1221"/><g transform="matrix(1 .16 0 1 380 342)"><rect width="84" height="204" rx="4" fill="#b64525"/><rect x="95" width="84" height="204" rx="4" fill="#cb6928"/><rect x="190" width="84" height="204" rx="4" fill="#daa12f"/></g></svg>';
 const small=make('div','inputStatus');small.append(indicator,el('orbLabel'));panel.insertBefore(small,bar);
 const voice=el('voiceMonitor');bar.after(voice,el('cancelVoice'));const options=document.querySelector('.voice-options');el('settings').append(options,document.querySelector('.orb-motion'));
 // Mic ring reports only actual microphone recording state.
 new MutationObserver(()=>el('dictate').classList.toggle('micRecording',!voice.hidden)).observe(voice,{attributes:true,attributeFilter:['hidden']});
 bindTabs([['tabEngineer','engineerWorkspace'],['tabAnalytics','analyticsWorkspace'],['tabReference','referenceWorkspace'],['tabSession','sessionWorkspace']]);
 const alerts=make('div','resourceConversation');panel.insertBefore(alerts,el('engineerChat').nextSibling);let lastAlert=0,highLoadChecks=0;
 let lastSuccess=0,inFlight=false,lastServerAge=0;
 function fail(){connection.dataset.health='red';connection.textContent='Scrunch connection lost — check missed';transport.textContent='— BPM · —';}
 async function poll(){if(inFlight)return;inFlight=true;try{const r=await fetch('/api/health',{cache:'no-store',signal:AbortSignal.timeout(2500)});if(!r.ok)throw Error('Heartbeat failed');const h=await r.json();if(!['green','amber','red'].includes(h.status)||!h.checks)throw Error('Invalid heartbeat');lastSuccess=performance.now();lastServerAge=Math.max(...['ableton','internet','tap'].map(k=>h.checks[k]?.ageSeconds??999));connection.dataset.health=h.status;connection.textContent=h.label.replace(/Audio tap stopped/g,'Audio listening stopped');const live=h.checks.ableton?.data;transport.textContent=live?`${live.tempo??'—'} BPM · ${live.timeSignature??'—'}`:'— BPM · —';raw.textContent=JSON.stringify(h,null,2);const resource=h.checks.resources?.data;highLoadChecks=resource&&(resource.cpuPercent>85||resource.loadAverage1Minute>resource.logicalCpus*1.5)?highLoadChecks+1:0;if(highLoadChecks>=3&&Date.now()-lastAlert>300000){lastAlert=Date.now();alerts.replaceChildren(make('p',null,'System load is high. If playback suffers, inspect device CPU in Live and consider freezing tracks you have finished editing. Scrunch cannot yet identify which track is responsible.'));}}catch{fail()}finally{inFlight=false}}
 setInterval(poll,3000);setInterval(()=>{if(!lastSuccess||performance.now()-lastSuccess>4000||lastServerAge+(performance.now()-lastSuccess)/1000>7)fail()},250);document.addEventListener('visibilitychange',()=>{if(!document.hidden){fail();poll()}});poll();
})();
