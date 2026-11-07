"""
ffkit job runner: a job file in, a finished folder out.

Job file (YAML or JSON):

    name: beach-day
    workers: 4                      # parallelism for "each" steps
    steps:
      - op: probe
        inputs: ["src/*.MP4"]

      - op: each                    # run one op per matching file, in parallel
        inputs: ["src/*.MP4"]
        do:
          op: timelapse
          output: "work/{stem}_tl.mp4"
          every: 30
          fps: 30

      - op: trim
        input: src/intro.MP4
        output: work/intro_cut.mp4
        start: "0:05"
        end: "0:45"

      - op: concat
        inputs: ["work/intro_cut.mp4", "work/*_tl.mp4"]
        output: out/final.mp4
        normalize: true

      - op: merge
        video: out/final.mp4
        audio: music/track.m4a
        output: out/final_music.mp4

Placeholders available in "each": {name} (file name), {stem}, {ext}, {parent}, {index}.
Outputs that already exist and verify clean are skipped (resume). --force redoes them.
"""
from __future__ import annotations

import glob
import json
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from .core import FFKitError, Runner, verify
from . import ops

try:
    import yaml  # type: ignore
except Exception:  # pragma: no cover
    yaml = None


def load_job(path: str) -> dict:
    p = Path(path)
    text = p.read_text()
    if p.suffix.lower() in {".yaml", ".yml"}:
        if yaml is None:
            raise FFKitError("PyYAML not installed; use a .json job file or pip install pyyaml")
        return yaml.safe_load(text)
    return json.loads(text)


def expand(patterns, base: Path, r: Runner | None = None) -> list[str]:
    if isinstance(patterns, str):
        patterns = [patterns]
    out: list[str] = []
    for pat in patterns:
        pat = str(base / pat) if not Path(pat).is_absolute() else pat
        hits = sorted(glob.glob(pat))
        if not hits and not any(ch in pat for ch in "*?["):
            hits = [pat]  # literal path; let the op report it missing
        if not hits and r is not None:
            r.say(f"# pattern matched 0 files: {pat}" + (" (not produced yet in dry-run)" if r.dry_run else ""))
        out.extend(hits)
    return out


def _resolve(path: str, base: Path) -> str:
    return path if Path(path).is_absolute() else str(base / path)


def _already_done(r: Runner, out: str, force: bool) -> bool:
    if force or r.dry_run:
        return False
    p = Path(out)
    if not p.exists():
        return False
    v = verify(out, expect_video=True)
    if v.ok:
        r.say(f"skip {p.name} (exists and verifies; use --force to redo)")
        return True
    r.say(f"redo {p.name} (exists but failed verification: {'; '.join(v.messages)})")
    return False


def run_step(r: Runner, step: dict, base: Path, *, force: bool = False) -> None:
    op = step.get("op")
    if not op:
        raise FFKitError(f"step without op: {step}")

    if op == "probe":
        files = expand(step.get("inputs", []), base, r)
        for i in ops.op_probe(files, gop=step.get("gop", True)):
            r.say("    " + i.summary())
        return

    if op == "each":
        files = expand(step.get("inputs", []), base, r)
        template = step.get("do") or {}
        workers = int(step.get("workers") or 1)
        jobs = []
        for idx, f in enumerate(files):
            fp = Path(f)
            fields = {"name": fp.name, "stem": fp.stem, "ext": fp.suffix,
                      "parent": str(fp.parent), "index": idx}
            sub = {k: (v.format(**fields) if isinstance(v, str) else v) for k, v in template.items()}
            sub.setdefault("input", f)
            jobs.append(sub)
        if workers <= 1 or r.dry_run:
            for sub in jobs:
                run_step(r, sub, base, force=force)
            return
        with ThreadPoolExecutor(max_workers=workers) as pool:
            futs = {pool.submit(run_step, r, sub, base, force=force): sub for sub in jobs}
            errors = []
            for fut in as_completed(futs):
                try:
                    fut.result()
                except FFKitError as e:
                    errors.append(str(e))
            if errors:
                raise FFKitError("each: " + " | ".join(errors))
        return

    out = step.get("output")
    if not out:
        raise FFKitError(f"{op} step needs an output")
    out = _resolve(out, base)
    if _already_done(r, out, force):
        return

    if op == "trim":
        ops.op_trim(r, _resolve(step["input"], base), out,
                    start=step.get("start"), end=step.get("end"), duration=step.get("duration"),
                    exact=bool(step.get("exact", False)), encoder=step.get("encoder", "h264"))
    elif op == "concat":
        ops.op_concat(r, expand(step.get("inputs", []), base, r), out,
                      normalize=bool(step.get("normalize", False)),
                      encoder=step.get("encoder", "h264"), mezz_dir=step.get("mezz_dir"))
    elif op == "merge":
        ops.op_merge(r, out, video=_resolve(step["video"], base), audio=_resolve(step["audio"], base),
                     shortest=bool(step.get("shortest", True)), offset=step.get("offset"),
                     replace_audio=bool(step.get("replace_audio", True)))
    elif op == "remux":
        ops.op_remux(r, _resolve(step["input"], base), out, fix_ts=bool(step.get("fix_ts", False)))
    elif op == "timelapse":
        ops.op_timelapse(r, _resolve(step["input"], base), out,
                         every=int(step.get("every", 30)), fps=float(step.get("fps", 30)),
                         mode=step.get("mode", "auto"), encoder=step.get("encoder", "h264"),
                         quality=step.get("quality", "delivery"),
                         keep_audio=bool(step.get("keep_audio", False)))
    elif op == "normalize":
        ops.op_normalize(r, _resolve(step["input"], base), out,
                         encoder=step.get("encoder", "h264"), fps=step.get("fps"),
                         width=step.get("width"), height=step.get("height"))
    else:
        raise FFKitError(f"unknown op: {op}")


def run_job(path: str, *, dry_run: bool = False, verbose: bool = False, force: bool = False) -> None:
    job = load_job(path)
    base = Path(path).resolve().parent
    name = job.get("name") or Path(path).stem
    r = Runner(dry_run=dry_run, verbose=verbose, job_name=name)
    r.say(f"== job {name} ({'dry run' if dry_run else 'live'}) base={base}")
    steps = job.get("steps") or []
    default_workers = int(job.get("workers") or 1)
    for n, step in enumerate(steps, 1):
        if step.get("op") == "each" and "workers" not in step:
            step["workers"] = default_workers
        r.say(f"-- step {n}/{len(steps)}: {step.get('op')}")
        run_step(r, step, base, force=force)
    r.say(f"== done in {__import__('time').time() - r.started:.1f}s; log: {r.log_path}")
