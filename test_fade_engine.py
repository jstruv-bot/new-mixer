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
