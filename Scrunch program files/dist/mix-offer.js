// Mix engineer candidate in the main chat: Original / Candidate / Keep / Revert / Neither on the waiting offer.
// The offer comes from GET /api/mix-engineer/offer; a choice goes to POST /api/mix-engineer/choose, which reads back
// and refuses to write over anything John changed. Failures are shown as the server words them.
(function(){
  const BUTTONS=[['original','Original'],['candidate','Candidate'],['keep','Keep'],['revert','Revert'],['neither','Neither']];
  let token=null,shown=null,busy=false;
  function panel(){
    let el=document.getElementById('mixOffer');
    if(el)return el;
    const chat=document.getElementById('engineerChat');if(!chat)return null;
    el=document.createElement('section');el.id='mixOffer';el.setAttribute('aria-label','Mix candidate');el.hidden=true;
    el.innerHTML='<h3>Mix candidate</h3><p class="mix-playing" role="status"></p><div class="mix-buttons"></div><p class="mix-result" role="status"></p>';
    chat.insertAdjacentElement('afterend',el);return el;
  }
  async function ensureToken(){
    if(token)return token;
    const s=await fetch('/api/state').then(r=>r.json());token=s.token;return token;
  }
  function render(offer,message){
    const el=panel();if(!el)return;
    if(message!==undefined)el.querySelector('.mix-result').textContent=message;
    if(!offer){el.hidden=!el.querySelector('.mix-result').textContent;el.querySelector('.mix-buttons').replaceChildren();el.querySelector('.mix-playing').textContent='No mix candidate waiting.';shown=null;return;}
    el.hidden=false;
    el.querySelector('.mix-playing').textContent='In the Set now: '+(offer.playing==='candidate'?'Candidate':'Original')+'. Nothing is kept until you choose Keep.';
    const box=el.querySelector('.mix-buttons');box.replaceChildren();
    for(const [choice,label] of BUTTONS){
      const b=document.createElement('button');b.type='button';b.textContent=label;b.dataset.choice=choice;
      b.disabled=busy||choice===offer.playing||(choice==='keep'&&offer.playing==='original');
      b.setAttribute('aria-pressed',String(choice===offer.playing));
      b.onclick=()=>choose(offer,choice);box.append(b);
    }
    shown=offer.offerId+':'+offer.playing;
  }
  async function choose(offer,choice){
    busy=true;render(offer,'Working…');
    try{
      await ensureToken();
      const r=await fetch('/api/mix-engineer/choose',{method:'POST',headers:{'Content-Type':'application/json','X-Scrunch-Token':token},
        body:JSON.stringify({choice,offerId:offer.offerId})});
      const data=await r.json();
      busy=false;render(data.offer||null,r.ok?(data.text||'Done.'):('Not done: '+(data.error||r.status)));
    }catch(e){busy=false;render(offer,'Not done: '+e.message);}
  }
  async function poll(){
    if(busy)return;
    try{
      const r=await fetch('/api/mix-engineer/offer');if(!r.ok)return;
      const {offer}=await r.json();
      const key=offer?offer.offerId+':'+offer.playing:null;
      if(key!==shown)render(offer);
    }catch(e){}
  }
  window.addEventListener('load',()=>{poll();setInterval(poll,2000);});
})();
