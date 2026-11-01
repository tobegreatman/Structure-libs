(function(){
  var out=[];
  function log(s){ out.push(s); }
  try{
    var raw=document.getElementById('rep').textContent;
    var parsed=JSON.parse(raw);
    var n=normalize(parsed);
    var a=auditReport(n);
    var items=a.items||a;
    var err=0,warn=0,info=0;
    items.forEach(function(it){
      if(it.level==='err')err++; else if(it.level==='warn')warn++; else info++;
      log(it.level.toUpperCase()+' | '+(it.scope||'-')+' | '+it.text);
    });
    var ml=(n.mainlines||[]);
    log('SUMMARY err='+err+' warn='+warn+' info='+info+' mainlines='+ml.length);
    ml.forEach(function(m){
      var p=calcPulse(m), pen=calcPenalty(m), st=calcStage(m);
      log('PULSE '+m.name+' -> P1='+p[0].ok+' P2='+p[1].ok+' P3='+p[2].ok+' passed='+(m.persistence&&m.persistence.pulse_check?m.persistence.pulse_check.passed:'?')+' rules='+(m.persistence&&m.persistence.pulse_check?m.persistence.pulse_check.rules_passed:'?'));
      log('PENALTY '+m.name+' -> calc='+pen.value+' claim='+m.penalty+' | '+JSON.stringify(pen.hits));
      log('STAGE '+m.name+' -> calc='+st.stage+' claim='+m.stage);
      Object.keys(st.hits).forEach(function(k){ log('   '+k+' '+st.hits[k].yes+'/'+st.hits[k].known); });
      if(m.__fixes) log('FIXES '+JSON.stringify(m.__fixes));
    });
    if(n.__fixes) log('FIXES-TOP '+JSON.stringify(n.__fixes));
  }catch(e){ log('EXCEPTION '+(e&&e.message)+' @ '+(e&&e.stack||'').split('\n')[1]); }
  var d=document.createElement('div'); d.id='probe'; d.textContent=out.join('\n');
  document.body.appendChild(d);
})();
