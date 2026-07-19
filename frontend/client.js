"use strict";
/* FABRIK-9 frontend client — the server owns the simulation; this file
   only listens (WebSocket state at 30 fps) and renders (Canvas).
   Agent motion is smoothed client-side by easing toward the last
   authoritative cell, so network jitter never looks like teleporting. */

const TS = 20;
const DIRS = [[1,0],[-1,0],[0,1],[0,-1]];
const T = { EMPTY:0, ORE:1, ROCK:2, BELT:3, MINER:4, ASM:5 };
const ORE_PER_TILE = 400, CRAFT_TICKS = 40, CARGO_CAP = 10;
const CRAFT_IN = 2, N_PATCHES = 5;
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
    else if (msg.type === "state") { state = msg; updateHUD(); updateTimeline(msg); updateGoals(msg); updateNarration(msg); }
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

/* ---------------- decision timeline ----------------
   Task lifecycle view derived purely from data the server already sends:
   new event-log entries (deduped via log_seq) are classified into lifecycle
   stages, and each agent's decision_pick (deduped via its tick) becomes an
   AUCTION entry. No extra backend traffic. */

const TL_RULES = [
  [/break detected/,           "CREATED",  "tag-created"],
  [/FAULT INJECTED/,           "FAULT",    "tag-fault"],
  [/task aborted/,             "ABORT",    "tag-fault"],
  [/dispatched to belt fault/, "ASSIGNED", "tag-selected"],
  [/mining ore at/,            "ASSIGNED", "tag-selected"],
  [/hauling \d+ ore/,          "ASSIGNED", "tag-selected"],
  [/blueprint approved/,       "ASSIGNED", "tag-selected"],
  [/belt repaired/,            "DONE",     "tag-done"],
  [/delivered \d+ ore/,        "DONE",     "tag-done"],
  [/automation line online/,   "DONE",     "tag-done"],
];

let tlEntries = [];
let tlSeq = 0;                  // last consumed log_seq
let tlPicks = new Map();        // agent id -> tick of last seen decision_pick
let tlDirty = false;

function timelinePush(t, tag, cls, msg) {
  tlEntries.push({ t, tag, cls, msg });
  if (tlEntries.length > 120) tlEntries.splice(0, tlEntries.length - 120);
  tlDirty = true;
}

function updateTimeline(s) {
  if (s.log_seq < tlSeq) {      // run reset or snapshot load: start fresh
    tlEntries = []; tlPicks.clear(); tlDirty = true;
  }
  for (const ag of s.agents) {
    const p = ag.decision_pick;
    if (!p || tlPicks.get(ag.id) === p.tick) continue;
    tlPicks.set(ag.id, p.tick);
    // supervising at rank 0 is the constant "nothing to do" case — logging
    // it every idle cycle would drown the real decisions
    if (p.type === "SUPERVISE" && p.rank === 0) continue;
    timelinePush(p.tick, "AUCTION", "tag-auction",
      `A${ag.id+1} won auction: ${p.label} (score ${p.score}) — ${p.reason}`);
  }
  const fresh = Math.min(s.log_seq - tlSeq, s.log.length);
  if (fresh > 0) {
    for (const l of s.log.slice(-fresh)) {
      for (const [re, tag, cls] of TL_RULES) {
        if (re.test(l.msg)) { timelinePush(l.t, tag, cls, l.msg); break; }
      }
    }
  }
  tlSeq = s.log_seq;
  if (!tlDirty) return;         // only touch the DOM when something changed
  tlDirty = false;
  const el = $("timeline");
  el.innerHTML = tlEntries.map(e =>
    `<div><span class="t">[${String(Math.floor(e.t/30)).padStart(4,"0")}s]</span>` +
    `<span class="tag ${e.cls}">${e.tag}</span><span>${e.msg}</span></div>`).join("");
  el.scrollTop = el.scrollHeight;
}

/* ---------------- factory goals ----------------
   The factory's current objectives, derived entirely from state the server
   already sends: faults -> repairs, assembler buffers -> production demand,
   miner count -> automation progress, agent states -> idle capacity. */

function updateGoals(s) {
  const rows = [];
  for (const f of s.faults)
    rows.push(`<div class="goal g-red">&#9888; Repair belt at (${f.x},${f.y}) ` +
              `&mdash; down ${((s.tick - f.created) / 30).toFixed(1)}s</div>`);
  for (const a of s.assemblers)
    if (a.buffer < CRAFT_IN)
      rows.push(`<div class="goal g-amber">&#9654; Deliver ore to assembler at ` +
                `(${a.x},${a.y}) &mdash; buffer ${a.buffer}/${CRAFT_IN}</div>`);
  if (s.stats.miners < N_PATCHES)
    rows.push(`<div class="goal">&#9654; Automate remaining ore patches ` +
              `&mdash; ${s.stats.miners}/${N_PATCHES} lines built</div>`);
  else
    rows.push(`<div class="goal g-green">&#10003; All ${N_PATCHES} ore patches automated</div>`);
  rows.push(`<div class="goal">&#9654; Sustain gear production &mdash; ` +
            `${s.stats.rate}/min, ${s.stock.gears} in stock</div>`);
  const idle = s.agents.filter(a => a.state === "IDLE").length;
  if (idle)
    rows.push(`<div class="goal g-dim">&#8226; ${idle}/${s.agents.length} agents ` +
              `supervising &mdash; no higher-value work pending</div>`);
  $("goals").innerHTML = rows.join("");
}

/* ---------------- narration ----------------
   A plain-language line every 4 simulated seconds, template-filled from the
   same state the panels use — deterministic per seed, zero backend cost.
   Keyed to sim ticks (not wall clock) so it is reproducible and speed-aware. */

const NARR_EVERY = 120;      // ticks between narration updates (4 sim-seconds)
let narrLastTick = -1;

function agentWhy(ag) {
  const p = ag.decision_pick;
  return p ? ` (auction score ${p.score} — ${p.reason})` : "";
}

function composeNarration(s) {
  const f = s.faults[0];
  if (f) {
    const rep = s.agents.find(a => a.task.startsWith("repair"));
    if (rep) {
      const doing = rep.state === "REPAIR"
        ? "is repairing it on site"
        : `is en route — ${rep.path.length} tiles to go`;
      return `Integrity scan flagged a belt break at (${f.x},${f.y}). ` +
             `A${rep.id + 1} won the repair auction${agentWhy(rep)} and ${doing}.`;
    }
    return `Belt break detected at (${f.x},${f.y}) — no agent free yet; ` +
           `repair will outscore other tasks at the next auction.`;
  }
  const lines = [];
  for (const ag of s.agents) {
    const t = ag.task;
    if (t.startsWith("build"))
      lines.push(`A${ag.id + 1} is constructing an automation line ` +
                 `(placement ${t.slice(6)})${agentWhy(ag)}.`);
    else if (t.startsWith("haul"))
      lines.push(`A${ag.id + 1} is delivering ${ag.cargo} ore to an assembler` +
                 (ag.path.length ? ` — ${ag.path.length} tiles to go` : "") +
                 `${agentWhy(ag)}.`);
    else if (t.startsWith("mine"))
      lines.push(`A${ag.id + 1} is mining ore at ${t.slice(5)} — the nearest ` +
                 `unreserved deposit${agentWhy(ag)}.`);
  }
  if (!lines.length) {
    return s.stats.miners >= N_PATCHES
      ? `Factory fully automated — ${s.stats.miners} miners feeding ` +
        `${s.stats.n_assemblers} assemblers at ${s.stats.rate} gears/min; ` +
        `all agents supervising until new work appears.`
      : `No transport or repair demand right now — agents standing by ` +
        `(${s.stats.rate} gears/min).`;
  }
  return lines[Math.floor(s.tick / NARR_EVERY) % lines.length];
}

function updateNarration(s) {
  if (narrLastTick >= 0 && s.tick >= narrLastTick &&
      s.tick - narrLastTick < NARR_EVERY) return;
  narrLastTick = s.tick;
  $("narration").textContent = composeNarration(s);
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
  const agSel = s.agents[selAgent];
  const d = agSel.decision;
  const pick = agSel.decision_pick;
  if (d.length) {
    const max = Math.max(...d.map(x => x.score), 1);
    const winIdx = pick ? pick.rank : 0;
    let html = d.map((x, i) =>
      `<div class="util-row ${i===winIdx?'win':''}">
         <span class="lbl">${x.label}</span>
         <span class="ubar"><div style="width:${Math.max(0, x.score/max*100)}%"></div></span>
         <span class="num">${x.score.toFixed(0)}</span>
       </div>`).join("");
    if (pick) {
      html += `<div class="pick-why"><span class="k">CHOSEN:</span> ` +
              `<span class="win-lbl">${pick.label}</span> &mdash; ${pick.reason}` +
              pick.rejected.map(r =>
                `<br><span class="k">PASSED OVER:</span> ${r.label} ` +
                `(${r.score}) &mdash; ${r.why}`).join("") +
              `</div>`;
    }
    if (agSel.idle_reason) {
      html += `<div class="pick-why idle-why">${agSel.idle_reason}</div>`;
    }
    $("utilBox").innerHTML = html;
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
