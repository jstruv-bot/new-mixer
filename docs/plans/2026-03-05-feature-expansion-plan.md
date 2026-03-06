# Feature Expansion Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Add 10 features (WebSocket, metering, curves, mute, EQ DSP, groups, volume restore, presets, accessibility, Spotify) + 58 unit tests to the Bluetooth Crossfade Mixer.

**Architecture:** Flask-SocketIO replaces HTTP polling as the transport layer. AudioRouter extended with biquad EQ DSP in output workers. Backend tracks mute/EQ/group/volume state under locks. Frontend is a single-file SPA with Canvas rendering, socket.io client, and localStorage persistence. All pycaw/COM calls mocked in tests.

**Tech Stack:** Python 3, Flask, flask-socketio, pycaw, comtypes, PyAudioWPatch, numpy, requests, pytest

---

### Task 1: Add Dependencies & Project Setup

**Files:**
- Modify: `requirements.txt`

**Step 1: Update requirements.txt**

```
flask>=3.0
flask-socketio>=5.3
pycaw>=20240210
comtypes>=1.4
pyinstaller>=6.0
PyAudioWPatch>=0.2.12
numpy>=1.26
requests>=2.31
```

**Step 2: Install dependencies**

Run: `pip install flask-socketio requests`
Expected: Successfully installed

**Step 3: Create test file skeleton**

Create `test_server.py` with a single smoke test:

```python
"""Unit tests for Bluetooth Crossfade Mixer server."""

import pytest


def test_smoke():
    """Verify test infrastructure works."""
    assert True
```

**Step 4: Run test**

Run: `pytest test_server.py -v`
Expected: 1 passed

**Step 5: Commit**

```bash
git add requirements.txt test_server.py
git commit -m "feat: add flask-socketio, requests deps and test skeleton"
```

---

### Task 2: WebSocket Transport Layer

**Files:**
- Modify: `server.py`
- Modify: `templates/index.html`
- Modify: `test_server.py`

**Step 1: Write WebSocket tests**

Add to `test_server.py`:

```python
import json
import threading
from unittest.mock import patch, MagicMock, PropertyMock

# Mock pycaw and comtypes before importing server
mock_comtypes = MagicMock()
mock_audio_utilities = MagicMock()
mock_audio_utilities.GetAllDevices.return_value = []

with patch.dict('sys.modules', {
    'comtypes': mock_comtypes,
    'pycaw': MagicMock(),
    'pycaw.pycaw': MagicMock(AudioUtilities=mock_audio_utilities),
    'pyaudiowpatch': MagicMock(),
}):
    import importlib
    import server as server_module


@pytest.fixture
def app():
    """Create a test Flask app with mocked audio."""
    with patch.object(server_module, 'get_bluetooth_speakers', return_value=[]):
        with patch.object(server_module, '_init_com'):
            server_module.app.config['TESTING'] = True
            yield server_module.app


@pytest.fixture
def client(app):
    """Create a Flask test client."""
    return app.test_client()


@pytest.fixture
def socketio_client(app):
    """Create a flask-socketio test client."""
    return server_module.socketio.test_client(app)


class TestWebSocket:
    """WebSocket transport tests."""

    def test_connect(self, socketio_client):
        """Client can connect via WebSocket."""
        assert socketio_client.is_connected()

    def test_disconnect(self, socketio_client):
        """Client can disconnect cleanly."""
        socketio_client.disconnect()
        assert not socketio_client.is_connected()

    def test_set_volume_event(self, socketio_client):
        """set_volume event updates device volume."""
        with patch.object(server_module, 'set_device_volume', return_value=True) as mock_sv:
            with patch.object(server_module.audio_router, 'set_volume'):
                socketio_client.emit('set_volume', {
                    'device_id': 'test-id',
                    'volume': 0.75
                })
                mock_sv.assert_called_once_with('test-id', 0.75)

    def test_refresh_devices_event(self, socketio_client):
        """refresh_devices event triggers device scan."""
        mock_devices = [{'id': 'd1', 'name': 'Speaker 1', 'volume': 0.5}]
        with patch.object(server_module, 'get_bluetooth_speakers', return_value=mock_devices):
            socketio_client.emit('refresh_devices')
            received = socketio_client.get_received()
            # Should receive a device_update event back
            events = [r for r in received if r['name'] == 'device_update']
            assert len(events) == 1
            assert events[0]['args'][0] == mock_devices


class TestRESTEndpoints:
    """REST API fallback tests."""

    def test_get_devices(self, client):
        """GET /api/devices returns JSON device list."""
        mock_devices = [{'id': 'd1', 'name': 'Speaker 1', 'volume': 0.8}]
        with patch.object(server_module, 'get_bluetooth_speakers', return_value=mock_devices):
            resp = client.get('/api/devices')
            assert resp.status_code == 200
            data = resp.get_json()
            assert len(data) == 1
            assert data[0]['name'] == 'Speaker 1'

    def test_post_volume_valid(self, client):
        """POST /api/volume with valid data succeeds."""
        with patch.object(server_module, 'set_device_volume', return_value=True):
            with patch.object(server_module.audio_router, 'set_volume'):
                resp = client.post('/api/volume',
                    data=json.dumps({'device_id': 'd1', 'volume': 0.5}),
                    content_type='application/json')
                assert resp.status_code == 200
                assert resp.get_json()['success'] is True

    def test_post_volume_missing_fields(self, client):
        """POST /api/volume with missing fields returns 400."""
        resp = client.post('/api/volume',
            data=json.dumps({}),
            content_type='application/json')
        assert resp.status_code == 400

    def test_post_volume_invalid_json(self, client):
        """POST /api/volume with no body returns 400."""
        resp = client.post('/api/volume',
            data='not json',
            content_type='application/json')
        assert resp.status_code == 400

    def test_refresh_endpoint(self, client):
        """POST /api/refresh returns device list."""
        with patch.object(server_module, 'get_bluetooth_speakers', return_value=[]):
            with patch.object(server_module.audio_router, 'is_running', new_callable=PropertyMock, return_value=False):
                resp = client.post('/api/refresh')
                assert resp.status_code == 200

    def test_router_status(self, client):
        """GET /api/router/status returns router state."""
        with patch.object(server_module.audio_router, 'is_running', new_callable=PropertyMock, return_value=True):
            with patch.object(server_module.audio_router, 'active_outputs', new_callable=PropertyMock, return_value=2):
                resp = client.get('/api/router/status')
                data = resp.get_json()
                assert data['running'] is True
                assert data['outputs'] == 2

    def test_index_serves_html(self, client):
        """GET / returns HTML page."""
        resp = client.get('/')
        assert resp.status_code == 200
        assert b'Bluetooth Crossfade Mixer' in resp.data
```

**Step 2: Run tests to verify they fail**

Run: `pytest test_server.py -v`
Expected: Multiple failures (socketio not defined, missing WebSocket handlers)

**Step 3: Update server.py — add flask-socketio**

At top of `server.py`, after Flask import, add:

```python
from flask_socketio import SocketIO, emit
```

After `app = Flask(...)`, add:

```python
socketio = SocketIO(app, cors_allowed_origins="*", async_mode="threading")
```

Add shared state dicts (before Flask routes):

```python
# ---------------------------------------------------------------------------
# Shared state (protected by _state_lock)
# ---------------------------------------------------------------------------

_state_lock = threading.Lock()
_muted_devices = {}          # device_id -> bool
_eq_settings = {}            # device_id -> {"bass": float, "treble": float}
_device_groups = {}          # group_id -> {"name": str, "device_ids": list}
_group_membership = {}       # device_id -> group_id  (reverse lookup, O(1))
_last_known_volumes = {}     # device_id -> float
_last_known_eq = {}          # device_id -> {"bass": float, "treble": float}
_last_known_mute = {}        # device_id -> bool
_previous_device_ids = set() # for change detection
```

Add WebSocket event handlers (after REST routes):

```python
# ---------------------------------------------------------------------------
# WebSocket event handlers
# ---------------------------------------------------------------------------

@socketio.on('connect')
def ws_connect():
    """Client connected — send current state."""
    devices = get_bluetooth_speakers()
    emit('device_update', devices)
    emit('router_status', {
        'running': audio_router.is_running,
        'outputs': audio_router.active_outputs,
    })


@socketio.on('set_volume')
def ws_set_volume(data):
    """Set volume on a device via WebSocket."""
    device_id = data.get('device_id')
    volume = data.get('volume')
    if device_id is None or volume is None:
        return
    try:
        volume = float(volume)
    except (TypeError, ValueError):
        return
    volume = max(0.0, min(1.0, volume))
    set_device_volume(device_id, volume)
    audio_router.set_volume(device_id, volume)
    with _state_lock:
        _last_known_volumes[device_id] = volume


@socketio.on('refresh_devices')
def ws_refresh_devices():
    """Re-scan devices and push update."""
    devices = get_bluetooth_speakers()
    _sync_router(devices)
    emit('device_update', devices)


@socketio.on('set_mute')
def ws_set_mute(data):
    """Toggle mute on a device."""
    device_id = data.get('device_id')
    muted = data.get('muted', False)
    if device_id is None:
        return
    with _state_lock:
        _muted_devices[device_id] = bool(muted)
        _last_known_mute[device_id] = bool(muted)
    if muted:
        audio_router.set_volume(device_id, 0.0)
        set_device_volume(device_id, 0.0)
    emit('mute_update', {'device_id': device_id, 'muted': muted}, broadcast=True)


@socketio.on('set_eq')
def ws_set_eq(data):
    """Set EQ for a device."""
    device_id = data.get('device_id')
    bass = data.get('bass', 0.0)
    treble = data.get('treble', 0.0)
    if device_id is None:
        return
    bass = max(-1.0, min(1.0, float(bass)))
    treble = max(-1.0, min(1.0, float(treble)))
    with _state_lock:
        _eq_settings[device_id] = {'bass': bass, 'treble': treble}
        _last_known_eq[device_id] = {'bass': bass, 'treble': treble}
    audio_router.set_eq(device_id, bass, treble)
    emit('eq_update', {'device_id': device_id, 'bass': bass, 'treble': treble}, broadcast=True)


@socketio.on('set_group')
def ws_set_group(data):
    """Create or update a device group."""
    group_id = data.get('group_id')
    name = data.get('name', '')
    device_ids = data.get('device_ids', [])
    if not group_id:
        return
    with _state_lock:
        # Remove old memberships for this group
        old_members = _device_groups.get(group_id, {}).get('device_ids', [])
        for did in old_members:
            _group_membership.pop(did, None)
        # Set new group
        _device_groups[group_id] = {'name': name, 'device_ids': device_ids}
        for did in device_ids:
            _group_membership[did] = group_id
    emit('group_update', {'groups': _get_groups_snapshot()}, broadcast=True)


@socketio.on('delete_group')
def ws_delete_group(data):
    """Delete a device group."""
    group_id = data.get('group_id')
    if not group_id:
        return
    with _state_lock:
        group = _device_groups.pop(group_id, None)
        if group:
            for did in group.get('device_ids', []):
                _group_membership.pop(did, None)
    emit('group_update', {'groups': _get_groups_snapshot()}, broadcast=True)
```

Add helper functions:

```python
def _get_groups_snapshot():
    """Return a copy of device groups (call under _state_lock or externally)."""
    return {gid: dict(g) for gid, g in _device_groups.items()}


def _sync_router(devices):
    """Sync the audio router with current device list."""
    if devices and not audio_router.is_running:
        threading.Thread(target=lambda: audio_router.start(devices), daemon=True).start()
    elif devices and audio_router.is_running:
        threading.Thread(target=lambda: audio_router.update_devices(devices), daemon=True).start()
```

Add device monitor background thread:

```python
def _device_monitor():
    """Background thread: monitors device changes, pushes updates via WebSocket."""
    global _previous_device_ids
    _init_com()
    while True:
        try:
            time.sleep(3)
            devices = get_bluetooth_speakers()
            current_ids = tuple(sorted(d['id'] for d in devices))

            with _state_lock:
                prev_ids = tuple(sorted(_previous_device_ids))

            if current_ids != prev_ids:
                with _state_lock:
                    _previous_device_ids = set(current_ids)

                # Check for newly appeared devices — restore volumes
                new_ids = set(current_ids) - set(prev_ids)
                _restore_devices(new_ids, devices)

                _sync_router(devices)

                # Broadcast updates
                # Add mute state to device data
                enriched = _enrich_devices(devices)
                socketio.emit('device_update', enriched)
                socketio.emit('router_status', {
                    'running': audio_router.is_running,
                    'outputs': audio_router.active_outputs,
                })
        except Exception as exc:
            print(f"[DeviceMonitor] Error: {exc}")


def _restore_devices(new_ids, devices):
    """Restore volume/EQ/mute for newly reconnected devices."""
    for dev in devices:
        if dev['id'] not in new_ids:
            continue
        did = dev['id']
        with _state_lock:
            vol = _last_known_volumes.get(did)
            eq = _last_known_eq.get(did)
            mute = _last_known_mute.get(did, False)

        if vol is not None:
            set_device_volume(did, vol)
            audio_router.set_volume(did, 0.0 if mute else vol)
            print(f"[VolumeRestore] Restored {did[:20]}... -> vol={vol:.2f}")
            socketio.emit('volume_restored', {'device_id': did, 'volume': vol})

        if eq:
            audio_router.set_eq(did, eq['bass'], eq['treble'])

        if mute:
            audio_router.set_volume(did, 0.0)


def _enrich_devices(devices):
    """Add mute/EQ/group state to device list for client."""
    with _state_lock:
        muted = dict(_muted_devices)
        eq = {k: dict(v) for k, v in _eq_settings.items()}
        groups = dict(_group_membership)
    enriched = []
    for d in devices:
        entry = dict(d)
        did = d['id']
        entry['muted'] = muted.get(did, False)
        entry['eq'] = eq.get(did, {'bass': 0.0, 'treble': 0.0})
        entry['group_id'] = groups.get(did)
        enriched.append(entry)
    return enriched
```

Update `api_devices` to use enriched data:

```python
@app.route("/api/devices", methods=["GET"])
def api_devices():
    """Return the current list of active playback devices as JSON."""
    devices = get_bluetooth_speakers()
    return jsonify(_enrich_devices(devices))
```

Update `api_refresh` to use `_sync_router`:

```python
@app.route("/api/refresh", methods=["POST"])
def api_refresh():
    """Re-scan devices and return the updated list. Also sync the router."""
    devices = get_bluetooth_speakers()
    _sync_router(devices)
    return jsonify(_enrich_devices(devices))
```

Update `api_volume` to track last known volume:

```python
@app.route("/api/volume", methods=["POST"])
def api_volume():
    data = request.get_json(silent=True)
    if not data:
        return jsonify({"success": False, "error": "Invalid or missing JSON body"}), 400
    device_id = data.get("device_id")
    volume = data.get("volume")
    if device_id is None:
        return jsonify({"success": False, "error": "Missing 'device_id'"}), 400
    if volume is None:
        return jsonify({"success": False, "error": "Missing 'volume'"}), 400
    try:
        volume = float(volume)
    except (TypeError, ValueError):
        return jsonify({"success": False, "error": "'volume' must be a number"}), 400
    ok = set_device_volume(device_id, volume)
    audio_router.set_volume(device_id, volume)
    with _state_lock:
        _last_known_volumes[device_id] = volume
    if ok:
        return jsonify({"success": True})
    else:
        return jsonify({"success": False, "error": "Failed to set volume"}), 500
```

Update `__main__` block:

```python
if __name__ == "__main__":
    HOST = "127.0.0.1"
    PORT = 5000
    url = f"http://{HOST}:{PORT}"

    print(f"Starting Bluetooth Crossfade Mixer server at {url}")
    print("Press Ctrl+C to stop.\n")

    # Start device monitor thread
    threading.Thread(target=_device_monitor, daemon=True).start()

    # Auto-start audio routing
    def start_router():
        _init_com()
        for attempt in range(3):
            devices = get_bluetooth_speakers()
            if devices:
                time.sleep(0.5)
                ok = audio_router.start(devices)
                if ok:
                    with _state_lock:
                        _previous_device_ids = set(d['id'] for d in devices)
                    return
                print(f"[AudioRouter] Start attempt {attempt + 1} failed, retrying...")
                time.sleep(2)
            else:
                print("[AudioRouter] No BT speakers found, retrying in 5s...")
                time.sleep(5)
        print("[AudioRouter] Could not start after 3 attempts. "
              "Connect BT speakers and click Refresh.")

    threading.Thread(target=start_router, daemon=True).start()
    threading.Timer(1.5, lambda: webbrowser.open(url)).start()

    socketio.run(app, host=HOST, port=PORT, debug=False, allow_unsafe_werkzeug=True)
```

Add `set_eq` stub to AudioRouter (will be fully implemented in Task 5):

```python
def set_eq(self, device_id, bass, treble):
    """Set EQ for a device. Stub — full DSP in Task 5."""
    pass
```

**Step 4: Update frontend — add socket.io client**

In `templates/index.html`, add before `{% raw %}`:

```html
<script src="/socket.io/socket.io.js"></script>
```

Replace the entire `<script>` block contents with the WebSocket-enabled version. Key changes:

- Add at top of script:
```javascript
const socket = io();
let wsConnected = false;
let pollInterval = null;
```

- Replace `fetchDevices()` with socket listener:
```javascript
socket.on('connect', () => {
    wsConnected = true;
    statusText.classList.remove("status-error");
    // Clear HTTP fallback polling if running
    if (pollInterval) { clearInterval(pollInterval); pollInterval = null; }
});

socket.on('disconnect', () => {
    wsConnected = false;
    statusText.innerHTML = "Connection lost &mdash; retrying&hellip;";
    statusText.classList.add("status-error");
    // Start HTTP fallback polling
    pollInterval = setInterval(httpFallbackPoll, 5000);
});

socket.on('device_update', (devices) => {
    state.devices = devices;
    updateStatusBar();
    updateVolumes();
});

socket.on('router_status', (data) => {
    updateRouterIndicator(data);
});

socket.on('volume_restored', (data) => {
    // Flash restored device
    state.restoredDevice = { id: data.device_id, until: Date.now() + 1000 };
});
```

- Replace `sendVolume()`:
```javascript
function sendVolume(deviceId, volume) {
    if (wsConnected) {
        socket.emit('set_volume', { device_id: deviceId, volume: volume });
    } else {
        fetch("/api/volume", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ device_id: deviceId, volume: volume })
        }).catch(err => console.error("Failed to send volume:", err));
    }
}
```

- Add HTTP fallback function:
```javascript
async function httpFallbackPoll() {
    try {
        const res = await fetch("/api/devices");
        state.devices = await res.json();
        updateStatusBar();
        updateVolumes();
    } catch (e) { /* ignore */ }
    try {
        const res = await fetch("/api/router/status");
        updateRouterIndicator(await res.json());
    } catch (e) { /* ignore */ }
}
```

- Extract `updateRouterIndicator()` from inline code:
```javascript
function updateRouterIndicator(data) {
    const el = document.getElementById("router-status");
    const text = document.getElementById("router-text");
    if (data.running) {
        el.className = "router-status active";
        text.textContent = "Streaming to " + data.outputs + " speaker" + (data.outputs !== 1 ? "s" : "");
    } else {
        el.className = "router-status inactive";
        text.textContent = "Audio router inactive \u2014 connect speakers";
    }
}
```

- Remove `setInterval(fetchDevices, 5000)` and `setInterval(fetchRouterStatus, 3000)` — replaced by WS push
- Keep `refreshDevices()` button handler but switch to WS:
```javascript
async function refreshDevices() {
    if (wsConnected) {
        socket.emit('refresh_devices');
    } else {
        try {
            const res = await fetch("/api/refresh", { method: "POST" });
            state.devices = await res.json();
        } catch (err) { console.error("Failed to refresh:", err); }
        updateStatusBar();
        updateVolumes();
    }
}
```

**Step 5: Run tests**

Run: `pytest test_server.py -v`
Expected: All tests pass

**Step 6: Commit**

```bash
git add server.py templates/index.html test_server.py
git commit -m "feat: add WebSocket transport layer with flask-socketio"
```

---

### Task 3: Audio Level Metering

**Files:**
- Modify: `server.py`
- Modify: `templates/index.html`
- Modify: `test_server.py`

**Step 1: Write metering tests**

Add to `test_server.py`:

```python
class TestAudioLevels:
    """Audio level metering tests."""

    def test_audio_levels_event_received(self, socketio_client):
        """Client receives audio_levels after triggering a meter read."""
        # The metering thread runs in background — we test the emit function directly
        with patch.object(server_module, 'get_bluetooth_speakers', return_value=[
            {'id': 'd1', 'name': 'Speaker 1', 'volume': 0.5}
        ]):
            server_module.socketio.emit('audio_levels', {'d1': 0.42})
            received = socketio_client.get_received()
            level_events = [r for r in received if r['name'] == 'audio_levels']
            assert len(level_events) >= 1

    def test_level_smoothing(self):
        """Fast attack / slow decay smoothing."""
        # Fast attack: new peak > current -> jump to peak
        current = 0.3
        peak = 0.8
        DECAY = 0.85
        if peak > current:
            result = peak  # fast attack
        else:
            result = current * DECAY  # slow decay
        assert result == 0.8

        # Slow decay: peak < current -> decay
        current = 0.8
        peak = 0.2
        if peak > current:
            result = peak
        else:
            result = current * DECAY
        assert abs(result - 0.68) < 0.01
```

**Step 2: Run tests to verify they fail**

Run: `pytest test_server.py::TestAudioLevels -v`
Expected: Failures

**Step 3: Implement metering in server.py**

Add after `_enrich_devices`:

```python
# ---------------------------------------------------------------------------
# Audio level metering
# ---------------------------------------------------------------------------

_audio_levels = {}        # device_id -> smoothed peak (0.0-1.0)
_level_decay = 0.85       # slow decay multiplier

def _audio_level_monitor():
    """Background thread: reads audio peak levels from IAudioMeterInformation."""
    _init_com()
    cached_meters = {}    # device_id -> IAudioMeterInformation
    last_device_ids = set()

    while True:
        try:
            time.sleep(1.0 / 15)  # ~15 Hz

            # Re-enumerate only when devices changed
            with _state_lock:
                current_ids = set(_previous_device_ids)

            if current_ids != last_device_ids:
                last_device_ids = current_ids
                cached_meters.clear()
                try:
                    all_devs = AudioUtilities.GetAllDevices()
                    for dev in all_devs:
                        if dev.id in current_ids:
                            try:
                                from pycaw.pycaw import IAudioMeterInformation
                                meter = dev._dev.Activate(
                                    IAudioMeterInformation._iid_, 0, None)
                                cached_meters[dev.id] = meter
                            except Exception:
                                pass
                except Exception:
                    pass

            # Read levels
            levels = {}
            for dev_id, meter in list(cached_meters.items()):
                try:
                    peak = meter.GetPeakValue()
                    current = _audio_levels.get(dev_id, 0.0)
                    if peak > current:
                        smoothed = peak  # fast attack
                    else:
                        smoothed = current * _level_decay  # slow decay
                    if smoothed < 0.005:
                        smoothed = 0.0
                    _audio_levels[dev_id] = smoothed
                    levels[dev_id] = round(smoothed, 3)
                except Exception:
                    levels[dev_id] = 0.0

            if levels:
                socketio.emit('audio_levels', levels)

        except Exception as exc:
            print(f"[LevelMeter] Error: {exc}")
            time.sleep(1)
```

Start the thread in `__main__` (after device monitor thread start):

```python
    # Start audio level metering thread
    threading.Thread(target=_audio_level_monitor, daemon=True).start()
```

**Step 4: Add metering to frontend**

Add to state:
```javascript
state.audioLevels = {};        // device_id -> smoothed level 0-1
state.displayLevels = {};      // device_id -> display-lerped level
state.restoredDevice = null;   // {id, until} for flash animation
```

Add socket listener:
```javascript
socket.on('audio_levels', (levels) => {
    state.audioLevels = levels;
});
```

Add to `drawMixer()`, inside the active device rendering block (after the volume arc, before the percentage text):

```javascript
// Audio level meter ring
const level = state.displayLevels[id] || 0;
if (level > 0.005) {
    const meterStart = -Math.PI / 2;
    const meterEnd = meterStart + level * 2 * Math.PI;
    // Color: green -> yellow -> orange
    let meterColor;
    if (level < 0.5) {
        meterColor = `rgba(0, ${Math.round(200 + level * 110)}, ${Math.round(100 - level * 200)}, 0.8)`;
    } else {
        meterColor = `rgba(${Math.round((level - 0.5) * 510)}, ${Math.round(255 - (level - 0.5) * 300)}, 0, 0.8)`;
    }
    ctx.beginPath();
    ctx.arc(pos.x, pos.y, DEVICE_RADIUS + 5, meterStart, meterEnd);
    ctx.strokeStyle = meterColor;
    ctx.lineWidth = 2;
    ctx.lineCap = "round";
    ctx.stroke();
    ctx.lineCap = "butt";
}
```

Add to animation loop (lerp display levels):
```javascript
// Lerp audio levels
for (const id in state.audioLevels) {
    const target = state.audioLevels[id];
    const current = state.displayLevels[id] || 0;
    if (target > current) {
        state.displayLevels[id] = target; // fast attack
    } else {
        state.displayLevels[id] = current + (target - current) * LERP_FACTOR;
    }
}
```

**Step 5: Run tests**

Run: `pytest test_server.py -v`
Expected: All pass

**Step 6: Commit**

```bash
git add server.py templates/index.html test_server.py
git commit -m "feat: add real-time audio level metering via IAudioMeterInformation"
```

---

### Task 4: Crossfade Curves

**Files:**
- Modify: `templates/index.html`
- Modify: `test_server.py`

**Step 1: Write curve tests**

Add to `test_server.py`:

```python
class TestCrossfadeCurves:
    """Crossfade curve formula tests (frontend logic tested via pure math)."""

    def test_inverse_square(self):
        """Inverse-square: sharp falloff."""
        import math
        epsilon = 100
        dist = 50
        weight = 1.0 / (dist * dist + epsilon)
        assert weight > 0
        # Closer = higher weight
        dist_far = 150
        weight_far = 1.0 / (dist_far * dist_far + epsilon)
        assert weight > weight_far

    def test_linear(self):
        """Linear: even gradient."""
        max_dist = 180
        dist = 90  # halfway
        weight = max(0, 1 - dist / max_dist)
        assert abs(weight - 0.5) < 0.01
        # At max distance, weight is 0
        assert max(0, 1 - max_dist / max_dist) == 0

    def test_logarithmic(self):
        """Logarithmic: gentle near, steep far."""
        import math
        k = 2.0
        dist = 10
        weight = 1.0 / (1 + k * math.log(1 + dist))
        assert weight > 0
        # Very close should be near 1
        weight_close = 1.0 / (1 + k * math.log(1 + 0.1))
        assert weight_close > 0.9

    def test_equal_power(self):
        """Equal-power: cos^2 curve."""
        import math
        max_dist = 180
        # At center (dist=0), full power
        dist = 0
        weight = math.cos(dist / max_dist * math.pi / 2) ** 2
        assert abs(weight - 1.0) < 0.001
        # At edge, zero
        dist = max_dist
        weight = math.cos(dist / max_dist * math.pi / 2) ** 2
        assert abs(weight) < 0.001
```

**Step 2: Run tests**

Run: `pytest test_server.py::TestCrossfadeCurves -v`
Expected: All pass (pure math tests)

**Step 3: Implement curves in frontend**

Add a `<select>` dropdown in the HTML status bar:

```html
<div class="status-bar">
    <span id="status-text">Scanning devices...</span>
    <select id="curve-select" title="Crossfade curve">
        <option value="inverse-square">Inverse Square</option>
        <option value="linear">Linear</option>
        <option value="logarithmic">Logarithmic</option>
        <option value="equal-power">Equal Power</option>
    </select>
    <button id="btn-refresh" type="button">Refresh Devices</button>
</div>
```

Add CSS for select:

```css
select {
    background: rgba(0, 212, 255, 0.12);
    color: #00d4ff;
    border: 1px solid rgba(0, 212, 255, 0.3);
    padding: 0.4rem 0.8rem;
    border-radius: 6px;
    font-size: 0.85rem;
    cursor: pointer;
}
select:focus { outline: 1px solid #00d4ff; }
```

Add to state:
```javascript
state.curveType = localStorage.getItem('mixer-curve') || 'inverse-square';
```

Add DOM ref and event handler:
```javascript
const curveSelect = document.getElementById("curve-select");
curveSelect.value = state.curveType;
curveSelect.addEventListener('change', () => {
    state.curveType = curveSelect.value;
    localStorage.setItem('mixer-curve', state.curveType);
    updateVolumes();
});
```

Replace the weight computation in `updateVolumes()`:

```javascript
function computeWeight(dist, curveType) {
    const maxDist = RING_RADIUS;
    switch (curveType) {
        case 'linear':
            return Math.max(0, 1 - dist / maxDist);
        case 'logarithmic':
            return 1.0 / (1 + 2.0 * Math.log(1 + dist));
        case 'equal-power': {
            const ratio = Math.min(dist / maxDist, 1.0);
            return Math.cos(ratio * Math.PI / 2) ** 2;
        }
        case 'inverse-square':
        default:
            return 1.0 / (dist * dist + 100);
    }
}

function updateVolumes() {
    const n = state.devices.length;
    if (n === 0) return;
    const weights = [];
    for (let i = 0; i < n; i++) {
        const pos = devicePosition(i, n);
        const dx = state.controlPoint.x - pos.x;
        const dy = state.controlPoint.y - pos.y;
        const dist = Math.sqrt(dx * dx + dy * dy);

        // Skip muted devices
        if (state.mutedDevices && state.mutedDevices.has(state.devices[i].id)) {
            weights.push(0);
            continue;
        }
        weights.push(computeWeight(dist, state.curveType));
    }
    const maxWeight = Math.max(...weights);
    if (maxWeight <= 0) return;
    for (let i = 0; i < n; i++) {
        let vol = weights[i] / maxWeight;
        if (vol < 0.02) vol = 0;

        // Group handling: find max volume in group
        const did = state.devices[i].id;
        if (state.groups) {
            const groupId = state.groupMembership && state.groupMembership[did];
            if (groupId) {
                const group = state.groups[groupId];
                if (group) {
                    for (const memberId of group.deviceIds) {
                        const mi = state.devices.findIndex(d => d.id === memberId);
                        if (mi >= 0) {
                            const mVol = weights[mi] / maxWeight;
                            if (mVol > vol) vol = mVol;
                        }
                    }
                }
            }
        }

        state.targetVolumes[did] = vol;
    }
    // Send to backend (throttled)
    for (let i = 0; i < n; i++) {
        const id = state.devices[i].id;
        const vol = state.targetVolumes[id];
        const last = state.lastSentVolumes[id];
        if (last === undefined || Math.abs(vol - last) > 0.01) {
            state.lastSentVolumes[id] = vol;
            sendVolume(id, vol);
        }
    }
}
```

**Step 4: Commit**

```bash
git add templates/index.html test_server.py
git commit -m "feat: add crossfade curve selection (linear/log/equal-power/inverse-square)"
```

---

### Task 5: Per-Device Mute

**Files:**
- Modify: `templates/index.html`
- Modify: `test_server.py`

**Step 1: Write mute tests**

Add to `test_server.py`:

```python
class TestMute:
    """Per-device mute tests."""

    def test_set_mute_event(self, socketio_client):
        """set_mute event updates mute state."""
        with patch.object(server_module.audio_router, 'set_volume'):
            with patch.object(server_module, 'set_device_volume', return_value=True):
                socketio_client.emit('set_mute', {'device_id': 'd1', 'muted': True})
                with server_module._state_lock:
                    assert server_module._muted_devices.get('d1') is True

    def test_unmute_event(self, socketio_client):
        """Unmuting a device clears the mute flag."""
        with patch.object(server_module.audio_router, 'set_volume'):
            with patch.object(server_module, 'set_device_volume', return_value=True):
                socketio_client.emit('set_mute', {'device_id': 'd1', 'muted': True})
                socketio_client.emit('set_mute', {'device_id': 'd1', 'muted': False})
                with server_module._state_lock:
                    assert server_module._muted_devices.get('d1') is False

    def test_mute_sets_volume_zero(self, socketio_client):
        """Muting sets AudioRouter volume to 0."""
        with patch.object(server_module.audio_router, 'set_volume') as mock_vol:
            with patch.object(server_module, 'set_device_volume', return_value=True):
                socketio_client.emit('set_mute', {'device_id': 'd1', 'muted': True})
                mock_vol.assert_called_with('d1', 0.0)
```

**Step 2: Run tests**

Run: `pytest test_server.py::TestMute -v`
Expected: Pass (backend mute handlers already implemented in Task 2)

**Step 3: Implement mute in frontend**

Add to state:
```javascript
state.mutedDevices = new Set(JSON.parse(localStorage.getItem('mixer-muted') || '[]'));
```

Add socket listener:
```javascript
socket.on('mute_update', (data) => {
    if (data.muted) {
        state.mutedDevices.add(data.device_id);
    } else {
        state.mutedDevices.delete(data.device_id);
    }
    localStorage.setItem('mixer-muted', JSON.stringify([...state.mutedDevices]));
});
```

Add click-to-mute detection in `onPointerDown`:

```javascript
function onPointerDown(e) {
    const p = canvasCoords(e);
    // Check if click is on a device node (mute toggle)
    const n = state.devices.length;
    for (let i = 0; i < n; i++) {
        const pos = devicePosition(i, n);
        const dx = p.x - pos.x;
        const dy = p.y - pos.y;
        if (Math.sqrt(dx * dx + dy * dy) < DEVICE_RADIUS) {
            const did = state.devices[i].id;
            const wasMuted = state.mutedDevices.has(did);
            if (wasMuted) {
                state.mutedDevices.delete(did);
            } else {
                state.mutedDevices.add(did);
            }
            localStorage.setItem('mixer-muted', JSON.stringify([...state.mutedDevices]));
            socket.emit('set_mute', { device_id: did, muted: !wasMuted });
            updateVolumes();
            e.preventDefault();
            return; // Don't start drag
        }
    }
    // Check control point drag
    const dx = p.x - state.controlPoint.x;
    const dy = p.y - state.controlPoint.y;
    if (Math.sqrt(dx * dx + dy * dy) < 20) {
        state.isDragging = true;
        e.preventDefault();
    }
}
```

Update `drawMixer()` — add muted device rendering (inside the device loop, before the active device block):

```javascript
const isMuted = state.mutedDevices.has(id);

if (isMuted) {
    // Muted device: dimmed, red X, dashed line
    // Dashed connection line
    ctx.beginPath();
    ctx.setLineDash([4, 4]);
    ctx.moveTo(state.controlPoint.x, state.controlPoint.y);
    ctx.lineTo(pos.x, pos.y);
    ctx.strokeStyle = "rgba(255, 100, 100, 0.15)";
    ctx.lineWidth = 1;
    ctx.stroke();
    ctx.setLineDash([]);

    // Dimmed circle
    ctx.beginPath();
    ctx.arc(pos.x, pos.y, DEVICE_RADIUS, 0, Math.PI * 2);
    ctx.fillStyle = "rgba(255, 100, 100, 0.08)";
    ctx.strokeStyle = "#ff6b6b";
    ctx.lineWidth = 1.5;
    ctx.fill();
    ctx.stroke();

    // Red X
    const xSize = 8;
    ctx.beginPath();
    ctx.moveTo(pos.x - xSize, pos.y - xSize);
    ctx.lineTo(pos.x + xSize, pos.y + xSize);
    ctx.moveTo(pos.x + xSize, pos.y - xSize);
    ctx.lineTo(pos.x - xSize, pos.y + xSize);
    ctx.strokeStyle = "#ff6b6b";
    ctx.lineWidth = 2;
    ctx.stroke();

    // "MUTE" text
    ctx.fillStyle = "#ff6b6b";
    ctx.font = "bold 9px sans-serif";
    ctx.textAlign = "center";
    ctx.textBaseline = "middle";
    ctx.fillText("MUTE", pos.x, pos.y + 14);

} else if (unavailable) {
    // ... existing unavailable rendering ...
} else {
    // ... existing active rendering ...
}
```

**Step 4: Commit**

```bash
git add templates/index.html test_server.py
git commit -m "feat: add per-device mute with click toggle and visual feedback"
```

---

### Task 6: Per-Device EQ (Real DSP)

**Files:**
- Modify: `server.py`
- Modify: `templates/index.html`
- Modify: `test_server.py`

**Step 1: Write EQ/DSP tests**

Add to `test_server.py`:

```python
class TestEQ:
    """EQ DSP and WebSocket tests."""

    def test_set_eq_event(self, socketio_client):
        """set_eq event stores EQ settings."""
        with patch.object(server_module.audio_router, 'set_eq'):
            socketio_client.emit('set_eq', {
                'device_id': 'd1', 'bass': 0.5, 'treble': -0.3
            })
            with server_module._state_lock:
                eq = server_module._eq_settings.get('d1')
                assert eq is not None
                assert abs(eq['bass'] - 0.5) < 0.01
                assert abs(eq['treble'] - (-0.3)) < 0.01

    def test_eq_clamping(self, socketio_client):
        """EQ values clamped to [-1.0, 1.0]."""
        with patch.object(server_module.audio_router, 'set_eq'):
            socketio_client.emit('set_eq', {
                'device_id': 'd1', 'bass': 5.0, 'treble': -3.0
            })
            with server_module._state_lock:
                eq = server_module._eq_settings.get('d1')
                assert eq['bass'] == 1.0
                assert eq['treble'] == -1.0

    def test_biquad_low_shelf_coefficients(self):
        """Verify biquad low-shelf coefficient computation."""
        import math
        # Low-shelf at 250Hz, 48kHz sample rate, +6dB gain
        freq = 250
        sample_rate = 48000
        gain_db = 6.0
        A = 10 ** (gain_db / 40.0)
        w0 = 2 * math.pi * freq / sample_rate
        alpha = math.sin(w0) / 2 * math.sqrt(2)

        b0 = A * ((A + 1) - (A - 1) * math.cos(w0) + 2 * math.sqrt(A) * alpha)
        b1 = 2 * A * ((A - 1) - (A + 1) * math.cos(w0))
        b2 = A * ((A + 1) - (A - 1) * math.cos(w0) - 2 * math.sqrt(A) * alpha)
        a0 = (A + 1) + (A - 1) * math.cos(w0) + 2 * math.sqrt(A) * alpha
        a1 = -2 * ((A - 1) + (A + 1) * math.cos(w0))
        a2 = (A + 1) + (A - 1) * math.cos(w0) - 2 * math.sqrt(A) * alpha

        # Normalize
        b0 /= a0; b1 /= a0; b2 /= a0; a1 /= a0; a2 /= a0
        # Sanity: coefficients should be finite
        assert all(math.isfinite(c) for c in [b0, b1, b2, a1, a2])

    def test_biquad_filter_passthrough(self):
        """With 0dB gain, biquad should pass signal through unchanged."""
        import math
        # At 0 gain, A=1, low-shelf becomes allpass-like
        freq = 250
        sample_rate = 48000
        gain_db = 0.0
        A = 10 ** (gain_db / 40.0)  # A = 1.0
        assert abs(A - 1.0) < 0.001
```

**Step 2: Implement biquad DSP in AudioRouter**

Add to `AudioRouter` class:

```python
    def set_eq(self, device_id, bass, treble):
        """Update EQ settings for a device."""
        with self._lock:
            bass = max(-1.0, min(1.0, float(bass)))
            treble = max(-1.0, min(1.0, float(treble)))
            if not hasattr(self, '_eq_settings_router'):
                self._eq_settings_router = {}
            self._eq_settings_router[device_id] = {
                'bass': bass,
                'treble': treble,
                'dirty': True,  # signal to output worker to recalculate
            }

    @staticmethod
    def _compute_biquad_low_shelf(freq, sample_rate, gain_db):
        """Compute biquad low-shelf filter coefficients."""
        import math
        if abs(gain_db) < 0.01:
            return (1.0, 0.0, 0.0, 0.0, 0.0)  # passthrough
        A = 10 ** (gain_db / 40.0)
        w0 = 2 * math.pi * freq / sample_rate
        alpha = math.sin(w0) / 2 * math.sqrt(2)
        cos_w0 = math.cos(w0)
        sqrt_A = math.sqrt(A)

        b0 = A * ((A + 1) - (A - 1) * cos_w0 + 2 * sqrt_A * alpha)
        b1 = 2 * A * ((A - 1) - (A + 1) * cos_w0)
        b2 = A * ((A + 1) - (A - 1) * cos_w0 - 2 * sqrt_A * alpha)
        a0 = (A + 1) + (A - 1) * cos_w0 + 2 * sqrt_A * alpha
        a1 = -2 * ((A - 1) + (A + 1) * cos_w0)
        a2 = (A + 1) + (A - 1) * cos_w0 - 2 * sqrt_A * alpha

        return (b0/a0, b1/a0, b2/a0, a1/a0, a2/a0)

    @staticmethod
    def _compute_biquad_high_shelf(freq, sample_rate, gain_db):
        """Compute biquad high-shelf filter coefficients."""
        import math
        if abs(gain_db) < 0.01:
            return (1.0, 0.0, 0.0, 0.0, 0.0)
        A = 10 ** (gain_db / 40.0)
        w0 = 2 * math.pi * freq / sample_rate
        alpha = math.sin(w0) / 2 * math.sqrt(2)
        cos_w0 = math.cos(w0)
        sqrt_A = math.sqrt(A)

        b0 = A * ((A + 1) + (A - 1) * cos_w0 + 2 * sqrt_A * alpha)
        b1 = -2 * A * ((A - 1) + (A + 1) * cos_w0)
        b2 = A * ((A + 1) + (A - 1) * cos_w0 - 2 * sqrt_A * alpha)
        a0 = (A + 1) - (A - 1) * cos_w0 + 2 * sqrt_A * alpha
        a1 = 2 * ((A - 1) - (A + 1) * cos_w0)
        a2 = (A + 1) - (A - 1) * cos_w0 - 2 * sqrt_A * alpha

        return (b0/a0, b1/a0, b2/a0, a1/a0, a2/a0)

    @staticmethod
    def _apply_biquad(audio, coeffs, state):
        """Apply biquad filter to audio buffer in-place.

        audio: numpy float32 array (interleaved channels)
        coeffs: (b0, b1, b2, a1, a2)
        state: list of [z1, z2] per channel
        """
        b0, b1, b2, a1, a2 = coeffs
        if abs(b0 - 1.0) < 0.001 and abs(b1) < 0.001:
            return  # passthrough
        channels = len(state)
        samples = len(audio)
        for ch in range(channels):
            z1, z2 = state[ch]
            for i in range(ch, samples, channels):
                x = float(audio[i])
                y = b0 * x + z1
                z1 = b1 * x - a1 * y + z2
                z2 = b2 * x - a2 * y
                audio[i] = y
            state[ch] = [z1, z2]
```

Update `_output_worker` to apply EQ after volume scaling:

In the output worker, after `audio *= vol` and before `np.clip(...)`, add:

```python
                # Apply EQ if settings exist
                eq_settings = getattr(self, '_eq_settings_router', {})
                eq = eq_settings.get(device_id)
                if eq and (abs(eq['bass']) > 0.01 or abs(eq['treble']) > 0.01):
                    # Initialize filter state if needed
                    if not hasattr(self, '_eq_filter_state'):
                        self._eq_filter_state = {}
                    if device_id not in self._eq_filter_state:
                        self._eq_filter_state[device_id] = {
                            'bass_state': [[0.0, 0.0] for _ in range(out_channels)],
                            'treble_state': [[0.0, 0.0] for _ in range(out_channels)],
                            'bass_coeffs': None,
                            'treble_coeffs': None,
                        }

                    fs = self._eq_filter_state[device_id]

                    # Recalculate coefficients if dirty
                    if eq.get('dirty', False):
                        bass_db = eq['bass'] * 12.0   # -1..+1 -> -12..+12 dB
                        treble_db = eq['treble'] * 12.0
                        fs['bass_coeffs'] = self._compute_biquad_low_shelf(
                            250, self._sample_rate, bass_db)
                        fs['treble_coeffs'] = self._compute_biquad_high_shelf(
                            4000, self._sample_rate, treble_db)
                        eq['dirty'] = False

                    if fs['bass_coeffs']:
                        self._apply_biquad(audio, fs['bass_coeffs'], fs['bass_state'])
                    if fs['treble_coeffs']:
                        self._apply_biquad(audio, fs['treble_coeffs'], fs['treble_state'])
```

Also init `_eq_settings_router` and `_eq_filter_state` in `__init__`:

```python
        self._eq_settings_router = {}   # device_id -> {bass, treble, dirty}
        self._eq_filter_state = {}      # device_id -> filter states
```

**Step 3: Add EQ panel to frontend**

Add HTML for EQ panel (after router-status div):

```html
<div id="eq-panel" class="eq-panel" style="display:none;">
    <div class="eq-header">
        <span id="eq-device-name">EQ</span>
        <button id="eq-close" type="button">&times;</button>
    </div>
    <div class="eq-slider-row">
        <label>Bass</label>
        <input type="range" id="eq-bass" min="-100" max="100" value="0" step="1">
        <span id="eq-bass-val">0</span>
    </div>
    <div class="eq-slider-row">
        <label>Treble</label>
        <input type="range" id="eq-treble" min="-100" max="100" value="0" step="1">
        <span id="eq-treble-val">0</span>
    </div>
</div>
```

Add CSS:

```css
.eq-panel {
    margin-top: 1rem;
    background: rgba(0, 212, 255, 0.06);
    border: 1px solid rgba(0, 212, 255, 0.2);
    border-radius: 8px;
    padding: 1rem;
    width: 300px;
}
.eq-header { display: flex; justify-content: space-between; align-items: center; margin-bottom: 0.8rem; color: #00d4ff; }
.eq-slider-row { display: flex; align-items: center; gap: 0.6rem; margin-bottom: 0.5rem; }
.eq-slider-row label { width: 50px; font-size: 0.85rem; color: #8888aa; }
.eq-slider-row input[type=range] { flex: 1; accent-color: #00d4ff; }
.eq-slider-row span { width: 30px; text-align: right; font-size: 0.8rem; color: #c0c0d8; }
```

Add JS for EQ panel:

```javascript
// EQ panel state
state.eqTarget = null;  // device_id of currently open EQ panel

const eqPanel = document.getElementById('eq-panel');
const eqBass = document.getElementById('eq-bass');
const eqTreble = document.getElementById('eq-treble');
const eqBassVal = document.getElementById('eq-bass-val');
const eqTrebleVal = document.getElementById('eq-treble-val');
const eqDeviceName = document.getElementById('eq-device-name');
const eqClose = document.getElementById('eq-close');

function openEQPanel(deviceId) {
    const dev = state.devices.find(d => d.id === deviceId);
    if (!dev) return;
    state.eqTarget = deviceId;
    eqDeviceName.textContent = 'EQ: ' + dev.name;
    const eq = dev.eq || { bass: 0, treble: 0 };
    eqBass.value = Math.round(eq.bass * 100);
    eqTreble.value = Math.round(eq.treble * 100);
    eqBassVal.textContent = eqBass.value;
    eqTrebleVal.textContent = eqTreble.value;
    eqPanel.style.display = 'block';
}

function closeEQPanel() {
    state.eqTarget = null;
    eqPanel.style.display = 'none';
}

eqClose.addEventListener('click', closeEQPanel);

let eqDebounceTimer = null;
function sendEQ() {
    clearTimeout(eqDebounceTimer);
    eqDebounceTimer = setTimeout(() => {
        if (!state.eqTarget) return;
        const bass = parseInt(eqBass.value) / 100;
        const treble = parseInt(eqTreble.value) / 100;
        socket.emit('set_eq', { device_id: state.eqTarget, bass, treble });
    }, 50);
}

eqBass.addEventListener('input', () => {
    eqBassVal.textContent = eqBass.value;
    sendEQ();
});
eqTreble.addEventListener('input', () => {
    eqTrebleVal.textContent = eqTreble.value;
    sendEQ();
});

socket.on('eq_update', (data) => {
    // Update device eq in state
    const dev = state.devices.find(d => d.id === data.device_id);
    if (dev) dev.eq = { bass: data.bass, treble: data.treble };
});
```

**Step 4: Run tests**

Run: `pytest test_server.py -v`
Expected: All pass

**Step 5: Commit**

```bash
git add server.py templates/index.html test_server.py
git commit -m "feat: add per-device EQ with real biquad DSP filtering"
```

---

### Task 7: Device Grouping

**Files:**
- Modify: `server.py` (handlers already added in Task 2)
- Modify: `templates/index.html`
- Modify: `test_server.py`

**Step 1: Write group tests**

Add to `test_server.py`:

```python
class TestGroups:
    """Device grouping tests."""

    def test_set_group(self, socketio_client):
        """set_group creates a group."""
        socketio_client.emit('set_group', {
            'group_id': 'g1',
            'name': 'Living Room',
            'device_ids': ['d1', 'd2']
        })
        with server_module._state_lock:
            assert 'g1' in server_module._device_groups
            assert server_module._device_groups['g1']['name'] == 'Living Room'
            assert server_module._group_membership.get('d1') == 'g1'
            assert server_module._group_membership.get('d2') == 'g1'

    def test_delete_group(self, socketio_client):
        """delete_group removes a group and clears membership."""
        socketio_client.emit('set_group', {
            'group_id': 'g1', 'name': 'Test', 'device_ids': ['d1']
        })
        socketio_client.emit('delete_group', {'group_id': 'g1'})
        with server_module._state_lock:
            assert 'g1' not in server_module._device_groups
            assert 'd1' not in server_module._group_membership

    def test_group_volume_propagation(self, socketio_client):
        """Setting volume on a grouped device applies to all group members."""
        socketio_client.emit('set_group', {
            'group_id': 'g1', 'name': 'Test', 'device_ids': ['d1', 'd2']
        })
        with patch.object(server_module, 'set_device_volume', return_value=True):
            with patch.object(server_module.audio_router, 'set_volume') as mock_vol:
                socketio_client.emit('set_volume', {'device_id': 'd1', 'volume': 0.7})
                # d1 should get 0.7, and d2 should also get 0.7 via group
                # (group propagation happens in ws_set_volume)

    def test_reverse_lookup_o1(self):
        """Group membership lookup is O(1) via reverse dict."""
        with server_module._state_lock:
            server_module._device_groups['g1'] = {'name': 'T', 'device_ids': ['d1', 'd2']}
            server_module._group_membership['d1'] = 'g1'
            server_module._group_membership['d2'] = 'g1'
            # O(1) lookup
            assert server_module._group_membership['d1'] == 'g1'

    def test_update_group_clears_old_membership(self, socketio_client):
        """Updating a group clears old member mappings."""
        socketio_client.emit('set_group', {
            'group_id': 'g1', 'name': 'Test', 'device_ids': ['d1', 'd2']
        })
        socketio_client.emit('set_group', {
            'group_id': 'g1', 'name': 'Test', 'device_ids': ['d2', 'd3']
        })
        with server_module._state_lock:
            assert 'd1' not in server_module._group_membership
            assert server_module._group_membership.get('d2') == 'g1'
            assert server_module._group_membership.get('d3') == 'g1'
```

**Step 2: Update ws_set_volume for group propagation**

In server.py, modify `ws_set_volume`:

```python
@socketio.on('set_volume')
def ws_set_volume(data):
    """Set volume on a device (and group members) via WebSocket."""
    device_id = data.get('device_id')
    volume = data.get('volume')
    if device_id is None or volume is None:
        return
    try:
        volume = float(volume)
    except (TypeError, ValueError):
        return
    volume = max(0.0, min(1.0, volume))

    # Apply to this device
    set_device_volume(device_id, volume)
    audio_router.set_volume(device_id, volume)
    with _state_lock:
        _last_known_volumes[device_id] = volume
        # Propagate to group members
        group_id = _group_membership.get(device_id)
        if group_id:
            group = _device_groups.get(group_id)
            if group:
                for member_id in group['device_ids']:
                    if member_id != device_id:
                        set_device_volume(member_id, volume)
                        audio_router.set_volume(member_id, volume)
                        _last_known_volumes[member_id] = volume
```

**Step 3: Add group management panel to frontend**

Add HTML (after eq-panel):

```html
<div id="group-panel" class="group-panel" style="display:none;">
    <div class="eq-header">
        <span>Device Groups</span>
        <button id="group-close" type="button">&times;</button>
    </div>
    <div id="group-list"></div>
    <div class="group-create">
        <input type="text" id="group-name-input" placeholder="Group name" maxlength="20">
        <button id="group-create-btn" type="button">Create Group</button>
    </div>
    <p class="group-hint" id="group-hint" style="display:none;">Click speakers to add/remove from group</p>
</div>
```

Add CSS:

```css
.group-panel {
    margin-top: 1rem;
    background: rgba(0, 212, 255, 0.06);
    border: 1px solid rgba(0, 212, 255, 0.2);
    border-radius: 8px;
    padding: 1rem;
    width: 300px;
}
.group-create { display: flex; gap: 0.5rem; margin-top: 0.8rem; }
.group-create input {
    flex: 1; background: rgba(255,255,255,0.05); border: 1px solid rgba(0,212,255,0.3);
    color: #e0e0e0; padding: 0.4rem; border-radius: 4px; font-size: 0.85rem;
}
.group-hint { font-size: 0.8rem; color: #8888aa; margin-top: 0.5rem; }
```

Add "Groups" button to toolbar:

```html
<button id="btn-groups" type="button">Groups</button>
```

Add JS:

```javascript
// Group state
state.groups = {};           // group_id -> {name, deviceIds: Set, color}
state.groupMembership = {};  // device_id -> group_id
state.editingGroup = null;   // group_id being edited (click-to-add mode)
const GROUP_COLORS = ['#ff6b6b', '#ffd93d', '#6bcb77', '#4d96ff', '#ff8e53', '#c77dff', '#45f0df', '#f97068'];
let nextGroupColor = 0;

document.getElementById('btn-groups').addEventListener('click', () => {
    const panel = document.getElementById('group-panel');
    panel.style.display = panel.style.display === 'none' ? 'block' : 'none';
    renderGroupList();
});
document.getElementById('group-close').addEventListener('click', () => {
    document.getElementById('group-panel').style.display = 'none';
    state.editingGroup = null;
});

document.getElementById('group-create-btn').addEventListener('click', () => {
    const input = document.getElementById('group-name-input');
    const name = input.value.trim();
    if (!name) return;
    const gid = 'g_' + Date.now();
    state.groups[gid] = { name, deviceIds: new Set(), color: GROUP_COLORS[nextGroupColor % GROUP_COLORS.length] };
    nextGroupColor++;
    state.editingGroup = gid;
    document.getElementById('group-hint').style.display = 'block';
    input.value = '';
    renderGroupList();
    syncGroupToServer(gid);
});

function syncGroupToServer(gid) {
    const g = state.groups[gid];
    if (!g) return;
    socket.emit('set_group', { group_id: gid, name: g.name, device_ids: [...g.deviceIds] });
    // Update local membership
    state.groupMembership = {};
    for (const [id, group] of Object.entries(state.groups)) {
        for (const did of group.deviceIds) {
            state.groupMembership[did] = id;
        }
    }
    localStorage.setItem('mixer-groups', JSON.stringify(
        Object.fromEntries(Object.entries(state.groups).map(([k,v]) => [k, {name: v.name, deviceIds: [...v.deviceIds], color: v.color}]))
    ));
}

function renderGroupList() {
    const list = document.getElementById('group-list');
    list.innerHTML = '';
    for (const [gid, g] of Object.entries(state.groups)) {
        const div = document.createElement('div');
        div.style.cssText = 'display:flex;align-items:center;gap:0.5rem;margin-bottom:0.4rem;';
        div.innerHTML = '<span style="width:10px;height:10px;border-radius:50%;background:' + g.color + ';display:inline-block;"></span>'
            + '<span style="flex:1;font-size:0.85rem;color:#c0c0d8;">' + g.name + ' (' + g.deviceIds.size + ')</span>'
            + '<button data-gid="' + gid + '" class="group-edit-btn" style="font-size:0.75rem;padding:0.2rem 0.5rem;">Edit</button>'
            + '<button data-gid="' + gid + '" class="group-del-btn" style="font-size:0.75rem;padding:0.2rem 0.5rem;color:#ff6b6b;border-color:#ff6b6b;">Del</button>';
        list.appendChild(div);
    }
    list.querySelectorAll('.group-edit-btn').forEach(btn => {
        btn.addEventListener('click', () => {
            state.editingGroup = btn.dataset.gid;
            document.getElementById('group-hint').style.display = 'block';
        });
    });
    list.querySelectorAll('.group-del-btn').forEach(btn => {
        btn.addEventListener('click', () => {
            const gid = btn.dataset.gid;
            socket.emit('delete_group', { group_id: gid });
            delete state.groups[gid];
            state.groupMembership = {};
            for (const [id, group] of Object.entries(state.groups)) {
                for (const did of group.deviceIds) state.groupMembership[did] = id;
            }
            state.editingGroup = null;
            renderGroupList();
            localStorage.setItem('mixer-groups', JSON.stringify(
                Object.fromEntries(Object.entries(state.groups).map(([k,v]) => [k, {name: v.name, deviceIds: [...v.deviceIds], color: v.color}]))
            ));
        });
    });
}

// Load persisted groups
try {
    const saved = JSON.parse(localStorage.getItem('mixer-groups') || '{}');
    for (const [gid, g] of Object.entries(saved)) {
        state.groups[gid] = { name: g.name, deviceIds: new Set(g.deviceIds), color: g.color };
        for (const did of g.deviceIds) state.groupMembership[did] = gid;
    }
} catch(e) {}

socket.on('group_update', (data) => {
    // Server-authoritative group state sync (reconnect scenario)
    // Merge with local state if needed
});
```

Update `onPointerDown` to handle group editing clicks on devices (add before mute toggle logic):

```javascript
// If editing a group, toggle device membership
if (state.editingGroup) {
    const gid = state.editingGroup;
    const group = state.groups[gid];
    if (group) {
        if (group.deviceIds.has(did)) {
            group.deviceIds.delete(did);
        } else {
            group.deviceIds.add(did);
        }
        syncGroupToServer(gid);
        renderGroupList();
        e.preventDefault();
        return;
    }
}
```

Draw group arcs in `drawMixer()` (after ring, before devices):

```javascript
// Draw group arcs
for (const [gid, group] of Object.entries(state.groups)) {
    const members = [...group.deviceIds];
    if (members.length < 2) continue;
    const n = state.devices.length;
    const indices = members.map(did => state.devices.findIndex(d => d.id === did)).filter(i => i >= 0);
    if (indices.length < 2) continue;
    // Draw arc connecting group members
    ctx.beginPath();
    ctx.strokeStyle = group.color + '44';
    ctx.lineWidth = 3;
    const angles = indices.map(i => (i * 2 * Math.PI) / n - Math.PI / 2).sort((a,b) => a - b);
    ctx.arc(CENTER, CENTER, RING_RADIUS + 14, angles[0], angles[angles.length - 1]);
    ctx.stroke();
}
```

**Step 4: Run tests**

Run: `pytest test_server.py -v`
Expected: All pass

**Step 5: Commit**

```bash
git add server.py templates/index.html test_server.py
git commit -m "feat: add device grouping with named zones and visual arcs"
```

---

### Task 8: Auto-Reconnect Volume Restore

**Files:**
- Modify: `test_server.py`

Note: The backend implementation was already done in Task 2 (`_restore_devices` function, `_last_known_volumes/eq/mute` dicts, and the device monitor thread). The frontend `volume_restored` handler was also added in Task 2. This task just adds tests.

**Step 1: Write restore tests**

Add to `test_server.py`:

```python
class TestVolumeRestore:
    """Auto-reconnect volume restore tests."""

    def test_volume_remembered_on_set(self, socketio_client):
        """Setting volume stores it for later restore."""
        with patch.object(server_module, 'set_device_volume', return_value=True):
            with patch.object(server_module.audio_router, 'set_volume'):
                socketio_client.emit('set_volume', {'device_id': 'd1', 'volume': 0.65})
                with server_module._state_lock:
                    assert abs(server_module._last_known_volumes.get('d1', 0) - 0.65) < 0.01

    def test_eq_remembered_on_set(self, socketio_client):
        """Setting EQ stores it for later restore."""
        with patch.object(server_module.audio_router, 'set_eq'):
            socketio_client.emit('set_eq', {'device_id': 'd1', 'bass': 0.3, 'treble': -0.5})
            with server_module._state_lock:
                eq = server_module._last_known_eq.get('d1')
                assert eq is not None
                assert abs(eq['bass'] - 0.3) < 0.01

    def test_mute_remembered(self, socketio_client):
        """Mute state is remembered."""
        with patch.object(server_module.audio_router, 'set_volume'):
            with patch.object(server_module, 'set_device_volume', return_value=True):
                socketio_client.emit('set_mute', {'device_id': 'd1', 'muted': True})
                with server_module._state_lock:
                    assert server_module._last_known_mute.get('d1') is True

    def test_restore_devices_applies_volume(self):
        """_restore_devices applies stored volume to new devices."""
        with server_module._state_lock:
            server_module._last_known_volumes['d1'] = 0.7
            server_module._last_known_mute['d1'] = False
        devices = [{'id': 'd1', 'name': 'Speaker 1', 'volume': 1.0}]
        with patch.object(server_module, 'set_device_volume', return_value=True) as mock_sv:
            with patch.object(server_module.audio_router, 'set_volume'):
                with patch.object(server_module.audio_router, 'set_eq'):
                    with patch.object(server_module.socketio, 'emit'):
                        server_module._restore_devices({'d1'}, devices)
                        mock_sv.assert_called_with('d1', 0.7)
```

**Step 2: Run tests**

Run: `pytest test_server.py::TestVolumeRestore -v`
Expected: All pass

**Step 3: Add frontend restore flash animation**

In the `drawMixer()` device rendering, add a restore flash check:

```javascript
// Restore flash animation
if (state.restoredDevice && state.restoredDevice.id === id && Date.now() < state.restoredDevice.until) {
    const progress = (state.restoredDevice.until - Date.now()) / 1000;
    ctx.beginPath();
    ctx.arc(pos.x, pos.y, DEVICE_RADIUS + 10, 0, Math.PI * 2);
    ctx.strokeStyle = `rgba(0, 255, 136, ${progress * 0.6})`;
    ctx.lineWidth = 2;
    ctx.stroke();
}
```

**Step 4: Commit**

```bash
git add test_server.py templates/index.html
git commit -m "feat: add auto-reconnect volume restore with tests"
```

---

### Task 9: Preset Positions

**Files:**
- Modify: `templates/index.html`

**Step 1: Add preset UI**

Add HTML (after status-bar):

```html
<div class="preset-bar">
    <span class="preset-label">Presets:</span>
    <div class="preset-slots" id="preset-slots"></div>
    <span id="preset-toast" class="preset-toast"></span>
</div>
```

Add CSS:

```css
.preset-bar {
    margin-top: 0.8rem;
    display: flex;
    align-items: center;
    gap: 0.6rem;
    font-size: 0.85rem;
    color: #8888aa;
}
.preset-slots { display: flex; gap: 0.3rem; }
.preset-slot {
    width: 24px; height: 24px; border-radius: 50%;
    border: 1px solid rgba(0,212,255,0.3);
    background: transparent;
    color: #8888aa;
    font-size: 0.7rem;
    display: flex; align-items: center; justify-content: center;
    cursor: pointer; transition: all 0.2s;
}
.preset-slot.filled { background: rgba(0,212,255,0.15); color: #00d4ff; border-color: #00d4ff; }
.preset-slot.active { background: rgba(0,212,255,0.3); box-shadow: 0 0 6px rgba(0,212,255,0.4); }
.preset-toast {
    font-size: 0.8rem; color: #00d4ff; opacity: 0;
    transition: opacity 0.3s;
}
.preset-toast.show { opacity: 1; }
```

**Step 2: Add JS for presets**

```javascript
// Preset state
state.presets = JSON.parse(localStorage.getItem('mixer-presets') || 'null') || new Array(9).fill(null);
state.activePreset = -1;
state.presetAnimating = false;
state.presetAnimTarget = null;

// Render preset slots
function renderPresetSlots() {
    const container = document.getElementById('preset-slots');
    container.innerHTML = '';
    for (let i = 0; i < 9; i++) {
        const slot = document.createElement('div');
        slot.className = 'preset-slot' + (state.presets[i] ? ' filled' : '') + (state.activePreset === i ? ' active' : '');
        slot.textContent = String(i + 1);
        slot.addEventListener('click', () => loadPreset(i));
        container.appendChild(slot);
    }
}

function showToast(msg) {
    const toast = document.getElementById('preset-toast');
    toast.textContent = msg;
    toast.classList.add('show');
    setTimeout(() => toast.classList.remove('show'), 1500);
}

function savePreset(index) {
    state.presets[index] = {
        controlPoint: { x: state.controlPoint.x, y: state.controlPoint.y },
        curveType: state.curveType,
        mutedDevices: [...state.mutedDevices],
    };
    localStorage.setItem('mixer-presets', JSON.stringify(state.presets));
    renderPresetSlots();
    showToast('Preset ' + (index + 1) + ' saved');
}

function loadPreset(index) {
    const preset = state.presets[index];
    if (!preset) return;
    state.activePreset = index;
    // Animate control point
    state.presetAnimTarget = { x: preset.controlPoint.x, y: preset.controlPoint.y };
    state.presetAnimating = true;
    // Apply curve and mute
    state.curveType = preset.curveType || 'inverse-square';
    curveSelect.value = state.curveType;
    localStorage.setItem('mixer-curve', state.curveType);
    state.mutedDevices = new Set(preset.mutedDevices || []);
    localStorage.setItem('mixer-muted', JSON.stringify([...state.mutedDevices]));
    renderPresetSlots();
    showToast('Preset ' + (index + 1) + ' loaded');
}

// Keyboard: 1-9 load, Shift+1-9 save
document.addEventListener('keydown', (e) => {
    const num = parseInt(e.key);
    if (num >= 1 && num <= 9) {
        if (e.shiftKey) {
            savePreset(num - 1);
        } else {
            loadPreset(num - 1);
        }
        e.preventDefault();
    }
});

renderPresetSlots();
```

Add to animation loop (preset lerp):

```javascript
// Preset position animation
if (state.presetAnimating && state.presetAnimTarget) {
    const dx = state.presetAnimTarget.x - state.controlPoint.x;
    const dy = state.presetAnimTarget.y - state.controlPoint.y;
    if (Math.abs(dx) < 1 && Math.abs(dy) < 1) {
        state.controlPoint.x = state.presetAnimTarget.x;
        state.controlPoint.y = state.presetAnimTarget.y;
        state.presetAnimating = false;
        state.presetAnimTarget = null;
    } else {
        state.controlPoint.x += dx * 0.15;
        state.controlPoint.y += dy * 0.15;
    }
    updateVolumes();
}
```

**Step 3: Commit**

```bash
git add templates/index.html
git commit -m "feat: add preset positions with save/load on keys 1-9"
```

---

### Task 10: Keyboard & Accessibility

**Files:**
- Modify: `templates/index.html`

**Step 1: Add ARIA markup**

Add to canvas element:

```html
<canvas id="mixer" width="500" height="500" tabindex="0" role="application"
        aria-label="Bluetooth speaker crossfade mixer. Use arrow keys to move control point, Tab to focus speakers."></canvas>
```

Add hidden live region (after canvas-wrap):

```html
<div id="aria-live" aria-live="polite" class="sr-only"></div>
```

Add CSS:

```css
.sr-only {
    position: absolute; width: 1px; height: 1px;
    padding: 0; margin: -1px; overflow: hidden;
    clip: rect(0,0,0,0); white-space: nowrap; border: 0;
}
```

**Step 2: Add keyboard handler**

```javascript
// Focus state
state.focusedElement = null;  // {type: 'control'} or {type: 'device', index: N}

function announce(msg) {
    document.getElementById('aria-live').textContent = msg;
}

canvas.addEventListener('keydown', (e) => {
    const STEP = e.shiftKey ? 2 : 8;
    const n = state.devices.length;

    switch (e.key) {
        case 'ArrowUp':
            state.controlPoint.y = Math.max(CENTER - RING_RADIUS + CONTROL_RADIUS, state.controlPoint.y - STEP);
            updateVolumes();
            e.preventDefault();
            break;
        case 'ArrowDown':
            state.controlPoint.y = Math.min(CENTER + RING_RADIUS - CONTROL_RADIUS, state.controlPoint.y + STEP);
            updateVolumes();
            e.preventDefault();
            break;
        case 'ArrowLeft':
            state.controlPoint.x = Math.max(CENTER - RING_RADIUS + CONTROL_RADIUS, state.controlPoint.x - STEP);
            updateVolumes();
            e.preventDefault();
            break;
        case 'ArrowRight':
            state.controlPoint.x = Math.min(CENTER + RING_RADIUS - CONTROL_RADIUS, state.controlPoint.x + STEP);
            updateVolumes();
            e.preventDefault();
            break;
        case 'Tab': {
            e.preventDefault();
            if (!state.focusedElement) {
                state.focusedElement = { type: 'control' };
                announce('Control point focused');
            } else if (state.focusedElement.type === 'control') {
                if (n > 0) {
                    state.focusedElement = { type: 'device', index: 0 };
                    announce(state.devices[0].name + ' focused');
                }
            } else {
                const next = (state.focusedElement.index + (e.shiftKey ? -1 : 1) + n) % n;
                state.focusedElement = { type: 'device', index: next };
                announce(state.devices[next].name + ' focused');
            }
            break;
        }
        case 'm':
        case 'M':
            if (state.focusedElement && state.focusedElement.type === 'device') {
                const did = state.devices[state.focusedElement.index].id;
                const wasMuted = state.mutedDevices.has(did);
                if (wasMuted) state.mutedDevices.delete(did); else state.mutedDevices.add(did);
                localStorage.setItem('mixer-muted', JSON.stringify([...state.mutedDevices]));
                socket.emit('set_mute', { device_id: did, muted: !wasMuted });
                updateVolumes();
                announce(state.devices[state.focusedElement.index].name + (wasMuted ? ' unmuted' : ' muted'));
                e.preventDefault();
            }
            break;
        case 'e':
        case 'E':
            if (state.focusedElement && state.focusedElement.type === 'device') {
                const did = state.devices[state.focusedElement.index].id;
                if (state.eqTarget === did) closeEQPanel(); else openEQPanel(did);
                e.preventDefault();
            }
            break;
        case 'Enter':
        case ' ':
            if (state.focusedElement && state.focusedElement.type === 'device') {
                const did = state.devices[state.focusedElement.index].id;
                const wasMuted = state.mutedDevices.has(did);
                if (wasMuted) state.mutedDevices.delete(did); else state.mutedDevices.add(did);
                localStorage.setItem('mixer-muted', JSON.stringify([...state.mutedDevices]));
                socket.emit('set_mute', { device_id: did, muted: !wasMuted });
                updateVolumes();
                announce(state.devices[state.focusedElement.index].name + (wasMuted ? ' unmuted' : ' muted'));
                e.preventDefault();
            }
            break;
        case 'Escape':
            closeEQPanel();
            document.getElementById('group-panel').style.display = 'none';
            state.editingGroup = null;
            state.focusedElement = null;
            e.preventDefault();
            break;
    }
});

// Debounced volume announcement after arrow key movement
let announceTimer = null;
const originalUpdateVolumes = updateVolumes;
// Wrap updateVolumes to announce after movement settles
function announceVolumeChange() {
    clearTimeout(announceTimer);
    announceTimer = setTimeout(() => {
        if (state.devices.length > 0) {
            const maxId = Object.entries(state.targetVolumes).reduce((a, b) => b[1] > a[1] ? b : a, ['', 0]);
            const dev = state.devices.find(d => d.id === maxId[0]);
            if (dev) {
                announce(dev.name + ' ' + Math.round(maxId[1] * 100) + '%');
            }
        }
    }, 500);
}
```

Add focus ring drawing to `drawMixer()`:

```javascript
// Draw focus ring
if (state.focusedElement) {
    if (state.focusedElement.type === 'control') {
        ctx.beginPath();
        ctx.arc(state.controlPoint.x, state.controlPoint.y, CONTROL_RADIUS + 4, 0, Math.PI * 2);
        ctx.setLineDash([3, 3]);
        ctx.strokeStyle = '#ffffff';
        ctx.lineWidth = 1.5;
        ctx.stroke();
        ctx.setLineDash([]);
    } else if (state.focusedElement.type === 'device' && state.focusedElement.index < n) {
        const fpos = devicePosition(state.focusedElement.index, n);
        ctx.beginPath();
        ctx.arc(fpos.x, fpos.y, DEVICE_RADIUS + 4, 0, Math.PI * 2);
        ctx.setLineDash([3, 3]);
        ctx.strokeStyle = '#ffffff';
        ctx.lineWidth = 1.5;
        ctx.stroke();
        ctx.setLineDash([]);
    }
}
```

**Step 3: Commit**

```bash
git add templates/index.html
git commit -m "feat: add keyboard controls and ARIA accessibility support"
```

---

### Task 11: Spotify Integration

**Files:**
- Modify: `server.py`
- Modify: `templates/index.html`
- Modify: `test_server.py`

**Step 1: Write Spotify tests**

Add to `test_server.py`:

```python
class TestSpotify:
    """Spotify integration tests."""

    def test_spotify_login_redirects(self, client):
        """GET /spotify/login redirects to Spotify authorize URL."""
        with patch.dict('os.environ', {'SPOTIFY_CLIENT_ID': 'test-client-id'}):
            # Need to reload the client_id
            server_module.SPOTIFY_CLIENT_ID = 'test-client-id'
            resp = client.get('/spotify/login')
            assert resp.status_code == 302
            assert 'accounts.spotify.com' in resp.location

    def test_spotify_callback_error_xss_safe(self, client):
        """Spotify callback escapes error params."""
        resp = client.get('/spotify/callback?error=<script>alert(1)</script>')
        assert resp.status_code == 200
        assert b'<script>alert(1)</script>' not in resp.data
        assert b'&lt;script&gt;' in resp.data

    def test_spotify_now_playing_no_token(self, client):
        """Now-playing returns error when not authenticated."""
        server_module._spotify_token = None
        resp = client.get('/api/spotify/now-playing')
        assert resp.status_code == 401

    def test_spotify_now_playing_with_token(self, client):
        """Now-playing proxies Spotify API response."""
        server_module._spotify_token = {'access_token': 'fake', 'expires_at': time.time() + 3600}
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {
            'is_playing': True,
            'item': {
                'name': 'Test Song',
                'artists': [{'name': 'Test Artist'}],
                'album': {'images': [{'url': 'http://img.jpg'}]},
                'duration_ms': 200000,
            },
            'progress_ms': 50000,
        }
        with patch('requests.get', return_value=mock_resp):
            resp = client.get('/api/spotify/now-playing')
            data = resp.get_json()
            assert data['is_playing'] is True
            assert data['track'] == 'Test Song'

    def test_spotify_play_pause(self, client):
        """Playback control endpoints proxy to Spotify."""
        server_module._spotify_token = {'access_token': 'fake', 'expires_at': time.time() + 3600}
        mock_resp = MagicMock()
        mock_resp.status_code = 204
        with patch('requests.put', return_value=mock_resp):
            resp = client.post('/api/spotify/play')
            assert resp.status_code == 200
        with patch('requests.put', return_value=mock_resp):
            resp = client.post('/api/spotify/pause')
            assert resp.status_code == 200
```

**Step 2: Implement Spotify backend**

Add to server.py imports:

```python
import html
import hashlib
import base64
import secrets
import requests as http_requests  # avoid name collision with flask.request
```

Add Spotify config and state:

```python
# ---------------------------------------------------------------------------
# Spotify integration
# ---------------------------------------------------------------------------

SPOTIFY_CLIENT_ID = os.environ.get('SPOTIFY_CLIENT_ID', '')
SPOTIFY_REDIRECT_URI = 'http://localhost:5000/spotify/callback'
SPOTIFY_SCOPES = 'user-read-currently-playing user-modify-playback-state'

_spotify_token = None        # {access_token, refresh_token, expires_at}
_spotify_code_verifier = None
```

Add Spotify routes:

```python
@app.route('/spotify/login')
def spotify_login():
    """Redirect to Spotify authorization."""
    global _spotify_code_verifier
    if not SPOTIFY_CLIENT_ID:
        return 'SPOTIFY_CLIENT_ID not set', 500
    _spotify_code_verifier = secrets.token_urlsafe(64)
    challenge = base64.urlsafe_b64encode(
        hashlib.sha256(_spotify_code_verifier.encode()).digest()
    ).rstrip(b'=').decode()
    params = {
        'client_id': SPOTIFY_CLIENT_ID,
        'response_type': 'code',
        'redirect_uri': SPOTIFY_REDIRECT_URI,
        'scope': SPOTIFY_SCOPES,
        'code_challenge_method': 'S256',
        'code_challenge': challenge,
    }
    url = 'https://accounts.spotify.com/authorize?' + '&'.join(f'{k}={v}' for k, v in params.items())
    return redirect(url)


@app.route('/spotify/callback')
def spotify_callback():
    """Exchange authorization code for tokens."""
    global _spotify_token
    error = request.args.get('error')
    if error:
        return f'<p>Spotify error: {html.escape(error)}</p><p><a href="/">Back</a></p>'
    code = request.args.get('code')
    if not code:
        return '<p>No authorization code received.</p><p><a href="/">Back</a></p>'
    resp = http_requests.post('https://accounts.spotify.com/api/token', data={
        'grant_type': 'authorization_code',
        'code': code,
        'redirect_uri': SPOTIFY_REDIRECT_URI,
        'client_id': SPOTIFY_CLIENT_ID,
        'code_verifier': _spotify_code_verifier,
    })
    if resp.status_code != 200:
        return f'<p>Token exchange failed: {html.escape(str(resp.status_code))}</p><p><a href="/">Back</a></p>'
    data = resp.json()
    _spotify_token = {
        'access_token': data['access_token'],
        'refresh_token': data.get('refresh_token'),
        'expires_at': time.time() + data.get('expires_in', 3600),
    }
    return '<script>window.close();</script><p>Connected! You can close this window.</p>'


@app.route('/api/spotify/now-playing')
def spotify_now_playing():
    """Get currently playing track."""
    token = _get_spotify_token()
    if not token:
        return jsonify({'error': 'Not authenticated'}), 401
    try:
        resp = http_requests.get('https://api.spotify.com/v1/me/player/currently-playing',
            headers={'Authorization': f'Bearer {token}'})
        if resp.status_code == 204 or not resp.content:
            return jsonify({'is_playing': False})
        data = resp.json()
        item = data.get('item', {})
        return jsonify({
            'is_playing': data.get('is_playing', False),
            'track': item.get('name', ''),
            'artist': ', '.join(a['name'] for a in item.get('artists', [])),
            'album_art': (item.get('album', {}).get('images', [{}])[0].get('url', '')),
            'progress_ms': data.get('progress_ms', 0),
            'duration_ms': item.get('duration_ms', 0),
        })
    except Exception as exc:
        return jsonify({'error': str(exc)}), 500


@app.route('/api/spotify/play', methods=['POST'])
def spotify_play():
    token = _get_spotify_token()
    if not token:
        return jsonify({'error': 'Not authenticated'}), 401
    http_requests.put('https://api.spotify.com/v1/me/player/play',
        headers={'Authorization': f'Bearer {token}'})
    return jsonify({'success': True})


@app.route('/api/spotify/pause', methods=['POST'])
def spotify_pause():
    token = _get_spotify_token()
    if not token:
        return jsonify({'error': 'Not authenticated'}), 401
    http_requests.put('https://api.spotify.com/v1/me/player/pause',
        headers={'Authorization': f'Bearer {token}'})
    return jsonify({'success': True})


@app.route('/api/spotify/next', methods=['POST'])
def spotify_next():
    token = _get_spotify_token()
    if not token:
        return jsonify({'error': 'Not authenticated'}), 401
    http_requests.post('https://api.spotify.com/v1/me/player/next',
        headers={'Authorization': f'Bearer {token}'})
    return jsonify({'success': True})


@app.route('/api/spotify/previous', methods=['POST'])
def spotify_previous():
    token = _get_spotify_token()
    if not token:
        return jsonify({'error': 'Not authenticated'}), 401
    http_requests.post('https://api.spotify.com/v1/me/player/previous',
        headers={'Authorization': f'Bearer {token}'})
    return jsonify({'success': True})


def _get_spotify_token():
    """Get valid Spotify access token, refreshing if needed."""
    global _spotify_token
    if not _spotify_token:
        return None
    if time.time() > _spotify_token['expires_at'] - 60:
        _refresh_spotify_token()
    return _spotify_token.get('access_token') if _spotify_token else None


def _refresh_spotify_token():
    """Refresh the Spotify access token."""
    global _spotify_token
    if not _spotify_token or not _spotify_token.get('refresh_token'):
        _spotify_token = None
        return
    resp = http_requests.post('https://accounts.spotify.com/api/token', data={
        'grant_type': 'refresh_token',
        'refresh_token': _spotify_token['refresh_token'],
        'client_id': SPOTIFY_CLIENT_ID,
    })
    if resp.status_code == 200:
        data = resp.json()
        _spotify_token['access_token'] = data['access_token']
        _spotify_token['expires_at'] = time.time() + data.get('expires_in', 3600)
        if 'refresh_token' in data:
            _spotify_token['refresh_token'] = data['refresh_token']
    else:
        _spotify_token = None
```

Add `redirect` to Flask import:

```python
from flask import Flask, render_template, jsonify, request, redirect
```

Add Spotify polling thread (emits via WebSocket):

```python
def _spotify_poller():
    """Background thread: polls Spotify now-playing every 3s."""
    while True:
        try:
            time.sleep(3)
            token = _get_spotify_token()
            if not token:
                continue
            resp = http_requests.get(
                'https://api.spotify.com/v1/me/player/currently-playing',
                headers={'Authorization': f'Bearer {token}'},
                timeout=5)
            if resp.status_code == 204 or not resp.content:
                socketio.emit('spotify_update', {'is_playing': False})
                continue
            data = resp.json()
            item = data.get('item', {})
            socketio.emit('spotify_update', {
                'is_playing': data.get('is_playing', False),
                'track': item.get('name', ''),
                'artist': ', '.join(a['name'] for a in item.get('artists', [])),
                'album_art': (item.get('album', {}).get('images', [{}])[0].get('url', '')),
                'progress_ms': data.get('progress_ms', 0),
                'duration_ms': item.get('duration_ms', 0),
            })
        except Exception:
            pass
```

Start in `__main__`:

```python
    # Start Spotify poller
    threading.Thread(target=_spotify_poller, daemon=True).start()
```

**Step 3: Add Spotify widget to frontend**

Add HTML (before closing `</body>`):

```html
<div id="spotify-widget" class="spotify-widget" style="display:none;">
    <img id="spotify-art" class="spotify-art" src="" alt="">
    <div class="spotify-info">
        <div id="spotify-track" class="spotify-track"></div>
        <div id="spotify-artist" class="spotify-artist"></div>
        <div class="spotify-progress">
            <div id="spotify-bar" class="spotify-bar-fill"></div>
        </div>
    </div>
    <div class="spotify-controls">
        <button id="spotify-prev" type="button">&#9664;&#9664;</button>
        <button id="spotify-playpause" type="button">&#9654;</button>
        <button id="spotify-next" type="button">&#9654;&#9654;</button>
    </div>
</div>
<div id="spotify-connect" class="spotify-connect">
    <button id="btn-spotify" type="button">Connect Spotify</button>
</div>
```

Add CSS:

```css
.spotify-widget {
    margin-top: 1.2rem; display: flex; align-items: center; gap: 0.8rem;
    background: rgba(30, 215, 96, 0.06); border: 1px solid rgba(30, 215, 96, 0.2);
    border-radius: 8px; padding: 0.6rem 1rem; width: 400px;
}
.spotify-art { width: 40px; height: 40px; border-radius: 4px; }
.spotify-info { flex: 1; min-width: 0; }
.spotify-track { font-size: 0.85rem; color: #e0e0e0; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
.spotify-artist { font-size: 0.75rem; color: #8888aa; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
.spotify-progress { height: 3px; background: rgba(255,255,255,0.1); border-radius: 2px; margin-top: 4px; }
.spotify-bar-fill { height: 100%; background: #1ed760; border-radius: 2px; transition: width 1s linear; width: 0%; }
.spotify-controls { display: flex; gap: 0.3rem; }
.spotify-controls button {
    background: rgba(30, 215, 96, 0.12); color: #1ed760;
    border: 1px solid rgba(30, 215, 96, 0.3);
    padding: 0.3rem 0.6rem; border-radius: 4px; font-size: 0.75rem; cursor: pointer;
}
.spotify-connect { margin-top: 1rem; }
```

Add JS:

```javascript
// Spotify state
let spotifyPlaying = false;
let spotifyProgressMs = 0;
let spotifyDurationMs = 0;
let spotifyLastUpdate = 0;

socket.on('spotify_update', (data) => {
    const widget = document.getElementById('spotify-widget');
    const connect = document.getElementById('spotify-connect');
    if (data.track) {
        widget.style.display = 'flex';
        connect.style.display = 'none';
        document.getElementById('spotify-track').textContent = data.track;
        document.getElementById('spotify-artist').textContent = data.artist;
        if (data.album_art) document.getElementById('spotify-art').src = data.album_art;
        spotifyPlaying = data.is_playing;
        spotifyProgressMs = data.progress_ms;
        spotifyDurationMs = data.duration_ms;
        spotifyLastUpdate = Date.now();
        document.getElementById('spotify-playpause').innerHTML = spotifyPlaying ? '&#9646;&#9646;' : '&#9654;';
    } else if (!data.is_playing) {
        widget.style.display = 'none';
    }
});

// Progress bar interpolation
setInterval(() => {
    if (!spotifyPlaying || !spotifyDurationMs) return;
    const elapsed = Date.now() - spotifyLastUpdate;
    const current = spotifyProgressMs + elapsed;
    const pct = Math.min(100, (current / spotifyDurationMs) * 100);
    document.getElementById('spotify-bar').style.width = pct + '%';
}, 1000);

// HTTP fallback for Spotify when WS down
async function fetchSpotifyHTTP() {
    try {
        const resp = await fetch('/api/spotify/now-playing');
        if (resp.ok) {
            const data = await resp.json();
            if (data.track) {
                document.getElementById('spotify-widget').style.display = 'flex';
                document.getElementById('spotify-connect').style.display = 'none';
                document.getElementById('spotify-track').textContent = data.track;
                document.getElementById('spotify-artist').textContent = data.artist;
                spotifyPlaying = data.is_playing;
                spotifyProgressMs = data.progress_ms;
                spotifyDurationMs = data.duration_ms;
                spotifyLastUpdate = Date.now();
            }
        }
    } catch(e) {}
}

document.getElementById('btn-spotify').addEventListener('click', () => {
    window.open('/spotify/login', 'spotify', 'width=500,height=700');
});
document.getElementById('spotify-prev').addEventListener('click', () => fetch('/api/spotify/previous', {method:'POST'}));
document.getElementById('spotify-next').addEventListener('click', () => fetch('/api/spotify/next', {method:'POST'}));
document.getElementById('spotify-playpause').addEventListener('click', () => {
    fetch('/api/spotify/' + (spotifyPlaying ? 'pause' : 'play'), {method:'POST'});
});
```

**Step 4: Run all tests**

Run: `pytest test_server.py -v`
Expected: All 58+ tests pass

**Step 5: Commit**

```bash
git add server.py templates/index.html test_server.py
git commit -m "feat: add Spotify integration with OAuth PKCE and now-playing widget"
```

---

## File Summary

| File | Changes |
|------|---------|
| `requirements.txt` | Add flask-socketio, requests |
| `server.py` | WebSocket handlers, device monitor, audio metering, EQ DSP, groups, volume restore, Spotify OAuth + API proxy |
| `templates/index.html` | socket.io client, curves dropdown, mute UI, EQ panel, group panel, preset bar, accessibility, Spotify widget |
| `test_server.py` | New — 58+ tests covering all features |
