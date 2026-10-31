# EventMate Arcade UI (revision 2 — cinematic 3D)

Process map for the `/arcade` page: a 3D arcade floor with a working cabinet, an
attendant character, and a chat. Every moving part below maps to real pipeline
code — nothing on screen claims a stage happened that did not.

## What the user sees, in order

1. **Room powers on** — fluorescent light flickers, the cabinet marquee lights up
   (`EVENTMATE // PLANNING ENGINE`), and the attendant character (a small rounded
   robot on a pedestal) wakes and turns to face the cabinet.
2. **Dormant loop inside the cabinet screen** — before any coin, the CRT runs the
   real workflow loop slowly and *uniquely*: parse -> decompose -> optimize ->
   verify -> dossier, as a water-cooled circuit (rigid tubes, elbow fittings, a
   BRIEF reservoir on the left that drains and refills). Continuous liquid, never
   dashed packets. Module state comes only from `/api/arcade/pulse`.
3. **Chat stays a chat** — the intake conversation happens in the normal chat
   overlay at the bottom. It drives the same NLU/intake as the classic UI.
4. **Coin only after confirmation** — the BUILD MY PLAN button is the coin. The
   attendant presses it, a coin arcs into the slot, the machine accepts it, and
   the camera pushes in until the CRT is nearly full-screen.
5. **Stage run with liquid fuel** — each stage's real duration (from the pulse
   timestamps) fills the active module's tube with liquid blue; the reservoir
   level drains as the run consumes it. `REPLAY` labeling applies whenever the
   run is a replay of recorded timings (slowed for watchability) — shown on the
   CRT header and in a fixed banner.
6. **Completion** — camera pulls back, the cabinet shakes strongly (subtle
   cogwheel audio), and sheets drop into the bottom tray. The attendant reaches
   for them, and the top sheet flies off and presses against the glass in front
   of the camera — that pressed sheet is the real, readable dossier (or the
   honest PAPER JAM slip on failure).
7. **Failure path** — an infeasible brief jams: the slip lists real violations
   and suggested repairs, a red PAPER JAM stamp, and any in-progress replay is
   stopped honestly rather than looping a fake success.

## Element -> code map

| On screen | Backed by |
|---|---|
| Module lights / stage order / stage durations | `GET /api/arcade/pulse` -> `eventmate/arcade_pulse.py`, which records real planner hooks (`eventmate/agents/planner.py`, `eventmate/agents/manager.py`) |
| Vendor table, budget split, schedule, check counts | The real plan JSON returned by the intake/build API (same engine as the classic UI) |
| Dossier stamp | `verification_label()` — amber CHECKED unless every check including the symbolic solver passed; never a blanket VERIFIED |
| Symbolic (PDDL) ribbon | Shown only when the solver actually ran; otherwise an explicit "unavailable - NOT symbolically confirmed" warning box |
| PAPER JAM slip | Real constraint violations + repair suggestions from the planner response |
| REPLAY banner | Set only when the scene replays recorded stage timings rather than live pulses |

## 3D implementation

- `eventmate/web/static/arcade3d.js` — the whole scene (room, cabinet, attendant,
  coin, sheets, CRT canvas texture with snake-routed rigid tubes, elbow fittings,
  reservoir, camera tweens, WebAudio-synthesized sound). No build step.
- `eventmate/web/static/three.min.js` — Three.js r128, MIT license, vendored
  locally so the page is CSP-safe and offline-capable.
- `eventmate/web/static/arcade.js` — chat/intake/dossier/jam logic; calls into
  the scene (`Arcade3D.powerOn / coinInsert / stageRun / finishRun / livePulse /
  resetWorkflow / zoomIn / setStageMs`).
- `eventmate/web/static/arcade.html`, `arcade.css` — page shell, chat overlay,
  paper dossier, replay banner, sound toggle, WebGL fallback message.
- `web/app.py` — `STATIC_FILES` gains `arcade3d.js` and `three.min.js` (flat
  names; the static route does not allow subdirectories).

## Honesty guarantees (load-bearing)

- Module lights come only from recorded pulse events; if the pulse feed is empty
  the screen says so instead of inventing progress.
- Replays are labeled REPLAY on the CRT header and in a fixed banner, and state
  that timings are recorded and slowed.
- Amber CHECKED / NEEDS VERIFICATION semantics are unchanged from
  `verification_label()`; the symbolic-check ribbon appears only on a real run.
- The dossier footer still states the plan books nothing, commits nothing, sends
  nothing, pays nothing.
- Failure renders the real violation list; the UI never converts a jam into a
  success animation.

## Accessibility and sound

- `prefers-reduced-motion: reduce` disables camera tween easing and paper press
  animation (scene jumps between poses); the CSS press animation is removed.
- Audio is synthesized in-page (WebAudio) and starts only after the user clicks
  BUILD MY PLAN; a SOUND toggle in the chat header mutes everything.

## Running it

    python -m eventmate.web
    # then open http://127.0.0.1:8765/arcade

Seed data has Pune venues for 2026-12-12 and 2026-12-19. Example brief:
"engagement party for 120 guests in Pune on 2026-12-12 with budget 800000
rupees, catering and photography". An infeasible brief (e.g. 20000 rupees for
200 guests) exercises the jam path.

## Performance

- Default render is 2x supersampled for crisp CRT text. A frame-time monitor
  measures the first ~90 frames; sustained averages above 34 ms automatically
  drop the scene to the light path (pixelRatio 1, CRT redraw throttled), no URL
  flag or user action needed. `?lowfx=1` forces the light path immediately
  (used for software-GL verification).
- The standby CRT is static and is not redrawn until state changes; rendering
  pauses with the tab (requestAnimationFrame), so an idle hidden tab costs
  nothing.
- Measured in this sandbox (software rasterizer, no GPU): with the light path
  the page stays responsive - intake round-trip ~1.5 s after send, review card
  immediate. Do not read real-GPU performance from these numbers; they only
  evidence layout and behavior.

## Known limits

- Dev-machine preview may lack Fast Downward / VAL; in that case the dossier
  honestly shows the symbolic check as unavailable (amber), by design.
- The classic UI at `/` still owns outreach, ideas and jobs; the arcade only
  visualizes planning.
- The scene targets desktop WebGL; mobile renders the same scene at reduced
  resolution with the chat overlay collapsing to a header bar.


Credits: cabinet model by Snji, attendant by dantheman, both CC BY 4.0 (see ATTRIBUTIONS.md); artwork and animation rigging by EventMate.
