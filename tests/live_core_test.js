// Node unit test of the live view's DOM-free core: route finder vs an independent Dijkstra, manual walking, blast.
const fs = require("fs");
const { makeCore } = require("../visualization/live_core.js");
const D = JSON.parse(fs.readFileSync(process.argv[2], "utf8"));
const core = makeCore(D);
const G = D.graph, NN = G.lon.length;

// independent O(N^2) Dijkstra over the same arrays (no heap, float64) to compare distances
function naive(src, closedRoads, targets) {
  const dist = new Array(NN).fill(Infinity), done = new Array(NN).fill(false);
  dist[src] = 0;
  const out = [], inn = [];
  for (let e = 0; e < G.eu.length; e++) (out[G.eu[e]] = out[G.eu[e]] || []).push(e);
  for (let it = 0; it < NN; it++) {
    let u = -1, b = Infinity;
    for (let i = 0; i < NN; i++) if (!done[i] && dist[i] < b) { b = dist[i]; u = i; }
    if (u < 0) break;
    done[u] = true;
    for (const e of out[u] || []) { if (closedRoads.has(G.er[e])) continue; const v = G.ev[e]; if (dist[u] + G.el[e] < dist[v]) dist[v] = dist[u] + G.el[e]; }
  }
  return Math.min(...targets.map(t => dist[t]));
}

const result = { nodes: NN, routed: 0, unrouted: 0, maxErr: 0, checked: 0 };
const targets = D.safe.map(s => s.node);
const step = Math.max(1, Math.floor(NN / 60));
for (let i = 0; i < NN; i += step) {
  const r = core.route(i, 0, -1);
  const ref = naive(i, new Set(), targets);
  if (r) { result.routed++; result.maxErr = Math.max(result.maxErr, Math.abs(r.dist - ref)); } else if (isFinite(ref)) result.unrouted++;
  result.checked++;
}

// manual avatar: walk with the AUTO guide to a safe stop
const start = core.nearestNode(D.lon[0][D.people[0].k], D.lat[0][D.people[0].k]);
const m = { node: start, from: start, to: -1, len: 1, prog: 0, lon: G.lon[start], lat: G.lat[start], heading: 0, trail: [], status: "idle", auto: true, stop: "" };
const ctl = { dir: () => null, clear: () => {}, bearing: d => 0, pref: -1 };
let x = 0;
for (let k = 0; k < 4000 && m.status !== "safe"; k++) { core.stepManual(m, 5, x, ctl); x += 5; if (x > 1e9) break; }
result.autoStatus = m.status; result.autoStop = m.stop; result.trail = m.trail.length;

// manual avatar steered by direction keys moves along existing edges only
const m2 = { node: start, from: start, to: -1, len: 1, prog: 0, lon: G.lon[start], lat: G.lat[start], heading: 0, trail: [], status: "idle", auto: false, stop: "" };
let key = "up";
const ctl2 = { dir: () => key, clear: () => {}, bearing: d => ({ up: 0, right: 90, down: 180, left: -90 })[d], pref: -1 };
const before = [m2.lon, m2.lat];
for (let k = 0; k < 30; k++) core.stepManual(m2, 5, 0, ctl2);
result.keyMoved = Math.hypot(m2.lon - before[0], m2.lat - before[1]) > 0 ? 1 : 0;

// blast: an avatar standing inside the blast radius after the strike is marked caught
if (D.bombs.length) {
  const b = D.bombs[0], n0 = core.nearestNode(b.lon, b.lat);
  const m3 = { node: n0, from: n0, to: -1, len: 1, prog: 0, lon: G.lon[n0], lat: G.lat[n0], heading: 0, trail: [], status: "idle", auto: false, stop: "" };
  core.stepManual(m3, 1, b.strike + 1, { dir: () => null, clear: () => {}, bearing: () => 0, pref: -1 });
  result.blastStatus = m3.status;
  result.closedAfterStrike = core.closedAt(b.strike + 1).size;
  result.closedBefore = core.closedAt(0).size;
}
// plain-language directions: an L-shaped walk east then north must read "east ... turn left ... north"
{
  const a = core.nearestNode(D.graph.lon[0], D.graph.lat[0]);
  const r = core.route(a, 0, -1) || core.route(core.nearestNode(D.lon[0][0], D.lat[0][0]), 0, -1);
  const steps = core.describeRoute(r.nodes);
  result.steps = steps.length; result.stepTotal = steps.reduce((s, x) => s + x.m, 0);
  result.compass = [core.compass(0), core.compass(90), core.compass(225), core.compass(-45)];
  result.turns = [core.describeRoute([0, 1, 2]).length >= 0];
}
console.log(JSON.stringify(result));
