"""
ffkit operations. Each function builds one ffmpeg invocation (or a short, explicit
sequence), runs it through a Runner, and verifies the result.

Every function returns the output path on success and raises FFKitError otherwise.
"""
from __future__ import annotations

import os
import tempfile
from pathlib import Path
from typing import Optional

from .core import (FFMPEG, FFKitError, Runner, VideoInfo, probe, probe_many, verify,
                   hwaccel_input_args, video_encoder_args, container_args)


def _hms(seconds: float) -> str:
    return f"{seconds:.3f}"


def _parse_time(t: str | float | int | None) -> float | None:
    """'90', '1:30', '0:01:30.5' -> seconds."""
    if t is None or t == "":
        return None
    if isinstance(t, (int, float)):
        return float(t)
    parts = [float(x) for x in str(t).split(":")]
    secs = 0.0
    for p in parts:
        secs = secs * 60 + p
    return secs


def _ensure_parent(out: str | Path) -> None:
    Path(out).parent.mkdir(parents=True, exist_ok=True)


def _probe(r: Runner, path: str, **kw) -> VideoInfo:
    """Probe, or in dry-run mode return a placeholder for files a prior step would make."""
    if r.dry_run and not Path(path).exists():
        r.say(f"# (dry-run) {Path(path).name} does not exist yet; assuming video+audio")
        return VideoInfo(path=path, has_video=True, has_audio=True, duration=0.0, fps=30.0,
                         fps_str="30/1", gop=0, notes=["placeholder"])
    return probe(path, **kw)


def _done(r: Runner, out: str | Path, verdict) -> str:
    if r.dry_run:
        return str(out)
    if not verdict.ok:
        raise FFKitError(f"verification failed for {out}: " + "; ".join(verdict.messages))
    r.say(f"ok  {verdict.info.summary()}")
    return str(out)


# --------------------------------------------------------------------------
# probe
# --------------------------------------------------------------------------

def op_probe(paths: list[str], *, gop: bool = True) -> list[VideoInfo]:
    return probe_many(paths, gop=gop)


# --------------------------------------------------------------------------
# trim
# --------------------------------------------------------------------------

def op_trim(r: Runner, src: str, out: str, *, start=None, end=None, duration=None,
            exact: bool = False, encoder: str = "h264") -> str:
    """
    Cut [start, end) out of src.

    Copy mode (default): -ss BEFORE -i seeks the demuxer to the keyframe at or before
    `start`, then -c copy. Fast as a file copy. The cut lands on a keyframe, so it may
    begin up to one GOP early. -avoid_negative_ts make_zero keeps timestamps sane.

    Exact mode (--exact): frame-accurate. -ss before -i for the fast seek, then the
    decoder trims precisely and we re-encode video once; audio is still copied.
    """
    s = _parse_time(start) or 0.0
    e = _parse_time(end)
    d = _parse_time(duration)
    if e is not None and d is not None:
        raise FFKitError("give --end or --duration, not both")
    if e is not None:
        d = e - s
        if d <= 0:
            raise FFKitError("end must be after start")

    info = _probe(r, src)
    if d is None:
        d = max(0.0, info.duration - s)

    _ensure_parent(out)
    cmd = [FFMPEG, "-hide_banner", "-y", "-v", "error", "-stats"]
    if exact:
        cmd += hwaccel_input_args()
    cmd += ["-ss", _hms(s), "-i", src, "-t", _hms(d)]
    if exact:
        cmd += ["-map", "0:v:0"] + (["-map", "0:a?"] if info.has_audio else [])
        cmd += video_encoder_args(encoder, "mezzanine") + ["-c:a", "copy"]
        why = f"trim (exact, one encode) {s:.2f}s +{d:.2f}s"
    else:
        cmd += ["-map", "0", "-c", "copy", "-avoid_negative_ts", "make_zero"]
        why = f"trim (stream copy, keyframe-aligned; gop={info.gop or '?'}) {s:.2f}s +{d:.2f}s"
    cmd += container_args(out) + [out]
    r.run(cmd, why=why)

    # copy mode can start early by up to one GOP, so tolerance scales with it
    tol = 0.75 if exact else max(0.75, (info.gop or 30) / max(info.fps, 1.0) + 0.5)
    v = verify(out, expect_duration=d, expect_audio=info.has_audio or None, tolerance=tol)
    return _done(r, out, v)


# --------------------------------------------------------------------------
# concat
# --------------------------------------------------------------------------

def concat_mismatches(infos: list[VideoInfo]) -> list[str]:
    """Human-readable reasons these files cannot be concat-copied."""
    base = infos[0]
    problems = []
    fields = [("vcodec", "video codec"), ("width", "width"), ("height", "height"),
              ("pix_fmt", "pixel format"), ("acodec", "audio codec"),
              ("sample_rate", "sample rate"), ("channels", "channels")]
    for other in infos[1:]:
        for attr, label in fields:
            a, b = getattr(base, attr), getattr(other, attr)
            if a != b:
                problems.append(f"{label}: {Path(base.path).name}={a} vs {Path(other.path).name}={b}")
        if base.fps and other.fps and abs(base.fps - other.fps) > 0.01:
            problems.append(f"fps: {Path(base.path).name}={base.fps_str} vs {Path(other.path).name}={other.fps_str}")
    for i in infos:
        if i.vfr:
            problems.append(f"{Path(i.path).name} is variable frame rate")
    return problems


def op_normalize(r: Runner, src: str, out: str, *, target: VideoInfo | None = None,
                 encoder: str = "h264", fps: float | None = None,
                 width: int | None = None, height: int | None = None) -> str:
    """
    The ONE allowed encode for mismatched sources: decode, scale/fps-match, encode to
    a mezzanine. Everything downstream of this is copy again.
    """
    info = _probe(r, src, gop=False)
    tf = fps or (target.fps if target else info.fps)
    tw = width or (target.width if target else info.width)
    th = height or (target.height if target else info.height)
    _ensure_parent(out)
    vf = [f"scale={tw}:{th}:flags=lanczos:force_original_aspect_ratio=decrease",
          f"pad={tw}:{th}:(ow-iw)/2:(oh-ih)/2", "setsar=1"]
    cmd = [FFMPEG, "-hide_banner", "-y", "-v", "error", "-stats"] + hwaccel_input_args()
    cmd += ["-i", src, "-vf", ",".join(vf), "-r", f"{tf:g}", "-fps_mode", "cfr"]
    cmd += video_encoder_args(encoder, "mezzanine")
    if info.has_audio:
        sr = (target.sample_rate if target and target.sample_rate else info.sample_rate) or 48000
        cmd += ["-c:a", "aac", "-b:a", "192k", "-ar", str(sr)]
    else:
        cmd += ["-an"]
    cmd += container_args(out) + [out]
    r.run(cmd, why=f"normalize -> {tw}x{th} @ {tf:g}fps CFR (one encode, mezzanine)")
    v = verify(out, expect_duration=info.duration, expect_audio=info.has_audio)
    return _done(r, out, v)


def _video_signature(i: VideoInfo) -> tuple:
    return (i.vcodec, i.width, i.height, i.pix_fmt, round(i.fps, 3))


def op_fix_audio(r: Runner, src: str, out: str, *, target: VideoInfo) -> str:
    """
    Audio-only mismatch: video is COPIED, audio is added (silence) or re-encoded to
    match the target's codec/rate/channels. Never re-encode video for an audio problem.
    """
    info = _probe(r, src, gop=False)
    sr = target.sample_rate or 48000
    ch = target.channels or 2
    _ensure_parent(out)
    cmd = [FFMPEG, "-hide_banner", "-y", "-v", "error", "-stats", "-i", src]
    if not info.has_audio:
        layout = {1: "mono", 2: "stereo", 6: "5.1"}.get(ch, "stereo")
        cmd += ["-f", "lavfi", "-i", f"anullsrc=r={sr}:cl={layout}",
                "-map", "0:v:0", "-map", "1:a:0", "-shortest"]
        why = "add silent audio track to match (video copy)"
    else:
        cmd += ["-map", "0:v:0", "-map", "0:a:0"]
        why = f"re-encode audio only -> aac {sr}Hz {ch}ch (video copy)"
    cmd += ["-c:v", "copy", "-c:a", "aac", "-b:a", "192k", "-ar", str(sr), "-ac", str(ch)]
    cmd += container_args(out) + [out]
    r.run(cmd, why=why)
    v = verify(out, expect_duration=info.duration, expect_audio=True)
    return _done(r, out, v)


def op_concat(r: Runner, inputs: list[str], out: str, *, normalize: bool = False,
              encoder: str = "h264", mezz_dir: str | None = None) -> str:
    """
    Join clips with the concat DEMUXER and -c copy: no decode, no encode, copy speed.

    Requires every input to share codec/resolution/fps/pixel format/audio layout. We
    probe first and refuse otherwise, unless normalize=True, in which case mismatched
    inputs are encoded ONCE to a mezzanine matching the first clip, then concat-copied.
    """
    if len(inputs) < 2:
        raise FFKitError("concat needs at least two inputs")
    infos = [_probe(r, p, gop=False) for p in inputs]
    for i in infos:
        r.say("in  " + i.summary())
    placeholders = any("placeholder" in i.notes for i in infos)
    problems = [] if placeholders else concat_mismatches(infos)
    if placeholders:
        r.say("# (dry-run) some inputs are not produced yet; mismatch check deferred to the live run")
    sources = list(inputs)
    if problems:
        if not normalize:
            raise FFKitError("inputs cannot be concat-copied:\n  " + "\n  ".join(problems) +
                             "\nre-run with --normalize to encode mismatched clips once to a mezzanine")
        r.say("mismatch detected; fixing non-matching clips to match the first:")
        # The target: first clip's video; audio present if ANY clip has audio, using the
        # first clip that has it as the reference for codec/rate/channels.
        base = infos[0]
        aref = next((i for i in infos if i.has_audio), None)
        target = VideoInfo(path=base.path, has_video=True, vcodec=base.vcodec, width=base.width,
                           height=base.height, pix_fmt=base.pix_fmt, fps=base.fps, fps_str=base.fps_str,
                           has_audio=aref is not None,
                           acodec=aref.acodec if aref else "", sample_rate=aref.sample_rate if aref else 0,
                           channels=aref.channels if aref else 0)
        mz = Path(mezz_dir or Path(out).parent / "mezzanine")
        mz.mkdir(parents=True, exist_ok=True)
        for idx, (p, i) in enumerate(zip(inputs, infos)):
            video_ok = _video_signature(i) == _video_signature(target) and not i.vfr
            audio_ok = (not target.has_audio) or (
                i.has_audio and i.acodec == target.acodec and i.sample_rate == target.sample_rate
                and i.channels == target.channels)
            if video_ok and audio_ok:
                continue
            m = mz / (Path(p).stem + ".mezz.mp4")
            if video_ok:
                op_fix_audio(r, p, str(m), target=target)          # video copy, audio only
            else:
                op_normalize(r, p, str(m), target=target, encoder=encoder)  # the one encode
                if target.has_audio and not i.has_audio:
                    m2 = mz / (Path(p).stem + ".mezz.a.mp4")
                    op_fix_audio(r, str(m), str(m2), target=target)
                    m = m2
            sources[idx] = str(m)
        infos_after = [_probe(r, s, gop=False) for s in sources]
        leftover = concat_mismatches(infos_after)
        if leftover and not r.dry_run:
            raise FFKitError("still mismatched after normalize:\n  " + "\n  ".join(leftover))

    _ensure_parent(out)
    total = sum(i.duration for i in infos)
    listfile = Path(out).with_suffix(".concat.txt")
    with listfile.open("w") as fh:
        for s in sources:
            fh.write("file " + _concat_escape(os.path.abspath(s)) + "\n")
    cmd = [FFMPEG, "-hide_banner", "-y", "-v", "error", "-stats",
           "-f", "concat", "-safe", "0", "-i", str(listfile),
           "-map", "0", "-c", "copy"] + container_args(out) + [out]
    r.run(cmd, why=f"concat {len(sources)} clips via demuxer, stream copy")
    v = verify(out, expect_duration=total, tolerance=max(1.0, 0.1 * len(sources)),
               expect_audio=infos[0].has_audio or None)
    if not r.dry_run:
        listfile.unlink(missing_ok=True)
    return _done(r, out, v)


def _concat_escape(path: str) -> str:
    return "'" + path.replace("'", "'\\''") + "'"


# --------------------------------------------------------------------------
# merge (mux streams from different files)
# --------------------------------------------------------------------------

def op_merge(r: Runner, out: str, *, video: str, audio: str, shortest: bool = True,
             offset: float | None = None, replace_audio: bool = True) -> str:
    """
    Put the video stream of one file with the audio stream of another. Pure copy.
    offset: shift the audio by N seconds (positive = audio starts later).
    """
    vi = _probe(r, video, gop=False)
    ai = _probe(r, audio, gop=False)
    if not ai.has_audio:
        raise FFKitError(f"{audio} has no audio stream")
    _ensure_parent(out)
    cmd = [FFMPEG, "-hide_banner", "-y", "-v", "error", "-stats", "-i", video]
    if offset:
        cmd += ["-itsoffset", _hms(offset)]
    cmd += ["-i", audio, "-map", "0:v:0", "-map", "1:a:0"]
    if not replace_audio and vi.has_audio:
        cmd += ["-map", "0:a?"]
    cmd += ["-c", "copy"]
    if shortest:
        cmd += ["-shortest"]
    cmd += container_args(out) + [out]
    r.run(cmd, why="merge video + audio streams, stream copy")
    exp = min(vi.duration, ai.duration) if shortest else max(vi.duration, ai.duration)
    v = verify(out, expect_duration=exp, expect_audio=True, tolerance=1.0)
    return _done(r, out, v)


# --------------------------------------------------------------------------
# remux (container swap)
# --------------------------------------------------------------------------

def op_remux(r: Runner, src: str, out: str, *, fix_ts: bool = False) -> str:
    """
    Change container without touching streams. MOV->MP4, MKV->MP4, etc.
    fix_ts: regenerate timestamps (-fflags +genpts) for files with broken PTS.
    """
    info = _probe(r, src, gop=False)
    if Path(src).resolve() == Path(out).resolve():
        raise FFKitError("remux output must differ from input")
    _ensure_parent(out)
    cmd = [FFMPEG, "-hide_banner", "-y", "-v", "error", "-stats"]
    if fix_ts:
        cmd += ["-fflags", "+genpts"]
    cmd += ["-i", src, "-map", "0", "-c", "copy"]
    if Path(out).suffix.lower() in {".mp4", ".m4v", ".mov"} and info.vcodec == "hevc":
        cmd += ["-tag:v", "hvc1"]   # makes HEVC-in-MP4 play in Apple apps
    cmd += container_args(out) + [out]
    r.run(cmd, why=f"remux {info.container.split(',')[0]} -> {Path(out).suffix}, stream copy")
    v = verify(out, expect_duration=info.duration, expect_audio=info.has_audio or None)
    return _done(r, out, v)


# --------------------------------------------------------------------------
# timelapse
# --------------------------------------------------------------------------

def op_timelapse(r: Runner, src: str, out: str, *, every: int = 30, fps: float = 30.0,
                 mode: str = "auto", encoder: str = "h264", quality: str = "delivery",
                 keep_audio: bool = False) -> str:
    """
    Keep 1 frame in `every`, play back at `fps`. No audio by default.

    This is the one step that must decode. Two ways to keep it cheap:
      keyframe mode: -skip_frame nokey tells the decoder to skip every non-key frame,
                     so we never decode the frames we throw away. Only valid when the
                     source GOP equals `every` (we check), because output rate then
                     depends on the GOP, not on us.
      select mode:   decode everything (hardware-accelerated where possible), keep
                     frame n where n % every == 0, retime with setpts.
    auto picks keyframe mode when gop == every (+/-1), else select.
    """
    info = _probe(r, src)
    if not info.has_video:
        raise FFKitError("timelapse needs a video stream")
    use_keyframes = False
    if mode == "keyframe":
        use_keyframes = True
        if info.gop and abs(info.gop - every) > 1:
            r.say(f"warning: source gop={info.gop} but every={every}; output speed will follow the GOP")
    elif mode == "auto":
        use_keyframes = bool(info.gop) and abs(info.gop - every) <= 1
    elif mode != "select":
        raise FFKitError("mode must be auto, keyframe or select")

    _ensure_parent(out)
    cmd = [FFMPEG, "-hide_banner", "-y", "-v", "error", "-stats"]
    if use_keyframes:
        cmd += ["-skip_frame", "nokey"] + hwaccel_input_args() + ["-i", src]
        vf = f"setpts=N/({fps:g}*TB)"
        why = f"timelapse 1:{every} via keyframe-only decode (gop={info.gop}) -> {fps:g}fps"
        step = info.gop
    else:
        cmd += hwaccel_input_args() + ["-i", src]
        vf = f"select='not(mod(n\\,{every}))',setpts=N/({fps:g}*TB)"
        why = f"timelapse 1:{every} via select (gop={info.gop or '?'} != every) -> {fps:g}fps"
        step = every
    cmd += ["-vf", vf, "-r", f"{fps:g}", "-fps_mode", "cfr"]
    cmd += video_encoder_args(encoder, quality)
    if keep_audio and info.has_audio:
        # audio sped up to match; atempo chains in <=2x steps
        factor = float(step)
        chain = []
        while factor > 2.0:
            chain.append("atempo=2.0")
            factor /= 2.0
        chain.append(f"atempo={factor:g}")
        cmd += ["-af", ",".join(chain), "-c:a", "aac", "-b:a", "128k"]
    else:
        cmd += ["-an"]
    cmd += container_args(out) + [out]
    r.run(cmd, why=why)

    src_frames = info.nb_frames or int(info.fps * info.duration)
    exp_frames = max(1, src_frames // step)
    exp_dur = exp_frames / fps
    v = verify(out, expect_duration=exp_dur, expect_audio=keep_audio and info.has_audio,
               tolerance=max(0.5, exp_dur * 0.05), expect_frames=exp_frames)
    return _done(r, out, v)
