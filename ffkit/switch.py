"""
ffkit switch — live-switch a multi-camera recording after the fact.

The person does the one thing software can't: decide which camera, in the moment,
by ear and eye. The computer does everything else.

    ffkit switch init                       write switch.yaml to fill in
    ffkit switch sync                       work out each camera's offset from its audio
    ffkit switch grid                       build the side-by-side proxy you watch while switching
    ffkit switch mark [--from T]            open the grid in mpv; press 1/2/3 as the show plays
    ffkit switch status                     how much is marked, long holds, camera share
    ffkit switch preview [--from T --to T]  fast low-res render of what you just marked
    ffkit switch render                     frame-accurate final with transitions, intro in front

The log (switch.log) is plain text: one `seconds,camera` line per press. Anything can
write it. To redo a stretch, run `mark --from T` and press keys again; the newer marks
replace the old ones in the range you re-marked.
"""
from __future__ import annotations

import json
import math
import os
import platform
import shutil
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from .core import (FFMPEG, FFKitError, Runner, VideoInfo, probe, verify,
                   hwaccel_input_args, video_encoder_args, container_args)
from .runner import load_job
from .ops import _parse_time

LUA = Path(__file__).with_name("switch.lua")

TEMPLATE = """# ffkit switch — fill in the three camera files and you're set.
# Paths are relative to this file. Keys 1-9 are what you press while switching.
cameras:
  1: {file: cams/main.mp4,  name: main,  offset: 0}
  2: {file: cams/front.mp4, name: front, offset: 0}
  3: {file: cams/side.mp4,  name: side,  offset: 0}
# offset = seconds into that file that line up with show time 0 (the audio camera's start).
#   Positive: that camera was already recording before the audio camera started.
#   Negative: that camera started late. `ffkit switch sync` works these out from the audio
#   and writes switch.offsets.json, which overrides the numbers above.

audio: 1              # the camera whose sound track is used for the whole show
fallback: 1           # camera used if the one you pressed has no footage at that moment
intro: ""             # optional clip to put in front (the birthday clip)
output: ~/Desktop/show.mp4

transition: 0.5       # seconds of dissolve at each cut; 0 = hard cuts
# fps: 30             # default: the audio camera's rate
# width: 1920
# height: 1080
workers: 4            # parallel encodes
"""


# --------------------------------------------------------------------------
# config
# --------------------------------------------------------------------------

@dataclass
class Cam:
    key: str
    file: str
    offset: float = 0.0
    name: str = ""
    info: Optional[VideoInfo] = None

    def src(self, t: float) -> float:
        """show time -> time in this camera's file"""
        return t + self.offset

    def covers(self, a: float, b: float) -> bool:
        if self.info is None:
            return True
        return self.src(a) >= -0.05 and self.src(b) <= self.info.duration + 0.05


@dataclass
class Cfg:
    path: Path
    base: Path
    cams: dict
    audio: str
    fallback: str
    intro: Optional[str]
    output: str
    log: Path
    work: Path
    transition: float
    fps: Optional[float]
    width: int
    height: int
    workers: int
    encoder: str
    tonemap: str
    min_shot: float

    def cam(self, key) -> Cam:
        k = str(key)
        if k not in self.cams:
            raise FFKitError(f"no camera {k} in {self.path.name} (have {', '.join(self.cams)})")
        return self.cams[k]

    def probe_all(self) -> None:
        for c in self.cams.values():
            if c.info is None:
                c.info = probe(c.file, gop=False)

    def show_fps(self) -> float:
        if self.fps:
            return float(self.fps)
        self.probe_all()
        f = self.cam(self.audio).info.fps or 30.0
        for std in (24, 25, 30, 50, 60):
            if abs(f - std) / std < 0.02:
                return float(std)
        return round(f, 3)

    def show_end_default(self) -> float:
        """Show time at which the audio camera ends."""
        self.probe_all()
        a = self.cam(self.audio)
        return a.info.duration - a.offset


def load_cfg(path: str | Path) -> Cfg:
    p = Path(path).expanduser().resolve()
    if not p.exists():
        raise FFKitError(f"{p} not found; run `ffkit switch init` to create one")
    job = load_job(str(p)) or {}
    base = p.parent

    def rel(x: str) -> str:
        x = os.path.expanduser(str(x))
        return x if os.path.isabs(x) else str(base / x)

    raw = job.get("cameras") or {}
    cams: dict = {}
    if isinstance(raw, list):
        raw = {str(i + 1): v for i, v in enumerate(raw)}
    for k, v in raw.items():
        k = str(k)
        if isinstance(v, str):
            v = {"file": v}
        cams[k] = Cam(key=k, file=rel(v["file"]), offset=float(v.get("offset") or 0.0),
                      name=str(v.get("name") or ""))
    if len(cams) < 2:
        raise FFKitError("need at least two cameras in switch.yaml")

    offsets_file = base / "switch.offsets.json"
    if offsets_file.exists():
        for k, off in json.loads(offsets_file.read_text()).items():
            if str(k) in cams:
                cams[str(k)].offset = float(off)

    audio = str(job.get("audio") or next(iter(cams)))
    if audio not in cams:
        raise FFKitError(f"audio camera {audio} is not one of the cameras")
    intro = job.get("intro") or None
    return Cfg(
        path=p, base=base, cams=cams, audio=audio,
        fallback=str(job.get("fallback") or audio),
        intro=rel(intro) if intro else None,
        output=rel(job.get("output") or "out/show.mp4"),
        log=Path(rel(job.get("log") or "switch.log")),
        work=Path(rel(job.get("work") or "work/switch")),
        transition=float(job.get("transition", 0.5) or 0.0),
        fps=job.get("fps"),
        width=int(job.get("width") or 1920),
        height=int(job.get("height") or 1080),
        workers=int(job.get("workers") or 4),
        encoder=str(job.get("encoder") or "h264"),
        tonemap=str(job.get("tonemap") or "auto"),
        min_shot=float(job.get("min_shot") or 0.5),
    )


# --------------------------------------------------------------------------
# the log
# --------------------------------------------------------------------------

@dataclass
class Session:
    start: Optional[float] = None
    marks: list = field(default_factory=list)      # (t, cam)
    starts: list = field(default_factory=list)
    ends: list = field(default_factory=list)


def parse_log(path: Path) -> list:
    sessions = [Session()]
    if not path.exists():
        return sessions
    for raw in path.read_text().splitlines():
        line = raw.strip()
        if not line:
            continue
        if line.startswith("#"):
            s = Session()
            if "start=" in line:
                try:
                    s.start = float(line.split("start=", 1)[1].split()[0])
                except ValueError:
                    pass
            sessions.append(s)
            continue
        head, _, tail = line.partition(",")
        try:
            if head == "START":
                sessions[-1].starts.append(float(tail))
            elif head == "END":
                sessions[-1].ends.append(float(tail))
            else:
                sessions[-1].marks.append((float(head), tail.strip()))
        except ValueError:
            continue
    return sessions


def resolve(sessions: list):
    """Later sessions override earlier marks inside the range they re-marked."""
    marks: list = []
    show_start = None
    show_end = None
    for s in sessions:
        if s.marks:
            lo = min(t for t, _ in s.marks)
            hi = max(t for t, _ in s.marks)
            marks = [(t, c) for t, c in marks if not (lo <= t <= hi)]
            marks.extend(s.marks)
        if s.starts:
            show_start = s.starts[-1]
        if s.ends:
            show_end = s.ends[-1]
    marks.sort(key=lambda m: m[0])
    # a fat-finger double press: keep the later one
    cleaned: list = []
    for t, c in marks:
        if cleaned and t - cleaned[-1][0] < 0.4:
            cleaned[-1] = (t, c)
        else:
            cleaned.append((t, c))
    # pressing the camera you're already on is a no-op
    dedup: list = []
    for t, c in cleaned:
        if dedup and dedup[-1][1] == c:
            continue
        dedup.append((t, c))
    return dedup, show_start, show_end


@dataclass
class Seg:
    a: float
    b: float
    cam: str

    @property
    def dur(self) -> float:
        return self.b - self.a


def build_segments(cfg: Cfg, marks: list, show_start: float, show_end: float, fps: float) -> list:
    if show_end <= show_start:
        raise FFKitError(f"show end {show_end:.1f} is not after show start {show_start:.1f}")

    def snap(t: float) -> float:
        return round(t * fps) / fps

    pts = [(snap(t), c) for t, c in marks if show_start <= t < show_end]
    if not pts or pts[0][0] > snap(show_start):
        before = [c for t, c in marks if t < show_start]
        first_cam = before[-1] if before else cfg.fallback
        pts.insert(0, (snap(show_start), first_cam))
    pts[0] = (snap(show_start), pts[0][1])
    end = snap(show_end)
    min_len = max(cfg.min_shot, 2 * cfg.transition + 2 / fps)

    segs: list = []
    for i, (t, c) in enumerate(pts):
        nxt = pts[i + 1][0] if i + 1 < len(pts) else end
        if nxt <= t:
            continue
        if segs and (nxt - t) < min_len:
            segs[-1].b = nxt          # too short to be a shot: fold into the previous one
            continue
        segs.append(Seg(t, nxt, c))
    if not segs:
        raise FFKitError("no marks inside the show range; run `ffkit switch mark`")
    # a camera that wasn't rolling at that moment falls back
    cfg.probe_all()
    for s in segs:
        cam = cfg.cam(s.cam)
        if not cam.covers(s.a, s.b):
            s.cam = cfg.fallback
    return segs


# --------------------------------------------------------------------------
# filters shared by grid and render
# --------------------------------------------------------------------------

def _tonemap_chain(cfg: Cfg, info: Optional[VideoInfo]) -> list:
    on = cfg.tonemap == "on" or (cfg.tonemap == "auto" and info is not None and info.is_hdr)
    if not on:
        return []
    return ["zscale=t=linear:npl=100", "format=gbrpf32le", "zscale=p=bt709",
            "tonemap=hable:desat=0", "zscale=t=bt709:m=bt709:r=tv"]


def _fit(w: int, h: int) -> list:
    return [f"scale={w}:{h}:force_original_aspect_ratio=decrease:flags=bicubic",
            f"pad={w}:{h}:(ow-iw)/2:(oh-ih)/2", "setsar=1"]


def _vf(cfg: Cfg, info: Optional[VideoInfo], w: int, h: int, fps: float) -> str:
    return ",".join(_fit(w, h) + _tonemap_chain(cfg, info) + ["format=yuv420p", f"fps={fps:g}"])


# --------------------------------------------------------------------------
# commands
# --------------------------------------------------------------------------

def cmd_init(dest: str) -> None:
    p = Path(dest)
    if p.exists():
        raise FFKitError(f"{p} already exists")
    p.write_text(TEMPLATE)
    print(f"wrote {p}; fill in the camera files, then: ffkit switch sync && ffkit switch grid")


def cmd_sync(cfg: Cfg, r: Runner, *, window: float = 480.0, max_lag: float = 480.0,
             rate: int = 4000, write: bool = True) -> dict:
    """Cross-correlate each camera's audio against the audio camera's to find offsets."""
    try:
        import numpy as np  # type: ignore
    except ImportError:
        raise FFKitError("sync needs numpy: pip install numpy  (or type the offsets into switch.yaml)")

    def pcm(cam: Cam, seconds: float):
        cmd = [FFMPEG, "-v", "error", "-i", cam.file, "-t", f"{seconds:.1f}", "-vn",
               "-ac", "1", "-ar", str(rate), "-f", "s16le", "-"]
        out = subprocess.run(cmd, capture_output=True).stdout
        x = np.frombuffer(out, dtype=np.int16).astype(np.float32)
        if len(x) < rate:
            raise FFKitError(f"{Path(cam.file).name}: no usable audio")
        x = np.diff(x)                       # emphasise transients
        x -= x.mean()
        return x / (np.sqrt((x * x).mean()) + 1e-9)

    ref = cfg.cam(cfg.audio)
    span = window + max_lag
    a = pcm(ref, span)
    offsets = {ref.key: ref.offset}
    r.say(f"sync: reference = cam {ref.key} ({Path(ref.file).name}), window {window:.0f}s, max lag ±{max_lag:.0f}s")
    for key, cam in cfg.cams.items():
        if key == ref.key:
            continue
        x = pcm(cam, span)
        n = len(a) + len(x)
        nfft = 1 << (n - 1).bit_length()
        A = np.fft.rfft(a, nfft)
        X = np.fft.rfft(x, nfft)
        cc = np.fft.irfft(np.conj(A) * X, nfft)      # cc[k] = sum_t a[t] * x[t + k]
        L = int(max_lag * rate)
        lags = np.arange(-L, L + 1)
        vals = cc[lags % nfft]
        k = int(lags[np.argmax(vals)])
        z = float((vals.max() - vals.mean()) / (vals.std() + 1e-9))
        off = ref.offset + k / rate
        offsets[key] = round(off, 3)
        cam.offset = offsets[key]
        flag = "" if z >= 8 else "   <-- weak match, check it"
        r.say(f"  cam {key} ({Path(cam.file).name}): offset {off:+.3f}s  confidence z={z:.1f}{flag}")
    if write:
        out = cfg.base / "switch.offsets.json"
        out.write_text(json.dumps(offsets, indent=2))
        r.say(f"wrote {out}")
    return offsets


def _layout(n: int):
    if n <= 3:
        return n, 1
    if n == 4:
        return 2, 2
    if n <= 6:
        return 3, 2
    return 3, 3


def cmd_grid(cfg: Cfg, r: Runner, *, tile_w: int = 640, tile_h: int = 360) -> str:
    cfg.probe_all()
    fps = cfg.show_fps()
    cams = list(cfg.cams.values())
    cols, rows = _layout(len(cams))
    show_len = cfg.show_end_default()
    out = cfg.work / "grid.mp4"
    out.parent.mkdir(parents=True, exist_ok=True)

    cmd = [FFMPEG, "-hide_banner", "-y", "-v", "error", "-stats"]
    filters = []
    for i, cam in enumerate(cams):
        if cam.offset >= 0:
            cmd += ["-ss", f"{cam.offset:.3f}"]
            pre = []
        else:
            pre = [f"tpad=start_duration={-cam.offset:.3f}:color=black"]
        cmd += hwaccel_input_args() + ["-i", cam.file]
        chain = pre + _fit(tile_w, tile_h) + _tonemap_chain(cfg, cam.info) + [
            "format=yuv420p", f"fps={fps:g}",
            "tpad=stop_mode=add:stop_duration=36000:color=black",
            (f"drawtext=text='{i + 1} {cam.name}':x=12:y=10:fontsize=28:fontcolor=white:"
             f"box=1:boxcolor=black@0.5:boxborderw=6" if cam.name else
             f"drawtext=text='{i + 1}':x=12:y=10:fontsize=28:fontcolor=white:box=1:boxcolor=black@0.5:boxborderw=6"),
        ]
        filters.append(f"[{i}:v]" + ",".join(chain) + f"[t{i}]")
    positions = []
    for i in range(len(cams)):
        c, rw = i % cols, i // cols
        positions.append(f"{c * tile_w}_{rw * tile_h}")
    stack = "".join(f"[t{i}]" for i in range(len(cams))) + \
        f"xstack=inputs={len(cams)}:layout={'|'.join(positions)}:fill=black[v]"
    ai = cams.index(cfg.cam(cfg.audio))
    aud = cfg.cam(cfg.audio)
    if aud.offset < 0:
        ms = int(-aud.offset * 1000)
        filters.append(f"[{ai}:a]adelay={ms}|{ms}[a]")
        amap = "[a]"
    else:
        amap = f"{ai}:a"
    cmd += ["-filter_complex", ";".join(filters + [stack]), "-map", "[v]", "-map", amap,
            "-t", f"{show_len:.3f}", "-r", f"{fps:g}", "-fps_mode", "cfr"]
    cmd += video_encoder_args(cfg.encoder, "preview") + ["-c:a", "aac", "-b:a", "96k", "-ac", "2"]
    cmd += container_args(out) + [str(out)]
    r.run(cmd, why=f"grid proxy: {len(cams)} cameras side by side, {show_len / 60:.1f} min")
    v = verify(out, expect_duration=show_len, expect_audio=True, tolerance=2.0)
    if not r.dry_run and not v.ok:
        raise FFKitError("grid failed: " + "; ".join(v.messages))
    if not r.dry_run:
        r.say(f"ok  {v.info.summary()}")
    return str(out)


def _mpv_hint() -> str:
    s = platform.system()
    if s == "Darwin":
        return "brew install mpv"
    if s == "Linux":
        return "sudo apt install mpv   (or: sudo dnf install mpv)"
    return "install mpv from https://mpv.io"


def cmd_mark(cfg: Cfg, r: Runner, *, start_from: Optional[float] = None) -> None:
    mpv = shutil.which("mpv")
    if not mpv:
        raise FFKitError("mpv is not installed. " + _mpv_hint())
    grid = cfg.work / "grid.mp4"
    if not grid.exists():
        raise FFKitError("no grid yet; run `ffkit switch grid` first")
    marks, ss, se = resolve(parse_log(cfg.log))
    if start_from is not None:
        t0 = max(0.0, start_from)
        why = f"starting at {_fmt(t0)} (marks you make here replace the old ones in that stretch)"
    elif marks:
        t0 = max(0.0, marks[-1][0] - 3.0)
        why = f"resuming 3s before your last mark ({_fmt(marks[-1][0])})"
    else:
        t0 = ss or 0.0
        why = "starting from the top"
    cfg.log.parent.mkdir(parents=True, exist_ok=True)
    with cfg.log.open("a") as fh:
        fh.write(f"# session {time.strftime('%Y-%m-%dT%H:%M:%S')} start={t0:.3f}\n")
    names = "|".join(f"{k}:{c.name}" for k, c in cfg.cams.items() if c.name)
    cmd = [mpv, f"--script={LUA}",
           f"--script-opts-append=switch-log={cfg.log}",
           f"--script-opts-append=switch-names={names}",
           f"--start={t0:.3f}", "--pause", "--keep-open=yes", "--hr-seek=yes",
           "--force-window=yes", "--title=ffkit switch", "--autofit-larger=92%x92%",
           str(grid)]
    print(f"\n{why}. Press SPACE to roll.\n"
          "  1 2 3 = camera    u = undo    s / e = show start / end    [ ] = slower / faster    q = quit\n"
          "  Quit any time; the same command picks up where you left off.\n", file=sys.stderr)
    r.say("$ " + " ".join(cmd))
    if r.dry_run:
        return
    subprocess.call(cmd)
    cmd_status(cfg, r)


def _fmt(t: float) -> str:
    t = max(0.0, t)
    h, rem = divmod(int(t), 3600)
    m, s = divmod(rem, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


def cmd_status(cfg: Cfg, r: Runner) -> None:
    marks, ss, se = resolve(parse_log(cfg.log))
    cfg.probe_all()
    start = ss if ss is not None else 0.0
    end = se if se is not None else cfg.show_end_default()
    print(f"show: {_fmt(start)} -> {_fmt(end)}  ({(end - start) / 60:.1f} min)" +
          ("" if ss is not None else "   [start not marked: using 0]") +
          ("" if se is not None else "   [end not marked: using end of audio]"))
    if not marks:
        print("no marks yet. ffkit switch mark")
        return
    fps = cfg.show_fps()
    segs = build_segments(cfg, marks, start, end, fps)
    covered = marks[-1][0] - start
    print(f"marks: {len(marks)}   shots: {len(segs)}   last mark at {_fmt(marks[-1][0])}   "
          f"marked through {100 * min(1.0, covered / max(1e-9, end - start)):.0f}% of the show")
    share: dict = {}
    for s in segs:
        share[s.cam] = share.get(s.cam, 0.0) + s.dur
    tot = sum(share.values()) or 1.0
    print("camera share: " + "   ".join(
        f"{k} {cfg.cams[k].name or ''} {100 * v / tot:.0f}%".replace("  ", " ") for k, v in sorted(share.items())))
    long = sorted(segs, key=lambda s: -s.dur)[:5]
    if long and long[0].dur > 25:
        print("longest holds: " + ", ".join(f"cam {s.cam} {_fmt(s.a)}-{_fmt(s.b)} ({s.dur:.0f}s)" for s in long if s.dur > 25))


# --------------------------------------------------------------------------
# rendering
# --------------------------------------------------------------------------

def _enc(cfg: Cfg, profile: str) -> list:
    return video_encoder_args(cfg.encoder, "preview" if profile == "preview" else "delivery")


def _seg_job(cfg: Cfg, seg: Seg, w: int, h: int, fps: float, profile: str, out: Path) -> list:
    cam = cfg.cam(seg.cam)
    cmd = [FFMPEG, "-hide_banner", "-y", "-v", "error"] + hwaccel_input_args()
    cmd += ["-ss", f"{cam.src(seg.a):.3f}", "-i", cam.file, "-t", f"{seg.dur:.3f}",
            "-map", "0:v:0", "-an", "-vf", _vf(cfg, cam.info, w, h, fps),
            "-r", f"{fps:g}", "-fps_mode", "cfr"] + _enc(cfg, profile) + container_args(out) + [str(out)]
    return cmd


def _tr_job(cfg: Cfg, t: float, c1: str, c2: str, d: float, w: int, h: int, fps: float,
            profile: str, out: Path) -> list:
    a, b = cfg.cam(c1), cfg.cam(c2)
    read = d + 2 / fps
    cmd = [FFMPEG, "-hide_banner", "-y", "-v", "error"]
    cmd += hwaccel_input_args() + ["-ss", f"{a.src(t - d / 2):.3f}", "-t", f"{read:.3f}", "-i", a.file]
    cmd += hwaccel_input_args() + ["-ss", f"{b.src(t - d / 2):.3f}", "-t", f"{read:.3f}", "-i", b.file]
    fa = _vf(cfg, a.info, w, h, fps) + ",settb=AVTB,setpts=PTS-STARTPTS"
    fb = _vf(cfg, b.info, w, h, fps) + ",settb=AVTB,setpts=PTS-STARTPTS"
    fc = f"[0:v]{fa}[x];[1:v]{fb}[y];[x][y]xfade=transition=fade:duration={d:.3f}:offset=0[v]"
    cmd += ["-filter_complex", fc, "-map", "[v]", "-an", "-t", f"{d:.3f}",
            "-r", f"{fps:g}", "-fps_mode", "cfr"] + _enc(cfg, profile) + container_args(out) + [str(out)]
    return cmd


def _audio_job(cfg: Cfg, a: float, b: float, out: Path) -> list:
    cam = cfg.cam(cfg.audio)
    return [FFMPEG, "-hide_banner", "-y", "-v", "error", "-ss", f"{cam.src(a):.3f}", "-i", cam.file,
            "-t", f"{b - a:.3f}", "-vn", "-map", "0:a:0", "-c:a", "aac", "-b:a", "192k",
            "-ar", "48000", "-ac", "2", str(out)]


def _done_already(path: Path, dur: float, tol: float = 0.2) -> bool:
    if not path.exists() or path.stat().st_size == 0:
        return False
    v = verify(path, expect_duration=dur, expect_video=path.suffix == ".mp4",
               expect_audio=None, tolerance=tol)
    return v.ok


def render_range(cfg: Cfg, r: Runner, *, a: float, b: float, profile: str, out: Path,
                 with_intro: bool, keep_segments: bool = False) -> str:
    """Encode the pieces (once, in parallel), then stream-copy them together."""
    cfg.probe_all()
    fps = cfg.show_fps()
    marks, ss, se = resolve(parse_log(cfg.log))
    show_start = ss if ss is not None else 0.0
    show_end = se if se is not None else cfg.show_end_default()
    a = max(a, show_start)
    b = min(b, show_end)
    if b <= a:
        raise FFKitError("nothing to render in that range")
    segs = build_segments(cfg, marks, show_start, show_end, fps)
    # clip the segment list to [a, b]
    snap = lambda t: round(t * fps) / fps
    a, b = snap(a), snap(b)
    clipped = []
    for s in segs:
        if s.b <= a or s.a >= b:
            continue
        clipped.append(Seg(max(s.a, a), min(s.b, b), s.cam))
    segs = clipped
    d = snap(cfg.transition) if profile != "preview" or cfg.transition else 0.0
    d = min(d, cfg.transition)
    if profile == "preview" and d > 0:
        d = snap(min(d, 0.5))

    w, h = (640, 360) if profile == "preview" else (cfg.width, cfg.height)
    wd = cfg.work / profile
    wd.mkdir(parents=True, exist_ok=True)

    # the pieces, in order
    pieces: list = []       # (path, expected_duration, cmd)
    n = len(segs)
    for i, s in enumerate(segs):
        pa = s.a + (d / 2 if (i > 0 and d) else 0.0)
        pb = s.b - (d / 2 if (i < n - 1 and d) else 0.0)
        seg = Seg(snap(pa), snap(pb), s.cam)
        path = wd / f"seg_{seg.a:09.3f}_{seg.b:09.3f}_c{seg.cam}.mp4"
        pieces.append((path, seg.dur, _seg_job(cfg, seg, w, h, fps, profile, path)))
        if d and i < n - 1:
            t = s.b
            nxt = segs[i + 1]
            path = wd / f"tr_{t:09.3f}_c{s.cam}_c{nxt.cam}.mp4"
            pieces.append((path, d, _tr_job(cfg, t, s.cam, nxt.cam, d, w, h, fps, profile, path)))

    audio_path = wd / f"audio_{a:09.3f}_{b:09.3f}.m4a"
    audio_job = _audio_job(cfg, a, b, audio_path)

    intro_v = intro_a = None
    intro_dur = 0.0
    if with_intro and cfg.intro:
        ii = probe(cfg.intro, gop=False)
        intro_dur = snap(ii.duration)
        intro_v = wd / "intro_v.mp4"
        intro_a = wd / "intro_a.m4a"
        iv = [FFMPEG, "-hide_banner", "-y", "-v", "error"] + hwaccel_input_args() + [
            "-i", cfg.intro, "-t", f"{intro_dur:.3f}", "-map", "0:v:0", "-an",
            "-vf", _vf(cfg, ii, w, h, fps), "-r", f"{fps:g}", "-fps_mode", "cfr"] + \
            _enc(cfg, profile) + container_args(intro_v) + [str(intro_v)]
        if ii.has_audio:
            ia = [FFMPEG, "-hide_banner", "-y", "-v", "error", "-i", cfg.intro, "-t", f"{intro_dur:.3f}",
                  "-vn", "-map", "0:a:0", "-c:a", "aac", "-b:a", "192k", "-ar", "48000", "-ac", "2", str(intro_a)]
        else:
            ia = [FFMPEG, "-hide_banner", "-y", "-v", "error", "-f", "lavfi", "-i", "anullsrc=r=48000:cl=stereo",
                  "-t", f"{intro_dur:.3f}", "-c:a", "aac", "-b:a", "96k", str(intro_a)]

    total_enc = sum(p[1] for p in pieces)
    r.say(f"{profile}: {len(segs)} shots, {len(pieces)} pieces, {total_enc / 60:.1f} min of video to encode "
          f"at {w}x{h}@{fps:g}, {cfg.workers} at a time" + (f", + {intro_dur:.0f}s intro" if intro_v else ""))

    jobs = [(p, dur, cmd) for p, dur, cmd in pieces]
    jobs.append((audio_path, b - a, audio_job))
    if intro_v:
        jobs.append((intro_v, intro_dur, iv))
        jobs.append((intro_a, intro_dur, ia))

    todo = [(p, dur, cmd) for p, dur, cmd in jobs if r.dry_run or not _done_already(p, dur)]
    skipped = len(jobs) - len(todo)
    if skipped:
        r.say(f"{skipped} pieces already done, reusing them")

    if r.dry_run:
        for p, dur, cmd in todo[:3]:
            r.run(cmd, why=f"piece {p.name} ({dur:.2f}s)")
        if len(todo) > 3:
            r.say(f"... and {len(todo) - 3} more pieces like those")
    else:
        t0 = time.time()
        errors: list = []
        done = 0
        with ThreadPoolExecutor(max_workers=max(1, cfg.workers)) as pool:
            futs = {pool.submit(subprocess.run, cmd, capture_output=True, text=True): (p, dur, cmd)
                    for p, dur, cmd in todo}
            for fut in as_completed(futs):
                p, dur, cmd = futs[fut]
                proc = fut.result()
                done += 1
                if proc.returncode != 0:
                    errors.append(f"{p.name}: " + "\n".join(proc.stderr.strip().splitlines()[-3:]))
                elif not _done_already(p, dur, tol=max(0.2, dur * 0.05)):
                    errors.append(f"{p.name}: wrong length after encode")
                if done % 25 == 0 or done == len(todo):
                    el = time.time() - t0
                    r.say(f"  {done}/{len(todo)} pieces, {el / 60:.1f} min elapsed")
        if errors:
            raise FFKitError("some pieces failed:\n  " + "\n  ".join(errors[:10]))

    # stream-copy everything together
    vlist = wd / "video.txt"
    alist = wd / "audio.txt"
    with vlist.open("w") as fh:
        if intro_v:
            fh.write(f"file '{intro_v.resolve()}'\n")
        for p, _, _ in pieces:
            fh.write(f"file '{p.resolve()}'\n")
    with alist.open("w") as fh:
        if intro_a:
            fh.write(f"file '{intro_a.resolve()}'\n")
        fh.write(f"file '{audio_path.resolve()}'\n")
    out.parent.mkdir(parents=True, exist_ok=True)
    cmd = [FFMPEG, "-hide_banner", "-y", "-v", "error", "-stats",
           "-f", "concat", "-safe", "0", "-i", str(vlist),
           "-f", "concat", "-safe", "0", "-i", str(alist),
           "-map", "0:v:0", "-map", "1:a:0", "-c", "copy"] + container_args(out) + [str(out)]
    r.run(cmd, why=f"join {len(pieces) + (1 if intro_v else 0)} pieces + audio, stream copy")
    expected = (b - a) + intro_dur
    v = verify(out, expect_duration=expected, expect_audio=True, tolerance=max(1.0, expected * 0.002))
    if not r.dry_run:
        if not v.ok:
            raise FFKitError(f"final check failed: " + "; ".join(v.messages))
        r.say(f"ok  {v.info.summary()}")
        if not keep_segments:
            for p, _, _ in pieces:
                p.unlink(missing_ok=True)
            vlist.unlink(missing_ok=True)
            alist.unlink(missing_ok=True)
    return str(out)


def cmd_preview(cfg: Cfg, r: Runner, *, start: Optional[float], end: Optional[float],
                play: bool = True, whole: bool = False) -> str:
    sessions = parse_log(cfg.log)
    marks, ss, se = resolve(sessions)
    if not marks:
        raise FFKitError("no marks yet; run `ffkit switch mark` first")
    show_start = ss if ss is not None else 0.0
    show_end = se if se is not None else cfg.show_end_default()
    if whole:
        a, b = show_start, show_end
    elif start is not None or end is not None:
        a = start if start is not None else show_start
        b = end if end is not None else show_end
    else:
        # default: what you marked in the most recent session that has marks
        last = [s for s in sessions if s.marks][-1]
        a = max(show_start, min(t for t, _ in last.marks) - 2.0)
        hi = max(t for t, _ in last.marks)
        after = [t for t, _ in marks if t > hi]
        b = min(show_end, after[0] if after else hi + 15.0)
    out = cfg.work / "preview.mp4"
    render_range(cfg, r, a=a, b=b, profile="preview", out=out, with_intro=False)
    r.say(f"preview of {_fmt(a)} -> {_fmt(b)}: {out}")
    mpv = shutil.which("mpv")
    if play and mpv and not r.dry_run:
        subprocess.call([mpv, "--really-quiet", "--title=ffkit preview", str(out)])
    return str(out)


def cmd_render(cfg: Cfg, r: Runner, *, keep_segments: bool = False) -> str:
    marks, ss, se = resolve(parse_log(cfg.log))
    if not marks:
        raise FFKitError("no marks yet; run `ffkit switch mark` first")
    show_start = ss if ss is not None else 0.0
    show_end = se if se is not None else cfg.show_end_default()
    out = Path(os.path.expanduser(cfg.output))
    render_range(cfg, r, a=show_start, b=show_end, profile="final", out=out,
                 with_intro=True, keep_segments=keep_segments)
    r.say(f"final: {out}")
    return str(out)


# --------------------------------------------------------------------------
# CLI glue
# --------------------------------------------------------------------------

def add_parser(sub) -> None:
    p = sub.add_parser("switch", help="live-switch a multi-camera recording: grid, mark, preview, render")
    p.add_argument("-f", "--file", default="switch.yaml", help="config (default switch.yaml)")
    ss = p.add_subparsers(dest="switch_cmd", required=True)
    ss.add_parser("init", help="write a switch.yaml to fill in")
    q = ss.add_parser("sync", help="work out camera offsets from their audio (needs numpy)")
    q.add_argument("--window", type=float, default=480.0, help="seconds of audio to compare")
    q.add_argument("--max-lag", type=float, default=480.0, help="largest offset to look for, seconds")
    q.add_argument("--no-write", action="store_true")
    ss.add_parser("grid", help="build the side-by-side proxy")
    q = ss.add_parser("mark", help="open the grid in mpv and press 1/2/3 as it plays")
    q.add_argument("--from", dest="start_from", help="start here instead of resuming (re-marks that stretch)")
    ss.add_parser("status", help="how much is marked, camera share, long holds")
    q = ss.add_parser("preview", help="fast low-res render of what you just marked (or a range)")
    q.add_argument("--from", dest="start")
    q.add_argument("--to", dest="end")
    q.add_argument("--all", action="store_true", help="the whole show")
    q.add_argument("--no-play", action="store_true")
    q = ss.add_parser("render", help="frame-accurate final with transitions and the intro")
    q.add_argument("--keep-segments", action="store_true")


def main(a, dry_run: bool, verbose: bool) -> int:
    if a.switch_cmd == "init":
        cmd_init(a.file)
        return 0
    cfg = load_cfg(a.file)
    r = Runner(dry_run=dry_run, verbose=verbose, job_name="switch-" + a.switch_cmd)
    if a.switch_cmd == "sync":
        cmd_sync(cfg, r, window=a.window, max_lag=a.max_lag, write=not a.no_write)
    elif a.switch_cmd == "grid":
        cmd_grid(cfg, r)
    elif a.switch_cmd == "mark":
        cmd_mark(cfg, r, start_from=_parse_time(a.start_from))
    elif a.switch_cmd == "status":
        cmd_status(cfg, r)
    elif a.switch_cmd == "preview":
        cmd_preview(cfg, r, start=_parse_time(a.start), end=_parse_time(a.end),
                    play=not a.no_play, whole=a.all)
    elif a.switch_cmd == "render":
        cmd_render(cfg, r, keep_segments=a.keep_segments)
    return 0
