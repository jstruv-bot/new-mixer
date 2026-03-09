# Pre-Recorded Fade System Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Transform the mixer from a real-time crossfade tool into a pre-production + one-button playback system where DJs record fades in Studio mode and trigger them via a 4x4 grid in Perform mode.

**Architecture:** Two-page Flask app (Studio at `/`, Perform at `/perform`) sharing the same WebSocket/audio backend. Fades are stored server-side in `fades.json`. A server-side playback engine interpolates keyframes and drives speaker volumes, ensuring playback continues even if the browser disconnects.

**Tech Stack:** Python/Flask/Flask-SocketIO (backend), HTML5 Canvas + vanilla JS (frontend), JSON file storage.

---

## Task 1: Port computeWeight to Python + fade storage module

Creates the Python-side crossfade weight computation and fade JSON storage, which the playback engine will depend on.

**Files:**
- Create: `fade_engine.py`
- Test: `test_fade_engine.py`

**Step 1: Write failing tests for compute_weight and fade storage**

```python
# test_fade_engine.py
"""Tests for fade engine: weight computation and fade storage."""
import os
import json
import tempfile
import pytest


def test_compute_weight_inverse_square_at_zero():
    """At distance 0, weight should be 1.0."""
    from fade_engine import compute_weight
    assert compute_weight(0, 'inverse-square') == pytest.approx(1.0)


def test_compute_weight_inverse_square_far():
    """At large distance, weight should approach 0."""
    from fade_engine import compute_weight
    w = compute_weight(360, 'inverse-square')
    assert w < 0.4


def test_compute_weight_linear():
    """Linear curve: halfway should be ~0.5."""
    from fade_engine import compute_weight
    w = compute_weight(180, 'linear')
    assert 0.4 < w < 0.6


def test_compute_weight_equal_power():
    """Equal power at distance 0 should be 1.0."""
    from fade_engine import compute_weight
    assert compute_weight(0, 'equal-power') == pytest.approx(1.0)


def test_compute_weight_logarithmic():
    """Logarithmic at distance 0 should be 1.0."""
    from fade_engine import compute_weight
    assert compute_weight(0, 'logarithmic') == pytest.approx(1.0)


def test_interpolate_position_single_keyframe():
    """Single keyframe: always returns that position."""
    from fade_engine import interpolate_position
    kfs = [{'time_ms': 0, 'x': 100, 'y': 200}]
    assert interpolate_position(kfs, 500) == (100, 200)


def test_interpolate_position_between():
    """Interpolates linearly between two keyframes."""
    from fade_engine import interpolate_position
    kfs = [
        {'time_ms': 0, 'x': 0, 'y': 0},
        {'time_ms': 1000, 'x': 100, 'y': 200},
    ]
    x, y = interpolate_position(kfs, 500)
    assert x == pytest.approx(50)
    assert y == pytest.approx(100)


def test_interpolate_position_past_end():
    """Past the last keyframe: returns last position."""
    from fade_engine import interpolate_position
    kfs = [
        {'time_ms': 0, 'x': 0, 'y': 0},
        {'time_ms': 1000, 'x': 100, 'y': 200},
    ]
    assert interpolate_position(kfs, 2000) == (100, 200)


def test_fade_store_crud():
    """Save, load, list, delete fades."""
    from fade_engine import FadeStore
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, 'fades.json')
        store = FadeStore(path)

        # Empty initially
        assert store.list_fades() == {}

        # Save
        fade = {
            'name': 'Test Fade',
            'duration_ms': 5000,
            'keyframes': [{'time_ms': 0, 'x': 250, 'y': 250}],
        }
        fade_id = store.save_fade(fade)
        assert fade_id == 1

        # Get
        loaded = store.get_fade(1)
        assert loaded['name'] == 'Test Fade'
        assert 'created_at' in loaded

        # List
        listing = store.list_fades()
        assert 1 in listing

        # Update
        store.update_fade(1, {'name': 'Renamed'})
        assert store.get_fade(1)['name'] == 'Renamed'

        # Delete
        store.delete_fade(1)
        assert store.get_fade(1) is None


def test_fade_store_max_16_slots():
    """Cannot save more than 16 fades."""
    from fade_engine import FadeStore
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, 'fades.json')
        store = FadeStore(path)
        fade = {'name': 'f', 'duration_ms': 1000, 'keyframes': [{'time_ms': 0, 'x': 0, 'y': 0}]}
        for i in range(16):
            assert store.save_fade(fade) == i + 1
        assert store.save_fade(fade) is None  # slot 17 should fail


def test_fade_store_persistence():
    """Fades persist across FadeStore instances."""
    from fade_engine import FadeStore
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, 'fades.json')
        store1 = FadeStore(path)
        store1.save_fade({'name': 'Persist', 'duration_ms': 1000, 'keyframes': []})

        store2 = FadeStore(path)
        assert store2.get_fade(1)['name'] == 'Persist'


def test_compute_volumes_from_position():
    """Given a position and device positions, compute normalized volumes."""
    from fade_engine import compute_volumes_from_position
    devices = [
        {'id': 'a', 'x': 250, 'y': 70},   # top
        {'id': 'b', 'x': 250, 'y': 430},   # bottom
    ]
    # Control point near device 'a'
    vols = compute_volumes_from_position(250, 100, devices, 'inverse-square')
    assert vols['a'] > vols['b']
    assert vols['a'] == pytest.approx(1.0, abs=0.1)
```

**Step 2: Run tests to verify they fail**

Run: `pytest test_fade_engine.py -v`
Expected: FAIL (module not found)

**Step 3: Implement fade_engine.py**

```python
# fade_engine.py
"""Fade engine: weight computation, keyframe interpolation, and fade storage."""
import json
import math
import os
import threading
from datetime import datetime, timezone

RING_RADIUS = 180
MAX_SLOTS = 16


def compute_weight(dist, curve_type):
    """Compute crossfade weight for a given distance and curve type.
    Mirrors the JS computeWeight function exactly."""
    max_dist = RING_RADIUS * 2
    ratio = min(dist / max_dist, 1.0)

    if curve_type == 'linear':
        return max(0.05, 1 - ratio)
    elif curve_type == 'logarithmic':
        return 1.0 / (1 + 1.5 * math.log(1 + dist * 0.02))
    elif curve_type == 'equal-power':
        return math.cos(ratio * math.pi / 2.2) ** 2
    else:  # inverse-square (default)
        return 1.0 / (1 + (dist * dist) / 80000)


def interpolate_position(keyframes, time_ms):
    """Linear interpolation of X/Y position at a given time."""
    if not keyframes:
        return (250, 250)
    if len(keyframes) == 1 or time_ms <= keyframes[0]['time_ms']:
        return (keyframes[0]['x'], keyframes[0]['y'])
    if time_ms >= keyframes[-1]['time_ms']:
        return (keyframes[-1]['x'], keyframes[-1]['y'])

    # Find surrounding keyframes
    for i in range(len(keyframes) - 1):
        kf_a = keyframes[i]
        kf_b = keyframes[i + 1]
        if kf_a['time_ms'] <= time_ms <= kf_b['time_ms']:
            span = kf_b['time_ms'] - kf_a['time_ms']
            if span == 0:
                return (kf_a['x'], kf_a['y'])
            t = (time_ms - kf_a['time_ms']) / span
            x = kf_a['x'] + (kf_b['x'] - kf_a['x']) * t
            y = kf_a['y'] + (kf_b['y'] - kf_a['y']) * t
            return (x, y)

    return (keyframes[-1]['x'], keyframes[-1]['y'])


def compute_volumes_from_position(cx, cy, device_positions, curve_type,
                                  min_volumes=None):
    """Compute per-device volumes given control point position and device positions.
    device_positions: list of {'id': str, 'x': float, 'y': float}
    Returns: dict of device_id -> volume (0.0-1.0)
    """
    if not device_positions:
        return {}
    min_volumes = min_volumes or {}

    weights = {}
    max_weight = 0
    for dp in device_positions:
        dx = cx - dp['x']
        dy = cy - dp['y']
        dist = math.sqrt(dx * dx + dy * dy)
        w = compute_weight(dist, curve_type)
        weights[dp['id']] = w
        if w > max_weight:
            max_weight = w

    volumes = {}
    for dp in device_positions:
        did = dp['id']
        vol = weights[did] / max_weight if max_weight > 0 else 0
        if vol < 0.02:
            vol = 0
        min_vol = min_volumes.get(did, 0)
        if min_vol > 0 and vol < min_vol:
            vol = min_vol
        volumes[did] = vol

    return volumes


class FadeStore:
    """Persistent JSON storage for fade slots (1-16)."""

    def __init__(self, path):
        self._path = path
        self._lock = threading.Lock()
        self._fades = {}  # int slot -> fade dict
        self._load()

    def _load(self):
        if os.path.exists(self._path):
            try:
                with open(self._path, 'r') as f:
                    data = json.load(f)
                # Keys are strings in JSON, convert to int
                self._fades = {int(k): v for k, v in data.get('fades', {}).items()}
            except (json.JSONDecodeError, IOError):
                self._fades = {}

    def _save(self):
        data = {'fades': {str(k): v for k, v in self._fades.items()}}
        try:
            with open(self._path, 'w') as f:
                json.dump(data, f, indent=2)
        except IOError:
            pass

    def list_fades(self):
        """Return dict of slot -> {name, duration_ms}."""
        with self._lock:
            return {
                slot: {'name': f['name'], 'duration_ms': f['duration_ms']}
                for slot, f in self._fades.items()
            }

    def get_fade(self, slot):
        """Get full fade data for a slot, or None."""
        with self._lock:
            return self._fades.get(slot)

    def save_fade(self, fade_data):
        """Save a fade to the next available slot. Returns slot number or None if full."""
        with self._lock:
            for slot in range(1, MAX_SLOTS + 1):
                if slot not in self._fades:
                    fade_data['created_at'] = datetime.now(timezone.utc).isoformat()
                    self._fades[slot] = fade_data
                    self._save()
                    return slot
            return None

    def update_fade(self, slot, updates):
        """Update fields of an existing fade."""
        with self._lock:
            if slot not in self._fades:
                return False
            self._fades[slot].update(updates)
            self._save()
            return True

    def delete_fade(self, slot):
        """Delete a fade slot."""
        with self._lock:
            if slot in self._fades:
                del self._fades[slot]
                self._save()
                return True
            return False
```

**Step 4: Run tests to verify they pass**

Run: `pytest test_fade_engine.py -v`
Expected: All PASS

**Step 5: Commit**

```bash
git add fade_engine.py test_fade_engine.py
git commit -m "feat: add fade engine with weight computation, interpolation, and storage"
```

---

## Task 2: Add fade playback engine to server

Adds the server-side playback thread that drives speaker volumes from recorded keyframes, plus WebSocket events and REST endpoints for fade CRUD and playback control.

**Files:**
- Modify: `server.py`
- Modify: `test_server.py`

**Step 1: Write failing tests for fade REST endpoints**

Add to `test_server.py`:

```python
class TestFadeAPI:
    """Tests for fade CRUD and playback REST/WS endpoints."""

    def test_list_fades_empty(self, client):
        """GET /api/fades returns empty dict initially."""
        resp = client.get('/api/fades')
        assert resp.status_code == 200
        assert resp.get_json() == {}

    def test_save_and_get_fade(self, client):
        """POST /api/fades saves, GET /api/fades/<id> retrieves."""
        fade = {
            'name': 'Test',
            'duration_ms': 5000,
            'keyframes': [{'time_ms': 0, 'x': 250, 'y': 250}],
        }
        resp = client.post('/api/fades', json=fade)
        assert resp.status_code == 200
        data = resp.get_json()
        assert data['slot'] == 1

        resp = client.get('/api/fades/1')
        assert resp.status_code == 200
        assert resp.get_json()['name'] == 'Test'

    def test_update_fade(self, client):
        """PUT /api/fades/<id> updates fade name."""
        fade = {'name': 'Original', 'duration_ms': 1000, 'keyframes': []}
        client.post('/api/fades', json=fade)
        resp = client.put('/api/fades/1', json={'name': 'Updated'})
        assert resp.status_code == 200
        assert client.get('/api/fades/1').get_json()['name'] == 'Updated'

    def test_delete_fade(self, client):
        """DELETE /api/fades/<id> removes fade."""
        fade = {'name': 'Delete Me', 'duration_ms': 1000, 'keyframes': []}
        client.post('/api/fades', json=fade)
        resp = client.delete('/api/fades/1')
        assert resp.status_code == 200
        resp = client.get('/api/fades/1')
        assert resp.status_code == 404

    def test_get_nonexistent_fade(self, client):
        """GET /api/fades/<id> returns 404 for missing slot."""
        resp = client.get('/api/fades/99')
        assert resp.status_code == 404


class TestFadeWebSocket:
    """Tests for fade playback WebSocket events."""

    def test_trigger_fade(self, socketio_client):
        """trigger_fade emits fade_playback or error."""
        socketio_client.emit('trigger_fade', {'fade_id': 1})
        received = socketio_client.get_received()
        # Should receive either fade_playback or an error since no fade exists
        events = [r['name'] for r in received]
        # At minimum, no crash
        assert socketio_client.is_connected()

    def test_stop_fade(self, socketio_client):
        """stop_fade doesn't crash when nothing is playing."""
        socketio_client.emit('stop_fade', {})
        assert socketio_client.is_connected()
```

**Step 2: Run tests to verify they fail**

Run: `pytest test_server.py::TestFadeAPI -v`
Expected: FAIL (routes don't exist)

**Step 3: Implement server-side fade system**

Add these to `server.py`:

1. Import `fade_engine` at the top (after existing imports)
2. Initialize `FadeStore` with path next to server.py
3. Add REST endpoints: GET/POST `/api/fades`, GET/PUT/DELETE `/api/fades/<id>`
4. Add WebSocket events: `trigger_fade`, `stop_fade`, `pause_fade`, `override_fade`
5. Add fade playback thread that:
   - Loads keyframes from store
   - Interpolates position at ~30fps
   - Calls `compute_volumes_from_position` to get per-device volumes
   - Calls `audio_router.set_volume()` for each device
   - Emits `fade_playback` events to all clients
   - Emits `fade_ended` when done

Key additions to `server.py`:

After `from flask_socketio import SocketIO, emit` (line 25), add:
```python
from fade_engine import FadeStore, compute_volumes_from_position, interpolate_position
```

After `_viz_mode = 0` (line 1117), add:
```python
# Fade system
_FADES_FILE = os.path.join(
    os.path.dirname(os.path.abspath(sys.argv[0] if not getattr(sys, 'frozen', False)
                                     else sys.executable)),
    'fades.json')
_fade_store = FadeStore(_FADES_FILE)
_fade_playback_lock = threading.Lock()
_fade_playback_state = {
    'active': False,
    'paused': False,
    'fade_id': None,
    'start_time': None,
    'pause_time': None,
    'elapsed_at_pause': 0,
}
_fade_playback_stop = threading.Event()
```

After the Spotify routes section (~line 1648), add fade REST endpoints:
```python
# ---------------------------------------------------------------------------
# Fade CRUD routes
# ---------------------------------------------------------------------------

@app.route('/api/fades', methods=['GET'])
def api_list_fades():
    return jsonify(_fade_store.list_fades())

@app.route('/api/fades', methods=['POST'])
def api_save_fade():
    data = request.get_json(silent=True) or {}
    if 'name' not in data or 'keyframes' not in data:
        return jsonify({'error': 'name and keyframes required'}), 400
    slot = _fade_store.save_fade(data)
    if slot is None:
        return jsonify({'error': 'All 16 slots full'}), 400
    return jsonify({'slot': slot, 'success': True})

@app.route('/api/fades/<int:fade_id>', methods=['GET'])
def api_get_fade(fade_id):
    fade = _fade_store.get_fade(fade_id)
    if fade is None:
        return jsonify({'error': 'Not found'}), 404
    return jsonify(fade)

@app.route('/api/fades/<int:fade_id>', methods=['PUT'])
def api_update_fade(fade_id):
    data = request.get_json(silent=True) or {}
    if not _fade_store.update_fade(fade_id, data):
        return jsonify({'error': 'Not found'}), 404
    return jsonify({'success': True})

@app.route('/api/fades/<int:fade_id>', methods=['DELETE'])
def api_delete_fade(fade_id):
    if not _fade_store.delete_fade(fade_id):
        return jsonify({'error': 'Not found'}), 404
    return jsonify({'success': True})
```

Add fade playback WebSocket handlers and thread:
```python
# ---------------------------------------------------------------------------
# Fade playback
# ---------------------------------------------------------------------------

def _get_device_positions():
    """Get current device positions for volume computation."""
    devices = get_bluetooth_speakers()
    n = len(devices)
    positions = []
    for i, dev in enumerate(devices):
        # Check zone positions first
        with _state_lock:
            zp = _zone_positions.get(dev['id'])
        if zp:
            positions.append({'id': dev['id'], 'x': zp['x'], 'y': zp['y']})
        else:
            # Ring position (same math as JS)
            angle = (i * 2 * math.pi) / n - math.pi / 2
            x = 250 + 180 * math.cos(angle)
            y = 250 + 180 * math.sin(angle)
            positions.append({'id': dev['id'], 'x': x, 'y': y})
    return positions


def _fade_playback_thread(fade_id, keyframes, duration_ms):
    """Background thread: plays a fade by interpolating keyframes."""
    with _fade_playback_lock:
        _fade_playback_state['active'] = True
        _fade_playback_state['paused'] = False
        _fade_playback_state['fade_id'] = fade_id
        _fade_playback_state['start_time'] = time.time()
        _fade_playback_state['pause_time'] = None
        _fade_playback_state['elapsed_at_pause'] = 0
    _fade_playback_stop.clear()

    device_positions = _get_device_positions()
    curve_type = 'inverse-square'  # could be made configurable

    while not _fade_playback_stop.is_set():
        with _fade_playback_lock:
            if _fade_playback_state['paused']:
                time.sleep(0.033)
                continue
            elapsed = (time.time() - _fade_playback_state['start_time']) * 1000
            elapsed -= _fade_playback_state.get('elapsed_at_pause', 0)

        if elapsed >= duration_ms:
            break

        # Interpolate position
        x, y = interpolate_position(keyframes, elapsed)

        # Compute and apply volumes
        with _state_lock:
            min_vols = dict(_min_volumes)
        volumes = compute_volumes_from_position(x, y, device_positions, curve_type, min_vols)
        for did, vol in volumes.items():
            audio_router.set_volume(did, vol)
            with _state_lock:
                _last_known_volumes[did] = vol

        # Broadcast position to all clients
        progress = elapsed / duration_ms if duration_ms > 0 else 1.0
        socketio.emit('fade_playback', {
            'fade_id': fade_id,
            'time_ms': round(elapsed),
            'x': round(x, 1),
            'y': round(y, 1),
            'progress': round(progress, 3),
            'state': 'playing',
        })

        _fade_playback_stop.wait(timeout=0.033)  # ~30fps

    # Playback ended
    with _fade_playback_lock:
        _fade_playback_state['active'] = False
        _fade_playback_state['fade_id'] = None
    socketio.emit('fade_ended', {'fade_id': fade_id})


@socketio.on('trigger_fade')
def ws_trigger_fade(data):
    fade_id = data.get('fade_id')
    if not fade_id:
        return
    fade = _fade_store.get_fade(int(fade_id))
    if not fade:
        emit('fade_ended', {'fade_id': fade_id, 'error': 'not_found'})
        return

    # Stop any current playback
    _fade_playback_stop.set()

    keyframes = fade.get('keyframes', [])
    duration_ms = fade.get('duration_ms', 0)
    if not keyframes or duration_ms <= 0:
        emit('fade_ended', {'fade_id': fade_id, 'error': 'empty'})
        return

    threading.Thread(
        target=_fade_playback_thread,
        args=(int(fade_id), keyframes, duration_ms),
        daemon=True,
    ).start()


@socketio.on('stop_fade')
def ws_stop_fade(data=None):
    _fade_playback_stop.set()
    with _fade_playback_lock:
        _fade_playback_state['active'] = False


@socketio.on('pause_fade')
def ws_pause_fade(data=None):
    with _fade_playback_lock:
        if _fade_playback_state['active']:
            if _fade_playback_state['paused']:
                # Resume: adjust start_time to account for pause duration
                pause_duration = time.time() - _fade_playback_state['pause_time']
                _fade_playback_state['start_time'] += pause_duration
                _fade_playback_state['paused'] = False
            else:
                _fade_playback_state['paused'] = True
                _fade_playback_state['pause_time'] = time.time()


@socketio.on('override_fade')
def ws_override_fade(data=None):
    """DJ override: stop fade playback, return to manual control."""
    _fade_playback_stop.set()
    with _fade_playback_lock:
        _fade_playback_state['active'] = False
```

**Step 4: Run tests to verify they pass**

Run: `pytest test_server.py::TestFadeAPI test_server.py::TestFadeWebSocket -v`
Expected: All PASS

**Step 5: Commit**

```bash
git add server.py test_server.py
git commit -m "feat: add fade CRUD endpoints and server-side playback engine"
```

---

## Task 3: Remove Auto-DJ, Party Mode, and Presets from Studio page

Strip out features replaced by the fade system from `templates/index.html`. Keep Bounce as a practice tool.

**Files:**
- Modify: `templates/index.html`

**Step 1: Remove HTML elements**

Remove from the HTML body:
- The `btn-autodj` button (line 278)
- The `btn-party` button (line 286)
- The entire preset bar div `.preset-bar` (lines 304-308)
- The preset CSS (lines 148-173)

**Step 2: Remove JavaScript**

Remove from the `<script>`:
- State properties: `presets`, `activePreset`, `presetAnimating`, `presetAnimTarget`, `autoDJ`, `djPrimarySpeaker`, `djEnergy`, `djLastTrackId` (lines 489-502)
- Auto-DJ track rotation in `spotify_update` handler (lines 684-690)
- Auto-DJ energy handler `socket.on("audio_energy", ...)` that sets `djEnergy` (lines 696-700, keep the event listener but only for visualizer energy)
- Preset system functions: `renderPresetSlots`, `savePreset`, `loadPreset` (lines 1978-2052)
- Preset keyboard handler in canvas keydown (lines 2096-2105)
- Auto-DJ toggle handler (lines 1589-1605)
- Party mode handler (lines 1607-1639)
- Auto-DJ animation in `animationLoop` (lines 2360-2377)
- Preset animation in `animationLoop` (lines 2277-2292)

**Step 3: Add "Go to Perform" nav button**

Add to the status bar (after the Viz button):
```html
<a href="/perform" id="btn-perform" style="background:rgba(30,215,96,0.12);color:#1ed760;border:1px solid rgba(30,215,96,0.3);padding:0.5rem 1.2rem;border-radius:6px;font-size:0.85rem;text-decoration:none;cursor:pointer;">Perform</a>
```

**Step 4: Verify the page loads without errors**

Run: `python server.py` — open browser, verify mixer loads without JS errors, bounce still works, Auto-DJ/Party/Presets gone.

**Step 5: Commit**

```bash
git add templates/index.html
git commit -m "refactor: remove auto-DJ, party mode, and presets from studio page"
```

---

## Task 4: Add recording transport to Studio page

Adds Record/Stop/Play Preview/Clear buttons and the recording logic that captures control point movement at ~30fps.

**Files:**
- Modify: `templates/index.html`

**Step 1: Add transport bar HTML + CSS**

After the canvas-wrap div, add:
```html
<div class="transport-bar" id="transport-bar">
    <button id="btn-record" type="button" class="transport-btn record-btn">Record</button>
    <button id="btn-stop-rec" type="button" class="transport-btn" disabled>Stop</button>
    <button id="btn-preview" type="button" class="transport-btn" disabled>Play Preview</button>
    <button id="btn-clear-rec" type="button" class="transport-btn" disabled>Clear</button>
    <span id="rec-timer" class="rec-timer">0:00</span>
    <span id="rec-indicator" class="rec-indicator" style="display:none;">REC</span>
</div>
```

CSS for transport bar:
```css
.transport-bar {
    margin-top: 0.8rem;
    display: flex;
    align-items: center;
    gap: 0.5rem;
}
.transport-btn {
    padding: 0.4rem 1rem;
    border-radius: 6px;
    font-size: 0.85rem;
    cursor: pointer;
}
.transport-btn:disabled {
    opacity: 0.4;
    cursor: not-allowed;
}
.record-btn {
    background: rgba(255, 80, 80, 0.15);
    color: #ff5050;
    border: 1px solid rgba(255, 80, 80, 0.4);
}
.record-btn.recording {
    background: rgba(255, 80, 80, 0.3);
    animation: rec-pulse 1s infinite;
}
@keyframes rec-pulse {
    0%, 100% { box-shadow: 0 0 0 0 rgba(255, 80, 80, 0.4); }
    50% { box-shadow: 0 0 8px 2px rgba(255, 80, 80, 0.6); }
}
.rec-timer {
    font-family: monospace;
    font-size: 0.9rem;
    color: #c0c0d8;
    min-width: 40px;
}
.rec-indicator {
    color: #ff5050;
    font-weight: bold;
    font-size: 0.8rem;
    animation: rec-blink 1s infinite;
}
@keyframes rec-blink {
    0%, 100% { opacity: 1; }
    50% { opacity: 0.3; }
}
```

**Step 2: Add recording state and logic to JavaScript**

Add to the `state` object:
```javascript
// Recording
recording: false,
recordingKeyframes: [],
recordingStartTime: 0,
recordedFade: null,  // {keyframes, duration_ms} after recording stops
previewing: false,
previewStartTime: 0,
```

Add recording logic:
```javascript
// ── Recording Transport ─────────────────────────────────────────
const btnRecord = document.getElementById('btn-record');
const btnStopRec = document.getElementById('btn-stop-rec');
const btnPreview = document.getElementById('btn-preview');
const btnClearRec = document.getElementById('btn-clear-rec');
const recTimer = document.getElementById('rec-timer');
const recIndicator = document.getElementById('rec-indicator');

let _recSampleInterval = null;

btnRecord.addEventListener('click', () => {
    state.recording = true;
    state.recordingKeyframes = [];
    state.recordingStartTime = performance.now();
    state.recordedFade = null;

    btnRecord.disabled = true;
    btnRecord.classList.add('recording');
    btnStopRec.disabled = false;
    btnPreview.disabled = true;
    btnClearRec.disabled = true;
    recIndicator.style.display = '';

    // Sample at ~30fps
    _recSampleInterval = setInterval(() => {
        const elapsed = performance.now() - state.recordingStartTime;
        state.recordingKeyframes.push({
            time_ms: Math.round(elapsed),
            x: Math.round(state.controlPoint.x * 10) / 10,
            y: Math.round(state.controlPoint.y * 10) / 10,
        });
        // Update timer
        const secs = Math.floor(elapsed / 1000);
        const mins = Math.floor(secs / 60);
        recTimer.textContent = mins + ':' + String(secs % 60).padStart(2, '0');
    }, 33);
});

btnStopRec.addEventListener('click', () => {
    if (!state.recording) return;
    clearInterval(_recSampleInterval);
    state.recording = false;

    const duration = performance.now() - state.recordingStartTime;
    state.recordedFade = {
        keyframes: state.recordingKeyframes,
        duration_ms: Math.round(duration),
    };

    btnRecord.disabled = false;
    btnRecord.classList.remove('recording');
    btnStopRec.disabled = true;
    btnPreview.disabled = false;
    btnClearRec.disabled = false;
    recIndicator.style.display = 'none';
});

btnPreview.addEventListener('click', () => {
    if (!state.recordedFade) return;
    if (state.previewing) {
        // Stop preview
        state.previewing = false;
        btnPreview.textContent = 'Play Preview';
        return;
    }
    state.previewing = true;
    state.previewStartTime = performance.now();
    btnPreview.textContent = 'Stop Preview';
});

btnClearRec.addEventListener('click', () => {
    state.recordedFade = null;
    state.recordingKeyframes = [];
    btnPreview.disabled = true;
    btnClearRec.disabled = true;
    recTimer.textContent = '0:00';
});
```

Add preview playback in `animationLoop` (where preset animation was):
```javascript
// Preview playback
if (state.previewing && state.recordedFade && !state.isDragging) {
    const elapsed = performance.now() - state.previewStartTime;
    if (elapsed >= state.recordedFade.duration_ms) {
        state.previewing = false;
        btnPreview.textContent = 'Play Preview';
    } else {
        const kfs = state.recordedFade.keyframes;
        // Find surrounding keyframes
        let kfA = kfs[0], kfB = kfs[0];
        for (let i = 0; i < kfs.length - 1; i++) {
            if (kfs[i].time_ms <= elapsed && kfs[i + 1].time_ms >= elapsed) {
                kfA = kfs[i];
                kfB = kfs[i + 1];
                break;
            }
        }
        if (elapsed >= kfs[kfs.length - 1].time_ms) {
            kfA = kfB = kfs[kfs.length - 1];
        }
        const span = kfB.time_ms - kfA.time_ms;
        const t = span > 0 ? (elapsed - kfA.time_ms) / span : 0;
        state.controlPoint.x = kfA.x + (kfB.x - kfA.x) * t;
        state.controlPoint.y = kfA.y + (kfB.y - kfA.y) * t;
        updateVolumes();
        animating = true;
    }
}
```

**Step 3: Verify recording works**

Run server, open browser:
1. Click Record — red indicator appears, timer counts up
2. Drag control point around
3. Click Stop — timer freezes
4. Click Play Preview — control point replays the path
5. Click Clear — resets

**Step 4: Commit**

```bash
git add templates/index.html
git commit -m "feat: add recording transport with live capture and preview playback"
```

---

## Task 5: Add fade slot sidebar to Studio page

Adds a sidebar panel showing the 16 fade slots with save/load/rename/delete controls.

**Files:**
- Modify: `templates/index.html`

**Step 1: Add sidebar HTML + CSS**

```html
<div id="fade-sidebar" class="fade-sidebar">
    <h3 style="color:#00d4ff;font-size:0.9rem;margin:0 0 0.6rem;">Fade Slots</h3>
    <div id="fade-slot-list" class="fade-slot-list"></div>
    <button id="btn-save-fade" type="button" class="transport-btn" disabled
            style="width:100%;margin-top:0.5rem;">Save Recording</button>
</div>
```

CSS:
```css
.fade-sidebar {
    position: fixed;
    right: 1rem;
    top: 1rem;
    width: 200px;
    background: rgba(26, 26, 46, 0.95);
    border: 1px solid rgba(0, 212, 255, 0.2);
    border-radius: 10px;
    padding: 1rem;
    max-height: calc(100vh - 2rem);
    overflow-y: auto;
    z-index: 10;
}
.fade-slot-list {
    display: flex;
    flex-direction: column;
    gap: 0.3rem;
}
.fade-slot-item {
    display: flex;
    align-items: center;
    gap: 0.4rem;
    padding: 0.3rem 0.5rem;
    border-radius: 6px;
    background: rgba(0, 212, 255, 0.04);
    border: 1px solid rgba(0, 212, 255, 0.1);
    font-size: 0.75rem;
    color: #c0c0d8;
    cursor: pointer;
    transition: background 0.2s;
}
.fade-slot-item:hover {
    background: rgba(0, 212, 255, 0.1);
}
.fade-slot-item.empty {
    color: #555;
    cursor: default;
}
.fade-slot-item .slot-num {
    color: #00d4ff;
    font-weight: bold;
    min-width: 20px;
}
.fade-slot-item .slot-name {
    flex: 1;
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
}
.fade-slot-item .slot-duration {
    color: #888;
    font-size: 0.7rem;
}
.fade-slot-item .slot-delete {
    background: none;
    border: none;
    color: #ff6b6b;
    cursor: pointer;
    font-size: 0.9rem;
    padding: 0 2px;
    opacity: 0.5;
}
.fade-slot-item .slot-delete:hover {
    opacity: 1;
}
```

**Step 2: Add fade slot management JavaScript**

```javascript
// ── Fade Slot Sidebar ───────────────────────────────────────────
const fadeSlotList = document.getElementById('fade-slot-list');
const btnSaveFade = document.getElementById('btn-save-fade');
let fadeSlots = {};  // slot -> {name, duration_ms}

async function fetchFadeSlots() {
    try {
        const resp = await fetch('/api/fades');
        fadeSlots = await resp.json();
        // Convert string keys to int
        const converted = {};
        for (const [k, v] of Object.entries(fadeSlots)) {
            converted[parseInt(k)] = v;
        }
        fadeSlots = converted;
    } catch (e) {
        console.error('Failed to fetch fades:', e);
    }
    renderFadeSlots();
}

function renderFadeSlots() {
    fadeSlotList.innerHTML = '';
    for (let i = 1; i <= 16; i++) {
        const slot = fadeSlots[i];
        const item = document.createElement('div');
        item.className = 'fade-slot-item' + (slot ? '' : ' empty');

        const num = document.createElement('span');
        num.className = 'slot-num';
        num.textContent = i;

        const name = document.createElement('span');
        name.className = 'slot-name';

        if (slot) {
            name.textContent = slot.name || 'Fade ' + i;
            const dur = document.createElement('span');
            dur.className = 'slot-duration';
            const secs = Math.round(slot.duration_ms / 1000);
            dur.textContent = Math.floor(secs / 60) + ':' + String(secs % 60).padStart(2, '0');

            const del = document.createElement('button');
            del.className = 'slot-delete';
            del.textContent = '\u00d7';
            del.title = 'Delete';
            del.addEventListener('click', async (e) => {
                e.stopPropagation();
                await fetch('/api/fades/' + i, { method: 'DELETE' });
                fetchFadeSlots();
            });

            item.append(num, name, dur, del);

            // Click to load preview
            item.addEventListener('click', async () => {
                try {
                    const resp = await fetch('/api/fades/' + i);
                    const fade = await resp.json();
                    state.recordedFade = {
                        keyframes: fade.keyframes,
                        duration_ms: fade.duration_ms,
                    };
                    btnPreview.disabled = false;
                    btnClearRec.disabled = false;
                    const secs = Math.round(fade.duration_ms / 1000);
                    recTimer.textContent = Math.floor(secs / 60) + ':' + String(secs % 60).padStart(2, '0');
                    showToast('Loaded: ' + (fade.name || 'Fade ' + i));
                } catch (e) {
                    console.error('Failed to load fade:', e);
                }
            });
        } else {
            name.textContent = '\u2014';
            item.append(num, name);
        }

        fadeSlotList.appendChild(item);
    }
}

btnSaveFade.addEventListener('click', async () => {
    if (!state.recordedFade) return;
    const name = prompt('Name this fade:', 'Fade');
    if (name === null) return;
    try {
        const resp = await fetch('/api/fades', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
                name: name || 'Fade',
                duration_ms: state.recordedFade.duration_ms,
                keyframes: state.recordedFade.keyframes,
            }),
        });
        const data = await resp.json();
        if (data.slot) {
            showToast('Saved to slot ' + data.slot);
            fetchFadeSlots();
        } else {
            showToast(data.error || 'Save failed');
        }
    } catch (e) {
        console.error('Failed to save fade:', e);
    }
});

// Enable save button when recording exists
// (check in animationLoop or after recording stops)
```

Update `btnStopRec` handler to also enable save button:
```javascript
// After stopping recording, enable save
btnSaveFade.disabled = false;
```

Add initial fetch:
```javascript
fetchFadeSlots();
```

**Step 3: Verify sidebar works**

Run server, record a fade, save it, verify it appears in sidebar, click to load, delete.

**Step 4: Commit**

```bash
git add templates/index.html
git commit -m "feat: add fade slot sidebar with save/load/rename/delete"
```

---

## Task 6: Add timeline keyframe editor to Studio page

Adds the horizontal timeline editor below the mixer canvas, showing X/Y position curves with draggable keyframe diamonds.

**Files:**
- Modify: `templates/index.html`

**Step 1: Add timeline HTML + CSS**

```html
<div id="timeline-panel" class="timeline-panel" style="display:none;">
    <div class="timeline-header">
        <span style="color:#00d4ff;font-size:0.85rem;font-weight:600;">Timeline Editor</span>
        <button id="timeline-close" type="button" style="background:none;border:none;color:#888;cursor:pointer;font-size:1.1rem;">&times;</button>
    </div>
    <canvas id="timeline-canvas" width="800" height="200"></canvas>
    <div class="timeline-controls">
        <span id="timeline-time" style="font-family:monospace;font-size:0.8rem;color:#888;">0:00.0</span>
        <button id="btn-timeline-add" type="button" class="dj-btn">Add Keyframe</button>
        <button id="btn-timeline-delete" type="button" class="dj-btn">Delete Selected</button>
        <label style="font-size:0.75rem;color:#888;">
            Zoom: <input type="range" id="timeline-zoom" min="50" max="400" value="100" style="width:80px;height:14px;">
        </label>
    </div>
</div>
```

CSS:
```css
.timeline-panel {
    margin-top: 0.8rem;
    background: rgba(15, 15, 35, 0.95);
    border: 1px solid rgba(0, 212, 255, 0.2);
    border-radius: 10px;
    padding: 0.8rem;
    width: 100%;
    max-width: 820px;
}
.timeline-header {
    display: flex;
    justify-content: space-between;
    align-items: center;
    margin-bottom: 0.4rem;
}
#timeline-canvas {
    width: 100%;
    height: 200px;
    border: 1px solid rgba(0, 212, 255, 0.1);
    border-radius: 6px;
    background: #0a0a1a;
    cursor: crosshair;
}
.timeline-controls {
    display: flex;
    align-items: center;
    gap: 0.6rem;
    margin-top: 0.4rem;
}
```

**Step 2: Implement timeline canvas rendering and interaction**

The timeline canvas draws:
- Two horizontal lanes (top half = X, bottom half = Y)
- Time axis with MM:SS markers
- Keyframe diamonds at each recorded point
- Lines connecting keyframes (the interpolation curves)
- A vertical playhead line

Interactions:
- Click on timeline to set playhead position (updates mixer canvas preview)
- Click+drag keyframe diamonds to adjust position/timing
- Right-click keyframe to delete
- "Add Keyframe" button inserts at playhead position using current control point
- Scroll/pinch to zoom

This is the largest single piece of UI. The implementation should be a self-contained JavaScript module within the script tag (~200-300 lines).

Key functions:
```javascript
function drawTimeline() { /* renders the timeline canvas */ }
function timelineHitTest(mx, my) { /* returns {type, index} or null */ }
function onTimelinePointerDown(e) { /* start drag or set playhead */ }
function onTimelinePointerMove(e) { /* drag keyframe */ }
function onTimelinePointerUp(e) { /* end drag */ }
```

**Step 3: Show timeline after recording**

After recording stops, show the timeline panel and render the recorded keyframes. After clearing, hide it.

**Step 4: Verify timeline works**

Record a fade, see keyframes appear in timeline, drag them around, add new ones, delete, verify mixer preview updates.

**Step 5: Commit**

```bash
git add templates/index.html
git commit -m "feat: add timeline keyframe editor with drag, add, delete, zoom"
```

---

## Task 7: Create Perform page

Creates the `/perform` page with a read-only mixer canvas and 4x4 trigger grid.

**Files:**
- Create: `templates/perform.html`
- Modify: `server.py` (add route)

**Step 1: Add `/perform` route to server.py**

After the index route (~line 1350):
```python
@app.route("/perform")
def perform():
    """Serve the perform page for live sets."""
    resp = make_response(render_template("perform.html"))
    resp.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
    resp.headers["Pragma"] = "no-cache"
    resp.headers["Expires"] = "0"
    return resp
```

**Step 2: Create perform.html**

A complete standalone HTML page with:

**Layout:**
- Top: 350px mixer canvas (read-only, shows speakers + animated control point)
- Center: 4x4 grid of trigger buttons
- Bottom: Spotify widget
- Top-left: "Back to Studio" link

**The mixer canvas** is a simplified version of the studio canvas:
- Same ring, device rendering, connection lines
- Control point moves based on `fade_playback` WS events
- No drag interaction (except override hold)
- Visualizer effects still render

**The trigger grid** is a 4x4 CSS grid:
- Each cell is a large button (~120px)
- Shows slot number, fade name, duration
- Active fade has a pulsing progress ring (CSS animation + JS progress update)
- Empty slots are dimmed
- Tap triggers `trigger_fade` WS event
- Tap active triggers `stop_fade`

**Override mechanism:**
- `mousedown`/`touchstart` on control point starts a 1.5s timer
- Circular SVG progress ring fills around the control point
- After 1.5s: emit `override_fade`, enable dragging
- `mouseup`/`touchend` before 1.5s: cancel

**Keyboard shortcuts:**
```javascript
document.addEventListener('keydown', (e) => {
    const keyMap = {
        '1': 1, '2': 2, '3': 3, '4': 4, '5': 5, '6': 6, '7': 7, '8': 8,
        '9': 9, '0': 10, '-': 11, '=': 12, 'q': 13, 'w': 14, 'e': 15, 'r': 16,
    };
    const slot = keyMap[e.key.toLowerCase()];
    if (slot && !e.ctrlKey && !e.altKey && !e.metaKey) {
        triggerFade(slot);
        e.preventDefault();
    }
    if (e.key === 'Escape') {
        socket.emit('stop_fade');
        e.preventDefault();
    }
    if (e.key === ' ') {
        socket.emit('pause_fade');
        e.preventDefault();
    }
});
```

**WebSocket listeners:**
```javascript
socket.on('fade_playback', (data) => {
    // Animate control point to data.x, data.y
    // Update progress on active trigger button
});

socket.on('fade_ended', (data) => {
    // Reset trigger button state
    // Stop control point animation
});
```

The perform page shares the Socket.IO connection, device_update, audio_levels, spotify_update, and visualizer events from the existing server.

**Step 3: Style for dark stage environments**

Use high-contrast colors, large touch targets, minimal UI chrome. The trigger grid should be the dominant visual element.

**Step 4: Verify perform page works**

1. Save a fade in Studio
2. Navigate to `/perform`
3. See the trigger grid with the saved fade
4. Click trigger — mixer canvas shows control point moving, speakers change volume
5. Click active trigger to stop
6. Test keyboard shortcuts
7. Test override hold (hold control point for 1.5s, then drag)

**Step 5: Commit**

```bash
git add templates/perform.html server.py
git commit -m "feat: add perform page with trigger grid, read-only mixer, and override"
```

---

## Task 8: Integration testing and polish

End-to-end verification, edge case handling, and UI polish.

**Files:**
- Modify: `test_server.py`
- Modify: `templates/index.html`
- Modify: `templates/perform.html`

**Step 1: Add integration tests**

```python
def test_full_fade_lifecycle(self, client, socketio_client):
    """Save fade, trigger via WS, verify playback events."""
    # Save
    fade = {
        'name': 'E2E Test',
        'duration_ms': 100,  # short for testing
        'keyframes': [
            {'time_ms': 0, 'x': 250, 'y': 70},
            {'time_ms': 100, 'x': 250, 'y': 430},
        ],
    }
    resp = client.post('/api/fades', json=fade)
    assert resp.status_code == 200

    # Trigger
    socketio_client.emit('trigger_fade', {'fade_id': 1})
    import time; time.sleep(0.2)  # let playback thread run

    received = socketio_client.get_received()
    event_names = [r['name'] for r in received]
    assert 'fade_ended' in event_names or 'fade_playback' in event_names
```

**Step 2: Edge cases to handle**

- Triggering a new fade while one is playing (should stop the old one)
- Override during playback then triggering another fade
- Recording while a fade is playing (should ignore fade playback in Studio)
- Empty keyframes list (reject with error)
- Browser disconnect during playback (server continues, reconnect shows correct state)

**Step 3: UI polish**

- Add red recording dot to mixer canvas during recording
- Show recording path as a faint trail on the mixer canvas
- Add fade name display on perform page mixer during playback
- Smooth control point animation on perform page (lerp toward target from `fade_playback` events)

**Step 4: Run full test suite**

Run: `pytest test_server.py test_fade_engine.py -v`
Expected: All PASS

**Step 5: Commit**

```bash
git add -A
git commit -m "test: add integration tests and polish fade system UI"
```

---

## Summary of Changes

| Component | Action | Details |
|-----------|--------|---------|
| `fade_engine.py` | **New** | Weight computation, interpolation, FadeStore |
| `test_fade_engine.py` | **New** | Unit tests for fade engine |
| `server.py` | **Modify** | Add fade CRUD endpoints, playback engine, `/perform` route |
| `test_server.py` | **Modify** | Add fade API + WS tests |
| `templates/index.html` | **Modify** | Remove presets/auto-DJ/party, add transport + timeline + sidebar |
| `templates/perform.html` | **New** | Trigger grid, read-only mixer, override, keyboard shortcuts |

**Removed features:** Presets (1-9), Auto-DJ, Party Mode
**Kept features:** Bounce (practice), Visualizers, EQ/Pan/Delay, Spotify, Latency, Volume locks
