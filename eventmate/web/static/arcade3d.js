/* EventMate Arcade — 3D floor (Three.js r128, vendored locally, MIT).
 *
 * "Our own version" of a classic game-ready upright cabinet (silhouette and
 * layout inspired by CC-BY reference cabinets the user picked on Sketchfab;
 * every mesh, texture and sprite here is generated in code - no third-party
 * art, fonts or model data).
 *
 * Realism layer: PBR materials with a procedural environment map, real
 * shadows, UnrealBloom on the emissive parts (marquee, neon, CRT), a curved
 * CRT under glass, laminate/metal/concrete canvas textures, dust in the air
 * and a hand-held camera sway. Falls back to a plain fast path (?lowfx=1 or
 * auto-degrade) that skips bloom and halves the pixel ratio.
 *
 * The cabinet screen shows the REAL pipeline as a water-cooling loop:
 * each module is a block, rigid glass tubes with metal elbow fittings connect
 * them in pipeline order, and a continuous volume of blue liquid fills each
 * tube as work passes to the next stage. Nothing here invents state: arcade.js
 * drives every light from /api/arcade/pulse (the recorded timings of the run,
 * replayed slowed and labelled).
 */
/* global THREE */
"use strict";

window.Arcade3D = (function () {
  const S = {
    ok: false, reduced: false, muted: localStorage.getItem("em-arcade-muted") === "1",
    renderer: null, scene: null, camera: null, clock: null,
    composer: null, bloom: null, dust: null,
    tweens: [], cabinet: null, marquee: null, marqueeLight: null,
    screenMesh: null, glassMesh: null, screenLight: null, lever: null, tray: null,
    attendant: null, attBody: null, attHead: null, coin: null, sheets: [], flySheet: null,
    audio: null, master: null, slotGlow: null,
    camWide: { pos: [0, 2.5, 7.3], tgt: [0, 1.72, 0] },
    camZoom: { pos: [0.02, 1.74, 1.95], tgt: [0.02, 1.72, 0.45] },
    powered: false, shaking: 0, zoomed: false, camImpulse: 0,
    swayPrev: new THREE.Vector3(),
  };

  /* ---------------- tween engine ---------------- */
  const easeInOut = (t) => t < 0.5 ? 2 * t * t : 1 - Math.pow(-2 * t + 2, 2) / 2;
  const easeOut = (t) => 1 - Math.pow(1 - t, 3);
  function tween(durMs, ease, onUpdate) {
    return new Promise((resolve) => {
      if (S.reduced || durMs <= 0) { onUpdate(1); resolve(); return; }
      S.tweens.push({ t0: performance.now(), dur: durMs, ease: ease || easeInOut, onUpdate, resolve });
    });
  }
  function stepTweens(now) {
    S.tweens = S.tweens.filter((tw) => {
      const t = Math.min(1, (now - tw.t0) / tw.dur);
      tw.onUpdate(tw.ease(t));
      if (t >= 1) { tw.resolve(); return false; }
      return true;
    });
  }
  const wait = (ms) => new Promise((r) => setTimeout(r, S.reduced ? 0 : ms));

  /* ---------------- synthesized audio (no assets; user-gesture gated) ---------------- */
  function userGesture() {
    if (S.muted) return;
    try {
      if (!S.audio) {
        const AC = window.AudioContext || window.webkitAudioContext;
        if (!AC) return;
        S.audio = new AC();
        S.master = S.audio.createGain();
        S.master.gain.value = 0.5;
        S.master.connect(S.audio.destination);
      }
      if (S.audio.state === "suspended") S.audio.resume();
    } catch (e) { /* audio unavailable: stay silent */ }
  }
  function env(node, t0, a, d, peak) {
    node.gain.setValueAtTime(0, t0);
    node.gain.linearRampToValueAtTime(peak, t0 + a);
    node.gain.exponentialRampToValueAtTime(0.0001, t0 + a + d);
  }
  function ping(freq, dur, peak) {
    if (!S.audio || S.muted) return;
    const t0 = S.audio.currentTime;
    const o = S.audio.createOscillator(), g = S.audio.createGain();
    o.type = "triangle"; o.frequency.value = freq;
    env(g, t0, 0.004, dur, peak);
    o.connect(g); g.connect(S.master);
    o.start(t0); o.stop(t0 + dur + 0.05);
  }
  function noiseBurst(dur, freq, q, peak, when) {
    if (!S.audio || S.muted) return;
    const t0 = when || S.audio.currentTime;
    const len = Math.max(1, Math.floor(S.audio.sampleRate * dur));
    const buf = S.audio.createBuffer(1, len, S.audio.sampleRate);
    const ch = buf.getChannelData(0);
    for (let i = 0; i < len; i++) ch[i] = Math.random() * 2 - 1;
    const src = S.audio.createBufferSource(); src.buffer = buf;
    const f = S.audio.createBiquadFilter(); f.type = "bandpass"; f.frequency.value = freq; f.Q.value = q;
    const g = S.audio.createGain(); env(g, t0, 0.005, dur, peak);
    src.connect(f); f.connect(g); g.connect(S.master);
    src.start(t0);
  }
  const snd = {
    clink() { ping(2400, 0.09, 0.10); setTimeout(() => ping(3100, 0.12, 0.07), 45); },
    clunk() { noiseBurst(0.08, 300, 1.2, 0.16); ping(140, 0.1, 0.08); },
    servo() { /* small motor whir for the attendant's arm */
      if (!S.audio || S.muted) return;
      const t0 = S.audio.currentTime;
      const o = S.audio.createOscillator(), g = S.audio.createGain();
      const f = S.audio.createBiquadFilter(); f.type = "bandpass"; f.frequency.value = 900; f.Q.value = 2;
      o.type = "sawtooth";
      o.frequency.setValueAtTime(150, t0);
      o.frequency.linearRampToValueAtTime(320, t0 + 0.16);
      env(g, t0, 0.01, 0.18, 0.028);
      o.connect(f); f.connect(g); g.connect(S.master);
      o.start(t0); o.stop(t0 + 0.24);
    },
    hum() { if (!S.audio || S.muted) return;
      const t0 = S.audio.currentTime;
      const o = S.audio.createOscillator(), g = S.audio.createGain();
      o.type = "sawtooth"; o.frequency.value = 68;
      g.gain.setValueAtTime(0, t0);
      g.gain.linearRampToValueAtTime(0.045, t0 + 0.3);
      g.gain.linearRampToValueAtTime(0, t0 + 1.1);
      o.connect(g); g.connect(S.master); o.start(t0); o.stop(t0 + 1.2);
    },
    cogs(durMs) { /* subtle ratchet of cogwheels under the shake */
      if (!S.audio || S.muted) return;
      const t0 = S.audio.currentTime;
      const n = Math.floor(durMs / 85);
      for (let i = 0; i < n; i++) {
        noiseBurst(0.03, 900 + Math.random() * 700, 3, 0.05, t0 + i * 0.085 + Math.random() * 0.02);
      }
      noiseBurst(durMs / 1000, 120, 0.8, 0.05, t0);
    },
    flutter() { noiseBurst(0.28, 2400, 0.7, 0.05); noiseBurst(0.2, 3600, 0.7, 0.035); },
    press() { noiseBurst(0.12, 500, 1, 0.09); },
  };

  /* ---------------- procedural textures (canvas, no fetched assets) ---------------- */
  function canvasTex(w, h, draw, opts) {
    const c = document.createElement("canvas");
    c.width = w; c.height = h;
    draw(c.getContext("2d"), w, h);
    const t = new THREE.CanvasTexture(c);
    if (opts && opts.repeat) { t.wrapS = t.wrapT = THREE.RepeatWrapping; }
    return t;
  }
  function textTexture(text, opts) {
    const o = opts || {};
    return canvasTex((o.w || 512) * 2, (o.h || 128) * 2, (x, w, h) => {
      x.fillStyle = o.bg || "#000";
      x.fillRect(0, 0, w, h);
      x.font = (o.weight || "bold") + " " + ((o.size || 64) * 2) + "px ui-monospace, Menlo, Consolas, monospace";
      x.textAlign = "center"; x.textBaseline = "middle";
      x.fillStyle = o.fg || "#ffb54d";
      x.fillText(text, w / 2, h / 2);
    });
  }
  const rnd = (() => { let s = 42; return () => (s = (s * 16807) % 2147483647) / 2147483647; })();

  /* wood-effect laminate with vertical grain + wear (cabinet shell) */
  function laminateTexture() {
    return canvasTex(512, 512, (x, w, h) => {
      x.fillStyle = "#232838"; x.fillRect(0, 0, w, h);
      for (let i = 0; i < 240; i++) {
        const gx = rnd() * w, gw = 1 + rnd() * 3, a = 0.03 + rnd() * 0.08;
        x.fillStyle = rnd() < 0.5 ? "rgba(8,10,16," + a + ")" : "rgba(52,60,82," + (a * 0.8) + ")";
        x.fillRect(gx, 0, gw, h);
      }
      for (let i = 0; i < 40; i++) { // worn scratches
        x.strokeStyle = "rgba(120,132,160," + (0.02 + rnd() * 0.05) + ")";
        x.lineWidth = 1;
        x.beginPath();
        const sx = rnd() * w, sy = rnd() * h;
        x.moveTo(sx, sy); x.lineTo(sx + (rnd() - 0.5) * 30, sy + rnd() * 90); x.stroke();
      }
    }, { repeat: true });
  }
  /* speckled dark concrete (floor) */
  function floorTexture() {
    return canvasTex(512, 512, (x, w, h) => {
      x.fillStyle = "#101319"; x.fillRect(0, 0, w, h);
      for (let i = 0; i < 2600; i++) {
        const g = 18 + rnd() * 30;
        x.fillStyle = "rgba(" + g + "," + (g + 4) + "," + (g + 10) + "," + (0.25 + rnd() * 0.5) + ")";
        x.fillRect(rnd() * w, rnd() * h, 1.6, 1.6);
      }
      for (let i = 0; i < 7; i++) { // stains
        const sx = rnd() * w, sy = rnd() * h, r = 40 + rnd() * 90;
        const g = x.createRadialGradient(sx, sy, 4, sx, sy, r);
        g.addColorStop(0, "rgba(4,6,10,0.35)"); g.addColorStop(1, "rgba(4,6,10,0)");
        x.fillStyle = g; x.beginPath(); x.arc(sx, sy, r, 0, 7); x.fill();
      }
      x.strokeStyle = "rgba(0,0,0,0.5)"; x.lineWidth = 3; // expansion joints
      x.strokeRect(0, 0, w, h);
      x.strokeStyle = "rgba(90,100,120,0.18)"; x.lineWidth = 1;
      x.strokeRect(2, 2, w - 4, h - 4);
    }, { repeat: true });
  }
  /* brushed metal (control deck) */
  function brushedMetalTexture() {
    return canvasTex(512, 256, (x, w, h) => {
      x.fillStyle = "#232a36"; x.fillRect(0, 0, w, h);
      for (let i = 0; i < 900; i++) {
        const gy = rnd() * h, gl = 20 + rnd() * 120, gx = rnd() * w;
        const v = 30 + rnd() * 46;
        x.strokeStyle = "rgba(" + v + "," + (v + 6) + "," + (v + 14) + "," + (0.12 + rnd() * 0.3) + ")";
        x.lineWidth = 1;
        x.beginPath(); x.moveTo(gx, gy); x.lineTo(gx + gl, gy); x.stroke();
      }
    }, { repeat: true });
  }
  /* dark acoustic wall panels */
  function wallTexture() {
    return canvasTex(512, 512, (x, w, h) => {
      x.fillStyle = "#0a0d14"; x.fillRect(0, 0, w, h);
      for (let px = 0; px < w; px += 128) {
        x.fillStyle = "rgba(20,26,38,0.8)"; x.fillRect(px + 3, 3, 122, h - 6);
        x.fillStyle = "rgba(4,5,9,0.9)"; x.fillRect(px, 0, 3, h);
        for (let i = 0; i < 60; i++) {
          x.fillStyle = "rgba(60,72,96," + (0.04 + rnd() * 0.08) + ")";
          x.fillRect(px + 6 + rnd() * 116, rnd() * h, 1.5, 1.5);
        }
        x.fillStyle = "rgba(90,104,130,0.5)"; // rivets
        [10, h / 2, h - 10].forEach((ry) => { x.beginPath(); x.arc(px + 8, ry, 2, 0, 7); x.fill(); });
      }
      const g = x.createLinearGradient(0, h * 0.7, 0, h); // grime at the floor line
      g.addColorStop(0, "rgba(0,0,0,0)"); g.addColorStop(1, "rgba(0,0,0,0.55)");
      x.fillStyle = g; x.fillRect(0, 0, w, h);
    }, { repeat: true });
  }
  /* cabinet side art: original EventMate vinyl wrap (no third-party art) */
  function sideArtTexture() {
    return canvasTex(640, 1600, (x, w, h) => {
      const bg = x.createLinearGradient(0, 0, 0, h);
      bg.addColorStop(0, "#141b30"); bg.addColorStop(0.55, "#0d1220"); bg.addColorStop(1, "#161126");
      x.fillStyle = bg; x.fillRect(0, 0, w, h);
      // circuit traces
      x.strokeStyle = "rgba(63,210,255,0.16)"; x.lineWidth = 3;
      for (let i = 0; i < 26; i++) {
        let px = rnd() * w, py = rnd() * h;
        x.beginPath(); x.moveTo(px, py);
        for (let s = 0; s < 4; s++) {
          if (rnd() < 0.5) px += (rnd() - 0.5) * 160; else py += (rnd() - 0.5) * 200;
          x.lineTo(px, py);
        }
        x.stroke();
        x.fillStyle = "rgba(63,210,255,0.30)";
        x.beginPath(); x.arc(px, py, 4, 0, 7); x.fill();
      }
      // coolant loop motif along the lower third
      x.save();
      x.strokeStyle = "rgba(30,159,224,0.75)"; x.lineWidth = 12; x.lineCap = "round";
      x.shadowColor = "rgba(50,190,255,0.8)"; x.shadowBlur = 18;
      x.beginPath(); x.moveTo(60, h * 0.66);
      x.bezierCurveTo(w * 0.4, h * 0.60, w * 0.6, h * 0.74, w - 60, h * 0.68);
      x.stroke();
      x.strokeStyle = "rgba(127,228,255,0.85)"; x.lineWidth = 3.5;
      x.beginPath(); x.moveTo(60, h * 0.66);
      x.bezierCurveTo(w * 0.4, h * 0.60, w * 0.6, h * 0.74, w - 60, h * 0.68);
      x.stroke();
      x.restore();
      // coin + little attendant glyph
      x.save();
      x.translate(w * 0.5, h * 0.80);
      x.fillStyle = "#22304a";
      if (x.roundRect) { x.beginPath(); x.roundRect(-46, -40, 92, 70, 14); x.fill(); }
      x.fillStyle = "#3fd2ff";
      x.beginPath(); x.arc(-18, -12, 7, 0, 7); x.fill();
      x.beginPath(); x.arc(18, -12, 7, 0, 7); x.fill();
      x.strokeStyle = "#3fd2ff"; x.lineWidth = 5;
      x.beginPath(); x.arc(0, 4, 22, 0.15 * Math.PI, 0.85 * Math.PI); x.stroke();
      x.fillStyle = "#d9a93f";
      x.beginPath(); x.arc(0, 58, 15, 0, 7); x.fill();
      x.strokeStyle = "#8a6414"; x.lineWidth = 3;
      x.beginPath(); x.arc(0, 58, 9, 0, 7); x.stroke();
      x.restore();
      // vertical logo
      x.save();
      x.translate(w * 0.5, h * 0.34);
      x.rotate(-Math.PI / 2);
      x.textAlign = "center"; x.textBaseline = "middle";
      x.font = "bold 92px ui-monospace, Menlo, Consolas, monospace";
      x.shadowColor = "rgba(63,210,255,0.9)"; x.shadowBlur = 26;
      x.fillStyle = "#bfeaff";
      x.fillText("EVENTMATE", 0, 0);
      x.shadowBlur = 0;
      x.font = "bold 30px ui-monospace, Menlo, Consolas, monospace";
      x.fillStyle = "#ffb54d";
      x.fillText("· VERIFIED PLANNING ENGINE ·", 0, 64);
      x.restore();
      // print grain
      for (let i = 0; i < 1800; i++) {
        x.fillStyle = "rgba(255,255,255," + (rnd() * 0.03) + ")";
        x.fillRect(rnd() * w, rnd() * h, 1.3, 1.3);
      }
    });
  }
  /* soft radial dot (dust + glow decals) */
  function glowDotTexture() {
    return canvasTex(64, 64, (x) => {
      const g = x.createRadialGradient(32, 32, 2, 32, 32, 30);
      g.addColorStop(0, "rgba(255,255,255,1)"); g.addColorStop(1, "rgba(255,255,255,0)");
      x.fillStyle = g; x.fillRect(0, 0, 64, 64);
    });
  }
  /* procedural environment: what the metals and glass "see" around them */
  function environmentTexture() {
    return canvasTex(1024, 512, (x, w, h) => {
      x.fillStyle = "#05070c"; x.fillRect(0, 0, w, h);
      const sky = x.createLinearGradient(0, 0, 0, h * 0.55);
      sky.addColorStop(0, "#0a1420"); sky.addColorStop(1, "#060a12");
      x.fillStyle = sky; x.fillRect(0, 0, w, h * 0.55);
      const floor = x.createLinearGradient(0, h * 0.55, 0, h);
      floor.addColorStop(0, "#0c0f16"); floor.addColorStop(1, "#030407");
      x.fillStyle = floor; x.fillRect(0, h * 0.55, w, h);
      const blob = (cx, cy, r, col) => {
        const g = x.createRadialGradient(cx, cy, 2, cx, cy, r);
        g.addColorStop(0, col); g.addColorStop(1, "rgba(0,0,0,0)");
        x.fillStyle = g; x.beginPath(); x.arc(cx, cy, r, 0, 7); x.fill();
      };
      blob(w * 0.5, h * 0.10, 190, "rgba(168,212,245,0.85)");   // cold tube light overhead
      blob(w * 0.13, h * 0.38, 130, "rgba(255,61,127,0.55)");   // magenta neon, left
      blob(w * 0.87, h * 0.38, 130, "rgba(63,210,255,0.55)");   // cyan neon, right
      blob(w * 0.5, h * 0.52, 150, "rgba(255,181,77,0.40)");    // warm marquee bounce
      blob(w * 0.5, h * 0.95, 260, "rgba(40,70,110,0.35)");     // floor sheen
    });
  }

  /* ================= the workflow screen (water-cooling loop) ================= */
  const STAGE_ORDER = ["parse", "decompose", "budget", "guests", "vendors",
    "select", "schedule", "symbolic", "validate", "store"];
  const STAGE_LABEL = { parse: "PARSE", decompose: "DECOMPOSE", budget: "BUDGET", guests: "GUESTS",
    vendors: "VENDORS", select: "SELECT", schedule: "CP-SAT", symbolic: "PDDL",
    validate: "VALIDATE", store: "STORE" };
  const SW = 2048, SH = 1280, SC = 2.0;
  const scr = { // everything drawn on the cabinet CRT
    canvas: null, ctx: null, tex: null, scan: null,
    crt: "STANDBY", nodes: {}, tubes: [], dirty: true,
  };
  // snake layout: row 1 left→right, drop at the far right, row 2 right→left
  const NODE_POS = {
    parse: [120, 210], decompose: [305, 210], budget: [490, 210], guests: [675, 210], vendors: [860, 210],
    select: [860, 452], schedule: [675, 452], symbolic: [490, 452], validate: [305, 452], store: [120, 452],
  };
  const NODE_W = 138, NODE_H = 78;

  function buildScreenModel() {
    scr.canvas = document.createElement("canvas");
    scr.canvas.width = SW; scr.canvas.height = SH;
    scr.ctx = scr.canvas.getContext("2d");
    scr.ctx.scale(SC, SC);
    STAGE_ORDER.forEach((n) => { scr.nodes[n] = { state: "waiting", ms: null }; });
    // tubes connect consecutive stages; each gets its own elbow route (unique, rigid-pipe style)
    const P = NODE_POS, hw = NODE_W / 2;
    const routes = [
      [[P.parse[0] + hw, 210], [170, 210], [170, 246], [220, 246], [220, 210], [P.decompose[0] - hw, 210]],
      [[P.decompose[0] + hw, 210], [400, 210], [400, 178], [428, 178], [428, 210], [P.budget[0] - hw, 210]],
      [[P.budget[0] + hw, 210], [560, 210], [560, 252], [596, 252], [596, 210], [P.guests[0] - hw, 210]],
      [[P.guests[0] + hw, 210], [768, 210], [768, 186], [790, 186], [790, 210], [P.vendors[0] - hw, 210]],
      [[P.vendors[0] + hw, 210], [968, 210], [968, 452], [P.select[0] + hw, 452]],
      [[P.select[0] - hw, 452], [790, 452], [790, 416], [762, 416], [762, 452], [P.schedule[0] + hw, 452]],
      [[P.schedule[0] - hw, 452], [600, 452], [600, 486], [570, 486], [570, 452], [P.symbolic[0] + hw, 452]],
      [[P.symbolic[0] - hw, 452], [410, 452], [410, 424], [382, 424], [382, 452], [P.validate[0] + hw, 452]],
      [[P.validate[0] - hw, 452], [225, 452], [225, 490], [196, 490], [196, 452], [P.store[0] + hw, 452]],
    ];
    scr.tubes = routes.map((pts) => {
      let len = 0;
      for (let i = 1; i < pts.length; i++) {
        len += Math.hypot(pts[i][0] - pts[i - 1][0], pts[i][1] - pts[i - 1][1]);
      }
      return { pts, len, fill: 0 };
    });
    // scanline pattern, drawn once
    scr.scan = document.createElement("canvas");
    scr.scan.width = SW; scr.scan.height = SH;
    const sx = scr.scan.getContext("2d");
    sx.fillStyle = "rgba(0,0,0,.10)";
    for (let y = 0; y < SH; y += 12) sx.fillRect(0, y, SW, 5);
    scr.tex = new THREE.CanvasTexture(scr.canvas);
    scr.tex.minFilter = THREE.LinearFilter;
  }

  function fitting(x, px, py, r) {
    x.beginPath(); x.arc(px, py, r, 0, Math.PI * 2);
    x.fillStyle = "#2c3644"; x.fill();
    x.strokeStyle = "#5a6c80"; x.lineWidth = 2; x.stroke();
    x.save(); x.translate(px, py);
    x.strokeStyle = "rgba(160,180,200,.55)"; x.lineWidth = 1.4;
    for (let i = 0; i < 8; i++) {
      x.rotate(Math.PI / 4);
      x.beginPath(); x.moveTo(r - 3, 0); x.lineTo(r, 0); x.stroke();
    }
    x.restore();
  }

  function drawTube(x, tb) {
    const pts = tb.pts;
    const path = () => {
      x.beginPath(); x.moveTo(pts[0][0], pts[0][1]);
      for (let i = 1; i < pts.length; i++) x.lineTo(pts[i][0], pts[i][1]);
    };
    // glass body
    path(); x.strokeStyle = "rgba(120,170,205,.28)"; x.lineWidth = 13; x.lineCap = "round"; x.stroke();
    path(); x.strokeStyle = "rgba(10,16,24,.55)"; x.lineWidth = 9; x.stroke();
    // continuous liquid volume: one solid fill front, never packets
    if (tb.fill > 0) {
      const fl = tb.len * Math.min(1, tb.fill);
      x.save();
      x.shadowColor = "rgba(50,190,255,.8)"; x.shadowBlur = 12;
      path(); x.setLineDash([fl, tb.len + 1]);
      x.strokeStyle = "#1e9fe0"; x.lineWidth = 7; x.stroke();
      path(); x.strokeStyle = "#7fe4ff"; x.lineWidth = 2.6; x.stroke();
      x.restore();
      x.setLineDash([]);
    }
    // glass highlight
    path(); x.strokeStyle = "rgba(200,230,250,.14)"; x.lineWidth = 2; x.stroke();
    // metal elbow fittings at every joint
    for (let i = 1; i < pts.length - 1; i++) fitting(x, pts[i][0], pts[i][1], 10);
    fitting(x, pts[0][0], pts[0][1], 9);
    fitting(x, pts[pts.length - 1][0], pts[pts.length - 1][1], 9);
  }

  function drawNode(x, name, t) {
    const n = scr.nodes[name];
    const [cx, cy] = NODE_POS[name];
    const w = NODE_W, h = NODE_H, lx = cx - w / 2, ly = cy - h / 2;
    const col = { waiting: "#26313e", active: "#3fd2ff", done: "#1f7ea8", failed: "#ff5a5a" }[n.state];
    x.save();
    if (n.state === "active") {
      const pulse = 0.7 + 0.3 * Math.sin(t / 130);
      x.shadowColor = "rgba(63,210,255," + (0.85 * pulse) + ")";
      x.shadowBlur = 26 * pulse;
    } else if (n.state === "failed") {
      x.shadowColor = "rgba(255,90,90,.8)"; x.shadowBlur = 20;
    }
    // waterblock body
    x.fillStyle = n.state === "waiting" ? "#0b1119" : "#0e1622";
    x.strokeStyle = col; x.lineWidth = n.state === "waiting" ? 1.5 : 2.5;
    x.beginPath();
    if (x.roundRect) x.roundRect(lx, ly, w, h, 8); else x.rect(lx, ly, w, h);
    x.fill(); x.stroke();
    x.restore();
    // acrylic top window
    x.fillStyle = n.state === "waiting" ? "rgba(40,60,80,.25)" :
      (n.state === "failed" ? "rgba(255,90,90,.22)" : "rgba(63,210,255,.30)");
    x.fillRect(lx + 6, ly + 6, w - 12, 12);
    // port fittings where tubes attach (visual anchors)
    x.fillStyle = "#2c3644";
    x.beginPath(); x.arc(lx, cy, 6, 0, Math.PI * 2); x.fill();
    x.beginPath(); x.arc(lx + w, cy, 6, 0, Math.PI * 2); x.fill();
    // label + timing
    x.textAlign = "center"; x.textBaseline = "middle";
    x.font = "bold 25px ui-monospace, Menlo, Consolas, monospace";
    x.fillStyle = n.state === "waiting" ? "#6b7f96" : (n.state === "failed" ? "#ffb0b0" : "#cfeaff");
    x.fillText(STAGE_LABEL[name], cx, cy + 8);
    x.font = "16px ui-monospace, Menlo, Consolas, monospace";
    x.fillStyle = "#5f7d9c";
    if (n.state === "done" && n.ms !== null) x.fillText(n.ms + " ms", cx, cy + 29);
    else if (n.state === "failed") x.fillText("FAILED", cx, cy + 29);
    else if (n.state === "active") x.fillText("RUNNING", cx, cy + 29);
  }

  function drawReservoir(x, doneCount) {
    const rx = 8, ry = 150, rw = 36, rh = 380;
    x.fillStyle = "#0b1119"; x.strokeStyle = "#2c3f52"; x.lineWidth = 2;
    x.beginPath();
    if (x.roundRect) x.roundRect(rx, ry, rw, rh, 12); else x.rect(rx, ry, rw, rh);
    x.fill(); x.stroke();
    const level = Math.max(0.12, 1 - doneCount / STAGE_ORDER.length * 0.85);
    const lh = (rh - 16) * level;
    x.save();
    x.shadowColor = "rgba(50,190,255,.7)"; x.shadowBlur = 10;
    x.fillStyle = "#1e9fe0";
    x.fillRect(rx + 6, ry + rh - 8 - lh, rw - 12, lh);
    x.fillStyle = "#7fe4ff"; x.fillRect(rx + 6, ry + rh - 8 - lh, rw - 12, 3);
    x.restore();
    x.fillStyle = "#4d6a84"; x.font = "14px ui-monospace, Menlo, Consolas, monospace";
    x.textAlign = "center";
    x.fillText("BRIEF", rx + rw / 2, ry - 10);
    // feed pipe: reservoir -> first block, always primed once powered
    x.strokeStyle = "rgba(120,170,205,.28)"; x.lineWidth = 13; x.lineCap = "round";
    x.beginPath(); x.moveTo(rx + rw, 210); x.lineTo(51, 210); x.stroke();
    x.save(); x.shadowColor = "rgba(50,190,255,.7)"; x.shadowBlur = 8;
    x.strokeStyle = "#1e9fe0"; x.lineWidth = 7;
    x.beginPath(); x.moveTo(rx + rw, 210); x.lineTo(51, 210); x.stroke(); x.restore();
  }

  function drawScreen(t) {
    const x = scr.ctx;
    const g = x.createLinearGradient(0, 0, 0, SH);
    g.addColorStop(0, "#05090f"); g.addColorStop(1, "#03060a");
    x.fillStyle = g; x.fillRect(0, 0, SW, SH);
    // faint PCB-ish grid
    x.strokeStyle = "rgba(40,60,84,.12)"; x.lineWidth = 1;
    for (let gy = 40; gy < SH; gy += 40) { x.beginPath(); x.moveTo(0, gy); x.lineTo(SW, gy); x.stroke(); }
    for (let gx = 40; gx < SW; gx += 40) { x.beginPath(); x.moveTo(gx, 0); x.lineTo(gx, SH); x.stroke(); }
    // header
    x.textAlign = "left"; x.textBaseline = "alphabetic";
    x.font = "bold 27px ui-monospace, Menlo, Consolas, monospace";
    x.fillStyle = "#ffb54d";
    x.fillText("EVENTMATE // PLANNING ENGINE", 28, 46);
    x.textAlign = "right";
    const crtCol = { "STANDBY": "#3a4a5c", "INSERT COIN": "#ffb54d", "RUNNING": "#3fd2ff",
      "REPLAY": "#ffb54d", "PLAN READY": "#59d68f", "JAMMED": "#ff5a5a" }[scr.crt] || "#3fd2ff";
    x.fillStyle = crtCol;
    x.font = "bold 33px ui-monospace, Menlo, Consolas, monospace";
    x.fillText(scr.crt, SW - 28, 48);
    x.strokeStyle = "#1d2836"; x.beginPath(); x.moveTo(28, 62); x.lineTo(SW - 28, 62); x.stroke();

    const doneCount = STAGE_ORDER.filter((n) => scr.nodes[n].state === "done").length;
    scr.tubes.forEach((tb) => drawTube(x, tb));
    STAGE_ORDER.forEach((n) => drawNode(x, n, t));
    drawReservoir(x, doneCount);

    x.drawImage(scr.scan, 0, 0, 1024, 640);
    // vignette
    const v = x.createRadialGradient(SW / 2, SH / 2, SH / 3, SW / 2, SH / 2, SH);
    v.addColorStop(0, "rgba(0,0,0,0)"); v.addColorStop(1, "rgba(0,0,0,.45)");
    x.fillStyle = v; x.fillRect(0, 0, SW, SH);
    scr.tex.needsUpdate = true;
  }

  /* ================= scene construction ================= */
  const mat = (c, r, m, e) => new THREE.MeshStandardMaterial({
    color: c, roughness: r === undefined ? 0.85 : r, metalness: m || 0,
    emissive: e || 0x000000,
  });
  /* bright basic material that UnrealBloom picks up (colors pushed past 1.0) */
  const glowMat = (map, boost) => new THREE.MeshBasicMaterial({
    map, color: new THREE.Color(boost || 1.6, boost || 1.6, boost || 1.6),
  });

  function shadowed(mesh, cast, receive) {
    mesh.castShadow = cast !== false;
    mesh.receiveShadow = !!receive;
    return mesh;
  }

  function buildRoom(sc) {
    sc.fog = new THREE.FogExp2(0x04060b, 0.045);
    sc.background = new THREE.Color(0x030509);
    const floorTex = floorTexture();
    floorTex.repeat.set(6, 6);
    const floor = new THREE.Mesh(new THREE.PlaneGeometry(40, 40),
      new THREE.MeshStandardMaterial({ map: floorTex, bumpMap: floorTex, bumpScale: 0.012,
        roughness: 0.5, metalness: 0.12, envMapIntensity: 0.7 }));
    floor.rotation.x = -Math.PI / 2;
    floor.receiveShadow = true;
    sc.add(floor);
    const rug = new THREE.Mesh(new THREE.CircleGeometry(1.15, 40), mat(0x120e18, 1));
    rug.rotation.x = -Math.PI / 2; rug.position.set(0, 0.005, 2.6); rug.receiveShadow = true; sc.add(rug);
    const manhole = new THREE.Mesh(new THREE.CircleGeometry(0.42, 32), mat(0x141920, 0.55, 0.55));
    manhole.rotation.x = -Math.PI / 2; manhole.position.set(-1.9, 0.005, 4.2); sc.add(manhole);
    const wallTex = wallTexture(); wallTex.repeat.set(7, 1.6);
    const wall = new THREE.Mesh(new THREE.PlaneGeometry(40, 12),
      new THREE.MeshStandardMaterial({ map: wallTex, roughness: 0.92, envMapIntensity: 0.25 }));
    wall.position.set(0, 6, -5); sc.add(wall);
    const wallL = new THREE.Mesh(new THREE.PlaneGeometry(30, 12),
      new THREE.MeshStandardMaterial({ map: wallTex, roughness: 0.92, envMapIntensity: 0.25 }));
    wallL.rotation.y = Math.PI / 2; wallL.position.set(-9, 6, 5); sc.add(wallL);
    const wallR = wallL.clone(); wallR.rotation.y = -Math.PI / 2; wallR.position.x = 9; sc.add(wallR);
    [[-2.8, -3.6, 0.7], [2.2, -3.9, 0.95]].forEach(([cx, cz, drop]) => {
      const pts = [new THREE.Vector3(cx, 5.2, cz), new THREE.Vector3(cx + 0.35, 5.2 - drop, cz + 0.15),
        new THREE.Vector3(cx - 0.15, 5.2 - drop * 1.35, cz + 0.4)];
      const cable = new THREE.Mesh(
        new THREE.TubeGeometry(new THREE.CatmullRomCurve3(pts), 20, 0.022, 6),
        mat(0x0a0c10, 0.6, 0.1));
      sc.add(cable);
    });
    const fixture = new THREE.Mesh(new THREE.BoxGeometry(2.6, 0.1, 0.24), mat(0x11161f, 0.6, 0.4));
    fixture.position.set(0, 4.55, -1.5); sc.add(fixture);
    const tube = new THREE.Mesh(new THREE.BoxGeometry(2.4, 0.06, 0.12),
      new THREE.MeshBasicMaterial({ color: new THREE.Color(1.55, 1.75, 1.95) }));
    tube.position.set(0, 4.49, -1.5); sc.add(tube);
    S.tubeLight = new THREE.PointLight(0xa8d4f5, 1.5, 20);
    S.tubeLight.position.set(0, 4.3, -1.2); sc.add(S.tubeLight);
    const glowDot = glowDotTexture();
    const addNeon = (txt, opt, pos, ry, glowCol) => {
      const sign = new THREE.Mesh(new THREE.PlaneGeometry(1.5, 0.5), glowMat(textTexture(txt, opt), 1.9));
      sign.position.set(pos[0], pos[1], pos[2]); sign.rotation.y = ry; sc.add(sign);
      const decal = new THREE.Mesh(new THREE.PlaneGeometry(3.4, 1.6),
        new THREE.MeshBasicMaterial({ map: glowDot, color: glowCol, transparent: true,
          opacity: 0.30, blending: THREE.AdditiveBlending, depthWrite: false }));
      decal.position.set(pos[0], pos[1], pos[2] - 0.06); decal.rotation.y = ry; sc.add(decal);
      const l = new THREE.PointLight(glowCol, 0.9, 9);
      l.position.set(pos[0], pos[1], pos[2] + 0.5); sc.add(l);
    };
    addNeon("プラン", { fg: "#ff3d7f", size: 72 }, [-4.4, 2.6, -3.2], 0.5, 0xff3d7f);
    addNeon("EVENT 夢", { fg: "#3fd2ff", size: 56 }, [4.4, 2.7, -3.2], -0.5, 0x3fd2ff);
    [[-3.4, -1.6, 0.35], [-4.6, -2.4, 0.5], [3.4, -1.6, -0.35], [4.6, -2.4, -0.5]].forEach(([x, z, ry]) => {
      const cab = new THREE.Mesh(new THREE.BoxGeometry(0.9, 1.9, 0.8),
        new THREE.MeshStandardMaterial({ map: laminateTexture(), color: 0x59606e, roughness: 0.85, envMapIntensity: 0.12 }));
      cab.position.set(x, 0.95, z); cab.rotation.y = ry;
      shadowed(cab); sc.add(cab);
      const scrn = new THREE.Mesh(new THREE.PlaneGeometry(0.55, 0.4),
        mat(0x0a141c, 0.4, 0, 0x06121a));
      scrn.position.set(x + Math.sin(ry) * 0.42, 1.25, z + Math.cos(ry) * 0.42);
      scrn.rotation.y = ry; sc.add(scrn);
    });
    sc.add(new THREE.AmbientLight(0x36486a, 1.3));
    const frontFill = new THREE.PointLight(0xa8c4e8, 0.55, 14);
    frontFill.position.set(0.6, 2.1, 4.6); sc.add(frontFill);
    const rim = new THREE.PointLight(0x6fa8ff, 0.55, 9);
    rim.position.set(-2.4, 3.0, -2.2); sc.add(rim);
    const key = new THREE.SpotLight(0xbfd9f0, 1.9, 20, 0.62, 0.55);
    key.position.set(1.6, 4.6, 5.6);
    key.target.position.set(0, 1.2, 0);
    key.castShadow = true;
    key.shadow.mapSize.width = key.shadow.mapSize.height = S.lowFx ? 512 : 1024;
    key.shadow.camera.near = 1.5; key.shadow.camera.far = 16;
    key.shadow.bias = -0.0004;
    sc.add(key); sc.add(key.target);
    S.keyLight = key;
    const n = 140, pos = new Float32Array(n * 3);
    for (let i = 0; i < n; i++) {
      pos[i * 3] = (rnd() - 0.5) * 9;
      pos[i * 3 + 1] = 0.2 + rnd() * 4.2;
      pos[i * 3 + 2] = (rnd() - 0.5) * 8 + 1.5;
    }
    const dg = new THREE.BufferGeometry();
    dg.setAttribute("position", new THREE.Float32BufferAttribute(pos, 3));
    S.dust = new THREE.Points(dg, new THREE.PointsMaterial({
      map: glowDot, color: 0x8fb6d8, size: 0.03, transparent: true, opacity: 0.32,
      blending: THREE.AdditiveBlending, depthWrite: false, sizeAttenuation: true,
    }));
    if (!S.lowFx) sc.add(S.dust);
  }

  /* -------- cabinet: classic upright silhouette, original EventMate art --------
     If the user-selected CC-BY model ships as /static/cabinet.glb, the GLB
     loader replaces this shell and reuses the same mount points. */
  const PROFILE = [ // (depth front+/back-, height) — the classic upright side view
    [0.42, 0.03], [0.46, 0.10], [0.46, 0.98], [0.60, 1.00], [0.60, 1.07],
    [0.32, 1.18], [0.28, 1.30], [0.16, 2.06], [0.24, 2.12], [0.28, 2.40],
    [0.16, 2.52], [-0.40, 2.52], [-0.42, 0.03],
  ];
  const CAB_W = 1.9;

  function extrudeCabinetShell() {
    const shape = new THREE.Shape();
    PROFILE.forEach(([px, py], i) => { if (i === 0) shape.moveTo(px, py); else shape.lineTo(px, py); });
    shape.closePath();
    const geo = new THREE.ExtrudeGeometry(shape, {
      depth: CAB_W, bevelEnabled: true, bevelThickness: 0.018, bevelSize: 0.018, bevelSegments: 2, steps: 1,
    });
    // normalize cap UVs to 0..1 so the side art spans the panel; mirror the far side
    const xs = PROFILE.map((p) => p[0]), ys = PROFILE.map((p) => p[1]);
    const minX = Math.min.apply(null, xs), maxX = Math.max.apply(null, xs);
    const minY = Math.min.apply(null, ys), maxY = Math.max.apply(null, ys);
    const capCount = geo.groups.length && geo.groups[0].materialIndex === 0 ? geo.groups[0].count : 0;
    const posA = geo.attributes.position, uvA = geo.attributes.uv;
    for (let i = 0; i < capCount; i++) {
      let u = (posA.getX(i) - minX) / (maxX - minX);
      const v = (posA.getY(i) - minY) / (maxY - minY);
      if (Math.abs(posA.getZ(i) - CAB_W) < 1e-6) u = 1 - u; // far cap reads correctly
      uvA.setXY(i, u, v);
    }
    geo.rotateY(-Math.PI / 2);           // extrusion axis -> world X (width)
    geo.translate(CAB_W / 2, 0, 0);
    const capMat = new THREE.MeshStandardMaterial({ map: sideArtTexture(), roughness: 0.55,
      metalness: 0.08, envMapIntensity: 0.9 });
    const edgeMat = new THREE.MeshStandardMaterial({ map: laminateTexture(), color: 0x8890a0,
      roughness: 0.6, metalness: 0.15, envMapIntensity: 0.85 });
    const shell = new THREE.Mesh(geo, [capMat, edgeMat]);
    shadowed(shell);
    return shell;
  }

  function addTMolding(cab) {
    [-1, 1].forEach((side) => {
      const pts = PROFILE.map(([px, py]) => new THREE.Vector3(side * (CAB_W / 2 + 0.012), py, px));
      const curve = new THREE.CatmullRomCurve3(pts, true, "catmullrom", 0.0);
      const tube = new THREE.Mesh(new THREE.TubeGeometry(curve, 90, 0.017, 8, true),
        new THREE.MeshStandardMaterial({ color: 0x5b3fd4, roughness: 0.32, metalness: 0.1,
          emissive: 0x241063, emissiveIntensity: 0.9, envMapIntensity: 1.1 }));
      cab.add(tube);
    });
  }

  /* curved CRT: bulged plane; the workflow texture and the glass share the curve */
  function curvedScreenGeometry(w, h, bulge) {
    const g = new THREE.PlaneGeometry(w, h, 24, 16);
    const p = g.attributes.position;
    for (let i = 0; i < p.count; i++) {
      const nx = p.getX(i) / (w / 2), ny = p.getY(i) / (h / 2);
      p.setZ(i, bulge * (1 - 0.5 * (nx * nx + ny * ny)));
    }
    g.computeVertexNormals();
    return g;
  }

  function buildCabinet(sc) {
    const cab = new THREE.Group();
    cab.add(extrudeCabinetShell());
    addTMolding(cab);

    // marquee: recessed, backlit printed acrylic under clear glass
    const marqueeFrame = new THREE.Mesh(new THREE.BoxGeometry(1.74, 0.4, 0.05),
      mat(0x0a0d13, 0.6, 0.2));
    marqueeFrame.position.set(0, 2.30, 0.31); marqueeFrame.rotation.x = -0.10; cab.add(shadowed(marqueeFrame));
    S.marquee = new THREE.Mesh(new THREE.PlaneGeometry(1.62, 0.3),
      glowMat(textTexture("EVENTMATE", { fg: "#8a6a28", w: 640, h: 128, size: 76 }), 1.05));
    S.marquee.position.set(0, 2.30, 0.345); S.marquee.rotation.x = -0.10; cab.add(S.marquee);
    const marqueeGlass = new THREE.Mesh(new THREE.PlaneGeometry(1.66, 0.34),
      new THREE.MeshStandardMaterial({ color: 0xaac4dd, transparent: true, opacity: 0.09,
        roughness: 0.06, metalness: 0.9, envMapIntensity: 1.4 }));
    marqueeGlass.position.set(0, 2.30, 0.355); marqueeGlass.rotation.x = -0.10; cab.add(marqueeGlass);
    S.marqueeLight = new THREE.PointLight(0xffb54d, 0, 5);
    S.marqueeLight.position.set(0, 2.35, 1.2); cab.add(S.marqueeLight);

    // tilted screen bay: bezel, curved CRT, curved glass, glare streak
    const bay = new THREE.Group();
    bay.position.set(0, 1.70, 0.285); bay.rotation.x = -0.157;
    const bezel = new THREE.Mesh(new THREE.BoxGeometry(1.74, 1.16, 0.09),
      new THREE.MeshStandardMaterial({ map: laminateTexture(), color: 0x39404e,
        roughness: 0.55, metalness: 0.1, envMapIntensity: 0.6 }));
    shadowed(bezel); bay.add(bezel);
    buildScreenModel();
    const scrGeo = curvedScreenGeometry(1.56, 0.975, 0.055);
    S.screenMesh = new THREE.Mesh(scrGeo, new THREE.MeshBasicMaterial({ map: scr.tex }));
    S.screenMesh.position.z = 0.048; bay.add(S.screenMesh);
    S.glassMesh = new THREE.Mesh(scrGeo.clone(),
      new THREE.MeshStandardMaterial({ color: 0x9fc0dd, transparent: true, opacity: 0.075,
        roughness: 0.05, metalness: 0.9, envMapIntensity: 1.5, depthWrite: false }));
    S.glassMesh.position.z = 0.052; bay.add(S.glassMesh);
    const glare = new THREE.Mesh(new THREE.PlaneGeometry(1.5, 0.9),
      new THREE.MeshBasicMaterial({ map: (function () { return canvasTex(128, 128, (x, w, h) => {
        const g = x.createLinearGradient(0, 0, w, h);
        g.addColorStop(0, "rgba(255,255,255,0)");
        g.addColorStop(0.42, "rgba(210,230,250,0.55)");
        g.addColorStop(0.5, "rgba(235,245,255,0.75)");
        g.addColorStop(0.58, "rgba(210,230,250,0.5)");
        g.addColorStop(1, "rgba(255,255,255,0)");
        x.fillStyle = g; x.fillRect(0, 0, w, h);
      }); })(), transparent: true, opacity: 0.10, blending: THREE.AdditiveBlending, depthWrite: false }));
    glare.position.z = 0.075; bay.add(glare);
    cab.add(bay);
    S.screenLight = new THREE.PointLight(0x3fd2ff, 0, 4);
    S.screenLight.position.set(0, 1.75, 1.1); cab.add(S.screenLight);

    // control deck: brushed metal plate with joystick + glossy buttons
    const deckTex = brushedMetalTexture();
    const deck = new THREE.Mesh(new THREE.BoxGeometry(1.72, 0.09, 0.54),
      new THREE.MeshStandardMaterial({ map: deckTex, roughness: 0.42, metalness: 0.75, envMapIntensity: 1.0 }));
    deck.position.set(0, 1.075, 0.475); deck.rotation.x = -0.36; cab.add(shadowed(deck));
    const stickBase = new THREE.Mesh(new THREE.CylinderGeometry(0.075, 0.09, 0.02, 20),
      mat(0x10141c, 0.5, 0.6));
    stickBase.position.set(-0.45, 1.14, 0.52); stickBase.rotation.x = -0.36; cab.add(stickBase);
    const stick = new THREE.Mesh(new THREE.CylinderGeometry(0.016, 0.02, 0.17, 12),
      mat(0x3a4452, 0.3, 0.85));
    stick.position.set(-0.45, 1.23, 0.545); stick.rotation.x = -0.36; cab.add(shadowed(stick));
    const ball = new THREE.Mesh(new THREE.SphereGeometry(0.048, 20, 14),
      new THREE.MeshStandardMaterial({ color: 0xc22434, roughness: 0.18, metalness: 0.05,
        envMapIntensity: 1.3 }));
    ball.position.set(-0.45, 1.325, 0.575); cab.add(shadowed(ball));
    [[0.18, 0x1f8ab0], [0.36, 0xd08a1d], [0.54, 0xa02535]].forEach(function (bc) {
      const bx = bc[0], col = bc[1];
      const rim = new THREE.Mesh(new THREE.CylinderGeometry(0.05, 0.055, 0.016, 18), mat(0x0c0f15, 0.5, 0.5));
      rim.position.set(bx, 1.135, 0.575); rim.rotation.x = -0.36; cab.add(rim);
      const btn = new THREE.Mesh(new THREE.SphereGeometry(0.042, 18, 10, 0, Math.PI * 2, 0, Math.PI * 0.45),
        new THREE.MeshStandardMaterial({ color: col, roughness: 0.15, metalness: 0.05, envMapIntensity: 1.4 }));
      btn.rotation.x = -Math.PI / 2 - 0.36;
      btn.position.set(bx, 1.145, 0.585); cab.add(shadowed(btn));
    });

    // coin door: steel panel, two slots, return button, hex bolts
    const door = new THREE.Mesh(new THREE.BoxGeometry(0.56, 0.5, 0.04),
      new THREE.MeshStandardMaterial({ map: brushedMetalTexture(), color: 0x707a88,
        roughness: 0.45, metalness: 0.8, envMapIntensity: 0.9 }));
    door.position.set(0.30, 0.58, 0.462); cab.add(shadowed(door));
    [[0.22, 0.66], [0.38, 0.66]].forEach(function (sp) {
      const slot = new THREE.Mesh(new THREE.BoxGeometry(0.045, 0.13, 0.02), mat(0x020304, 0.4));
      slot.position.set(sp[0], sp[1], 0.486); cab.add(slot);
      const frame = new THREE.Mesh(new THREE.BoxGeometry(0.09, 0.18, 0.012), mat(0x161c26, 0.5, 0.6));
      frame.position.set(sp[0], sp[1], 0.480); cab.add(frame);
    });
    const ret = new THREE.Mesh(new THREE.CylinderGeometry(0.035, 0.035, 0.02, 16),
      mat(0x8a94a4, 0.3, 0.85));
    ret.rotation.x = Math.PI / 2; ret.position.set(0.30, 0.44, 0.484); cab.add(ret);
    [[0.07, 0.36], [0.53, 0.36], [0.07, 0.80], [0.53, 0.80]].forEach(function (bp) {
      const bolt = new THREE.Mesh(new THREE.CylinderGeometry(0.014, 0.014, 0.012, 6),
        mat(0x9aa4b2, 0.3, 0.9));
      bolt.rotation.x = Math.PI / 2; bolt.position.set(bp[0], bp[1], 0.484); cab.add(bolt);
    });
    S.coinSlotPos = new THREE.Vector3(0.22, 0.66, 0.52);
    S.slotGlow = new THREE.Mesh(new THREE.PlaneGeometry(0.05, 0.14),
      new THREE.MeshBasicMaterial({ color: new THREE.Color(1.8, 1.2, 0.4), transparent: true,
        opacity: 0, blending: THREE.AdditiveBlending, depthWrite: false }));
    S.slotGlow.position.set(0.22, 0.66, 0.492); cab.add(S.slotGlow);
    const coinLabel = new THREE.Mesh(new THREE.PlaneGeometry(0.5, 0.09),
      new THREE.MeshBasicMaterial({ map: textTexture("25c · ONE BRIEF = ONE PLAN", { fg: "#7d8ea0", w: 512, h: 64, size: 34 }) }));
    coinLabel.position.set(0.30, 0.30, 0.468); cab.add(coinLabel);

    // power lever on the right flank
    S.lever = new THREE.Group();
    const leverBase = new THREE.Mesh(new THREE.BoxGeometry(0.1, 0.2, 0.08), mat(0x1a2330, 0.6, 0.4));
    S.lever.add(leverBase);
    const handle = new THREE.Mesh(new THREE.CylinderGeometry(0.02, 0.02, 0.3, 10), mat(0x8a94a4, 0.3, 0.85));
    handle.position.y = 0.17; S.lever.add(handle);
    const knob = new THREE.Mesh(new THREE.SphereGeometry(0.045, 14, 10),
      new THREE.MeshStandardMaterial({ color: 0xc22434, roughness: 0.2, envMapIntensity: 1.3 }));
    knob.position.y = 0.33; S.lever.add(knob);
    S.lever.position.set(1.0, 1.5, 0.1); S.lever.rotation.z = 0.6;
    cab.add(S.lever);

    // reward tray at the bottom front
    const trayFrame = new THREE.Mesh(new THREE.BoxGeometry(1.1, 0.34, 0.16), mat(0x05070b, 0.8));
    trayFrame.position.set(-0.25, 0.28, 0.44); cab.add(shadowed(trayFrame));
    S.tray = new THREE.Mesh(new THREE.BoxGeometry(0.92, 0.06, 0.3), mat(0x161d28, 0.45, 0.5));
    S.tray.position.set(-0.25, 0.24, 0.52); cab.add(S.tray);
    const trayLabel = new THREE.Mesh(new THREE.PlaneGeometry(0.7, 0.1),
      new THREE.MeshBasicMaterial({ map: textTexture("PLAN OUT", { fg: "#5a6c80", w: 512, h: 64, size: 40 }) }));
    trayLabel.position.set(-0.25, 0.12, 0.45); cab.add(trayLabel);
    const out = new THREE.Mesh(new THREE.BoxGeometry(0.8, 0.04, 0.06), mat(0x000000, 0.5));
    out.position.set(-0.25, 0.44, 0.46); cab.add(out);
    S.paperSlotPos = new THREE.Vector3(-0.25, 0.44, 0.52);

    sc.add(cab);
    S.cabinet = cab;
    S.bay = bay; S.marqueeFrame = marqueeFrame; S.marqueeGlass = marqueeGlass;
    S.trayFrame = trayFrame; S.trayLabel = trayLabel; S.outStrip = out;
    mountGLBCabinet(sc);
  }

  // If the user-selected CC-BY model ships as /static/cabinet.glb, mount it as
  // the cabinet shell and keep only the functional overlays from the
  // procedural stand-in (live CRT, lit marquee, lever, tray, slot targets).
  function mountGLBCabinet(sc) {
    if (!THREE.GLTFLoader) return;
    new THREE.GLTFLoader().load('/static/cabinet.glb', function (g) {
      const root = g.scene;
      root.traverse(function (o) {
        if (o.isMesh) {
          o.castShadow = true; o.receiveShadow = true;
          if (o.material) { o.material.emissiveIntensity = 0.05; o.material.envMapIntensity = 0.7; o.material.needsUpdate = true; }
        }
      });
      let box = new THREE.Box3().setFromObject(root);
      const size = box.getSize(new THREE.Vector3());
      const k = 2.52 / size.y;
      root.scale.setScalar(k);
      root.rotation.y = Math.PI / 2; // model front faces +x as shipped; bring it to +z
      box = new THREE.Box3().setFromObject(root);
      const ctr = box.getCenter(new THREE.Vector3());
      root.position.set(-ctr.x, -box.min.y, -ctr.z);
      sc.add(root); S.cabGLB = root;
      S.cabinet.children.forEach(function (ch) { ch.visible = false; });
      [S.bay, S.trayFrame, S.tray, S.trayLabel,
       S.outStrip, S.slotGlow, S.lever, S.screenLight, S.marqueeLight, S.marquee].forEach(function (o) { if (o) o.visible = true; });
      S.marquee.scale.set(0.72, 0.85, 1);
      S.marquee.position.set(0, 2.27, 0.40);
      S.marquee.rotation.x = -0.49;
      S.bay.scale.setScalar(0.52); S.bay.position.set(0, 1.56, 0.30);
      S.marqueeFrame.scale.set(0.72, 0.85, 1); S.marqueeFrame.position.set(0, 2.26, 0.30);
      S.marquee.scale.set(0.72, 0.85, 1); S.marquee.position.set(0, 2.26, 0.335);
      S.marqueeGlass.scale.set(0.72, 0.85, 1); S.marqueeGlass.position.set(0, 2.26, 0.345);
      S.lever.position.set(0.55, 1.02, 0.42);
      S.trayFrame.position.set(-0.1, 0.28, 0.50); S.tray.position.set(-0.1, 0.24, 0.58);
      S.trayLabel.position.set(-0.1, 0.12, 0.51); S.outStrip.position.set(-0.1, 0.44, 0.52);
      S.paperSlotPos.set(-0.1, 0.44, 0.58);
      S.coinSlotPos.set(0.15, 0.62, 0.55);
      S.slotGlow.position.set(0.15, 0.62, 0.55);
      fitZoom();
    }, undefined, function () { /* no GLB shipped: keep the procedural cabinet */ });
  }

  /* ================= the attendant (3D character) ================= */
  function capsule(r, len, material) {
    const g = new THREE.Group();
    const cyl = new THREE.Mesh(new THREE.CylinderGeometry(r, r, len, 12), material);
    g.add(cyl);
    const t = new THREE.Mesh(new THREE.SphereGeometry(r, 12, 8), material);
    t.position.y = len / 2; g.add(t);
    const b = new THREE.Mesh(new THREE.SphereGeometry(r, 12, 8), material);
    b.position.y = -len / 2; g.add(b);
    g.traverse(function (m) { if (m.isMesh) shadowed(m); });
    return g;
  }

  function buildAttendant(sc) {
    const g = new THREE.Group();
    const shellMat = new THREE.MeshStandardMaterial({ color: 0x2c3550, roughness: 0.32,
      metalness: 0.85, envMapIntensity: 1.15 });
    const darkMat = new THREE.MeshStandardMaterial({ color: 0x141a26, roughness: 0.5,
      metalness: 0.6, envMapIntensity: 0.8 });
    // hover base with glow ring + down light
    const base = new THREE.Mesh(new THREE.CylinderGeometry(0.17, 0.21, 0.09, 24), darkMat);
    base.position.y = 0.055; shadowed(base); g.add(base);
    const ring = new THREE.Mesh(new THREE.TorusGeometry(0.185, 0.014, 8, 32),
      new THREE.MeshBasicMaterial({ color: new THREE.Color(0.5, 1.7, 2.0) }));
    ring.rotation.x = Math.PI / 2; ring.position.y = 0.03; g.add(ring);
    const glow = new THREE.Mesh(new THREE.CircleGeometry(0.26, 24),
      new THREE.MeshBasicMaterial({ map: glowDotTexture(), color: 0x3fd2ff, transparent: true,
        opacity: 0.4, blending: THREE.AdditiveBlending, depthWrite: false }));
    glow.rotation.x = -Math.PI / 2; glow.position.y = 0.006; g.add(glow);
    const hover = new THREE.PointLight(0x3fd2ff, 0.55, 1.8);
    hover.position.y = 0.12; g.add(hover);
    // torso: lathed rounded shell
    const torsoPts = [];
    [[0.0, 0.0], [0.13, 0.0], [0.16, 0.06], [0.17, 0.16], [0.15, 0.28], [0.10, 0.35], [0.04, 0.38], [0.0, 0.385]]
      .forEach(function (p) { torsoPts.push(new THREE.Vector2(p[0], p[1])); });
    const torso = new THREE.Mesh(new THREE.LatheGeometry(torsoPts, 24), shellMat);
    torso.position.y = 0.12; shadowed(torso); g.add(torso);
    S.attBody = torso;
    const chest = new THREE.Mesh(new THREE.CircleGeometry(0.04, 16),
      new THREE.MeshBasicMaterial({ color: new THREE.Color(0.35, 1.5, 1.9) }));
    chest.position.set(0, 0.36, 0.155); g.add(chest);
    S.attChest = chest;
    // head: rounded dome + visor band + antenna
    const headG = new THREE.Group();
    const head = new THREE.Mesh(new THREE.SphereGeometry(0.135, 24, 16), shellMat);
    head.scale.set(1.05, 0.82, 0.95); shadowed(head); headG.add(head);
    const visor = new THREE.Mesh(new THREE.CylinderGeometry(0.128, 0.128, 0.045, 24, 1, true, -0.7, 1.4),
      new THREE.MeshBasicMaterial({ color: new THREE.Color(0.4, 1.6, 2.0), side: THREE.DoubleSide }));
    visor.position.y = 0.008; headG.add(visor);
    S.attVisor = visor;
    const ant = new THREE.Mesh(new THREE.CylinderGeometry(0.006, 0.006, 0.12, 6), darkMat);
    ant.position.set(0.09, 0.16, 0); headG.add(ant);
    const antTip = new THREE.Mesh(new THREE.SphereGeometry(0.015, 8, 6),
      new THREE.MeshBasicMaterial({ color: new THREE.Color(2.0, 1.4, 0.5) }));
    antTip.position.set(0.09, 0.23, 0); headG.add(antTip);
    headG.position.y = 0.62;
    g.add(headG);
    S.attHead = headG;
    // right arm: shoulder pivot, capsule segments, claw ball
    const shoulder = new THREE.Group();
    shoulder.position.set(0.19, 0.44, 0);
    const sBall = new THREE.Mesh(new THREE.SphereGeometry(0.045, 12, 8), darkMat);
    shoulder.add(sBall);
    const upper = capsule(0.032, 0.14, shellMat);
    upper.position.y = -0.09; shoulder.add(upper);
    const elbow = new THREE.Group(); elbow.position.y = -0.19; shoulder.add(elbow);
    const eBall = new THREE.Mesh(new THREE.SphereGeometry(0.036, 12, 8), darkMat);
    elbow.add(eBall);
    const fore = capsule(0.027, 0.13, shellMat);
    fore.position.y = -0.08; elbow.add(fore);
    const claw = new THREE.Mesh(new THREE.SphereGeometry(0.038, 12, 8),
      new THREE.MeshStandardMaterial({ color: 0x8a94a4, roughness: 0.25, metalness: 0.9, envMapIntensity: 1.3 }));
    claw.position.y = -0.18; shadowed(claw); elbow.add(claw);
    g.add(shoulder);
    S.attArm = { shoulder, elbow, claw };
    const armL = capsule(0.03, 0.2, shellMat);
    armL.position.set(-0.19, 0.32, 0); g.add(armL);
    g.scale.set(1.3, 1.3, 1.3);
    g.position.set(-2.6, 0, 0.6);
    sc.add(g);
    S.attendant = g;
    mountGLBCharacter();
  }

  // If the user-supplied character ships as /static/avatar.glb, it becomes
  // the attendant: the procedural robot hides and the GLB rides the same
  // group choreography. Its only clip is a run, retimed as a walk while the
  // group glides; interactions lean the whole body (no arm clip exists).
  function mountGLBCharacter() {
    if (!THREE.GLTFLoader) return;
    new THREE.GLTFLoader().load('/static/avatar.glb', function (g) {
      const root = g.scene;
      root.traverse(function (o) { if (o.isMesh) { o.castShadow = true; o.frustumCulled = false; } });
      const box = new THREE.Box3().setFromObject(root);
      const size = box.getSize(new THREE.Vector3());
      root.scale.setScalar(1.32 / size.y);
      root.position.y = 0;
      root.rotation.y = Math.PI / 2; // model ships facing -x; faceTo() assumes +z-forward
      S.attendant.add(root);
      S.character = root;
      S.attendant.children.forEach(function (ch) { if (ch !== root) ch.visible = false; });
      S.mixer = new THREE.AnimationMixer(root);
      S.anims = {};
      g.animations.forEach(function (a) {
        const key = a.name.replace(/\.\d+$/, '');
        if (!S.anims[key]) S.anims[key] = S.mixer.clipAction(a);
      });
      setAnim('Idle', 0);
      // the posed bbox is bigger than the load-time bbox: rescale from the actual Idle pose
      S.mixer.update(0.05);
      root.updateMatrixWorld(true);
      const pb = new THREE.Box3().setFromObject(root);
      const ph = pb.getSize(new THREE.Vector3()).y;
      if (ph > 0.01) root.scale.multiplyScalar(1.32 / ph);
      root.position.y = -new THREE.Box3().setFromObject(root).min.y * 0; // feet already at 0 via attendant group
    }, undefined, function () { /* keep the procedural attendant */ });
  }
  function setAnim(name, fade) {
    if (!S.anims || !S.anims[name]) return;
    const next = S.anims[name];
    if (S.curAnim === next) return;
    next.reset(); next.play();
    if (S.curAnim) next.crossFadeFrom(S.curAnim, fade == null ? 0.25 : fade, false);
    S.curAnim = next;
  }

  function faceTo(x, z) {
    const p = S.attendant.position;
    S.attendant.rotation.y = Math.atan2(x - p.x, z - p.z);
  }
  function glideTo(x, z, durMs) {
    const from = S.attendant.position.clone();
    faceTo(x, z);
    if (S.character) setAnim('Walk'); else if (S.runAction) S.runAction.paused = false;
    return tween(durMs || 900, easeInOut, function (t) {
      S.attendant.position.x = from.x + (x - from.x) * t;
      S.attendant.position.z = from.z + (z - from.z) * t;
      S.attendant.position.y = S.character ? 0 : Math.abs(Math.sin(t * Math.PI * 6)) * 0.03; // hover bob (robot only)
      if (S.attBody) S.attBody.rotation.x = Math.sin(t * Math.PI) * 0.09; // lean into the move
    }).then(function () { if (S.character) setAnim('Idle'); else if (S.runAction) S.runAction.paused = true; });
  }
  function raiseArm(toShoulder, toElbow, durMs) {
    const a = S.attArm;
    snd.servo();
    if (S.character) {
      // the supplied rig has no arm gesture clip: Point reads as the interaction tell
      if (Math.abs(toShoulder) > 0.1) setAnim('Point');
      return tween(durMs || 420, easeInOut, function () {}).then(function () { setAnim('Idle'); });
    }
    const s0 = a.shoulder.rotation.z, e0 = a.elbow.rotation.z;
    return tween(durMs || 420, easeInOut, function (t) {
      a.shoulder.rotation.z = s0 + (toShoulder - s0) * t;
      a.elbow.rotation.z = e0 + (toElbow - e0) * t;
    });
  }

  /* ================= coin + sheets ================= */
  function buildProps(sc) {
    const coinG = new THREE.Group();
    const coinFace = new THREE.Mesh(new THREE.CylinderGeometry(0.05, 0.05, 0.013, 24),
      new THREE.MeshStandardMaterial({ color: 0xd9a93f, roughness: 0.22, metalness: 0.95,
        emissive: 0x3a2a08, envMapIntensity: 1.4 }));
    coinG.add(coinFace);
    const coinRim = new THREE.Mesh(new THREE.TorusGeometry(0.048, 0.006, 8, 24),
      new THREE.MeshStandardMaterial({ color: 0xb78c2e, roughness: 0.35, metalness: 0.9, envMapIntensity: 1.2 }));
    coinRim.rotation.x = Math.PI / 2; coinG.add(coinRim);
    coinG.visible = false; sc.add(coinG);
    S.coin = coinG;
    const sheetTex = (function () {
      const c = document.createElement("canvas"); c.width = 256; c.height = 340;
      const x = c.getContext("2d");
      x.fillStyle = "#f4efe3"; x.fillRect(0, 0, 256, 340);
      x.fillStyle = "#22201a"; x.fillRect(20, 22, 150, 10);
      x.fillStyle = "#6b6350";
      for (let y = 52; y < 310; y += 16) x.fillRect(20, y, 200 - (y % 48), 5);
      return new THREE.CanvasTexture(c);
    })();
    for (let i = 0; i < 3; i++) {
      const sg = new THREE.PlaneGeometry(0.34, 0.45, 8, 8);
      const sp = sg.attributes.position;
      for (let vi = 0; vi < sp.count; vi++) { // slight paper curl
        const px = sp.getX(vi);
        sp.setZ(vi, 0.028 * Math.pow(px / 0.17, 2));
      }
      sg.computeVertexNormals();
      const sh = new THREE.Mesh(sg,
        new THREE.MeshStandardMaterial({ map: sheetTex, side: THREE.DoubleSide, roughness: 0.9 }));
      shadowed(sh);
      sh.visible = false; sc.add(sh); S.sheets.push(sh);
    }
  }

  /* ================= camera ================= */
  function camTo(dst, durMs) {
    const c = S.camera;
    const p0 = c.position.clone();
    const t0 = S.camTarget ? S.camTarget.clone() : new THREE.Vector3(0, 1.5, 0);
    const p1 = new THREE.Vector3(dst.pos[0], dst.pos[1], dst.pos[2]);
    const t1 = new THREE.Vector3(dst.tgt[0], dst.tgt[1], dst.tgt[2]);
    return tween(durMs, easeInOut, function (t) {
      c.position.lerpVectors(p0, p1, t);
      S.camTarget.lerpVectors(t0, t1, t);
    });
  }

  /* ================= sequences (the choreography the user asked for) ================= */
  // 1. power-on: the attendant crosses the floor and flips the cabinet's lever
  async function powerOn() {
    if (S.powered) return;
    S.powered = true;
    S.attChest.material.color.setRGB(0.35, 1.5, 1.9);
    await glideTo(1.35, 1.1, 1100);
    faceTo(1.0, 0.1);
    await raiseArm(-1.9, -0.4, 420);
    snd.clunk();
    const l0 = S.lever.rotation.z;
    await tween(320, easeInOut, function (t) { S.lever.rotation.z = l0 + (-0.7 - l0) * t; });
    snd.hum();
    if (S.cabGLB) {
      tween(700, easeInOut, function (tt) {
        S.cabGLB.traverse(function (o) {
          if (o.isMesh && o.material && o.material.emissiveMap) o.material.emissiveIntensity = 0.05 + tt * 1.75;
        });
      });
    }
    S.marquee.material = glowMat(textTexture("EVENTMATE", { fg: "#ffb54d", w: 640, h: 128, size: 76 }), 1.7);
    S.marqueeLight.intensity = 1.8;
    S.screenLight.intensity = 1.2;
    scr.crt = "INSERT COIN"; scr.dirty = true;
    await raiseArm(0, 0, 350);
    await glideTo(-2.6, 0.6, 1000);
    faceTo(0, 0);
  }

  // 2. coin: attendant carries it to the door, the coin arcs into the slot
  async function coinInsert() {
    await glideTo(0.85, 1.35, 900);
    faceTo(0.22, 0.4);
    await raiseArm(-2.1, -0.5, 420);
    const clawPos = new THREE.Vector3();
    S.attArm.claw.getWorldPosition(clawPos);
    S.coin.visible = true;
    S.coin.position.copy(clawPos);
    const end = S.coinSlotPos;
    snd.clink();
    await tween(650, easeOut, function (t) {
      const mx = clawPos.x + (end.x - clawPos.x) * t;
      const my = clawPos.y + (end.y - clawPos.y) * t + Math.sin(t * Math.PI) * 0.28;
      const mz = clawPos.z + (end.z - clawPos.z) * t;
      S.coin.position.set(mx, my, mz);
      S.coin.rotation.x += 0.3; S.coin.rotation.z += 0.2;
      const s = 1 - t * 0.45; S.coin.scale.set(s, s, s);
    });
    S.coin.visible = false; S.coin.scale.set(1, 1, 1);
    scr.crt = "RUNNING"; scr.dirty = true;
    await raiseArm(0, 0, 350);
    await glideTo(-2.5, 0.8, 800);
  }

  // 3. one stage of the slowed replay: block works, then liquid fills its out-tube
  async function stageRun(name, showMs, endState) {
    const idx = STAGE_ORDER.indexOf(name);
    const node = scr.nodes[name];
    if (!node) return;
    node.state = "active";
    const workMs = showMs * 0.55, fillMs = showMs * 0.45;
    await wait(workMs);
    if (endState === "failed") {
      node.state = "failed";
      scr.crt = "JAMMED"; scr.dirty = true;
      return;
    }
    node.state = "done";
    const tb = scr.tubes[idx]; // tube leaving this stage toward the next
    if (tb && idx < STAGE_ORDER.length - 1) {
      await tween(fillMs, easeInOut, function (t) { tb.fill = t; });
    }
  }

  // instant state set from a live pulse snapshot (no animation, mid-run)
  function livePulse(stages) {
    for (const st of stages) {
      const node = scr.nodes[st.name];
      if (!node) continue;
      if (node.state === "done" || node.state === "failed") continue;
      node.state = st.state === "waiting" ? "waiting" : st.state;
      node.ms = st.ms;
    }
  }
  function resetWorkflow() {
    STAGE_ORDER.forEach(function (n) { scr.nodes[n] = { state: "waiting", ms: null }; });
    scr.tubes.forEach(function (tb) { tb.fill = 0; });
  }

  // 4. completion: camera pulls back, cabinet shudders hard, sheets drop,
  //    the attendant reaches for them and the top sheet flies to the glass
  async function finishRun(ok) {
    scr.crt = ok ? "PLAN READY" : "JAMMED"; scr.dirty = true;
    await camTo(S.camWide, 1300);
    snd.cogs(1200);
    const cab = S.cabGLB || S.cabinet;
    const baseP = cab.position.clone(), baseR = cab.rotation.clone();
    await tween(1200, function (t) { return t; }, function (t) {
      const amp = (1 - t) * (ok ? 0.06 : 0.025);
      cab.position.x = baseP.x + (Math.random() - 0.5) * amp;
      cab.position.z = baseP.z + (Math.random() - 0.5) * amp;
      cab.rotation.z = baseR.z + (Math.random() - 0.5) * amp * 0.25;
      cab.rotation.x = baseR.x + (Math.random() - 0.5) * amp * 0.12;
      S.camImpulse = (1 - t) * amp * 0.7;
    });
    cab.position.copy(baseP); cab.rotation.copy(baseR);
    S.camImpulse = 0;
    snd.flutter();
    const nSheets = ok ? S.sheets.length : 1;
    for (let i = 0; i < nSheets; i++) {
      const sh = S.sheets[i];
      sh.visible = true;
      sh.position.set(S.paperSlotPos.x, S.paperSlotPos.y, S.paperSlotPos.z + 0.02);
      sh.rotation.set(-0.4, 0, (i - 1) * 0.12);
      const targetY = 0.29 + i * 0.012;
      await tween(420, easeOut, function (t) {
        sh.position.y = S.paperSlotPos.y + (targetY - S.paperSlotPos.y) * t;
        sh.rotation.x = -0.4 + (-Math.PI / 2 + 0.4) * t;
        sh.rotation.z = (i - 1) * 0.12 + Math.sin(t * 9) * 0.08 * (1 - t);
      });
    }
    await glideTo(0.55, 1.15, 900);
    faceTo(-0.25, 0.45);
    await raiseArm(-1.7, -0.6, 500);
    snd.flutter();
    const sheet = S.sheets[ok ? S.sheets.length - 1 : 0];
    S.flySheet = sheet;
    const cam = S.camera;
    const start = sheet.position.clone();
    const endP = cam.position.clone()
      .add(new THREE.Vector3(0, -0.02, -0.62).applyQuaternion(cam.quaternion));
    await tween(950, easeOut, function (t) {
      sheet.position.lerpVectors(start, endP, t);
      sheet.position.y += Math.sin(t * Math.PI) * 0.35;
      sheet.rotation.x = -Math.PI / 2 + (Math.PI / 2) * t + Math.sin(t * 12) * 0.12 * (1 - t);
      sheet.rotation.z = Math.sin(t * 8) * 0.2 * (1 - t);
      sheet.lookAt(cam.position);
    });
    sheet.rotation.set(0, 0, 0);
    sheet.lookAt(cam.position);
    snd.press();
    await wait(180);
    raiseArm(0, 0, 300);
    glideTo(-2.6, 0.6, 1000); // back to its parking spot, fire and forget
  }

  function hideSheets() {
    S.sheets.forEach(function (sh) { sh.visible = false; });
    S.flySheet = null;
  }

  /* ================= init + render loop ================= */
  function fitZoom() {
    // frame the real screen-mesh bounds with 8% padding on both dimensions
    S.scene.updateMatrixWorld(true);
    const box = new THREE.Box3().setFromObject(S.screenMesh);
    const c = box.getCenter(new THREE.Vector3());
    const wdt = box.max.x - box.min.x, hgt = box.max.y - box.min.y;
    const t = Math.tan(THREE.MathUtils.degToRad(S.camera.fov / 2));
    const d = Math.max((wdt * 1.08) / (2 * t * S.camera.aspect), (hgt * 1.08) / (2 * t));
    const dir = new THREE.Vector3(0, 0.157, 1).normalize(); // screen faces slightly up (tilt)
    S.camZoom = { pos: [c.x + dir.x * d, c.y + dir.y * d, c.z + dir.z * d + 0.05], tgt: [c.x, c.y, c.z] };
  }

  function resize() {
    if (!S.ok) return;
    const w = window.innerWidth, h = window.innerHeight, aspect = w / h;
    S.camera.aspect = aspect;
    S.camera.updateProjectionMatrix();
    S.renderer.setSize(w, h, false);
    if (S.composer) S.composer.setSize(w, h);
    S.camWide.pos = aspect < 0.75 ? [0, 2.8, 9.8] : [0, 2.5, 7.3];
    fitZoom();
  }

  /* film grain + vignette overlay (subtle; generated at runtime, no assets) */
  function filmOverlay() {
    const el = document.getElementById("film");
    if (!el) return;
    const c = document.createElement("canvas");
    c.width = 128; c.height = 128;
    const x = c.getContext("2d");
    const img = x.createImageData(128, 128);
    for (let i = 0; i < img.data.length; i += 4) {
      const v = 110 + Math.random() * 60;
      img.data[i] = img.data[i + 1] = img.data[i + 2] = v;
      img.data[i + 3] = 14;
    }
    x.putImageData(img, 0, 0);
    el.style.backgroundImage = "url(" + c.toDataURL() + ")";
  }

  function init() {
    S.reduced = window.matchMedia("(prefers-reduced-motion: reduce)").matches;
    const canvas = document.getElementById("gl");
    try {
      S.renderer = new THREE.WebGLRenderer({ canvas, antialias: true });
    } catch (e) {
      try {
        S.renderer = new THREE.WebGLRenderer({ canvas, antialias: false });
      } catch (e2) {
        window.__glError = String(e2 && e2.message || e2) + " || first: " + String(e && e.message || e);
        return false;
      }
    }
    S.lowFx = new URLSearchParams(location.search).has("lowfx");
    S.renderer.setPixelRatio(S.lowFx ? 1 : 2);
    S.renderer.shadowMap.enabled = true;
    S.renderer.shadowMap.type = THREE.PCFSoftShadowMap;
    S.scene = new THREE.Scene();
    S.camera = new THREE.PerspectiveCamera(55, 1, 0.05, 60);
    S.camTarget = new THREE.Vector3(S.camWide.tgt[0], S.camWide.tgt[1], S.camWide.tgt[2]);
    S.camera.position.set(S.camWide.pos[0], S.camWide.pos[1], S.camWide.pos[2]);
    S.clock = new THREE.Clock();
    try {
      // procedural environment: metals/glass reflect the room they stand in
      const envTex = environmentTexture();
      envTex.mapping = THREE.EquirectangularReflectionMapping;
      const pmrem = new THREE.PMREMGenerator(S.renderer);
      pmrem.compileEquirectangularShader();
      S.scene.environment = pmrem.fromEquirectangular(envTex).texture;
      buildRoom(S.scene);
      buildCabinet(S.scene);
      buildAttendant(S.scene);
      buildProps(S.scene);
    } catch (e) {
      window.__glError = "scene: " + String(e && e.message || e);
      return false;
    }
    // bloom on the emissive parts; skipped entirely on the light path
    if (!S.lowFx && THREE.EffectComposer && THREE.RenderPass && THREE.UnrealBloomPass) {
      try {
        S.composer = new THREE.EffectComposer(S.renderer);
        S.composer.addPass(new THREE.RenderPass(S.scene, S.camera));
        S.bloom = new THREE.UnrealBloomPass(
          new THREE.Vector2(window.innerWidth / 2, window.innerHeight / 2), 0.5, 0.55, 0.8);
        S.composer.addPass(S.bloom);
      } catch (e) {
        S.composer = null; S.bloom = null; // bloom is garnish: never a hard failure
      }
    }
    scr.tex.anisotropy = S.renderer.capabilities.getMaxAnisotropy();
    S.ok = true;
    fitZoom();
    resize();
    window.addEventListener("resize", resize);
    filmOverlay();
    window.__arcade = S; // debug/capture hook

    let frameNo = 0;
    const frameTimes = [];
    let lastNow = 0;
    const loop = function () {
      requestAnimationFrame(loop);
      frameNo++;
      const now = performance.now();
      // auto-degrade on slow devices: sustained >34ms average drops to the
      // light path (pixelRatio 1, no bloom, throttled CRT redraw)
      if (!S.lowFx && !S.fxChecked) {
        if (lastNow) frameTimes.push(now - lastNow);
        lastNow = now;
        if (frameTimes.length >= 90) {
          S.fxChecked = true;
          const tail = frameTimes.slice(15);
          const avg = tail.reduce(function (a, b) { return a + b; }, 0) / tail.length;
          if (avg > 34) {
            S.lowFx = true;
            S.composer = null; S.bloom = null;
            S.renderer.setPixelRatio(1);
            S.renderer.setSize(window.innerWidth, window.innerHeight, false);
            if (S.dust) S.dust.visible = false;
          }
        }
      }
      stepTweens(now);
      const dt = S.clock.getDelta();
      const t = S.clock.elapsedTime * 1000;
      if (S.mixer) S.mixer.update(dt || 0.016);
      // standby screen is static: redraw only when something changed
      if (!S.powered && !scr.dirty) { /* skip CRT redraw */ }
      else if (!S.lowFx || frameNo % 3 === 0) { drawScreen(t); scr.dirty = false; }
      if (S.lowFx && frameNo % 2 === 1) return;
      // cold tube-light flicker
      if (S.tubeLight) {
        const f = Math.random() < 0.03 ? 0.35 : 1;
        S.tubeLight.intensity = 1.5 * f * (0.92 + 0.08 * Math.sin(t / 90));
      }
      if (S.screenLight && S.powered) {
        S.screenLight.intensity = 1.2 + 0.08 * Math.sin(t / 210);
      }
      if (S.slotGlow) {
        S.slotGlow.material.opacity = (S.powered && scr.crt === "INSERT COIN")
          ? 0.35 + 0.3 * Math.sin(t / 300) : 0;
      }
      if (S.attVisor) S.attVisor.material.color.setRGB(0.4, 1.6, 2.0);
      if (S.attVisor && Math.sin(t / 1700) > 0.96) S.attVisor.material.color.setRGB(0.12, 0.5, 0.7);
      if (S.attHead) S.attHead.rotation.y = Math.sin(t / 2400) * 0.3; // idle scan
      if (S.dust && S.dust.visible) {
        S.dust.rotation.y = t * 0.00004;
        S.dust.position.y = Math.sin(t * 0.00005) * 0.12;
      }
      // hand-held sway + impact impulse, without fighting the camera tweens
      if (!S.reduced) {
        const amp = (S.zoomed ? 0.006 : 0.018);
        const sway = new THREE.Vector3(
          Math.sin(t * 0.0005) * amp + (Math.random() - 0.5) * S.camImpulse,
          Math.cos(t * 0.00037) * amp * 0.7 + (Math.random() - 0.5) * S.camImpulse,
          0);
        S.camera.position.sub(S.swayPrev).add(sway);
        S.swayPrev.copy(sway);
      }
      S.camera.lookAt(S.camTarget);
      if (S.composer) S.composer.render();
      else S.renderer.render(S.scene, S.camera);
    };
    loop();
    return true;
  }

  return {
    init: init, resize: resize, userGesture: userGesture,
    powerOn: powerOn, coinInsert: coinInsert, stageRun: stageRun,
    livePulse: livePulse, resetWorkflow: resetWorkflow, finishRun: finishRun, hideSheets: hideSheets,
    setCRT: function (t) { scr.crt = t; scr.dirty = true; },
    setStageMs: function (name, ms) { const n = scr.nodes[name]; if (n) n.ms = ms; },
    zoomIn: function () { S.zoomed = true; fitZoom(); return camTo(S.camZoom, 1400); },
    zoomOut: function () { S.zoomed = false; return camTo(S.camWide, 1100); },
    setMuted: function (m) {
      S.muted = m;
      localStorage.setItem("em-arcade-muted", m ? "1" : "0");
      if (m && S.audio) S.audio.suspend(); else if (!m && S.audio) S.audio.resume();
    },
    isMuted: function () { return S.muted; },
  };
})();
