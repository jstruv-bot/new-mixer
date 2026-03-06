# Bluetooth Crossfade Mixer

A real-time audio crossfade mixer for Bluetooth speakers on Windows. Drag a control point between your connected speakers to smoothly blend system audio across them — like a DJ crossfader, but for any audio playing on your PC.

![Python](https://img.shields.io/badge/Python-3.10+-blue)
![Flask](https://img.shields.io/badge/Flask-3.0+-green)
![Platform](https://img.shields.io/badge/Platform-Windows%2010%2F11-lightgrey)

## Quick Start

```bash
pip install -r requirements.txt
python server.py
```

Opens automatically at **http://127.0.0.1:5000**

## How It Works

The app captures your system audio via WASAPI loopback and re-routes it to multiple Bluetooth speakers simultaneously. A radial mixer UI lets you position a control point between speakers — the closer the point is to a speaker, the louder it plays. Move the point to crossfade smoothly between devices in real time.

## Features

### Radial Crossfade Mixer
- **Drag-to-mix** — Position the control point between speakers on a circular canvas to set their relative volumes
- **4 crossfade curves** — Inverse Square (default), Linear, Logarithmic, and Equal Power curves for different blending feels
- **Live volume display** — Each speaker shows its calculated volume percentage with animated arcs
- **Smooth transitions** — All volume changes interpolate at 60fps for seamless fading

### WASAPI Loopback Audio Routing
- **System audio capture** — Captures all PC audio via Windows WASAPI loopback (zero additional drivers needed)
- **Multi-device streaming** — Routes audio to all connected Bluetooth speakers simultaneously with independent volume control
- **Real-time DSP** — Per-device biquad filtering applied inline during streaming
- **Adaptive format** — Auto-detects sample rate and channel count from the system loopback device

### Per-Device Equalizer
- **Bass & Treble controls** — Double-click any speaker to open a dedicated EQ panel
- **Biquad DSP filters** — Low-shelf (250 Hz) and high-shelf (4 kHz) with +/-12 dB range
- **Stateful filtering** — Filter state maintained across audio chunks for artifact-free processing
- **Passthrough optimization** — Skips DSP math entirely when gain is below 0.01 dB

### Real-Time Audio Metering
- **Live peak meters** — Color-gradient rings on each speaker show real-time audio levels (~15 Hz update rate)
- **Hardware metering** — Reads actual audio peaks from Windows IAudioMeterInformation COM interface
- **Smooth decay** — Fast attack with 0.85 decay factor for natural-looking meters

### Device Muting
- **Click to mute** — Single-click any speaker circle to toggle mute
- **Visual feedback** — Muted devices show a red X overlay with dashed connection lines
- **Persistent** — Mute state saved to localStorage and restored on reload

### Device Groups
- **Named zones** — Create groups like "Living Room" or "Upstairs" to link speakers together
- **Synchronized volume** — Moving one grouped speaker's volume moves them all
- **Visual arcs** — Colored arcs on the ring connect grouped speakers
- **Click-to-assign** — While editing a group, click devices to add or remove them

### Preset System
- **9 slots** — Save and recall mixer positions with number keys 1-9
- **Full state capture** — Each preset stores control point position, curve type, and mute states
- **Save with Shift** — Shift+1 through Shift+9 saves the current mixer state to a slot
- **Animated recall** — Loading a preset smoothly animates the control point to its saved position

### Keyboard & Accessibility
- **Arrow keys** — Move the control point (8px per press, 2px with Shift for fine control)
- **Tab / Shift+Tab** — Cycle focus between the control point and speakers
- **M key** — Mute/unmute focused speaker
- **E key** — Open/close EQ panel for focused speaker
- **Escape** — Close all open panels
- **ARIA live region** — Screen reader announcements for focus changes and volume updates

### Spotify Integration
- **OAuth PKCE** — Secure authentication without exposing client secrets
- **Now Playing widget** — Shows current track name, artist, album art, and progress bar
- **Playback controls** — Play, pause, next, and previous track buttons
- **Auto-polling** — Updates every 3 seconds with client-side progress interpolation

### Auto-Recovery
- **Volume restore** — When a Bluetooth speaker reconnects, its last-known volume, EQ, and mute state are automatically restored
- **Device monitoring** — Background polling detects new/removed speakers every 3 seconds
- **WebSocket reconnection** — Falls back to HTTP polling if the WebSocket connection drops

## Architecture

```
Browser (Canvas UI + Socket.IO)
    │
    ├── WebSocket ──► Flask-SocketIO ──► Volume / Mute / EQ / Group state
    │                                         │
    │                                         ▼
    │                                   AudioRouter
    │                                    ┌──────────┐
    │                                    │ Capture   │ ◄── WASAPI Loopback
    │                                    │ Thread    │     (system audio)
    │                                    └────┬─────┘
    │                                         │ Queue per device
    │                                    ┌────┴─────┐
    │                                    │ Output   │ ──► BT Speaker 1
    │                                    │ Workers  │ ──► BT Speaker 2
    │                                    │          │ ──► BT Speaker N
    │                                    └──────────┘
    │                                    (volume + EQ DSP)
    │
    └── HTTP fallback ──► REST API (/api/devices, /api/volume, etc.)
```

### Threading Model

| Thread | Purpose | Rate |
|---|---|---|
| Main | Flask + SocketIO event loop | — |
| Device Monitor | Polls for BT speaker changes | 3s |
| Audio Level Monitor | Reads peak meters via COM | ~15 Hz |
| Spotify Poller | Fetches now-playing state | 3s |
| Capture Thread | WASAPI loopback read loop | Real-time |
| Output Workers (×N) | Per-speaker stream + DSP | Real-time |

## Requirements

- **Windows 10/11** (WASAPI + pycaw require Windows Core Audio)
- **Python 3.10+**
- **Bluetooth speakers** paired and connected via A2DP

### Dependencies

```
flask>=3.0
flask-socketio>=5.3
pycaw>=20240210
comtypes>=1.4
pyaudiowpatch>=0.2.12
numpy>=1.26
requests>=2.31
```

### Optional

- **Spotify Developer App** — Set `SPOTIFY_CLIENT_ID` environment variable for Spotify integration
- **PyInstaller** — `pip install pyinstaller>=6.0` for building a standalone `.exe`

## Building a Standalone Executable

```bash
pip install pyinstaller
python build.py
```

Output lands in `dist/BluetoothCrossfadeMixer/`.

## Testing

```bash
pip install pytest
pytest test_server.py -v
```

42 tests covering WebSocket events, REST endpoints, audio levels, crossfade curves, mute/EQ/groups, volume restore, and Spotify integration.

## License

MIT
