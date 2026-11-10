// Dream sounds. Sampled instruments (one recording every minor third, pitched to the nearest), loaded on first use:
// Grand Piano = Salamander Grand (Alexander Holm); the rest = MusyngKite General MIDI set (midi-js-soundfonts).
// "Dream Synth" is the plain WebAudio synth John liked. Levels are matched per instrument (measured RMS of the samples).
const DreamSynth=(()=>{
  let ctx,out;const live=new Set(),bufs={},loading={};
  const S={C:0,Ds:3,Fs:6,A:9},F={C:0,Eb:3,Gb:6,A:9};
  const INST={
    piano:{label:'Grand Piano',dir:'piano',names:S,gain:1.7,release:.25},
    epiano:{label:'Electric Piano',dir:'sounds/electric_piano_1',names:F,gain:2.76,release:.2},
    guitar:{label:'Acoustic Guitar',dir:'sounds/acoustic_guitar_steel',names:F,gain:5.06,release:.15},
    eguitar:{label:'Clean Electric Guitar',dir:'sounds/electric_guitar_clean',names:F,gain:7.9,release:.12},
    bass:{label:'Finger Bass',dir:'sounds/electric_bass_finger',names:F,gain:1.45,release:.08},
    synthbass:{label:'Synth Bass',dir:'sounds/synth_bass_1',names:F,gain:5.01,release:.06},
    strings:{label:'Strings',dir:'sounds/string_ensemble_1',names:F,gain:2.38,release:.35,loop:true},
    pad:{label:'Warm Pad',dir:'sounds/pad_2_warm',names:F,gain:1.98,release:.6,loop:true},
    lead:{label:'Saw Lead',dir:'sounds/lead_2_sawtooth',names:F,gain:3.83,release:.1,loop:true},
    synth:{label:'Dream Synth'}};
  function audio(){if(!ctx){ctx=new (window.AudioContext||window.webkitAudioContext)();out=ctx.createGain();out.gain.value=.5;
    const lim=ctx.createDynamicsCompressor();lim.threshold.value=-10;lim.ratio.value=12;out.connect(lim).connect(ctx.destination)}
    if(ctx.state!=='running')ctx.resume();return ctx}
  const hz=p=>440*Math.pow(2,(p-69)/12);
  const notesOf=k=>{const o=[];for(let m=21;m<=108;m++){const nm=Object.keys(INST[k].names).find(n=>INST[k].names[n]===m%12);if(nm)o.push([m,nm+(Math.floor(m/12)-1)])}return o};
  function load(k){if(!INST[k]||!INST[k].dir)return Promise.resolve();if(loading[k])return loading[k];const c=audio();bufs[k]={};
    loading[k]=Promise.all(notesOf(k).map(([m,file])=>fetch(`/${INST[k].dir}/${file}.mp3`).then(r=>r.ok?r.arrayBuffer():Promise.reject())
      .then(b=>new Promise((ok,no)=>c.decodeAudioData(b,ok,no))).then(b=>{bufs[k][m]=b}).catch(()=>{})));return loading[k]}
  function nearest(k,p){let best=null;for(const m in bufs[k]){if(best===null||Math.abs(m-p)<Math.abs(best-p))best=+m}return best}
  function track(n,stop){live.add(n);n.onended=()=>live.delete(n);n.stop(stop)}
  function sampled(k,p,t,d,vel){const c=audio(),I=INST[k],m=nearest(k,p);if(m===null)return false;const s=c.createBufferSource(),g=c.createGain(),end=t+d;
    s.buffer=bufs[k][m];s.playbackRate.value=Math.pow(2,(p-m)/12);if(I.loop&&d>2.2){s.loop=true;s.loopStart=1.2;s.loopEnd=2.6}
    s.connect(g).connect(out);g.gain.setValueAtTime(0,t);g.gain.linearRampToValueAtTime(vel*I.gain,t+.004);g.gain.setTargetAtTime(0,end,I.release/3);s.start(t);track(s,end+I.release*3+.05);return true}
  function synth(p,t,d,vel){const c=audio(),g=c.createGain(),end=t+d,f=c.createBiquadFilter();g.connect(out);f.type='lowpass';f.Q.value=6;
    f.frequency.setValueAtTime(400,t);f.frequency.linearRampToValueAtTime(3200,t+.08);f.frequency.setTargetAtTime(1400,t+.08,.3);f.connect(g);
    [-7,7].forEach(dt=>{const o=c.createOscillator();o.type='sawtooth';o.frequency.value=hz(p);o.detune.value=dt;o.connect(f);o.start(t);track(o,end+.2)});
    g.gain.setValueAtTime(0,t);g.gain.linearRampToValueAtTime(vel*.55,t+.01);g.gain.setTargetAtTime(vel*.4,t+.01,.4);g.gain.setTargetAtTime(0,end,.04)}
  function note(k,p,t,d,v){const vel=.35+.55*(v/127);if(k==='synth'||!sampled(k,p,t,d,vel))synth(p,t,d,vel)}
  function play(k,notes,offset=.08,rate=1){const c=audio(),t0=c.currentTime+offset;notes.forEach(n=>note(k,n.p,t0+n.s/rate,Math.max(.06,(n.e-n.s)/rate),n.v||90));return t0}
  function buffer(b,offset=.08,gain=1){const c=audio(),s=c.createBufferSource(),g=c.createGain();g.gain.value=gain;s.buffer=b;s.connect(g).connect(out);const t0=c.currentTime+offset;s.start(t0);live.add(s);s.onended=()=>live.delete(s);return t0}
  function tone(p,t,d){const c=audio(),o=c.createOscillator(),g=c.createGain();o.type='sine';o.frequency.value=hz(p);o.connect(g).connect(out);g.gain.setValueAtTime(0,t);g.gain.linearRampToValueAtTime(.25,t+.02);g.gain.setTargetAtTime(0,t+d,.05);o.start(t);track(o,t+d+.3)}
  function click(t,accent){const c=audio(),len=Math.round(c.sampleRate*.03),b=c.createBuffer(1,len,c.sampleRate),d=b.getChannelData(0);
    for(let i=0;i<len;i++)d[i]=(Math.random()*2-1)*Math.exp(-i/(len/6)); // noise tick: not pitched, so the tracker ignores bleed
    const s=c.createBufferSource(),f=c.createBiquadFilter(),g=c.createGain();f.type='highpass';f.frequency.value=accent?2500:1500;g.gain.value=accent?.9:.5;s.buffer=b;s.connect(f).connect(g).connect(out);s.start(t);live.add(s);s.onended=()=>live.delete(s)}
  function stop(){live.forEach(n=>{try{n.stop()}catch(e){}});live.clear()}
  return {audio,play,buffer,click,stop,load,tone,INST,get out(){audio();return out}}})();
