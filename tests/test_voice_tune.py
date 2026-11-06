"""Reaper's voice v2 tuning script (tools/voice_tune.py): the robot chain on a made-up tone, no voice model needed.
Skipped unless the optional voice extras are installed (`pip install -e ".[voice]"`)."""
import importlib.util
import sys
from pathlib import Path

import pytest

np = pytest.importorskip("numpy")
pytest.importorskip("scipy")

spec = importlib.util.spec_from_file_location("voice_tune", Path(__file__).resolve().parent.parent / "tools" / "voice_tune.py")
vt = importlib.util.module_from_spec(spec)
sys.modules["voice_tune"] = vt
spec.loader.exec_module(vt)


def tone(seconds, rate=24000, hz=140.0):
    t = np.arange(int(seconds * rate)) / rate
    return 0.5 * np.sin(2 * np.pi * hz * t) * np.hanning(len(t))


def test_the_robot_chain_is_clean_and_lowers_the_pitch():
    s = vt.options([])
    out, rate = vt.robot([tone(1.0), tone(0.8)], 24000, s)
    assert rate == vt.OUT_RATE
    assert np.max(np.abs(out)) == pytest.approx(10 ** (-1 / 20), rel=1e-3)   # peaks at -1 dB, never clips
    # Deeper and slower by the pitch factor, plus one pause between the two phrases.
    assert len(out) / rate > (1.8 / s.pitch) * 0.85


def test_static_is_only_in_the_pauses():
    s = vt.options(["--metal", "0", "--machine", "0", "--crush", "0", "--rasp", "0", "--static", "1", "--pause", "0.5"])
    out, rate = vt.robot([tone(1.0), tone(1.0)], 24000, s)
    pause = out[int(rate * 1.2 / s.pitch): int(rate * 1.4 / s.pitch)]
    assert 0 < np.sqrt(np.mean(pause ** 2)) < 0.2   # some static in the pause, quiet
    s.static = 0
    quiet, _ = vt.robot([tone(1.0), tone(1.0)], 24000, s)
    assert np.sqrt(np.mean(quiet[int(rate * 1.2 / s.pitch): int(rate * 1.4 / s.pitch)] ** 2)) < 1e-3


def test_the_line_is_cut_into_phrases_for_clipped_delivery():
    assert vt.phrases(vt.LINE) == ["Status report.", "Four new businesses are waiting for your decision.",
                                   "Never send an organic to do a machine's work."]


def test_rasp_adds_grit_in_the_upper_voice_band():
    from scipy import signal
    def upper_share(rasp):
        s = vt.options(["--rasp", str(rasp), "--static", "0", "--crush", "0"])
        out, rate = vt.robot([tone(1.0)], 24000, s)
        hi = signal.sosfilt(signal.butter(4, [2000, 5000], btype="band", fs=rate, output="sos"), out)
        return float(np.sqrt(np.mean(hi ** 2)) / np.sqrt(np.mean(out ** 2)))
    assert upper_share(0.8) > upper_share(0.0) * 1.5


def test_saved_settings_round_trip_and_unknown_keys_are_ignored(tmp_path):
    from engine import voice
    path = voice.save_settings(voice.Settings(voice="am_onyx", rasp=0.9), tmp_path / "v.json")
    assert voice.load_settings(path).rasp == 0.9 and voice.load_settings(path).voice == "am_onyx"
    path.write_text('{"rasp": 0.3, "something_else": 1}', encoding="utf-8")
    assert voice.load_settings(path).rasp == 0.3
    assert voice.load_settings(tmp_path / "missing.json") == voice.REAPER
