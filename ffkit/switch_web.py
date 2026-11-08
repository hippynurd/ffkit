"""
Browser-based marking for `ffkit switch mark`, used when mpv isn't installed (or with --web).

A tiny local web server serves the grid proxy to your normal browser with range requests
(so seeking works) and receives each key press as a mark. Nothing to install, no sudo.
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
  video{flex:1;min-height:0;width:100%;background:#000;object-fit:contain;outline:none}
  #banner{position:absolute;top:12px;right:16px;text-align:right;pointer-events:none;text-shadow:0 2px 8px #000}
  #cam{font-size:46px;font-weight:800;letter-spacing:.02em}
  #cam.none{font-size:26px;font-weight:500;opacity:.8}
  #count{font-size:18px;opacity:.85}
  #toast{position:absolute;left:16px;top:12px;font-size:22px;font-weight:600;text-shadow:0 2px 8px #000;opacity:0;transition:opacity .2s}
  #bar{display:flex;gap:18px;align-items:center;padding:10px 16px;background:#1b1b1b;border-top:1px solid #333;flex-wrap:wrap}
  #time{font-variant-numeric:tabular-nums;font-size:22px;font-weight:600;min-width:7ch}
  #speed{font-variant-numeric:tabular-nums;opacity:.8}
  .k{display:inline-block;padding:1px 7px;border:1px solid #555;border-radius:5px;background:#222;font-family:ui-monospace,monospace;margin-right:3px}
  #hint{opacity:.75;flex:1}
  button{background:#2b2b2b;color:#eee;border:1px solid #555;border-radius:6px;padding:8px 14px;font-size:15px;cursor:pointer}
  button:hover{background:#383838}
  #overlay{position:absolute;inset:0;display:flex;align-items:center;justify-content:center;background:rgba(0,0,0,.55);font-size:28px;text-align:center;line-height:1.6;cursor:pointer}
  #overlay.hidden{display:none}
</style>
<div id="wrap">
  <video id="v" src="/grid.mp4" preload="auto" playsinline></video>
  <div id="banner"><div id="cam" class="none">no camera yet</div><div id="count"></div></div>
  <div id="toast"></div>
  <div id="overlay">Click, then press <b>space</b> to roll<br><small>1 2 3 = camera &nbsp; u = undo &nbsp; s / e = show start / end &nbsp; [ ] = speed &nbsp; ← → = 5s</small></div>
  <div id="bar">
    <span id="time">0:00</span>
    <span id="speed">1.00×</span>
    <span id="hint"><span class="k">1</span><span class="k">2</span><span class="k">3</span> camera &nbsp; <span class="k">space</span> pause &nbsp; <span class="k">u</span> undo &nbsp; <span class="k">s</span><span class="k">e</span> show start/end &nbsp; <span class="k">[</span><span class="k">]</span> speed &nbsp; <span class="k">←</span><span class="k">→</span> 5s</span>
    <button id="done">Done for now</button>
  </div>
</div>
<script>
const v = document.getElementById('v'), camEl = document.getElementById('cam'), countEl = document.getElementById('count');
const toast = document.getElementById('toast'), timeEl = document.getElementById('time'), speedEl = document.getElementById('speed');
const overlay = document.getElementById('overlay');
let names = {}, current = null, count = 0, toastTimer = null;

function fmt(t){ t = Math.max(0, t); const h = Math.floor(t/3600), m = Math.floor(t%3600/60), s = t%60;
  return (h ? h + ':' + String(m).padStart(2,'0') : m) + ':' + s.toFixed(2).padStart(5,'0'); }
function banner(){ if (current){ camEl.className=''; camEl.textContent = 'CAM ' + current + (names[current] ? '  ' + names[current] : ''); }
  else { camEl.className='none'; camEl.textContent = 'no camera yet'; } countEl.textContent = count + ' marks'; }
function say(msg){ toast.textContent = msg; toast.style.opacity = 1; clearTimeout(toastTimer); toastTimer = setTimeout(()=>toast.style.opacity=0, 1400); }
async function post(path, body){ const r = await fetch(path, {method:'POST', headers:{'content-type':'application/json'}, body: JSON.stringify(body||{})}); return r.json(); }

fetch('/state').then(r=>r.json()).then(s => { names = s.names; current = s.current; count = s.count; banner();
  const go = () => { v.currentTime = s.start; }; if (v.readyState >= 1) go(); else v.addEventListener('loadedmetadata', go, {once:true}); });

overlay.addEventListener('click', () => { overlay.classList.add('hidden'); v.play(); });
v.addEventListener('timeupdate', () => { timeEl.textContent = fmt(v.currentTime); });
setInterval(() => { timeEl.textContent = fmt(v.currentTime); }, 100);

document.addEventListener('keydown', async (e) => {
  if (e.repeat) return;
  const k = e.key;
  if (k >= '1' && k <= '9') { e.preventDefault(); const t = v.currentTime; const r = await post('/mark', {t, cam: k}); current = r.current; count = r.count; banner(); say('cam ' + k + '  @ ' + fmt(t)); }
  else if (k === ' ') { e.preventDefault(); overlay.classList.add('hidden'); if (v.paused) v.play(); else v.pause(); }
  else if (k === 'u') { e.preventDefault(); const r = await post('/undo'); current = r.current; count = r.count; banner(); say(r.removed ? 'undid ' + r.removed : 'nothing to undo'); }
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
    def __init__(self, grid: Path, log: Path, names: dict, start: float, current, count: int):
        self.grid, self.log, self.names, self.start = grid, log, names, start
        self.current, self.count = current, count
        self.lock = threading.Lock()
        self.quit = threading.Event()


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
                self._send_file(ctx.grid, "video/mp4")
            elif path == "/state":
                self._json({"start": ctx.start, "names": ctx.names, "current": ctx.current, "count": ctx.count})
            else:
                self.send_error(404)

        def _send_file(self, p: Path, ctype: str):
            size = p.stat().st_size
            start, end, status = 0, size - 1, 200
            rng = self.headers.get("Range")
            if rng and rng.startswith("bytes="):
                s, _, e = rng[6:].partition("-")
                if s:
                    start = int(s)
                    end = int(e) if e else size - 1
                elif e:
                    start = max(0, size - int(e))
                end = min(end, size - 1)
                if start > end:
                    self.send_response(416)
                    self.send_header("Content-Range", f"bytes */{size}")
                    self.end_headers()
                    return
                status = 206
            length = end - start + 1
            self.send_response(status)
            self.send_header("Content-Type", ctype)
            self.send_header("Accept-Ranges", "bytes")
            self.send_header("Content-Length", str(length))
            if status == 206:
                self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
            self.end_headers()
            with p.open("rb") as fh:
                fh.seek(start)
                left = length
                while left > 0:
                    chunk = fh.read(min(1 << 20, left))
                    if not chunk:
                        break
                    try:
                        self.wfile.write(chunk)
                    except (BrokenPipeError, ConnectionResetError):
                        return
                    left -= len(chunk)

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
                    t = float(body.get("t", 0.0))
                    cam = str(body.get("cam", "")).strip()
                    if cam.isdigit():
                        with ctx.log.open("a") as fh:
                            fh.write(f"{t:.3f},{cam}\n")
                        ctx.current = cam
                        ctx.count += 1
                    self._json({"current": ctx.current, "count": ctx.count})
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
                        head, _, tail = lines[i].partition(",")
                        if head and head[0].isdigit() and tail.strip().isdigit():
                            removed = lines[i]
                            del lines[i]
                            break
                    if removed:
                        ctx.log.write_text("\n".join(lines) + ("\n" if lines else ""))
                        ctx.count = max(0, ctx.count - 1)
                        ctx.current = None
                        for ln in reversed(lines):
                            head, _, tail = ln.partition(",")
                            if head and head[0].isdigit() and tail.strip().isdigit():
                                ctx.current = tail.strip()
                                break
                    self._json({"current": ctx.current, "count": ctx.count, "removed": removed})
                elif path == "/quit":
                    self._json({"ok": True})
                    ctx.quit.set()
                else:
                    self.send_error(404)
    return H


def serve(grid: Path, log: Path, names: dict, start: float, current, count: int,
          open_browser: bool = True, port: int = 0) -> None:
    ctx = _Ctx(grid, log, names, start, current, count)
    httpd = ThreadingHTTPServer(("127.0.0.1", port), _make_handler(ctx))
    httpd.daemon_threads = True
    url = f"http://127.0.0.1:{httpd.server_address[1]}/"
    th = threading.Thread(target=httpd.serve_forever, daemon=True)
    th.start()
    print(f"\nmarking page: {url}\n(if it didn't open by itself, open that address in any browser; "
          f"click Done or press Ctrl+C here when you stop)\n")
    if open_browser:
        try:
            webbrowser.open(url)
        except Exception:
            pass
    try:
        while not ctx.quit.wait(0.5):
            pass
        time.sleep(0.3)
    except KeyboardInterrupt:
        pass
    finally:
        httpd.shutdown()
        httpd.server_close()
