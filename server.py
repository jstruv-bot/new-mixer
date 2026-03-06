"""
Bluetooth Speaker Crossfade Mixer — Backend
Flask API + pycaw device enumeration + WASAPI loopback audio routing.
"""

import sys
import os
import math
import warnings
import webbrowser
import threading
import time
import queue
import hashlib
import base64
import secrets
import urllib.parse
import collections
import json as _json

# Optional: MIDI controller support
try:
    import mido
    _MIDI_AVAILABLE = True
except ImportError:
    _MIDI_AVAILABLE = False

# Suppress noisy pycaw COMError warnings from non-Bluetooth devices
warnings.filterwarnings("ignore", message="COMError attempting to get property")

from flask import Flask, render_template, jsonify, request, redirect, make_response
from flask_socketio import SocketIO, emit
import html
import requests as http_requests  # avoid collision with flask.request

# pycaw / COM imports for Windows Core Audio
import comtypes
from pycaw.pycaw import AudioUtilities, IAudioMeterInformation

# Audio routing imports
import numpy as np
import pyaudiowpatch as pyaudio


def get_base_dir():
    """Get the base directory - works both in dev and when frozen by PyInstaller."""
    if getattr(sys, 'frozen', False):
        # Running as PyInstaller bundle
        return sys._MEIPASS
    return os.path.dirname(os.path.abspath(__file__))


BASE_DIR = get_base_dir()

# ---------------------------------------------------------------------------
# Flask app setup
# ---------------------------------------------------------------------------

app = Flask(__name__, template_folder=os.path.join(BASE_DIR, "templates"))
socketio = SocketIO(app, cors_allowed_origins="http://127.0.0.1:5000", async_mode="threading")

# ---------------------------------------------------------------------------
# Device enumeration helpers
# ---------------------------------------------------------------------------


def _init_com():
    """Initialize COM for the current thread. Safe to call multiple times."""
    try:
        comtypes.CoInitialize()
    except OSError:
        # COM already initialized on this thread — that is fine.
        pass


def _is_render_device(device):
    """Check if device is a render (playback) device by its endpoint ID prefix."""
    return device.id and device.id.startswith("{0.0.0.")


def _is_bluetooth_device(device):
    """Check if a pycaw AudioDevice is a Bluetooth A2DP (stereo) device.

    Uses PKEY_Device_EnumeratorName ({A45C254E-...}, 24) which is
    'BTHENUM' for A2DP Bluetooth and 'BTHHFENUM' for Hands-Free.
    We only include A2DP — Hands-Free is low-quality mono meant for
    calls and duplicates the same physical speaker.
    """
    if not hasattr(device, 'properties') or not device.properties:
        return False
    # Check the enumerator name property directly
    ENUMERATOR_KEY = "{A45C254E-DF1C-4EFD-8020-67D146A850E0} 24"
    enumerator = device.properties.get(ENUMERATOR_KEY, "")
    if isinstance(enumerator, str):
        upper = enumerator.upper()
        if upper == "BTHHFENUM":
            return False
        if upper == "BTHENUM":
            return True
    # Fallback: scan properties but exclude Hands-Free
    has_bt = False
    for value in device.properties.values():
        if isinstance(value, str):
            v = value.upper()
            if "BTHHFENUM" in v or "HANDS-FREE" in v:
                return False
            if "BTHENUM" in v or "BTH" in v:
                has_bt = True
    return has_bt


def get_bluetooth_speakers():
    """
    Enumerate active Bluetooth audio render (playback) devices and return
    a list of dicts with id, name, and current volume.
    """
    _init_com()
    devices_info = []

    try:
        all_devices = AudioUtilities.GetAllDevices()
    except Exception as exc:
        print(f"[enumerate] Failed to list devices: {exc}")
        return devices_info

    for device in all_devices:
        try:
            # Only include render (output) devices
            if not _is_render_device(device):
                continue

            # Only include Bluetooth devices
            if not _is_bluetooth_device(device):
                continue

            # Attempt to read the friendly name; skip unnamed devices.
            friendly_name = device.FriendlyName
            if not friendly_name:
                continue

            # Obtain a unique device id string.
            device_id = device.id
            if not device_id:
                continue

            # Try to get the IAudioEndpointVolume interface to read volume.
            volume_level = None
            try:
                endpoint_volume = device.EndpointVolume
                if endpoint_volume is not None:
                    volume_level = endpoint_volume.GetMasterVolumeLevelScalar()
            except Exception:
                pass

            devices_info.append({
                "id": device_id,
                "name": friendly_name,
                "volume": round(volume_level, 4) if volume_level is not None else None,
            })

        except Exception as exc:
            print(f"[enumerate] Skipping device: {exc}")
            continue

    return devices_info


_endpoint_volume_cache = {}  # device_id -> IAudioEndpointVolume COM interface
_endpoint_cache_lock = threading.Lock()


def _get_endpoint_volume(device_id):
    """Get (and cache) the IAudioEndpointVolume COM interface for a device."""
    with _endpoint_cache_lock:
        ev = _endpoint_volume_cache.get(device_id)
        if ev is not None:
            return ev

    # Cache miss — enumerate once to find the interface
    _init_com()
    try:
        for device in AudioUtilities.GetAllDevices():
            try:
                if device.id == device_id:
                    ev = device.EndpointVolume
                    if ev is not None:
                        with _endpoint_cache_lock:
                            _endpoint_volume_cache[device_id] = ev
                    return ev
            except Exception:
                return None
    except Exception:
        return None
    return None


def invalidate_endpoint_cache():
    """Clear cached COM interfaces (call when devices change)."""
    with _endpoint_cache_lock:
        _endpoint_volume_cache.clear()


def set_device_volume(device_id, volume):
    """
    Set the master volume on the device identified by *device_id*.
    Returns True on success, False on failure.
    """
    volume = max(0.0, min(1.0, float(volume)))
    ev = _get_endpoint_volume(device_id)
    if ev is None:
        return False
    try:
        ev.SetMasterVolumeLevelScalar(volume, None)
        return True
    except comtypes.COMError:
        # Stale interface — invalidate and retry once
        invalidate_endpoint_cache()
        ev = _get_endpoint_volume(device_id)
        if ev is None:
            return False
        try:
            ev.SetMasterVolumeLevelScalar(volume, None)
            return True
        except Exception as exc:
            print(f"[set_volume] Error on retry for {device_id}: {exc}")
            return False
    except Exception as exc:
        print(f"[set_volume] Error setting volume on {device_id}: {exc}")
        return False


# ---------------------------------------------------------------------------
# Audio Router — WASAPI loopback capture → multi-device output
# ---------------------------------------------------------------------------


class AudioRouter:
    """Captures system audio via WASAPI loopback and streams to BT speakers."""

    CHUNK = 1024        # frames per buffer (~21ms at 48000Hz)
    FORMAT = pyaudio.paFloat32
    NUMPY_DTYPE = np.float32

    def __init__(self):
        self._pa = None
        self._running = False
        self._capture_thread = None
        self._output_threads = {}       # pycaw_device_id -> thread
        self._output_streams = {}       # pycaw_device_id -> pyaudio stream
        self._volumes = {}              # pycaw_device_id -> float 0.0-1.0
        self._lock = threading.Lock()
        self._start_lock = threading.Lock()  # prevent concurrent start/stop
        self._audio_queues = {}         # pycaw_device_id -> queue.Queue
        self._sample_rate = 48000
        self._channels = 2
        self._loopback_info = None
        self._device_index_map = {}     # pycaw_device_id -> pyaudio device index
        self._eq_settings_router = {}   # device_id -> {bass, treble, dirty}
        self._eq_filter_state = {}      # device_id -> {bass_state, treble_state, bass_coeffs, treble_coeffs}
        self._delay_ms = {}             # device_id -> delay in ms (0-500)
        self._delay_buffers = {}        # device_id -> deque of audio chunk bytes
        self._effects = {}              # device_id -> {type, rate_hz, depth, phase}
        self._latency = {}              # device_id -> latest write latency in ms

    def start(self, bt_devices):
        """Start audio routing to the given Bluetooth devices.

        Parameters
        ----------
        bt_devices : list[dict]
            From get_bluetooth_speakers(), each with 'id' and 'name'.
        """
        if not self._start_lock.acquire(blocking=False):
            return False  # Another start/stop in progress
        try:
            return self._start_impl(bt_devices)
        finally:
            self._start_lock.release()

    def _start_impl(self, bt_devices):
        if self._running:
            self._stop_impl()

        try:
            self._pa = pyaudio.PyAudio()
        except Exception as exc:
            print(f"[AudioRouter] Failed to initialize PyAudio: {exc}")
            return False

        # Find a working WASAPI loopback device
        self._loopback_info = self._find_loopback_device()
        if not self._loopback_info:
            print("[AudioRouter] No working WASAPI loopback device found")
            self._cleanup_pa()
            return False

        self._sample_rate = int(self._loopback_info["defaultSampleRate"])
        self._channels = self._loopback_info["maxInputChannels"]

        # Match pycaw BT devices to PyAudio output device indices
        self._device_index_map = self._match_devices(bt_devices)

        if not self._device_index_map:
            print("[AudioRouter] No BT devices matched in PyAudio device list")
            self._cleanup_pa()
            return False

        # Initialize queues and preserve existing volumes
        for dev_id in self._device_index_map:
            self._volumes.setdefault(dev_id, 1.0)
            self._audio_queues[dev_id] = queue.Queue(maxsize=50)

        self._running = True

        # Start output threads first (they block waiting on queues)
        for dev_id, pa_index in self._device_index_map.items():
            t = threading.Thread(
                target=self._output_worker,
                args=(dev_id, pa_index),
                daemon=True,
            )
            self._output_threads[dev_id] = t
            t.start()

        # Start capture thread
        self._capture_thread = threading.Thread(
            target=self._capture_worker,
            daemon=True,
        )
        self._capture_thread.start()

        matched_names = [f"  - {d['name']}" for d in bt_devices if d['id'] in self._device_index_map]
        print(f"[AudioRouter] Started: capturing '{self._loopback_info['name']}' "
              f"({self._sample_rate}Hz {self._channels}ch)")
        for name in matched_names:
            print(f"[AudioRouter]   -> {name}")
        return True

    def stop(self):
        """Stop all audio threads and clean up."""
        with self._start_lock:
            self._stop_impl()

    def _stop_impl(self):
        self._running = False

        # Unblock output threads
        for q in self._audio_queues.values():
            try:
                q.put(None, block=False)
            except queue.Full:
                pass

        if self._capture_thread and self._capture_thread.is_alive():
            self._capture_thread.join(timeout=2)

        for t in self._output_threads.values():
            if t.is_alive():
                t.join(timeout=2)

        for stream in self._output_streams.values():
            try:
                stream.stop_stream()
                stream.close()
            except Exception:
                pass

        self._output_streams.clear()
        self._output_threads.clear()
        self._audio_queues.clear()
        self._device_index_map.clear()
        self._cleanup_pa()
        print("[AudioRouter] Stopped")

    def set_volume(self, device_id, volume):
        """Update the volume multiplier for a device's output stream."""
        with self._lock:
            self._volumes[device_id] = max(0.0, min(1.0, float(volume)))

    def update_devices(self, bt_devices):
        """Re-sync with current BT device list. Restart if devices changed."""
        if not self._pa or not self._running:
            self.start(bt_devices)
            return

        # Don't rebuild the device map if we can't acquire the lock
        if not self._start_lock.acquire(blocking=False):
            return
        try:
            old_ids = set(self._device_index_map.keys())
            new_map = self._match_devices(bt_devices)
            new_ids = set(new_map.keys())
            needs_restart = old_ids != new_ids

            if needs_restart:
                print("[AudioRouter] Device change detected, restarting...")
                self._stop_impl()
        finally:
            self._start_lock.release()

        if needs_restart:
            self.start(bt_devices)

    def set_eq(self, device_id, bass, treble):
        """Update EQ settings for a device."""
        with self._lock:
            bass = max(-1.0, min(1.0, float(bass)))
            treble = max(-1.0, min(1.0, float(treble)))
            self._eq_settings_router[device_id] = {
                'bass': bass,
                'treble': treble,
                'dirty': True,
            }

    def set_delay(self, device_id, delay_ms):
        """Set delay compensation for a device (0-500ms)."""
        with self._lock:
            clamped = max(0.0, min(500.0, float(delay_ms)))
            self._delay_ms[device_id] = clamped
            if clamped <= 0:
                self._delay_buffers.pop(device_id, None)

    def set_effect(self, device_id, effect_type, rate_hz=2.0, depth=0.5):
        """Set BPM-synced effect for a device."""
        with self._lock:
            if not effect_type or effect_type == 'off':
                self._effects.pop(device_id, None)
            else:
                self._effects[device_id] = {
                    'type': effect_type,
                    'rate_hz': max(0.1, float(rate_hz)),
                    'depth': max(0.0, min(1.0, float(depth))),
                    'phase': 0.0,
                }

    def get_latency(self):
        """Return latest per-device write latency."""
        with self._lock:
            return dict(self._latency)

    @property
    def is_running(self):
        return self._running

    @property
    def active_outputs(self):
        return len(self._device_index_map)

    @staticmethod
    def _compute_biquad_low_shelf(freq, sample_rate, gain_db):
        """Compute biquad low-shelf filter coefficients."""
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
        a1_coeff = -2 * ((A - 1) + (A + 1) * cos_w0)
        a2_coeff = (A + 1) + (A - 1) * cos_w0 - 2 * sqrt_A * alpha
        return (b0/a0, b1/a0, b2/a0, a1_coeff/a0, a2_coeff/a0)

    @staticmethod
    def _compute_biquad_high_shelf(freq, sample_rate, gain_db):
        """Compute biquad high-shelf filter coefficients."""
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
        a1_coeff = 2 * ((A - 1) - (A + 1) * cos_w0)
        a2_coeff = (A + 1) - (A - 1) * cos_w0 - 2 * sqrt_A * alpha
        return (b0/a0, b1/a0, b2/a0, a1_coeff/a0, a2_coeff/a0)

    @staticmethod
    def _apply_biquad(audio, coeffs, state):
        """Apply biquad filter to interleaved audio buffer in-place.

        De-interleaves channels and processes with plain Python lists to avoid
        numpy per-element overhead. ~3-5x faster than strided numpy indexing.
        coeffs: (b0, b1, b2, a1, a2)
        state: list of [z1, z2] per channel
        """
        b0, b1, b2, a1, a2 = coeffs
        if abs(b0 - 1.0) < 0.001 and abs(b1) < 0.001:
            return  # passthrough
        channels = len(state)
        for ch in range(channels):
            # Extract channel as contiguous Python list (much faster indexing)
            x = audio[ch::channels].tolist()
            z1, z2 = state[ch]
            for i in range(len(x)):
                xi = x[i]
                yi = b0 * xi + z1
                z1 = b1 * xi - a1 * yi + z2
                z2 = b2 * xi - a2 * yi
                x[i] = yi
            state[ch] = [z1, z2]
            audio[ch::channels] = x

    def _find_loopback_device(self):
        """Find the first valid WASAPI loopback device."""
        if not self._pa:
            return None

        for i in range(self._pa.get_device_count()):
            try:
                info = self._pa.get_device_info_by_index(i)
                if (info.get("isLoopbackDevice", False)
                        and info.get("maxInputChannels", 0) > 0
                        and info.get("defaultSampleRate", 0) > 0
                        and info.get("name", "")):
                    return info
            except Exception:
                continue
        return None

    def _match_devices(self, bt_devices):
        """Match pycaw BT devices to PyAudio output device indices.

        Tries all host APIs — prefers WASAPI (sees all endpoints), then
        DirectSound, then MME as fallback. Each BT device is matched at
        most once; first match wins.
        """
        result = {}
        if not self._pa:
            return result

        # Build host-API index map (prefer WASAPI > DirectSound > MME)
        api_indices = {}
        for i in range(self._pa.get_host_api_count()):
            info = self._pa.get_host_api_info_by_index(i)
            name = info.get("name", "")
            api_indices[name] = i

        preferred_order = [
            "MME",
            "Windows DirectSound",
            "Windows WASAPI",
        ]
        ordered_apis = [api_indices[n] for n in preferred_order if n in api_indices]
        # Add any remaining host APIs not in our preference list
        for idx in api_indices.values():
            if idx not in ordered_apis:
                ordered_apis.append(idx)

        # Collect all output devices grouped by host API
        outputs_by_api = {idx: [] for idx in ordered_apis}
        for i in range(self._pa.get_device_count()):
            try:
                info = self._pa.get_device_info_by_index(i)
                api = info.get("hostApi")
                if (api in outputs_by_api
                        and info.get("maxOutputChannels", 0) > 0
                        and info.get("name", "")
                        and not info.get("isLoopbackDevice", False)):
                    outputs_by_api[api].append(info)
            except Exception:
                continue

        print(f"[AudioRouter] Host APIs: {api_indices}")
        for api_idx in ordered_apis:
            api_name = next((n for n, i in api_indices.items() if i == api_idx), str(api_idx))
            devs = outputs_by_api.get(api_idx, [])
            if devs:
                print(f"[AudioRouter] {api_name} outputs: {[d['name'] for d in devs]}")

        matched_bt = set()
        for api_idx in ordered_apis:
            devs = outputs_by_api.get(api_idx, [])
            for bt_dev in bt_devices:
                bt_id = bt_dev["id"]
                if bt_id in matched_bt:
                    continue
                bt_name = bt_dev["name"].lower()

                for pa_dev in devs:
                    pa_name = pa_dev["name"]

                    # Match 1: endpoint ID prefix (MME truncates to ~31 chars)
                    if pa_name.startswith("{") and bt_id.lower().startswith(pa_name.lower()):
                        result[bt_id] = pa_dev["index"]
                        matched_bt.add(bt_id)
                        print(f"[AudioRouter] Matched '{bt_dev['name']}' -> "
                              f"index {pa_dev['index']} (ID prefix)")
                        break

                    # Match 2: friendly name substring
                    pa_lower = pa_name.lower()
                    if bt_name in pa_lower or pa_lower in bt_name:
                        result[bt_id] = pa_dev["index"]
                        matched_bt.add(bt_id)
                        print(f"[AudioRouter] Matched '{bt_dev['name']}' -> "
                              f"'{pa_name}' index {pa_dev['index']} (name)")
                        break

        for bt_dev in bt_devices:
            if bt_dev["id"] not in result:
                print(f"[AudioRouter] UNMATCHED: '{bt_dev['name']}' "
                      f"(id={bt_dev['id'][:40]}...)")

        return result

    def _capture_worker(self):
        """Thread: capture loopback audio and distribute to output queues."""
        try:
            stream = self._pa.open(
                format=self.FORMAT,
                channels=self._channels,
                rate=self._sample_rate,
                input=True,
                input_device_index=self._loopback_info["index"],
                frames_per_buffer=self.CHUNK,
            )
        except Exception as exc:
            print(f"[AudioRouter] Failed to open loopback stream: {exc}")
            self._running = False
            return

        print("[AudioRouter] Capture thread running")

        # Snapshot queue references — avoids list() allocation per chunk
        queues = list(self._audio_queues.values())

        try:
            while self._running:
                try:
                    data = stream.read(self.CHUNK, exception_on_overflow=False)
                except Exception as exc:
                    if self._running:
                        print(f"[AudioRouter] Capture read error: {exc}")
                    time.sleep(0.01)
                    continue

                # Distribute to all output queues
                for q in queues:
                    try:
                        q.put_nowait(data)
                    except queue.Full:
                        # Drop oldest to prevent lag buildup
                        try:
                            q.get_nowait()
                        except queue.Empty:
                            pass
                        try:
                            q.put_nowait(data)
                        except queue.Full:
                            pass
        finally:
            try:
                stream.stop_stream()
                stream.close()
            except Exception:
                pass
            print("[AudioRouter] Capture thread stopped")

    def _output_worker(self, device_id, pa_device_index):
        """Thread: read from queue, apply volume, write to output device."""
        try:
            pa_info = self._pa.get_device_info_by_index(pa_device_index)
            out_channels = min(self._channels, pa_info.get("maxOutputChannels", 2))
            out_rate = self._sample_rate
            needs_resample = False

            try:
                stream = self._pa.open(
                    format=self.FORMAT,
                    channels=out_channels,
                    rate=out_rate,
                    output=True,
                    output_device_index=pa_device_index,
                    frames_per_buffer=self.CHUNK,
                )
            except Exception:
                # Device may not support capture sample rate — try native rate
                native_rate = int(pa_info.get("defaultSampleRate", 44100))
                if native_rate != out_rate:
                    print(f"[AudioRouter] Retrying output {pa_device_index} "
                          f"at native {native_rate}Hz (capture is {out_rate}Hz)")
                    out_rate = native_rate
                    needs_resample = True
                    stream = self._pa.open(
                        format=self.FORMAT,
                        channels=out_channels,
                        rate=out_rate,
                        output=True,
                        output_device_index=pa_device_index,
                        frames_per_buffer=self.CHUNK,
                    )
                else:
                    raise

            self._output_streams[device_id] = stream
        except Exception as exc:
            print(f"[AudioRouter] Failed to open output {pa_device_index}: {exc}")
            return

        dev_name = pa_info.get("name", str(pa_device_index))
        print(f"[AudioRouter] Output thread running for '{dev_name}'"
              f"{' (resampling)' if needs_resample else ''}")

        q = self._audio_queues.get(device_id)
        if not q:
            return

        try:
            while self._running:
                try:
                    data = q.get(timeout=0.5)
                except queue.Empty:
                    continue

                if data is None:
                    break

                with self._lock:
                    vol = self._volumes.get(device_id, 1.0)
                    eq = self._eq_settings_router.get(device_id)
                    eq_snap = None
                    if eq:
                        eq_snap = dict(eq)
                        # Clear dirty flag on the original (not the snapshot)
                        eq['dirty'] = False

                if vol < 0.001:
                    # Muted — write silence to keep stream alive
                    try:
                        stream.write(b'\x00' * len(data))
                    except Exception:
                        pass
                    continue

                # Convert to numpy, apply volume, clip
                audio = np.frombuffer(data, dtype=self.NUMPY_DTYPE).copy()

                # Handle channel mismatch
                if self._channels != out_channels and out_channels > 0:
                    audio = audio.reshape(-1, self._channels)[:, :out_channels].flatten()

                audio *= vol

                # Apply EQ if settings exist
                eq = eq_snap
                if eq and (abs(eq['bass']) > 0.01 or abs(eq['treble']) > 0.01):
                    if device_id not in self._eq_filter_state:
                        self._eq_filter_state[device_id] = {
                            'bass_state': [[0.0, 0.0] for _ in range(out_channels)],
                            'treble_state': [[0.0, 0.0] for _ in range(out_channels)],
                            'bass_coeffs': None,
                            'treble_coeffs': None,
                        }
                    fs = self._eq_filter_state[device_id]
                    if eq.get('dirty', False):
                        bass_db = eq['bass'] * 12.0
                        treble_db = eq['treble'] * 12.0
                        fs['bass_coeffs'] = self._compute_biquad_low_shelf(250, self._sample_rate, bass_db)
                        fs['treble_coeffs'] = self._compute_biquad_high_shelf(4000, self._sample_rate, treble_db)
                    if fs['bass_coeffs']:
                        self._apply_biquad(audio, fs['bass_coeffs'], fs['bass_state'])
                    if fs['treble_coeffs']:
                        self._apply_biquad(audio, fs['treble_coeffs'], fs['treble_state'])

                # Delay compensation
                with self._lock:
                    delay_ms = self._delay_ms.get(device_id, 0)
                if delay_ms > 0:
                    chunks_needed = max(1, round(
                        delay_ms * self._sample_rate / (1000 * self.CHUNK)))
                    if device_id not in self._delay_buffers:
                        self._delay_buffers[device_id] = collections.deque()
                    buf = self._delay_buffers[device_id]
                    buf.append(audio.tobytes())
                    if len(buf) > chunks_needed:
                        audio = np.frombuffer(
                            buf.popleft(), dtype=self.NUMPY_DTYPE).copy()
                    else:
                        audio = np.zeros_like(audio)

                # BPM-synced effects
                with self._lock:
                    fx = self._effects.get(device_id)
                    fx_snap = dict(fx) if fx else None
                if fx_snap:
                    frames = len(audio) // out_channels
                    if frames > 0:
                        rate = fx_snap['rate_hz']
                        depth = fx_snap['depth']
                        phase = fx_snap['phase']
                        t = (np.arange(frames, dtype=np.float32)
                             / self._sample_rate + phase)
                        if fx_snap['type'] == 'tremolo':
                            mod = (1.0 - depth * 0.5
                                   * (1 + np.sin(2 * np.pi * rate * t)))
                            for ch in range(out_channels):
                                audio[ch::out_channels] *= mod
                        elif (fx_snap['type'] == 'autopan'
                              and out_channels >= 2):
                            pan = 0.5 * (1 + np.sin(
                                2 * np.pi * rate * t))
                            audio[0::out_channels] *= (
                                1 - depth * pan).astype(self.NUMPY_DTYPE)
                            audio[1::out_channels] *= (
                                1 - depth * (1 - pan)).astype(
                                    self.NUMPY_DTYPE)
                        elif fx_snap['type'] == 'filter_sweep':
                            sweep = 0.5 + 0.5 * np.sin(
                                2 * np.pi * rate * 0.25 * t)
                            mod = (1 - depth * 0.5 * (1 - sweep)).astype(
                                self.NUMPY_DTYPE)
                            for ch in range(out_channels):
                                audio[ch::out_channels] *= mod
                        new_phase = float(t[-1]) % max(
                            10.0, 1.0 / max(rate, 0.01))
                        with self._lock:
                            if device_id in self._effects:
                                self._effects[device_id]['phase'] = new_phase

                np.clip(audio, -1.0, 1.0, out=audio)

                # Resample if output device runs at a different rate
                if needs_resample:
                    frames_in = len(audio) // out_channels
                    frames_out = int(frames_in * out_rate / self._sample_rate)
                    if frames_out > 0 and frames_in > 0:
                        resampled = np.zeros(frames_out * out_channels,
                                             dtype=self.NUMPY_DTYPE)
                        for ch in range(out_channels):
                            src = audio[ch::out_channels]
                            idx = np.linspace(0, len(src) - 1, frames_out)
                            resampled[ch::out_channels] = np.interp(
                                idx, np.arange(len(src)), src)
                        audio = resampled

                _t0 = time.perf_counter()
                try:
                    stream.write(audio.tobytes())
                except Exception as exc:
                    if self._running:
                        print(f"[AudioRouter] Output write error: {exc}")
                    time.sleep(0.01)
                    continue
                _t1 = time.perf_counter()
                with self._lock:
                    self._latency[device_id] = round((_t1 - _t0) * 1000, 1)
        finally:
            try:
                stream.stop_stream()
                stream.close()
            except Exception:
                pass
            print(f"[AudioRouter] Output thread stopped for '{dev_name}'")

    def _cleanup_pa(self):
        """Terminate the PyAudio instance."""
        if self._pa:
            try:
                self._pa.terminate()
            except Exception:
                pass
            self._pa = None


# Global audio router instance
audio_router = AudioRouter()

# ---------------------------------------------------------------------------
# Shared state (protected by _state_lock)
# ---------------------------------------------------------------------------

_state_lock = threading.Lock()
_eq_settings = {}            # device_id -> {"bass": float, "treble": float}
_device_groups = {}          # group_id -> {"name": str, "device_ids": list}
_group_membership = {}       # device_id -> group_id  (reverse lookup, O(1))
_last_known_volumes = {}     # device_id -> float
_last_known_eq = {}          # device_id -> {"bass": float, "treble": float}
_previous_device_ids = set() # for change detection
_audio_levels = {}           # device_id -> smoothed peak
_level_decay = 0.85
_shutdown_event = threading.Event()  # signal background threads to stop
_zone_positions = {}             # device_id -> {x, y} canvas coords
_cue_device_id = None            # device used as cue/headphone output
_cue_members = set()             # device_ids in cue (preview) mode
_min_volumes = {}                # device_id -> float (0.0-1.0) minimum volume floor
_setlist_presets = {}            # spotify_track_id -> preset dict
_automation_keyframes = []       # [{pct: 0-1, x, y}] sorted by pct
_automation_active = False
_midi_device_name = None         # selected MIDI input device name
_midi_mappings = {}              # cc_number -> {target, device_id}
_midi_thread = None

# ---------------------------------------------------------------------------
# Spotify integration
# ---------------------------------------------------------------------------

SPOTIFY_CLIENT_ID = os.environ.get('SPOTIFY_CLIENT_ID', '180f7b3240d7473b9e56aedc227b5f3e')
SPOTIFY_REDIRECT_URI = 'http://127.0.0.1:5000/spotify/callback'
SPOTIFY_SCOPES = 'user-read-currently-playing user-modify-playback-state'
_SPOTIFY_TOKEN_FILE = os.path.join(
    os.path.dirname(os.path.abspath(sys.argv[0] if not getattr(sys, 'frozen', False)
                                     else sys.executable)),
    '.spotify_token.json')

_spotify_lock = threading.Lock()
_spotify_token = None        # {access_token, refresh_token, expires_at}
_spotify_code_verifier = None


def _save_spotify_token():
    """Persist refresh token and client ID to disk."""
    if not _spotify_token or not _spotify_token.get('refresh_token'):
        return
    try:
        data = {
            'refresh_token': _spotify_token['refresh_token'],
            'client_id': SPOTIFY_CLIENT_ID,
        }
        with open(_SPOTIFY_TOKEN_FILE, 'w') as f:
            _json.dump(data, f)
    except Exception:
        pass  # non-critical


def _load_spotify_token():
    """Load persisted refresh token from disk and refresh access token."""
    global _spotify_token, SPOTIFY_CLIENT_ID
    try:
        with open(_SPOTIFY_TOKEN_FILE, 'r') as f:
            data = _json.load(f)
        refresh_token = data.get('refresh_token')
        saved_client_id = data.get('client_id')
        if not refresh_token or not saved_client_id:
            return
        # Restore client ID if not set via env
        if SPOTIFY_CLIENT_ID == '180f7b3240d7473b9e56aedc227b5f3e' or not SPOTIFY_CLIENT_ID:
            SPOTIFY_CLIENT_ID = saved_client_id
        # Try refreshing with saved token
        resp = http_requests.post('https://accounts.spotify.com/api/token', data={
            'grant_type': 'refresh_token',
            'refresh_token': refresh_token,
            'client_id': SPOTIFY_CLIENT_ID,
        }, timeout=10)
        if resp.status_code == 200:
            tok = resp.json()
            with _spotify_lock:
                _spotify_token = {
                    'access_token': tok['access_token'],
                    'refresh_token': tok.get('refresh_token', refresh_token),
                    'expires_at': time.time() + tok.get('expires_in', 3600),
                }
                _save_spotify_token()  # update if new refresh token issued
    except Exception:
        pass  # file missing or invalid — user will auth normally

# ---------------------------------------------------------------------------
# Helper functions
# ---------------------------------------------------------------------------


def _get_groups_snapshot():
    """Return a copy of _device_groups under lock."""
    with _state_lock:
        return {gid: dict(g) for gid, g in _device_groups.items()}


def _sync_router(devices):
    """Start or update the AudioRouter with the given device list."""
    invalidate_endpoint_cache()
    if devices and not audio_router.is_running:
        threading.Thread(
            target=lambda: audio_router.start(devices),
            daemon=True,
        ).start()
    elif devices and audio_router.is_running:
        threading.Thread(
            target=lambda: audio_router.update_devices(devices),
            daemon=True,
        ).start()


def _enrich_devices(devices):
    """Add EQ / group / zone / cue state to device dicts. Returns a new list."""
    with _state_lock:
        enriched = []
        for d in devices:
            d2 = dict(d)
            did = d2["id"]
            d2["eq"] = dict(_eq_settings.get(did, {"bass": 0.0, "treble": 0.0}))
            d2["group"] = _group_membership.get(did, None)
            d2["zone"] = _zone_positions.get(did, None)
            d2["cue"] = did in _cue_members
            d2["min_volume"] = _min_volumes.get(did, 0.0)
            d2["delay_ms"] = audio_router._delay_ms.get(did, 0)
            enriched.append(d2)
        return enriched


def _restore_devices(new_ids, devices):
    """Restore volume / EQ / mute for devices that have reconnected."""
    # Snapshot state under lock, then apply COM calls outside lock
    with _state_lock:
        restore_plan = []
        for did in new_ids:
            vol = _last_known_volumes.get(did)
            eq = _last_known_eq.get(did)
            if vol is not None:
                restore_plan.append((did, vol, eq))

    restored = []
    for did, vol, eq in restore_plan:
        audio_router.set_volume(did, vol)
        restored.append(did)
        if eq is not None:
            audio_router.set_eq(did, eq.get("bass", 0.0), eq.get("treble", 0.0))
    return restored


def _device_monitor():
    """Background thread: poll devices every 3 s, emit device_update on change."""
    global _previous_device_ids
    _init_com()
    while not _shutdown_event.is_set():
        _shutdown_event.wait(timeout=3)
        if _shutdown_event.is_set():
            break
        try:
            devices = get_bluetooth_speakers()
            current_ids = {d["id"] for d in devices}
            with _state_lock:
                changed = current_ids != _previous_device_ids
                prev = set(_previous_device_ids)
                _previous_device_ids = current_ids

            if changed:
                new_ids = current_ids - prev
                if new_ids:
                    restored = _restore_devices(new_ids, devices)
                    for rid in restored:
                        socketio.emit("volume_restored", {"device_id": rid})
                _sync_router(devices)
                enriched = _enrich_devices(devices)
                socketio.emit("device_update", enriched)
                socketio.emit("router_status", {
                    "running": audio_router.is_running,
                    "outputs": audio_router.active_outputs,
                })
        except Exception as exc:
            print(f"[device_monitor] Error: {exc}")


def _audio_level_monitor():
    """Background thread: reads audio peak levels at ~15Hz."""
    _init_com()
    cached_meters = {}
    last_device_ids = set()

    while not _shutdown_event.is_set():
        try:
            time.sleep(1.0 / 15)

            with _state_lock:
                current_ids = set(_previous_device_ids)

            # Re-enumerate meters only when devices change
            if current_ids != last_device_ids:
                last_device_ids = set(current_ids)
                cached_meters.clear()
                try:
                    all_devs = AudioUtilities.GetAllDevices()
                    for dev in all_devs:
                        if dev.id and dev.id in current_ids:
                            try:
                                meter = dev._dev.Activate(
                                    IAudioMeterInformation._iid_, 0, None)
                                cached_meters[dev.id] = meter
                            except Exception:
                                pass
                except Exception:
                    pass

            # Read levels with smoothing
            levels = {}
            stale_meters = []
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
                except comtypes.COMError:
                    # Device disconnected — mark meter for removal
                    stale_meters.append(dev_id)
                    levels[dev_id] = 0.0
                except Exception:
                    levels[dev_id] = 0.0
            for dev_id in stale_meters:
                cached_meters.pop(dev_id, None)

            if levels:
                socketio.emit('audio_levels', levels)

            # Emit latency data alongside levels
            latency = audio_router.get_latency()
            if latency:
                socketio.emit('latency_update', latency)
        except Exception:
            time.sleep(1)


# ---------------------------------------------------------------------------
# Flask routes
# ---------------------------------------------------------------------------


@app.route("/")
def index():
    """Serve the frontend single-page application."""
    resp = make_response(render_template("index.html"))
    resp.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
    resp.headers["Pragma"] = "no-cache"
    resp.headers["Expires"] = "0"
    return resp


@app.route("/api/devices", methods=["GET"])
def api_devices():
    """Return the current list of active playback devices as JSON."""
    devices = get_bluetooth_speakers()
    return jsonify(_enrich_devices(devices))


@app.route("/api/volume", methods=["POST"])
def api_volume():
    """
    Set volume on a specific device.
    Expects JSON body: {"device_id": "...", "volume": 0.0-1.0}
    """
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

    # Only set the AudioRouter stream multiplier (not COM endpoint volume)
    audio_router.set_volume(device_id, volume)

    # Track last known volume
    with _state_lock:
        _last_known_volumes[device_id] = volume

    return jsonify({"success": True})


@app.route("/api/refresh", methods=["POST"])
def api_refresh():
    """Re-scan devices and return the updated list. Also sync the router."""
    devices = get_bluetooth_speakers()
    _sync_router(devices)
    return jsonify(_enrich_devices(devices))


@app.route("/api/router/status", methods=["GET"])
def api_router_status():
    """Return the current state of the audio router."""
    return jsonify({
        "running": audio_router.is_running,
        "outputs": audio_router.active_outputs,
    })


# ---------------------------------------------------------------------------
# Spotify routes
# ---------------------------------------------------------------------------


@app.route('/spotify/setup')
def spotify_setup():
    """Show Spotify Client ID setup page."""
    return '''<!DOCTYPE html>
<html><head><title>Spotify Setup</title>
<style>
body{font-family:system-ui;background:#0f0f23;color:#e0e0e0;display:flex;
justify-content:center;align-items:center;min-height:100vh;margin:0}
.box{background:#1a1a2e;border:1px solid #333;border-radius:12px;padding:2rem;
max-width:480px;width:90%}
h2{color:#00d4ff;margin-top:0}
input{width:100%;padding:0.6rem;border:1px solid #444;border-radius:6px;
background:#0f0f23;color:#e0e0e0;font-size:1rem;box-sizing:border-box;margin:0.5rem 0}
button{background:#00d4ff;color:#0f0f23;border:none;padding:0.6rem 1.5rem;
border-radius:6px;font-size:1rem;cursor:pointer;font-weight:bold;margin-top:0.5rem}
button:hover{background:#00b8d4}
a{color:#00d4ff}
ol{padding-left:1.2rem;line-height:1.8}
code{background:#0f0f23;padding:2px 6px;border-radius:3px;font-size:0.9em}
.err{color:#ff6b6b;margin-top:0.5rem;display:none}
.ok{color:#4caf50;margin-top:0.5rem;display:none}
</style></head><body>
<div class="box">
<h2>Spotify Setup</h2>
<p>To use Spotify integration, you need a free Spotify Developer app:</p>
<ol>
<li>Go to <a href="https://developer.spotify.com/dashboard" target="_blank">developer.spotify.com/dashboard</a></li>
<li>Create a new app (any name)</li>
<li>Add redirect URI: <code>http://127.0.0.1:5000/spotify/callback</code></li>
<li>Copy the <strong>Client ID</strong> and paste below</li>
</ol>
<input id="cid" type="text" placeholder="Paste your Spotify Client ID here" spellcheck="false">
<button onclick="save()">Save & Connect</button>
<div class="err" id="err"></div>
<div class="ok" id="ok"></div>
<script>
async function save(){
  const cid=document.getElementById('cid').value.trim();
  if(!cid){document.getElementById('err').style.display='block';
    document.getElementById('err').textContent='Please enter a Client ID';return}
  const r=await fetch('/api/spotify/client-id',{method:'POST',
    headers:{'Content-Type':'application/json'},body:JSON.stringify({client_id:cid})});
  if(r.ok){document.getElementById('ok').style.display='block';
    document.getElementById('ok').textContent='Saved! Redirecting to Spotify login...';
    document.getElementById('err').style.display='none';
    setTimeout(()=>window.location.href='/spotify/login',1000)}
  else{document.getElementById('err').style.display='block';
    document.getElementById('err').textContent='Failed to save';
    document.getElementById('ok').style.display='none'}}
</script></div></body></html>'''


@app.route('/api/spotify/client-id', methods=['POST'])
def set_spotify_client_id():
    """Set the Spotify Client ID at runtime."""
    global SPOTIFY_CLIENT_ID
    data = request.get_json(silent=True) or {}
    client_id = data.get('client_id', '').strip()
    if not client_id:
        return jsonify({'error': 'client_id required'}), 400
    SPOTIFY_CLIENT_ID = client_id
    return jsonify({'success': True})


@app.route('/spotify/login')
def spotify_login():
    """Redirect to Spotify authorization."""
    global _spotify_code_verifier
    if not SPOTIFY_CLIENT_ID:
        return redirect('/spotify/setup')
    _spotify_code_verifier = secrets.token_urlsafe(64)
    challenge = base64.urlsafe_b64encode(
        hashlib.sha256(_spotify_code_verifier.encode()).digest()
    ).rstrip(b'=').decode()
    params = urllib.parse.urlencode({
        'client_id': SPOTIFY_CLIENT_ID,
        'response_type': 'code',
        'redirect_uri': SPOTIFY_REDIRECT_URI,
        'scope': SPOTIFY_SCOPES,
        'code_challenge_method': 'S256',
        'code_challenge': challenge,
        'show_dialog': 'false',
    })
    return redirect(f'https://accounts.spotify.com/authorize?{params}')


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
    try:
        resp = http_requests.post('https://accounts.spotify.com/api/token', data={
            'grant_type': 'authorization_code',
            'code': code,
            'redirect_uri': SPOTIFY_REDIRECT_URI,
            'client_id': SPOTIFY_CLIENT_ID,
            'code_verifier': _spotify_code_verifier,
        }, timeout=10)
        if resp.status_code != 200:
            return f'<p>Token exchange failed: {html.escape(str(resp.status_code))}</p><p><a href="/">Back</a></p>'
        data = resp.json()
        with _spotify_lock:
            _spotify_token = {
                'access_token': data['access_token'],
                'refresh_token': data.get('refresh_token'),
                'expires_at': time.time() + data.get('expires_in', 3600),
            }
            _save_spotify_token()
    except Exception as exc:
        return f'<p>Error: {html.escape(str(exc))}</p><p><a href="/">Back</a></p>'
    return '<script>window.close();</script><p>Connected! You can close this window.</p>'


@app.route('/api/spotify/now-playing')
def spotify_now_playing():
    """Get currently playing track."""
    token = _get_spotify_token()
    if not token:
        return jsonify({'error': 'Not authenticated'}), 401
    try:
        resp = http_requests.get(
            'https://api.spotify.com/v1/me/player/currently-playing',
            headers={'Authorization': f'Bearer {token}'},
            timeout=5)
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


def _spotify_playback_action(endpoint, method='PUT'):
    """Execute a Spotify playback action (play/pause/next/previous)."""
    token = _get_spotify_token()
    if not token:
        return jsonify({'error': 'Not authenticated'}), 401
    url = f'https://api.spotify.com/v1/me/player/{endpoint}'
    req_fn = http_requests.put if method == 'PUT' else http_requests.post
    try:
        resp = req_fn(url, headers={'Authorization': f'Bearer {token}'}, timeout=5)
        if resp.status_code >= 400:
            return jsonify({'error': f'Spotify returned {resp.status_code}'}), resp.status_code
    except Exception as exc:
        return jsonify({'error': str(exc)}), 500
    return jsonify({'success': True})


@app.route('/api/spotify/play', methods=['POST'])
def spotify_play():
    return _spotify_playback_action('play')


@app.route('/api/spotify/pause', methods=['POST'])
def spotify_pause():
    return _spotify_playback_action('pause')


@app.route('/api/spotify/next', methods=['POST'])
def spotify_next():
    return _spotify_playback_action('next', method='POST')


@app.route('/api/spotify/previous', methods=['POST'])
def spotify_previous():
    return _spotify_playback_action('previous', method='POST')


@app.route('/api/spotify/seek', methods=['POST'])
def spotify_seek():
    """Seek to a position in the current track."""
    token = _get_spotify_token()
    if not token:
        return jsonify({'error': 'Not authenticated'}), 401
    data = request.get_json(silent=True) or {}
    position_ms = data.get('position_ms', 0)
    position_ms = max(0, int(position_ms))
    http_requests.put(
        f'https://api.spotify.com/v1/me/player/seek?position_ms={position_ms}',
        headers={'Authorization': f'Bearer {token}'}, timeout=5
    )
    return jsonify({'success': True})


@app.route('/api/spotify/audio-features/<track_id>')
def spotify_audio_features(track_id):
    """Get audio features (BPM/tempo) for a track."""
    token = _get_spotify_token()
    if not token:
        return jsonify({'error': 'Not authenticated'}), 401
    try:
        resp = http_requests.get(
            f'https://api.spotify.com/v1/audio-features/{track_id}',
            headers={'Authorization': f'Bearer {token}'}, timeout=5
        )
        if resp.status_code != 200:
            return jsonify({'tempo': 120})  # fallback BPM
        data = resp.json()
        return jsonify({'tempo': data.get('tempo', 120)})
    except Exception:
        return jsonify({'tempo': 120})


def _get_spotify_token():
    """Get valid Spotify access token, refreshing if needed."""
    global _spotify_token
    with _spotify_lock:
        if not _spotify_token:
            return None
        if time.time() > _spotify_token['expires_at'] - 60:
            _refresh_spotify_token_locked()
        return _spotify_token.get('access_token') if _spotify_token else None


def _refresh_spotify_token_locked():
    """Refresh the Spotify access token. Caller must hold _spotify_lock."""
    global _spotify_token
    if not _spotify_token or not _spotify_token.get('refresh_token'):
        _spotify_token = None
        return
    try:
        resp = http_requests.post('https://accounts.spotify.com/api/token', data={
            'grant_type': 'refresh_token',
            'refresh_token': _spotify_token['refresh_token'],
            'client_id': SPOTIFY_CLIENT_ID,
        }, timeout=10)
        if resp.status_code == 200:
            data = resp.json()
            _spotify_token['access_token'] = data['access_token']
            _spotify_token['expires_at'] = time.time() + data.get('expires_in', 3600)
            if 'refresh_token' in data:
                _spotify_token['refresh_token'] = data['refresh_token']
            _save_spotify_token()
        else:
            _spotify_token = None
    except Exception:
        _spotify_token = None


# ---------------------------------------------------------------------------
# WebSocket event handlers
# ---------------------------------------------------------------------------


@socketio.on("connect")
def ws_connect():
    """Send current device list and router status on connect."""
    devices = get_bluetooth_speakers()
    enriched = _enrich_devices(devices)
    emit("device_update", enriched)
    emit("router_status", {
        "running": audio_router.is_running,
        "outputs": audio_router.active_outputs,
    })


@socketio.on("disconnect")
def ws_disconnect():
    """Handle client disconnect — currently a no-op."""
    pass


@socketio.on("set_volume")
def ws_set_volume(data):
    """Set volume for a device, with group propagation."""
    device_id = data.get("device_id")
    volume = data.get("volume")
    if device_id is None or volume is None:
        return
    volume = max(0.0, min(1.0, float(volume)))

    # Only set the AudioRouter stream multiplier — NOT the COM endpoint volume.
    # Setting both would square the volume (endpoint_vol * stream_vol).
    audio_router.set_volume(device_id, volume)

    with _state_lock:
        _last_known_volumes[device_id] = volume
        # Group propagation — snapshot members list and update all volumes in one lock
        group_id = _group_membership.get(device_id)
        group_members = []
        if group_id and group_id in _device_groups:
            group_members = [m for m in _device_groups[group_id].get("device_ids", [])
                            if m != device_id]
            for member_id in group_members:
                _last_known_volumes[member_id] = volume

    # Apply to grouped members
    for member_id in group_members:
        audio_router.set_volume(member_id, volume)


@socketio.on("set_min_volume")
def ws_set_min_volume(data):
    """Set minimum volume floor for a device."""
    device_id = data.get("device_id")
    min_vol = data.get("min_volume")
    if device_id is None or min_vol is None:
        return
    min_vol = max(0.0, min(1.0, float(min_vol)))
    with _state_lock:
        _min_volumes[device_id] = min_vol


@socketio.on("refresh_devices")
def ws_refresh_devices():
    """Rescan devices and emit updated list."""
    devices = get_bluetooth_speakers()
    _sync_router(devices)
    enriched = _enrich_devices(devices)
    emit("device_update", enriched)
    emit("router_status", {
        "running": audio_router.is_running,
        "outputs": audio_router.active_outputs,
    })


@socketio.on("set_eq")
def ws_set_eq(data):
    """Set EQ for a device, clamped to [-1, 1]."""
    device_id = data.get("device_id")
    bass = data.get("bass", 0.0)
    treble = data.get("treble", 0.0)
    if device_id is None:
        return

    bass = max(-1.0, min(1.0, float(bass)))
    treble = max(-1.0, min(1.0, float(treble)))

    with _state_lock:
        _eq_settings[device_id] = {"bass": bass, "treble": treble}
        _last_known_eq[device_id] = {"bass": bass, "treble": treble}

    audio_router.set_eq(device_id, bass, treble)

    socketio.emit("eq_update", {
        "device_id": device_id,
        "bass": bass,
        "treble": treble,
    })


@socketio.on("set_group")
def ws_set_group(data):
    """Create or update a device group."""
    group_id = data.get("group_id")
    name = data.get("name", "")
    device_ids = data.get("device_ids", [])
    if group_id is None:
        return

    # Sanitize name
    name = html.escape(str(name))[:64]

    with _state_lock:
        # Clear old memberships for this group
        if group_id in _device_groups:
            for old_did in _device_groups[group_id].get("device_ids", []):
                if _group_membership.get(old_did) == group_id:
                    del _group_membership[old_did]

        _device_groups[group_id] = {"name": name, "device_ids": list(device_ids)}
        for did in device_ids:
            _group_membership[did] = group_id


@socketio.on("delete_group")
def ws_delete_group(data):
    """Remove a device group."""
    group_id = data.get("group_id")
    if group_id is None:
        return

    with _state_lock:
        if group_id in _device_groups:
            for did in _device_groups[group_id].get("device_ids", []):
                if _group_membership.get(did) == group_id:
                    del _group_membership[did]
            del _device_groups[group_id]


@socketio.on("set_delay")
def ws_set_delay(data):
    """Set delay compensation for a speaker (0-500ms)."""
    device_id = data.get("device_id")
    delay_ms = data.get("delay_ms", 0)
    if device_id is None:
        return
    delay_ms = max(0.0, min(500.0, float(delay_ms)))
    audio_router.set_delay(device_id, delay_ms)


@socketio.on("set_zone_position")
def ws_set_zone_position(data):
    """Set 2D canvas position for a speaker (zone mapping)."""
    device_id = data.get("device_id")
    x = data.get("x")
    y = data.get("y")
    if device_id is None or x is None or y is None:
        return
    with _state_lock:
        _zone_positions[device_id] = {"x": float(x), "y": float(y)}
    socketio.emit("zone_update", _get_zone_snapshot())


@socketio.on("set_effect")
def ws_set_effect(data):
    """Set BPM-synced effect for a device."""
    device_id = data.get("device_id")
    effect_type = data.get("type", "off")
    rate_hz = data.get("rate_hz", 2.0)
    depth = data.get("depth", 0.5)
    if device_id is None:
        return
    audio_router.set_effect(device_id, effect_type, rate_hz, depth)


@socketio.on("set_cue")
def ws_set_cue(data):
    """Toggle cue/preview mode for a device."""
    device_id = data.get("device_id")
    enabled = data.get("enabled", False)
    if device_id is None:
        return
    with _state_lock:
        if enabled:
            _cue_members.add(device_id)
        else:
            _cue_members.discard(device_id)
    socketio.emit("cue_update", {"cue_members": list(_cue_members),
                                  "cue_device": _cue_device_id})


@socketio.on("set_cue_device")
def ws_set_cue_device(data):
    """Select which output device serves as the cue/headphone output."""
    global _cue_device_id
    with _state_lock:
        _cue_device_id = data.get("device_id")
    socketio.emit("cue_update", {"cue_members": list(_cue_members),
                                  "cue_device": _cue_device_id})


@socketio.on("save_setlist_preset")
def ws_save_setlist_preset(data):
    """Link/unlink a mixer preset to a Spotify track."""
    track_id = data.get("track_id")
    preset = data.get("preset")
    if not track_id:
        return
    with _state_lock:
        if preset:
            _setlist_presets[track_id] = preset
        else:
            _setlist_presets.pop(track_id, None)
    socketio.emit("setlist_update", _get_setlist_snapshot())


@socketio.on("set_automation")
def ws_set_automation(data):
    """Set crossfade automation keyframes."""
    global _automation_active
    keyframes = data.get("keyframes", [])
    active = data.get("active", False)
    with _state_lock:
        _automation_keyframes.clear()
        _automation_keyframes.extend(
            sorted(keyframes, key=lambda k: k.get('pct', 0)))
        _automation_active = active
    socketio.emit("automation_update", {
        "keyframes": list(_automation_keyframes),
        "active": _automation_active})


@socketio.on("set_midi_device")
def ws_set_midi_device(data):
    """Select a MIDI input device for controller mapping."""
    global _midi_device_name
    name = data.get("name")
    with _state_lock:
        _midi_device_name = name
    _start_midi_listener()


@socketio.on("set_midi_mapping")
def ws_set_midi_mapping(data):
    """Map a MIDI CC number to a mixer control."""
    cc = data.get("cc")
    target = data.get("target")  # 'crossfade_x', 'crossfade_y', 'volume'
    device_id = data.get("device_id")
    if cc is None:
        return
    with _state_lock:
        if target:
            _midi_mappings[int(cc)] = {
                "target": target, "device_id": device_id}
        else:
            _midi_mappings.pop(int(cc), None)


def _get_zone_snapshot():
    """Return zone positions under lock."""
    with _state_lock:
        return dict(_zone_positions)


def _get_setlist_snapshot():
    """Return setlist presets under lock."""
    with _state_lock:
        return dict(_setlist_presets)


# ---------------------------------------------------------------------------
# REST endpoints for new DJ features
# ---------------------------------------------------------------------------


@app.route('/api/delay', methods=['POST'])
def api_set_delay():
    """Set delay compensation for a speaker."""
    data = request.get_json(silent=True) or {}
    device_id = data.get('device_id')
    delay_ms = data.get('delay_ms', 0)
    if not device_id:
        return jsonify({'error': 'device_id required'}), 400
    audio_router.set_delay(device_id, delay_ms)
    return jsonify({'success': True})


@app.route('/api/latency')
def api_latency():
    """Get per-speaker write latency measurements."""
    return jsonify(audio_router.get_latency())


@app.route('/api/midi/devices')
def api_midi_devices():
    """List available MIDI input devices."""
    if not _MIDI_AVAILABLE:
        return jsonify({'available': False, 'devices': []})
    try:
        inputs = mido.get_input_names()
    except Exception:
        inputs = []
    return jsonify({
        'available': True, 'devices': inputs,
        'selected': _midi_device_name})


@app.route('/api/zone-positions', methods=['GET'])
def api_get_zone_positions():
    """Get all speaker zone positions."""
    return jsonify(_get_zone_snapshot())


@app.route('/api/zone-positions', methods=['POST'])
def api_set_zone_positions():
    """Set speaker zone positions."""
    data = request.get_json(silent=True) or {}
    with _state_lock:
        for did, pos in data.items():
            _zone_positions[did] = {
                "x": float(pos.get("x", 250)),
                "y": float(pos.get("y", 250))}
    return jsonify({'success': True})


@app.route('/api/setlist-presets')
def api_setlist_presets():
    """Get all setlist-linked presets."""
    return jsonify(_get_setlist_snapshot())


@app.route('/api/automation')
def api_automation():
    """Get current automation keyframes."""
    with _state_lock:
        return jsonify({
            "keyframes": list(_automation_keyframes),
            "active": _automation_active})


# ---------------------------------------------------------------------------
# MIDI listener
# ---------------------------------------------------------------------------


def _start_midi_listener():
    """Start or restart the MIDI listener thread."""
    global _midi_thread
    if not _MIDI_AVAILABLE or not _midi_device_name:
        return
    _midi_thread = threading.Thread(target=_midi_worker, daemon=True)
    _midi_thread.start()


def _midi_worker():
    """Thread: listen for MIDI CC messages and map to mixer controls."""
    try:
        with mido.open_input(_midi_device_name) as port:
            print(f"[MIDI] Listening on '{_midi_device_name}'")
            for msg in port:
                if _shutdown_event.is_set():
                    break
                if _midi_device_name != port.name:
                    break  # Device changed, exit to be restarted
                if msg.type == 'control_change':
                    _handle_midi_cc(msg.control, msg.value / 127.0)
    except Exception as exc:
        print(f"[MIDI] Error: {exc}")


def _handle_midi_cc(cc, value):
    """Process a MIDI CC message through the mapping table."""
    with _state_lock:
        mapping = _midi_mappings.get(cc)
    if not mapping:
        return
    target = mapping['target']
    if target == 'crossfade_x':
        socketio.emit('midi_cc', {'target': 'crossfade_x', 'value': value})
    elif target == 'crossfade_y':
        socketio.emit('midi_cc', {'target': 'crossfade_y', 'value': value})
    elif target == 'volume' and mapping.get('device_id'):
        audio_router.set_volume(mapping['device_id'], value)
        socketio.emit('midi_cc', {
            'target': 'volume',
            'device_id': mapping['device_id'],
            'value': value})


def _spotify_poller():
    """Background thread: polls Spotify every 3s, emits via WebSocket."""
    last_setlist_track = None
    while not _shutdown_event.is_set():
        _shutdown_event.wait(timeout=3)
        if _shutdown_event.is_set():
            break
        try:
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
            track_id = item.get('id', '')
            progress_ms = data.get('progress_ms', 0)
            duration_ms = item.get('duration_ms', 0)
            socketio.emit('spotify_update', {
                'is_playing': data.get('is_playing', False),
                'track': item.get('name', ''),
                'artist': ', '.join(a['name'] for a in item.get('artists', [])),
                'album_art': (item.get('album', {}).get('images', [{}])[0].get('url', '')),
                'progress_ms': progress_ms,
                'duration_ms': duration_ms,
                'track_id': track_id,
            })
            # Setlist preset matching — only emit once per track change
            if track_id and track_id != last_setlist_track:
                last_setlist_track = track_id
                with _state_lock:
                    matched = _setlist_presets.get(track_id)
                if matched:
                    socketio.emit('setlist_preset_match', {
                        'track_id': track_id, 'preset': matched})
            # Automation playback
            with _state_lock:
                if _automation_active and _automation_keyframes and duration_ms:
                    pct = progress_ms / duration_ms
                    kfs = list(_automation_keyframes)
                    # Interpolate position from keyframes
                    if kfs:
                        pos = _interpolate_keyframes(kfs, pct)
                        if pos:
                            socketio.emit('automation_position', pos)
        except Exception:
            pass


def _interpolate_keyframes(keyframes, pct):
    """Interpolate x,y position from sorted keyframes at given percentage."""
    if not keyframes:
        return None
    if pct <= keyframes[0].get('pct', 0):
        return {'x': keyframes[0].get('x', 250), 'y': keyframes[0].get('y', 250)}
    if pct >= keyframes[-1].get('pct', 1):
        return {'x': keyframes[-1].get('x', 250), 'y': keyframes[-1].get('y', 250)}
    for i in range(len(keyframes) - 1):
        p0 = keyframes[i].get('pct', 0)
        p1 = keyframes[i + 1].get('pct', 1)
        if p0 <= pct <= p1:
            t = (pct - p0) / max(p1 - p0, 0.001)
            return {
                'x': keyframes[i].get('x', 250) + t * (
                    keyframes[i + 1].get('x', 250) - keyframes[i].get('x', 250)),
                'y': keyframes[i].get('y', 250) + t * (
                    keyframes[i + 1].get('y', 250) - keyframes[i].get('y', 250)),
            }
    return None


# ---------------------------------------------------------------------------
# Application entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    HOST = "127.0.0.1"
    PORT = 5000
    url = f"http://{HOST}:{PORT}"

    print(f"Starting Bluetooth Crossfade Mixer server at {url}")
    print("Press Ctrl+C to stop.\n")

    # Auto-start audio routing in a background thread with retry
    def start_router():
        global _previous_device_ids
        _init_com()
        for attempt in range(3):
            devices = get_bluetooth_speakers()
            if devices:
                with _state_lock:
                    _previous_device_ids = {d["id"] for d in devices}
                time.sleep(0.5)  # Let pycaw COM calls settle
                ok = audio_router.start(devices)
                if ok:
                    return
                print(f"[AudioRouter] Start attempt {attempt + 1} failed, retrying...")
                time.sleep(2)
            else:
                print("[AudioRouter] No BT speakers found, retrying in 5s...")
                time.sleep(5)
        print("[AudioRouter] Could not start after 3 attempts. "
              "Connect BT speakers and click Refresh.")

    threading.Thread(target=start_router, daemon=True).start()

    # Start device monitor background thread
    threading.Thread(target=_device_monitor, daemon=True).start()

    # Start audio level metering background thread
    threading.Thread(target=_audio_level_monitor, daemon=True).start()

    # Restore Spotify session from saved token (skips re-auth if valid)
    _load_spotify_token()
    if _spotify_token:
        print("[Spotify] Restored session from saved token — no login needed")
    else:
        print("[Spotify] No saved session — click Connect Spotify to authenticate")

    # Start Spotify poller
    threading.Thread(target=_spotify_poller, daemon=True).start()

    # Open the browser after a short delay so the server is ready.
    threading.Timer(1.5, lambda: webbrowser.open(url)).start()

    socketio.run(app, host=HOST, port=PORT, debug=False, allow_unsafe_werkzeug=True)
