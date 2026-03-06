# Bounce Customization, Optimization & Party Features

**Date:** 2026-03-06
**Status:** Approved

## 1. Bounce Customization

Expand auto-bounce from two hardcoded modes into a fully customizable system.

### Controls (client-side only, no server changes)
- **Speed slider** (0.5x - 4x): multiplier on base period/BPM rate
- **Radius slider** (20% - 100%): how far from center the bounce travels
- **Pattern selector** (cycles via button):
  - `circle` — circular orbit (current smooth mode)
  - `figure-8` — lemniscate path through device pairs
  - `ping-pong` — straight-line bounce between two opposite speakers
  - `random-wander` — smooth Perlin-noise-style drift
  - `beat-snap` — BPM-synced jumps between devices (current beat mode)

### State
```javascript
bounceSpeed: 1.0,      // 0.5 - 4.0 multiplier
bounceRadius: 0.75,    // 0.2 - 1.0 fraction of RING_RADIUS
bouncePattern: 'circle' // circle | figure-8 | ping-pong | random-wander | beat-snap
```
All persisted in localStorage.

### UI
Compact settings row that appears below the status bar when bounce is active.
Speed slider, radius slider, pattern cycle button — all inline.

## 2. Optimization & Code Cleanup

### Performance
- Cache canvas radial gradient (created once, not every frame)
- Consolidate three lerp passes into one loop in animationLoop()
- Throttle idle rendering — use slower timer when no animation active
- Debounce Spotify seek — attach mousemove listener once, not on every update

### Dead Code Removal
- `mutedDevices` set (legacy preset compat, unused in volume calc)
- `focusedElement` state (set but no visual feedback)
- Orphaned delay indicator variables from effects panel removal
- Deprecated Spotify audio-features BPM fetch (API deprecated Nov 2024)
  - Replace with manual BPM input / tap-tempo for beat-snap mode

### Code Cleanliness
- Rename DJ button or simplify (only opens cue + latency now)
- Clean any lingering fxTarget references

## 3. New Features (Party-Focused)

### Speaker Volume Lock (small effort)
Per-device max volume cap. Lock slider in EQ panel sets ceiling.
Prevents individual speakers from exceeding a threshold (e.g., near neighbors).
Backend: `_max_volumes` dict, clamped in `set_volume()`.

### Party Mode Toggle (small effort)
One-click button: enables Auto-DJ + sets bounce to random-wander + shows Now Playing.
"Just make it work" for party hosts. Single button in status bar.

### Visualizer Overlay (medium effort)
Energy-reactive glow/pulse on the canvas:
- Ring glows brighter with audio energy
- Device nodes pulse with their individual audio levels
- Subtle particle effects on beat detection
All in the existing canvas drawMixer() function.

### Mobile-Friendly Layout (medium effort)
Responsive CSS breakpoints:
- Stack canvas and controls vertically on narrow screens
- Touch-friendly larger hit targets for sliders/buttons
- Spotify widget adapts to full-width on mobile
Enables phone control via local network (127.0.0.1:5000).
