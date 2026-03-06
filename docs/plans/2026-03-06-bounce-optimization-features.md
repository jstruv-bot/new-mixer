# Bounce Customization, Optimization & Party Features — Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Add customizable bounce patterns, optimize performance/code, add speaker volume lock, party mode, and energy visualizer.

**Architecture:** All bounce/UI changes are client-side only (templates/index.html). Server changes limited to max volume clamping in set_volume() and removing the deprecated Spotify audio-features endpoint. New features follow existing patterns: state in JS object, persisted in localStorage, communicated via WebSocket.

**Tech Stack:** Vanilla JS, Canvas 2D, Flask/SocketIO backend, pytest

---

### Task 1: Dead Code Cleanup

**Files:**
- Modify: `templates/index.html:420,427`
- Modify: `server.py:1449-1465`

**Step 1: Remove dead state variables from index.html**

Remove `mutedDevices` (line 420) and `focusedElement` (line 427) from the state object. Then search for any references and remove those too.

```javascript
// DELETE these lines from the state object:
mutedDevices:   new Set(),  // legacy: kept for preset compatibility
focusedElement: null,    // {type: 'control'} or {type: 'device', index}
```

Also search for and remove any `state.mutedDevices` and `state.focusedElement` references throughout the file (preset save/load may reference mutedDevices — remove from preset serialization but keep backward-compat on load by ignoring the field).

**Step 2: Remove deprecated Spotify audio-features endpoint from server.py**

Delete the `spotify_audio_features` route (lines 1449-1465) and the BPM fetch from the client (lines 594-599 in index.html). Replace with a manual BPM input field.

In `templates/index.html`, replace the BPM fetch block:
```javascript
// DELETE this block (lines 594-599):
if (data.track_id && data.track_id !== state.bounceTrackId) {
    state.bounceTrackId = data.track_id;
    fetch('/api/spotify/audio-features/' + data.track_id)
        .then(r => r.json())
        .then(d => { state.bounceBPM = d.tempo || 120; })
        .catch(() => { state.bounceBPM = 120; });
}
```

Also remove `bounceTrackId` from state (line 430).

**Step 3: Run tests**

Run: `py -m pytest test_server.py -q`
Expected: All 67 tests pass (audio-features endpoint had no test)

**Step 4: Commit**

```bash
git add server.py templates/index.html
git commit -m "refactor: remove dead code (mutedDevices, focusedElement, deprecated Spotify audio-features)"
```

---

### Task 2: Performance — Cache Canvas Gradient

**Files:**
- Modify: `templates/index.html:936-946`

**Step 1: Create gradient once outside drawMixer**

Move the radial gradient creation out of `drawMixer()` and into a cached variable initialized once. The gradient only depends on constants (CENTER, RING_RADIUS) which never change.

Add before `drawMixer()` definition:
```javascript
// Pre-cached ring glow gradient (constants never change)
const _ringGlow = (() => {
    const g = ctx.createRadialGradient(CENTER, CENTER, RING_RADIUS - 40, CENTER, CENTER, RING_RADIUS + 10);
    g.addColorStop(0, "rgba(0, 212, 255, 0.0)");
    g.addColorStop(1, "rgba(0, 212, 255, 0.04)");
    return g;
})();
```

Then inside `drawMixer()`, replace lines 937-946 with:
```javascript
ctx.beginPath();
ctx.arc(CENTER, CENTER, RING_RADIUS, 0, Math.PI * 2);
ctx.fillStyle = _ringGlow;
ctx.fill();
```

**Step 2: Verify visually**

Run the app and confirm the ring glow still appears correctly.

**Step 3: Commit**

```bash
git add templates/index.html
git commit -m "perf: cache canvas radial gradient instead of recreating every frame"
```

---

### Task 3: Performance — Consolidate Lerp Loops

**Files:**
- Modify: `templates/index.html:1760-1797`

**Step 1: Merge the three lerp passes into a single loop**

Replace the separate volume lerp, fade-out, and level lerp with a single combined pass:

```javascript
function animationLoop() {
    let animating = false;

    // Single pass: lerp volumes, fade removed devices, lerp audio levels
    const allIds = new Set([
        ...Object.keys(state.targetVolumes),
        ...Object.keys(state.displayVolumes),
        ...Object.keys(state.audioLevels)
    ]);
    for (const id of allIds) {
        // Volume lerp
        if (id in state.targetVolumes) {
            const target = state.targetVolumes[id];
            const current = state.displayVolumes[id] || 0;
            if (Math.abs(target - current) > 0.001) {
                state.displayVolumes[id] = current + (target - current) * LERP_FACTOR;
                animating = true;
            } else {
                state.displayVolumes[id] = target;
            }
        } else if (id in state.displayVolumes) {
            // Fade out removed devices
            state.displayVolumes[id] *= (1 - LERP_FACTOR);
            if (state.displayVolumes[id] < 0.001) {
                delete state.displayVolumes[id];
                delete state.displayLevels[id];
                delete state.lastSentVolumes[id];
            } else {
                animating = true;
            }
        }
        // Level lerp
        if (id in state.audioLevels) {
            const target = state.audioLevels[id];
            const current = state.displayLevels[id] || 0;
            if (target > current) {
                state.displayLevels[id] = target;
                animating = true;
            } else {
                const newVal = current + (target - current) * LERP_FACTOR;
                if (Math.abs(newVal - current) > 0.001) animating = true;
                state.displayLevels[id] = newVal;
            }
        }
    }
    // ... rest of animationLoop unchanged
```

**Step 2: Verify visually**

Run the app and confirm volume meters and device volumes still animate smoothly.

**Step 3: Commit**

```bash
git add templates/index.html
git commit -m "perf: consolidate three lerp passes into single loop"
```

---

### Task 4: Performance — Optimize maxWeight Calculation

**Files:**
- Modify: `templates/index.html:860-882`

**Step 1: Compute max weight inline instead of Math.max(...spread)**

Replace the weight computation and normalization in `updateVolumes()`:

```javascript
function updateVolumes() {
    const n = state.devices.length;
    if (n === 0) return;

    // 1. Compute weights using the selected curve, tracking max inline
    let maxWeight = 0;
    const weights = new Array(n);
    for (let i = 0; i < n; i++) {
        const pos  = devicePosition(i, n);
        const dx   = state.controlPoint.x - pos.x;
        const dy   = state.controlPoint.y - pos.y;
        const dist = Math.sqrt(dx * dx + dy * dy);
        const w = computeWeight(dist, state.curveType);
        weights[i] = w;
        if (w > maxWeight) maxWeight = w;
    }
    // ... rest unchanged from line 873 onward
```

**Step 2: Run tests**

Run: `py -m pytest test_server.py -q`
Expected: All pass

**Step 3: Commit**

```bash
git add templates/index.html
git commit -m "perf: compute maxWeight inline instead of spread operator"
```

---

### Task 5: Bounce Customization — State & UI Controls

**Files:**
- Modify: `templates/index.html` (state object ~line 428, status bar ~line 266, CSS, bounce handler ~line 1189)

**Step 1: Add new state variables**

Add to state object after `autoBounce`:
```javascript
bounceSpeed:    parseFloat(localStorage.getItem('mixer-bounce-speed') || '1.0'),   // 0.5 - 4.0
bounceRadius:   parseFloat(localStorage.getItem('mixer-bounce-radius') || '0.75'), // 0.2 - 1.0
bouncePattern:  localStorage.getItem('mixer-bounce-pattern') || 'circle',          // circle|figure-8|ping-pong|random-wander|beat-snap
bounceBPM:      parseInt(localStorage.getItem('mixer-bounce-bpm') || '120'),       // manual BPM
```

Remove old `bounceTrackId` (should be gone from Task 1).

**Step 2: Add CSS for bounce settings row**

Add to CSS section:
```css
.bounce-settings {
    display: none; align-items: center; gap: 0.5rem; flex-wrap: wrap;
    padding: 0.4rem 0.8rem; margin-top: 0.3rem;
    background: rgba(30, 215, 96, 0.06); border: 1px solid rgba(30, 215, 96, 0.15);
    border-radius: 6px; font-size: 0.75rem; color: #aaa;
}
.bounce-settings.active { display: flex; }
.bounce-settings label { white-space: nowrap; min-width: 45px; }
.bounce-settings input[type=range] { width: 70px; height: 14px; accent-color: #1ed760; }
.bounce-settings .val { min-width: 32px; color: #1ed760; font-size: 0.7rem; }
```

**Step 3: Add HTML for bounce settings row**

Add after the status bar div (after line 276):
```html
<div id="bounce-settings" class="bounce-settings">
    <label>Pattern:</label>
    <button id="bounce-pattern-btn" class="dj-btn" type="button" style="font-size:0.7rem;padding:2px 8px;">circle</button>
    <label>Speed:</label>
    <input type="range" id="bounce-speed" min="50" max="400" value="100" step="10">
    <span id="bounce-speed-val" class="val">1.0x</span>
    <label>Radius:</label>
    <input type="range" id="bounce-radius" min="20" max="100" value="75" step="5">
    <span id="bounce-radius-val" class="val">75%</span>
    <label>BPM:</label>
    <input type="number" id="bounce-bpm" min="40" max="300" value="120" step="1" style="width:50px;background:#1a1a2e;color:#e0e0e0;border:1px solid rgba(0,212,255,0.3);border-radius:4px;text-align:center;font-size:0.75rem;">
</div>
```

**Step 4: Add JS for bounce settings interaction**

Replace the bounce toggle handler (lines 1189-1207) and add settings wiring:

```javascript
// ── Bounce pattern + settings ───────────────────────────────────
const btnBounce = document.getElementById('btn-bounce');
const bounceSettings = document.getElementById('bounce-settings');
const bouncePatterns = ['circle', 'figure-8', 'ping-pong', 'random-wander', 'beat-snap'];
const bouncePatternBtn = document.getElementById('bounce-pattern-btn');
const bounceSpeedSlider = document.getElementById('bounce-speed');
const bounceRadiusSlider = document.getElementById('bounce-radius');
const bounceBpmInput = document.getElementById('bounce-bpm');

// Init UI from state
bouncePatternBtn.textContent = state.bouncePattern;
bounceSpeedSlider.value = Math.round(state.bounceSpeed * 100);
document.getElementById('bounce-speed-val').textContent = state.bounceSpeed.toFixed(1) + 'x';
bounceRadiusSlider.value = Math.round(state.bounceRadius * 100);
document.getElementById('bounce-radius-val').textContent = Math.round(state.bounceRadius * 100) + '%';
bounceBpmInput.value = state.bounceBPM;

btnBounce.addEventListener('click', () => {
    state.autoBounce = state.autoBounce === 'off' ? 'on' : 'off';
    btnBounce.textContent = state.autoBounce !== 'off' ? 'Bounce: On' : 'Bounce: Off';
    btnBounce.style.borderColor = state.autoBounce !== 'off' ? 'rgba(30, 215, 96, 0.6)' : 'rgba(0, 212, 255, 0.3)';
    btnBounce.style.color = state.autoBounce !== 'off' ? '#1ed760' : '#00d4ff';
    bounceSettings.classList.toggle('active', state.autoBounce !== 'off');
    if (state.autoBounce !== 'off' && state.autoDJ) {
        state.autoDJ = false;
        document.getElementById('btn-autodj').textContent = 'Auto-DJ: Off';
        document.getElementById('btn-autodj').style.borderColor = 'rgba(0, 212, 255, 0.3)';
        document.getElementById('btn-autodj').style.color = '#00d4ff';
    }
});

bouncePatternBtn.addEventListener('click', () => {
    const idx = (bouncePatterns.indexOf(state.bouncePattern) + 1) % bouncePatterns.length;
    state.bouncePattern = bouncePatterns[idx];
    bouncePatternBtn.textContent = state.bouncePattern;
    localStorage.setItem('mixer-bounce-pattern', state.bouncePattern);
});

bounceSpeedSlider.addEventListener('input', () => {
    state.bounceSpeed = parseInt(bounceSpeedSlider.value) / 100;
    document.getElementById('bounce-speed-val').textContent = state.bounceSpeed.toFixed(1) + 'x';
    localStorage.setItem('mixer-bounce-speed', state.bounceSpeed);
});

bounceRadiusSlider.addEventListener('input', () => {
    state.bounceRadius = parseInt(bounceRadiusSlider.value) / 100;
    document.getElementById('bounce-radius-val').textContent = Math.round(state.bounceRadius * 100) + '%';
    localStorage.setItem('mixer-bounce-radius', state.bounceRadius);
});

bounceBpmInput.addEventListener('change', () => {
    state.bounceBPM = Math.max(40, Math.min(300, parseInt(bounceBpmInput.value) || 120));
    bounceBpmInput.value = state.bounceBPM;
    localStorage.setItem('mixer-bounce-bpm', state.bounceBPM);
});
```

**Step 5: Commit**

```bash
git add templates/index.html
git commit -m "feat: add bounce customization UI (pattern, speed, radius, BPM controls)"
```

---

### Task 6: Bounce Customization — Pattern Implementations

**Files:**
- Modify: `templates/index.html:1816-1853` (animation loop bounce block)

**Step 1: Replace bounce logic with pattern-driven implementation**

Replace the entire auto-bounce block (lines 1816-1853) with:

```javascript
// Auto-bounce — customizable patterns
if (state.autoBounce !== 'off' && !state.isDragging) {
    const n = state.devices.length;
    if (n >= 2) {
        const now = performance.now() / 1000;
        const speed = state.bounceSpeed;
        const radius = state.bounceRadius * RING_RADIUS;
        let targetX, targetY;

        if (state.bouncePattern === 'circle') {
            const period = 4.0 / speed;
            const t = (now % period) / period;
            const angle = t * 2 * Math.PI - Math.PI / 2;
            targetX = CENTER + radius * Math.cos(angle);
            targetY = CENTER + radius * Math.sin(angle);

        } else if (state.bouncePattern === 'figure-8') {
            const period = 6.0 / speed;
            const t = (now % period) / period;
            const angle = t * 2 * Math.PI;
            // Lemniscate of Bernoulli parametric
            targetX = CENTER + radius * Math.cos(angle);
            targetY = CENTER + radius * 0.5 * Math.sin(2 * angle);

        } else if (state.bouncePattern === 'ping-pong') {
            const period = 2.0 / speed;
            const t = (now % period) / period;
            // Triangle wave between first two device positions
            const posA = devicePosition(0, n);
            const posB = devicePosition(Math.floor(n / 2), n);
            const frac = t < 0.5 ? t * 2 : 2 - t * 2; // 0→1→0
            const ease = frac * frac * (3 - 2 * frac); // smoothstep
            targetX = posA.x + (posB.x - posA.x) * ease;
            targetY = posA.y + (posB.y - posA.y) * ease;

        } else if (state.bouncePattern === 'random-wander') {
            // Smooth random drift using layered sine waves (Perlin-like)
            const s1 = Math.sin(now * 0.7 * speed) * 0.6;
            const s2 = Math.sin(now * 1.3 * speed + 2.1) * 0.3;
            const s3 = Math.sin(now * 0.4 * speed + 4.7) * 0.1;
            const c1 = Math.cos(now * 0.5 * speed + 1.3) * 0.6;
            const c2 = Math.cos(now * 1.1 * speed + 3.5) * 0.3;
            const c3 = Math.cos(now * 0.3 * speed + 5.2) * 0.1;
            targetX = CENTER + radius * (s1 + s2 + s3);
            targetY = CENTER + radius * (c1 + c2 + c3);

        } else if (state.bouncePattern === 'beat-snap') {
            const bps = state.bounceBPM / 60 * speed;
            const beatPhase = (now * bps) % n;
            const beatIndex = Math.floor(beatPhase);
            const beatFrac = beatPhase - beatIndex;
            const nextIndex = (beatIndex + 1) % n;
            const posA = devicePosition(beatIndex, n);
            const posB = devicePosition(nextIndex, n);
            const ease = beatFrac * beatFrac * (3 - 2 * beatFrac);
            targetX = posA.x + (posB.x - posA.x) * ease;
            targetY = posA.y + (posB.y - posA.y) * ease;
        }

        if (targetX !== undefined) {
            const lerpRate = state.bouncePattern === 'beat-snap' ? 0.15 : 0.08;
            state.controlPoint.x += (targetX - state.controlPoint.x) * lerpRate;
            state.controlPoint.y += (targetY - state.controlPoint.y) * lerpRate;
            updateVolumes();
            animating = true;
        }
    }
}
```

**Step 2: Verify all 5 patterns visually**

Run the app, activate bounce, and cycle through each pattern confirming movement.

**Step 3: Commit**

```bash
git add templates/index.html
git commit -m "feat: implement 5 bounce patterns (circle, figure-8, ping-pong, random-wander, beat-snap)"
```

---

### Task 7: Speaker Volume Lock (Max Volume)

**Files:**
- Modify: `server.py:378-381,954` (set_volume, state)
- Modify: `templates/index.html` (EQ panel, state)
- Modify: `test_server.py`

**Step 1: Write failing test**

Add to test_server.py:
```python
class TestMaxVolume:
    """Speaker volume lock / max volume tests."""

    def test_max_volume_clamp(self):
        with server_module._state_lock:
            server_module._max_volumes['d1'] = 0.5
        server_module.audio_router.set_volume('d1', 0.8)
        with server_module.audio_router._lock:
            assert server_module.audio_router._volumes['d1'] <= 0.5
        # Cleanup
        with server_module._state_lock:
            server_module._max_volumes.pop('d1', None)

    def test_set_max_volume_via_ws(self, socketio_client):
        socketio_client.emit('set_max_volume', {
            'device_id': 'd1', 'max_volume': 0.6
        })
        with server_module._state_lock:
            assert server_module._max_volumes.get('d1') == 0.6

    def test_max_volume_zero_means_unlimited(self, socketio_client):
        socketio_client.emit('set_max_volume', {
            'device_id': 'd1', 'max_volume': 0
        })
        with server_module._state_lock:
            assert 'd1' not in server_module._max_volumes
```

**Step 2: Run tests to verify they fail**

Run: `py -m pytest test_server.py::TestMaxVolume -v`
Expected: FAIL

**Step 3: Implement backend**

In `server.py`, add state variable near line 954:
```python
_max_volumes = {}                # device_id -> float (0.0-1.0) max volume cap
```

Modify `set_volume` (line 378):
```python
def set_volume(self, device_id, volume):
    """Update the volume multiplier for a device's output stream."""
    vol = max(0.0, min(1.0, float(volume)))
    # Clamp to max volume lock if set
    with _state_lock:
        max_vol = _max_volumes.get(device_id)
    if max_vol is not None and max_vol > 0:
        vol = min(vol, max_vol)
    with self._lock:
        self._volumes[device_id] = vol
```

Add WS handler:
```python
@socketio.on("set_max_volume")
def ws_set_max_volume(data):
    """Set max volume lock for a device (0 = unlimited)."""
    device_id = data.get("device_id")
    max_vol = data.get("max_volume", 0)
    if device_id is None:
        return
    with _state_lock:
        if max_vol and float(max_vol) > 0:
            _max_volumes[device_id] = max(0.0, min(1.0, float(max_vol)))
        else:
            _max_volumes.pop(device_id, None)
```

Add to `_enrich_devices`:
```python
d2["max_volume"] = _max_volumes.get(did, 0.0)
```

**Step 4: Run tests to verify they pass**

Run: `py -m pytest test_server.py::TestMaxVolume -v`
Expected: PASS

**Step 5: Add UI — Max Vol slider in EQ panel**

Add a slider row in the EQ panel HTML (after the Pan slider row):
```html
<div class="eq-slider-row">
    <label>Max Vol</label>
    <input type="range" id="eq-maxvol" min="0" max="100" value="0" step="5">
    <span id="eq-maxvol-val">Off</span>
</div>
```

Add JS handler in the EQ panel section:
```javascript
const eqMaxVol = document.getElementById('eq-maxvol');
const eqMaxVolVal = document.getElementById('eq-maxvol-val');

eqMaxVol.addEventListener('input', () => {
    if (!state.eqTarget) return;
    const val = parseInt(eqMaxVol.value);
    eqMaxVolVal.textContent = val === 0 ? 'Off' : val + '%';
    socket.emit('set_max_volume', { device_id: state.eqTarget, max_volume: val / 100 });
});
```

Update the EQ panel open function to initialize slider from device data.

**Step 6: Run all tests**

Run: `py -m pytest test_server.py -q`
Expected: All pass (67 + 3 new = 70)

**Step 7: Commit**

```bash
git add server.py templates/index.html test_server.py
git commit -m "feat: add speaker volume lock (max volume cap per device)"
```

---

### Task 8: Party Mode Toggle

**Files:**
- Modify: `templates/index.html` (status bar HTML, JS)

**Step 1: Add Party Mode button to status bar**

Add button in the status bar HTML (before Refresh Devices):
```html
<button id="btn-party" type="button" title="Party Mode: auto-DJ + random bounce + full stereo">Party</button>
```

**Step 2: Add JS handler**

```javascript
const btnParty = document.getElementById('btn-party');
let partyMode = false;

btnParty.addEventListener('click', () => {
    partyMode = !partyMode;
    btnParty.style.borderColor = partyMode ? 'rgba(30, 215, 96, 0.6)' : 'rgba(0, 212, 255, 0.3)';
    btnParty.style.color = partyMode ? '#1ed760' : '#00d4ff';
    btnParty.textContent = partyMode ? 'Party: On' : 'Party';

    if (partyMode) {
        // Enable Auto-DJ
        state.autoDJ = true;
        btnAutoDJ.textContent = 'Auto-DJ: On';
        btnAutoDJ.style.borderColor = 'rgba(30, 215, 96, 0.6)';
        btnAutoDJ.style.color = '#1ed760';
        // Disable bounce (conflicts with Auto-DJ)
        state.autoBounce = 'off';
        btnBounce.textContent = 'Bounce: Off';
        btnBounce.style.borderColor = 'rgba(0, 212, 255, 0.3)';
        btnBounce.style.color = '#00d4ff';
        bounceSettings.classList.remove('active');
        // Set stereo separation to 80%
        state.stereoSep = 0.8;
        document.getElementById('stereo-sep').value = 80;
        document.getElementById('stereo-sep-val').textContent = '80%';
        socket.emit('set_stereo_separation', { value: 0.8 });
        localStorage.setItem('mixer-stereo-sep', '0.8');
        showToast('Party Mode activated');
    } else {
        showToast('Party Mode deactivated');
    }
});
```

**Step 3: Commit**

```bash
git add templates/index.html
git commit -m "feat: add Party Mode one-click toggle (auto-DJ + stereo separation)"
```

---

### Task 9: Energy Visualizer Overlay

**Files:**
- Modify: `templates/index.html` (drawMixer function, ~line 923)

**Step 1: Add energy-reactive ring glow**

In `drawMixer()`, after drawing the static ring glow, add a dynamic energy-reactive overlay. Replace the cached ring glow draw with:

```javascript
// Static ring glow
ctx.beginPath();
ctx.arc(CENTER, CENTER, RING_RADIUS, 0, Math.PI * 2);
ctx.fillStyle = _ringGlow;
ctx.fill();

// Energy-reactive ring pulse
const energy = state.djEnergy || 0;
if (energy > 0.01) {
    const pulseRadius = RING_RADIUS + energy * 15;
    const pulseGlow = ctx.createRadialGradient(
        CENTER, CENTER, RING_RADIUS - 10,
        CENTER, CENTER, pulseRadius
    );
    const alpha = energy * 0.15;
    pulseGlow.addColorStop(0, `rgba(30, 215, 96, 0)`);
    pulseGlow.addColorStop(0.5, `rgba(30, 215, 96, ${alpha})`);
    pulseGlow.addColorStop(1, `rgba(30, 215, 96, 0)`);
    ctx.beginPath();
    ctx.arc(CENTER, CENTER, pulseRadius, 0, Math.PI * 2);
    ctx.fillStyle = pulseGlow;
    ctx.fill();
}
```

**Step 2: Add per-device energy-reactive node glow**

In `drawMixer()`, in the device drawing loop, after drawing the device circle, add a pulse effect based on that device's audio level:

```javascript
// Energy pulse around device node
const level = state.displayLevels[dev.id] || 0;
if (level > 0.05) {
    const nodeGlow = ctx.createRadialGradient(
        pos.x, pos.y, DEVICE_RADIUS,
        pos.x, pos.y, DEVICE_RADIUS + level * 20
    );
    nodeGlow.addColorStop(0, `rgba(0, 212, 255, ${level * 0.3})`);
    nodeGlow.addColorStop(1, `rgba(0, 212, 255, 0)`);
    ctx.beginPath();
    ctx.arc(pos.x, pos.y, DEVICE_RADIUS + level * 20, 0, Math.PI * 2);
    ctx.fillStyle = nodeGlow;
    ctx.fill();
}
```

**Step 3: Always receive energy updates (not just in Auto-DJ)**

Modify the energy socket handler (line 615-619):
```javascript
socket.on("audio_energy", (data) => {
    state.djEnergy = data.energy || 0;
    if (state.djEnergy > 0.01) markDirty();
});
```

Remove the `if (state.autoDJ)` guard so the visualizer works independently.

**Step 4: Verify visually**

Run the app with audio playing. Confirm ring pulses green with energy, device nodes glow with their individual levels.

**Step 5: Commit**

```bash
git add templates/index.html
git commit -m "feat: add energy-reactive visualizer (ring pulse + device node glow)"
```

---

### Task 10: Simplify DJ Button

**Files:**
- Modify: `templates/index.html` (button label, handler)

**Step 1: Rename DJ button and simplify**

The DJ button now only opens cue + latency panels. Rename it to something clearer:

In HTML: Change `title="DJ features menu">DJ` to `title="Cue & latency panels">More`

In JS: The existing handler is fine — just opens/closes the two panels.

**Step 2: Commit**

```bash
git add templates/index.html
git commit -m "refactor: rename DJ button to More (only opens cue + latency)"
```

---

### Task 11: Final Test Run & Exe Rebuild

**Step 1: Run full test suite**

Run: `py -m pytest test_server.py -v`
Expected: All pass (70 tests)

**Step 2: Rebuild exe**

```bash
taskkill /F /IM BluetoothCrossfadeMixer.exe 2>/dev/null
py -m PyInstaller BluetoothCrossfadeMixer.spec --noconfirm
```

**Step 3: Commit all remaining changes**

```bash
git add -A
git commit -m "chore: final cleanup and exe rebuild"
```
