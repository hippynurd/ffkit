# ffkit

A copy-first FFmpeg toolkit for batch video work: probe, trim, concat, merge, remux,
timelapse, and a job runner. Stream copy by default; one encode per pipeline when
something has to change; probe before acting; dry-run; verify every output.

## Install

```
cd ffkit
pip install -e ".[yaml]"      # or: python -m ffkit ... without installing
```

Needs `ffmpeg` and `ffprobe` on PATH. On a Mac, `brew install ffmpeg` gives you the
VideoToolbox hardware encoders; `ffkit caps` shows what's usable.

## Commands

```
ffkit probe   clip1.mp4 clip2.mov            # codec, size, fps, duration, GOP, VFR
ffkit trim    in.mp4 out.mp4 -s 0:10 -e 1:20 # stream copy; --exact re-encodes once
ffkit concat  out.mp4 a.mp4 b.mp4 c.mp4      # demuxer + copy; refuses mismatches
ffkit concat  out.mp4 *.mp4 --normalize      # encodes only mismatched clips, once
ffkit merge   out.mp4 --video v.mp4 --audio music.m4a
ffkit remux   in.mov out.mp4                 # container swap, streams untouched
ffkit timelapse in.mp4 out.mp4 --every 30 --fps 30
ffkit run     jobs/beach.yaml                # -n to dry-run, --force to redo
ffkit caps                                   # hardware encode/decode available
```

Add `-n` before the command for a dry run (prints commands, runs nothing).
Every run writes `ffkit-logs/<timestamp>-<job>.log`.

## Job files

```yaml
name: beach-day
workers: 4
steps:
  - op: probe
    inputs: ["src/*.MP4"]
  - op: each                      # one op per file, in parallel
    inputs: ["src/*.MP4"]
    do:
      op: timelapse
      output: "work/{stem}_tl.mp4"
      every: 30
      fps: 30
  - op: trim
    input: src/intro.MP4
    output: work/intro.mp4
    start: "0:05"
    end: "0:45"
  - op: concat
    inputs: ["work/intro.mp4", "work/*_tl.mp4"]
    output: out/final.mp4
    normalize: true
  - op: merge
    video: out/final.mp4
    audio: music/track.m4a
    output: out/final_music.mp4
```

Outputs that exist and verify are skipped on re-run. Paths are relative to the job file.

## Why it's built this way

See `AGENTS.md` for the rules, written for a coding agent but true for people too.

The reference page (each method, why each flag is where it is, where it breaks, the
wrong way) is `docs/index.html`.


