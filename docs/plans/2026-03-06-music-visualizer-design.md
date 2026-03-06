# Music Visualizer Suite — Design Document

**Date:** 2026-03-06
**Status:** Approved

## Overview

Add a music-reactive visualization system to the Bluetooth Crossfade Mixer. Two layers: subtle always-on effects embedded in the mixer canvas, and a dedicated fullscreen visualizer with 5 modes. All Canvas 2D — zero external dependencies.

## Audio Analysis Backend

Three selectable tiers of audio analysis. Client tells server which tier to compute via `set_visualizer_mode` WS event.

### Tier 1: Energy (existing)

- Single smoothed float 0–1
- Already implemented via `audio_energy` event
- No additional CPU cost

### Tier 2: FFT Spectrum

- 512-point FFT on the capture buffer
- 8 logarithmically-spaced frequency bands:
  - Sub-bass (20–60 Hz)
  - Bass (60–250 Hz)
  - Low-mid (250–500 Hz)
  - Mid (500–2 kHz)
  - Upper-mid (2–4 kHz)
  - Presence (4–6 kHz)
  - Brilliance (6–12 kHz)
  - Air (12–20 kHz)
- Computed every ~3rd audio chunk (~16 Hz update rate)
- Runs in the capture thread (numpy buffer already available)
- New WS event: `audio_spectrum` with `{ bands: [8 floats 0-1] }`

### Tier 3: FFT + Beat Detection

- Everything from Tier 2, plus:
- Onset detection via energy flux: compare current frame energy to rolling average
- Spike above threshold (1.5× rolling average) = beat
- BPM estimate: track inter-beat intervals, median filter for stability
- Beat phase: 0–1 sawtooth synchronized to detected tempo
- Extended WS payload: `{ bands: [8 floats], beat: bool, bpm: float, phase: float }`

### Server-side implementation

- FFT + beat logic in capture thread (data is already a numpy array)
- `_viz_mode` global: 0 = energy only, 1 = spectrum, 2 = spectrum + beat
- Client controls via `set_visualizer_mode` WS event
- `audio_energy` event unchanged (backwards compatible)
- `audio_spectrum` event emitted at ~16 Hz only when mode >= 1

## Mixer-Embedded Visualizations

Four background layers drawn BEFORE the ring, device nodes, and control point. All react to the active audio tier.

### Layer 1: Spectrum Ring

- 64 frequency bars drawn radially along the inside edge of the mixer ring
- Bars interpolated from the 8 FFT bands
- Low frequencies at 12 o'clock, sweeping clockwise
- Soft cyan with alpha proportional to amplitude
- Fallback when energy-only tier: simple pulsing ring

### Layer 2: Waveform Orbit

- Circular oscilloscope line orbiting the center
- Radius wobbles based on energy/spectrum bands
- `globalCompositeOperation: 'lighter'` for additive glow
- Color shifts: cyan (quiet) → green (medium) → warm orange (loud)

### Layer 3: Beat Flash

- On beat detection: radial burst from center
- White-to-transparent radial gradient, expands and fades over ~150ms
- Only active when beat detection tier is enabled

### Layer 4: Particle Drift

- 50–100 small particles floating from center outward
- Speed and brightness scale with energy
- Velocity kick on beats
- Fade to zero alpha approaching ring edge
- Drawn behind everything else

### Design principles

- All effects drawn before ring/nodes/control point (background layer)
- Each effect has a master opacity multiplier (0–1)
- Effects fade to near-invisible when energy < 0.05
- No effect touches device node or control point rendering
- Total particle budget: ≤ 200 objects for 60fps

## Fullscreen Visualizer Mode

Dedicated view — mixer disappears, entire page becomes visual. Escape or click to return.

### Mode 1: Radial Spectrum

- 8 FFT bands as smooth arcs radiating from center, mirrored symmetrically
- Each band has its own color (sub-bass = deep red, bass = orange, mids = cyan, treble = violet)
- Arcs pulse outward on beats
- Slow rotation

### Mode 2: Particle Nebula

- ~800 particles in gravitational orbit around center
- Bass pushes particles outward, treble tightens orbit
- Beat-triggered cluster spawns
- Trail effect via semi-transparent canvas clearing (fillRect alpha 0.03)

### Mode 3: Waveform Terrain

- Horizontal lines stacked vertically (Joy Division aesthetic)
- Each line displaced by energy
- Lines scroll upward, creating 3D terrain illusion
- Color gradient: warm (bottom) to cool (top)
- Beat pulses inject sharp peaks

### Mode 4: Geometric Kaleidoscope

- Symmetric shapes (triangles, hexagons) that grow/shrink/rotate with music
- 6-fold or 8-fold symmetry via canvas transforms
- Shape size = bass, rotation speed = mids, hue = treble
- Shapes fragment and reform on beats

### Mode 5: Frequency Waterfall

- Spectrogram scrolling left-to-right
- Each column = current 8-band spectrum mapped to color intensity
- Dark blue → cyan → white → orange for increasing amplitude
- Visual record of song structure over time

### Fullscreen UI

- Enter: "Viz" button in toolbar or `V` keyboard shortcut
- Exit: `Escape` or click
- Cycle modes: `Left/Right` arrow keys or click
- Mode name fades in briefly on switch, auto-hides
- Spotify now-playing overlay: bottom-left, semi-transparent, auto-hides after 5s, reappears on track change
- Canvas resizes to `window.innerWidth × window.innerHeight`

### Performance budget

- Target: 60fps on mid-range hardware
- Particle cap: 800 max
- Gradient creation minimized — reuse where possible
- Shared `requestAnimationFrame` loop (one runs at a time: mixer OR fullscreen)

## Settings UI

### Toolbar

Single "Viz" button next to "Party" button. Opens settings panel (same style as EQ panel).

### Settings panel

| Setting | Control | Default |
|---------|---------|---------|
| Audio Tier | 3 radio buttons: Energy / Spectrum / Spectrum+Beat | Energy |
| Mixer Effects | 4 toggles: Spectrum Ring, Waveform Orbit, Beat Flash, Particle Drift | All on |
| Mixer Effect Intensity | Slider 0–100% | 60% |
| Fullscreen Mode | Dropdown: 5 modes | Radial Spectrum |
| Color Theme | 4 options: Cyan / Sunset / Neon / Monochrome | Cyan |

### Keyboard shortcuts

- `V` — toggle fullscreen visualizer
- `Left/Right` (fullscreen) — cycle visualization mode
- `Up/Down` (fullscreen) — cycle color theme

### Color themes

Each theme = 5 colors: `{ primary, secondary, accent, background, beat }`

- **Cyan** (default): existing mixer palette — no visual change
- **Sunset**: warm oranges, deep reds, gold accents
- **Neon**: hot pink, electric green, purple background
- **Monochrome**: white, grey, black — clean and minimal

### Persistence

All settings saved to localStorage under `mixer-viz-*` keys.

## Integration with Existing Code

### Data flow

```
User selects audio tier
  → client emits set_visualizer_mode
  → server starts/stops FFT/beat computation
  → server emits audio_spectrum at ~16Hz
  → client stores in state.vizSpectrum / state.vizBeat / state.vizBPM
  → animation loop reads viz state
  → passes to drawMixerEffects() or drawVisualizerFrame()
```

### Code changes

- `drawMixer()` gains a new first call: `drawMixerEffects()` for background layers
- `state.vizFullscreen` flag: animation loop calls `drawVisualizerFrame()` instead of `drawMixer()`
- Canvas resizes to fill window on fullscreen enter, restores to 500×500 on exit
- Shared `requestAnimationFrame` loop — no second loop
- All viz draw functions reference `vizTheme.*` colors instead of hardcoded values

### Architecture

- **server.py**: add FFT computation + beat detection in capture thread, add `audio_spectrum` WS event, add `set_visualizer_mode` handler
- **index.html**: add viz state variables, settings panel HTML/CSS, `drawMixerEffects()`, `drawVisualizerFrame()` (5 mode functions), fullscreen toggle, color theme system, keyboard shortcuts
- **test_server.py**: add tests for FFT band computation, beat detection, viz mode switching
