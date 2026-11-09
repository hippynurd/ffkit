"""
ffkit switch — live-switch a multi-camera recording after the fact.

The person does the one thing software can't: decide which camera, in the moment,
by ear and eye. The computer does everything else.

    ffkit switch init                       write switch.yaml to fill in
    ffkit switch sync                       work out each camera's offset from its audio
    ffkit switch grid                       build the side-by-side proxy you watch while switching
    ffkit switch mark [--from T]            open the grid in mpv; press 1/2/3 as the show plays
    ffkit switch status                     how much is marked, long holds, camera share
    ffkit switch zoom [--from T]            second pass: play the cut, set zoom and pan per camera
    ffkit switch preview [--from T --to T]  fast low-res render of what you just marked
    ffkit switch render                     frame-accurate final with transitions, intro in front
    ffkit switch render --from T --to T     one stretch (a song) at full quality, to check it

The log (switch.log) is plain text: one `seconds,camera` line per press. Anything can
write it. To redo a stretch, run `mark --from T` and press keys again; the newer marks
replace the old ones in the range you re-marked.

Zoom (zoom.log) works the same way: one `seconds,camera,zoom,cx,cy,ease` line per change,
where cx/cy is the centre of the picture as a fraction of the frame (0.5,0.5 = middle) and
ease is how many seconds the move takes. Each camera keeps its own zoom, so a push-in on
main is still there when you cut back to it. Preview and render apply it automatically.
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

transition: 0.75      # seconds of crossfade at each camera cut, like a T-bar pull; 0 = hard cuts
intro_fade: 1.0       # fade through black between the intro and the show; 0 = none
# fps: 30             # default: the audio camera's rate
# width: 1920
# height: 1080
workers: 4            # parallel encodes

# shots: a key = a camera plus a zoom and where it points, like a PTZ camera's presets. Set them up
# on the mark page (shift + the key), then press the key as the show plays. Saved in switch.shots.json.
# shots: switch.shots.json
# second pass (ffkit switch zoom): touch up a camera's framing; moves take zoom_ease seconds; changes made
# within zoom_settle seconds of each other render as one direct move to where you ended up
# zoom_levels: [1, 1.2, 1.4, 1.6, 1.8]    # keys 1-5 without a shot on them
# zoom_ease: 1.0
# zoom_settle: 4.0
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
    intro_fade: float = 1.0
    zoom_log: Optional[Path] = None
    zoom_ease: float = 1.0
    zoom_levels: tuple = (1.0, 1.2, 1.4, 1.6, 1.8)
    zoom_settle: float = 4.0
    shots: Optional[Path] = None

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
        intro_fade=float(job.get("intro_fade", 1.0) or 0.0),
        zoom_log=Path(rel(job.get("zoom_log") or "zoom.log")),
        zoom_ease=float(job.get("zoom_ease", 1.0) or 0.0),
        zoom_levels=tuple(float(z) for z in (job.get("zoom_levels") or (1.0, 1.2, 1.4, 1.6, 1.8))),
        zoom_settle=float(job.get("zoom_settle", 4.0) or 0.0),
        shots=Path(rel(job.get("shots") or "switch.shots.json")),
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


# --------------------------------------------------------------------------
# shots: one key = a camera plus a framing, like a PTZ camera's presets
# --------------------------------------------------------------------------

def load_shots(path: Optional[Path]) -> dict:
    """switch.shots.json -> {key: {"cam", "z", "cx", "cy", "name"}}. A key with no shot on it
    is the plain camera of that number, as it is."""
    if path is None or not path.exists():
        return {}
    try:
        raw = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return {}
    shots: dict = {}
    for key, v in (raw or {}).items():
        if not isinstance(v, dict) or not str(v.get("cam", "")).strip():
            continue
        try:
            z = max(1.0, float(v.get("z", 1.0)))
            cx = min(1.0, max(0.0, float(v.get("cx", 0.5))))
            cy = min(1.0, max(0.0, float(v.get("cy", 0.5))))
        except (TypeError, ValueError):
            continue
        shots[str(key)] = {"cam": str(v["cam"]).strip(), "z": z, "cx": cx, "cy": cy,
                           "name": str(v.get("name") or "")}
    return shots


def save_shots(path: Path, shots: dict) -> None:
    data = {k: {"cam": s["cam"], "z": round(s["z"], 4), "cx": round(s["cx"], 4), "cy": round(s["cy"], 4),
                "name": s.get("name", "")} for k, s in sorted(shots.items())}
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(data, indent=1) + "\n")
    tmp.replace(path)


def shot_label(key: str, s: dict, cfg: Optional["Cfg"] = None) -> str:
    cam = cfg.cams[s["cam"]].name if cfg and s["cam"] in cfg.cams and cfg.cams[s["cam"]].name else f"cam {s['cam']}"
    z = "full frame" if s["z"] <= 1.0005 else f"{s['z']:.1f}x"
    return f"{key} {s['name']} ({cam}, {z})" if s.get("name") else f"{key} {cam} {z}"


def load_marks(cfg: Cfg, shots: Optional[dict] = None):
    """The marks with shots resolved: [(t, camera, shot key or None)], show start, show end.
    A mark `s3` is shot 3 (its camera, framed as the shot says now); a plain `3` is camera 3 as it is."""
    marks, ss, se = resolve(parse_log(cfg.log))
    if shots is None:
        shots = load_shots(cfg.shots)
    out: list = []
    for t, key in marks:
        if key.startswith("s"):
            k = key[1:]
            s = shots.get(k)
            if s and s["cam"] in cfg.cams:
                out.append((t, s["cam"], k))
            else:                       # the shot was removed since: its key as a plain camera, else the fallback
                out.append((t, k if k in cfg.cams else cfg.fallback, None))
        else:
            out.append((t, key, None))
    return out, ss, se


# --------------------------------------------------------------------------
# zoom: one track per camera, keyframes with an eased move into each
# --------------------------------------------------------------------------

@dataclass
class ZKey:
    t: float
    cam: str
    z: float
    cx: float
    cy: float
    ease: float
    follow: bool = False      # part of a recorded path (shift-drag); never merged with its neighbours
    fixed: bool = False       # a shot's framing, from the marks: kept as it is, never merged
    shot: Optional[str] = None   # the shot key it came from, if any


ZOOM_HOME = (1.0, 0.5, 0.5)


def settle(keys: list, gap: float) -> list:
    """Changes made within `gap` seconds of each other on one camera are one move: from where
    the picture was when the first was made, straight to where the last one landed, with the
    first one's ease. So finding the framing by trial and error renders as a single direct
    move. Follow keys are a recorded path and are kept one by one, as are the shots' own
    framings; a hard change (ease 0) starts a group of its own, so it is never pulled back
    into an earlier move."""
    if gap <= 0:
        return list(keys)
    out: list = []
    group: list = []

    def flush():
        if group:
            f, l = group[0], group[-1]
            out.append(ZKey(f.t, f.cam, l.z, l.cx, l.cy, f.ease, shot=l.shot))
            group.clear()

    for k in keys:
        if k.follow or k.fixed:
            flush()
            out.append(k)
        elif group and k.t - group[-1].t <= gap and k.ease > 0:
            group.append(k)
        else:
            flush()
            group.append(k)
    flush()
    return out


def parse_zoom(path: Optional[Path], shots: Optional[dict] = None) -> dict:
    """zoom.log -> {cam: [ZKey, ...]} sorted by time. A later session replaces the
    earlier keys of the same camera inside the stretch it re-did. A key made by pressing a
    shot (`p3` on the line) takes whatever framing shot 3 has now."""
    tracks: dict = {}
    if path is None or not path.exists():
        return tracks
    sessions: list = [[]]
    for raw in path.read_text().splitlines():
        line = raw.strip()
        if not line:
            continue
        if line.startswith("#"):
            sessions.append([])
            continue
        p = [x.strip() for x in line.split(",")]
        if len(p) < 6:
            continue
        flags = p[6:]
        shot = next((x[1:] for x in flags if len(x) > 1 and x[0] == "p" and x[1:].isdigit()), None)
        try:
            k = ZKey(float(p[0]), p[1], max(1.0, float(p[2])), float(p[3]), float(p[4]),
                     max(0.0, float(p[5])), follow="f" in flags, shot=shot)
        except ValueError:
            continue
        if shot and shots:
            s = shots.get(shot)
            if s and s["cam"] == k.cam:
                k.z, k.cx, k.cy = s["z"], s["cx"], s["cy"]
        sessions[-1].append(k)
    for s in sessions:
        by_cam: dict = {}
        for k in s:
            by_cam.setdefault(k.cam, []).append(k)
        for cam, keys in by_cam.items():
            lo, hi = min(k.t for k in keys), max(k.t for k in keys)
            old = [k for k in tracks.get(cam, []) if not (lo <= k.t <= hi)]
            tracks[cam] = old + keys
    for cam in tracks:
        tracks[cam].sort(key=lambda k: k.t)
    return tracks


def _smooth(s: float) -> float:
    s = min(1.0, max(0.0, s))
    return s * s * (3 - 2 * s)


def _moves(keys: list, gap: float = 0.0) -> list:
    """Keyframes -> moves (t, dur, from, to); each move starts from wherever the picture
    was at the moment of the press, so a press mid-move carries on smoothly."""
    moves: list = []
    state = ZOOM_HOME
    for k in settle(keys, gap):
        cur = _value(moves, k.t, state)
        moves.append((k.t, k.ease, cur, (k.z, k.cx, k.cy)))
    return moves


def _value(moves: list, t: float, home=ZOOM_HOME) -> tuple:
    v = home
    for t0, d, fr, to in moves:
        if t < t0:
            break
        s = 1.0 if d <= 0 else _smooth((t - t0) / d)
        v = tuple(a + (b - a) * s for a, b in zip(fr, to))
    return v


def zoom_exprs(keys: list, a: float, b: float, fps: float, gap: float = 0.0) -> Optional[tuple]:
    """ffmpeg expressions for zoom and centre over show time [a, b], with T = a + on/fps.
    None when the camera is at full frame the whole time (no zoom filter needed)."""
    if not keys:
        return None
    moves = _moves(keys, gap)
    rel = [m for m in moves if m[0] < b and m[0] + m[1] > a]     # moves still running inside [a, b]
    start = _value(moves, a)
    zs = [start[0]] + [m[3][0] for m in rel] + [m[2][0] for m in rel]
    if max(zs) <= 1.0005:
        return None
    T = f"({a:.4f}+on/{fps:g})"

    # These go inside single quotes in the filter, so the commas need no escaping.
    def piece(i: int) -> str:
        expr = f"{start[i]:.5f}"
        for t0, d, fr, to in rel:
            if d <= 0:
                cur = f"{to[i]:.5f}"
            else:
                s = f"clip(({T}-{t0:.4f})/{d:.4f},0,1)"
                cur = f"({fr[i]:.5f}+({to[i] - fr[i]:.5f})*{s}*{s}*(3-2*{s}))"
            expr = f"if(gte({T},{t0:.4f}),{cur},{expr})"
        return expr

    return piece(0), piece(1), piece(2)


@dataclass
class Seg:
    a: float
    b: float
    cam: str

    @property
    def dur(self) -> float:
        return self.b - self.a


def plan_cuts(cfg: Cfg, marks: list, show_start: float, show_end: float, fps: float):
    """marks [(t, cam, shot)] -> (segments, framing events).
    Pressing a shot on the camera already on screen is not a cut: the segment carries on and
    the camera moves to the new framing. Events are (t, cam, shot key, cut?) for every press
    with a shot that made it to the screen."""
    if show_end <= show_start:
        raise FFKitError(f"show end {show_end:.1f} is not after show start {show_start:.1f}")

    def snap(t: float) -> float:
        return round(t * fps) / fps

    pts = [(snap(t), c, k) for t, c, k in marks if show_start <= t < show_end]
    if not pts or pts[0][0] > snap(show_start):
        before = [(c, k) for t, c, k in marks if t < show_start]
        c0, k0 = before[-1] if before else (cfg.fallback, None)
        pts.insert(0, (snap(show_start), c0, k0))
    pts[0] = (snap(show_start), pts[0][1], pts[0][2])
    end = snap(show_end)
    min_len = max(cfg.min_shot, 2 * cfg.transition + 2 / fps)
    cfg.probe_all()

    segs: list = []
    events: list = []
    for i, (t, c, k) in enumerate(pts):
        nxt = pts[i + 1][0] if i + 1 < len(pts) else end
        if nxt <= t:
            continue
        if not cfg.cam(c).covers(t, nxt):
            c, k = cfg.fallback, None     # a camera that wasn't rolling at that moment falls back, as it is
        if segs and segs[-1].cam == c:    # the same camera again: no cut, just (maybe) a new framing
            segs[-1].b = nxt
            if k:
                events.append((t, c, k, False))
            continue
        if segs and (nxt - t) < min_len:
            segs[-1].b = nxt              # too short to be a shot: fold into the previous one
            continue
        segs.append(Seg(t, nxt, c))
        if k:
            events.append((t, c, k, True))
    if not segs:
        raise FFKitError("no marks inside the show range; run `ffkit switch mark`")
    return segs, events


def build_segments(cfg: Cfg, marks: list, show_start: float, show_end: float, fps: float) -> list:
    return plan_cuts(cfg, marks, show_start, show_end, fps)[0]


def shot_keys(cfg: Cfg, events: list, shots: dict, show_start: float, fps: float) -> dict:
    """The framing each shot press asks for, as fixed keys on that camera's track. A cut to a
    shot sets its framing just before the fade into it, as a cut; a shot pressed on the camera
    already on screen moves there over zoom_ease seconds, the way a PTZ camera does."""
    snap = lambda t: round(t * fps) / fps
    tracks: dict = {}
    for t, cam, key, cut in events:
        s = shots.get(key)
        if not s or s["cam"] != cam:
            continue
        if cut:
            tk = max(snap(show_start), snap(t) - cfg.transition / 2 - 0.05)
            tracks.setdefault(cam, []).append(ZKey(tk, cam, s["z"], s["cx"], s["cy"], 0.0, fixed=True, shot=key))
        else:
            tracks.setdefault(cam, []).append(ZKey(snap(t), cam, s["z"], s["cx"], s["cy"], cfg.zoom_ease,
                                                  fixed=True, shot=key))
    return tracks


def all_tracks(cfg: Cfg, marks: list, show_start: float, show_end: float, fps: float,
               shots: Optional[dict] = None) -> dict:
    """Every camera's zoom track: the shots' framings from the marks, plus the zoom pass's changes.
    At the same instant a zoom-pass change comes after a shot's framing, so it wins."""
    if shots is None:
        shots = load_shots(cfg.shots)
    _, events = plan_cuts(cfg, marks, show_start, show_end, fps)
    tracks = shot_keys(cfg, events, shots, show_start, fps)
    for cam, keys in parse_zoom(cfg.zoom_log, shots).items():
        tracks.setdefault(cam, []).extend(keys)
    for cam in tracks:
        tracks[cam].sort(key=lambda k: (k.t, 0 if k.fixed else 1))
    return tracks


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


def _even(x: float) -> int:
    return max(2, int(round(x / 2)) * 2)


def _vf_zoom(cfg: Cfg, info: Optional[VideoInfo], w: int, h: int, fps: float, ex: tuple) -> str:
    """Crop-and-scale from the camera's full resolution, so zooming into a 4K camera
    stays sharp up to (source height / output height)x. The frame is first fitted into
    an output-shaped box at source size, the same geometry the proxy shows."""
    z, cx, cy = ex
    hk = max(h, _even((info.height if info and info.height else h)))
    wk = _even(hk * w / h)
    zp = (f"zoompan=z='max(1,{z})'"
          f":x='max(0,min(iw-iw/zoom,({cx})*iw-iw/zoom/2))'"
          f":y='max(0,min(ih-ih/zoom,({cy})*ih-ih/zoom/2))'"
          f":d=1:s={w}x{h}:fps={fps:g}")
    return ",".join(_tonemap_chain(cfg, info) + ["format=yuv420p", f"fps={fps:g}"] + _fit(wk, hk) +
                    [zp, "setsar=1"])


def _cam_vf(cfg: Cfg, cam: "Cam", a: float, b: float, w: int, h: int, fps: float,
            tracks: Optional[dict]) -> tuple:
    """(filter, filename tag) for camera `cam` over show time [a, b]."""
    ex = zoom_exprs(tracks.get(cam.key, []), a, b, fps, cfg.zoom_settle) if tracks else None
    if ex is None:
        return _vf(cfg, cam.info, w, h, fps), ""
    import hashlib
    tag = "_z" + hashlib.sha1("|".join(ex).encode()).hexdigest()[:8]
    return _vf_zoom(cfg, cam.info, w, h, fps, ex), tag


# --------------------------------------------------------------------------
# commands
# --------------------------------------------------------------------------

def cmd_init(dest: str) -> None:
    p = Path(dest)
    if p.exists():
        raise FFKitError(f"{p} already exists")
    p.write_text(TEMPLATE)
    print(f"wrote {p}; fill in the camera files, then: ffkit switch sync && ffkit switch grid")


def cmd_sync(cfg: Cfg, r: Runner, *, window: Optional[float] = None, max_lag: Optional[float] = None,
             rate: int = 4000, write: bool = True) -> dict:
    """
    Find each camera's offset from the audio camera by cross-correlating their sound.

    Two passes. Coarse: a loudness envelope of the WHOLE file at 400 Hz, so a camera
    that started an hour late is still found. Fine: two minutes of raw audio from the
    overlap, to land the offset within a sample or so.
    """
    try:
        import numpy as np  # type: ignore
    except ImportError:
        raise FFKitError("sync needs numpy: pip install numpy  (or type the offsets into switch.yaml)")

    HOP = 10                      # envelope rate = rate / HOP
    env_rate = rate / HOP

    def pcm(cam: Cam, start: float, seconds: float):
        cmd = [FFMPEG, "-v", "error", "-ss", f"{max(0.0, start):.3f}", "-i", cam.file,
               "-t", f"{seconds:.1f}", "-vn", "-ac", "1", "-ar", str(rate), "-f", "s16le", "-"]
        out = subprocess.run(cmd, capture_output=True).stdout
        x = np.frombuffer(out, dtype=np.int16).astype(np.float32)
        if len(x) < rate:
            raise FFKitError(f"{Path(cam.file).name}: no usable audio at {start:.0f}s")
        return x

    def norm(x):
        x = np.diff(x)                      # transients
        x = x - x.mean()
        return x / (np.sqrt((x * x).mean()) + 1e-9)

    def envelope(x):
        n = (len(x) // HOP) * HOP
        e = np.abs(x[:n]).reshape(-1, HOP).mean(axis=1)
        return norm(e)

    def peak(a, x, max_lag_samples=None):
        nfft = 1 << (len(a) + len(x) - 1).bit_length()
        cc = np.fft.irfft(np.conj(np.fft.rfft(a, nfft)) * np.fft.rfft(x, nfft), nfft)   # cc[k] = sum a[t] x[t+k]
        L = max_lag_samples if max_lag_samples is not None else max(len(a), len(x)) - 1
        lags = np.arange(-L, L + 1)
        vals = cc[lags % nfft]
        k = int(lags[int(np.argmax(vals))])
        z = float((vals.max() - vals.mean()) / (vals.std() + 1e-9))
        return k, z

    cfg.probe_all()
    ref = cfg.cam(cfg.audio)
    ref_len = ref.info.duration
    span_ref = min(ref_len, window) if window else ref_len
    r.say(f"sync: reference = cam {ref.key} ({Path(ref.file).name}); coarse pass over "
          f"{span_ref / 60:.0f} min of audio" + (f", max lag ±{max_lag:.0f}s" if max_lag else ""))
    ea = envelope(pcm(ref, 0.0, span_ref))
    offsets = {ref.key: ref.offset}
    for key, cam in cfg.cams.items():
        if key == ref.key:
            continue
        cam_len = cam.info.duration
        span = min(cam_len, window) if window else cam_len
        ex = envelope(pcm(cam, 0.0, span))
        k_env, z1 = peak(ea, ex, int(max_lag * env_rate) if max_lag else None)
        k = k_env * HOP / rate                       # seconds: x[t + k] ~ ref[t]
        # fine pass on two minutes of raw audio inside the overlap
        lo, hi = max(0.0, -k), min(ref_len, cam_len - k)
        z2 = 0.0
        if hi - lo >= 20:
            dur = min(120.0, hi - lo)
            T = lo + max(0.0, (hi - lo - dur) / 2)
            a = norm(pcm(ref, T, dur))
            x = norm(pcm(cam, T + k, dur))
            kf, z2 = peak(a, x, int(2.0 * rate))
            k += kf / rate
        off = ref.offset + k
        offsets[key] = round(off, 3)
        cam.offset = offsets[key]
        weak = z1 < 8 or (hi - lo) < 20
        flag = "   <-- weak match, check it" if weak else ""
        r.say(f"  cam {key} ({Path(cam.file).name}): offset {off:+.3f}s  "
              f"(overlap {max(0.0, hi - lo) / 60:.0f} min, confidence z={z1:.1f}/{z2:.1f}){flag}")
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
    # On a machine without hardware decoding, 4K sources make the grid a multi-hour job.
    # Skipping B-frames in the decoder roughly halves it; the proxy gets a little choppier
    # but the audio (what you cut to) is untouched.
    fast = not hwaccel_input_args() and any((c.info.height or 0) > 1080 for c in cams)
    if fast:
        r.say("no hardware decoder and >1080p sources: decoding without B-frames to keep this to hours, not days")
    filters = []
    for i, cam in enumerate(cams):
        if cam.offset >= 0:
            cmd += ["-ss", f"{cam.offset:.3f}"]
            pre = []
        else:
            pre = [f"tpad=start_duration={-cam.offset:.3f}:color=black"]
        if fast:
            cmd += ["-skip_frame", "bidir"]
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


def cmd_mark(cfg: Cfg, r: Runner, *, start_from: Optional[float] = None, web: bool = False) -> None:
    mpv = shutil.which("mpv")
    grid = cfg.work / "grid.mp4"
    if not grid.exists():
        raise FFKitError("no grid yet; run `ffkit switch grid` first")
    shots = load_shots(cfg.shots)
    marks, ss, se = load_marks(cfg, shots)
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
    if shots and mpv and not web:
        r.say("shots are set up, so marking in your browser (mpv only knows plain cameras)")
    if web or not mpv or shots:
        from . import switch_web
        print(f"\n{why}.", file=sys.stderr)
        if not mpv:
            r.say("mpv not installed; marking in your browser instead")
        if r.dry_run:
            return
        # what is on screen where we start: the last mark at or before t0
        cur = [(c, k) for t, c, k in marks if t <= t0 + 1e-6]
        current, current_shot = cur[-1] if cur else (None, None)
        # where each camera sits in the grid, so the page can show one camera big while you frame a shot
        gi = probe(str(grid), gop=False)
        cols, rows = _layout(len(cfg.cams))
        gw, gh = (gi.width or cols * 640), (gi.height or rows * 360)
        tw, th = gw / cols, gh / rows
        tiles = {k: [(i % cols) * tw, (i // cols) * th, tw, th] for i, k in enumerate(cfg.cams)}
        # when each camera has footage, in show time
        cfg.probe_all()
        cover = {k: [-c.offset, (c.info.duration if c.info else 0.0) - c.offset] for k, c in cfg.cams.items()}
        # The page shows the cameras in the grid's slots and, in a spare slot, the program view (what the
        # viewer sees now). With the grid full, two cameras whose footage never overlaps (one camera that
        # was restarted) share a slot, which frees one.
        groups = [[k] for k in cfg.cams]
        if len(groups) >= cols * rows:
            merged = False
            for i in range(len(groups)):
                for j in range(i + 1, len(groups)):
                    a, b = cover[groups[i][0]], cover[groups[j][0]]
                    if a[1] <= b[0] or b[1] <= a[0]:
                        groups[i] += groups.pop(j)
                        merged = True
                        break
                if merged:
                    break
        slots = [{"x": (i % cols) * tw, "y": (i // cols) * th, "w": tw, "h": th, "cams": grp} for i, grp in enumerate(groups)]
        n = len(groups)
        pslot = {"x": (n % cols) * tw, "y": (n // cols) * th, "w": tw, "h": th} if n < cols * rows else None
        fps = cfg.show_fps()

        def program_now() -> dict:
            # the cut and the framing as the render would do them right now, for the program view
            m2, ss2, se2 = load_marks(cfg)
            if not m2:
                return {"segs": [], "tracks": {}}
            s0 = ss2 if ss2 is not None else 0.0
            e0 = se2 if se2 is not None else cfg.show_end_default()
            return {"segs": [[s.a, s.b, s.cam] for s in build_segments(cfg, m2, s0, e0, fps)],
                    "tracks": switch_web._tracks_json(all_tracks(cfg, m2, s0, e0, fps))}

        switch_web.serve(grid, cfg.log, {k: c.name for k, c in cfg.cams.items()}, t0, current, len(marks),
                         shots_path=cfg.shots, tiles=tiles, current_shot=current_shot,
                         levels=list(cfg.zoom_levels), slots=slots, pslot=pslot, cover=cover,
                         settle=cfg.zoom_settle, program_fn=program_now)
        cmd_status(cfg, r)
        return
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
    shots = load_shots(cfg.shots)
    marks, ss, se = load_marks(cfg, shots)
    cfg.probe_all()
    if shots:
        print("shots: " + "   ".join(shot_label(k, s, cfg) for k, s in sorted(shots.items())))
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
    print(f"marks: {len(marks)}   segments: {len(segs)}   last mark at {_fmt(marks[-1][0])}   "
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
    tracks = all_tracks(cfg, marks, start, end, fps, shots)
    if tracks:
        fixed = sum(1 for v in tracks.values() for k in v if k.fixed)
        n = sum(len(v) for v in tracks.values()) - fixed
        moves = {c: len(settle(v, cfg.zoom_settle)) for c, v in tracks.items()}
        last = max(k.t for v in tracks.values() for k in v)
        print(f"zoom: {n} changes" + (f" + {fixed} shot framings" if fixed else "") +
              f" -> {sum(moves.values())} moves (" +
              ", ".join(f"cam {c}: {m}" for c, m in sorted(moves.items())) + f"), last at {_fmt(last)}")


def cmd_zoom(cfg: Cfg, r: Runner, *, start_from: Optional[float] = None) -> None:
    """Second pass: play the cut so far and set zoom/pan for whichever camera is on."""
    marks, ss, se = load_marks(cfg)
    if not marks:
        raise FFKitError("no camera marks yet; run `ffkit switch mark` first, zoom is the second pass")
    cfg.probe_all()
    fps = cfg.show_fps()
    show_start = ss if ss is not None else 0.0
    show_end = se if se is not None else cfg.show_end_default()
    a0 = round(show_start * fps) / fps
    # the cut so far: to the marked end of the show, or 30 s past the last mark while marking is under way
    b0 = round((show_end if se is not None else min(show_end, marks[-1][0] + 30.0)) * fps) / fps
    proxy = cfg.work / "zoom_proxy.mp4"
    r.say(f"zoom proxy: the cut from {_fmt(a0)} to {_fmt(b0)} at 960x540, no zoom applied. "
          "Segments already built are reused, so only new ones take time.")
    render_range(cfg, r, a=a0, b=b0, profile="proxy", out=proxy, with_intro=False,
                 keep_segments=True, zoom=False)
    segs = [s for s in build_segments(cfg, marks, show_start, show_end, fps) if s.a < b0]
    changes = parse_zoom(cfg.zoom_log)
    if start_from is not None:
        t0 = start_from
        why = f"starting at {_fmt(t0)} (zoom changes you make here replace the old ones in that stretch)"
    elif changes:
        t0 = max(k.t for v in changes.values() for k in v) - 3.0
        why = f"resuming 3s before your last zoom change"
    else:
        t0 = a0
        why = "starting from the top of the cut"
    t0 = min(max(t0, a0), b0 - 1.0)
    cfg.zoom_log.parent.mkdir(parents=True, exist_ok=True)
    with cfg.zoom_log.open("a") as fh:
        fh.write(f"# session {time.strftime('%Y-%m-%dT%H:%M:%S')} start={t0:.3f}\n")
    print(f"\n{why}.", file=sys.stderr)
    if r.dry_run:
        return
    from . import switch_web
    state = {
        "offset": a0, "start": t0, "end": b0, "fps": fps, "out_h": cfg.height, "tr": cfg.transition,
        "ease": cfg.zoom_ease, "levels": list(cfg.zoom_levels), "settle": cfg.zoom_settle,
        "segs": [[s.a, s.b, s.cam] for s in segs],
        "cams": {k: {"name": c.name, "h": (c.info.height if c.info else 0)} for k, c in cfg.cams.items()},
    }

    def tracks_now() -> dict:
        # the shots' framings come from the marks and the shots file, so read both fresh each time
        return all_tracks(cfg, load_marks(cfg)[0], show_start, show_end, fps)

    switch_web.serve_zoom(proxy, cfg.zoom_log, state, tracks_fn=tracks_now, shots_path=cfg.shots)
    cmd_status(cfg, r)


# --------------------------------------------------------------------------
# rendering
# --------------------------------------------------------------------------

def _enc(cfg: Cfg, profile: str) -> list:
    return video_encoder_args(cfg.encoder, "delivery" if profile == "final" else "preview")


def _seg_job(cfg: Cfg, seg: Seg, w: int, h: int, fps: float, profile: str, out: Path,
             fade_in: float = 0.0, vf: Optional[str] = None) -> list:
    cam = cfg.cam(seg.cam)
    vf = vf or _vf(cfg, cam.info, w, h, fps)
    if fade_in:
        vf += f",fade=t=in:st=0:d={fade_in:.3f}"
    cmd = [FFMPEG, "-hide_banner", "-y", "-v", "error"] + hwaccel_input_args()
    cmd += ["-ss", f"{cam.src(seg.a):.3f}", "-i", cam.file, "-t", f"{seg.dur:.3f}",
            "-map", "0:v:0", "-an", "-vf", vf,
            "-r", f"{fps:g}", "-fps_mode", "cfr"] + _enc(cfg, profile) + container_args(out) + [str(out)]
    return cmd


def _tr_job(cfg: Cfg, t: float, c1: str, c2: str, d: float, w: int, h: int, fps: float,
            profile: str, out: Path, vfa: Optional[str] = None, vfb: Optional[str] = None) -> list:
    a, b = cfg.cam(c1), cfg.cam(c2)
    read = d + 2 / fps
    cmd = [FFMPEG, "-hide_banner", "-y", "-v", "error"]
    cmd += hwaccel_input_args() + ["-ss", f"{a.src(t - d / 2):.3f}", "-t", f"{read:.3f}", "-i", a.file]
    cmd += hwaccel_input_args() + ["-ss", f"{b.src(t - d / 2):.3f}", "-t", f"{read:.3f}", "-i", b.file]
    fa = (vfa or _vf(cfg, a.info, w, h, fps)) + ",settb=AVTB,setpts=PTS-STARTPTS"
    fb = (vfb or _vf(cfg, b.info, w, h, fps)) + ",settb=AVTB,setpts=PTS-STARTPTS"
    fc = f"[0:v]{fa}[x];[1:v]{fb}[y];[x][y]xfade=transition=fade:duration={d:.3f}:offset=0[v]"
    cmd += ["-filter_complex", fc, "-map", "[v]", "-an", "-t", f"{d:.3f}",
            "-r", f"{fps:g}", "-fps_mode", "cfr"] + _enc(cfg, profile) + container_args(out) + [str(out)]
    return cmd


def _audio_job(cfg: Cfg, a: float, b: float, out: Path, fade_in: float = 0.0) -> list:
    cam = cfg.cam(cfg.audio)
    af = ["-af", f"afade=t=in:st=0:d={fade_in:.3f}"] if fade_in else []
    return [FFMPEG, "-hide_banner", "-y", "-v", "error", "-ss", f"{cam.src(a):.3f}", "-i", cam.file,
            "-t", f"{b - a:.3f}", "-vn", "-map", "0:a:0"] + af + ["-c:a", "aac", "-b:a", "192k",
            "-ar", "48000", "-ac", "2", str(out)]


def _done_already(path: Path, dur: float, tol: float = 0.2) -> bool:
    if not path.exists() or path.stat().st_size == 0:
        return False
    v = verify(path, expect_duration=dur, expect_video=path.suffix == ".mp4",
               expect_audio=None, tolerance=tol)
    return v.ok


def render_range(cfg: Cfg, r: Runner, *, a: float, b: float, profile: str, out: Path,
                 with_intro: bool, keep_segments: bool = False, zoom: bool = True) -> str:
    """Encode the pieces (once, in parallel), then stream-copy them together.
    profile: final (full size), preview (640x360), proxy (960x540, what the zoom page plays)."""
    cfg.probe_all()
    fps = cfg.show_fps()
    shots = load_shots(cfg.shots)
    marks, ss, se = load_marks(cfg, shots)
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
    if profile != "final" and d > 0:
        d = snap(min(d, 0.5))

    w, h = {"preview": (640, 360), "proxy": (960, 540)}.get(profile, (cfg.width, cfg.height))
    tracks = all_tracks(cfg, marks, show_start, show_end, fps, shots) if zoom else {}
    zoomed = 0
    wd = cfg.work / profile
    wd.mkdir(parents=True, exist_ok=True)

    # fade through black between the intro and the show (separates the made thing from the real one)
    fi = snap(cfg.intro_fade) if (with_intro and cfg.intro and cfg.intro_fade > 0) else 0.0

    # the pieces, in order
    pieces: list = []       # (path, expected_duration, cmd)
    n = len(segs)
    for i, s in enumerate(segs):
        pa = s.a + (d / 2 if (i > 0 and d) else 0.0)
        pb = s.b - (d / 2 if (i < n - 1 and d) else 0.0)
        seg = Seg(snap(pa), snap(pb), s.cam)
        fade_in = fi if i == 0 else 0.0
        vf, tag = _cam_vf(cfg, cfg.cam(seg.cam), seg.a, seg.b, w, h, fps, tracks)
        zoomed += bool(tag)
        path = wd / f"seg_{seg.a:09.3f}_{seg.b:09.3f}_c{seg.cam}{tag}{'_fi' if fade_in else ''}.mp4"
        pieces.append((path, seg.dur, _seg_job(cfg, seg, w, h, fps, profile, path, fade_in=fade_in, vf=vf)))
        if d and i < n - 1:
            t = s.b
            nxt = segs[i + 1]
            vfa, ta = _cam_vf(cfg, cfg.cam(s.cam), t - d / 2, t + d / 2, w, h, fps, tracks)
            vfb, tb = _cam_vf(cfg, cfg.cam(nxt.cam), t - d / 2, t + d / 2, w, h, fps, tracks)
            path = wd / f"tr_{t:09.3f}_c{s.cam}{ta}_c{nxt.cam}{tb}.mp4"
            pieces.append((path, d, _tr_job(cfg, t, s.cam, nxt.cam, d, w, h, fps, profile, path, vfa, vfb)))

    audio_path = wd / f"audio_{a:09.3f}_{b:09.3f}{'_fi' if fi else ''}.m4a"
    audio_job = _audio_job(cfg, a, b, audio_path, fade_in=fi)

    intro_v = intro_a = None
    intro_dur = 0.0
    if with_intro and cfg.intro:
        ii = probe(cfg.intro, gop=False)
        intro_dur = snap(ii.duration)
        intro_v = wd / "intro_v.mp4"
        intro_a = wd / "intro_a.m4a"
        vf = _vf(cfg, ii, w, h, fps)
        if fi:
            vf += f",fade=t=out:st={max(0.0, intro_dur - fi):.3f}:d={fi:.3f}"
        iv = [FFMPEG, "-hide_banner", "-y", "-v", "error"] + hwaccel_input_args() + [
            "-i", cfg.intro, "-t", f"{intro_dur:.3f}", "-map", "0:v:0", "-an",
            "-vf", vf, "-r", f"{fps:g}", "-fps_mode", "cfr"] + \
            _enc(cfg, profile) + container_args(intro_v) + [str(intro_v)]
        afade = ["-af", f"afade=t=out:st={max(0.0, intro_dur - fi):.3f}:d={fi:.3f}"] if fi else []
        if ii.has_audio:
            ia = [FFMPEG, "-hide_banner", "-y", "-v", "error", "-i", cfg.intro, "-t", f"{intro_dur:.3f}",
                  "-vn", "-map", "0:a:0"] + afade + ["-c:a", "aac", "-b:a", "192k", "-ar", "48000", "-ac", "2", str(intro_a)]
        else:
            ia = [FFMPEG, "-hide_banner", "-y", "-v", "error", "-f", "lavfi", "-i", "anullsrc=r=48000:cl=stereo",
                  "-t", f"{intro_dur:.3f}", "-c:a", "aac", "-b:a", "96k", str(intro_a)]

    total_enc = sum(p[1] for p in pieces)
    r.say(f"{profile}: {len(segs)} segments, {len(pieces)} pieces, {total_enc / 60:.1f} min of video to encode "
          f"at {w}x{h}@{fps:g}, {cfg.workers} at a time" + (f", + {intro_dur:.0f}s intro" if intro_v else "") +
          (f", {zoomed} segments zoomed" if zoomed else ""))

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
    if play and not r.dry_run:
        mpv = shutil.which("mpv")
        if mpv:
            subprocess.call([mpv, "--really-quiet", "--title=ffkit preview", str(out)])
        else:
            import webbrowser
            webbrowser.open(out.resolve().as_uri())
    return str(out)


def cmd_render(cfg: Cfg, r: Runner, *, keep_segments: bool = False, start: Optional[float] = None,
               end: Optional[float] = None, out: Optional[str] = None) -> str:
    marks, ss, se = resolve(parse_log(cfg.log))
    if not marks:
        raise FFKitError("no marks yet; run `ffkit switch mark` first")
    show_start = ss if ss is not None else 0.0
    show_end = se if se is not None else cfg.show_end_default()
    if start is None and end is None:
        dest = Path(os.path.expanduser(out or cfg.output))
        render_range(cfg, r, a=show_start, b=show_end, profile="final", out=dest,
                     with_intro=True, keep_segments=keep_segments)
        r.say(f"final: {dest}")
        return str(dest)
    # One stretch at full quality, without the intro: for checking a song before the show is
    # finished. Its pieces are kept, so rendering it again (or the whole show later) reuses
    # every shot whose cut and zoom haven't changed.
    a = start if start is not None else show_start
    b = end if end is not None else show_end
    tag = lambda t: _fmt(t).replace(":", ".")
    dest = Path(os.path.expanduser(out)) if out else cfg.base / "out" / f"clip_{tag(a)}-{tag(b)}.mp4"
    render_range(cfg, r, a=a, b=b, profile="final", out=dest, with_intro=False, keep_segments=True)
    r.say(f"clip {_fmt(a)} -> {_fmt(b)} at full quality: {dest}")
    return str(dest)


# --------------------------------------------------------------------------
# CLI glue
# --------------------------------------------------------------------------

def add_parser(sub) -> None:
    p = sub.add_parser("switch", help="live-switch a multi-camera recording: grid, mark, preview, render")
    p.add_argument("-f", "--file", default="switch.yaml", help="config (default switch.yaml)")
    ss = p.add_subparsers(dest="switch_cmd", required=True)
    ss.add_parser("init", help="write a switch.yaml to fill in")
    q = ss.add_parser("sync", help="work out camera offsets from their audio (needs numpy)")
    q.add_argument("--window", type=float, default=None, help="seconds of each file to scan (default: all of it)")
    q.add_argument("--max-lag", type=float, default=None, help="largest offset to consider, seconds (default: any)")
    q.add_argument("--no-write", action="store_true")
    ss.add_parser("grid", help="build the side-by-side proxy")
    q = ss.add_parser("mark", help="play the grid in your browser (or mpv) and press a key per shot; shift+key sets a shot up")
    q.add_argument("--from", dest="start_from", help="start here instead of resuming (re-marks that stretch)")
    q.add_argument("--web", action="store_true", help="mark in the browser even if mpv is installed")
    q = ss.add_parser("zoom", help="second pass in your browser: touch up the framing of whichever camera is on")
    q.add_argument("--from", dest="start_from", help="start here instead of resuming (redoes that stretch)")
    ss.add_parser("status", help="how much is marked, camera share, long holds")
    q = ss.add_parser("preview", help="fast low-res render of what you just marked (or a range)")
    q.add_argument("--from", dest="start")
    q.add_argument("--to", dest="end")
    q.add_argument("--all", action="store_true", help="the whole show")
    q.add_argument("--no-play", action="store_true")
    q = ss.add_parser("render", help="frame-accurate final with transitions and the intro")
    q.add_argument("--keep-segments", action="store_true")
    q.add_argument("--from", dest="start", help="only this stretch, at full quality and without the intro (to check a song)")
    q.add_argument("--to", dest="end")
    q.add_argument("--out", help="where to write it (default: switch.yaml's output; out/clip_FROM-TO.mp4 for a stretch)")


LAST = Path.home() / ".ffkit_switch_last"


def _find_cfg(given: str) -> str:
    """-f if given; else switch.yaml here or in a parent; else the one used last time."""
    if given != "switch.yaml" or Path(given).exists():
        return given
    here = Path.cwd()
    for d in [here] + list(here.parents):
        if (d / "switch.yaml").exists():
            return str(d / "switch.yaml")
    if LAST.exists():
        last = LAST.read_text().strip()
        if last and Path(last).exists():
            print(f"using {last}", file=sys.stderr)
            return last
    raise FFKitError("no switch.yaml here; run `ffkit switch init` or pass -f path/to/switch.yaml")


def main(a, dry_run: bool, verbose: bool) -> int:
    if a.switch_cmd == "init":
        cmd_init(a.file)
        return 0
    cfg = load_cfg(_find_cfg(a.file))
    try:
        LAST.write_text(str(cfg.path))
    except OSError:
        pass
    r = Runner(dry_run=dry_run, verbose=verbose, job_name="switch-" + a.switch_cmd)
    if a.switch_cmd == "sync":
        cmd_sync(cfg, r, window=a.window, max_lag=a.max_lag, write=not a.no_write)
    elif a.switch_cmd == "grid":
        cmd_grid(cfg, r)
    elif a.switch_cmd == "mark":
        cmd_mark(cfg, r, start_from=_parse_time(a.start_from), web=a.web)
    elif a.switch_cmd == "zoom":
        cmd_zoom(cfg, r, start_from=_parse_time(a.start_from))
    elif a.switch_cmd == "status":
        cmd_status(cfg, r)
    elif a.switch_cmd == "preview":
        cmd_preview(cfg, r, start=_parse_time(a.start), end=_parse_time(a.end),
                    play=not a.no_play, whole=a.all)
    elif a.switch_cmd == "render":
        cmd_render(cfg, r, keep_segments=a.keep_segments, start=_parse_time(a.start),
                   end=_parse_time(a.end), out=a.out)
    return 0
