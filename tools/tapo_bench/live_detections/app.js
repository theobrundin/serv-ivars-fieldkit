'use strict';
const $ = id => document.getElementById(id);
let lastSuccess = 0, lastData = null, lastSignature = '', paused = false;
const seen = new Set();
const localTime = iso => new Date(iso).toLocaleTimeString([], {hour:'numeric', minute:'2-digit', second:'2-digit'});
const percent = n => `${Math.round(100 * (Number(n) || 0))}%`;
function el(tag, cls, text) { const n=document.createElement(tag); if(cls)n.className=cls; if(text!==undefined)n.textContent=text; return n; }
function notice(text, kind='') { $('notice').textContent=text; $('notice').className=`notice ${kind}`; }
function connection(text, kind) { $('connection-text').textContent=text; $('connection').className=`connection ${kind}`; }
function card(row) {
  const root=el('article',`reading${row.confirmed?' confirmed':''}${seen.has(row.id)?'':' new'}`);
  root.dataset.id=row.id; root.dataset.receivedAt=row.receivedAt; seen.add(row.id);
  const first=row.candidates.find(c=>c.plate===row.confirmedPlate) || row.candidates[0], plate=row.confirmedPlate || (first && first.plate);
  const left=el('div'); left.append(el('div',`plate${plate?'':' unread'}`,plate || 'No plate read'));
  if(row.candidates.length>1)left.append(el('p','alternatives',`Other guesses: ${row.candidates.filter(c=>c.plate!==plate).map(c=>c.plate).join(' · ')}`));
  const right=el('div'), top=el('div','reading-top');
  top.append(el('span',`badge${row.confirmed?' confirmed':!plate?' unread':''}`,row.confirmed?'CONFIRMED':plate?'UNCONFIRMED':'VEHICLE DETECTED'));
  const time=el('time','',localTime(row.observedAt)); time.dateTime=row.observedAt; top.append(time); right.append(top);
  const description=[row.color,row.make,row.model].filter(Boolean).join(' ') || row.vehicleType || 'Vehicle';
  right.append(el('h3','vehicle',description),el('p','vehicle-note','Vehicle appearance is an estimate'));
  if(first){
    const metrics=el('div','scores');
    for(const [name,value] of [['OCR',first.ocr],['Plate detection',first.detection]]) { const s=el('span','',name);s.append(el('strong','',percent(value)));metrics.append(s); }
    right.append(metrics);
  }
  right.append(el('p',row.confirmed?'confirmed-note':'status-message',row.confirmed?'Matched to a confirmed Serv result':plate?'Candidate reading — not yet confirmed by Serv':'The recognizer saw a vehicle without a readable plate.'));
  if(!row.providerTimeValid)right.append(el('p','status-message','Time shown is arrival at the Pi.'));
  root.append(left,right); return root;
}
function render(data) {
  const camera=data.camera || {}, checked=Date.parse(camera.checkedAt || 0);
  const healthy=camera.online && camera.appRunning && Date.now()-checked<15000;
  $('camera-health').textContent=camera.online?'Connected':'Offline';
  $('camera-detail').textContent=camera.online?`${Number(camera.fps).toFixed(1)} fps · local Wi-Fi`:'Checking camera stream';
  $('recognizer-health').textContent=camera.online&&camera.appRunning?'Running':'Needs attention';
  $('recognizer-detail').textContent=camera.online?`${Math.round(camera.health)}% stream health`:'Waiting for video';
  $('transport').textContent=data.connection || 'Connected to the Pi';
  connection(paused?'Display paused':healthy?'Live · receiving updates':'Pipeline needs attention',paused?'':healthy?'live':'offline');
  if(!healthy)notice('The camera or recognizer needs attention. Previous readings below are not new detections.','error');
  else if(paused)notice('Display paused. Recognition continues on the Pi; unpause to catch up.','warn');
  else if(!data.confirmationAvailable)notice('Live plate guesses are available, but confirmation status is temporarily unavailable.','warn');
  else notice('Ready for your test. Drive slowly through view and allow about 12 seconds for a reading.');
  $('session-time').textContent=`Session started ${localTime(data.sessionStarted)} · last 30 minutes shown`;
  if(paused)return;
  $('reading-count').textContent=data.rows.length;
  $('empty').hidden=data.rows.length>0;
  const signature=JSON.stringify(data.rows);
  if(signature===lastSignature)return;
  lastSignature=signature;
  $('feed').replaceChildren(...data.rows.map(card));
  // Keep this client-side dedupe set bounded too.
  if(seen.size>400){seen.clear();data.rows.forEach(r=>seen.add(r.id));}
}
function lost(message) {
  connection('Connection lost','offline');
  notice(message || 'Cannot reach the Pi. This page will reconnect automatically. Previous readings remain visible.','error');
  $('camera-health').textContent='Unknown';$('recognizer-health').textContent='Unknown';
  $('camera-detail').textContent='Connection unavailable';$('recognizer-detail').textContent='Waiting for the Pi';
}
async function poll() {
  try {
    const controller=new AbortController();const timer=setTimeout(()=>controller.abort(),5000);
    let res;try{res=await fetch('/api/state',{cache:'no-store',signal:controller.signal});}finally{clearTimeout(timer);}
    if(res.status===401){lost('Open the private viewer link from Codex to connect this phone.');return;}
    if(!res.ok)throw new Error('offline');
    const data=await res.json();
    if(!data.serverTime || Math.abs(Date.now()-Date.parse(data.serverTime))>20000)throw new Error('stale');
    lastSuccess=Date.now(); lastData=data; render(data);
  } catch {lost();}
  finally {setTimeout(poll,1500);}
}
$('pause').addEventListener('change',()=>{paused=$('pause').checked;if(lastData)render(lastData);});
setInterval(()=>{
  const cutoff=Date.now()-1800000;
  if(lastData)lastData.rows=lastData.rows.filter(r=>Date.parse(r.receivedAt)>=cutoff);
  let expired=false;
  for(const node of document.querySelectorAll('.reading')) {
    if(Date.parse(node.dataset.receivedAt)<cutoff){node.remove();expired=true;}
  }
  if(expired){
    const count=$('feed').childElementCount;
    $('reading-count').textContent=count;$('empty').hidden=count>0;lastSignature='';
  }
  if(!lastSuccess)return;
  const age=Math.floor((Date.now()-lastSuccess)/1000);
  $('last-sync').textContent=age<2?'Just now':`${age}s ago`;
  if(age>8)lost();
},1000);
(async()=>{
  const token=location.hash.slice(1);
  if(token){
    history.replaceState(null,'',location.pathname);
    try{await fetch('/api/session',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({token})});}catch{}
  }
  poll();
})();
