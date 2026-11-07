"""
ffkit core: probing, command execution, hardware detection, verification.

Rules this module enforces (see AGENTS.md):
  * Stream copy is the default. Re-encoding happens only where the output must
    differ from the input in content, and only once per pipeline.
  * Probe before acting. Decisions (copy vs encode, keyframe vs select) are made
    from ffprobe data, not assumptions.
  * Every command is printable before it runs (dry-run) and logged after.
  * Every output is verified with ffprobe before a step is called done.
"""
from __future__ import annotations

import json
import os
import platform
import shlex
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass, field, asdict
from fractions import Fraction
from pathlib import Path
from typing import Iterable, Optional

FFMPEG = shutil.which("ffmpeg") or "ffmpeg"
FFPROBE = shutil.which("ffprobe") or "ffprobe"

LOG_DIR = Path(os.environ.get("FFKIT_LOG_DIR", "ffkit-logs"))


class FFKitError(RuntimeError):
    pass


# --------------------------------------------------------------------------
# Logging / execution
# --------------------------------------------------------------------------

class Runner:
    """Runs commands, honouring dry-run, and writes a per-job log."""

    def __init__(self, dry_run: bool = False, verbose: bool = False, job_name: str = "ffkit"):
        self.dry_run = dry_run
        self.verbose = verbose
        self.job_name = job_name
        self.started = time.time()
        self.entries: list[dict] = []
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        stamp = time.strftime("%Y%m%d-%H%M%S")
        self.log_path = LOG_DIR / f"{stamp}-{job_name}.log"

    def say(self, msg: str) -> None:
        print(msg, file=sys.stderr)
        self._append(msg)

    def _append(self, text: str) -> None:
        with self.log_path.open("a") as fh:
            fh.write(text.rstrip("\n") + "\n")

    def run(self, cmd: list[str], *, why: str = "", check: bool = True) -> subprocess.CompletedProcess | None:
        pretty = " ".join(shlex.quote(c) for c in cmd)
        prefix = "[dry-run] " if self.dry_run else "$ "
        if why:
            self.say(f"# {why}")
        self.say(prefix + pretty)
        if self.dry_run:
            return None
        t0 = time.time()
        proc = subprocess.run(cmd, capture_output=True, text=True)
        dt = time.time() - t0
        self._append(f"  exit={proc.returncode} time={dt:.1f}s")
        if self.verbose or proc.returncode != 0:
            tail = "\n".join(proc.stderr.strip().splitlines()[-25:])
            self._append(tail)
            if proc.returncode != 0:
                print(tail, file=sys.stderr)
        if check and proc.returncode != 0:
            raise FFKitError(f"command failed (exit {proc.returncode}): {pretty}")
        return proc


# --------------------------------------------------------------------------
# Probing
# --------------------------------------------------------------------------

@dataclass
class VideoInfo:
    path: str
    container: str = ""
    duration: float = 0.0
    size_bytes: int = 0
    # video
    vcodec: str = ""
    width: int = 0
    height: int = 0
    pix_fmt: str = ""
    fps: float = 0.0
    fps_str: str = ""
    time_base: str = ""
    nb_frames: int = 0
    gop: int = 0            # median keyframe spacing in frames (0 = unknown)
    vfr: bool = False
    rotation: int = 0
    # audio
    acodec: str = ""
    sample_rate: int = 0
    channels: int = 0
    has_audio: bool = False
    has_video: bool = False
    notes: list[str] = field(default_factory=list)

    def signature(self) -> tuple:
        """What must match for a lossless concat."""
        return (self.vcodec, self.width, self.height, self.pix_fmt, round(self.fps, 3),
                self.acodec, self.sample_rate, self.channels)

    def summary(self) -> str:
        v = f"{self.vcodec} {self.width}x{self.height} {self.fps_str} {self.pix_fmt}" if self.has_video else "no video"
        a = f"{self.acodec} {self.sample_rate}Hz {self.channels}ch" if self.has_audio else "no audio"
        extra = []
        if self.gop:
            extra.append(f"gop={self.gop}")
        if self.vfr:
            extra.append("VFR")
        if self.rotation:
            extra.append(f"rot={self.rotation}")
        ex = (" [" + ", ".join(extra) + "]") if extra else ""
        return f"{Path(self.path).name}: {self.duration:.2f}s  {v}  |  {a}{ex}"


def _ffprobe_json(args: list[str]) -> dict:
    cmd = [FFPROBE, "-v", "error", "-print_format", "json"] + args
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0:
        raise FFKitError(f"ffprobe failed on {args[-1]}: {proc.stderr.strip()}")
    return json.loads(proc.stdout or "{}")


def _parse_rate(s: str) -> float:
    try:
        return float(Fraction(s)) if "/" in s else float(s)
    except (ValueError, ZeroDivisionError):
        return 0.0


def probe(path: str | Path, *, gop: bool = True, max_gop_frames: int = 600) -> VideoInfo:
    """Probe a file. gop=True also measures keyframe spacing on the first few hundred frames."""
    p = str(path)
    if not Path(p).exists():
        raise FFKitError(f"no such file: {p}")
    data = _ffprobe_json(["-show_format", "-show_streams", p])
    info = VideoInfo(path=p)
    fmt = data.get("format", {})
    info.container = fmt.get("format_name", "")
    info.duration = float(fmt.get("duration") or 0)
    info.size_bytes = int(fmt.get("size") or 0)

    for s in data.get("streams", []):
        if s.get("codec_type") == "video" and not info.has_video:
            info.has_video = True
            info.vcodec = s.get("codec_name", "")
            info.width = int(s.get("width") or 0)
            info.height = int(s.get("height") or 0)
            info.pix_fmt = s.get("pix_fmt", "")
            info.fps_str = s.get("avg_frame_rate", "")
            info.fps = _parse_rate(info.fps_str)
            r = _parse_rate(s.get("r_frame_rate", ""))
            info.time_base = s.get("time_base", "")
            info.nb_frames = int(s.get("nb_frames") or 0)
            # avg vs r frame rate disagreeing is the usual VFR tell
            if info.fps and r and abs(info.fps - r) / r > 0.02:
                info.vfr = True
            for sd in s.get("side_data_list", []) or []:
                if "rotation" in sd:
                    info.rotation = int(sd["rotation"]) % 360
            tags = s.get("tags", {}) or {}
            if "rotate" in tags:
                info.rotation = int(tags["rotate"]) % 360
        elif s.get("codec_type") == "audio" and not info.has_audio:
            info.has_audio = True
            info.acodec = s.get("codec_name", "")
            info.sample_rate = int(s.get("sample_rate") or 0)
            info.channels = int(s.get("channels") or 0)

    if gop and info.has_video:
        info.gop = measure_gop(p, max_frames=max_gop_frames)
        if info.nb_frames == 0 and info.fps and info.duration:
            info.nb_frames = int(round(info.fps * info.duration))
    if info.vfr:
        info.notes.append("variable frame rate: normalise to CFR before concat/timelapse")
    return info


def measure_gop(path: str, max_frames: int = 600) -> int:
    """Median spacing between keyframes over the first max_frames frames (0 if unknown)."""
    cmd = [FFPROBE, "-v", "error", "-select_streams", "v:0",
           "-show_entries", "frame=key_frame", "-read_intervals", f"%+#{max_frames}",
           "-of", "csv=p=0", path]
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0:
        return 0
    keys = [i for i, line in enumerate(proc.stdout.split()) if line.strip() == "1"]
    if len(keys) < 2:
        return 0
    gaps = sorted(b - a for a, b in zip(keys, keys[1:]))
    return gaps[len(gaps) // 2]


def probe_many(paths: Iterable[str | Path], **kw) -> list[VideoInfo]:
    return [probe(p, **kw) for p in paths]


# --------------------------------------------------------------------------
# Hardware / encoder detection
# --------------------------------------------------------------------------

_caps_cache: dict | None = None


def capabilities() -> dict:
    """What this ffmpeg build can do. Cached per process."""
    global _caps_cache
    if _caps_cache is not None:
        return _caps_cache
    encs = subprocess.run([FFMPEG, "-v", "error", "-encoders"], capture_output=True, text=True).stdout
    hw = subprocess.run([FFMPEG, "-v", "error", "-hwaccels"], capture_output=True, text=True).stdout

    def works(encoder: str) -> bool:
        """A listed hardware encoder is not a working one; try a one-frame encode."""
        if encoder not in encs:
            return False
        proc = subprocess.run([FFMPEG, "-v", "error", "-f", "lavfi", "-i", "color=c=black:s=64x64:r=1",
                               "-frames:v", "1", "-c:v", encoder, "-f", "null", "-"],
                              capture_output=True, text=True, timeout=30)
        return proc.returncode == 0

    caps = {
        "platform": platform.system(),
        "videotoolbox": "videotoolbox" in hw and works("h264_videotoolbox"),
        "nvenc": works("h264_nvenc"),
        "qsv": works("h264_qsv"),
        "vaapi": "vaapi" in hw,
        "libx264": "libx264" in encs,
        "libx265": "libx265" in encs,
        "prores_ks": "prores_ks" in encs,
        "prores_videotoolbox": works("prores_videotoolbox"),
    }
    _caps_cache = caps
    return caps


def hwaccel_input_args() -> list[str]:
    """Decoder-side hardware acceleration flags, placed before -i."""
    c = capabilities()
    if c["videotoolbox"]:
        return ["-hwaccel", "videotoolbox"]
    if c["nvenc"]:
        return ["-hwaccel", "cuda"]
    return []


def video_encoder_args(kind: str = "h264", quality: str = "mezzanine") -> list[str]:
    """
    Encoder flags for the one encode a pipeline is allowed.
      kind:    h264 | hevc | prores
      quality: mezzanine (edit-friendly, high bitrate) | delivery (small, compatible)
    Prefers the hardware encoder when present; falls back to software.
    """
    c = capabilities()
    if kind == "prores":
        if c["prores_videotoolbox"]:
            return ["-c:v", "prores_videotoolbox", "-profile:v", "2", "-pix_fmt", "yuv422p10le"]
        return ["-c:v", "prores_ks", "-profile:v", "2", "-pix_fmt", "yuv422p10le"]
    if kind == "hevc":
        if c["videotoolbox"]:
            q = ["-q:v", "70"] if quality == "mezzanine" else ["-q:v", "55"]
            return ["-c:v", "hevc_videotoolbox", "-tag:v", "hvc1", "-pix_fmt", "yuv420p"] + q
        crf = "18" if quality == "mezzanine" else "26"
        return ["-c:v", "libx265", "-preset", "medium", "-crf", crf, "-tag:v", "hvc1", "-pix_fmt", "yuv420p"]
    # h264
    if c["videotoolbox"]:
        q = ["-q:v", "75"] if quality == "mezzanine" else ["-q:v", "60"]
        return ["-c:v", "h264_videotoolbox", "-pix_fmt", "yuv420p"] + q
    if c["nvenc"]:
        cq = "19" if quality == "mezzanine" else "26"
        return ["-c:v", "h264_nvenc", "-preset", "p5", "-rc", "vbr", "-cq", cq, "-pix_fmt", "yuv420p"]
    crf = "18" if quality == "mezzanine" else "23"
    return ["-c:v", "libx264", "-preset", "medium", "-crf", crf, "-pix_fmt", "yuv420p"]


def container_args(out: str | Path) -> list[str]:
    """Muxer flags that cost nothing and help playback."""
    ext = Path(out).suffix.lower()
    if ext in {".mp4", ".m4v", ".mov"}:
        return ["-movflags", "+faststart"]
    return []


# --------------------------------------------------------------------------
# Verification
# --------------------------------------------------------------------------

@dataclass
class Verdict:
    ok: bool
    messages: list[str]
    info: Optional[VideoInfo] = None


def verify(out: str | Path, *, expect_duration: float | None = None,
           expect_video: bool = True, expect_audio: bool | None = None,
           tolerance: float = 0.75, expect_frames: int | None = None) -> Verdict:
    """Probe an output and check it is what the step promised."""
    msgs: list[str] = []
    p = Path(out)
    if not p.exists() or p.stat().st_size == 0:
        return Verdict(False, [f"missing or empty: {p}"])
    try:
        info = probe(p, gop=False)
    except FFKitError as e:
        return Verdict(False, [str(e)])
    if expect_video and not info.has_video:
        msgs.append("no video stream")
    if expect_audio is True and not info.has_audio:
        msgs.append("expected audio, found none")
    if expect_audio is False and info.has_audio:
        msgs.append("expected no audio, found an audio stream")
    if expect_duration is not None:
        diff = abs(info.duration - expect_duration)
        if diff > tolerance:
            msgs.append(f"duration {info.duration:.2f}s differs from expected {expect_duration:.2f}s by {diff:.2f}s")
    if expect_frames is not None and info.nb_frames:
        if abs(info.nb_frames - expect_frames) > max(2, expect_frames * 0.02):
            msgs.append(f"frame count {info.nb_frames} differs from expected {expect_frames}")
    return Verdict(not msgs, msgs, info)


def to_json(objs) -> str:
    if isinstance(objs, list):
        return json.dumps([asdict(o) for o in objs], indent=2)
    return json.dumps(asdict(objs), indent=2)
