// Exact commands only. Other speech is treated as prompt text, never executable code.
(function(root){
function parseVoice(text){
 const clean=text.trim().replace(/[.!?]+$/,'').trim();
 const words={one:1,two:2,three:3,four:4,five:5,six:6,seven:7,eight:8,nine:9,ten:10,eleven:11,twelve:12};
 let m;
 if(/^(stop|stop playback)$/i.test(clean))return {action:'stop'};
 if(/^(play|audition) (the )?original$/i.test(clean))return {action:'original'};
 if(/^(play|audition) (the )?(variation|latest take)$/i.test(clean))return {action:'variation'};
 if((m=clean.match(/^(accept|reject|play|show)(?: (?:take )?(\d+|one|two|three|four|five|six|seven|eight|nine|ten|eleven|twelve))?$/i))){
  const take=m[2]?(words[m[2].toLowerCase()]||Number(m[2])):null;
  return {action:m[1].toLowerCase(),take};
 }
 if((m=clean.match(/^(?:generate|regenerate)(?:[,:]?\s+(.*))?$/i)))return {action:'generate',prompt:m[1]||''};
 return {action:'dictate',text};
}
root.parseScrunchVoice=parseVoice;
if(typeof module!=='undefined')module.exports=parseVoice;
})(typeof window==='undefined'?globalThis:window);
