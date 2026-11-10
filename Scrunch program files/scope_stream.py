"""Many tracks at once, live: one lane per Live Ears tap, read continuously and kept time-aligned.

Every Live Ears plug-in publishes its own shared-memory stream, so putting one on the kick, the
bass and the master gives three streams. This reads them all in one background thread and keeps
the last few seconds of each in a ring buffer, so the page can ask for "the last 4 seconds" at
any moment without waiting for a capture.

Two things this is careful about, because a scope that lies is worse than no scope:

  * **Alignment.** Lanes are not drawn against each other by arrival time - a thread reads them
    one after another, and that alone would be milliseconds of false phase difference. Every
    block carries the host's own sample clock, so lanes are lined up on that, and a lane whose
    clock has not been seen is drawn but reported as unaligned rather than compared.
  * **Gaps.** The producer's audio thread never waits for this reader. If it falls behind, the
    ring drops blocks, and the drop count is reported with the frame. A gap is shown as a gap.

Nothing here writes to Live, and nothing here is a new plug-in: it reads taps that already exist.
"""
import threading,time
import numpy as np

SECONDS=12.0                # how much history each lane keeps
MAX_LANES=16
IDLE_SLEEP=0.002            # how long the reader waits when every stream is empty
STALE_AFTER=2.0             # a lane with no new audio for this long is reported as quiet, not live
SILENCE_AFTER=0.15          # and after this long, silence is drawn rather than its last seconds again

def upstream_switch(host,track_id,tap_device_id):
    """The on/off switch of the device feeding a tap, or None when the tap is first in the chain.

    A Live Ears tap sits in the device chain, and Live's track mute happens at the mixer - after
    the devices. Muting a track therefore does not quieten a tap at all, which is exactly the
    trap this avoids: to make a tapped lane go silent, the device in front of it is switched off.
    """
    devices=host.devices(track_id)
    ids=[d['id'] for d in devices]
    if tap_device_id not in ids:return None
    position=ids.index(tap_device_id)
    if position==0:return None
    device=host.device(ids[position-1])
    for parameter in device.get('parameters',[]):
        if parameter.get('name')=='Device On':
            return {'device':device['id'],'parameter':parameter['id'],'name':parameter['name'],
                    'deviceName':devices[position-1].get('name')}
    return None

def set_switch(host,switch,option):
    return host.set_device_parameter(switch['device'],switch['parameter'],switch['name'],option=option)

def live_colour(value):
    """Live stores a track colour as one integer; the page wants it as #rrggbb."""
    if not isinstance(value,int) or value<0:return None
    return '#%06x'%(value&0xFFFFFF)

class Lane:
    """One tap's recent audio, its clock, and what it is bound to in Live."""
    def __init__(self,stream,rate=48000.0,channels=2,seconds=SECONDS):
        self.stream=stream;self.rate=float(rate) or 48000.0;self.channels=int(channels) or 2
        self.size=int(self.rate*seconds)
        self.ring=np.zeros((self.size,2),dtype=np.float32)
        self.written=0                       # total frames ever written; the ring holds the last `size`
        self.sample_time=None                # host sample clock of the newest frame
        self.dropped=0;self.drop_base=None;self.last_block=None;self.error=None
        self.track=None;self.name=None;self.color=None;self.label=None
    def write(self,samples,channels,sample_time,rate=None,dropped=0):
        if rate:self.rate=float(rate)
        frames=len(samples)//max(1,channels)
        if frames<=0:return 0
        block=np.asarray(samples,dtype=np.float32)[:frames*channels].reshape(frames,channels)
        pair=np.repeat(block[:,:1],2,axis=1) if channels==1 else block[:,:2]
        start=self.written%self.size
        end=start+frames
        if end<=self.size:self.ring[start:end]=pair
        else:
            split=self.size-start
            self.ring[start:]=pair[:split];self.ring[:end-self.size]=pair[split:]
        self.written+=frames
        if sample_time is not None:self.sample_time=float(sample_time)+frames
        # The producer counts dropped frames from when IT started, and it has usually been
        # publishing to nobody for a while before the scope attaches. What matters is what has
        # been missed since we started watching, so the first reading is the baseline.
        count=int(dropped or 0)
        if self.drop_base is None:self.drop_base=count
        self.dropped=max(0,count-self.drop_base)
        self.last_block=time.time()
        return frames
    def recent(self,frames):
        """The last `frames` frames in order, oldest first."""
        frames=int(min(max(1,frames),self.size,self.written or 1))
        if not self.written:return np.zeros((frames,2),dtype=np.float32)
        end=self.written%self.size
        start=end-frames
        return self.ring[start:end].copy() if start>=0 else np.concatenate((self.ring[start:],self.ring[:end]))
    def live(self,now=None):
        return bool(self.last_block and (now or time.time())-self.last_block<STALE_AFTER)
    def title(self,index=0):
        """What to call this lane: Live's track name when that is known, else an honest placeholder."""
        return self.name or self.label or 'Tap %d'%(index+1)
    def view(self,now=None,index=0):
        return {'stream':self.stream,'name':self.title(index),'namedFromLive':bool(self.name),
                'color':self.color,'track':self.track,
                'sampleRate':self.rate,'channels':self.channels,'framesSeen':self.written,
                'dropped':self.dropped,'live':self.live(now),'aligned':self.sample_time is not None,
                'error':self.error}

class Scope:
    """All the taps in the session, read continuously."""
    def __init__(self,discover=None,open_source=None,seconds=SECONDS,clock=time.time):
        self.clock=clock;self.seconds=seconds
        self._discover=discover;self._open=open_source
        self.lanes={};self._sources={};self._thread=None;self._stop=threading.Event()
        self.lock=threading.RLock();self.started=None;self.reads=0

    # ---- the streams themselves ----
    def discover(self):
        if self._discover:return list(self._discover())
        from live_ears import LiveEarsSource
        return list(LiveEarsSource.discover())
    def _open_source(self,stream):
        if self._open:return self._open(stream)
        from live_ears import LiveEarsSource
        return LiveEarsSource(stream)

    def attach(self):
        """Open every tap that is publishing, and drop lanes whose tap has gone."""
        found=self.discover()[:MAX_LANES]
        with self.lock:
            for stream in found:
                if stream in self.lanes:continue
                lane=Lane(stream,seconds=self.seconds)
                try:self._sources[stream]=self._open_source(stream)
                except Exception as e:lane.error=str(e)
                self.lanes[stream]=lane
            for stream in [s for s in self.lanes if s not in found]:
                self.lanes.pop(stream,None)
                src=self._sources.pop(stream,None)
                if src is not None:
                    try:src.close()
                    except Exception:pass
        return list(self.lanes)

    def start(self):
        if self._thread and self._thread.is_alive():return {'running':True,'lanes':len(self.lanes)}
        self.attach()
        self._stop.clear()
        self._thread=threading.Thread(target=self._run,name='scrunch-scope',daemon=True)
        self.started=self.clock();self._thread.start()
        return {'running':True,'lanes':len(self.lanes),'startedAt':self.started}

    def stop(self):
        self._stop.set()
        thread=self._thread
        if thread:thread.join(timeout=2.0)
        self._thread=None
        with self.lock:
            for stream,src in list(self._sources.items()):
                try:src.close()
                except Exception:pass
                self._sources.pop(stream,None)
        return {'running':False}

    def running(self):
        return bool(self._thread and self._thread.is_alive())

    def _run(self):
        while not self._stop.is_set():
            moved=self.pump()
            if not moved:time.sleep(IDLE_SLEEP)

    def pump(self):
        """Read whatever every tap has ready. Returns how many frames arrived."""
        moved=0
        with self.lock:
            items=list(self._sources.items())
        for stream,src in items:
            lane=self.lanes.get(stream)
            if lane is None:continue
            for _ in range(64):                         # bounded, so one busy tap cannot starve the rest
                try:block=src.read()
                except Exception as e:
                    lane.error=str(e);block=None
                if block is None:break
                samples=np.frombuffer(block.pcm_float32_interleaved,dtype=np.float32)
                with self.lock:
                    moved+=lane.write(samples,block.channels or 2,getattr(block,'sample_time',None),
                                      block.sample_rate,getattr(block,'dropped',0))
                self.reads+=1
        return moved

    # ---- what the page asks for ----
    def bind(self,observe=None,host=None):
        """Give each lane the name and colour of the Live track its tap sits on - where that is known.

        The sure way is the tap's own identity: four numbers the plug-in publishes and also
        exposes as parameters, so a stream can be matched to a device with no guessing. Some
        builds of the plug-in do not expose those parameters, and then there is nothing to match
        on. In that case exactly one tap publishing and exactly one tap in the Set is still an
        answer - anything beyond that is a guess, so the lanes stay honestly unnamed and the
        producer says which is which.
        """
        import live_ears_binding
        done=[];unresolved=[]
        for stream,lane in list(self.lanes.items()):
            try:
                meta=live_ears_binding.metadata(stream)
                observation=(observe or live_ears_binding.observe_binding)(stream)
                found=live_ears_binding.bind(meta['identity'],observation)
                lane.track=found;lane.name=found.get('trackName')
                lane.color=live_colour(found.get('trackColor'))
                done.append({'stream':stream,'track':lane.name,'how':'identity'})
            except Exception as e:
                lane.track=None;lane.name=None;unresolved.append((stream,lane,str(e)))
        if len(unresolved)==1 and len(self.lanes)==1:
            taps=self.taps_in_set(host)
            if len(taps)==1:
                stream,lane,_=unresolved[0]
                lane.name=taps[0].get('trackName');lane.color=live_colour(taps[0].get('color'))
                lane.track={'trackId':taps[0].get('trackId'),'deviceId':taps[0].get('deviceId')}
                done.append({'stream':stream,'track':lane.name,'how':'only tap in the Set'})
                unresolved=[]
        for stream,lane,why in unresolved:lane.error=None if 'uniquely bound' in why else why
        return {'named':done,'unnamed':[s for s,_,_ in unresolved]}

    def taps_in_set(self,host=None):
        """Every Live Ears device Live can see, with the track it is on."""
        if host is None:
            import engine;host=engine.host()
        out=[]
        for track in host.session()['tracks']:
            for device in host.devices(track['id']):
                if 'Live Ears' in str(device.get('name','')):
                    out.append({'trackId':track['id'],'trackName':track.get('name'),
                                'color':track.get('color'),'deviceId':device['id'],
                                'deviceName':device.get('name')})
        return out

    def identify(self,host=None,settle=.35,quiet_db=6.0,sleep=time.sleep,accept_hung_note_risk=False):
        """Work out which lane is which track by muting each tapped track for a moment.

        The plug-in cannot always say where it is, but the audio can. With the music playing,
        the device feeding each tap is switched off in turn: the lane that goes quiet is that
        track. It is a deliberate, audible act - about a third of a second per track - and every
        switch is put back whether it works or not. (Live's track mute is no use here: it happens
        after the device chain, so a tap keeps hearing the instrument through it.)

        A track that is not making a sound cannot be identified this way, and is reported as
        such instead of being matched to whichever lane happened to dip.
        """
        import scope_dsp
        if not accept_hung_note_risk:
            # Learned the hard way on 20 Sep 2026: switching an instrument off mid-note means its
            # note-off arrives while it is bypassed, and the voice hangs inside the plug-in. On a
            # patch with amp sustain at 100% that is a drone that survives transport stops, track
            # mute and switching the device back on - only reloading the Set clears it.
            raise ValueError('Identifying lanes by ear switches each instrument off for a moment, and '
                             'an instrument switched off mid-note can be left with a note stuck on - '
                             'a drone that only a reload clears. Name the lanes yourself, or say to '
                             'accept that risk with the music stopped.')
        if host is None:
            import engine;host=engine.host()
        taps=self.taps_in_set(host)
        if not taps:raise ValueError('There are no Live Ears taps in this Set.')
        for tap in taps:
            tap['upstream']=upstream_switch(host,tap['trackId'],tap['deviceId'])
        without=[tap['trackName'] for tap in taps if not tap['upstream']]
        if without and len(without)==len(taps):
            raise ValueError('Nothing sits before the tap on %s, so there is no way to make it go quiet.'
                             %', '.join(without))
        def levels():
            with self.lock:
                return {s:scope_dsp.level(l.recent(int(l.rate*settle*.8)))['rmsDbFS'] for s,l in self.lanes.items()}
        sleep(settle)
        base=levels()
        playing=[s for s,v in base.items() if v>scope_dsp.FLOOR_DB]
        if not playing:raise ValueError('Nothing is playing, so there is nothing to listen for. Start the music and try again.')
        found=[];unclear=[]
        for tap in taps:
            switch=tap.get('upstream')
            if not switch:
                unclear.append({'track':tap['trackName'],'fellBy':None,
                                'why':'nothing sits before the tap on that track to switch off'})
                continue
            try:
                set_switch(host,switch,'Off')
                sleep(settle)
                during=levels()
            finally:
                set_switch(host,switch,'On')
            sleep(settle*.6)
            drops={s:base.get(s,scope_dsp.FLOOR_DB)-during.get(s,scope_dsp.FLOOR_DB) for s in self.lanes}
            ranked=sorted(drops.items(),key=lambda kv:kv[1],reverse=True)
            best,fell=ranked[0] if ranked else (None,0)
            runner=ranked[1][1] if len(ranked)>1 else -99
            if best is None or fell<quiet_db or (fell-runner)<quiet_db/2:
                unclear.append({'track':tap['trackName'],'fellBy':round(float(fell),1),
                                'why':'that track was not loud enough on its own to tell'})
                continue
            lane=self.lanes.get(best)
            if lane is not None:
                lane.name=tap['trackName'];lane.color=live_colour(tap.get('color'))
                lane.track={'trackId':tap['trackId'],'deviceId':tap['deviceId']}
            found.append({'stream':best,'track':tap['trackName'],'fellBy':round(float(fell),1),'how':'listened'})
        return {'named':found,'unclear':unclear,'lanes':self.lane_list()}

    def name_lane(self,stream,label,color=None):
        """The producer says which track a lane is. Kept until the tap goes away."""
        lane=self.lanes.get(stream)
        if lane is None:raise ValueError('That tap is not publishing.')
        lane.label=str(label)[:60] or None
        if color:lane.color=color if str(color).startswith('#') else live_colour(color)
        return lane.view()

    def lane_list(self):
        now=self.clock()
        with self.lock:return [lane.view(now,i) for i,lane in enumerate(self.lanes.values())]

    def quiet_tail(self,lane,now=None):
        """How many frames of silence belong on the right of a lane that has stopped arriving.

        A tap that stops publishing - the track switched off, the transport stopped - leaves its
        last seconds sitting in the ring. Redrawing those forever looks like audio stuck in a
        loop. What is actually happening is silence, so silence is what is drawn.
        """
        if not lane.last_block:return 0
        gap=(now if now is not None else time.time())-lane.last_block
        if gap<=SILENCE_AFTER:return 0
        return int(min(lane.size,(gap-SILENCE_AFTER)*lane.rate))

    def window(self,seconds=4.0,streams=None):
        """The same stretch of time from every lane, lined up on the host's sample clock.

        Lanes that carry a clock are trimmed so their newest sample is the oldest of those
        newest samples: what you see across lanes is one moment, not three arrival times.
        """
        with self.lock:
            lanes=[l for l in self.lanes.values() if streams is None or l.stream in streams]
            if not lanes:return {'lanes':[],'seconds':seconds,'aligned':False}
            clocked=[l for l in lanes if l.sample_time is not None and l.written]
            edge=min((l.sample_time for l in clocked),default=None)
            out=[]
            now=self.clock()
            for lane in lanes:
                want=int(lane.rate*seconds)
                behind=int(max(0,(lane.sample_time-edge))) if edge is not None and lane.sample_time is not None else 0
                have=lane.recent(min(lane.size,want+behind))
                audio=have[:len(have)-behind] if behind else have
                gap=self.quiet_tail(lane,now)
                if gap:
                    gap=min(gap,want)
                    audio=np.concatenate((audio[:max(0,len(audio)-gap)],
                                          np.zeros((gap,2),dtype=np.float32)))
                out.append((lane,audio[-want:] if want<len(audio) else audio))
            return {'lanes':out,'seconds':seconds,'aligned':edge is not None and len(clocked)==len(lanes),
                    'alignedOn':edge}
