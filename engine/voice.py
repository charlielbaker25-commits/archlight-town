"""Reaper's voice: an original robot voice, made locally on Charlie's laptop (decisions log 2026-10-27/28).

Kokoro (a free, open voice model run locally through the kokoro-onnx package) reads each phrase in one of its own
stock voices. Then plain Python (numpy and scipy) turns it into a machine:
1. Clipped delivery: each phrase is read on its own, trimmed tight, with short pauses between phrases.
2. Pitch and formants down: played back slower (deeper and bigger), read a little faster first so timing stays.
3. Ring modulation (the metallic edge): the voice multiplied by a low hum.
4. A short comb resonance ("inside a machine"): a few milliseconds of echo feeding back, like a metal box.
5. Rasp: a gravelly flutter (vocal fry) and breathy grit that follow the voice, plus soft overdrive.
6. Radio band: the very low and very high ends trimmed.
7. Light bitcrush: fewer loudness steps and a slightly lower sample rate.
8. Gated radio static: a little filtered noise only in the pauses, never under the words.
It never uses or imitates the game's audio or any actor's voice (BLUEPRINT 8.5): Kokoro's stock voices and maths.

Reaper HQ (`town hq`) calls `speak()` for every line Reaper says on the page. If the optional voice packages or
Kokoro's model files aren't installed, `available()` says why and the page falls back to the browser's own voice.
Charlie's tuned settings live in data/voice/reaper-voice.json (written by `python tools/voice_tune.py ... --save`);
without that file, REAPER below is used.
"""
import io
import json
import re
import threading
import wave
from dataclasses import asdict, dataclass, fields, replace
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
MODEL_DIR = ROOT / "data" / "voice"
SETTINGS_FILE = MODEL_DIR / "reaper-voice.json"
MODEL_FILES = {   # Kokoro v1.0, from the kokoro-onnx project's GitHub releases (model-files-v1.0)
    "kokoro-v1.0.onnx": "7d5df8ecf7d4b187" "8015a32686053fd0" "eebe2bc377234608" "764cc0ef3636a6c5",
    "voices-v1.0.bin": "bca610b8308e8d99" "f32e6fe4197e7ec0" "1679264efed0cac9" "140fe9c29f1fbf7d",
}
MODEL_URL = "https://github.com/thewh1teagle/kokoro-onnx/releases/download/model-files-v1.0/"
OUT_RATE = 22050      # plenty for a voice, and keeps the audio small
MAX_CHARS = 600       # one request speaks at most this much (the page sends a sentence or two at a time)
VOICE_THREADS = 2     # CPU threads the voice may use (the rest stay free for the browser)
WARM_DELAY_SECONDS = 20   # HQ loads the voice this long after it starts, once the page has finished loading


@dataclass
class Settings:
    voice: str = "am_onyx"
    speed: float = 1.0      # speaking speed (1.0 = Kokoro's normal)
    pitch: float = 0.86     # pitch and formants (1.0 = unchanged; lower = deeper)
    metal: float = 0.45     # ring modulation, 0 to 1
    ring_hz: float = 72.0   # the ring modulator's hum
    machine: float = 0.35   # comb resonance, 0 to 1
    comb_ms: float = 5.5    # the resonance's delay
    rasp: float = 0.0       # gravel, breath and overdrive, 0 to 1
    crush: float = 0.15     # bitcrush, 0 to 1
    static: float = 0.2     # radio static in the pauses, 0 to 1
    pause: float = 0.16     # seconds between phrases
    low_hz: float = 110.0   # radio band, lowest frequency kept
    high_hz: float = 6000.0  # radio band, highest frequency kept
    seed: int = 7           # the static's random seed


# Charlie's pick (2026-10-28): sample 6 (bm_george, heavy), made raspier.
REAPER = Settings(voice="bm_george", pitch=0.82, metal=0.55, machine=0.6, comb_ms=4.0, crush=0.2, static=0.25,
                  rasp=0.55)


def load_settings(path: Path = SETTINGS_FILE) -> Settings:
    """Charlie's saved settings if there are any (unknown keys ignored), else Reaper's defaults."""
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return REAPER
    known = {f.name for f in fields(Settings)}
    return replace(REAPER, **{k: v for k, v in raw.items() if k in known})


def save_settings(s: Settings, path: Path = SETTINGS_FILE) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(asdict(s), indent=2), encoding="utf-8")
    return path


def phrases(text: str) -> list[str]:
    """The line cut into short phrases at full stops, question marks and commas (clipped delivery)."""
    return [p.strip() for p in re.split(r"(?<=[.!?,;:])\s+", text) if p.strip()]


def sentences(text: str) -> list[str]:
    """Whole sentences, as the page sends them (one at a time, so Reaper starts talking sooner)."""
    return [p.strip() for p in re.split(r"(?<=[.!?])\s+", " ".join(str(text).split())) if p.strip()]


def trim(x: Any, rate: int, floor_db: float = -42.0) -> Any:
    """Cut the quiet start and end of a phrase, keeping 25 ms either side so words don't click."""
    import numpy as np
    if not len(x):
        return x
    level = np.abs(x) / (np.max(np.abs(x)) + 1e-9)
    loud = np.where(level > 10 ** (floor_db / 20))[0]
    if not len(loud):
        return x[:0]
    pad = int(0.025 * rate)
    return x[max(0, loud[0] - pad): loud[-1] + pad]


def robot(voice_parts: list[Any], rate: int, s: Any) -> tuple[Any, int]:
    """The machine chain. `voice_parts` are the phrases as Kokoro read them; returns (samples, sample rate)."""
    import numpy as np
    from scipy import signal

    rng = np.random.default_rng(s.seed)
    # 2. Pitch and formants down: resample so that played back at `rate` it is `pitch` times lower.
    lowered = [signal.resample_poly(trim(p, rate), int(round(1000 / s.pitch)), 1000) for p in voice_parts]
    gap = int(s.pause * rate)
    pieces, gates = [], []
    for i, part in enumerate(lowered):
        pieces.append(part)
        gates.append(np.zeros(len(part)))
        if i < len(lowered) - 1:
            pieces.append(np.zeros(gap))
            gates.append(np.ones(gap))
    x = np.concatenate(pieces).astype(np.float64) if pieces else np.zeros(1)
    gate = np.concatenate(gates) if gates else np.zeros(1)
    t = np.arange(len(x)) / rate

    # 3. Ring modulation: mixed in by `metal` (0 = none, 1 = fully modulated).
    if s.metal > 0:
        x = x * ((1 - s.metal) + s.metal * np.sin(2 * np.pi * s.ring_hz * t))
    # 4. Comb resonance: a short feedback echo (a few milliseconds) mixed in by `machine`.
    if s.machine > 0:
        d = max(1, int(s.comb_ms / 1000 * rate))
        feedback = np.zeros(d + 1)
        feedback[0], feedback[d] = 1.0, -s.machine * 0.6   # y[n] = x[n] + 0.6 * machine * y[n - d]
        y = signal.lfilter([1.0], feedback, x)
        x = 0.6 * x + 0.4 * y / (1 + s.machine)
    # 5. Rasp. The voice's loudness (its envelope) shapes everything here, so the grit sits only under words.
    rasp = getattr(s, "rasp", 0.0)
    if rasp > 0:
        peak = np.max(np.abs(x)) + 1e-9
        env = signal.sosfiltfilt(signal.butter(2, 25, fs=rate, output="sos"), np.abs(x))
        env = np.clip(env / (np.max(env) + 1e-9), 0, 1)
        # Vocal fry: an uneven flutter around 38 Hz (its speed wobbles, like a rough throat).
        wobble = signal.sosfiltfilt(signal.butter(1, 6, fs=rate, output="sos"), rng.standard_normal(len(x)))
        phase = np.cumsum(2 * np.pi * 38 * (1 + 0.6 * wobble / (np.std(wobble) + 1e-9) * 0.25) / rate)
        fry = np.abs(np.sin(phase)) ** 2.5
        x = x * ((1 - 0.45 * rasp) + 0.45 * rasp * fry)
        # Breath and grit: noise in the upper voice band, riding the envelope.
        grit = signal.sosfilt(signal.butter(2, [1800, 5200], btype="band", fs=rate, output="sos"),
                              rng.standard_normal(len(x)))
        x = x + grit / (np.max(np.abs(grit)) + 1e-9) * env * peak * 0.22 * rasp
        # Soft overdrive: rounds the peaks off, which adds a hoarse edge.
        drive = 1 + 5 * rasp
        x = np.tanh(drive * x / peak) / np.tanh(drive) * peak
    # 6. Radio band: trim the rumble and the hiss.
    sos = signal.butter(4, [s.low_hz, min(s.high_hz, rate / 2 - 100)], btype="band", fs=rate, output="sos")
    x = signal.sosfilt(sos, x)
    # 7. Bitcrush: hold every other sample and round to fewer loudness steps, mixed in by `crush`.
    if s.crush > 0:
        held = np.repeat(x[::2], 2)[: len(x)]
        steps = 2 ** max(4, int(16 - 8 * s.crush))
        peak = np.max(np.abs(held)) + 1e-9
        crushed = np.round(held / peak * steps) / steps * peak
        x = (1 - s.crush) * x + s.crush * crushed
    # 8. Static, only in the pauses (a soft fade in and out so it never clicks).
    if s.static > 0:
        noise = signal.sosfilt(signal.butter(2, [900, 4200], btype="band", fs=rate, output="sos"),
                               rng.standard_normal(len(x)))
        crackle = (rng.random(len(x)) < 0.002) * rng.standard_normal(len(x)) * 3
        width = max(1, int(0.02 * rate))
        fade = np.convolve(gate, np.ones(width) / width, mode="same")
        x = x + (noise * 0.06 + crackle * 0.05) * s.static * fade * (np.max(np.abs(x)) + 1e-9)
    # Level: peaks at -1 dB, and the result in the output sample rate.
    x = signal.resample_poly(x, OUT_RATE, rate)
    return x / (np.max(np.abs(x)) + 1e-9) * 10 ** (-1 / 20), OUT_RATE


def wav_bytes(samples: Any, rate: int) -> bytes:
    import numpy as np
    out = io.BytesIO()
    with wave.open(out, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes((np.clip(samples, -1, 1) * 32767).astype("<i2").tobytes())
    return out.getvalue()


class Voice:
    """Kokoro loaded once (it takes a few seconds) and shared; one line is made at a time. A small cache keeps
    lines Reaper says often (the stand-down line, the greeting) instant."""

    def __init__(self, model_dir: Path = MODEL_DIR, settings: Settings | None = None) -> None:
        self.model_dir = model_dir
        self.settings = settings or load_settings()
        self._engine: Any = None
        self._lock = threading.Lock()
        self._cache: dict[str, bytes] = {}

    def available(self) -> str | None:
        """None when Reaper's voice can be made here; otherwise a plain reason (and the page uses the browser's
        own voice instead)."""
        try:
            import kokoro_onnx  # noqa: F401
            import scipy  # noqa: F401
        except ImportError:
            return 'the voice packages aren\'t installed (pip install -e ".[voice]")'
        missing = [n for n in MODEL_FILES if not (self.model_dir / n).is_file()]
        if missing:
            return "Kokoro's model files aren't downloaded (python tools/voice_tune.py --download)"
        return None

    def engine(self) -> Any:
        if self._engine is None:
            import onnxruntime as rt
            from kokoro_onnx import Kokoro
            # At most 2 CPU threads, so making Reaper's voice never starves the browser drawing the village
            # (by default it would grab every core for a second or two per sentence).
            options = rt.SessionOptions()
            options.intra_op_num_threads = VOICE_THREADS
            options.inter_op_num_threads = 1
            session = rt.InferenceSession(str(self.model_dir / "kokoro-v1.0.onnx"), sess_options=options,
                                          providers=["CPUExecutionProvider"])
            self._engine = Kokoro.from_session(session, str(self.model_dir / "voices-v1.0.bin"))
        return self._engine

    def warm(self, *lines: str, delay: float = 0.0) -> None:
        """Load Kokoro and make a few lines ahead of time (run in the background when HQ starts, after `delay`
        seconds so the page loads first)."""
        import time
        time.sleep(delay)
        try:
            for line in lines:
                for sentence in sentences(line):
                    self.speak(sentence)
        except Exception:
            pass   # the first real line will try again, and say so if it fails

    def speak(self, text: str, s: Settings | None = None) -> bytes:
        """`text` in Reaper's voice, as WAV bytes."""
        import numpy as np
        s = s or self.settings
        text = " ".join(str(text).split())[:MAX_CHARS]
        key = json.dumps([text, asdict(s)])
        with self._lock:
            if key in self._cache:
                return self._cache[key]
            parts, rate = [], 24000
            for phrase in phrases(text) or ["."]:
                # Read a little faster, by the amount the pitch drop slows it, so the timing stays natural.
                audio, rate = self.engine().create(phrase, voice=s.voice, speed=min(2.0, s.speed / s.pitch),
                                                   lang="en-us")
                parts.append(np.asarray(audio, dtype=np.float64))
            data = wav_bytes(*robot(parts, rate, s))
            if len(self._cache) > 40:
                self._cache.pop(next(iter(self._cache)))
            self._cache[key] = data
            return data
