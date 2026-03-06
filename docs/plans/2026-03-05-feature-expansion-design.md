# Bluetooth Crossfade Mixer — Feature Expansion Design

## Overview

Add 10 features + unit tests to the existing Bluetooth Crossfade Mixer. Built on top of the
existing WASAPI AudioRouter (loopback capture to multi-speaker output). Layered incremental
build in dependency order.

## Decisions

- **Keep AudioRouter** — all new features layer on top of existing WASAPI loopback routing
- **Real DSP EQ** — biquad filters applied in AudioRouter output workers, not metadata-only
- **Spotify via PKCE** — user has credentials; no client secret needed
- **Memory-only volume restore** — no disk persistence; stable within a session

## Build Order

1. WebSocket transport (flask-socketio)
2. Audio level metering (IAudioMeterInformation)
3. Crossfade curves (linear/log/equal-power/inverse-square)
4. Per-device mute
5. Per-device EQ (real DSP biquad filters)
6. Device grouping (named zones)
7. Auto-reconnect volume restore
8. Preset positions (keys 1-9)
9. Keyboard & accessibility (arrow keys, Tab, ARIA)
10. Spotify integration (OAuth PKCE, now-playing, controls)
11. Unit tests (58+ tests, all pycaw/COM mocked)

---

## 1. WebSocket Transport Layer

**Current:** HTTP polling (devices 5s, router status 3s, volume via POST).
**Change:** flask-socketio for bidirectional real-time communication.

### Backend

- `flask-socketio` dependency, `socketio = SocketIO(app)`, replace `app.run()` with `socketio.run()`
- Background thread: monitors devices every 3s, emits `device_update` on change
- **Server -> Client events:** `device_update`, `router_status`, `volume_update`, `audio_levels`, `eq_update`, `spotify_update`
- **Client -> Server events:** `set_volume`, `set_eq`, `refresh_devices`, `set_mute`, `set_group`, `delete_group`
- Keep REST endpoints as fallback (Spotify callback needs HTTP)

### Frontend

- socket.io client JS (auto-served at `/socket.io/socket.io.js`)
- Replace polling intervals with socket event listeners
- HTTP polling fallback when WebSocket disconnects

---

## 2. Audio Level Metering

### Backend

- Background thread at ~15Hz
- `IAudioMeterInformation.GetPeakValue()` per device via pycaw
- Device cache: re-enumerate only on device list change
- Emits `audio_levels` via WebSocket: `{device_id: peak_value, ...}`
- Smoothing: fast attack, slow decay (0.85 multiplier per tick)

### Frontend

- Thin arc ring outside device circle, length proportional to level
- Color gradient: green -> yellow -> orange at high levels
- Lerp smoothing on display values

---

## 3. Crossfade Curves

Four selectable curves, dropdown in toolbar. Purely frontend.

- **Inverse-square:** `1 / (dist^2 + e)` (current, sharp falloff)
- **Linear:** `max(0, 1 - dist / maxDist)` (even gradient)
- **Logarithmic:** `1 / (1 + k * ln(1 + dist))` (gentle near, steep far)
- **Equal-power:** `cos^2(dist / maxDist * pi/2)` (constant perceived loudness)

`state.curveType` stored and persisted to localStorage.
`updateVolumes()` refactored to use `computeWeight(dist, curveType)`.

---

## 4. Per-Device Mute

### Frontend

- `state.mutedDevices` Set of device IDs
- Click on speaker node toggles mute (distinct from drag)
- Muted: red X overlay, dimmed, "MUTE" label, dashed connection lines
- `updateVolumes()` excludes muted devices, redistributes weights
- Persisted to localStorage

### Backend

- `muted_devices` dict protected by lock
- `set_mute` WebSocket handler -> sets AudioRouter volume to 0
- Mute state included in `device_update` events

---

## 5. Per-Device EQ (Real DSP)

### Backend — AudioRouter

- `_eq_settings` dict: `device_id -> {bass, treble}` (range -1.0 to +1.0)
- Two biquad filters per output: low-shelf 250Hz (bass), high-shelf 4kHz (treble)
- Gain: -1.0 = -12dB, +1.0 = +12dB
- Coefficients recalculated only on EQ change
- Filter state (z1, z2) per-channel for frame continuity
- Applied after volume scaling, before clipping
- `set_eq` WS handler, `eq_update` emitted back

### Frontend

- EQ panel per device (toggle via icon or "E" key)
- Bass/treble sliders, debounced 50ms, emit via WebSocket
- Panel re-renders only on device list change

---

## 6. Device Grouping

### Frontend

- "Groups" button opens management panel
- Create group: name + click speakers to toggle membership
- Visual: colored arcs between grouped speakers on canvas
- Volume behavior: max weight among group members applied to all
- Persisted to localStorage

### Backend

- `device_groups` dict: `group_id -> {name, device_ids}`
- Reverse lookup: `device_id -> group_id` (O(1))
- `set_group` / `delete_group` WS handlers
- `set_volumes_batch()`: single lock acquisition, group expansion

---

## 7. Auto-Reconnect Volume Restore

### Backend

- `_last_known_volumes`, `_last_known_eq`, `_last_known_mute` dicts
- Device monitor thread diffs new vs previous device list
- Newly appeared devices: restore stored volume/EQ/mute immediately
- Emit `volume_restored` event via WebSocket

### Frontend

- Brief pulse animation on restored device node (~1s)
- Update targetVolumes with restored values

Memory-only — no disk persistence needed for session-scoped usage.

---

## 8. Preset Positions

Purely frontend. 9 slots (keys 1-9).

- **Save:** Shift + number key -> stores `{controlPoint, curveType, mutedDevices, groups}`
- **Load:** number key -> recalls preset, animates control point (~300ms lerp)
- Visual: row of 9 numbered circles below status bar (filled = saved, hollow = empty)
- Toast notification on save/load (1.5s fade)
- Persisted to localStorage

---

## 9. Keyboard & Accessibility

Purely frontend.

| Key | Action |
|-----|--------|
| Arrows | Move control point (8px, Shift for 2px fine) |
| Tab/Shift+Tab | Cycle focus: control point -> speakers -> toolbar |
| Enter/Space | Toggle mute on focused speaker / activate button |
| M | Mute focused speaker |
| E | Toggle EQ panel |
| 1-9 / Shift+1-9 | Load/save presets |
| Escape | Close open panels |

- `state.focusedElement` tracks focus, visible focus ring on canvas
- `role="application"` on canvas, `aria-live="polite"` region
- Actions announced to screen readers via live region
- `.sr-only` CSS class for hidden but accessible content

---

## 10. Spotify Integration

### Backend

- OAuth PKCE: `/spotify/login`, `/spotify/callback` (XSS-safe with html.escape)
- Token refresh background thread
- Client ID via env var `SPOTIFY_CLIENT_ID`
- Proxy endpoints: `/api/spotify/now-playing`, `/play`, `/pause`, `/next`, `/previous`
- Background poll every 3s, emits `spotify_update` via WebSocket

### Frontend

- Compact widget: album art, track/artist, progress bar, play/pause/skip
- "Connect Spotify" button when unauthenticated
- Progress bar interpolated between 3s polls
- HTTP fallback when WebSocket disconnected

### Dependencies

- `requests` added to requirements.txt

---

## 11. Unit Tests

**File:** `test_server.py` — 58+ tests, all pycaw/COM/PyAudio mocked.

| Category | Count | Coverage |
|----------|-------|----------|
| Device enumeration | ~10 | BT filtering, render checks, properties |
| Volume control | ~5 | Clamping, error handling |
| Flask routes | ~10 | All REST endpoints, JSON, error codes |
| WebSocket events | ~10 | All emit/receive events |
| AudioRouter | ~8 | Lifecycle, device matching, volume |
| EQ/DSP | ~5 | Biquad coefficients, filter math |
| Groups | ~5 | Creation, volume propagation, reverse lookup |
| Spotify | ~5 | OAuth flow, now-playing, errors |

**Runner:** pytest (dev dependency)

---

## Dependencies (requirements.txt additions)

```
flask-socketio>=5.3
requests>=2.31
pytest>=8.0  # dev
```

## File Summary

| File | Changes |
|------|---------|
| `requirements.txt` | Add flask-socketio, requests, pytest |
| `server.py` | WebSocket, metering, EQ DSP, groups, volume restore, Spotify |
| `templates/index.html` | All frontend features, WS client, accessibility |
| `test_server.py` | New file, 58+ unit tests |
| `build.py` | No changes needed |
