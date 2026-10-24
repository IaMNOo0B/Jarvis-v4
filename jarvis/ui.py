"""JARVIS app UI — fresh build (2026-10-22).

The old UI was deleted and made again on the owner's order: on
his PC the browser tab died with ERR_CONNECTION_REFUSED and the
real reason stayed hidden inside a console window he could not
usefully read back. This build is shaped around ONE law:

    THE PAGE ALWAYS OPENS.

How that is enforced, in order:

1. main()/start_ui() binds the HTTP server FIRST and serves the
   page immediately. ChatApp construction moves to a background
   thread (BootState). The page polls /api/boot:
   starting -> a clear "start ho raha hai" face around the orb;
   ready -> chat enables; error -> THE FULL ERROR TEXT IS SHOWN
   INSIDE THE PAGE, readable and copyable. The same traceback is
   still written to startup-error.txt (belt and braces).
2. Ports fall back: the requested port, then +1 ... +9 — the
   first that binds wins. The browser opens at the ACTUAL bound
   URL, and only after a self health-check GET has succeeded.
3. The supervisor contract is unchanged: `python -m jarvis.ui`
   is the entry, the heartbeat file is refreshed every 10 s,
   and the background services (scheduler / telegram /
   auto-watch) start only after the app is ready, each guarded
   as before — a side service can never veto the UI again.

The face: the owner's golden particle-orb image
(embedded in jarvis/core_image.py, served at /assets/jarvis-core.jpg)
— large, central, ringed, gently glowing; the state word under
it is the TRUTH polled from the app (never a rotating script);
chat, confirmation cards, build cards, notifications, the
Settings panel (keys / telegram / accounts), voice and PTT
controls — the same ChatApp core behind it all, no second brain.

    python -m jarvis.ui [--data-dir DIR] [--port 8765] [--no-open]
"""

from __future__ import annotations

import argparse
import json
import sys
import threading
import traceback
import urllib.request
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from .chat import (DEFAULT_DATA_DIR, ChatApp, _due_reminder_notifications,
                   _mark_notifications_read, _maybe_auto_watch,
                   _maybe_self_check, _read_notifications, _unread_count)
from .config import Config
from .version import BUILD_STAMP

ASSET_NAME = "jarvis-core.jpg"
ASSET_PATH = "/assets/" + ASSET_NAME
_ASSET_FILE = Path(__file__).resolve().parent / "assets" / ASSET_NAME
_asset_cache: dict = {}


def _asset_bytes():
    """The orb image's bytes (decoded once, then cached). The
    image ships embedded as base64 text (jarvis/core_image.py —
    the repo's push route carries text only); a loose file at
    jarvis/assets/jarvis-core.jpg, when present, wins. None when
    neither exists — the route answers an honest 404 and the
    page still opens; the face degrades, the app does not."""
    if "bytes" not in _asset_cache:
        data = None
        try:
            data = _ASSET_FILE.read_bytes()
        except OSError:
            try:
                import base64

                from .core_image import CORE_JPG_B64

                data = base64.b64decode(CORE_JPG_B64)
            except Exception:
                data = None
        _asset_cache["bytes"] = data
    return _asset_cache["bytes"]


class BootState:
    """Where the app behind this server currently stands.

    state: "starting" (ChatApp building in its thread), "ready"
    (app set, chat live), or "error" (construction failed —
    `error` carries the full traceback text for the page).
    """

    def __init__(self, data_dir):
        self.data_dir = Path(data_dir)
        self.state = "starting"
        self.app = None
        self.error = ""

    def mark_ready(self, app) -> None:
        self.app = app
        self.state = "ready"

    def mark_error(self, exc: BaseException) -> None:
        self.error = "".join(traceback.format_exception(
            type(exc), exc, exc.__traceback__))
        self.state = "error"

    def payload(self) -> dict:
        return {"state": self.state, "error": self.error}


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
    background:#0a0603;
    background-image:
      radial-gradient(ellipse 90% 60% at 50% 36%, rgba(190,120,20,.20), transparent 62%),
      radial-gradient(ellipse 60% 40% at 50% 110%, rgba(120,60,10,.12), transparent 70%);
    color:#f3e7d3; font-family:"Segoe UI", system-ui, sans-serif;
    display:flex; flex-direction:column; align-items:center;
    height:100vh; overflow:hidden;
  }
  .stage { flex:0 0 auto; display:flex; flex-direction:column;
           align-items:center; padding-top:3.6vh; }
  /* The face: the owner's golden particle orb, ringed and
     glowing. The motion is pure CSS mood — slow breathing at
     rest, quicker and brighter while JARVIS works or speaks.
     It never pretends to be a state; the WORD below it is the
     state, and that word is polled truth. */
  .orb-wrap { position:relative; width:228px; height:228px; }
  .orb-glow { position:absolute; inset:-52px; border-radius:50%;
    background:radial-gradient(circle, rgba(255,176,32,.34) 0%, rgba(255,140,20,.10) 55%, transparent 72%);
    animation:breathe 4.6s ease-in-out infinite; }
  @keyframes breathe { 0%,100% { opacity:.72; transform:scale(1);} 50% { opacity:1; transform:scale(1.055);} }
  .orb-ring { position:absolute; inset:-11px; border-radius:50%;
    border:2px solid rgba(245,185,66,.55);
    box-shadow:0 0 26px rgba(255,170,40,.35), 0 0 9px rgba(255,190,60,.5) inset;
    animation:ringpulse 4.6s ease-in-out infinite; }
  @keyframes ringpulse { 0%,100% { opacity:.75; } 50% { opacity:1; } }
  #orb { position:absolute; inset:0; width:100%; height:100%;
    border-radius:50%; object-fit:cover; display:block;
    box-shadow:0 0 44px rgba(255,160,30,.30); }
  body.busy .orb-glow { animation-duration:1.3s; }
  body.busy .orb-ring { animation-duration:1.3s; }
  body.speaking .orb-glow { animation-duration:.8s;
    background:radial-gradient(circle, rgba(255,196,64,.5) 0%, rgba(255,150,30,.16) 55%, transparent 72%); }
  body.speaking .orb-ring { border-color:rgba(255,214,110,.95); }
  body.listening .orb-ring { border-color:rgba(255,226,160,.8); }
  #state { margin-top:20px; font-size:13px; letter-spacing:4.5px;
    text-transform:uppercase; color:#ffd479; min-height:17px;
    text-shadow:0 0 12px rgba(255,180,60,.55); }
  #provider { margin-top:5px; font-size:11px; letter-spacing:1.6px;
    color:rgba(230,200,150,.5); min-height:14px; }
  #bootBox { margin-top:8px; font-size:13px; letter-spacing:1.2px;
    color:#ffdf9e; }
  #bootError { display:none; width:min(760px, 94vw); margin-top:10px;
    background:rgba(60,14,8,.85); border:1px solid rgba(255,120,80,.55);
    border-radius:10px; padding:12px 14px; }
  #bootError .etitle { color:#ffb59e; font-size:13.5px; margin-bottom:8px; }
  #bootError pre { white-space:pre-wrap; word-wrap:break-word;
    font-size:12px; line-height:1.45; color:#ffe3d3;
    max-height:30vh; overflow-y:auto; user-select:text; }
  #bootError .enote { color:rgba(255,210,180,.65); font-size:12px;
    margin-top:8px; }
  .chat { flex:1 1 auto; width:min(760px, 94vw); display:flex;
    flex-direction:column; min-height:0; margin-top:2vh; }
  #log { flex:1 1 auto; overflow-y:auto; padding:6px 4px 12px;
    display:flex; flex-direction:column; gap:10px; scrollbar-width:thin; }
  .msg { max-width:82%; padding:10px 14px; border-radius:12px;
    font-size:14.5px; line-height:1.5; white-space:pre-wrap;
    word-wrap:break-word; }
  .msg.user { align-self:flex-end; color:#fff4e0;
    background:linear-gradient(160deg, rgba(146,90,16,.55), rgba(92,54,8,.55));
    border:1px solid rgba(245,185,66,.4); border-bottom-right-radius:3px; }
  .msg.jarvis { align-self:flex-start; color:#f6ead2;
    background:rgba(26,18,8,.66); border:1px solid rgba(245,185,66,.18);
    border-bottom-left-radius:3px; }
  .bar { flex:0 0 auto; display:flex; gap:10px; padding:12px 2px 18px; }
  #text { flex:1; background:rgba(24,16,6,.78); color:#fff6e6;
    border:1px solid rgba(245,185,66,.32); border-radius:10px;
    padding:12px 14px; font-size:15px; outline:none; }
  #text:focus { border-color:rgba(255,205,95,.8);
    box-shadow:0 0 14px rgba(255,170,40,.25); }
  #text:disabled { opacity:.55; }
  #send { background:rgba(120,70,10,.6); color:#ffe1a1;
    border:1px solid rgba(245,185,66,.45); border-radius:10px;
    padding:0 22px; font-size:13px; letter-spacing:3px; cursor:pointer; }
  #send:hover { background:rgba(165,98,16,.7); color:#fff; }
  #send:disabled { opacity:.5; cursor:default; }
  .icons { flex:0 0 auto; display:flex; gap:26px; padding-bottom:14px;
    color:rgba(235,205,155,.45); font-size:13px; letter-spacing:2px; }
  #bell { cursor:pointer; }
  #bell.hot { color:#ffd479; text-shadow:0 0 12px rgba(255,200,90,.8); }
  #npanel { display:none; width:min(760px, 94vw); max-height:30vh;
    overflow-y:auto; margin-bottom:8px; padding:10px 14px;
    background:rgba(26,18,8,.88); border:1px solid rgba(255,205,110,.35);
    border-radius:10px; font-size:13.5px; }
  #npanel .nitem { padding:7px 2px; border-bottom:1px solid rgba(245,185,66,.12); }
  #npanel .nitem:last-child { border-bottom:none; }
  #npanel .ntitle { color:#ffe1a1; }
  #npanel .nempty { color:rgba(230,205,165,.55); }
  #spanel { display:none; width:min(760px, 94vw); max-height:44vh;
    overflow:auto; margin:0 auto 8px; padding:10px 14px;
    background:rgba(22,14,5,.94); border:1px solid rgba(245,185,66,.28);
    border-radius:10px; font-size:13px; color:#f3e7d3; }
  #spanel .stitle { color:#ffe1a1; margin-bottom:6px; }
  #spanel .srow { display:flex; gap:6px; flex-wrap:wrap; margin:5px 0; }
  #spanel input, #spanel select { flex:1 1 130px; background:rgba(40,26,8,.9);
    border:1px solid rgba(245,185,66,.3); border-radius:6px;
    color:#fff6e6; padding:6px 8px; font-size:13px; }
  #spanel button { background:rgba(165,98,16,.9); color:#fff;
    border:none; border-radius:6px; padding:6px 14px; cursor:pointer; }
  #spanel .pname { min-width:110px; color:#ffd479; align-self:center; }
  #spanel .pstat { color:rgba(230,205,165,.65); align-self:center; }
  #smsg { color:#ffd479; margin-top:6px; min-height:16px; }
</style>
</head>
<body>
  <div class="stage">
    <div class="orb-wrap">
      <div class="orb-glow"></div>
      <div class="orb-ring"></div>
      <img id="orb" src="/assets/jarvis-core.jpg" alt="JARVIS">
    </div>
    <div id="state">Start ho raha hai…</div>
    <div id="provider"></div>
    <div id="bootBox">JARVIS start ho raha hai — chat ek pal me khul jayegi…</div>
    <div id="bootError">
      <div class="etitle">JARVIS start nahi ho paya — wajah ye rahi:</div>
      <pre id="bootErrorText"></pre>
      <div class="enote">Is text ko copy karke bhej dijiye, fix ho jayega. Yahi wajah startup-error.txt me bhi save hai.</div>
    </div>
  </div>
  <div class="chat">
    <div id="cards"></div>
    <div id="log"></div>
    <div class="bar">
      <input id="text" placeholder="JARVIS start ho raha hai…" autocomplete="off" disabled>
      <button id="send" disabled>SEND</button>
    </div>
  </div>
  <div id="npanel"></div>
  <div id="spanel">
    <div class="stitle">⚙ API KEYS — ek key set karo, naya brain usi waqt chalu ho jayega (restart nahi chahiye). Key kabhi screen par wapas nahi dikhti.</div>
    <div id="presetRows"></div>
    <div class="stitle" style="margin-top:10px">Naya provider (OpenAI-compatible):</div>
    <div class="srow">
      <input id="cpName" placeholder="Naam (jaise: myprovider)" autocomplete="off">
      <input id="cpUrl" placeholder="Base URL (https://…)" autocomplete="off">
      <input id="cpModel" placeholder="Model" autocomplete="off">
      <input id="cpKey" placeholder="API key" type="password" autocomplete="off">
      <button id="cpSet">SET</button>
    </div>
    <div class="stitle" style="margin-top:12px">📱 TELEGRAM — phone se JARVIS:</div>
    <div class="srow"><span class="pstat" id="tgStatus">…</span></div>
    <div class="srow">
      <input id="tgToken" placeholder="Bot token (BotFather se)" type="password" autocomplete="off">
      <button id="tgSet">SET</button>
      <button id="tgPair">PAIR CODE</button>
      <button id="tgToggle">BAND/CHALU</button>
    </div>
    <div class="srow"><span class="pstat" id="tgCode"></span></div>
    <div class="stitle" style="margin-top:12px">👥 ACCOUNTS — saare khaate, ek hisaab (jitne bhi do, rotation + roz ki had ke saath):</div>
    <div id="accList"></div>
    <div class="srow">
      <select id="accChannel">
        <option value="email">email</option>
        <option value="instagram">instagram</option>
        <option value="threads">threads</option>
        <option value="facebook">facebook</option>
      </select>
      <input id="accLabel" placeholder="Naam (label)" autocomplete="off">
      <input id="accAddress" placeholder="Email / @handle" autocomplete="off">
    </div>
    <div class="srow">
      <input id="accSecret" placeholder="App password (email ke liye)" type="password" autocomplete="off">
      <input id="accCap" placeholder="Roz ki had" type="number" min="1" style="max-width:90px">
      <button id="accAdd">ADD</button>
    </div>
    <div id="smsg"></div>
  </div>
  <div class="icons"><span>◉ CHAT</span><span>▣ TOOLS</span><span>◎ MEMORY</span><span id="bell">🔔 <span id="ncount">0</span></span><span id="keysBtn" style="cursor:pointer">⚙ KEYS</span><span id="voiceBtn" style="cursor:pointer">♪ VOICE · OFF</span><span id="pttBtn" style="cursor:pointer" title="Push-to-talk: dabaye rakho, bolo, chhodo">⌨ PTT · OFF</span><span id="muteBtn" style="cursor:pointer;display:none">🎙 MIC</span><span id="cutBtn" style="cursor:pointer;display:none">✋ CUT</span></div>
<script>
  const log = document.getElementById('log');
  const input = document.getElementById('text');
  const sendBtn = document.getElementById('send');
  const stateEl = document.getElementById('state');
  const providerEl = document.getElementById('provider');
  const bootBox = document.getElementById('bootBox');
  const bootError = document.getElementById('bootError');
  const bootErrorText = document.getElementById('bootErrorText');
  const bell = document.getElementById('bell');
  const ncount = document.getElementById('ncount');
  const npanel = document.getElementById('npanel');
  const cardsEl = document.getElementById('cards');
  const voiceBtn = document.getElementById('voiceBtn');
  const muteBtn = document.getElementById('muteBtn');
  const cutBtn = document.getElementById('cutBtn');
  const pttBtn = document.getElementById('pttBtn');
  const spanel = document.getElementById('spanel');
  const smsg = document.getElementById('smsg');
  // The state word is the TRUTH, polled from the app while a turn
  // runs — never a rotating script (owner's order, 2026-10-21).
  const stateLabels = { standby: 'Standby', starting: 'Start ho raha hai…',
    thinking: 'Thinking…', researching: 'Researching…',
    creating: 'Creating…', working: 'Working…',
    listening: 'Listening…', speaking: 'Speaking…',
    sending: 'Sending…', writing: 'Writing…' };
  let timer = null;
  let bootReady = false;
  let pttOn = false;
  let pttHeld = false;
  let pttBooted = false;
  let voiceActive = false;
  function showState(s) {
    stateEl.textContent = stateLabels[s] || 'Standby';
    document.body.classList.toggle('busy', !!s && s !== 'standby' && s !== 'starting');
    document.body.classList.toggle('speaking', s === 'speaking');
    document.body.classList.toggle('listening', s === 'listening');
  }
  async function pollState() {
    try {
      const r = await fetch('/api/state');
      const s = await r.json();
      showState(s.activity);
    } catch (e) { /* decoration only */ }
  }
  // The state WORD follows reality at all times — boot and voice
  // included. It used to refresh only during typed turns, so it
  // sat on "Standby" while JARVIS spoke.
  async function pollFx() {
    try {
      const r = await fetch('/api/state');
      const s = await r.json();
      showState(s.activity);
    } catch (e) { /* decoration only */ }
  }
  setInterval(pollFx, 700);

  function addMsg(text, who) {
    const div = document.createElement('div');
    div.className = 'msg ' + who;
    div.textContent = text;
    log.appendChild(div);
    log.scrollTop = log.scrollHeight;
  }
  function setBusy(on) {
    clearInterval(timer);
    if (on) {
      showState('thinking');
      timer = setInterval(pollState, 600);
    } else {
      // Ask the app for the TRUE word — forcing 'standby'
      // here flashed Standby after every typed turn while
      // JARVIS was in fact listening.
      pollState();
    }
  }
  function setBadge(n) {
    ncount.textContent = n;
    bell.classList.toggle('hot', n > 0);
  }
  async function decideCard(id, approve) {
    try {
      const r = await fetch('/api/confirm', {method:'POST',
        headers:{'Content-Type':'application/json'},
        body: JSON.stringify({id, approve})});
      const out = await r.json();
      addMsg(out.result || '(card tay ho gaya)', 'jarvis');
    } catch(e) { addMsg('Card ka faisla nahi gaya: ' + e, 'jarvis'); }
    refreshState();
  }
  function renderCards(cards) {
    cardsEl.innerHTML = '';
    (cards || []).forEach(c => {
      const d = document.createElement('div');
      d.style.cssText = 'border:1px solid #ffd479;border-radius:10px;padding:10px 12px;margin:8px;background:rgba(255,212,121,.08)';
      const t = document.createElement('div');
      t.textContent = '🪪 ' + c.summary + ' — ye kaam ulta mushkil hai. Chalun?';
      const yes = document.createElement('button');
      yes.textContent = '✓ HAAN, chalao';
      yes.onclick = () => decideCard(c.id, true);
      const no = document.createElement('button');
      no.textContent = '✗ NAHI';
      no.style.marginLeft = '8px';
      no.onclick = () => decideCard(c.id, false);
      d.appendChild(t); d.appendChild(yes); d.appendChild(no);
      cardsEl.appendChild(d);
    });
  }
  async function decideBuild(id, approve) {
    try {
      const r = await fetch('/api/builds/proceed', {method:'POST',
        headers:{'Content-Type':'application/json'},
        body: JSON.stringify({id, approve})});
      const out = await r.json();
      addMsg(out.ok ? (approve ? 'Build shuru kar di — ant me walkthrough milega.' : 'Build rok di gayi.')
                              : ('Proceed nahi hua: ' + (out.error || '?')), 'jarvis');
    } catch(e) { addMsg('Build ka faisla nahi gaya: ' + e, 'jarvis'); }
    refreshState();
  }
  function renderBuilds(builds) {
    (builds || []).filter(b => b.status === 'awaiting_proceed').forEach(b => {
      const d = document.createElement('div');
      d.style.cssText = 'border:1px solid #ffd479;border-radius:10px;padding:10px 12px;margin:8px;background:rgba(255,212,121,.06)';
      const t = document.createElement('div');
      t.textContent = '🔨 Build plan taiyaar: ' + b.goal + ' — plan dekh kar aage badhun? (chat me /buildplan ' + b.id + ')';
      const yes = document.createElement('button');
      yes.textContent = '▶ PROCEED';
      yes.onclick = () => decideBuild(b.id, true);
      const no = document.createElement('button');
      no.textContent = '✗ NAHI';
      no.style.marginLeft = '8px';
      no.onclick = () => decideBuild(b.id, false);
      d.appendChild(t); d.appendChild(yes); d.appendChild(no);
      cardsEl.appendChild(d);
    });
  }
  async function refreshState() {
    if (!bootReady) return;
    try {
      const r = await fetch('/api/state');
      const s = await r.json();
      providerEl.textContent = s.provider ? ('brain · ' + s.provider + ' · tools ' + s.tools + ' · learned ' + s.learned + (s.build ? ' · build ' + s.build : '')) : '';
      setBadge(s.notifications || 0);
      renderCards(s.confirmations || []);
      renderBuilds(s.builds || []);
      const v = s.voice || {};
      voiceActive = !!v.active;
      voiceBtn.textContent = v.active
        ? ('♪ VOICE · ' + (v.connected ? 'ON' : '…')) : '♪ VOICE · OFF';
      muteBtn.style.display = v.active ? '' : 'none';
      muteBtn.textContent = v.muted ? '🔇 MUTED' : '🎙 MIC';
      cutBtn.style.display = v.speaking ? '' : 'none';
      pttOn = !!s.ptt;
      pttBtn.textContent = pttOn ? '⌨ PTT · ON (Space dabaye rakho)'
                                  : '⌨ PTT · OFF';
    } catch (e) { /* state line is decoration; never break chat */ }
  }
  setInterval(refreshState, 20000);
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
  async function voiceAction(action) {
    try {
      const r = await fetch('/api/voice', { method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ action }) });
      const s = await r.json();
      if (s.message) addMsg(s.message, 'jarvis');
      refreshState();
    } catch (e) { /* voice controls are additive; never break chat */ }
  }
  voiceBtn.addEventListener('click', () => voiceAction('toggle'));
  async function pttHold(held) {
    try {
      await fetch('/api/ptt', { method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ held }) });
    } catch (e) { /* additive control */ }
  }
  pttBtn.addEventListener('click', async () => {
    try {
      await fetch('/api/ptt', { method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ enabled: !pttOn }) });
      refreshState();
    } catch (e) { /* additive control */ }
  });
  // Push-to-talk: Space dabaye rakho to mic khula, chhodo to
  // band — sirf tab jab kahin type na ho raha ho. Voice ON ho
  // to Space sirf HOLD hai (gate khulta hai, engine band
  // NAHI hota); voice OFF ho to Space engine ko is ek bol
  // ke liye chalata hai aur chhodne par band karta hai.
  document.addEventListener('keydown', (e) => {
    if (!pttOn || pttHeld || e.code !== 'Space' || e.repeat) { return; }
    const t = e.target;
    if (t && (t.tagName === 'INPUT' || t.tagName === 'TEXTAREA'
              || t.tagName === 'SELECT')) { return; }
    e.preventDefault();
    pttHeld = true;
    if (voiceActive) { pttHold(true); }
    else { pttBooted = true; voiceAction('start'); }
  });
  document.addEventListener('keyup', (e) => {
    if (!pttOn || !pttHeld || e.code !== 'Space') { return; }
    pttHeld = false;
    if (pttBooted) { pttBooted = false; voiceAction('stop'); }
    else { pttHold(false); }
  });
  async function loadKeys() {
    try {
      const r = await fetch('/api/keys');
      const data = await r.json();
      const rows = document.getElementById('presetRows');
      rows.innerHTML = '';
      for (const p of (data.presets || [])) {
        const row = document.createElement('div');
        row.className = 'srow';
        row.innerHTML = '<span class="pname"></span>'
          + '<span class="pstat"></span>'
          + '<input type="password" placeholder="API key" autocomplete="off">'
          + '<button>SET</button>';
        row.querySelector('.pname').textContent = p.name;
        row.querySelector('.pstat').textContent =
          p.keys > 0 ? ('✓ ' + p.keys + ' key set') : 'koi key nahi';
        const inp = row.querySelector('input');
        row.querySelector('button').addEventListener('click',
          () => setKey({ name: p.name, key: inp.value }));
        rows.appendChild(row);
      }
      for (const c of (data.custom || [])) {
        const row = document.createElement('div');
        row.className = 'srow';
        row.innerHTML = '<span class="pname"></span><span class="pstat"></span>';
        row.querySelector('.pname').textContent = c.name + ' (custom)';
        row.querySelector('.pstat').textContent =
          c.model + ' @ ' + c.base_url
          + (c.has_key ? ' · ✓ key set' : ' · key gayab!');
        rows.appendChild(row);
      }
    } catch (e) { /* panel just stays empty */ }
  }
  async function setKey(payload) {
    smsg.textContent = 'Set kar raha hoon…';
    try {
      const r = await fetch('/api/keys', { method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(payload) });
      const out = await r.json();
      smsg.textContent = out.ok ? (out.note || 'Set ho gaya')
                                : ('Nahi hua: ' + (out.error || '?'));
      if (out.ok) { loadKeys(); }
    } catch (e) { smsg.textContent = 'Nahi hua: server se jawab nahi'; }
  }
  document.getElementById('keysBtn').addEventListener('click', () => {
    const open = spanel.style.display === 'block';
    spanel.style.display = open ? 'none' : 'block';
    if (!open) { loadKeys(); loadTelegram(); loadAccounts(); }
  });
  document.getElementById('cpSet').addEventListener('click', () => {
    setKey({ name: document.getElementById('cpName').value,
             base_url: document.getElementById('cpUrl').value,
             model: document.getElementById('cpModel').value,
             key: document.getElementById('cpKey').value });
  });
  let tgEnabled = true;
  async function loadTelegram() {
    try {
      const r = await fetch('/api/telegram');
      const s = await r.json();
      tgEnabled = !!s.enabled;
      document.getElementById('tgStatus').textContent =
        'Token: ' + (s.token_set ? 'set hai ✓' : 'nahi hai')
        + ' · Phone: ' + (s.bound ? ('juda hai (' + (s.bound_name || '') + ')') : 'juda nahi')
        + ' · Haal: ' + (s.enabled ? 'chalu' : 'band');
    } catch (e) { /* status just stays blank */ }
  }
  async function tgAction(payload, after) {
    smsg.textContent = 'Kar raha hoon…';
    try {
      const r = await fetch('/api/telegram', { method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(payload) });
      const out = await r.json();
      smsg.textContent = out.ok ? (out.note || 'Ho gaya')
                                : ('Nahi hua: ' + (out.error || '?'));
      if (out.ok && after) { after(out); }
      loadTelegram();
    } catch (e) { smsg.textContent = 'Nahi hua: server se jawab nahi'; }
  }
  document.getElementById('tgSet').addEventListener('click', () =>
    tgAction({ action: 'token',
               token: document.getElementById('tgToken').value }));
  document.getElementById('tgPair').addEventListener('click', () =>
    tgAction({ action: 'pair' }, (out) => {
      document.getElementById('tgCode').textContent =
        'Pair code: ' + out.code + ' — phone se bot ko yehi bhejo (10 minute valid)';
    }));
  document.getElementById('tgToggle').addEventListener('click', () =>
    tgAction({ action: 'toggle', enabled: !tgEnabled }));
  async function loadAccounts() {
    try {
      const r = await fetch('/api/accounts');
      const s = await r.json();
      const box = document.getElementById('accList');
      box.innerHTML = '';
      (s.accounts || []).forEach((a) => {
        const row = document.createElement('div');
        row.className = 'srow';
        const name = document.createElement('span');
        name.className = 'pname';
        name.textContent = a.channel + ' · ' + (a.label || a.address);
        const stat = document.createElement('span');
        stat.className = 'pstat';
        stat.textContent = a.sent_today + '/' + a.cap + ' aaj · '
          + (a.enabled ? (a.eligible ? 'taiyaar' : ('rest: ' + (a.rest_reason || 'had'))) : 'band');
        const tog = document.createElement('button');
        tog.textContent = a.enabled ? 'BAND' : 'CHALU';
        tog.addEventListener('click', () =>
          accAction({ action: 'toggle', id: a.id, enabled: !a.enabled }));
        const del = document.createElement('button');
        del.textContent = '✕';
        del.addEventListener('click', () =>
          accAction({ action: 'remove', id: a.id }));
        row.appendChild(name); row.appendChild(stat);
        row.appendChild(tog); row.appendChild(del);
        box.appendChild(row);
      });
    } catch (e) { /* list just stays as-is */ }
  }
  async function accAction(payload) {
    smsg.textContent = 'Kar raha hoon…';
    try {
      const r = await fetch('/api/accounts', { method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(payload) });
      const out = await r.json();
      smsg.textContent = out.ok ? (out.note || 'Ho gaya')
                                : ('Nahi hua: ' + (out.error || '?'));
      loadAccounts();
    } catch (e) { smsg.textContent = 'Nahi hua: server se jawab nahi'; }
  }
  document.getElementById('accAdd').addEventListener('click', () =>
    accAction({ action: 'add',
      channel: document.getElementById('accChannel').value,
      label: document.getElementById('accLabel').value,
      address: document.getElementById('accAddress').value,
      secret: document.getElementById('accSecret').value,
      cap: parseInt(document.getElementById('accCap').value || '0', 10) }));
  muteBtn.addEventListener('click', () => voiceAction('mute'));
  cutBtn.addEventListener('click', () => voiceAction('interrupt'));
  async function send() {
    if (!bootReady) return;
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
  sendBtn.addEventListener('click', send);
  input.addEventListener('keydown', e => { if (e.key === 'Enter') send(); });
  // ---- Boot gate ----------------------------------------------
  // The page opens BEFORE the app exists (the server binds
  // first, ChatApp builds behind it). Poll /api/boot: while
  // starting, chat stays disabled with a clear note; on ready,
  // everything enables; on error, the full reason is shown
  // right here in the page — never a dead tab again.
  async function pollBoot() {
    try {
      const r = await fetch('/api/boot');
      const b = await r.json();
      if (b.state === 'ready') {
        bootReady = true;
        bootBox.style.display = 'none';
        input.disabled = false;
        sendBtn.disabled = false;
        input.placeholder = 'Type here, sir…';
        refreshState();
        input.focus();
        return;
      }
      if (b.state === 'error') {
        bootBox.style.display = 'none';
        bootError.style.display = 'block';
        bootErrorText.textContent = b.error || 'unknown error';
        stateEl.textContent = 'Start fail ho gaya';
        return;
      }
    } catch (e) { /* the server hiccuped; keep polling */ }
    setTimeout(pollBoot, 600);
  }
  pollBoot();
</script>
</body>
</html>
"""


def ui_bind_host(app: ChatApp) -> str:
    """Where the UI listens. Loopback by default — the owner's
    PC only. config ui.bind_host may widen it (his Tailscale
    address, or 0.0.0.0 on his own network): phone access is
    HIS call, made in config, never a silent default."""
    try:
        host = str(app.config.get("ui", "bind_host",
                                  "127.0.0.1")).strip()
        return host or "127.0.0.1"
    except Exception:
        return "127.0.0.1"


def _bind_host_for_data_dir(data_dir) -> str:
    """The bind host BEFORE any ChatApp exists — the server must
    bind before the app is built, so the same config ui.bind_host
    is read straight from the data dir's config store. Any
    failure means the safe default: loopback."""
    try:
        host = str(Config(data_dir).get("ui", "bind_host",
                                        "127.0.0.1")).strip()
        return host or "127.0.0.1"
    except Exception:
        return "127.0.0.1"


def _boot_state_payload(boot: BootState) -> dict:
    """/api/state while the app is not (yet) there: the same
    shape the page and the buddy overlay poll, with honest
    starting values — the overlay must keep working (and keep
    telling the truth) even while JARVIS is still booting."""
    unread = 0
    try:
        unread = _unread_count(boot.data_dir)
    except Exception:
        pass
    return {"provider": "", "tools": 0, "learned": 0,
            "activity": "starting" if boot.state == "starting"
                        else "standby",
            "tool": "", "voice": {"active": False},
            "ptt": False, "confirmations": [], "builds": [],
            "incognito": False, "notifications": unread,
            "boot": boot.state, "build": BUILD_STAMP}


def build_server(app: ChatApp = None, host: str = "127.0.0.1",
                 port: int = 8765, boot: BootState = None):
    """An HTTP server over the boot state. Called with a ready
    ChatApp (the classic shape) it wraps it in a ready BootState;
    called with boot= it serves the page from the first second
    and the app whenever the boot thread lands it. Turns are
    serialized — the core is single-conversation by design."""
    if boot is None:
        if app is None:
            raise ValueError("build_server needs an app or a boot")
        boot = BootState(app.data_dir)
        boot.mark_ready(app)
    lock = threading.Lock()

    def _not_ready(handler):
        handler._json(503, {"ok": False,
                            "error": "JARVIS abhi start ho raha "
                                     "hai, sir — ek pal rukiye."})

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

        def _read_payload(self):
            size = int(self.headers.get("Content-Length", 0))
            return json.loads(self.rfile.read(size).decode("utf-8"))

        def do_GET(self):
            if self.path == ASSET_PATH:
                data = _asset_bytes()
                if data is None:
                    self._json(404, {"error": "asset missing"})
                    return
                self.send_response(200)
                self.send_header("Content-Type", "image/jpeg")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)
                return
            if self.path == "/api/boot":
                self._json(200, boot.payload())
                return
            if self.path == "/api/notifications":
                data_dir = boot.app.data_dir if boot.app is not None \
                    else boot.data_dir
                try:
                    _due_reminder_notifications(data_dir)
                except Exception:
                    pass
                items = _read_notifications(data_dir)[-20:]
                self._json(200, {
                    "items": list(reversed(items)),
                    "unread": _unread_count(data_dir)})
                return
            if self.path == "/api/state":
                app = boot.app
                if app is None:
                    self._json(200, _boot_state_payload(boot))
                    return
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
                voice = {"active": False}
                try:
                    voice = app.voice_status()
                except Exception:
                    pass
                self._json(200, {"provider": app.last_provider,
                                 "tools": tools, "learned": learned,
                                 "activity": app.activity.state,
                                 "tool": app.activity.tool,
                                 "voice": voice,
                                 "ptt": app.push_to_talk(),
                                 "confirmations":
                                     app.pending_confirmations(),
                                 "builds": app.list_builds(),
                                 "incognito": bool(app.incognito),
                                 "notifications":
                                     _unread_count(app.data_dir),
                                 "boot": boot.state,
                                 "build": BUILD_STAMP})
                return
            app = boot.app
            if self.path == "/api/keys":
                if app is None:
                    _not_ready(self)
                    return
                with lock:
                    settings = app.provider_settings()
                self._json(200, settings)
                return
            if self.path == "/api/telegram":
                if app is None:
                    _not_ready(self)
                    return
                with lock:
                    status = app.telegram_status()
                self._json(200, status)
                return
            if self.path == "/api/accounts":
                if app is None:
                    _not_ready(self)
                    return
                with lock:
                    rows = app.accounts_overview()
                self._json(200, {"accounts": rows})
                return
            body = PAGE.encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_POST(self):
            app = boot.app
            if self.path == "/api/notifications/read":
                data_dir = app.data_dir if app is not None \
                    else boot.data_dir
                _mark_notifications_read(data_dir)
                self._json(200, {"ok": True})
                return
            if app is None:
                if self.path == "/api/chat":
                    self._json(503, {
                        "error": "JARVIS abhi start ho raha hai, "
                                 "sir — ek pal rukiye."})
                else:
                    _not_ready(self)
                return
            if self.path == "/api/builds/proceed":
                try:
                    payload = self._read_payload()
                except Exception:
                    self._json(400, {"ok": False,
                                     "error": "bad request"})
                    return
                with lock:
                    out = app.proceed_build(
                        str(payload.get("id", "")),
                        # Fail CLOSED: a missing approve field
                        # is not a Proceed (was default True).
                        approve=bool(payload.get("approve", False)))
                self._json(200, out)
                return
            if self.path == "/api/confirm":
                try:
                    payload = self._read_payload()
                except Exception:
                    self._json(400, {"ok": False,
                                     "error": "bad request"})
                    return
                with lock:
                    out = app.confirm_card(
                        str(payload.get("id", "")),
                        approve=bool(payload.get("approve")))
                self._json(200, {"ok": True, "result": out})
                return
            if self.path == "/api/ptt":
                try:
                    payload = self._read_payload()
                except Exception:
                    self._json(400, {"ok": False,
                                     "error": "bad request"})
                    return
                with lock:
                    if "held" in payload:
                        out = app.voice_ptt_hold(
                            bool(payload.get("held")))
                    else:
                        out = app.set_push_to_talk(
                            bool(payload.get("enabled", False)))
                self._json(200 if out.get("ok") else 400, out)
                return
            if self.path == "/api/accounts":
                try:
                    payload = self._read_payload()
                except Exception:
                    self._json(400, {"ok": False,
                                     "error": "bad request"})
                    return
                action = str(payload.get("action", ""))
                with lock:
                    if action == "add":
                        out = app.add_account(
                            str(payload.get("channel", "")),
                            str(payload.get("label", "")),
                            str(payload.get("address", "")),
                            str(payload.get("secret", "")),
                            int(payload.get("cap", 0) or 0))
                    elif action == "toggle":
                        out = app.set_account_enabled(
                            str(payload.get("id", "")),
                            bool(payload.get("enabled", True)))
                    elif action == "remove":
                        out = app.remove_account(
                            str(payload.get("id", "")))
                    else:
                        out = {"ok": False,
                               "error": "unknown action"}
                self._json(200 if out.get("ok") else 400, out)
                return
            if self.path == "/api/telegram":
                try:
                    payload = self._read_payload()
                except Exception:
                    self._json(400, {"ok": False,
                                     "error": "bad request"})
                    return
                action = str(payload.get("action", ""))
                with lock:
                    if action == "token":
                        out = app.set_telegram_token(
                            str(payload.get("token", "")))
                    elif action == "pair":
                        out = app.telegram_pair_code()
                    elif action == "toggle":
                        out = app.set_telegram_enabled(
                            bool(payload.get("enabled", True)))
                    else:
                        out = {"ok": False,
                               "error": "unknown action"}
                self._json(200 if out.get("ok") else 400, out)
                return
            if self.path == "/api/keys":
                try:
                    payload = self._read_payload()
                except Exception:
                    self._json(400, {"ok": False,
                                     "error": "bad request"})
                    return
                with lock:
                    out = app.set_provider(
                        str(payload.get("name", "")),
                        str(payload.get("key", "")),
                        base_url=str(payload.get("base_url", "")),
                        model=str(payload.get("model", "")))
                self._json(200 if out.get("ok") else 400, out)
                return
            if self.path == "/api/voice":
                try:
                    action = str(self._read_payload().get(
                        "action", "toggle")).lower()
                except Exception:
                    # Malformed JSON is a 400 — it must never
                    # TOGGLE the owner's voice (the old fallback
                    # did exactly that).
                    self._json(400, {"ok": False,
                                     "error": "bad request"})
                    return
                with lock:
                    if action == "mute":
                        message = app.handle_command("/voice mute")
                    elif action == "interrupt":
                        message = app.handle_command(
                            "/voice interrupt")
                    elif action == "start":
                        message = app.start_voice()
                    elif action == "stop":
                        message = app.stop_voice()
                    else:
                        status = app.voice_status()
                        message = (app.stop_voice()
                                   if status.get("active")
                                   else app.start_voice())
                self._json(200, {"message": message,
                                 "voice": app.voice_status()})
                return
            if self.path != "/api/chat":
                self._json(404, {"error": "not found"})
                return
            try:
                payload = self._read_payload()
                text = str(payload["text"]).strip()
            except Exception:
                self._json(400, {"error": "bad request — JSON with a "
                                          "'text' field expected"})
                return
            if not text:
                self._json(400, {"error": "empty message"})
                return
            with lock:
                app.activity.set("thinking")
                try:
                    if text.startswith("/"):
                        reply = app.handle_command(text)
                    else:
                        reply = app.turn(text)
                except Exception as exc:
                    # A crashed turn is a JSON error the UI can
                    # SHOW — never a dropped connection (the
                    # old behaviour made the UI ask "is JARVIS
                    # still running?" about a healthy process).
                    self._json(500, {
                        "error": f"turn fail ho gaya, sir: "
                                 f"{type(exc).__name__}: "
                                 f"{str(exc)[:200]}"})
                    return
                finally:
                    # The resting word after a turn is Listening
                    # — JARVIS is waiting for the owner's next
                    # input. Standby arrives only through the
                    # Activity idle rule (120s of silence), per
                    # the owner's order; it is never snapped on
                    # at the end of a reply.
                    v = app.voice_status()
                    if v.get("active"):
                        app.activity.set("speaking" if v.get(
                            "speaking") else "listening")
                    else:
                        app.activity.set("listening")
            self._json(200, {"reply": reply,
                             "provider": app.last_provider})

    return ThreadingHTTPServer((host, port), Handler)


def _write_startup_error(data_dir, exc) -> None:
    """Leave the crash evidence in files the owner can paste back:
    <data dir>/startup-error.txt AND ./startup-error.txt (the app
    folder, where the launcher runs). Never raises — a failure to
    record a failure must not become a second failure."""
    import time as _time
    import traceback as _traceback
    text = (f"--- {_time.strftime('%Y-%m-%d %H:%M:%S')} ---\n"
            + "".join(_traceback.format_exception(
                type(exc), exc, exc.__traceback__)))
    targets = []
    for target in (Path(data_dir) / "startup-error.txt",
                   Path.cwd() / "startup-error.txt"):
        if target not in targets:
            targets.append(target)
    for target in targets:
        try:
            with target.open("a", encoding="utf-8") as fh:
                fh.write(text)
        except Exception:
            pass
    print("JARVIS could not start — the reason was saved to "
          "startup-error.txt in the JARVIS folder. Paste that "
          "file's contents back for a fix.")


def _start_background_services(app) -> None:
    """Scheduler / telegram / auto-watch start AFTER the server
    exists, each behind its own guard: a side service that raises
    or hangs at startup must never again keep the UI itself from
    opening (owner's PC, 2026-10-22: the app would not open at
    all and no evidence survived anywhere he could reach)."""
    from .chat import start_scheduler, start_telegram
    for name, starter in (("auto-watch", _maybe_auto_watch),
                          ("scheduler", start_scheduler),
                          ("telegram", start_telegram)):
        try:
            starter(app)
        except Exception as exc:  # the UI opening outranks it
            _write_startup_error(app.data_dir, exc)
            print(f"JARVIS: {name} did not start "
                  f"({type(exc).__name__}: {exc}) — the app "
                  "itself is running.")


def _boot_app(data_dir, boot: BootState, start_services: bool) -> None:
    """The boot thread: build the ChatApp behind the already-live
    server. A crash here is NOT a dead UI — it lands in the boot
    state (the page shows the full text) and in startup-error.txt,
    and the process keeps serving so the owner can read both."""
    try:
        app = ChatApp(data_dir)
    except Exception as exc:
        boot.mark_error(exc)
        _write_startup_error(data_dir, exc)
        return
    boot.mark_ready(app)
    # Seed the back-of-mind loop at once: the four questions are
    # answered from the first second, not only after the first
    # scheduler tick a minute later. Never raises, never speaks.
    try:
        _maybe_self_check(app)
    except Exception:
        pass
    if start_services:
        _start_background_services(app)


class UIHandle:
    """A running UI: the server, its boot state, the bound URL."""

    def __init__(self, server, boot: BootState, host: str):
        self.server = server
        self.boot = boot
        self.host = host
        self.port = server.server_address[1]
        self.url = f"http://{host}:{self.port}/"

    def serve_forever(self) -> None:
        self.server.serve_forever()

    def shutdown(self) -> None:
        self.server.shutdown()
        self.server.server_close()


def _health_check(url: str, attempts: int = 25) -> bool:
    """GET the boot endpoint until the server answers — the
    browser is opened only once this succeeds, at the URL that
    actually bound. Loopback only: no proxy may intercept it."""
    opener = urllib.request.build_opener(
        urllib.request.ProxyHandler({}))
    import time as _time
    for _ in range(attempts):
        try:
            with opener.open(url + "api/boot", timeout=1) as resp:
                if resp.status == 200:
                    return True
        except Exception:
            _time.sleep(0.1)
    return False


def start_ui(data_dir, port: int = 8765, open_browser: bool = True,
             start_services: bool = True) -> UIHandle:
    """Bind FIRST, boot behind it. Ports fall back +1 ... +9 from
    the requested one (port 0 binds one ephemeral port, for
    tests). Raises the last OSError when no port binds at all —
    the one failure no page can paper over, so main() records it
    in startup-error.txt and returns 1."""
    data_dir = Path(data_dir)
    host = _bind_host_for_data_dir(data_dir)
    boot = BootState(data_dir)
    server = None
    last_exc = None
    candidates = [port] if port == 0 else \
        list(range(port, port + 10))
    for candidate in candidates:
        try:
            server = build_server(boot=boot, host=host,
                                  port=candidate)
            break
        except OSError as exc:
            last_exc = exc
    if server is None:
        raise last_exc if last_exc is not None else OSError(
            "no port could be bound")
    handle = UIHandle(server, boot, host)

    def _heartbeat():
        # The supervisor (jarvis/supervisor.py) watches this file:
        # a live process with a stale heartbeat is a HUNG app. It
        # beats from the moment the server exists — a slow app
        # boot is not a hang.
        from .supervisor import write_heartbeat
        import time as _time
        while True:
            write_heartbeat(data_dir)
            _time.sleep(10)

    threading.Thread(target=_heartbeat, daemon=True,
                     name="jarvis-heartbeat").start()
    threading.Thread(target=_boot_app,
                     args=(data_dir, boot, start_services),
                     daemon=True, name="jarvis-boot").start()
    if open_browser:
        if _health_check(handle.url):
            webbrowser.open(handle.url)
        else:
            print("JARVIS: the page did not answer its health "
                  f"check — open {handle.url} yourself, sir.")
    return handle


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        prog="jarvis.ui", description="JARVIS v4 app UI (local web).")
    parser.add_argument("--data-dir", default=str(DEFAULT_DATA_DIR))
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--no-open", action="store_true",
                        help="do not open the browser automatically")
    args = parser.parse_args(argv)

    data_dir = Path(args.data_dir)
    try:
        handle = start_ui(data_dir, port=args.port,
                          open_browser=not args.no_open)
    except Exception as exc:
        _write_startup_error(data_dir, exc)
        return 1
    print(f"JARVIS UI — {handle.url}")
    if handle.host != "127.0.0.1":
        print("Note: UI is reachable beyond this PC (config "
              "ui.bind_host). On a private tailnet that is the "
              "point; on an open network, set it back to "
              "127.0.0.1.")
    try:
        handle.serve_forever()
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
