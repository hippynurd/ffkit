"""ffkit command line. `python -m ffkit <command> ...` or the `ffkit` entry point."""
from __future__ import annotations

import argparse
import sys

from .core import FFKitError, Runner, capabilities, to_json
from . import ops, runner


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="ffkit",
                                 description="Copy-first FFmpeg toolkit: probe, trim, concat, merge, remux, timelapse, run.")
    ap.add_argument("-n", "--dry-run", action="store_true", help="print commands, run nothing")
    ap.add_argument("-v", "--verbose", action="store_true", help="keep ffmpeg output in the log")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("probe", help="codec, size, fps, duration, GOP, VFR for files")
    p.add_argument("files", nargs="+")
    p.add_argument("--json", action="store_true")
    p.add_argument("--no-gop", action="store_true", help="skip keyframe-spacing measurement (faster)")

    p = sub.add_parser("trim", help="cut a range out (stream copy; --exact re-encodes once)")
    p.add_argument("input"); p.add_argument("output")
    p.add_argument("-s", "--start", default="0")
    g = p.add_mutually_exclusive_group()
    g.add_argument("-e", "--end"); g.add_argument("-d", "--duration")
    p.add_argument("--exact", action="store_true", help="frame-accurate (re-encodes video once)")
    p.add_argument("--encoder", default="h264", choices=["h264", "hevc", "prores"])

    p = sub.add_parser("concat", help="join clips (demuxer + stream copy; refuses mismatches)")
    p.add_argument("output"); p.add_argument("inputs", nargs="+")
    p.add_argument("--normalize", action="store_true",
                   help="encode mismatched clips once to a mezzanine, then concat-copy")
    p.add_argument("--encoder", default="h264", choices=["h264", "hevc", "prores"])
    p.add_argument("--mezz-dir")

    p = sub.add_parser("merge", help="video from one file + audio from another (stream copy)")
    p.add_argument("output")
    p.add_argument("--video", required=True); p.add_argument("--audio", required=True)
    p.add_argument("--offset", type=float, help="shift audio by seconds (+ = later)")
    p.add_argument("--no-shortest", action="store_true", help="do not cut to the shorter stream")
    p.add_argument("--keep-original-audio", action="store_true")

    p = sub.add_parser("remux", help="change container, streams untouched")
    p.add_argument("input"); p.add_argument("output")
    p.add_argument("--fix-ts", action="store_true", help="regenerate timestamps (+genpts)")

    p = sub.add_parser("timelapse", help="keep 1 frame in N, drop audio (the one encode)")
    p.add_argument("input"); p.add_argument("output")
    p.add_argument("--every", type=int, default=30)
    p.add_argument("--fps", type=float, default=30)
    p.add_argument("--mode", default="auto", choices=["auto", "keyframe", "select"])
    p.add_argument("--encoder", default="h264", choices=["h264", "hevc", "prores"])
    p.add_argument("--quality", default="delivery", choices=["delivery", "mezzanine"])
    p.add_argument("--keep-audio", action="store_true", help="speed audio up to match (atempo)")

    p = sub.add_parser("normalize", help="the one encode for mismatched sources -> mezzanine")
    p.add_argument("input"); p.add_argument("output")
    p.add_argument("--fps", type=float); p.add_argument("--width", type=int); p.add_argument("--height", type=int)
    p.add_argument("--encoder", default="h264", choices=["h264", "hevc", "prores"])

    p = sub.add_parser("run", help="run a job file (yaml/json)")
    p.add_argument("job")
    p.add_argument("--force", action="store_true", help="redo outputs that already exist")

    sub.add_parser("caps", help="show what this ffmpeg build can do (hw decode/encode)")

    a = ap.parse_args(argv)
    try:
        if a.cmd == "caps":
            print(__import__("json").dumps(capabilities(), indent=2))
            return 0
        if a.cmd == "probe":
            infos = ops.op_probe(a.files, gop=not a.no_gop)
            if a.json:
                print(to_json(infos))
            else:
                for i in infos:
                    print(i.summary())
                    for n in i.notes:
                        print("    note: " + n)
            return 0
        if a.cmd == "run":
            runner.run_job(a.job, dry_run=a.dry_run, verbose=a.verbose, force=a.force)
            return 0

        r = Runner(dry_run=a.dry_run, verbose=a.verbose, job_name=a.cmd)
        if a.cmd == "trim":
            ops.op_trim(r, a.input, a.output, start=a.start, end=a.end, duration=a.duration,
                        exact=a.exact, encoder=a.encoder)
        elif a.cmd == "concat":
            ops.op_concat(r, a.inputs, a.output, normalize=a.normalize, encoder=a.encoder,
                          mezz_dir=a.mezz_dir)
        elif a.cmd == "merge":
            ops.op_merge(r, a.output, video=a.video, audio=a.audio, shortest=not a.no_shortest,
                         offset=a.offset, replace_audio=not a.keep_original_audio)
        elif a.cmd == "remux":
            ops.op_remux(r, a.input, a.output, fix_ts=a.fix_ts)
        elif a.cmd == "timelapse":
            ops.op_timelapse(r, a.input, a.output, every=a.every, fps=a.fps, mode=a.mode,
                             encoder=a.encoder, quality=a.quality, keep_audio=a.keep_audio)
        elif a.cmd == "normalize":
            ops.op_normalize(r, a.input, a.output, encoder=a.encoder, fps=a.fps,
                             width=a.width, height=a.height)
        r.say(f"log: {r.log_path}")
        return 0
    except FFKitError as e:
        print(f"ffkit: {e}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
