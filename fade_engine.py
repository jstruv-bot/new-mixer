# fade_engine.py
"""Fade engine: weight computation, keyframe interpolation, and fade storage."""
import copy
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
        except IOError as e:
            print(f"[FadeStore] Failed to save fades: {e}")

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
            fade = self._fades.get(slot)
            return copy.deepcopy(fade) if fade else None

    def save_fade(self, fade_data):
        """Save a fade to the next available slot. Returns slot number or None if full."""
        with self._lock:
            for slot in range(1, MAX_SLOTS + 1):
                if slot not in self._fades:
                    stored = copy.deepcopy(fade_data)
                    stored['created_at'] = datetime.now(timezone.utc).isoformat()
                    self._fades[slot] = stored
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

    def clear(self):
        """Remove all fades (thread-safe)."""
        with self._lock:
            self._fades.clear()
            self._save()
