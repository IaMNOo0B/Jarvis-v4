"""JARVIS app UI — the face from the owner's reference photo.

A local web app (stdlib http.server, bound to 127.0.0.1 only) over
the SAME ChatApp core the CLI uses — no second brain, no drift: the
chat he perfects here is the chat everywhere. The design follows his
photo: dark grid, one glowing ring with JARVIS in the middle, state
words under the ring (Listening / Thinking / Researching /
Processing), minimal chrome. Voice arrives later by his order; this
is chat-first, fully working.

    python -m jarvis.ui [--data-dir DIR] [--port 8765] [--no-open]
"""

from __future__ import annotations

import argparse
import json
import sys
import threading
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from .chat import (DEFAULT_DATA_DIR, ChatApp, _due_reminder_notifications,
                   _mark_notifications_read, _maybe_auto_watch,
                   _read_notifications, _unread_count)

PAGE = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>JARVIS</title>
<style>
  * { margin:0; padding:0; box-sizing:border-box; }
  html, body { height:100%; }
  body {
    background:#04070d;
    background-image:
      radial-gradient(ellipse 90% 60% at 50% 38%, rgba(24,80,140,.28), transparent 60%),
      linear-gradient(rgba(90,160,230,.055) 1px, transparent 1px),
      linear-gradient(90deg, rgba(90,160,230,.055) 1px, transparent 1px);
    background-size:100% 100%, 44px 44px, 44px 44px;
    color:#cfe7ff; font-family:"Segoe UI", system-ui, sans-serif;
    display:flex; flex-direction:column; align-items:center;
    height:100vh; overflow:hidden;
  }
  .stage { flex:0 0 auto; display:flex; flex-direction:column;
           align-items:center; padding-top:4.5vh; }
  .ring-wrap { position:relative; width:212px; height:212px; }
  .ring-glow { position:absolute; inset:-34px; border-radius:50%;
    background:radial-gradient(circle, rgba(56,170,255,.30) 0%, rgba(56,170,255,.06) 55%, transparent 70%);
    animation:breathe 4.5s ease-in-out infinite; }
  @keyframes breathe { 0%,100% { opacity:.75; transform:scale(1);} 50% { opacity:1; transform:scale(1.05);} }
  svg.ring { position:absolute; inset:0; transform:rotate(-90deg); }
  .ring-track { fill:none; stroke:rgba(110,190,255,.16); stroke-width:2; }
  .ring-arc { fill:none; stroke:#57c4ff; stroke-width:2.5; stroke-linecap:round;
    stroke-dasharray:120 560; animation:spin 7s linear infinite;
    filter:drop-shadow(0 0 6px rgba(87,196,255,.9)); }
  @keyframes spin { to { transform:rotate(360deg); } }
  body.busy .ring-arc { animation-duration:1.4s; }
  body.busy .ring-glow { animation-duration:1.2s; }
  .ring-core { position:absolute; inset:26px; border-radius:50%;
    border:1px solid rgba(120,200,255,.35);
    background:radial-gradient(circle at 50% 42%, rgba(20,50,90,.55), rgba(3,8,16,.9) 75%);
    box-shadow:0 0 34px rgba(56,170,255,.35) inset, 0 0 22px rgba(56,170,255,.25);
    display:flex; align-items:center; justify-content:center; }
  .ring-core span { font-size:25px; letter-spacing:7px; font-weight:300;
    color:#eaf6ff; text-indent:7px; text-shadow:0 0 14px rgba(120,210,255,.8); }
  .ticks { position:absolute; inset:6px; border-radius:50%;
    border:1px dashed rgba(120,200,255,.14); animation:spin 60s linear infinite reverse; }
  #state { margin-top:18px; font-size:12.5px; letter-spacing:4.5px;
    text-transform:uppercase; color:#8fd2ff; min-height:16px;
    text-shadow:0 0 10px rgba(87,196,255,.55); }
  #provider { margin-top:5px; font-size:11px; letter-spacing:1.6px;
    color:rgba(160,200,240,.45); min-height:14px; }
  .chat { flex:1 1 auto; width:min(760px, 94vw); display:flex;
    flex-direction:column; min-height:0; margin-top:2.2vh; }
  #log { flex:1 1 auto; overflow-y:auto; padding:6px 4px 12px;
    display:flex; flex-direction:column; gap:10px; scrollbar-width:thin; }
  .msg { max-width:82%; padding:10px 14px; border-radius:12px;
    font-size:14.5px; line-height:1.5; white-space:pre-wrap;
    word-wrap:break-word; }
  .msg.user { align-self:flex-end; color:#e8f4ff;
    background:linear-gradient(160deg, rgba(30,90,160,.5), rgba(16,50,95,.5));
    border:1px solid rgba(110,190,255,.35); border-bottom-right-radius:3px; }
  .msg.jarvis { align-self:flex-start; color:#d7ecff;
    background:rgba(10,22,42,.62); border:1px solid rgba(110,190,255,.16);
    border-bottom-left-radius:3px; }
  .bar { flex:0 0 auto; display:flex; gap:10px; padding:12px 2px 18px; }
  #text { flex:1; background:rgba(8,18,36,.75); color:#eaf6ff;
    border:1px solid rgba(110,190,255,.3); border-radius:10px;
    padding:12px 14px; font-size:15px; outline:none; }
  #text:focus { border-color:rgba(120,205,255,.75);
    box-shadow:0 0 14px rgba(56,170,255,.25); }
  #send { background:rgba(16,60,110,.6); color:#bfe4ff;
    border:1px solid rgba(110,190,255,.4); border-radius:10px;
    padding:0 22px; font-size:13px; letter-spacing:3px; cursor:pointer; }
  #send:hover { background:rgba(30,100,175,.65); color:#fff; }
  .icons { flex:0 0 auto; display:flex; gap:26px; padding-bottom:14px;
    color:rgba(150,200,245,.4); font-size:13px; letter-spacing:2px; }
  #bell { cursor:pointer; }
  #bell.hot { color:#ffd479; text-shadow:0 0 12px rgba(255,200,90,.8); }
  #npanel { display:none; width:min(760px, 94vw); max-height:30vh;
    overflow-y:auto; margin-bottom:8px; padding:10px 14px;
    background:rgba(10,22,42,.85); border:1px solid rgba(255,205,110,.35);
    border-radius:10px; font-size:13.5px; }
  #npanel .nitem { padding:7px 2px; border-bottom:1px solid rgba(120,200,255,.1); }
  #npanel .nitem:last-child { border-bottom:none; }
  #npanel .ntitle { color:#ffe1a1; }
  #npanel .nempty { color:rgba(180,210,245,.55); }
</style>
</head>
<body>
  <div class="stage">
    <div class="ring-wrap">
      <div class="ring-glow"></div>
      <div class="ticks"></div>
      <svg class="ring" viewBox="0 0 200 200">
        <circle class="ring-track" cx="100" cy="100" r="96"></circle>
        <circle class="ring-arc" cx="100" cy="100" r="96"></circle>
      </svg>
      <div class="ring-core"><span>JARVIS</span></div>
    </div>
    <div id="state">Ready</div>
    <div id="provider"></div>
  </div>
  <div class="chat">
    <div id="log"></div>
    <div class="bar">
      <input id="text" placeholder="Type here, sir…" autocomplete="off">
      <button id="send">SEND</button>
    </div>
  </div>
  <div id="npanel"></div>
  <div class="icons"><span>◉ CHAT</span><span>▣ TOOLS</span><span>◎ MEMORY</span><span id="bell">🔔 <span id="ncount">0</span></span><span>♪ VOICE · SOON</span></div>
<script>
  const log = document.getElementById('log');
  const input = document.getElementById('text');
  const stateEl = document.getElementById('state');
  const providerEl = document.getElementById('provider');
  const busyWords = ['Listening', 'Thinking', 'Researching', 'Processing', 'Creating'];
  let timer = null;

  function addMsg(text, who) {
    const div = document.createElement('div');
    div.className = 'msg ' + who;
    div.textContent = text;
    log.appendChild(div);
    log.scrollTop = log.scrollHeight;
  }
  function setBusy(on) {
    document.body.classList.toggle('busy', on);
    clearInterval(timer);
    if (on) {
      let i = 0;
      stateEl.textContent = busyWords[0] + '…';
      timer = setInterval(() => { i = (i + 1) % busyWords.length;
        stateEl.textContent = busyWords[i] + '…'; }, 1700);
    } else {
      stateEl.textContent = 'Ready';
    }
  }
  const bell = document.getElementById('bell');
  const ncount = document.getElementById('ncount');
  const npanel = document.getElementById('npanel');
  function setBadge(n) {
    ncount.textContent = n;
    bell.classList.toggle('hot', n > 0);
  }
  async function refreshState() {
    try {
      const r = await fetch('/api/state');
      const s = await r.json();
      providerEl.textContent = s.provider ? ('brain · ' + s.provider + ' · tools ' + s.tools + ' · learned ' + s.learned) : '';
      setBadge(s.notifications || 0);
    } catch (e) { /* state line is decoration; never break chat */ }
  }
  bell.addEventListener('click', async () => {
    if (npanel.style.display === 'block') { npanel.style.display = 'none'; return; }
    try {
      const r = await fetch('/api/notifications');
      const s = await r.json();
      const esc = t => { const d = document.createElement('div');
        d.textContent = t || ''; return d.innerHTML; };
      npanel.innerHTML = s.items.length
        ? s.items.map(n => '<div class="nitem"><span class="ntitle">' +
            esc(n.title) + '</span> — ' + esc(n.body) + '</div>').join('')
        : '<div class="nempty">No notifications, sir.</div>';
      npanel.style.display = 'block';
      await fetch('/api/notifications/read', { method: 'POST' });
      setBadge(0);
    } catch (e) { /* panel is decoration; never break chat */ }
  });
  setInterval(refreshState, 20000);
  async function send() {
    const text = input.value.trim();
    if (!text) return;
    input.value = '';
    addMsg(text, 'user');
    setBusy(true);
    try {
      const r = await fetch('/api/chat', { method:'POST',
        headers:{'Content-Type':'application/json'},
        body: JSON.stringify({ text }) });
      const data = await r.json();
      addMsg(data.reply || data.error || '…', 'jarvis');
      if (data.provider) providerEl.textContent = 'brain · ' + data.provider;
    } catch (e) {
      addMsg('Connection lost — is JARVIS still running?', 'jarvis');
    }
    setBusy(false);
    refreshState();
    input.focus();
  }
  document.getElementById('send').addEventListener('click', send);
  input.addEventListener('keydown', e => { if (e.key === 'Enter') send(); });
  addMsg('Hello, sir. How may I assist you today?', 'jarvis');
  refreshState();
  input.focus();
</script>
</body>
</html>
"""


def build_server(app: ChatApp, host: str = "127.0.0.1", port: int = 8765):
    """An HTTP server bound to one ChatApp. Turns are serialized —
    the core is single-conversation by design."""
    lock = threading.Lock()

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_args):  # keep the console clean
            pass

        def _json(self, code: int, payload: dict):
            body = json.dumps(payload, ensure_ascii=False).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            if self.path == "/api/notifications":
                try:
                    _due_reminder_notifications(app.data_dir)
                except Exception:
                    pass
                items = _read_notifications(app.data_dir)[-20:]
                self._json(200, {
                    "items": list(reversed(items)),
                    "unread": _unread_count(app.data_dir)})
                return
            if self.path == "/api/state":
                learned = 0
                store = getattr(app.memory, "_learned", None)
                if store is not None:
                    learned = len(store._records)
                tools = 0
                try:
                    tools = len(app.registry.advertised_tools(
                        app.allowances))
                except Exception:
                    pass
                try:
                    _due_reminder_notifications(app.data_dir)
                except Exception:
                    pass
                self._json(200, {"provider": app.last_provider,
                                 "tools": tools, "learned": learned,
                                 "notifications":
                                     _unread_count(app.data_dir)})
                return
            body = PAGE.encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_POST(self):
            if self.path == "/api/notifications/read":
                _mark_notifications_read(app.data_dir)
                self._json(200, {"ok": True})
                return
            if self.path != "/api/chat":
                self._json(404, {"error": "not found"})
                return
            try:
                size = int(self.headers.get("Content-Length", 0))
                payload = json.loads(
                    self.rfile.read(size).decode("utf-8"))
                text = str(payload["text"]).strip()
            except Exception:
                self._json(400, {"error": "bad request — JSON with a "
                                          "'text' field expected"})
                return
            if not text:
                self._json(400, {"error": "empty message"})
                return
            with lock:
                if text.startswith("/"):
                    reply = app.handle_command(text)
                else:
                    reply = app.turn(text)
            self._json(200, {"reply": reply,
                             "provider": app.last_provider})

    return ThreadingHTTPServer((host, port), Handler)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        prog="jarvis.ui", description="JARVIS v4 app UI (local web).")
    parser.add_argument("--data-dir", default=str(DEFAULT_DATA_DIR))
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--no-open", action="store_true",
                        help="do not open the browser automatically")
    args = parser.parse_args(argv)

    app = ChatApp(Path(args.data_dir))
    _maybe_auto_watch(app)
    server = build_server(app, port=args.port)
    url = f"http://127.0.0.1:{args.port}/"
    print(f"JARVIS UI — {url}")
    if not args.no_open:
        webbrowser.open(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
