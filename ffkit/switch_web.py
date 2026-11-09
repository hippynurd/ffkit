"""
Browser pages for `ffkit switch mark` and `ffkit switch zoom`.

A tiny local web server serves the proxy video to your normal browser with range requests
(so seeking works) and receives each key press. Nothing to install, no sudo.
"""
from __future__ import annotations

import json
import threading
import time
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

PAGE = r"""<!doctype html>
<meta charset="utf-8">
<title>ffkit switch</title>
<style>
  html,body{margin:0;background:#111;color:#eee;font:15px/1.4 system-ui,sans-serif;height:100%;overflow:hidden}
  #wrap{position:relative;height:100%;display:flex;flex-direction:column}
  #stage{position:relative;flex:1;min-height:0;background:#000}
  canvas{position:absolute;inset:0;width:100%;height:100%;cursor:crosshair}
  video{position:absolute;width:2px;height:2px;opacity:0;pointer-events:none}
  .hidden{display:none !important}
  #banner{position:absolute;top:12px;right:16px;text-align:right;pointer-events:none;text-shadow:0 2px 8px #000}
  #cam{font-size:46px;font-weight:800;letter-spacing:.02em}
  #cam.none{font-size:26px;font-weight:500;opacity:.8}
  #shotname{font-size:20px;font-weight:600;color:#ffd34d;min-height:1.4em}
  #count{font-size:18px;opacity:.85}
  #toast{position:absolute;left:16px;top:12px;font-size:22px;font-weight:600;text-shadow:0 2px 8px #000;opacity:0;transition:opacity .2s;pointer-events:none}
  #setup{position:absolute;left:16px;right:16px;bottom:14px;background:rgba(0,0,0,.7);padding:10px 14px;border-radius:8px;font-size:16px;line-height:1.5;pointer-events:none}
  #shots{display:flex;gap:8px;padding:8px 16px;background:#161616;border-top:1px solid #333;flex-wrap:wrap;align-items:center;min-height:22px}
  .shot{padding:4px 10px;border:1px solid #444;border-radius:6px;background:#222;white-space:nowrap}
  .shot b{font-family:ui-monospace,monospace;margin-right:7px;color:#ffd34d}
  .shot.plain{opacity:.7}
  .shot.dark{opacity:.3}
  .shot.on{border-color:#ffd34d;background:#3a3010;opacity:1}
  .note{opacity:.6;font-size:14px}
  #seekrow{display:flex;align-items:center;gap:12px;padding:6px 16px;background:#1b1b1b;border-top:1px solid #333}
  #seek{flex:1;accent-color:#ffd34d;height:22px;cursor:pointer;margin:0}
  #dur{font-variant-numeric:tabular-nums;opacity:.7;min-width:7ch;text-align:right}
  #bar{display:flex;gap:18px;align-items:center;padding:10px 16px;background:#1b1b1b;border-top:1px solid #333;flex-wrap:wrap}
  #time{font-variant-numeric:tabular-nums;font-size:22px;font-weight:600;min-width:7ch}
  #speed{font-variant-numeric:tabular-nums;opacity:.8}
  .k{display:inline-block;padding:1px 7px;border:1px solid #555;border-radius:5px;background:#222;font-family:ui-monospace,monospace;margin-right:3px}
  #hint{opacity:.75;flex:1}
  button{background:#2b2b2b;color:#eee;border:1px solid #555;border-radius:6px;padding:8px 14px;font-size:15px;cursor:pointer}
  button:hover{background:#383838}
  #overlay{position:absolute;inset:0;display:flex;align-items:center;justify-content:center;background:rgba(0,0,0,.55);font-size:26px;text-align:center;line-height:1.6;cursor:pointer;z-index:2}
  #overlay.hidden{display:none}
</style>
<div id="wrap">
  <div id="stage">
    <video id="v" src="/grid.mp4" preload="auto" playsinline></video>
    <canvas id="c"></canvas>
    <div id="banner"><div id="cam" class="none">no camera yet</div><div id="shotname"></div><div id="count"></div></div>
    <div id="toast"></div>
    <div id="setup" class="hidden"></div>
    <div id="overlay">Click, then press <b>space</b> to roll<br>
      <small><b>1 2 3</b>… = the key's shot (a camera, framed) or plain camera &nbsp; <b>shift</b> + a key = set up that key's shot<br>
      <b>u</b> undo &nbsp; <b>s</b> / <b>e</b> show start / end &nbsp; <b>[ ]</b> speed &nbsp; <b>← →</b> 5s &nbsp; the red tile is what the viewer sees</small></div>
  </div>
  <div id="shots"></div>
  <div id="seekrow"><span id="time">0:00</span><input id="seek" type="range" min="0" max="1000" value="0" step="1" title="drag to move through the show"><span id="dur"></span></div>
  <div id="bar">
    <span id="speed">1.00×</span>
    <span id="hint"><span class="k">1</span><span class="k">2</span><span class="k">3</span>… shot / camera (or click a tile) &nbsp; <span class="k">⇧</span>+key set up &nbsp; <span class="k">space</span> pause &nbsp; <span class="k">u</span> undo &nbsp; <span class="k">s</span><span class="k">e</span> show start/end &nbsp; <span class="k">[</span><span class="k">]</span> speed &nbsp; <span class="k">←</span><span class="k">→</span> 5s</span>
    <button id="done">Done for now</button>
  </div>
</div>
<script>
const v = document.getElementById('v'), cv = document.getElementById('c'), g = cv.getContext('2d');
const camEl = document.getElementById('cam'), shotEl = document.getElementById('shotname'), countEl = document.getElementById('count');
const toast = document.getElementById('toast'), timeEl = document.getElementById('time'), speedEl = document.getElementById('speed');
const overlay = document.getElementById('overlay'), shotsEl = document.getElementById('shots'), setupEl = document.getElementById('setup');
let names = {}, cams = [], shots = {}, tiles = {}, slots = [], pslot = null, cover = {};
let current = null, currentShot = null, count = 0, toastTimer = null, stripKey = '';
let P = {segs: [], tracks: {}}, moves = {}, settleGap = 0;   // the cut and the framing, as the render sees them
let setup = null;   // while setting up a key's shot: {key, cam, z, cx, cy, name, existed}
const ZMAX = 4, NUDGE = 0.08, NUDGE_BIG = 0.25, ZSTEP = 0.1, ZSTEP_BIG = 0.5;

function fmt(t){ t = Math.max(0, t); const h = Math.floor(t/3600), m = Math.floor(t%3600/60), s = t%60;
  return (h ? h + ':' + String(m).padStart(2,'0') : m) + ':' + s.toFixed(2).padStart(5,'0'); }
const esc = s => String(s).replace(/[&<>"]/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));
const clamp = (x, a, b) => Math.min(b, Math.max(a, x));
const camName = c => names[c] ? names[c] : 'cam ' + c;
const shotText = (k, s) => (s.name || camName(s.cam)) + (s.z > 1.0005 ? ' ' + s.z.toFixed(1) + '×' : '');
const rolling = (cam, T) => { const c = cover[cam]; return !c || (T >= c[0] && T < c[1]); };
function banner(){
  if (current){ camEl.className = ''; camEl.textContent = 'CAM ' + current + (names[current] ? '  ' + names[current] : ''); }
  else { camEl.className = 'none'; camEl.textContent = 'no camera yet'; }
  const s = currentShot && shots[currentShot];
  shotEl.textContent = s ? 'shot ' + currentShot + (s.name ? '  ' + s.name : '') + (s.z > 1.0005 ? '  ' + s.z.toFixed(1) + '×' : '') : '';
  countEl.textContent = count + ' marks'; }
// the strip: what each key does right now (greyed when that camera has no footage at this moment)
function strip(T){
  const dark = k => !rolling(k, T);
  const key = [currentShot, current, cams.map(c => dark(c) ? 1 : 0).join('')].join('|');
  if (key === stripKey) return; stripKey = key;
  const parts = [];
  for (let n = 1; n <= 9; n++){ const k = String(n), s = shots[k];
    if (s) parts.push('<span class="shot' + (dark(s.cam) ? ' dark' : '') + (currentShot === k ? ' on' : '') + '"><b>' + k + '</b>' + esc(shotText(k, s)) + '</span>');
    else if (cams.includes(k)) parts.push('<span class="shot plain' + (dark(k) ? ' dark' : '') + (!currentShot && current === k ? ' on' : '') + '"><b>' + k + '</b>' + esc(camName(k)) + '</span>'); }
  const missing = cams.filter(c => shots[c] && shots[c].cam !== c && !Object.values(shots).some(s => s.cam === c));
  if (missing.length) parts.push('<span class="note">no key for ' + esc(missing.map(camName).join(', ')) + ': shift + a spare digit sets one up</span>');
  else parts.push('<span class="note">shift + a digit: set up (or change) that key’s shot</span>');
  shotsEl.innerHTML = parts.join(''); }
function say(msg){ toast.textContent = msg; toast.style.opacity = 1; clearTimeout(toastTimer); toastTimer = setTimeout(()=>toast.style.opacity=0, 1600); }
async function post(path, body){ const r = await fetch(path, {method:'POST', headers:{'content-type':'application/json'}, body: JSON.stringify(body||{})}); return r.json(); }

// ---- what the viewer sees: the same maths as ffkit/switch.py (and the zoom page)
const smooth = s => { s = clamp(s, 0, 1); return s*s*(3-2*s); };
function valueOf(ms, t){ let val = [1, .5, .5];
  for (const m of ms){ if (t < m.t) break; const s = m.d <= 0 ? 1 : smooth((t - m.t)/m.d);
    val = m.from.map((a, i) => a + (m.to[i] - a)*s); } return val; }
function settle(keys){ const gap = settleGap; if (!(gap > 0)) return keys; const out = []; let gr = [];
  const flush = () => { if (gr.length){ const f = gr[0], l = gr[gr.length-1]; out.push({t: f.t, z: l.z, cx: l.cx, cy: l.cy, e: f.e, s: l.s}); gr = []; } };
  for (const k of keys){ if (k.f || k.m){ flush(); out.push(k); } else if (gr.length && k.t - gr[gr.length-1].t <= gap && k.e > 0) gr.push(k); else { flush(); gr.push(k); } }
  flush(); return out; }
function build(){ moves = {}; for (const cam in P.tracks){ const ms = []; for (const k of settle(P.tracks[cam])){ const from = valueOf(ms, k.t);
  ms.push({t: k.t, d: k.e, from, to: [k.z, k.cx, k.cy], s: k.s}); } moves[cam] = ms; } }
function segAt(T){ for (const s of P.segs){ if (T >= s[0] && T < s[1]) return s; } return P.segs.length ? P.segs[P.segs.length-1] : null; }
function liveCam(T){ const s = segAt(T); return s ? s[2] : (current || cams[0]); }
function stateAt(cam, T){ const [z, cx, cy] = valueOf(moves[cam] || [], T); const zz = Math.max(1, z), h = .5/zz;
  return {z: zz, cx: clamp(cx, h, 1-h), cy: clamp(cy, h, 1-h)}; }
function shotAt(cam, T){ let s = null; for (const m of (moves[cam] || [])){ if (m.t > T) break; s = m.s || null; } return s; }
function takeProgram(r){ if (r && r.program){ P = r.program; build(); } }

fetch('/state').then(r=>r.json()).then(s => { names = s.names; cams = s.cams || Object.keys(names); shots = s.shots || {}; tiles = s.tiles || {};
  slots = s.slots || cams.map(c => ({x: tiles[c][0], y: tiles[c][1], w: tiles[c][2], h: tiles[c][3], cams: [c]})); pslot = s.pslot || null; cover = s.cover || {};
  settleGap = s.settle || 0; takeProgram(s);
  current = s.current; currentShot = s.current_shot || null; count = s.count; banner(); strip(s.start);
  const go = () => { v.currentTime = s.start; }; if (v.readyState >= 1) go(); else v.addEventListener('loadedmetadata', go, {once:true});
  requestAnimationFrame(draw); });

overlay.addEventListener('click', () => { overlay.classList.add('hidden'); v.play(); });
// the slider: drag to move through the show (rough while dragging, exact when you let go)
const seek = document.getElementById('seek'), durEl = document.getElementById('dur');
let scrubbing = false;
const seekTo = (t, exact) => { if (!exact && v.fastSeek) v.fastSeek(t); else v.currentTime = t; timeEl.textContent = fmt(t); };
v.addEventListener('loadedmetadata', () => { seek.max = Math.floor(v.duration*10); durEl.textContent = fmt(v.duration); });
seek.addEventListener('pointerdown', () => { scrubbing = true; });
seek.addEventListener('input', () => { scrubbing = true; seekTo(seek.value/10, false); });
seek.addEventListener('change', () => { seekTo(seek.value/10, true); scrubbing = false; seek.blur(); });
seek.addEventListener('pointerup', () => { scrubbing = false; seek.blur(); });
setInterval(() => { timeEl.textContent = fmt(v.currentTime); if (!scrubbing && v.duration) seek.value = Math.round(v.currentTime*10); }, 100);

// ---- drawing: the cameras' tiles from the grid, the one on air outlined with its crop, and the
// program view (what the viewer sees right now) in the spare slot
function fitRect(){ const W = cv.width, H = cv.height, vw = v.videoWidth || 16, vh = v.videoHeight || 9, k = Math.min(W/vw, H/vh);
  return {x: (W - vw*k)/2, y: (H - vh*k)/2, k}; }
function dest(fr, s){ return {x: fr.x + s.x*fr.k, y: fr.y + s.y*fr.k, w: s.w*fr.k, h: s.h*fr.k}; }
function label(text, x, y, dpr, big){ g.font = (big ? '600 ' : '500 ') + ((big ? 17 : 14)*dpr) + 'px system-ui';
  const w = g.measureText(text).width, h = (big ? 26 : 21)*dpr;
  g.fillStyle = 'rgba(0,0,0,.6)'; g.fillRect(x, y, w + 12*dpr, h); g.fillStyle = '#fff'; g.fillText(text, x + 6*dpr, y + h - 7*dpr); }
function cropBox(d, st){ return {x: d.x + (st.cx - .5/st.z)*d.w, y: d.y + (st.cy - .5/st.z)*d.h, w: d.w/st.z, h: d.h/st.z}; }
function mask(d, b){ g.fillStyle = 'rgba(0,0,0,.45)';
  g.fillRect(d.x, d.y, d.w, b.y - d.y); g.fillRect(d.x, b.y + b.h, d.w, d.y + d.h - b.y - b.h);
  g.fillRect(d.x, b.y, b.x - d.x, b.h); g.fillRect(b.x + b.w, b.y, d.x + d.w - b.x - b.w, b.h); }
function drawGrid(dpr){
  const T = v.currentTime, fr = fitRect(), live = liveCam(T), st = stateAt(live, T);
  for (const s of slots){
    const cam = s.cams.find(c => rolling(c, T)) || s.cams[0], src = tiles[cam], d = dest(fr, s);
    if (!src) continue;
    g.drawImage(v, src[0], src[1], src[2], src[3], d.x, d.y, d.w, d.h);
    if (!rolling(cam, T)) label('no footage at this moment', d.x + 12*dpr, d.y + d.h - 34*dpr, dpr);
    if (cam === live){
      if (st.z > 1.001){ const b = cropBox(d, st); mask(d, b); g.strokeStyle = '#ffd34d'; g.lineWidth = 2*dpr; g.strokeRect(b.x, b.y, b.w, b.h); }
      g.strokeStyle = '#e33'; g.lineWidth = 4*dpr; g.strokeRect(d.x + 2*dpr, d.y + 2*dpr, d.w - 4*dpr, d.h - 4*dpr); }
  }
  if (pslot){
    const d = dest(fr, pslot), src = tiles[live];
    g.fillStyle = '#000'; g.fillRect(d.x, d.y, d.w, d.h);
    if (src && rolling(live, T)){ const sw = src[2]/st.z, sh = src[3]/st.z, sx = src[0] + (st.cx - .5/st.z)*src[2], sy = src[1] + (st.cy - .5/st.z)*src[3];
      g.drawImage(v, sx, sy, sw, sh, d.x, d.y, d.w, d.h); }
    const on = shotAt(live, T), os = on && shots[on];
    label('PROGRAM   cam ' + live + (names[live] ? ' ' + names[live] : '') + (os ? '   ·   ' + on + (os.name ? ' ' + os.name : '') : '') + (st.z > 1.001 ? '   ·   ' + st.z.toFixed(2) + '×' : ''),
          d.x + 10*dpr, d.y + 8*dpr, dpr, true);
    g.strokeStyle = '#e33'; g.lineWidth = 4*dpr; g.strokeRect(d.x + 2*dpr, d.y + 2*dpr, d.w - 4*dpr, d.h - 4*dpr);
  }
  strip(T);
}
function draw(){
  requestAnimationFrame(draw);
  const dpr = window.devicePixelRatio || 1, W = Math.round(cv.clientWidth*dpr), H = Math.round(cv.clientHeight*dpr);
  if (cv.width !== W || cv.height !== H){ cv.width = W; cv.height = H; }
  g.fillStyle = '#000'; g.fillRect(0, 0, W, H);
  if (!v.videoWidth) return;
  if (setup) drawSetup(dpr); else drawGrid(dpr);
}

// ---- setting up a key's shot: one camera's tile from the grid, shown big, with the framing box on it
function enterSetup(key){
  const s = shots[key], cam = s ? s.cam : (cams.includes(key) ? key : cams[0]);
  setup = {key, cam, z: s ? s.z : 1, cx: s ? s.cx : .5, cy: s ? s.cy : .5, name: s ? s.name : '', existed: !!s};
  v.pause(); setupEl.classList.remove('hidden'); overlay.classList.add('hidden'); setupText(); }
function leaveSetup(){ setup = null; drag = null; setupEl.classList.add('hidden'); }
function setupText(){ const s = setup;
  setupEl.innerHTML = '<b>Setting up key ' + s.key + '</b>: camera <b>' + s.cam + '</b> ' + esc(names[s.cam] || '') + ' at <b>' + s.z.toFixed(2) + '×</b>' +
    (s.name ? ' — “' + esc(s.name) + '”' : '') +
    '<br><small><b>drag a box</b> around what you want to see · click to re-centre · wheel or - + fine-tune the zoom · a d w s nudge · a camera number changes camera · space, ← → or the slider to find a good moment · ' +
    '<b>Enter</b> saves · <b>Esc</b> cancels' + (s.existed ? ' · <b>Delete</b> removes this shot' : '') + '</small>'; }
function tileRect(){ const W = cv.width, H = cv.height, t = tiles[setup.cam] || [0, 0, v.videoWidth || 16, v.videoHeight || 9];
  const k = Math.min(W/t[2], H/t[3]); return {x:(W - t[2]*k)/2, y:(H - t[3]*k)/2, w:t[2]*k, h:t[3]*k, t}; }
function boxOf(r, z, cx, cy){ const h = .5/z; cx = clamp(cx, h, 1-h); cy = clamp(cy, h, 1-h);
  return {x: r.x + (cx - h)*r.w, y: r.y + (cy - h)*r.h, w: r.w/z, h: r.h/z}; }
function drawSetup(dpr){
  const r = tileRect(), t = r.t, s = setup, f = liveFraming();
  g.drawImage(v, t[0], t[1], t[2], t[3], r.x, r.y, r.w, r.h);
  if (!rolling(s.cam, v.currentTime)) label('no footage at this moment: move the slider to where this camera was rolling', r.x + 12*dpr, r.y + r.h - 34*dpr, dpr);
  for (const [k2, s2] of Object.entries(shots)){      // the other shots on this camera, faintly
    if (s2.cam !== s.cam || k2 === s.key || s2.z <= 1.001) continue;
    const b = boxOf(r, s2.z, s2.cx, s2.cy); g.setLineDash([6*dpr, 6*dpr]); g.strokeStyle = 'rgba(255,255,255,.45)'; g.lineWidth = dpr; g.strokeRect(b.x, b.y, b.w, b.h); g.setLineDash([]);
    g.fillStyle = 'rgba(255,255,255,.6)'; g.font = (14*dpr) + 'px system-ui'; g.fillText(k2 + (s2.name ? ' ' + s2.name : ''), b.x + 6*dpr, b.y + 16*dpr); }
  if (f.z > 1.001){ const b = boxOf(r, f.z, f.cx, f.cy); mask(r, b); g.strokeStyle = '#ffd34d'; g.lineWidth = 2*dpr; g.strokeRect(b.x, b.y, b.w, b.h); }
  else { g.strokeStyle = '#ffd34d'; g.lineWidth = 2*dpr; g.strokeRect(r.x + dpr, r.y + dpr, r.w - 2*dpr, r.h - 2*dpr); }
  if (drag && drag.moved){                              // the box being drawn
    const x = r.x + Math.min(drag.x0, drag.x)*r.w, y = r.y + Math.min(drag.y0, drag.y)*r.h;
    g.setLineDash([4*dpr, 4*dpr]); g.strokeStyle = '#fff'; g.lineWidth = dpr;
    g.strokeRect(x, y, Math.abs(drag.x - drag.x0)*r.w, Math.abs(drag.y - drag.y0)*r.h); g.setLineDash([]); }
}
function mousePix(e){ const b = cv.getBoundingClientRect(), dpr = window.devicePixelRatio || 1; return [(e.clientX - b.left)*dpr, (e.clientY - b.top)*dpr]; }
function pointAt(e){ const [px, py] = mousePix(e), r = tileRect(); return [clamp((px - r.x)/r.w, 0, 1), clamp((py - r.y)/r.h, 0, 1)]; }
// Drag a box around what you want to see: the shot becomes the smallest frame (same shape as the
// picture) that covers it. Both axes run 0..1 across the whole picture, so the zoom that just
// covers a box w wide and h tall is 1/max(w, h). A plain click re-centres the shot where you clicked.
let drag = null;
function framingFor(d){ const w = Math.abs(d.x - d.x0), h = Math.abs(d.y - d.y0);
  return {z: Math.round(clamp(1/Math.max(w, h, 1/ZMAX), 1, ZMAX)*100)/100, cx: (d.x + d.x0)/2, cy: (d.y + d.y0)/2}; }
function liveFraming(){ return (drag && drag.moved) ? framingFor(drag) : setup; }
cv.addEventListener('mousedown', e => { if (!v.videoWidth) return; e.preventDefault();
  if (setup){ const [x, y] = pointAt(e); drag = {x0: x, y0: y, x, y, moved: false}; return; }
  // clicking a camera's tile is the same as pressing its key
  const [px, py] = mousePix(e), fr = fitRect(), T = v.currentTime;
  for (const s of slots){ const d = dest(fr, s);
    if (px >= d.x && px < d.x + d.w && py >= d.y && py < d.y + d.h){ mark(s.cams.find(c => rolling(c, T)) || s.cams[0], T, true); return; } } });
cv.addEventListener('mousemove', e => { if (!setup || !drag) return; [drag.x, drag.y] = pointAt(e);
  if (Math.abs(drag.x - drag.x0) > 0.01 || Math.abs(drag.y - drag.y0) > 0.01) drag.moved = true; });
window.addEventListener('mouseup', () => { if (!setup || !drag) return; const d = drag; drag = null;
  if (d.moved) Object.assign(setup, framingFor(d)); else { setup.cx = d.x0; setup.cy = d.y0; }
  setupText(); });
cv.addEventListener('wheel', e => { if (!setup) return; e.preventDefault();
  setup.z = Math.round(clamp(setup.z - e.deltaY*0.0008, 1, ZMAX)*100)/100; setupText(); }, {passive:false});
async function saveSetup(){ const s = setup;
  const def = s.name || (camName(s.cam) + (s.z > 1.0005 ? ' ' + s.z.toFixed(1) + '×' : ' wide'));
  const name = window.prompt('Name for key ' + s.key + ' (shows on the strip):', def);
  if (name === null) return;
  const r = await post('/shot', {key: s.key, cam: s.cam, z: s.z, cx: s.cx, cy: s.cy, name: name.trim()});
  shots = r.shots || shots; takeProgram(r); leaveSetup(); banner(); stripKey = ''; say('key ' + s.key + ' = ' + (name.trim() || def)); }
async function setupKey(e){
  const s = setup, k = (e.key || '').toLowerCase();
  if (e.code && e.code.startsWith('Digit')){ const n = e.code.slice(5); e.preventDefault();
    if (cams.includes(n)){ s.cam = n; setupText(); } else say('no camera ' + n); }
  else if (k === 'enter'){ e.preventDefault(); await saveSetup(); }
  else if (k === 'escape'){ e.preventDefault(); leaveSetup(); say('left key ' + s.key + ' as it was'); }
  else if (k === 'delete' || k === 'backspace'){ e.preventDefault(); if (!s.existed) return;
    const r = await post('/shot', {key: s.key, delete: true}); shots = r.shots || shots; takeProgram(r); leaveSetup(); banner(); stripKey = ''; say('removed the shot on key ' + s.key); }
  else if (k.length === 1 && 'adws'.includes(k)){ e.preventDefault(); const step = (e.shiftKey ? NUDGE_BIG : NUDGE)/s.z;
    if (k === 'a') s.cx = clamp(s.cx - step, 0, 1); else if (k === 'd') s.cx = clamp(s.cx + step, 0, 1);
    else if (k === 'w') s.cy = clamp(s.cy - step, 0, 1); else s.cy = clamp(s.cy + step, 0, 1); }
  else if (k === '-' || k === '_' || k === '=' || k === '+'){ e.preventDefault(); const d = (e.shiftKey ? ZSTEP_BIG : ZSTEP)*((k === '-' || k === '_') ? -1 : 1);
    s.z = Math.round(clamp(s.z + d, 1, ZMAX)*100)/100; setupText(); }
  else if (e.key === ' '){ e.preventDefault(); if (v.paused) v.play(); else v.pause(); }
  else if (e.key === 'ArrowLeft'){ e.preventDefault(); v.currentTime = Math.max(0, v.currentTime - 5); }
  else if (e.key === 'ArrowRight'){ e.preventDefault(); v.currentTime = v.currentTime + 5; }
  else if (e.key === '['){ v.playbackRate = Math.max(0.5, Math.round((v.playbackRate - 0.25)*100)/100); speedEl.textContent = v.playbackRate.toFixed(2) + '×'; }
  else if (e.key === ']'){ v.playbackRate = Math.min(3, Math.round((v.playbackRate + 0.25)*100)/100); speedEl.textContent = v.playbackRate.toFixed(2) + '×'; }
}

// ---- marking
// a key press: that key's shot, or the plain camera of that number. plain = the camera itself, as it
// is (clicking a tile), even when a shot sits on that number
async function mark(n, t, plain){
  const s = plain ? null : shots[n];
  if (!s && !cams.includes(n)){ say('nothing on key ' + n + ' (shift-' + n + ' sets up a shot there)'); return; }
  const r = await post('/mark', {t, key: n, plain: !!plain});
  current = r.current; currentShot = r.current_shot || null; count = r.count; takeProgram(r); banner(); stripKey = '';
  say((s ? 'shot ' + n + (s.name ? ' ' + s.name : '') : camName(n) + (plain && shots[n] ? ' as it is' : '')) + '  @ ' + fmt(t)); }
document.addEventListener('keydown', async (e) => {
  if (setup){ if (!e.repeat || 'adws-_=+'.includes(e.key)) await setupKey(e); return; }
  if (e.repeat) return;
  const k = e.key;
  if (e.code && e.code.startsWith('Digit')){ const n = e.code.slice(5); if (n === '0') return; e.preventDefault();
    if (e.shiftKey){ enterSetup(n); return; }
    await mark(n, v.currentTime); }
  else if (k === ' ') { e.preventDefault(); overlay.classList.add('hidden'); if (v.paused) v.play(); else v.pause(); }
  else if (k === 'u') { e.preventDefault(); const r = await post('/undo'); current = r.current; currentShot = r.current_shot || null; count = r.count; takeProgram(r); banner(); stripKey = '';
    say(r.removed ? 'undid ' + r.removed : 'nothing to undo'); }
  else if (k === 's' || k === 'e') { e.preventDefault(); const t = v.currentTime; await post('/edge', {kind: k === 's' ? 'START' : 'END', t}); say('show ' + (k === 's' ? 'start' : 'end') + ' = ' + fmt(t)); }
  else if (k === '[') { v.playbackRate = Math.max(0.5, Math.round((v.playbackRate - 0.25)*100)/100); speedEl.textContent = v.playbackRate.toFixed(2) + '×'; }
  else if (k === ']') { v.playbackRate = Math.min(3, Math.round((v.playbackRate + 0.25)*100)/100); speedEl.textContent = v.playbackRate.toFixed(2) + '×'; }
  else if (k === 'ArrowLeft') { e.preventDefault(); v.currentTime = Math.max(0, v.currentTime - 5); }
  else if (k === 'ArrowRight') { e.preventDefault(); v.currentTime = v.currentTime + 5; }
  else if (k === 'Backspace') { v.playbackRate = 1; speedEl.textContent = '1.00×'; }
});
document.getElementById('done').addEventListener('click', async () => { v.pause(); await post('/quit'); document.body.innerHTML = '<div style="padding:40px;font-size:22px">Saved. You can close this tab.<br><small style="opacity:.7">Run the same command again to pick up where you left off.</small></div>'; });
window.addEventListener('beforeunload', () => { navigator.sendBeacon('/quit'); });
</script>
"""


class _Ctx:
    def __init__(self, grid: Path, log: Path, names: dict, start: float, current, count: int,
                 shots_path: Path | None = None, tiles: dict | None = None, current_shot=None, levels=None,
                 slots: list | None = None, pslot: dict | None = None, cover: dict | None = None,
                 settle: float = 0.0, program_fn=None):
        self.grid, self.log, self.names, self.start = grid, log, names, start
        self.current, self.count, self.current_shot = current, count, current_shot
        self.cams = list(names.keys())
        self.shots_path, self.tiles, self.levels = shots_path, tiles or {}, levels or []
        self.slots, self.pslot, self.cover, self.settle = slots, pslot, cover or {}, settle
        self.program_fn = program_fn      # () -> {"segs": [[a, b, cam]...], "tracks": {...}}: the cut as the render sees it
        self.lock = threading.Lock()
        self.quit = threading.Event()

    def shots(self) -> dict:
        from .switch import load_shots
        return load_shots(self.shots_path) if self.shots_path else {}

    def program(self) -> dict:
        if self.program_fn is None:
            return {"segs": [], "tracks": {}}
        try:
            return self.program_fn()
        except Exception:               # no marks yet, or a camera missing: the page just shows the tiles
            return {"segs": [], "tracks": {}}

    def is_mark(self, line: str) -> bool:
        head, _, tail = line.partition(",")
        tail = tail.strip()
        return bool(head) and head[0].isdigit() and (tail.isdigit() or (tail.startswith("s") and tail[1:].isdigit()))

    def set_current_from(self, lines: list) -> None:
        """After an undo: what the last remaining mark put on screen."""
        self.current = self.current_shot = None
        for ln in reversed(lines):
            if self.is_mark(ln):
                key = ln.partition(",")[2].strip()
                if key.startswith("s"):
                    s = self.shots().get(key[1:])
                    self.current, self.current_shot = (s["cam"], key[1:]) if s else (key[1:], None)
                else:
                    self.current = key
                break


def _make_handler(ctx: _Ctx):
    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):  # quiet
            pass

        def _json(self, obj, status=200):
            data = json.dumps(obj).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self):
            path = urlparse(self.path).path
            if path == "/":
                data = PAGE.encode()
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)
            elif path == "/grid.mp4":
                _send_file(self, ctx.grid, "video/mp4")
            elif path == "/state":
                self._json({"start": ctx.start, "names": ctx.names, "cams": ctx.cams, "current": ctx.current,
                            "current_shot": ctx.current_shot, "count": ctx.count, "shots": ctx.shots(),
                            "tiles": ctx.tiles, "levels": ctx.levels, "slots": ctx.slots, "pslot": ctx.pslot,
                            "cover": ctx.cover, "settle": ctx.settle, "program": ctx.program()})
            else:
                self.send_error(404)

        def do_POST(self):
            path = urlparse(self.path).path
            n = int(self.headers.get("Content-Length") or 0)
            body = {}
            if n:
                try:
                    body = json.loads(self.rfile.read(n) or b"{}")
                except json.JSONDecodeError:
                    body = {}
            with ctx.lock:
                if path == "/mark":
                    # a key is a shot (a camera plus its framing, written as s3) if one is set up on it,
                    # else the plain camera of that number; "plain" asks for the camera itself either way
                    t = float(body.get("t", 0.0))
                    key = str(body.get("key", body.get("cam", ""))).strip()
                    shots = ctx.shots()
                    if key.isdigit() and key in shots and not body.get("plain"):
                        with ctx.log.open("a") as fh:
                            fh.write(f"{t:.3f},s{key}\n")
                        ctx.current, ctx.current_shot = shots[key]["cam"], key
                        ctx.count += 1
                    elif key.isdigit() and key in ctx.cams:
                        with ctx.log.open("a") as fh:
                            fh.write(f"{t:.3f},{key}\n")
                        ctx.current, ctx.current_shot = key, None
                        ctx.count += 1
                    self._json({"current": ctx.current, "current_shot": ctx.current_shot, "count": ctx.count,
                                "program": ctx.program()})
                elif path == "/shot":
                    from .switch import save_shots
                    shots = ctx.shots()
                    key = str(body.get("key", "")).strip()
                    if key.isdigit() and ctx.shots_path is not None:
                        if body.get("delete"):
                            shots.pop(key, None)
                        else:
                            try:
                                cam = str(body["cam"]).strip()
                                if cam in ctx.cams:
                                    shots[key] = {"cam": cam, "z": max(1.0, float(body.get("z", 1.0))),
                                                  "cx": min(1.0, max(0.0, float(body.get("cx", 0.5)))),
                                                  "cy": min(1.0, max(0.0, float(body.get("cy", 0.5)))),
                                                  "name": str(body.get("name") or "").strip()[:40]}
                            except (KeyError, TypeError, ValueError):
                                pass
                        save_shots(ctx.shots_path, shots)
                        if ctx.current_shot and ctx.current_shot not in shots:
                            ctx.current_shot = None
                    self._json({"shots": ctx.shots(), "program": ctx.program()})
                elif path == "/edge":
                    kind = body.get("kind")
                    if kind in ("START", "END"):
                        with ctx.log.open("a") as fh:
                            fh.write(f"{kind},{float(body.get('t', 0.0)):.3f}\n")
                    self._json({"ok": True})
                elif path == "/undo":
                    removed = None
                    lines = ctx.log.read_text().splitlines() if ctx.log.exists() else []
                    for i in range(len(lines) - 1, -1, -1):
                        if ctx.is_mark(lines[i]):
                            removed = lines[i]
                            del lines[i]
                            break
                    if removed:
                        ctx.log.write_text("\n".join(lines) + ("\n" if lines else ""))
                        ctx.count = max(0, ctx.count - 1)
                        ctx.set_current_from(lines)
                    self._json({"current": ctx.current, "current_shot": ctx.current_shot, "count": ctx.count,
                                "removed": removed, "program": ctx.program()})
                elif path == "/quit":
                    self._json({"ok": True})
                    ctx.quit.set()
                else:
                    self.send_error(404)
    return H


def _send_file(h: BaseHTTPRequestHandler, p: Path, ctype: str) -> None:
    """Serve a file with range requests, so the browser can seek."""
    size = p.stat().st_size
    start, end, status = 0, size - 1, 200
    rng = h.headers.get("Range")
    if rng and rng.startswith("bytes="):
        s, _, e = rng[6:].partition("-")
        if s:
            start = int(s)
            end = int(e) if e else size - 1
        elif e:
            start = max(0, size - int(e))
        end = min(end, size - 1)
        if start > end:
            h.send_response(416)
            h.send_header("Content-Range", f"bytes */{size}")
            h.end_headers()
            return
        status = 206
    length = end - start + 1
    h.send_response(status)
    h.send_header("Content-Type", ctype)
    h.send_header("Accept-Ranges", "bytes")
    h.send_header("Content-Length", str(length))
    if status == 206:
        h.send_header("Content-Range", f"bytes {start}-{end}/{size}")
    h.end_headers()
    with p.open("rb") as fh:
        fh.seek(start)
        left = length
        while left > 0:
            chunk = fh.read(min(1 << 20, left))
            if not chunk:
                break
            try:
                h.wfile.write(chunk)
            except (BrokenPipeError, ConnectionResetError):
                return
            left -= len(chunk)


def _run_server(handler, open_browser: bool, port: int, quit_evt: threading.Event, what: str) -> None:
    httpd = ThreadingHTTPServer(("127.0.0.1", port), handler)
    httpd.daemon_threads = True
    url = f"http://127.0.0.1:{httpd.server_address[1]}/"
    th = threading.Thread(target=httpd.serve_forever, daemon=True)
    th.start()
    print(f"\n{what} page: {url}\n(if it didn't open by itself, open that address in any browser; "
          f"click Done or press Ctrl+C here when you stop)\n")
    if open_browser:
        try:
            webbrowser.open(url)
        except Exception:
            pass
    try:
        while not quit_evt.wait(0.5):
            pass
        time.sleep(0.3)
    except KeyboardInterrupt:
        pass
    finally:
        httpd.shutdown()
        httpd.server_close()


def serve(grid: Path, log: Path, names: dict, start: float, current, count: int,
          open_browser: bool = True, port: int = 0, shots_path: Path | None = None,
          tiles: dict | None = None, current_shot=None, levels=None, slots: list | None = None,
          pslot: dict | None = None, cover: dict | None = None, settle: float = 0.0, program_fn=None) -> None:
    ctx = _Ctx(grid, log, names, start, current, count, shots_path=shots_path, tiles=tiles,
               current_shot=current_shot, levels=levels, slots=slots, pslot=pslot, cover=cover,
               settle=settle, program_fn=program_fn)
    _run_server(_make_handler(ctx), open_browser, port, ctx.quit, "marking")


# --------------------------------------------------------------------------
# zoom pass
# --------------------------------------------------------------------------

ZOOM_PAGE = r"""<!doctype html>
<meta charset="utf-8">
<title>ffkit zoom</title>
<style>
  html,body{margin:0;background:#111;color:#eee;font:15px/1.4 system-ui,sans-serif;height:100%;overflow:hidden}
  #wrap{position:relative;height:100%;display:flex;flex-direction:column}
  #stage{position:relative;flex:1;min-height:0;background:#000}
  canvas{position:absolute;inset:0;width:100%;height:100%;cursor:crosshair}
  video{position:absolute;width:2px;height:2px;opacity:0;pointer-events:none}
  #banner{position:absolute;top:12px;right:16px;text-align:right;pointer-events:none;text-shadow:0 2px 8px #000}
  #cam{font-size:40px;font-weight:800;letter-spacing:.02em}
  #shot{font-size:20px;font-weight:600;color:#ffd34d;min-height:1.4em}
  #zoom{font-size:22px;font-weight:600}
  #soft{font-size:16px;color:#f0b35a;font-weight:600;min-height:1.4em}
  #mode{font-size:15px;opacity:.85}
  #toast{position:absolute;left:16px;top:12px;font-size:22px;font-weight:600;text-shadow:0 2px 8px #000;opacity:0;transition:opacity .2s;pointer-events:none}
  #shots{display:flex;gap:8px;padding:8px 16px;background:#161616;border-top:1px solid #333;flex-wrap:wrap;align-items:center;min-height:22px}
  .shot{padding:4px 10px;border:1px solid #444;border-radius:6px;background:#222;white-space:nowrap}
  .shot b{font-family:ui-monospace,monospace;margin-right:7px;color:#ffd34d}
  .shot.other{opacity:.45}
  .shot.on{border-color:#ffd34d;background:#3a3010}
  .note{opacity:.6;font-size:14px}
  #seekrow{display:flex;align-items:center;gap:12px;padding:6px 16px;background:#1b1b1b;border-top:1px solid #333}
  #seek{flex:1;accent-color:#ffd34d;height:22px;cursor:pointer;margin:0}
  #dur{font-variant-numeric:tabular-nums;opacity:.7;min-width:7ch;text-align:right}
  #bar{display:flex;gap:18px;align-items:center;padding:10px 16px;background:#1b1b1b;border-top:1px solid #333;flex-wrap:wrap}
  #time{font-variant-numeric:tabular-nums;font-size:22px;font-weight:600;min-width:7ch}
  #speed{font-variant-numeric:tabular-nums;opacity:.8}
  .k{display:inline-block;padding:1px 7px;border:1px solid #555;border-radius:5px;background:#222;font-family:ui-monospace,monospace;margin-right:3px}
  #hint{opacity:.75;flex:1}
  button{background:#2b2b2b;color:#eee;border:1px solid #555;border-radius:6px;padding:8px 14px;font-size:15px;cursor:pointer}
  button:hover{background:#383838}
  #overlay{position:absolute;inset:0;display:flex;align-items:center;justify-content:center;background:rgba(0,0,0,.6);font-size:26px;text-align:center;line-height:1.7;cursor:pointer;z-index:2}
  #overlay.hidden{display:none}
</style>
<div id="wrap">
  <div id="stage">
    <video id="v" src="/zoom_proxy.mp4" preload="auto" playsinline></video>
    <canvas id="c"></canvas>
    <div id="banner"><div id="cam"></div><div id="shot"></div><div id="zoom"></div><div id="soft"></div><div id="mode"></div></div>
    <div id="toast"></div>
    <div id="overlay">Click, then press <b>space</b> to roll<br>
      <small><b>1 2 3</b>… = that key's shot, if it is on this camera &nbsp; <b>0</b> full frame &nbsp; <b>shift</b> + a key = save this framing as that key's shot<br>
      <b>click</b> or <b>drag</b> to aim (<b>shift-drag</b> records a path, to follow someone) &nbsp; <b>a d w s</b> nudge (hold to keep going; shift = big step)<br>
      <b>- +</b> or the <b>mouse wheel</b> = fine zoom &nbsp; <b>v</b> whole frame / result &nbsp; <b>u</b> undo &nbsp; <b>shift-u</b> undo the whole last adjustment<br>
      <span style="opacity:.75">Take your time: changes within a few seconds of each other become one direct move to where you end up.</span></small></div>
  </div>
  <div id="shots"></div>
  <div id="seekrow"><span id="time">0:00</span><input id="seek" type="range" min="0" max="1000" value="0" step="1" title="drag to move through the cut"><span id="dur"></span></div>
  <div id="bar">
    <span id="speed">1.00×</span>
    <span id="hint"><span class="k">1</span>–<span class="k">9</span> shot &nbsp; <span class="k">0</span> full frame &nbsp; <span class="k">⇧</span>+key save shot &nbsp; <span class="k">-</span><span class="k">+</span>/wheel zoom &nbsp; click/drag aim &nbsp; <span class="k">a</span><span class="k">d</span><span class="k">w</span><span class="k">s</span> nudge &nbsp; <span class="k">v</span> view &nbsp; <span class="k">u</span> undo &nbsp; <span class="k">⇧u</span> undo all &nbsp; <span class="k">space</span> pause &nbsp; <span class="k">[</span><span class="k">]</span> speed &nbsp; <span class="k">←</span><span class="k">→</span> 5s</span>
    <button id="done">Done for now</button>
  </div>
</div>
<script>
const v = document.getElementById('v'), cv = document.getElementById('c'), g = cv.getContext('2d');
const camEl = document.getElementById('cam'), shotEl = document.getElementById('shot'), zoomEl = document.getElementById('zoom'), softEl = document.getElementById('soft');
const modeEl = document.getElementById('mode'), toast = document.getElementById('toast'), shotsEl = document.getElementById('shots');
const timeEl = document.getElementById('time'), speedEl = document.getElementById('speed'), overlay = document.getElementById('overlay');
let S = null, tracks = {}, moves = {}, count = 0, nmoves = 0, view = 'frame', toastTimer = null, stripKey = '';
let drag = null;   // a drag in progress

function fmt(t){ t = Math.max(0, t); const h = Math.floor(t/3600), m = Math.floor(t%3600/60), s = t%60;
  return (h ? h + ':' + String(m).padStart(2,'0') : m) + ':' + s.toFixed(2).padStart(5,'0'); }
const esc = s => String(s).replace(/[&<>"]/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));
function say(msg){ toast.textContent = msg; toast.style.opacity = 1; clearTimeout(toastTimer); toastTimer = setTimeout(()=>toast.style.opacity=0, 1600); }
async function post(path, body){ const r = await fetch(path, {method:'POST', headers:{'content-type':'application/json'}, body: JSON.stringify(body||{})}); return r.json(); }
const clamp = (x, a, b) => Math.min(b, Math.max(a, x));
const smooth = s => { s = clamp(s, 0, 1); return s*s*(3-2*s); };
const camName = c => (S && S.cams[c] && S.cams[c].name) ? S.cams[c].name : 'cam ' + c;
const shotText = (k, s) => (s.name || camName(s.cam)) + (s.z > 1.0005 ? ' ' + s.z.toFixed(1) + '×' : '');

// same maths as ffkit/switch.py: each change eases from wherever the picture was at that moment
function valueOf(ms, t){ let val = [1, .5, .5];
  for (const m of ms){ if (t < m.t) break; const s = m.d <= 0 ? 1 : smooth((t - m.t)/m.d);
    val = m.from.map((a, i) => a + (m.to[i] - a)*s); } return val; }
// same rule as settle() in ffkit/switch.py: changes within S.settle seconds of each other on one camera are one
// move to where the last one landed; recorded-path keys (shift-drag) and the shots' own framings (from the marks)
// are kept as they are; a hard change (ease 0) starts a group of its own
function settle(keys){ const gap = S ? S.settle : 0; if (!(gap > 0)) return keys; const out = []; let g = [];
  const flush = () => { if (g.length){ const f = g[0], l = g[g.length-1]; out.push({t: f.t, z: l.z, cx: l.cx, cy: l.cy, e: f.e, s: l.s}); g = []; } };
  for (const k of keys){ if (k.f || k.m){ flush(); out.push(k); } else if (g.length && k.t - g[g.length-1].t <= gap && k.e > 0) g.push(k); else { flush(); g.push(k); } }
  flush(); return out; }
function build(){ moves = {}; count = 0; nmoves = 0;
  for (const cam in tracks){ count += tracks[cam].filter(k => !k.m).length; const ms = []; for (const k of settle(tracks[cam])){ const from = valueOf(ms, k.t);
      ms.push({t:k.t, d:k.e, from, to:[k.z, k.cx, k.cy], s: k.s}); nmoves++; } moves[cam] = ms; } }
// what to draw right now: during a plain drag the box follows the mouse before anything is saved
function liveState(cam, T){ const st = stateAt(cam, T); if (!drag || drag.follow) return st; const h = .5/st.z;
  return {...st, cx: clamp(drag.x, h, 1-h), cy: clamp(drag.y, h, 1-h)}; }
function segAt(T){ for (const s of S.segs){ if (T >= s[0] && T < s[1]) return s; } return S.segs.length ? S.segs[S.segs.length-1] : null; }
function camAt(T){ const s = segAt(T); return s ? s[2] : '1'; }
function stateAt(cam, T){ const [z, cx, cy] = valueOf(moves[cam] || [], T); const zz = Math.max(1, z), h = .5/zz;
  return {z: zz, cx: clamp(cx, h, 1-h), cy: clamp(cy, h, 1-h), rawcx: cx, rawcy: cy}; }
function shotAt(cam, T){ let s = null; for (const m of (moves[cam] || [])){ if (m.t > T) break; s = m.s || null; } return s; }
function showT(){ return S.offset + v.currentTime; }
// A shot (or the full frame) pressed in the first few seconds of a shot is that shot's framing: it applies from
// its first frame, as the fade into it begins, as a cut, instead of easing in late. Same instant as switch.py uses.
function snapTime(T){ const s = segAt(T); if (!s || T - s[0] > S.settle) return null;
  return s[0] <= S.offset + 1e-6 ? s[0] : Math.max(S.offset, Math.round((s[0] - S.tr/2 - 0.05)*1000)/1000); }

function rect(){ // where the video sits inside the canvas
  const W = cv.width, H = cv.height, vw = v.videoWidth || 16, vh = v.videoHeight || 9, k = Math.min(W/vw, H/vh);
  return {x:(W - vw*k)/2, y:(H - vh*k)/2, w:vw*k, h:vh*k}; }
function boxOf(r, z, cx, cy){ const h = .5/z; cx = clamp(cx, h, 1-h); cy = clamp(cy, h, 1-h);
  return {x: r.x + (cx - h)*r.w, y: r.y + (cy - h)*r.h, w: r.w/z, h: r.h/z}; }

function strip(cam, T){
  const on = shotAt(cam, T), key = cam + '|' + on;
  if (key === stripKey) return; stripKey = key;
  const parts = [];
  for (let n = 1; n <= 9; n++){ const k = String(n), s = S.shots[k]; if (!s) continue;
    parts.push('<span class="shot' + (s.cam !== cam ? ' other' : '') + (on === k ? ' on' : '') + '"><b>' + k + '</b>' + esc(shotText(k, s)) + '</span>'); }
  parts.push('<span class="note">' + (parts.length ? 'keys on other cameras are greyed out · ' : '') + 'shift + a digit saves this framing as that key’s shot</span>');
  shotsEl.innerHTML = parts.join(''); }

function draw(){
  requestAnimationFrame(draw);
  const dpr = window.devicePixelRatio || 1, W = Math.round(cv.clientWidth*dpr), H = Math.round(cv.clientHeight*dpr);
  if (cv.width !== W || cv.height !== H){ cv.width = W; cv.height = H; }
  g.fillStyle = '#000'; g.fillRect(0, 0, W, H);
  if (!S || !v.videoWidth) return;
  const T = showT(), cam = camAt(T), st = liveState(cam, T), r = rect(), vw = v.videoWidth, vh = v.videoHeight;
  if (view === 'result'){
    const sw = vw/st.z, sh = vh/st.z, sx = (st.cx - .5/st.z)*vw, sy = (st.cy - .5/st.z)*vh;
    g.drawImage(v, sx, sy, sw, sh, r.x, r.y, r.w, r.h);
  } else {
    g.drawImage(v, 0, 0, vw, vh, r.x, r.y, r.w, r.h);
    for (const [k2, s2] of Object.entries(S.shots)){      // this camera's shots, faintly
      if (s2.cam !== cam || s2.z <= 1.001) continue;
      const b = boxOf(r, s2.z, s2.cx, s2.cy); g.setLineDash([6*dpr, 6*dpr]); g.strokeStyle = 'rgba(255,255,255,.4)'; g.lineWidth = dpr; g.strokeRect(b.x, b.y, b.w, b.h); g.setLineDash([]);
      g.fillStyle = 'rgba(255,255,255,.55)'; g.font = (14*dpr) + 'px system-ui'; g.fillText(k2 + (s2.name ? ' ' + s2.name : ''), b.x + 6*dpr, b.y + 16*dpr); }
    if (st.z > 1.001){
      const fx = r.x + (st.cx - .5/st.z)*r.w, fy = r.y + (st.cy - .5/st.z)*r.h, fw = r.w/st.z, fh = r.h/st.z;
      g.fillStyle = 'rgba(0,0,0,.5)';
      g.fillRect(r.x, r.y, r.w, fy - r.y); g.fillRect(r.x, fy + fh, r.w, r.y + r.h - fy - fh);
      g.fillRect(r.x, fy, fx - r.x, fh); g.fillRect(fx + fw, fy, r.x + r.w - fx - fw, fh);
      g.strokeStyle = '#ffd34d'; g.lineWidth = 2*dpr; g.strokeRect(fx, fy, fw, fh);
    }
  }
  const c = S.cams[cam] || {};
  camEl.textContent = 'CAM ' + cam + (c.name ? '  ' + c.name : '');
  const on = shotAt(cam, T), os = on && S.shots[on];
  shotEl.textContent = os ? 'shot ' + on + (os.name ? '  ' + os.name : '') : '';
  zoomEl.textContent = st.z.toFixed(2) + '×   ·   ' + count + ' changes → ' + nmoves + ' moves';
  const sharp = c.h ? c.h / S.out_h : 1;
  softEl.textContent = st.z > sharp + 0.02 ? (sharp <= 1.001 ? 'soft: this camera is only ' + c.h + 'p' : 'soft past ' + sharp.toFixed(1) + '× on this camera') : '';
  modeEl.textContent = view === 'result' ? 'showing: the result (v = whole frame)' : 'showing: whole frame (v = result)';
  timeEl.textContent = fmt(T);
  strip(cam, T);
}

// Where the picture is heading: the destination of the latest change (which is also where it is,
// once that move has finished). Nudges build on this, so pressing twice moves twice even while paused.
function targetAt(cam, T){ let to = [1, .5, .5]; for (const m of (moves[cam] || [])){ if (m.t > T) break; to = m.to; }
  const z = Math.max(1, to[0]), h = .5/z; return {z, cx: clamp(to[1], h, 1-h), cy: clamp(to[2], h, 1-h), rawcx: to[1], rawcy: to[2]}; }
// opts.at: write the key at that instant instead of now; opts.shot: it is that key's shot;
// opts.replace: the trial-and-error changes just made on this camera become this one key
async function key(fields, ease, label, follow, opts){
  opts = opts || {};
  const T = showT(), cam = camAt(T), tg = targetAt(cam, T), at = (opts.at === null || opts.at === undefined) ? T : opts.at;
  const k = {t: at, now: T, cam, z: tg.z, cx: tg.rawcx, cy: tg.rawcy, e: ease, follow: !!follow, p: opts.shot || null, replace: !!opts.replace, ...fields};
  k.cx = clamp(k.cx, 0, 1); k.cy = clamp(k.cy, 0, 1);
  const r = await post('/zkey', k); tracks = r.tracks; build();
  if (label) say(label + '  cam ' + cam + '  @ ' + fmt(at));
}
async function saveShot(n, cam, T){
  const tg = targetAt(cam, T), old = S.shots[n];
  const def = (old && old.cam === cam && old.name) || (camName(cam) + (tg.z > 1.0005 ? ' ' + tg.z.toFixed(1) + '×' : ' wide'));
  const name = window.prompt('Name for key ' + n + ' (shows on the strip):', def);
  if (name === null) return;
  const r = await post('/zshot', {key: n, cam, z: tg.z, cx: tg.rawcx, cy: tg.rawcy, name: name.trim()});
  S.shots = r.shots || S.shots; if (r.tracks){ tracks = r.tracks; build(); } stripKey = '';
  const at = snapTime(T);
  await key({z: tg.z, cx: tg.rawcx, cy: tg.rawcy}, at === null ? S.ease : 0, 'key ' + n + ' = ' + (name.trim() || def), false, {at, shot: n, replace: true});
}
function pointAt(e){ const b = cv.getBoundingClientRect(), dpr = window.devicePixelRatio || 1, r = rect();
  let nx = ((e.clientX - b.left)*dpr - r.x)/r.w, ny = ((e.clientY - b.top)*dpr - r.y)/r.h;
  if (view === 'result'){ const T = showT(), st = stateAt(camAt(T), T);
    nx = st.cx - .5/st.z + nx/st.z; ny = st.cy - .5/st.z + ny/st.z; }
  return [clamp(nx, 0, 1), clamp(ny, 0, 1)]; }

fetch('/zstate').then(r => r.json()).then(s => { S = s; S.shots = s.shots || {}; S.tr = s.tr || 0; tracks = s.tracks; build();
  const go = () => { v.currentTime = Math.max(0, s.start - s.offset); };
  if (v.readyState >= 1) go(); else v.addEventListener('loadedmetadata', go, {once:true});
  requestAnimationFrame(draw); });

overlay.addEventListener('click', () => { overlay.classList.add('hidden'); v.play(); });
// the slider: drag to move through the cut (rough while dragging, exact when you let go)
const seek = document.getElementById('seek'), durEl = document.getElementById('dur');
let scrubbing = false;
const seekTo = (t, exact) => { if (!exact && v.fastSeek) v.fastSeek(t); else v.currentTime = t; };
v.addEventListener('loadedmetadata', () => { seek.max = Math.floor(v.duration*10); durEl.textContent = fmt((S ? S.offset : 0) + v.duration); });
seek.addEventListener('pointerdown', () => { scrubbing = true; });
seek.addEventListener('input', () => { scrubbing = true; seekTo(seek.value/10, false); });
seek.addEventListener('change', () => { seekTo(seek.value/10, true); scrubbing = false; seek.blur(); });
seek.addEventListener('pointerup', () => { scrubbing = false; seek.blur(); });
setInterval(() => { if (!scrubbing && v.duration) seek.value = Math.round(v.currentTime*10); }, 100);
// Plain click/drag: aim, saved once when you let go (in the result view, dragging slides the picture).
// Shift-drag: a recorded path, saved as you go, for following someone across the stage.
function mousePix(e){ const b = cv.getBoundingClientRect(), dpr = window.devicePixelRatio || 1; return [(e.clientX - b.left)*dpr, (e.clientY - b.top)*dpr]; }
cv.addEventListener('mousedown', e => { if (!S) return; e.preventDefault();
  const T = showT(), st = stateAt(camAt(T), T), [px, py] = mousePix(e);
  drag = {follow: e.shiftKey, last: 0, px, py, cx0: st.cx, cy0: st.cy, z: st.z, r: rect(), x: st.cx, y: st.cy, moved: false};
  if (view === 'frame'){ [drag.x, drag.y] = pointAt(e); drag.moved = true; }
  if (drag.follow) key({cx: drag.x, cy: drag.y}, 0.5, 'following', true); });
cv.addEventListener('mousemove', e => { if (!drag) return; drag.moved = true;
  if (view === 'frame'){ [drag.x, drag.y] = pointAt(e); }
  else { const [px, py] = mousePix(e); drag.x = clamp(drag.cx0 - (px - drag.px)/drag.r.w/drag.z, 0, 1); drag.y = clamp(drag.cy0 - (py - drag.py)/drag.r.h/drag.z, 0, 1); }
  if (drag.follow){ const now = performance.now(); if (now - drag.last >= 250){ drag.last = now; key({cx: drag.x, cy: drag.y}, 0.35, null, true); } } });
window.addEventListener('mouseup', () => { if (!drag) return; const d = drag; drag = null; if (!d.moved) return;
  key({cx: d.x, cy: d.y}, d.follow ? 0.35 : 0.6, d.follow ? 'path done' : 'aim', d.follow); });
// mouse wheel / trackpad over the picture: fine zoom, batched so a flick is one smooth move
let wheelAcc = 0, wheelTimer = null;
cv.addEventListener('wheel', e => { e.preventDefault(); if (!S) return;
  wheelAcc += -e.deltaY * 0.0008;                       // one mouse-wheel notch (~100) = 0.08x
  if (wheelTimer) return;
  wheelTimer = setTimeout(async () => { wheelTimer = null; const d = clamp(wheelAcc, -1, 1); wheelAcc = 0;
    const T = showT(), tg = targetAt(camAt(T), T), z = Math.round(clamp(tg.z + d, 1, ZMAX) * 100) / 100;
    if (Math.abs(z - tg.z) >= 0.005) await key({z}, 0.4, z.toFixed(2) + '×'); }, 200);
}, {passive: false});
v.addEventListener('ended', () => say('end of what is marked so far'));

// a/d/w/s nudge by 8% of the view (shift: 25%); - and + change zoom by 0.1x (shift: 0.5x).
// Holding a key keeps going, one small move every 180 ms, each starting from where the picture is.
const NUDGE = 0.08, NUDGE_BIG = 0.25, ZSTEP = 0.1, ZSTEP_BIG = 0.5, ZMAX = 4;
let lastNudge = 0;
document.addEventListener('keydown', async (e) => {
  if (!S) return;
  const k = (e.key || '').toLowerCase();
  const nudgeKey = k.length === 1 && 'adws'.includes(k), zoomKey = k === '-' || k === '_' || k === '=' || k === '+';
  if (e.repeat && !(nudgeKey || zoomKey)) return;
  if (e.repeat){ e.preventDefault(); if (performance.now() - lastNudge < 180) return; }
  const T = showT(), cam = camAt(T), tg = targetAt(cam, T);
  if (e.code && e.code.startsWith('Digit')){ e.preventDefault(); const n = e.code.slice(5);
    if (n === '0'){ const at = snapTime(T); await key({z: 1, cx: .5, cy: .5}, at === null ? S.ease : 0, 'full frame' + (at === null ? '' : ' from the start of the shot'), false, {at}); }
    else if (e.shiftKey){ await saveShot(n, cam, T); }
    else { const s = S.shots[n];
      if (s && s.cam === cam){ const at = snapTime(T);
        await key({z: s.z, cx: s.cx, cy: s.cy}, at === null ? S.ease : 0, 'shot ' + n + (s.name ? ' ' + s.name : '') + (at === null ? '' : ' from the start of the shot'), false, {at, shot: n}); }
      else if (s) say('key ' + n + ' is a shot on ' + camName(s.cam) + '; this is ' + camName(cam));
      else { const i = parseInt(n) - 1;
        if (i === 0){ const at = snapTime(T); await key({z: 1, cx: .5, cy: .5}, at === null ? S.ease : 0, 'full frame (no shot on 1 yet)', false, {at}); }
        else if (i < S.levels.length){ const z = S.levels[i]; await key({z}, S.ease, z + '×  (no shot on ' + n + ' yet: shift-' + n + ' saves one)'); }
        else say('no shot on key ' + n + ' yet: frame it, then shift-' + n); } } }
  else if (nudgeKey){ e.preventDefault(); lastNudge = performance.now();
    const step = (e.shiftKey ? NUDGE_BIG : NUDGE) / tg.z;
    if (k === 'a' || k === 'd') await key({cx: tg.cx + (k === 'a' ? -step : step)}, 0.5, e.repeat ? null : (k === 'a' ? 'nudge left' : 'nudge right'));
    else await key({cy: tg.cy + (k === 'w' ? -step : step)}, 0.5, e.repeat ? null : (k === 'w' ? 'nudge up' : 'nudge down')); }
  else if (zoomKey){ e.preventDefault(); lastNudge = performance.now();
    const d = (e.shiftKey ? ZSTEP_BIG : ZSTEP) * ((k === '-' || k === '_') ? -1 : 1);
    const z = Math.round(clamp(tg.z + d, 1, ZMAX) * 100) / 100;
    if (Math.abs(z - tg.z) >= 0.005) await key({z}, 0.5, e.repeat ? null : z.toFixed(2) + '×'); }
  else if (e.key === ' '){ e.preventDefault(); overlay.classList.add('hidden'); if (v.paused) v.play(); else v.pause(); }
  else if (e.key === 'v'){ view = view === 'frame' ? 'result' : 'frame'; }
  else if (e.key === 'u'){ e.preventDefault(); const r = await post('/zundo'); tracks = r.tracks; build(); say(r.removed ? 'undid that' : 'nothing to undo'); }
  else if (e.key === 'U'){ e.preventDefault(); const r = await post('/zundo', {burst: true}); tracks = r.tracks; build();
    say(r.count > 1 ? 'undid the last ' + r.count + ' changes' : r.removed ? 'undid that' : 'nothing to undo'); }
  else if (e.key === '['){ v.playbackRate = Math.max(0.25, Math.round((v.playbackRate - 0.25)*100)/100); speedEl.textContent = v.playbackRate.toFixed(2) + '×'; }
  else if (e.key === ']'){ v.playbackRate = Math.min(3, Math.round((v.playbackRate + 0.25)*100)/100); speedEl.textContent = v.playbackRate.toFixed(2) + '×'; }
  else if (e.key === 'ArrowLeft'){ e.preventDefault(); v.currentTime = Math.max(0, v.currentTime - 5); }
  else if (e.key === 'ArrowRight'){ e.preventDefault(); v.currentTime = v.currentTime + 5; }
  else if (e.key === 'Backspace'){ v.playbackRate = 1; speedEl.textContent = '1.00×'; }
});
document.getElementById('done').addEventListener('click', async () => { v.pause(); await post('/quit');
  document.body.innerHTML = '<div style="padding:40px;font-size:22px">Saved. You can close this tab.<br><small style="opacity:.7">ffkit switch preview shows the zoom; run ffkit switch zoom again to carry on.</small></div>'; });
window.addEventListener('beforeunload', () => { navigator.sendBeacon('/quit'); });
</script>
"""


def _tracks_json(tracks: dict) -> dict:
    return {cam: [{"t": k.t, "z": k.z, "cx": k.cx, "cy": k.cy, "e": k.ease, "f": k.follow, "m": k.fixed, "s": k.shot}
                  for k in keys] for cam, keys in tracks.items()}


def _pop_burst(log: Path, cam: str, now: float, gap: float) -> int:
    """Drop the trailing changes on `cam` made within `gap` seconds of `now` (and of each other):
    the trial and error that found a framing, once that framing is saved as one key."""
    lines = log.read_text().splitlines() if log.exists() else []
    n = 0
    prev_t = now
    while lines:
        ln = lines[-1]
        if not ln.strip() or ln.startswith("#"):
            break
        p = ln.split(",")
        try:
            t, c = float(p[0]), p[1].strip()
        except (IndexError, ValueError):
            break
        if c != cam or abs(prev_t - t) > gap:
            break
        lines.pop()
        n += 1
        prev_t = t
    if n:
        log.write_text("\n".join(lines) + ("\n" if lines else ""))
    return n


def serve_zoom(proxy: Path, log: Path, state: dict, open_browser: bool = True, port: int = 0,
               tracks_fn=None, shots_path: Path | None = None) -> None:
    from .switch import load_shots, parse_zoom, save_shots
    quit_evt = threading.Event()
    lock = threading.Lock()

    def shots() -> dict:
        return load_shots(shots_path) if shots_path else {}

    def tracks() -> dict:
        return tracks_fn() if tracks_fn else parse_zoom(log, shots())

    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def _json(self, obj, status=200):
            data = json.dumps(obj).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self):
            path = urlparse(self.path).path
            if path == "/":
                data = ZOOM_PAGE.encode()
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)
            elif path == "/zoom_proxy.mp4":
                _send_file(self, proxy, "video/mp4")
            elif path == "/zstate":
                self._json(dict(state, tracks=_tracks_json(tracks()), shots=shots()))
            else:
                self.send_error(404)

        def do_POST(self):
            path = urlparse(self.path).path
            n = int(self.headers.get("Content-Length") or 0)
            try:
                body = json.loads(self.rfile.read(n) or b"{}") if n else {}
            except json.JSONDecodeError:
                body = {}
            with lock:
                if path == "/zkey":
                    cam = str(body.get("cam", "")).strip()
                    shot = str(body.get("p") or "").strip()
                    try:
                        t = float(body["t"])
                        line = (f"{t:.3f},{cam},{float(body['z']):.4f},"
                                f"{float(body['cx']):.4f},{float(body['cy']):.4f},{float(body['e']):.3f}"
                                + (",f" if body.get("follow") else "") + (f",p{shot}" if shot.isdigit() else "") + "\n")
                    except (KeyError, TypeError, ValueError):
                        line = None
                    if line and cam.isdigit():
                        if body.get("replace"):
                            try:
                                now = float(body.get("now", t))
                            except (TypeError, ValueError):
                                now = t
                            _pop_burst(log, cam, now, float(state.get("settle") or 0.0))
                        with log.open("a") as fh:
                            fh.write(line)
                    self._json({"tracks": _tracks_json(tracks())})
                elif path == "/zshot":
                    # save this framing as a key's shot (camera + zoom + where it points)
                    key = str(body.get("key", "")).strip()
                    cur = shots()
                    if key.isdigit() and shots_path is not None:
                        try:
                            cam = str(body["cam"]).strip()
                            if cam in (state.get("cams") or {cam: 1}):
                                cur[key] = {"cam": cam, "z": max(1.0, float(body.get("z", 1.0))),
                                            "cx": min(1.0, max(0.0, float(body.get("cx", 0.5)))),
                                            "cy": min(1.0, max(0.0, float(body.get("cy", 0.5)))),
                                            "name": str(body.get("name") or "").strip()[:40]}
                                save_shots(shots_path, cur)
                        except (KeyError, TypeError, ValueError):
                            pass
                    self._json({"shots": shots(), "tracks": _tracks_json(tracks())})
                elif path == "/zundo":
                    # one change, or with {"burst": true} the whole last adjustment: the trailing
                    # changes on the same camera that fall within `settle` seconds of each other
                    lines = log.read_text().splitlines() if log.exists() else []
                    burst = bool(body.get("burst"))
                    gap = float(state.get("settle") or 0.0)
                    removed: list = []
                    prev_cam = prev_t = None
                    i = len(lines) - 1
                    while i >= 0:
                        ln = lines[i]
                        if not ln or ln.startswith("#"):
                            i -= 1
                            continue
                        if burst:
                            p = ln.split(",")
                            try:
                                t, cam = float(p[0]), p[1].strip()
                            except (IndexError, ValueError):
                                break
                            if prev_cam is not None and (cam != prev_cam or abs(prev_t - t) > gap):
                                break
                            prev_cam, prev_t = cam, t
                        removed.append(lines.pop(i))
                        i -= 1
                        if not burst:
                            break
                    if removed:
                        log.write_text("\n".join(lines) + ("\n" if lines else ""))
                    self._json({"tracks": _tracks_json(tracks()), "removed": removed[0] if removed else None,
                                "count": len(removed)})
                elif path == "/quit":
                    self._json({"ok": True})
                    quit_evt.set()
                else:
                    self.send_error(404)

    _run_server(H, open_browser, port, quit_evt, "zoom")
