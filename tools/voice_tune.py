"""Tune Reaper's voice by ear: writes one sample with the settings you give, and can save them for Reaper HQ.

The voice itself lives in engine/voice.py (Kokoro, read locally, then a robot chain in plain numpy/scipy). Reaper HQ
(`town hq`) speaks with the settings saved in data/voice/reaper-voice.json, or Reaper's defaults when there are none.

Once per laptop (inside the project's virtual environment):
    pip install -e ".[voice]"
    python tools/voice_tune.py --download          (fetches Kokoro's two model files into data\\voice, ~350 MB)
Then tune, one sample at a time (every setting starts from Reaper's current voice):
    python tools/voice_tune.py --rasp 0.7 --out my-try.wav
    python tools/voice_tune.py --rasp 0.7 --save     (Reaper HQ uses these settings from its next launch)
    python tools/voice_tune.py --list-voices
`--help` lists every setting. The sample is a WAV file (open it in any player).
"""
import argparse
import hashlib
import sys
import urllib.request
from dataclasses import fields, replace
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from engine.voice import (MODEL_DIR, MODEL_FILES, MODEL_URL, OUT_RATE, Settings, Voice,  # noqa: E402,F401
                          load_settings, phrases, robot, save_settings)

LINE = ("Status report. Four new businesses are waiting for your decision. "
        "Never send an organic to do a machine's work.")


def download() -> None:
    """Fetch the two model files once, and check each against its known fingerprint (SHA-256)."""
    MODEL_DIR.mkdir(parents=True, exist_ok=True)
    for name, digest in MODEL_FILES.items():
        target = MODEL_DIR / name
        if not target.is_file():
            print(f"Downloading {name} ...")
            urllib.request.urlretrieve(MODEL_URL + name, target)
        h = hashlib.sha256()
        with target.open("rb") as f:
            for block in iter(lambda: f.read(1 << 20), b""):
                h.update(block)
        if h.hexdigest() != digest:
            target.unlink()
            sys.exit(f"{name} didn't match its fingerprint; deleted it. Try --download again.")
        print(f"{name}: OK")


def settings_from(s: argparse.Namespace) -> Settings:
    return Settings(**{f.name: getattr(s, f.name) for f in fields(Settings)})


def make(s: argparse.Namespace, voice: Voice | None = None) -> Path:
    voice = voice or Voice()
    reason = voice.available()
    if reason:
        sys.exit(f"Can't make a sample: {reason}.")
    out = Path(s.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_bytes(voice.speak(s.text, settings_from(s)))
    return out


def options(argv: list[str] | None = None) -> argparse.Namespace:
    base = load_settings()   # start from Reaper's current voice (saved settings, or the defaults)
    p = argparse.ArgumentParser(description="Make one sample of Reaper's voice, to tune by ear.")
    p.add_argument("--voice", default=base.voice, help="Kokoro voice (deep ones: bm_george, am_onyx, am_fenrir)")
    p.add_argument("--text", default=LINE, help="what Reaper says")
    p.add_argument("--speed", type=float, default=base.speed, help="speaking speed (1.0 = Kokoro's normal)")
    p.add_argument("--pitch", type=float, default=base.pitch, help="pitch and formants (1.0 = unchanged; lower = deeper)")
    p.add_argument("--metal", type=float, default=base.metal, help="ring modulation, 0 to 1 (the metallic edge)")
    p.add_argument("--ring-hz", type=float, default=base.ring_hz, help="the ring modulator's hum, in Hz")
    p.add_argument("--machine", type=float, default=base.machine, help="comb resonance, 0 to 1 (inside a machine)")
    p.add_argument("--comb-ms", type=float, default=base.comb_ms, help="the resonance's delay, in milliseconds")
    p.add_argument("--rasp", type=float, default=base.rasp, help="gravel, breath and overdrive, 0 to 1 (raspy)")
    p.add_argument("--crush", type=float, default=base.crush, help="bitcrush, 0 to 1 (digital grit)")
    p.add_argument("--static", type=float, default=base.static, help="radio static in the pauses, 0 to 1")
    p.add_argument("--pause", type=float, default=base.pause, help="pause between phrases, in seconds")
    p.add_argument("--low-hz", type=float, default=base.low_hz, help="radio band: the lowest frequency kept")
    p.add_argument("--high-hz", type=float, default=base.high_hz, help="radio band: the highest frequency kept")
    p.add_argument("--seed", type=int, default=base.seed, help="the static's random seed (same seed = same static)")
    p.add_argument("--out", default="reaper-voice.wav", help="the WAV file to write")
    p.add_argument("--save", action="store_true", help="also save these settings as Reaper's voice in Reaper HQ")
    p.add_argument("--download", action="store_true", help="fetch Kokoro's model files into data/voice and stop")
    p.add_argument("--list-voices", action="store_true", help="list Kokoro's English voices and stop")
    return p.parse_args(argv)


def main(argv: list[str] | None = None) -> None:
    s = options(argv)
    if s.download:
        download()
        return
    if s.list_voices:
        import numpy as np
        names = np.load(MODEL_DIR / "voices-v1.0.bin").files
        print("\n".join(sorted(n for n in names if n[:2] in ("am", "bm", "af", "bf"))))
        return
    print(f"Wrote {make(s)}")
    if s.save:
        print(f"Saved as Reaper's voice: {save_settings(settings_from(s))} (restart `town hq` to hear it)")


if __name__ == "__main__":
    main()
