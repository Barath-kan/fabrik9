"use strict";
/* FABRIK-9 frontend client — the server owns the simulation; this file
   only listens (WebSocket state at 30 fps) and renders (Canvas).
   Agent motion is smoothed client-side by easing toward the last
   authoritative cell, so network jitter never looks like teleporting. */

const TS = 20;
const DIRS = [[1,0],[-1,0],[0,1],[0,-1]];
const T = { EMPTY:0, ORE:1, ROCK:2, BELT:3, MINER:4, ASM:5 };
const ORE_PER_TILE = 400, CRAFT_TICKS = 40, CARGO_CAP = 10;
const AGENT_COLORS = ["#35d0e0", "#b07af0", "#a4e05a"];

const cv = document.getElementById("cv");
const ctx = cv.getContext("2d");
const $ = id => document.getElementById(id);

let world = null;            // {grid, cols, rows, seed, version}
let state = null;            // latest server state message
let smooth = new Map();      // agent id -> {px, py}
let selAgent = 0;
let ws = null;

/* ---------------- WebSocket ---------------- */

function connect() {
  const proto = location.protocol === "https:" ? "wss" : "ws";
  ws = new WebSocket(`${proto}://${location.host}/ws`);
  ws.onopen = () => setConn(true);
  ws.onclose = () => { setConn(false); setTimeout(connect, 1000); };
  ws.onmessage = e => {
    const msg = JSON.parse(e.data);
    if (msg.type === "world") { world = msg; $("seedLbl").textContent = msg.seed; }
    else if (msg.type === "state") { state = msg; updateHUD(); }
  };
}
function setConn(ok) {
  const b = $("connBadge");
  b.textContent = ok ? "\u25CF LIVE " : "\u25CF RECONNECTING ";
  b.style.color = ok ? "var(--green)" : "var(--red)";
}
function send(action, value) {
  if (ws && ws.readyState === 1) ws.send(JSON.stringify({ action, value }));
}

/* ---------------- render loop ---------------- */

function draw() {
  requestAnimationFrame(draw);
  ctx.fillStyle = "#0a0e13";
  ctx.fillRect(0, 0, cv.width, cv.height);
  if (!world) return;
  const tick = state ? state.tick : 0;

  ctx.fillStyle = "#141c26";
  for (let y = 0; y < world.rows; y++) for (let x = 0; x < world.cols; x++)
    ctx.fillRect(x*TS + TS/2, y*TS + TS/2, 1, 1);

  for (let y = 0; y < world.rows; y++) for (let x = 0; x < world.cols; x++) {
    const [t, d, amt] = world.grid[y][x];
    const px = x*TS, py = y*TS;
    if (t === T.ROCK) {
      ctx.fillStyle = "#232d3a"; ctx.fillRect(px+2, py+2, TS-4, TS-4);
      ctx.fillStyle = "#2c3846"; ctx.fillRect(px+5, py+5, TS-10, TS-10);
    } else if (t === T.ORE) {
      const f = amt / ORE_PER_TILE;
      ctx.fillStyle = "#3a2c14"; ctx.fillRect(px+1, py+1, TS-2, TS-2);
      ctx.fillStyle = `rgba(232,163,61,${0.35 + 0.6*f})`;
      ctx.fillRect(px+4, py+4, 5, 5);
      ctx.fillRect(px+11, py+9, 4, 4);
      ctx.fillRect(px+6, py+12, 4, 4);
    } else if (t === T.BELT) {
      ctx.fillStyle = "#1b232e"; ctx.fillRect(px+1, py+1, TS-2, TS-2);
      ctx.strokeStyle = "#39485c"; ctx.strokeRect(px+1.5, py+1.5, TS-3, TS-3);
      const [dx, dy] = DIRS[d];
      const phase = (tick % 24) / 24;
      ctx.fillStyle = "#e8a33d";
      const cx = px + TS/2 + dx * (phase - 0.5) * (TS - 8);
      const cyy = py + TS/2 + dy * (phase - 0.5) * (TS - 8);
      ctx.beginPath();
      ctx.moveTo(cx + dx*4, cyy + dy*4);
      ctx.lineTo(cx - dx*2 + dy*3, cyy - dy*2 + dx*3);
      ctx.lineTo(cx - dx*2 - dy*3, cyy - dy*2 - dx*3);
      ctx.fill();
    } else if (t === T.MINER) {
      ctx.fillStyle = "#2a3a2e"; ctx.fillRect(px+1, py+1, TS-2, TS-2);
      ctx.strokeStyle = "#57d98a"; ctx.strokeRect(px+2.5, py+2.5, TS-5, TS-5);
      ctx.fillStyle = "#57d98a";
      const w = 3 + 2*Math.sin(tick*0.2);
      ctx.fillRect(px + TS/2 - w/2, py + TS/2 - w/2, w, w);
    } else if (t === T.ASM) {
      ctx.fillStyle = "#1c2a3f"; ctx.fillRect(px, py, TS, TS);
      ctx.strokeStyle = "#4d7dc4"; ctx.lineWidth = 1.5;
      ctx.strokeRect(px+1.5, py+1.5, TS-3, TS-3);
      ctx.lineWidth = 1;
    }
  }
  if (!state) return;

  for (const a of state.assemblers) {
    ctx.strokeStyle = "#35d0e0";
    ctx.beginPath();
    ctx.arc(a.x*TS+TS/2, a.y*TS+TS/2, 6, -Math.PI/2,
            -Math.PI/2 + (a.progress/CRAFT_TICKS)*Math.PI*2);
    ctx.stroke();
  }

  for (const f of state.faults) {
    const px = f.x*TS, py = f.y*TS;
    const a = 0.5 + 0.5*Math.sin(tick*0.3);
    ctx.strokeStyle = `rgba(224,96,96,${a})`;
    ctx.lineWidth = 2;
    ctx.beginPath();
    ctx.moveTo(px+4, py+4); ctx.lineTo(px+TS-4, py+TS-4);
    ctx.moveTo(px+TS-4, py+4); ctx.lineTo(px+4, py+TS-4);
    ctx.stroke();
    ctx.lineWidth = 1;
  }

  for (const [x, y, prog] of state.belt_items) {
    const [t, d] = world.grid[y][x];
    if (t !== T.BELT) continue;
    const [dx, dy] = DIRS[d];
    const ix = (x + 0.5 + dx * (prog - 0.5)) * TS;
    const iy = (y + 0.5 + dy * (prog - 0.5)) * TS;
    ctx.fillStyle = "#e8a33d"; ctx.fillRect(ix-3, iy-3, 6, 6);
    ctx.strokeStyle = "#7a5a22"; ctx.strokeRect(ix-3.5, iy-3.5, 7, 7);
  }

  for (const ag of state.agents) {
    let s = smooth.get(ag.id);
    if (!s) { s = { px: ag.x, py: ag.y }; smooth.set(ag.id, s); }
    s.px += (ag.x - s.px) * 0.35;
    s.py += (ag.y - s.py) * 0.35;
    const color = AGENT_COLORS[ag.id];
    const sel = ag.id === selAgent;

    if (ag.path.length) {
      ctx.strokeStyle = sel ? color : hexA(color, 0.22);
      ctx.setLineDash([4, 4]);
      ctx.beginPath();
      ctx.moveTo((s.px+0.5)*TS, (s.py+0.5)*TS);
      for (const [px2, py2] of ag.path) ctx.lineTo((px2+0.5)*TS, (py2+0.5)*TS);
      ctx.stroke();
      ctx.setLineDash([]);
      if (sel) {
        const g = ag.path[ag.path.length-1];
        ctx.strokeStyle = color;
        ctx.strokeRect(g[0]*TS+2.5, g[1]*TS+2.5, TS-5, TS-5);
      }
    }

    const ax = (s.px+0.5)*TS, ay = (s.py+0.5)*TS;
    ctx.fillStyle = hexA(color, sel ? 0.22 : 0.12);
    ctx.beginPath(); ctx.arc(ax, ay, 10, 0, Math.PI*2); ctx.fill();
    ctx.fillStyle = color;
    ctx.beginPath(); ctx.arc(ax, ay, 6, 0, Math.PI*2); ctx.fill();
    ctx.fillStyle = "#0a0e13";
    ctx.beginPath();
    ctx.arc(ax + ag.facing[0]*3, ay + ag.facing[1]*3, 2, 0, Math.PI*2);
    ctx.fill();
    ctx.fillStyle = color;
    ctx.font = "9px monospace";
    ctx.fillText(String(ag.id+1), ax - 2.5, ay - 9);
    if (ag.cargo > 0) { ctx.fillStyle = "#e8a33d"; ctx.fillRect(ax+5, ay-9, 5, 5); }
    if (["MINE","BUILD","REPAIR"].includes(ag.state)) {
      ctx.strokeStyle = ag.state === "MINE" ? "#e8a33d"
                       : ag.state === "REPAIR" ? "#e06060" : "#57d98a";
      const r = 8 + 3*Math.sin(tick*0.5);
      ctx.beginPath(); ctx.arc(ax, ay, r, 0, Math.PI*2); ctx.stroke();
    }
  }
}

function hexA(hex, a) {
  const r = parseInt(hex.slice(1,3),16), g = parseInt(hex.slice(3,5),16), b = parseInt(hex.slice(5,7),16);
  return `rgba(${r},${g},${b},${a})`;
}

/* ---------------- HUD ---------------- */

const STATE_ABBR = { PLAN:"PLAN", MOVE:"MOVE", MINE:"MINE", DELIVER:"HAUL",
                     BUILD:"BUILD", REPAIR:"FIX", IDLE:"IDLE" };

function updateHUD() {
  const s = state;
  $("squad").innerHTML = s.agents.map(ag =>
    `<button class="${ag.id===selAgent?'sel':''}" data-agent="${ag.id}">
       <span class="aid c${ag.id}">A${ag.id+1}</span>
       <span class="ast">${STATE_ABBR[ag.state] || ag.state}</span>
       <span class="atask">${ag.task}</span>
     </button>`).join("");

  $("selLbl").textContent = "A" + (selAgent + 1);
  const d = s.agents[selAgent].decision;
  if (d.length) {
    const max = Math.max(...d.map(x => x.score), 1);
    $("utilBox").innerHTML = d.map((x, i) =>
      `<div class="util-row ${i===0?'win':''}">
         <span class="lbl">${x.label}</span>
         <span class="ubar"><div style="width:${Math.max(0, x.score/max*100)}%"></div></span>
         <span class="num">${x.score.toFixed(0)}</span>
       </div>`).join("");
  }

  $("hGears").textContent = s.stock.gears;
  $("hRate").textContent = s.stats.rate;
  $("hOre").textContent = s.stock.ore_delivered;
  $("hAsm").textContent = s.stats.n_assemblers;
  $("hMin").textContent = s.stats.miners;
  $("hBelt").textContent = world ? world.grid.flat().filter(t => t[0] === T.BELT).length : 0;
  $("hTasks").textContent = s.stats.tasks_done;
  $("hTick").textContent = s.tick;
  $("hFaults").textContent = s.faults.length;
  $("hFixed").textContent = s.stats.faults_fixed;
  $("hMTTR").textContent = s.stats.mttr_s !== null ? s.stats.mttr_s + "s" : "\u2014";
  $("hUp").textContent = s.stats.uptime + "%";
  $("hRuns").textContent = s.stats.astar_runs;
  $("hNodes").textContent = s.stats.nodes;
  $("hHeap").textContent = s.stats.heap_ops;
  $("hPath").textContent = "\u2014";

  $("hudLog").innerHTML = s.log.map(l =>
    `<div><span class="t">[${String(Math.floor(l.t/30)).padStart(4,"0")}s]</span>` +
    `<span class="${l.cls}">${l.msg}</span></div>`).join("");

  $("btnPause").textContent = s.paused ? "RESUME" : "PAUSE";
  $("btnChaos").textContent = s.chaos ? "CHAOS: ON" : "CHAOS: OFF";
  document.querySelectorAll(".speed button").forEach(b =>
    b.classList.toggle("active", +b.dataset.speed === s.speed));
}

/* ---------------- controls ---------------- */

$("btnPause").addEventListener("click", () =>
  send(state && state.paused ? "resume" : "pause"));
$("btnFault").addEventListener("click", () => send("inject_fault"));
$("btnChaos").addEventListener("click", () =>
  send("chaos", !(state && state.chaos)));
document.querySelectorAll(".speed button").forEach(b =>
  b.addEventListener("click", () => send("speed", +b.dataset.speed)));
$("squad").addEventListener("click", e => {
  const b = e.target.closest("button[data-agent]");
  if (b) selAgent = +b.dataset.agent;
});

$("btnReset").addEventListener("click", async () => {
  const seed = prompt("Seed (blank = keep current):",
                      world ? world.seed : "1337");
  if (seed === null) return;
  await fetch("/api/runs", { method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ seed: seed ? +seed : world.seed }) });
});
$("btnSeed").addEventListener("click", () =>
  fetch("/api/runs", { method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({}) }));

$("btnSave").addEventListener("click", async () => {
  const label = prompt("Snapshot label:", "checkpoint") || "";
  const r = await fetch("/api/runs/current/snapshots", { method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ label }) });
  const j = await r.json();
  alert(`Saved snapshot at tick ${j.tick}`);
  refreshSnapshots();
});

async function refreshSnapshots() {
  const r = await fetch("/api/snapshots");
  const list = await r.json();
  $("loadSel").innerHTML = '<option value="">LOAD SNAPSHOT\u2026</option>' +
    list.map(s => `<option value="${s.id}">t=${s.tick} seed ${s.seed} ${s.label}</option>`).join("");
}
$("loadSel").addEventListener("focus", refreshSnapshots);
$("loadSel").addEventListener("change", async e => {
  if (!e.target.value) return;
  await fetch(`/api/snapshots/${e.target.value}/load`, { method: "POST" });
  smooth.clear();
  e.target.value = "";
});

connect();
refreshSnapshots();
requestAnimationFrame(draw);
