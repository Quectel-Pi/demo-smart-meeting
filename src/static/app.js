'use strict';
const $ = id => document.getElementById(id);
let session = null, stream = null, polling = false, joining = false;
let screenStream = null, screenBusy = false, cameraBusy = false;
let alertContext = null, alertSoundEnabled = true, latestAsrState = '';
const raiseStates = new Map();
let language = localStorage.getItem('meeting-language') === 'en' ? 'en' : 'zh';
const peers = new Map();
const clock = () => $('clock').firstChild.textContent = new Date().toLocaleTimeString('zh-CN', {hour12:false});
clock(); setInterval(clock, 1000);
function status(text) { $('status').textContent = text; }
async function api(path, payload) {
  const response = await fetch('/api/' + path, {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify(payload)});
  const result = await response.json();
  if (!response.ok) throw new Error(result.error || '服务请求失败');
  return result;
}
function identity() { return {room:session.room, peer:session.peer, token:session.token}; }
function ui() {
  const active = !!session;
  $('roomNumber').textContent = active ? session.room.replace(/(\d{3})(\d{3})(\d{3})/, '$1 $2 $3') : '--- --- ---';
  $('connection').textContent = active ? 'MEETING ACTIVE' : 'STANDBY';
  $('live').textContent = active ? '● LIVE' : '● STANDBY';
  $('people').textContent = `在线设备 ${active ? peers.size + 1 : 0}`;
  for (const name of ['create','join','number']) $(name).disabled = active || joining;
  for (const name of ['copy','leave']) $(name).disabled = !active;
  $('mic').disabled = !stream?.getAudioTracks().length;
  $('camera').disabled = !stream?.getVideoTracks().length;
  $('mic').textContent='麦克风：'+(stream?.getAudioTracks()[0]?.enabled===false?'关':'开');
  $('camera').textContent='摄像头：'+(stream?.getVideoTracks()[0]?.enabled===false?'关':'开');
  $('cameraDevice').disabled = !active || !stream?.getVideoTracks().length || cameraBusy;
  $('retryCamera').disabled = !active || !stream?.getVideoTracks().length || cameraBusy;
  $('screenShare').disabled = !active || screenBusy;
  $('screenShare').textContent = screenStream ? '停止共享屏幕' : '共享屏幕';
  $('visionToggle').disabled = !active || !stream?.getVideoTracks().length;
  $('asrToggle').disabled = !active || !stream?.getAudioTracks().length;
}
async function acquireMedia() {
  if (!navigator.mediaDevices?.getUserMedia || !window.RTCPeerConnection) throw new Error('浏览器不支持 WebRTC 或当前页面不是安全连接');
  try { stream = await navigator.mediaDevices.getUserMedia({video:true, audio:true}); }
  catch (bothError) {
    try { stream = await navigator.mediaDevices.getUserMedia({audio:true,video:false}); status('摄像头不可用，已使用仅音频模式'); }
    catch (audioError) {
      try { stream = await navigator.mediaDevices.getUserMedia({video:true,audio:false}); status('麦克风不可用，已使用仅视频模式'); }
      catch (videoError) { throw new Error('无法打开摄像头或麦克风，请检查权限和设备占用'); }
    }
  }
  $('localVideo').srcObject = stream;
  $('localVideo').play().catch(()=>{});
  $('videoHint').hidden = stream.getVideoTracks().length > 0;
  await refreshCameras();
  if (stream.getVideoTracks().length) checkCameraFrames(stream);
}
async function refreshCameras() {
  const select=$('cameraDevice'), current=stream?.getVideoTracks()[0]?.getSettings().deviceId;
  try {
    const devices=(await navigator.mediaDevices.enumerateDevices()).filter(item=>item.kind==='videoinput');
    select.replaceChildren();
    for (const [index,device] of devices.entries()) {
      const option=document.createElement('option'); option.value=device.deviceId;
      option.textContent=device.label || `摄像头 ${index+1}`; select.append(option);
    }
    if (!devices.length) { const option=document.createElement('option'); option.textContent='未发现摄像头'; select.append(option); }
    if (current) select.value=current;
  } catch (_) { /* Device enumeration is optional. */ }
}
function checkCameraFrames(media) {
  setTimeout(()=>{
    if (stream===media && media.getVideoTracks()[0]?.enabled && !$('localVideo').videoWidth) {
      $('videoHint').hidden=false; $('videoHint').textContent='未收到摄像头画面，请选择摄像头或点击重试';
      status('摄像头未输出画面，请选择设备或重试');
    }
  },6000);
}
$('localVideo').addEventListener('playing',()=>{ if ($('localVideo').videoWidth) $('videoHint').hidden=true; });
async function switchCamera() {
  if (!session || cameraBusy || !stream?.getVideoTracks().length) return;
  const current=session, old=stream.getVideoTracks()[0], deviceId=$('cameraDevice').value;
  cameraBusy=true; ui(); let replacement;
  try {
    const media=await navigator.mediaDevices.getUserMedia({video:deviceId?{deviceId:{exact:deviceId}}:true,audio:false});
    replacement=media.getVideoTracks()[0];
    if (!replacement || session!==current) { media.getTracks().forEach(t=>t.stop()); return; }
    replacement.enabled=old.enabled;
    const senders=[...peers.values()].flatMap(entry=>entry.pc.getSenders().filter(sender=>sender.track===old));
    const results=await Promise.allSettled(senders.map(sender=>sender.replaceTrack(replacement)));
    if (results.some(result=>result.status==='rejected')) {
      await Promise.allSettled(senders.map(sender=>sender.replaceTrack(old)));
      throw new Error('无法更新远端画面');
    }
    if (session!==current) { replacement.stop(); return; }
    stream.removeTrack(old); stream.addTrack(replacement); old.stop();
    $('localVideo').srcObject=null; $('localVideo').srcObject=stream; $('localVideo').play().catch(()=>{});
    $('videoHint').textContent='正在等待摄像头画面…'; $('videoHint').hidden=false;
    clearBoxes(); await refreshCameras(); checkCameraFrames(stream); status('摄像头已重新连接');
  } catch(error) { if (replacement) replacement.stop(); if (session===current) status('摄像头重试失败：'+error.message); await refreshCameras(); }
  finally { cameraBusy=false; ui(); }
}
$('cameraDevice').onchange=switchCamera; $('retryCamera').onclick=switchCamera;
function focusTile(tile) {
  const wasFocused=tile.classList.contains('focused');
  for (const item of $('videos').querySelectorAll('.tile')) item.classList.remove('focused');
  $('videos').classList.toggle('has-focus', !wasFocused);
  if (!wasFocused) tile.classList.add('focused');
}
function resetFocus() {
  $('videos').classList.remove('has-focus');
  for (const item of $('videos').querySelectorAll('.tile')) item.classList.remove('focused');
}
function makeFocusable(tile, label) {
  tile.tabIndex=0; tile.setAttribute('role','button'); tile.setAttribute('aria-label',label+'，点击放大或恢复');
  tile.addEventListener('click',()=>focusTile(tile));
  tile.addEventListener('keydown',event=>{
    if (event.key==='Enter' || event.key===' ') { event.preventDefault(); focusTile(tile); }
  });
}
makeFocusable($('localTile'),'本机画面');
document.addEventListener('keydown',event=>{ if (event.key==='Escape') resetFocus(); });
function removeTile(tile) {
  if (tile?.classList.contains('focused')) resetFocus();
  tile?.remove();
}
function screenTile(label, media, muted=false) {
  const tile=document.createElement('div'); tile.className='tile screen';
  const video=document.createElement('video'); video.autoplay=true; video.playsInline=true; video.muted=muted;
  video.srcObject=media;
  const caption=document.createElement('span'); caption.textContent=label;
  tile.append(video,caption); makeFocusable(tile,label); $('videos').append(tile); return tile;
}
let localScreenTile=null;
function closePeer(id) {
  const entry = peers.get(id);
  if (!entry) return;
  raiseStates.delete(id); renderRaiseBanner(); entry.pc.close(); removeTile(entry.tile); removeTile(entry.screenTile); peers.delete(id); visionResults.delete(id); renderVision(); ui();
}
function peerFor(id, answering=false) {
  if (peers.has(id)) return peers.get(id).pc;
  const pc = new RTCPeerConnection({iceServers:[]});
  const tile = document.createElement('div'); tile.className = 'tile';
  const video = document.createElement('video'); video.autoplay = true; video.playsInline = true;
  const label = document.createElement('span'); label.textContent = '远端参会设备 // REMOTE';
  tile.append(video, label); makeFocusable(tile,'远端参会设备'); $('videos').append(tile);
  const entry={pc,tile,screenTile:null,screenMedia:null,screenActive:false,screenTransceiver:null,pendingTracks:[],signallingReady:false};
  peers.set(id,entry);
  entry.handleTrack = event => {
    if (event.transceiver === entry.screenTransceiver) {
      entry.screenMedia=new MediaStream([event.track]);
      updateRemoteScreen(entry);
    } else {
      if (event.streams[0]) video.srcObject = event.streams[0];
      else { if (!video.srcObject) video.srcObject=new MediaStream(); video.srcObject.addTrack(event.track); }
    }
  };
  pc.ontrack = event => {
    if (!entry.screenTransceiver) entry.pendingTracks.push(event);
    else entry.handleTrack(event);
  };
  pc.onconnectionstatechange = () => {
    if (pc.connectionState === 'failed') status('无法连接远端设备，请检查双方网络或重新加入');
  };
  for (const track of stream.getTracks()) pc.addTrack(track,stream);
  if (!answering) {
    if (!stream.getVideoTracks().length) pc.addTransceiver('video',{direction:'recvonly'});
    if (!stream.getAudioTracks().length) pc.addTransceiver('audio',{direction:'recvonly'});
    entry.screenTransceiver=pc.addTransceiver(screenStream?.getVideoTracks()[0] || 'video',{direction:'sendrecv'});
  }
  ui(); return pc;
}
function updateRemoteScreen(entry) {
  if (entry.screenActive && entry.screenMedia && !entry.screenTile) entry.screenTile=screenTile('远端屏幕共享 // SCREEN',entry.screenMedia,true);
  if (!entry.screenActive && entry.screenTile) { removeTile(entry.screenTile); entry.screenTile=null; }
}
async function announceScreen(id) {
  if (session && peers.get(id)?.signallingReady) await signal(id,{type:'screen',active:!!screenStream});
}
async function broadcastScreen() {
  const results=await Promise.allSettled([...peers.keys()].map(announceScreen));
  if (session && results.some(result=>result.status==='rejected')) status('屏幕共享状态通知失败，请重新加入会议');
}
async function stopScreen() {
  const old=screenStream;
  if (!old) return;
  screenStream=null; screenBusy=true; ui();
  old.getTracks().forEach(track=>{track.onended=null; track.stop();});
  removeTile(localScreenTile); localScreenTile=null;
  const results=await Promise.allSettled([...peers.values()].filter(entry=>entry.screenTransceiver).map(entry=>entry.screenTransceiver.sender.replaceTrack(null)));
  await broadcastScreen();
  screenBusy=false; ui();
  if (results.some(result=>result.status==='rejected')) status('屏幕共享停止失败，请重新加入会议');
  else if (session) status('已停止屏幕共享');
}
$('screenShare').onclick=async()=>{
  if (screenStream) return stopScreen();
  if (!session || screenBusy) return;
  if (!navigator.mediaDevices?.getDisplayMedia) return status('当前浏览器不支持屏幕共享');
  const current=session; screenBusy=true; ui();
  let selected;
  try {
    selected=await navigator.mediaDevices.getDisplayMedia({video:true,audio:false});
    if (session!==current) { selected.getTracks().forEach(track=>track.stop()); return; }
    const track=selected.getVideoTracks()[0];
    if (!track || track.readyState!=='live') throw new Error('未获取到屏幕画面');
    screenStream=selected; track.onended=()=>stopScreen();
    localScreenTile=screenTile('本机屏幕共享 // SCREEN',selected,true);
    const results=await Promise.allSettled([...peers.values()].filter(entry=>entry.screenTransceiver).map(entry=>entry.screenTransceiver.sender.replaceTrack(track)));
    if (results.some(result=>result.status==='rejected')) throw new Error('无法向所有参会设备发送屏幕画面');
    if (session!==current || screenStream!==selected) return;
    await broadcastScreen();
    if (session===current && screenStream===selected) status('正在共享屏幕');
  } catch(error) {
    if (screenStream===selected) await stopScreen();
    else if (selected) selected.getTracks().forEach(track=>track.stop());
    if (session===current && error.name!=='NotAllowedError') status('屏幕共享失败：'+error.message);
    else if (session===current) status('已取消屏幕共享');
  } finally { screenBusy=false; ui(); }
};
async function gather(pc) {
  if (pc.iceGatheringState === 'complete') return;
  await new Promise(resolve => {
    let timer;
    const done = () => { if (pc.iceGatheringState === 'complete') finish(); };
    const finish = () => { clearTimeout(timer); pc.removeEventListener('icegatheringstatechange',done); resolve(); };
    pc.addEventListener('icegatheringstatechange',done); timer = setTimeout(finish,5000);
    done();
  });
}
async function signal(id, message) { await api('signal', {...identity(),target:id,message}); }
async function makeOffer(id) {
  const pc = peerFor(id);
  await pc.setLocalDescription(await pc.createOffer()); await gather(pc);
  await signal(id,{type:'offer',sdp:pc.localDescription.sdp});
  if (peers.get(id)?.pc===pc) peers.get(id).signallingReady=true;
  await announceScreen(id);
}
async function onSignal(id, msg) {
  if (!session) return;
  const pc = peerFor(id, msg.type === 'offer');
  if (msg.type === 'offer') {
    await pc.setRemoteDescription({type:'offer',sdp:msg.sdp});
    const entry=peers.get(id);
    entry.screenTransceiver=pc.getTransceivers().filter(t=>t.receiver.track.kind==='video' && t.mid!==null)[1];
    if (!entry.screenTransceiver) throw new Error('对方不支持屏幕共享，请刷新页面');
    entry.screenTransceiver.direction='sendrecv';
    await entry.screenTransceiver.sender.replaceTrack(screenStream?.getVideoTracks()[0] || null);
    for (const event of entry.pendingTracks.splice(0)) entry.handleTrack(event);
    await pc.setLocalDescription(await pc.createAnswer()); await gather(pc);
    await signal(id,{type:'answer',sdp:pc.localDescription.sdp});
    if (peers.get(id)?.pc===pc) peers.get(id).signallingReady=true;
    await announceScreen(id);
  } else if (msg.type === 'answer') await pc.setRemoteDescription({type:'answer',sdp:msg.sdp});
  else if (msg.type === 'screen') { const entry=peers.get(id); entry.screenActive=msg.active===true; updateRemoteScreen(entry); }
}
async function poll() {
  if (!session || polling) return;
  polling = true;
  try {
    const {messages} = await api('poll',identity());
    for (const msg of messages) {
      if (msg.type === 'left') closePeer(msg.peer);
      else if (msg.type === 'signal') await onSignal(msg.peer,msg.message);
      else if (msg.type === 'vision') { visionResults.set(msg.peer,{...msg,at:Date.now()}); updateRaiseAlert(msg.peer,msg.raised===true); renderVision(); }
      else if (msg.type === 'transcript') showTranscript(msg);
      else if (msg.type === 'asr_status' && msg.peer === session.peer) { latestAsrState = msg.state; $('asrStatus').textContent = msg.state; if (audioState) audioState.ready = msg.state === 'ASR_STREAMING'; }
    }
  } catch (err) { status('信令异常：' + err.message); }
  finally { polling = false; if(session) setTimeout(poll,1000); }
}
async function start(create) {
  if (joining || session) return;
  const number = $('number').value.replace(/\s/g,'');
  if (!create && !/^\d{9}$/.test(number)) return status('请输入 9 位会议号');
  prepareAlertAudio(); joining = true; ui();
  try {
    status('正在开启设备…'); await acquireMedia();
    session = await api(create?'create':'join',create?{}:{room:number});
    status(create?'会议已创建，可将会议号分享给同一局域网的参会者':'已加入会议，正在连接其他设备');
    ui(); poll();
    for (const peer of session.existing) await makeOffer(peer);
  } catch (err) {
    status(err.message);
    if (!session && stream) { stream.getTracks().forEach(t=>t.stop()); stream=null; $('localVideo').srcObject=null; $('videoHint').hidden=false; }
  } finally { joining=false; ui(); }
}
function leave(notify=true) {
  const old = session;
  if (!old) return;
  stopVision(); stopAudio(); stopScreen(); resetFocus();
  const credentials = identity(); session=null;
  visionResults.clear(); raiseStates.clear(); renderRaiseBanner(); renderVision();
  if (notify) fetch('/api/leave',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(credentials),keepalive:true}).catch(()=>{});
  for (const id of [...peers.keys()]) closePeer(id);
  if (stream) stream.getTracks().forEach(t=>t.stop());
  stream=null; $('localVideo').srcObject=null; $('videoHint').textContent='创建或加入会议后开启音视频'; $('videoHint').hidden=false;
  status('已离开会议'); ui();
}
function fullscreenLabel() {
  const active=!!document.fullscreenElement;
  $('fullscreen').textContent=active?'退出全屏':'全屏';
  $('fullscreen').setAttribute('aria-label',active?'退出全屏':'全屏');
}
$('fullscreen').onclick=async()=>{
  try {
    if (document.fullscreenElement) await document.exitFullscreen();
    else await document.documentElement.requestFullscreen();
  } catch (error) { status('无法切换全屏：'+error.message); }
};
document.addEventListener('fullscreenchange',fullscreenLabel);
$('create').onclick=()=>start(true); $('join').onclick=()=>start(false); $('leave').onclick=()=>leave();
$('copy').onclick=()=>navigator.clipboard.writeText(session.room).then(()=>status('会议号已复制')).catch(()=>status('复制失败，请手动记录会议号'));
$('mic').onclick=()=>{const t=stream.getAudioTracks()[0];t.enabled=!t.enabled;$('mic').textContent='麦克风：'+(t.enabled?'开':'关');};
$('camera').onclick=()=>{const t=stream.getVideoTracks()[0];t.enabled=!t.enabled;$('camera').textContent='摄像头：'+(t.enabled?'开':'关'); if (!t.enabled) clearBoxes();};
window.addEventListener('pagehide',()=>leave()); ui();

function prepareAlertAudio() {
  try {
    if (!alertContext) alertContext=new (window.AudioContext || window.webkitAudioContext)();
    if (alertContext.state==='suspended') alertContext.resume().catch(()=>{});
  } catch (_) { /* Some browsers do not support audio synthesis. */ }
}
function playRaiseTone() {
  if (!alertSoundEnabled || !alertContext || alertContext.state!=='running') return;
  const now=alertContext.currentTime;
  for (const [offset,frequency] of [[0,740],[0.16,980]]) {
    const oscillator=alertContext.createOscillator(), gain=alertContext.createGain();
    oscillator.frequency.value=frequency; oscillator.type='sine';
    gain.gain.setValueAtTime(0.0001,now+offset); gain.gain.exponentialRampToValueAtTime(0.12,now+offset+0.02);
    gain.gain.exponentialRampToValueAtTime(0.0001,now+offset+0.14);
    oscillator.connect(gain); gain.connect(alertContext.destination); oscillator.start(now+offset); oscillator.stop(now+offset+0.15);
  }
}
function renderRaiseBanner() {
  const active=[...raiseStates.entries()].filter(([,item])=>item.active).map(([id])=>sourceLabel(id));
  $('raiseBanner').hidden=active.length===0;
  $('raiseDetail').textContent=active.length ? '参会设备：'+active.join('、') : '';
}
function updateRaiseAlert(id,raised) {
  let item=raiseStates.get(id);
  if (!item) { item={yes:0,no:0,active:false,lastTone:0}; raiseStates.set(id,item); }
  if (raised) {
    item.yes++; item.no=0;
    if (!item.active && item.yes>=2) {
      item.active=true;
      if (Date.now()-item.lastTone>8000) { playRaiseTone(); item.lastTone=Date.now(); }
    }
  } else { item.yes=0; if (++item.no>=2) item.active=false; }
  renderRaiseBanner();
}
$('alertSound').onclick=()=>{ alertSoundEnabled=!alertSoundEnabled; $('alertSound').textContent='提示音：'+(alertSoundEnabled?'开':'关'); $('alertSound').setAttribute('aria-pressed',String(alertSoundEnabled)); if(alertSoundEnabled) prepareAlertAudio(); };
let boxTimestamp=0;
function clearBoxes() { boxTimestamp=0; $('personBoxes').replaceChildren(); }
function drawBoxes(boxes) {
  const video=$('localVideo'), svg=$('personBoxes');
  if (!video.videoWidth || !video.videoHeight) return clearBoxes();
  const width=video.clientWidth, height=video.clientHeight, scale=Math.min(width/video.videoWidth,height/video.videoHeight);
  const actualWidth=video.videoWidth*scale, actualHeight=video.videoHeight*scale;
  svg.style.left=(video.offsetLeft+(width-actualWidth)/2)+'px'; svg.style.top=(video.offsetTop+(height-actualHeight)/2)+'px';
  svg.style.width=actualWidth+'px'; svg.style.height=actualHeight+'px'; svg.replaceChildren();
  for (const box of boxes || []) {
    if (![box.x,box.y,box.width,box.height].every(Number.isFinite)) continue;
    const rect=document.createElementNS('http://www.w3.org/2000/svg','rect');
    for (const name of ['x','y','width','height']) rect.setAttribute(name, Math.max(0,Math.min(1,box[name])));
    svg.append(rect);
  }
  boxTimestamp=Date.now();
}
let lastBoxes=[];
window.addEventListener('resize',()=>{ if(boxTimestamp) drawBoxes(lastBoxes); });
if (window.ResizeObserver) new ResizeObserver(()=>{ if(boxTimestamp) drawBoxes(lastBoxes); }).observe($('localVideo'));
setInterval(()=>{ if(boxTimestamp && Date.now()-boxTimestamp>7000) { lastBoxes=[]; clearBoxes(); } },1000);
const visionResults = new Map();
let visionOn = false, visionGeneration = 0, audioState = null;
const sourceLabel = id => session && id === session.peer ? '本机' : '远端 ' + id.slice(0, 5);
function renderVision() {
  const box = $('visionResults'); box.replaceChildren();
  for (const [id, item] of visionResults) {
    if (Date.now() - item.at > 10000) { visionResults.delete(id); raiseStates.delete(id); renderRaiseBanner(); continue; }
    const row = document.createElement('div');
    row.textContent = `${sourceLabel(id)}：画面人数 ${item.count}　${item.raised ? '✋ 检测到举手' : '未检测到举手'}　${item.milliseconds}ms`;
    if (item.raised) row.className = 'raised';
    box.append(row);
  }
  if (!box.childNodes.length) box.textContent = '暂无有效画面分析';
}
setInterval(renderVision, 3000);
function stopVision() { visionOn=false; visionGeneration++; lastBoxes=[]; clearBoxes(); $('visionToggle').textContent='启动本机画面分析'; }
$('visionToggle').onclick = () => {
  if (visionOn) return stopVision();
  visionOn=true; const generation=++visionGeneration; $('visionToggle').textContent='停止本机画面分析';
  const canvas=document.createElement('canvas'); canvas.width=480; canvas.height=270;
  const tick=async()=>{
    if (!visionOn || !session || generation!==visionGeneration) return;
    const video=$('localVideo');
    if (video.readyState >= 2 && stream?.getVideoTracks()[0]?.enabled) {
      const analysedTrack=stream.getVideoTracks()[0];
      const ratio=Math.min(480/video.videoWidth,270/video.videoHeight);
      canvas.width=Math.max(1,Math.round(video.videoWidth*ratio)); canvas.height=Math.max(1,Math.round(video.videoHeight*ratio));
      canvas.getContext('2d').drawImage(video,0,0,canvas.width,canvas.height);
      try { const result=await api('ai/vision',{...identity(),image:canvas.toDataURL('image/jpeg',0.65).split(',')[1]}); if (visionOn && session && generation===visionGeneration && stream?.getVideoTracks()[0]===analysedTrack && analysedTrack.enabled) { lastBoxes=result.boxes || []; drawBoxes(lastBoxes); } }
      catch(e) { if (!e.message.includes('繁忙')) status('画面分析：'+e.message); }
    }
    if (visionOn && generation===visionGeneration) setTimeout(tick,2000);
  }; tick();
};
function stopAudio() {
  const a=audioState; audioState=null;
  if (a) { a.node.disconnect(); a.source.disconnect(); a.context.close(); if(session) api('ai/audio-stop',identity()).catch(()=>{}); }
  latestAsrState=''; $('inputLevel').hidden=true; $('micLevel').value=0;
  $('asrToggle').textContent='启动本机语音转写'; $('asrStatus').textContent='已停止';
}
$('asrToggle').onclick=async()=>{
  if (audioState) return stopAudio();
  if (!session) return;
  $('asrToggle').disabled=true;
  const credentials=identity(); let context; latestAsrState='';
  try {
    context=new AudioContext(); await context.resume();
    if (context.state!=='running') throw new Error('音频处理未启动，请检查浏览器权限');
    if (!session || session.peer !== credentials.peer) { await context.close(); return; }
    await api('ai/audio-start',credentials);
    if (!session || session.peer !== credentials.peer) { await context.close(); await api('ai/audio-stop',credentials).catch(()=>{}); return; }
    const source=context.createMediaStreamSource(stream), node=context.createScriptProcessor(4096,1,1);
    const state={context,source,node,busy:false,ready:latestAsrState==='ASR_STREAMING',samples:[],phase:0,lastMeter:0}; audioState=state; $('inputLevel').hidden=false;
    node.onaudioprocess=e=>{
      if(audioState!==state) return;
      const input=e.inputBuffer.getChannelData(0), step=context.sampleRate/16000;
      if (Date.now()-state.lastMeter>250) { let sum=0; for (const sample of input) sum+=sample*sample; $('micLevel').value=Math.min(1,Math.sqrt(sum/input.length)*5); state.lastMeter=Date.now(); }
      if (!state.ready) return;
      while(state.phase<input.length) { state.samples.push(Math.max(-1,Math.min(1,input[Math.floor(state.phase)]))); state.phase+=step; }
      state.phase-=input.length;
      if(state.samples.length>=8000) {
        const samples=state.samples.splice(0,8000);
        if(state.busy || !session) return;
        const pcm=new Int16Array(samples.length); for(let i=0;i<samples.length;i++) pcm[i]=samples[i]*32767;
        const bytes=new Uint8Array(pcm.buffer); let binary=''; for(const byte of bytes) binary+=String.fromCharCode(byte);
        state.busy=true; api('ai/audio',{...identity(),pcm:btoa(binary)}).catch(e=>{$('asrStatus').textContent='音频发送：'+e.message;}).finally(()=>state.busy=false);
      }
    };
    source.connect(node); node.connect(context.destination);
    $('asrToggle').textContent='停止本机语音转写'; $('asrStatus').textContent=state.ready?'ASR_STREAMING':'正在加载识别模型…';
  } catch(e) { if(context && context.state!=='closed') context.close(); if(session && session.peer===credentials.peer) api('ai/audio-stop',credentials).catch(()=>{}); $('asrStatus').textContent='语音转写：'+e.message; }
  finally { ui(); }
};
const transcriptRows=new Map();
function showTranscript(msg) {
  const key=msg.peer+':'+msg.utterance;
  let row=transcriptRows.get(key);
  if(!row) { row=document.createElement('p'); transcriptRows.set(key,row); $('transcripts').append(row); }
  row.dataset.peer=msg.peer;
  if (!row.firstChild) { row.append(document.createElement('span'),document.createElement('span')); }
  row.firstChild.textContent=(language==='en' ? (msg.peer===session?.peer ? 'Local' : 'Remote '+msg.peer.slice(0,5)) : sourceLabel(msg.peer))+'：';
  row.lastChild.textContent=msg.text+(msg.final ? '' : ' …');
  while($('transcripts').childNodes.length>200) { const first=$('transcripts').firstChild; for(const [k,v] of transcriptRows) if(v===first) transcriptRows.delete(k); first.remove(); }
  $('transcripts').scrollTop=$('transcripts').scrollHeight;
}
$('clearTranscript').onclick=()=>{ $('transcripts').replaceChildren(); transcriptRows.clear(); };
$('downloadTranscript').onclick=()=>{ const a=document.createElement('a'); a.href=URL.createObjectURL(new Blob([$('transcripts').innerText],{type:'text/plain;charset=utf-8'})); a.download='会议转写.txt'; a.click(); setTimeout(()=>URL.revokeObjectURL(a.href),1000); };

// Translate interface labels only; device names and recognised speech remain untouched.
const translations = [
  ['SMT–3576 // 智能会议终端','SMT–3576 // SMART MEETING'],
  ['◢ 实时会议画面','◢ LIVE VIDEO'], ['◢ 会议控制','◢ MEETING'], ['◢ 设备控制','◢ DEVICES'], ['◢ 智能分析','◢ AI ANALYSIS'],
  ['WEBRTC // 端到端音视频','WEBRTC // PEER-TO-PEER MEDIA'], ['本机画面 // LOCAL','Local video // LOCAL'],
  ['远端参会设备 // REMOTE','Remote participant // REMOTE'], ['远端屏幕共享 // SCREEN','Remote screen // SCREEN'], ['本机屏幕共享 // SCREEN','Local screen // SCREEN'],
  ['创建或加入会议后开启音视频','Create or join a meeting to enable media'], ['创建并加入会议','Create meeting'], ['输入 9 位会议号','Enter 9-digit meeting ID'],
  ['会议号 // MEETING ID','Meeting ID // MEETING ID'], ['复制会议号','Copy meeting ID'], ['离开会议','Leave meeting'], ['加入','Join'],
  ['麦克风：开','Microphone: On'], ['麦克风：关','Microphone: Off'], ['摄像头：开','Camera: On'], ['摄像头：关','Camera: Off'],
  ['选择摄像头','Select camera'], ['当前摄像头','Current camera'], ['未发现摄像头','No camera found'], ['重试摄像头','Retry camera'],
  ['停止共享屏幕','Stop sharing'], ['共享屏幕','Share screen'], ['点击画面放大，再次点击或按 Esc 恢复','Click a tile to enlarge; click again or press Esc to restore'],
  ['各设备摄像头识别人数与举手状态','People and raised hands by camera'], ['尚未启动分析','Analysis not started'], ['暂无有效画面分析','No recent camera analysis'],
  ['启动本机画面分析','Start camera analysis'], ['停止本机画面分析','Stop camera analysis'], ['提示音：开','Alert sound: On'], ['提示音：关','Alert sound: Off'],
  ['语音转写按设备标注；同一麦克风前的多人无法区分。','Transcripts are labeled by device; speakers sharing a microphone cannot be distinguished.'],
  ['启动本机语音转写','Start transcription'], ['停止本机语音转写','Stop transcription'], ['清空记录','Clear'], ['下载记录','Download'],
  ['未启动','Not started'], ['已停止','Stopped'], ['正在加载识别模型…','Loading speech model…'],
  ['等待创建或加入会议','Create or join a meeting'], ['正在开启设备…','Opening devices…'],
  ['会议已创建，可将会议号分享给同一局域网的参会者','Meeting created. Share the ID with participants on the LAN.'],
  ['已加入会议，正在连接其他设备','Joined. Connecting to participants…'], ['已离开会议','Left meeting'], ['会议号已复制','Meeting ID copied'],
  ['复制失败，请手动记录会议号','Copy failed; please note the meeting ID'], ['请输入 9 位会议号','Enter a 9-digit meeting ID'],
  ['摄像头不可用，已使用仅音频模式','Camera unavailable; using audio only'], ['麦克风不可用，已使用仅视频模式','Microphone unavailable; using video only'],
  ['浏览器不支持 WebRTC 或当前页面不是安全连接','WebRTC unavailable or page is not a secure context'],
  ['无法打开摄像头或麦克风，请检查权限和设备占用','Cannot open camera or microphone; check permissions and device use'],
  ['未收到摄像头画面，请选择摄像头或点击重试','No camera frames. Select a camera or retry'], ['摄像头未输出画面，请选择设备或重试','Camera is not producing frames; select a device or retry'],
  ['正在等待摄像头画面…','Waiting for camera frames…'], ['摄像头已重新连接','Camera reconnected'], ['摄像头重试失败：','Camera retry failed: '],
  ['无法连接远端设备，请检查双方网络或重新加入','Cannot connect; check both networks or rejoin'], ['无法更新远端画面','Could not update remote video'],
  ['屏幕共享状态通知失败，请重新加入会议','Screen-sharing notification failed; rejoin'], ['屏幕共享停止失败，请重新加入会议','Could not stop screen sharing; rejoin'],
  ['已停止屏幕共享','Screen sharing stopped'], ['当前浏览器不支持屏幕共享','Screen sharing is unavailable in this browser'], ['未获取到屏幕画面','No screen video received'],
  ['无法向所有参会设备发送屏幕画面','Could not send the screen to all participants'], ['正在共享屏幕','Sharing screen'], ['屏幕共享失败：','Screen sharing failed: '],
  ['已取消屏幕共享','Screen sharing cancelled'], ['对方不支持屏幕共享，请刷新页面','The other device does not support screen sharing; refresh'],
  ['信令异常：','Signalling error: '], ['画面分析：','Camera analysis: '], ['音频发送：','Audio upload: '], ['语音转写：','Transcription: '],
  ['✋ 检测到举手','✋ Hand raised'], ['未检测到举手','No raised hand'], ['画面人数','People in view'], ['在线设备','Devices online'],
  ['退出全屏','Exit fullscreen'], ['全屏','Fullscreen'], ['检测到举手','Hand raise detected'], ['参会设备：','Participant: '], ['本机麦克风音量','Local microphone level'], ['音频处理未启动，请检查浏览器权限','Audio processing did not start; check browser permissions'], ['无法切换全屏：','Cannot change fullscreen: '],
  ['本机','Local'], ['远端','Remote'], ['，点击放大或恢复',', click to enlarge or restore'], ['会议号','Meeting ID']
];
function translateText(value, toEnglish) {
  const pairs=translations.map(([zh,en])=>toEnglish?[zh,en]:[en,zh]).sort((a,b)=>b[0].length-a[0].length);
  for (const [from,to] of pairs) value=value.split(from).join(to);
  return value;
}
function translateNode(root, toEnglish) {
  if (root.nodeType===Node.TEXT_NODE) {
    const parent=root.parentElement;
    if (!parent || parent.closest('#transcripts, #cameraDevice, #language')) return;
    const next=translateText(root.nodeValue,toEnglish); if(next!==root.nodeValue) root.nodeValue=next;
    return;
  }
  if (root.nodeType!==Node.ELEMENT_NODE || root.closest('#transcripts, #cameraDevice, #language')) return;
  for (const attr of ['placeholder','aria-label']) if(root.hasAttribute(attr)) {
    const value=root.getAttribute(attr), next=translateText(value,toEnglish); if(value!==next) root.setAttribute(attr,next);
  }
  for(const child of root.childNodes) translateNode(child,toEnglish);
}
const languageObserver=new MutationObserver(changes=>{
  if(language!=='en') return;
  for(const change of changes) {
    if(change.type==='characterData') translateNode(change.target,true);
    else if(change.type==='childList') for(const node of change.addedNodes) translateNode(node,true);
  }
});
languageObserver.observe(document.body,{subtree:true,childList:true,characterData:true});
function setLanguage(next) {
  languageObserver.disconnect(); language=next; localStorage.setItem('meeting-language',language);
  translateNode(document.body, language==='en'); document.documentElement.lang=language==='en'?'en':'zh-CN';
  document.title=language==='en'?'Smart Meeting':'智能会议终端';
  $('language').textContent=language==='en'?'中文 / EN':'EN / 中文';
  renderRaiseBanner(); translateNode($('raiseBanner'), language==='en');
  for(const row of $('transcripts').children) if(row.dataset.peer && row.firstChild) row.firstChild.textContent=(language==='en' ? (row.dataset.peer===session?.peer ? 'Local' : 'Remote '+row.dataset.peer.slice(0,5)) : sourceLabel(row.dataset.peer))+'：';
  languageObserver.observe(document.body,{subtree:true,childList:true,characterData:true});
}
$('language').onclick=()=>setLanguage(language==='zh'?'en':'zh');
if(language==='en') setLanguage('en');
