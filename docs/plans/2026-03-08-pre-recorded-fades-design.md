# Pre-Recorded Fade System Design

**Date:** 2026-03-08
**Purpose:** Shift the mixer from real-time live crossfading to a pre-production + one-button playback tool for DJs performing at parties.

## Overview

DJs use the mixer in two phases:
1. **Studio** — Practice crossfade movements, record them (live capture or timeline keyframe editor), and save to 16 fade slots
2. **Perform** — During a live set, trigger saved fades with individual buttons; the pre-recorded crossfade plays out automatically through the speakers

## Page Architecture

| Page | Route | Purpose |
|------|-------|---------|
| Studio | `/` | Practice mixer + record/edit fades + timeline editor |
| Perform | `/perform` | Minimal trigger board + mixer visualization for live sets |

Both pages share the same WebSocket connection and server state.

## Studio Page (`/`)

### Layout
- **Top:** Existing mixer canvas (full interaction — drag, bounce, visualizers)
- **Middle:** Transport bar (Record / Stop / Play Preview / Clear)
- **Bottom:** Timeline editor panel (full width, ~300px tall)
- **Sidebar:** Fade slot list (16 slots) with save/load/rename/delete

### Live Recording
1. DJ clicks **Record** — red indicator + timer appears on mixer canvas
2. Control point X/Y sampled at ~30fps → stored as keyframes `{time_ms, x, y}`
3. Audio crossfades in real-time as DJ drags (they hear the result)
4. Click **Stop** to end recording
5. Path appears in timeline editor; **Play Preview** replays it

### Timeline Keyframe Editor
- **Time axis:** Horizontal, scrollable, zoomable (pinch/scroll wheel), MM:SS markers
- **Two curve lanes:** X position and Y position plotted over time
- **Keyframe dots:** Clickable/draggable diamonds — drag vertically (position) or horizontally (timing)
- **Add keyframe:** Click on timeline at a time position; preview dot shows on mixer canvas
- **Delete keyframe:** Right-click or select + Delete key
- **Playhead:** Vertical scrub line — dragging updates mixer canvas preview (no audio)
- **Interpolation:** Linear between keyframes (easing curves deferred to future)

## Perform Page (`/perform`)

### Layout
- **Top:** Mixer canvas at ~350px, read-only mode (shows speakers + animating control point)
- **Center:** 4x4 grid of large, touch-friendly trigger buttons (slots 1-16)
- **Bottom:** Spotify now-playing widget (if connected)

### Trigger Grid
- Each button shows: slot number, fade name, duration
- Empty slots: dimmed with "—" placeholder
- Active fade: button pulses with progress ring
- Tap to trigger, tap active to stop
- Color: filled = cyan, empty = dark gray

### Override Mechanism
- Hold control point for 1.5 seconds to take manual control
- Circular progress indicator fills during hold
- On completion: playback pauses, control point becomes draggable
- Releasing keeps control point at that position (no snap-back)

### Keyboard Shortcuts
| Keys | Action |
|------|--------|
| `1-9, 0, -, =, Q, W, E, R` | Trigger slots 1-16 |
| `Escape` | Stop current fade |
| `Space` | Pause/resume current fade |

## Data Model

```json
{
  "id": 1,
  "name": "Drop Build",
  "duration_ms": 45000,
  "keyframes": [
    {"time_ms": 0, "x": 250, "y": 250},
    {"time_ms": 5000, "x": 350, "y": 150},
    {"time_ms": 10000, "x": 200, "y": 300}
  ],
  "created_at": "2026-03-08T12:00:00Z"
}
```

Only control point position is recorded (not EQ, pan, mutes, stereo separation).

## Server API

### REST Endpoints
| Method | Route | Purpose |
|--------|-------|---------|
| `GET` | `/api/fades` | List all saved fades (id, name, duration) |
| `GET` | `/api/fades/<id>` | Get full fade with keyframes |
| `POST` | `/api/fades` | Save new fade (auto-assigns next slot 1-16) |
| `PUT` | `/api/fades/<id>` | Update fade (rename, replace keyframes) |
| `DELETE` | `/api/fades/<id>` | Delete a fade slot |
| `GET` | `/perform` | Serve the perform page |

### WebSocket Events
| Direction | Event | Payload | Purpose |
|-----------|-------|---------|---------|
| Client→Server | `trigger_fade` | `{fade_id}` | Start fade playback |
| Client→Server | `stop_fade` | — | Stop current playback |
| Client→Server | `pause_fade` | — | Pause/resume playback |
| Client→Server | `override_fade` | — | DJ manual takeover |
| Server→Client | `fade_playback` | `{fade_id, time_ms, x, y, progress, state}` | Position broadcast (~30fps) |
| Server→Client | `fade_ended` | `{fade_id}` | Fade completed |

### Playback Engine (Server-Side)

Fade playback runs on the server to ensure reliability if the browser disconnects:
1. Load fade keyframes
2. Interpolate X/Y at current time (linear lerp between keyframes)
3. Compute crossfade volumes using existing weight computation (ported to Python)
4. Apply volumes to AudioRouter
5. Broadcast `fade_playback` at ~30fps to all clients
6. Emit `fade_ended` on completion

### Storage

`fades.json` adjacent to `server.py`:
```json
{
  "fades": {
    "1": {"name": "Drop Build", "duration_ms": 45000, "keyframes": [...], "created_at": "..."},
    "3": {"name": "Slow Pan", "duration_ms": 120000, "keyframes": [...], "created_at": "..."}
  }
}
```
Loaded on startup, saved on every mutation.

## Features Removed

| Feature | Reason |
|---------|--------|
| Presets (1-9 hotkeys) | Replaced by 16 fade slots |
| Auto-DJ | Replaced by pre-recorded fades |
| Party Mode | Replaced by pre-recorded fades |

## Features Kept

| Feature | Notes |
|---------|-------|
| Auto-Bounce patterns | Available in Studio as practice tools |
| Visualizers (embedded + fullscreen) | Both pages |
| EQ / Pan / Delay / Stereo Separation | Manual controls, not recorded |
| Spotify integration | Both pages |
| Latency monitoring | Both pages |
| Speaker volume locks (min/max) | Both pages |

## Navigation

- Studio: "Go to Perform" button (top-right)
- Perform: "Back to Studio" button (small, tucked away to avoid accidental stage hits)
