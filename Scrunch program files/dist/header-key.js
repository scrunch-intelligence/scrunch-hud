// Presentation contract only. No key detection or changes to DSP confidence.
(function(root){
 function formatKey(result){
  const missing={label:'Key —',reason:'No complete whole-track key result is available. Section candidates are not a whole-track estimate.'};
  if(!result||result.scope!=='whole-track'||result.coverageComplete!==true||result.stale===true)return missing;
  const candidates=result.candidates;
  if(!Array.isArray(candidates)||!candidates.length)return missing;
  const c=candidates[0];if(!/^[A-G](#|b)?$/.test(c.tonic)||!['minor','major'].includes(c.mode))return missing;
  const confident=result.confidenceCalibrated===true&&Number.isFinite(result.confidence)&&result.confidence>=.8&&result.confidence<=1&&result.dominant===true&&result.modulating!==true;
  return {label:c.tonic+' '+c.mode+(confident?'':'?'),reason:confident?'Whole-track detected key; calibrated confidence '+Math.round(result.confidence*100)+'%.':'Whole-track candidate; uncertain confidence or no single dominant key.'};
 }
 if(typeof module!=='undefined')module.exports=formatKey;else root.ScrunchHeaderKey=formatKey;
})(typeof window!=='undefined'?window:globalThis);
