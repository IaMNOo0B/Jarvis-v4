"""JARVIS app UI — the face from the owner's reference photo.

A local web app (stdlib http.server, bound to 127.0.0.1 only) over
the SAME ChatApp core the CLI uses — no second brain, no drift: the
chat he perfects here is the chat everywhere. The design follows his
photo: dark grid, one glowing ring with JARVIS in the middle, a
state word under the ring that tells the TRUTH about what the core
is doing (Standby / Thinking / Researching / Creating / Working —
polled from the app's activity, never rotated on a timer), minimal
chrome. Voice arrives later by his order; this is chat-first.

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
  #spanel { display:none; width:min(760px, 94vw); max-height:44vh;
    overflow:auto; margin:0 auto 8px; padding:10px 14px;
    background:rgba(10,22,38,.92); border:1px solid rgba(120,200,255,.25);
    border-radius:10px; font-size:13px; color:#cfe8ff; }
  #spanel .stitle { color:#ffe1a1; margin-bottom:6px; }
  #spanel .srow { display:flex; gap:6px; flex-wrap:wrap; margin:5px 0; }
  #spanel input { flex:1 1 130px; background:rgba(20,40,66,.9);
    border:1px solid rgba(120,200,255,.3); border-radius:6px;
    color:#eaf6ff; padding:6px 8px; font-size:13px; }
  #spanel button { background:rgba(40,110,180,.85); color:#fff;
    border:none; border-radius:6px; padding:6px 14px; cursor:pointer; }
  #spanel .pname { min-width:110px; color:#9fd8ff; align-self:center; }
  #spanel .pstat { color:rgba(180,210,245,.6); align-self:center; }
  #smsg { color:#ffd479; margin-top:6px; min-height:16px; }
  /* Living HUD — a particle field + waveform ring around the
     core, driven by JARVIS's REAL states (see the engine in
     the script): speaking pulses, listening draws inward,
     working swirls amber, standby merely drifts. */
  #orbFx { position:absolute; inset:-96px; width:calc(100% + 192px);
    height:calc(100% + 192px); pointer-events:none; z-index:1; }
  .ring-wrap svg.ring, .ring-wrap .ticks { z-index:2; }
  .ring-core { z-index:3; }
  #hudTool { margin-top:4px; font-size:11px; letter-spacing:2.5px;
    color:#ffd479; min-height:14px; text-transform:uppercase;
    text-shadow:0 0 8px rgba(255,200,90,.5); }
  body.speaking .ring-core { border-color:rgba(170,228,255,.85);
    box-shadow:0 0 54px rgba(56,170,255,.6) inset, 0 0 46px rgba(56,170,255,.5); }
  body.speaking .ring-arc { animation-duration:.9s; stroke:#aee6ff; }
  body.listening .ring-core { border-color:rgba(140,255,190,.6);
    box-shadow:0 0 34px rgba(90,255,170,.3) inset, 0 0 26px rgba(90,255,170,.25); }
  body.listening .ring-arc { stroke:#8dffbe; }
</style>
</head>
<body>
  <div class="stage">
    <div class="ring-wrap">
      <div class="ring-glow"></div>
      <canvas id="orbFx" width="600" height="600"></canvas>
      <div class="ticks"></div>
      <svg class="ring" viewBox="0 0 200 200">
        <circle class="ring-track" cx="100" cy="100" r="96"></circle>
        <circle class="ring-arc" cx="100" cy="100" r="96"></circle>
      </svg>
      <div class="ring-core"><span>JARVIS</span></div>
    </div>
    <div id="state">Standby</div>
    <div id="provider"></div>
    <div id="hudTool"></div>
  </div>
  <div class="chat">
    <div id="cards"></div>
    <div id="log"></div>
    <div class="bar">
      <input id="text" placeholder="Type here, sir…" autocomplete="off">
      <button id="send">SEND</button>
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
  const stateEl = document.getElementById('state');
  const providerEl = document.getElementById('provider');
  // The state word is the TRUTH, polled from the app while a turn
  // runs — never a rotating script (owner's order, 2026-10-21).
  const stateLabels = { standby: 'Standby', thinking: 'Thinking…',
    researching: 'Researching…', creating: 'Creating…',
    working: 'Working…', listening: 'Listening…',
    speaking: 'Speaking…' };
  let timer = null;
  function showState(s) {
    stateEl.textContent = stateLabels[s] || 'Standby';
    document.body.classList.toggle('busy', !!s && s !== 'standby');
  }
  async function pollState() {
    try {
      const r = await fetch('/api/state');
      const s = await r.json();
      showState(s.activity);
      setFx(s);
    } catch (e) { /* decoration only */ }
  }

  // ---- Living HUD engine -------------------------------------
  // Motion follows JARVIS's REAL state, never a script (the
  // owner's truth law): speaking pulses with a voice-like
  // envelope, listening draws the field inward, working
  // swirls it amber, standby lets it drift and rest.
  const fx = { mode: 'idle', amp: 0.12, level: null };
  const hudToolEl = document.getElementById('hudTool');
  function setFx(s) {
    const v = (s && s.voice) || {};
    const a = (s && s.activity) || 'standby';
    let mode = 'idle';
    if (v.speaking || a === 'speaking') mode = 'speaking';
    else if (a === 'listening' || v.awake) mode = 'listening';
    else if (a && a !== 'standby') mode = 'busy';
    fx.mode = mode;
    fx.level = (typeof v.level === 'number') ? v.level : null;
    document.body.classList.toggle('speaking', mode === 'speaking');
    document.body.classList.toggle('listening', mode === 'listening');
    if (hudToolEl) {
      const tool = (mode === 'busy' || mode === 'speaking')
        ? (s && s.tool) : '';
      hudToolEl.textContent = tool
        ? ('⚙ ' + String(tool).replace(/_/g, ' ')) : '';
    }
  }
  async function pollFx() {
    try {
      const r = await fetch('/api/state');
      const s = await r.json();
      // The state WORD follows reality at all times — voice
      // included. It used to refresh only during typed
      // turns, so it sat on "Standby" while JARVIS spoke.
      showState(s.activity);
      setFx(s);
    } catch (e) { /* decoration only */ }
  }
  setInterval(pollFx, 700);
  (function orbEngine() {
    const cv = document.getElementById('orbFx');
    if (!cv || !cv.getContext) return;
    const ctx = cv.getContext('2d');
    const W = cv.width, H = cv.height, CX = W / 2, CY = H / 2;
    const ps = [];
    for (let i = 0; i < 110; i++) {
      ps.push({ a: Math.random() * Math.PI * 2,
                r: 122 + Math.random() * 150,
                sp: (Math.random() * 0.5 + 0.12)
                    * (Math.random() < 0.5 ? -1 : 1),
                sz: Math.random() * 1.9 + 0.6,
                tw: Math.random() * Math.PI * 2 });
    }
    let t = 0;
    function frame() {
      requestAnimationFrame(frame);
      if (document.hidden) return;
      t += 0.016;
      // A measured voice level (the app reports the real
      // PCM level of what JARVIS says/hears) drives the
      // dance when present; the envelope is only a resting
      // fallback for when no level is being measured.
      let target = fx.mode === 'speaking' ? 1
        : fx.mode === 'listening' ? 0.45
        : fx.mode === 'busy' ? 0.6 : 0.14;
      const surge = fx.mode === 'speaking'
        ? (0.55 + 0.45 * Math.sin(t * 7.3)
             * Math.sin(t * 3.1 + 1.7)
           + 0.25 * Math.sin(t * 13.7)) : 1;
      target *= Math.max(0.25, surge);
      if (fx.level !== null
          && (fx.mode === 'speaking' || fx.mode === 'listening'))
        target = Math.min(1, 0.12 + fx.level * 1.7);
      fx.amp += (target - fx.amp) * 0.08;
      const amp = fx.amp;
      ctx.clearRect(0, 0, W, H);
      // Waveform ring: 56 bars breathing with the envelope.
      const bars = 56, baseR = 118;
      ctx.lineWidth = 2;
      for (let i = 0; i < bars; i++) {
        const ang = (i / bars) * Math.PI * 2;
        const w = 0.5 + 0.5 * Math.sin(
          i * 1.93 + t * (fx.mode === 'speaking' ? 9 : 2.2));
        const len = 3 + amp * 30 * w;
        const r1 = baseR + amp * 7 * Math.sin(ang * 3 + t * 2);
        ctx.strokeStyle = fx.mode === 'listening'
          ? 'rgba(140,255,190,' + (0.22 + amp * 0.5) + ')'
          : 'rgba(96,196,255,' + (0.2 + amp * 0.55) + ')';
        ctx.beginPath();
        ctx.moveTo(CX + Math.cos(ang) * r1,
                   CY + Math.sin(ang) * r1);
        ctx.lineTo(CX + Math.cos(ang) * (r1 + len),
                   CY + Math.sin(ang) * (r1 + len));
        ctx.stroke();
      }
      // The particle field around the core.
      const swirl = fx.mode === 'busy' ? 2.6
        : fx.mode === 'speaking' ? 1.6 : 1;
      for (const p of ps) {
        p.a += 0.0016 * p.sp * swirl
             * (fx.mode === 'listening' ? 1.4 : 1);
        let rr = p.r + Math.sin(t * 0.9 + p.tw) * 7
          + (fx.mode === 'listening'
             ? -18 * (0.5 + 0.5 * Math.sin(t * 0.7 + p.tw))
             : amp * 16 * Math.sin(p.a * 3 + t * 2.4));
        const x = CX + Math.cos(p.a) * rr;
        const y = CY + Math.sin(p.a) * rr * 0.98;
        const tw = 0.35 + 0.65
          * (0.5 + 0.5 * Math.sin(t * 2 + p.tw));
        ctx.fillStyle = (fx.mode === 'busy' && p.sz > 2)
          ? 'rgba(255,212,121,' + (0.75 * tw) + ')'
          : 'rgba(140,215,255,' + (0.8 * tw) + ')';
        ctx.beginPath();
        ctx.arc(x, y, p.sz * (1 + amp * 0.6), 0, Math.PI * 2);
        ctx.fill();
      }
    }
    requestAnimationFrame(frame);
  })();

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
      showState('standby');
    }
  }
  const bell = document.getElementById('bell');
  const ncount = document.getElementById('ncount');
  const npanel = document.getElementById('npanel');
  function setBadge(n) {
    ncount.textContent = n;
    bell.classList.toggle('hot', n > 0);
  }
  const cardsEl = document.getElementById('cards');
  async function decideCard(id, approve) {
    try {
      const r = await fetch('/api/confirm', {method:'POST',
        headers:{'Content-Type':'application/json'},
        body: JSON.stringify({id, approve})});
      const out = await r.json();
      addMsg('jarvis', out.result || '(card tay ho gaya)');
    } catch(e) { addMsg('jarvis', 'Card ka faisla nahi gaya: ' + e); }
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
      addMsg('jarvis', out.ok ? (approve ? 'Build shuru kar di — ant me walkthrough milega.' : 'Build rok di gayi.')
                              : ('Proceed nahi hua: ' + (out.error || '?')));
    } catch(e) { addMsg('jarvis', 'Build ka faisla nahi gaya: ' + e); }
    refreshState();
  }
  function renderBuilds(builds) {
    (builds || []).filter(b => b.status === 'awaiting_proceed').forEach(b => {
      const d = document.createElement('div');
      d.style.cssText = 'border:1px solid #7ad9ff;border-radius:10px;padding:10px 12px;margin:8px;background:rgba(122,217,255,.08)';
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
    try {
      const r = await fetch('/api/state');
      const s = await r.json();
      providerEl.textContent = s.provider ? ('brain · ' + s.provider + ' · tools ' + s.tools + ' · learned ' + s.learned) : '';
      setBadge(s.notifications || 0);
      renderCards(s.confirmations || []);
      renderBuilds(s.builds || []);
      const v = s.voice || {};
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
  const voiceBtn = document.getElementById('voiceBtn');
  const muteBtn = document.getElementById('muteBtn');
  const cutBtn = document.getElementById('cutBtn');
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
  const pttBtn = document.getElementById('pttBtn');
  let pttOn = false;
  let pttHeld = false;
  pttBtn.addEventListener('click', async () => {
    try {
      await fetch('/api/ptt', { method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ enabled: !pttOn }) });
      refreshState();
    } catch (e) { /* additive control */ }
  });
  // Push-to-talk: Space dabaye rakho to mic khula, chhodo to
  // band — sirf tab jab kahin type na ho raha ho.
  document.addEventListener('keydown', (e) => {
    if (!pttOn || pttHeld || e.code !== 'Space' || e.repeat) { return; }
    const t = e.target;
    if (t && (t.tagName === 'INPUT' || t.tagName === 'TEXTAREA'
              || t.tagName === 'SELECT')) { return; }
    e.preventDefault();
    pttHeld = true;
    voiceAction('start');
  });
  document.addEventListener('keyup', (e) => {
    if (!pttOn || !pttHeld || e.code !== 'Space') { return; }
    pttHeld = false;
    voiceAction('stop');
  });
  const spanel = document.getElementById('spanel');
  const smsg = document.getElementById('smsg');
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
            if self.path == "/api/keys":
                with lock:
                    settings = app.provider_settings()
                self._json(200, settings)
                return
            if self.path == "/api/telegram":
                with lock:
                    status = app.telegram_status()
                self._json(200, status)
                return
            if self.path == "/api/accounts":
                with lock:
                    rows = app.accounts_overview()
                self._json(200, {"accounts": rows})
                return
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
                                     _unread_count(app.data_dir)})
                return
            body = PAGE.encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_POST(self):
            if self.path == "/api/builds/proceed":
                try:
                    size = int(self.headers.get("Content-Length", 0))
                    payload = json.loads(
                        self.rfile.read(size).decode("utf-8"))
                except Exception:
                    self._json(400, {"ok": False,
                                     "error": "bad request"})
                    return
                with lock:
                    out = app.proceed_build(
                        str(payload.get("id", "")),
                        approve=bool(payload.get("approve", True)))
                self._json(200, out)
                return
            if self.path == "/api/confirm":
                try:
                    size = int(self.headers.get("Content-Length", 0))
                    payload = json.loads(
                        self.rfile.read(size).decode("utf-8"))
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
                    size = int(self.headers.get("Content-Length", 0))
                    payload = json.loads(
                        self.rfile.read(size).decode("utf-8"))
                except Exception:
                    self._json(400, {"ok": False,
                                     "error": "bad request"})
                    return
                with lock:
                    out = app.set_push_to_talk(
                        bool(payload.get("enabled", False)))
                self._json(200 if out.get("ok") else 400, out)
                return
            if self.path == "/api/accounts":
                try:
                    size = int(self.headers.get("Content-Length", 0))
                    payload = json.loads(
                        self.rfile.read(size).decode("utf-8"))
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
                    size = int(self.headers.get("Content-Length", 0))
                    payload = json.loads(
                        self.rfile.read(size).decode("utf-8"))
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
                    size = int(self.headers.get("Content-Length", 0))
                    payload = json.loads(
                        self.rfile.read(size).decode("utf-8"))
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
            if self.path == "/api/notifications/read":
                _mark_notifications_read(app.data_dir)
                self._json(200, {"ok": True})
                return
            if self.path == "/api/voice":
                try:
                    size = int(self.headers.get("Content-Length", 0))
                    action = str(json.loads(
                        self.rfile.read(size).decode("utf-8")
                    ).get("action", "toggle")).lower()
                except Exception:
                    action = "toggle"
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
                app.activity.set("thinking")
                try:
                    if text.startswith("/"):
                        reply = app.handle_command(text)
                    else:
                        reply = app.turn(text)
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
    from .chat import start_scheduler, start_telegram
    start_scheduler(app)
    start_telegram(app)

    def _heartbeat():
        # The supervisor (jarvis/supervisor.py) watches this file:
        # a live process with a stale heartbeat is a HUNG app.
        from .supervisor import write_heartbeat
        import time as _time
        while True:
            write_heartbeat(app.data_dir)
            _time.sleep(10)

    threading.Thread(target=_heartbeat, daemon=True,
                     name="jarvis-heartbeat").start()
    host = ui_bind_host(app)
    server = build_server(app, host=host, port=args.port)
    url = f"http://{host}:{args.port}/"
    print(f"JARVIS UI — {url}")
    if host != "127.0.0.1":
        print("Note: UI is reachable beyond this PC (config "
              "ui.bind_host). On a private tailnet that is the "
              "point; on an open network, set it back to "
              "127.0.0.1.")
    if not args.no_open:
        webbrowser.open(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
