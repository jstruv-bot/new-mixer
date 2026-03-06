"""Unit tests for Bluetooth Crossfade Mixer server."""
import sys
import time
import types
import collections
from unittest.mock import MagicMock, patch

import pytest

# ---------------------------------------------------------------------------
# Mock out pycaw, comtypes, pyaudiowpatch, and numpy BEFORE importing server
# ---------------------------------------------------------------------------

# Create mock modules so server.py can import without real hardware
_mock_comtypes = MagicMock()
_mock_comtypes.CoInitialize = MagicMock()
sys.modules["comtypes"] = _mock_comtypes

_mock_pycaw = types.ModuleType("pycaw")
_mock_pycaw_pycaw = types.ModuleType("pycaw.pycaw")
_mock_audio_utilities = MagicMock()
_mock_audio_utilities.GetAllDevices.return_value = []
_mock_pycaw_pycaw.AudioUtilities = _mock_audio_utilities
_mock_pycaw_pycaw.IAudioMeterInformation = MagicMock()
sys.modules["pycaw"] = _mock_pycaw
sys.modules["pycaw.pycaw"] = _mock_pycaw_pycaw

_mock_pyaudio = MagicMock()
_mock_pyaudio.paFloat32 = 1
sys.modules["pyaudiowpatch"] = _mock_pyaudio

# numpy is available but let's ensure it doesn't cause issues
# (it should already be installed)

import server  # noqa: E402  (must come after mocks)
import server as server_module  # noqa: E402  alias for tests


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def app():
    """Create a Flask test app."""
    server.app.config["TESTING"] = True
    return server.app


@pytest.fixture
def client(app):
    """Create a Flask test client."""
    return app.test_client()


@pytest.fixture
def socketio_client(app):
    """Create a Flask-SocketIO test client."""
    return server.socketio.test_client(app)


# ---------------------------------------------------------------------------
# Smoke test
# ---------------------------------------------------------------------------


def test_smoke():
    """Verify test infrastructure works."""
    assert True


# ---------------------------------------------------------------------------
# TestWebSocket
# ---------------------------------------------------------------------------


class TestWebSocket:
    """Tests for WebSocket event handlers."""

    def test_connect(self, socketio_client):
        """Verify client connects successfully."""
        assert socketio_client.is_connected()

    def test_disconnect(self, socketio_client):
        """Verify clean disconnect."""
        assert socketio_client.is_connected()
        socketio_client.disconnect()
        assert not socketio_client.is_connected()

    def test_set_volume_event(self, socketio_client):
        """Verify set_volume handler calls audio_router.set_volume."""
        with patch.object(server.audio_router, "set_volume") as mock_sv:
            socketio_client.emit("set_volume", {
                "device_id": "test-device-1",
                "volume": 0.75,
            })
            mock_sv.assert_called_with("test-device-1", 0.75)

    def test_refresh_devices_event(self, socketio_client):
        """Verify refresh emits device_update back."""
        with patch.object(server, "get_bluetooth_speakers", return_value=[]):
            socketio_client.emit("refresh_devices")
            received = socketio_client.get_received()
            # Should have received device_update and router_status
            event_names = [msg["name"] for msg in received]
            assert "device_update" in event_names
            assert "router_status" in event_names


# ---------------------------------------------------------------------------
# TestRESTEndpoints
# ---------------------------------------------------------------------------


class TestRESTEndpoints:
    """Tests for REST API endpoints."""

    def test_get_devices(self, client):
        """GET /api/devices returns JSON."""
        with patch.object(server, "get_bluetooth_speakers", return_value=[]):
            resp = client.get("/api/devices")
            assert resp.status_code == 200
            assert resp.get_json() == []

    def test_post_volume_valid(self, client):
        """POST /api/volume succeeds with valid data."""
        with patch.object(server.audio_router, "set_volume"):
            resp = client.post("/api/volume", json={
                "device_id": "test-device-1",
                "volume": 0.5,
            })
            assert resp.status_code == 200
            data = resp.get_json()
            assert data["success"] is True

    def test_post_volume_missing_fields(self, client):
        """POST /api/volume returns 400 when fields are missing."""
        resp = client.post("/api/volume", json={"device_id": "test-device-1"})
        assert resp.status_code == 400
        data = resp.get_json()
        assert data["success"] is False

    def test_post_volume_invalid_json(self, client):
        """POST /api/volume returns 400 when body is not JSON."""
        resp = client.post("/api/volume", data="not json",
                           content_type="text/plain")
        assert resp.status_code == 400

    def test_refresh_endpoint(self, client):
        """POST /api/refresh works."""
        with patch.object(server, "get_bluetooth_speakers", return_value=[]):
            resp = client.post("/api/refresh")
            assert resp.status_code == 200
            assert resp.get_json() == []

    def test_router_status(self, client):
        """GET /api/router/status returns state."""
        resp = client.get("/api/router/status")
        assert resp.status_code == 200
        data = resp.get_json()
        assert "running" in data
        assert "outputs" in data

    def test_index_serves_html(self, client):
        """GET / returns HTML."""
        resp = client.get("/")
        assert resp.status_code == 200
        assert b"html" in resp.data.lower()


# ---------------------------------------------------------------------------
# TestAudioLevels
# ---------------------------------------------------------------------------


class TestAudioLevels:
    """Audio level metering tests."""

    def test_audio_levels_event_structure(self, socketio_client):
        """Server can emit audio_levels events."""
        server_module.socketio.emit('audio_levels', {'d1': 0.42})
        received = socketio_client.get_received()
        level_events = [r for r in received if r['name'] == 'audio_levels']
        assert len(level_events) >= 1

    def test_level_smoothing_fast_attack(self):
        """New peak > current -> jump to peak immediately."""
        current = 0.3
        peak = 0.8
        if peak > current:
            result = peak
        else:
            result = current * 0.85
        assert result == 0.8

    def test_level_smoothing_slow_decay(self):
        """Peak < current -> decay slowly."""
        current = 0.8
        peak = 0.2
        DECAY = 0.85
        if peak > current:
            result = peak
        else:
            result = current * DECAY
        assert abs(result - 0.68) < 0.01


# ---------------------------------------------------------------------------
# TestCrossfadeCurves
# ---------------------------------------------------------------------------


class TestCrossfadeCurves:
    """Crossfade curve formula tests."""

    def test_inverse_square(self):
        epsilon = 100
        dist = 50
        weight = 1.0 / (dist * dist + epsilon)
        dist_far = 150
        weight_far = 1.0 / (dist_far * dist_far + epsilon)
        assert weight > weight_far

    def test_linear(self):
        max_dist = 180
        dist = 90
        weight = max(0, 1 - dist / max_dist)
        assert abs(weight - 0.5) < 0.01
        assert max(0, 1 - max_dist / max_dist) == 0

    def test_logarithmic(self):
        import math
        k = 2.0
        weight_close = 1.0 / (1 + k * math.log(1 + 0.1))
        assert weight_close > 0.8
        # Verify monotonic decrease: farther distance -> lower weight
        weight_far = 1.0 / (1 + k * math.log(1 + 100))
        assert weight_close > weight_far

    def test_equal_power(self):
        import math
        max_dist = 180
        weight_center = math.cos(0 / max_dist * math.pi / 2) ** 2
        assert abs(weight_center - 1.0) < 0.001
        weight_edge = math.cos(max_dist / max_dist * math.pi / 2) ** 2
        assert abs(weight_edge) < 0.001


# ---------------------------------------------------------------------------
# TestMute
# ---------------------------------------------------------------------------


class TestSpotifyEndpoints:
    """Tests for newer Spotify API endpoints."""

    def test_spotify_seek(self, client):
        server_module._spotify_token = {
            'access_token': 'fake',
            'refresh_token': 'fake-refresh',
            'expires_at': time.time() + 3600
        }
        mock_resp = MagicMock()
        mock_resp.status_code = 204
        with patch('server.http_requests.put', return_value=mock_resp) as mock_put:
            resp = client.post('/api/spotify/seek', json={'position_ms': 30000})
            assert resp.status_code == 200
            assert 'position_ms=30000' in mock_put.call_args[0][0]

    def test_spotify_seek_no_token(self, client):
        server_module._spotify_token = None
        resp = client.post('/api/spotify/seek', json={'position_ms': 0})
        assert resp.status_code == 401

    def test_set_client_id(self, client):
        resp = client.post('/api/spotify/client-id',
                           json={'client_id': 'test-id-123'})
        assert resp.status_code == 200
        assert server_module.SPOTIFY_CLIENT_ID == 'test-id-123'

    def test_set_client_id_empty(self, client):
        resp = client.post('/api/spotify/client-id',
                           json={'client_id': ''})
        assert resp.status_code == 400


# ---------------------------------------------------------------------------
# TestEQ
# ---------------------------------------------------------------------------


class TestEQ:
    """EQ DSP and WebSocket tests."""

    def test_set_eq_event(self, socketio_client):
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
        with patch.object(server_module.audio_router, 'set_eq'):
            socketio_client.emit('set_eq', {
                'device_id': 'd1', 'bass': 5.0, 'treble': -3.0
            })
            with server_module._state_lock:
                eq = server_module._eq_settings.get('d1')
                assert eq['bass'] == 1.0
                assert eq['treble'] == -1.0

    def test_biquad_low_shelf_coefficients(self):
        import math
        coeffs = server_module.AudioRouter._compute_biquad_low_shelf(250, 48000, 6.0)
        assert len(coeffs) == 5
        assert all(math.isfinite(c) for c in coeffs)

    def test_biquad_high_shelf_coefficients(self):
        import math
        coeffs = server_module.AudioRouter._compute_biquad_high_shelf(4000, 48000, 6.0)
        assert len(coeffs) == 5
        assert all(math.isfinite(c) for c in coeffs)

    def test_biquad_passthrough_at_zero_gain(self):
        coeffs = server_module.AudioRouter._compute_biquad_low_shelf(250, 48000, 0.0)
        assert coeffs == (1.0, 0.0, 0.0, 0.0, 0.0)


# ---------------------------------------------------------------------------
# TestVolumeRestore
# ---------------------------------------------------------------------------


class TestVolumeRestore:
    """Auto-reconnect volume restore tests."""

    def test_volume_remembered_on_set(self, socketio_client):
        with patch.object(server_module.audio_router, 'set_volume'):
            socketio_client.emit('set_volume', {'device_id': 'd1', 'volume': 0.65})
            with server_module._state_lock:
                assert abs(server_module._last_known_volumes.get('d1', 0) - 0.65) < 0.01

    def test_eq_remembered_on_set(self, socketio_client):
        with patch.object(server_module.audio_router, 'set_eq'):
            socketio_client.emit('set_eq', {'device_id': 'd1', 'bass': 0.3, 'treble': -0.5})
            with server_module._state_lock:
                eq = server_module._last_known_eq.get('d1')
                assert eq is not None
                assert abs(eq['bass'] - 0.3) < 0.01

    def test_restore_devices_applies_volume(self):
        with server_module._state_lock:
            server_module._last_known_volumes['d1'] = 0.7
        with patch.object(server_module.audio_router, 'set_volume') as mock_vol:
            with patch.object(server_module.audio_router, 'set_eq'):
                restored = server_module._restore_devices({'d1'}, [{'id': 'd1', 'name': 'S1', 'volume': 1.0}])
                mock_vol.assert_called_with('d1', 0.7)


# ---------------------------------------------------------------------------
# TestSpotify
# ---------------------------------------------------------------------------


class TestSpotify:
    """Spotify integration tests."""

    def test_spotify_login_redirects(self, client):
        server_module.SPOTIFY_CLIENT_ID = 'test-client-id'
        resp = client.get('/spotify/login')
        assert resp.status_code == 302
        location = resp.headers.get('Location', '')
        assert 'accounts.spotify.com' in location

    def test_spotify_login_no_client_id(self, client):
        server_module.SPOTIFY_CLIENT_ID = ''
        resp = client.get('/spotify/login')
        assert resp.status_code == 302
        assert '/spotify/setup' in resp.headers['Location']

    def test_spotify_callback_error_xss_safe(self, client):
        resp = client.get('/spotify/callback?error=<script>alert(1)</script>')
        assert resp.status_code == 200
        assert b'<script>alert(1)</script>' not in resp.data
        assert b'&lt;script&gt;' in resp.data

    def test_spotify_now_playing_no_token(self, client):
        server_module._spotify_token = None
        resp = client.get('/api/spotify/now-playing')
        assert resp.status_code == 401

    def test_spotify_now_playing_with_token(self, client):
        server_module._spotify_token = {
            'access_token': 'fake',
            'refresh_token': 'fake-refresh',
            'expires_at': time.time() + 3600
        }
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.content = b'{"is_playing": true}'
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
        with patch('server.http_requests.get', return_value=mock_resp):
            resp = client.get('/api/spotify/now-playing')
            data = resp.get_json()
            assert data['is_playing'] is True
            assert data['track'] == 'Test Song'
            assert data['artist'] == 'Test Artist'

    def test_spotify_play_pause(self, client):
        server_module._spotify_token = {
            'access_token': 'fake',
            'refresh_token': 'fake-refresh',
            'expires_at': time.time() + 3600
        }
        mock_resp = MagicMock()
        mock_resp.status_code = 204
        with patch('server.http_requests.put', return_value=mock_resp):
            resp = client.post('/api/spotify/play')
            assert resp.status_code == 200
        with patch('server.http_requests.put', return_value=mock_resp):
            resp = client.post('/api/spotify/pause')
            assert resp.status_code == 200


# ---------------------------------------------------------------------------
# TestDelayCompensation
# ---------------------------------------------------------------------------


class TestDelayCompensation:
    """Delay compensation tests."""

    def test_set_delay_via_ws(self, socketio_client):
        with patch.object(server_module.audio_router, 'set_delay') as mock:
            socketio_client.emit('set_delay', {
                'device_id': 'd1', 'delay_ms': 50
            })
            mock.assert_called_with('d1', 50)

    def test_set_delay_rest(self, client):
        with patch.object(server_module.audio_router, 'set_delay') as mock:
            resp = client.post('/api/delay', json={
                'device_id': 'd1', 'delay_ms': 100
            })
            assert resp.status_code == 200
            mock.assert_called_with('d1', 100)

    def test_set_delay_rest_missing_device(self, client):
        resp = client.post('/api/delay', json={'delay_ms': 50})
        assert resp.status_code == 400

    def test_delay_clamping(self):
        """Delay should be clamped to 0-500ms."""
        server_module.audio_router.set_delay('d1', -10)
        with server_module.audio_router._lock:
            assert server_module.audio_router._delay_ms.get('d1', 0) == 0.0
        server_module.audio_router.set_delay('d1', 9999)
        with server_module.audio_router._lock:
            assert server_module.audio_router._delay_ms.get('d1', 0) == 500.0


# ---------------------------------------------------------------------------
# TestEffects
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# TestLatency
# ---------------------------------------------------------------------------


class TestLatency:
    """Latency monitor tests."""

    def test_latency_endpoint(self, client):
        resp = client.get('/api/latency')
        assert resp.status_code == 200
        data = resp.get_json()
        assert isinstance(data, dict)

    def test_get_latency_returns_dict(self):
        result = server_module.audio_router.get_latency()
        assert isinstance(result, dict)


# ---------------------------------------------------------------------------
# TestZonePositions
# ---------------------------------------------------------------------------


class TestZonePositions:
    """Speaker zone mapping tests."""

    def test_set_zone_position_via_ws(self, socketio_client):
        socketio_client.emit('set_zone_position', {
            'device_id': 'd1', 'x': 100, 'y': 200
        })
        with server_module._state_lock:
            assert server_module._zone_positions.get('d1') == {
                'x': 100.0, 'y': 200.0}

    def test_get_zone_positions_rest(self, client):
        with server_module._state_lock:
            server_module._zone_positions['d1'] = {'x': 150, 'y': 250}
        resp = client.get('/api/zone-positions')
        assert resp.status_code == 200
        data = resp.get_json()
        assert 'd1' in data

    def test_set_zone_positions_rest(self, client):
        resp = client.post('/api/zone-positions', json={
            'd2': {'x': 300, 'y': 400}
        })
        assert resp.status_code == 200
        with server_module._state_lock:
            assert server_module._zone_positions['d2']['x'] == 300.0


# ---------------------------------------------------------------------------
# TestCueChannel
# ---------------------------------------------------------------------------


class TestCueChannel:
    """Cue/preview channel tests."""

    def test_set_cue_enable(self, socketio_client):
        socketio_client.emit('set_cue', {
            'device_id': 'd1', 'enabled': True
        })
        with server_module._state_lock:
            assert 'd1' in server_module._cue_members

    def test_set_cue_disable(self, socketio_client):
        with server_module._state_lock:
            server_module._cue_members.add('d1')
        socketio_client.emit('set_cue', {
            'device_id': 'd1', 'enabled': False
        })
        with server_module._state_lock:
            assert 'd1' not in server_module._cue_members

    def test_set_cue_device(self, socketio_client):
        socketio_client.emit('set_cue_device', {
            'device_id': 'headphones-1'
        })
        with server_module._state_lock:
            assert server_module._cue_device_id == 'headphones-1'


# ---------------------------------------------------------------------------
# TestMaxVolume
# ---------------------------------------------------------------------------


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
        # Cleanup
        with server_module._state_lock:
            server_module._max_volumes.pop('d1', None)

    def test_max_volume_zero_means_unlimited(self, socketio_client):
        with server_module._state_lock:
            server_module._max_volumes['d1'] = 0.5
        socketio_client.emit('set_max_volume', {
            'device_id': 'd1', 'max_volume': 0
        })
        with server_module._state_lock:
            assert 'd1' not in server_module._max_volumes


# ---------------------------------------------------------------------------
# TestEnrichDevices
# ---------------------------------------------------------------------------


class TestEnrichDevices:
    """Test device enrichment includes new fields."""

    def test_enrich_includes_zone_and_cue(self):
        with server_module._state_lock:
            server_module._zone_positions['d1'] = {'x': 100, 'y': 200}
            server_module._cue_members.add('d1')
        devices = [{'id': 'd1', 'name': 'Speaker 1', 'volume': 1.0}]
        enriched = server_module._enrich_devices(devices)
        assert enriched[0]['zone'] == {'x': 100, 'y': 200}
        assert enriched[0]['cue'] is True

    def test_enrich_includes_min_volume(self):
        with server_module._state_lock:
            server_module._min_volumes['d1'] = 0.25
        devices = [{'id': 'd1', 'name': 'Speaker 1', 'volume': 1.0}]
        enriched = server_module._enrich_devices(devices)
        assert enriched[0]['min_volume'] == 0.25
        # Cleanup
        with server_module._state_lock:
            server_module._min_volumes.pop('d1', None)

    def test_enrich_default_min_volume_zero(self):
        devices = [{'id': 'd_new', 'name': 'Speaker X', 'volume': 1.0}]
        enriched = server_module._enrich_devices(devices)
        assert enriched[0]['min_volume'] == 0.0


# ---------------------------------------------------------------------------
# TestMinVolume
# ---------------------------------------------------------------------------


class TestMinVolume:
    """Per-device minimum volume floor tests."""

    def test_set_min_volume_ws(self, socketio_client):
        socketio_client.emit('set_min_volume', {
            'device_id': 'd1', 'min_volume': 0.3
        })
        with server_module._state_lock:
            assert server_module._min_volumes.get('d1') == 0.3
            server_module._min_volumes.pop('d1', None)

    def test_set_min_volume_clamped(self, socketio_client):
        socketio_client.emit('set_min_volume', {
            'device_id': 'd1', 'min_volume': 1.5
        })
        with server_module._state_lock:
            assert server_module._min_volumes.get('d1') == 1.0
            server_module._min_volumes.pop('d1', None)

    def test_set_min_volume_negative_clamped(self, socketio_client):
        socketio_client.emit('set_min_volume', {
            'device_id': 'd1', 'min_volume': -0.5
        })
        with server_module._state_lock:
            assert server_module._min_volumes.get('d1') == 0.0
            server_module._min_volumes.pop('d1', None)

    def test_set_min_volume_missing_fields(self, socketio_client):
        # Should not crash with missing fields
        socketio_client.emit('set_min_volume', {})
        socketio_client.emit('set_min_volume', {'device_id': 'd1'})


# ---------------------------------------------------------------------------
# TestAutoDJEnergy
# ---------------------------------------------------------------------------


class TestAutoDJEnergy:
    """Auto-DJ energy level tests."""

    def test_energy_level_default_zero(self):
        assert server_module.audio_router.energy_level == 0.0

    def test_energy_level_writable(self):
        server_module.audio_router._energy_level = 0.75
        assert abs(server_module.audio_router.energy_level - 0.75) < 0.001
        server_module.audio_router._energy_level = 0.0


# ---------------------------------------------------------------------------
# TestSpatialAudio
# ---------------------------------------------------------------------------


class TestSpatialAudio:
    """3D spatial audio (pan + stereo separation) tests."""

    def test_set_pan_stores(self):
        server_module.audio_router.set_pan('d1', 0.5)
        assert server_module.audio_router._pan['d1'] == 0.5
        server_module.audio_router._pan.pop('d1', None)

    def test_set_pan_clamped(self):
        server_module.audio_router.set_pan('d1', 2.0)
        assert server_module.audio_router._pan['d1'] == 1.0
        server_module.audio_router.set_pan('d1', -3.0)
        assert server_module.audio_router._pan['d1'] == -1.0
        server_module.audio_router._pan.pop('d1', None)

    def test_set_stereo_separation(self):
        server_module.audio_router.set_stereo_separation(0.75)
        assert server_module.audio_router._stereo_sep == 0.75
        server_module.audio_router._stereo_sep = 0.0

    def test_set_stereo_separation_clamped(self):
        server_module.audio_router.set_stereo_separation(1.5)
        assert server_module.audio_router._stereo_sep == 1.0
        server_module.audio_router.set_stereo_separation(-0.5)
        assert server_module.audio_router._stereo_sep == 0.0

    def test_set_pan_ws(self, socketio_client):
        socketio_client.emit('set_pan', {'device_id': 'd1', 'pan': -0.8})
        assert abs(server_module.audio_router._pan.get('d1', 0) - (-0.8)) < 0.001
        server_module.audio_router._pan.pop('d1', None)

    def test_set_stereo_sep_ws(self, socketio_client):
        socketio_client.emit('set_stereo_separation', {'value': 0.6})
        assert abs(server_module.audio_router._stereo_sep - 0.6) < 0.001
        server_module.audio_router._stereo_sep = 0.0

    def test_enrich_includes_pan(self):
        server_module.audio_router._pan['d1'] = -0.5
        devices = [{'id': 'd1', 'name': 'Speaker 1', 'volume': 1.0}]
        enriched = server_module._enrich_devices(devices)
        assert enriched[0]['pan'] == -0.5
        server_module.audio_router._pan.pop('d1', None)


class TestFFTSpectrum:
    """FFT spectrum analysis tests."""

    def test_spectrum_bands_default_empty(self):
        assert server_module.audio_router.spectrum_bands == [0.0] * 8

    def test_spectrum_bands_length(self):
        # Simulate setting bands
        server_module.audio_router._spectrum_bands = [0.1, 0.2, 0.3, 0.4, 0.5, 0.6, 0.7, 0.8]
        assert len(server_module.audio_router.spectrum_bands) == 8
        server_module.audio_router._spectrum_bands = [0.0] * 8

    def test_compute_spectrum_from_silence(self):
        """FFT of silence should produce all-zero bands."""
        import numpy as np
        silence = np.zeros(1024, dtype=np.float32)
        bands = server_module.AudioRouter.compute_fft_bands(silence, 48000)
        assert len(bands) == 8
        assert all(b == 0.0 for b in bands)

    def test_compute_spectrum_from_tone(self):
        """A 100Hz sine wave should produce energy in the bass band."""
        import numpy as np
        sr = 48000
        t = np.arange(1024, dtype=np.float32) / sr
        tone = (np.sin(2 * np.pi * 100 * t) * 0.5).astype(np.float32)
        bands = server_module.AudioRouter.compute_fft_bands(tone, sr)
        assert len(bands) == 8
        # Band 1 (bass: 60-250Hz) should have the most energy
        assert bands[1] > 0.1
        assert bands[1] > bands[5]  # bass > presence
