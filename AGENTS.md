# AGENTS.md — rules for working on video in this repo

You are editing video with FFmpeg on behalf of someone who batch-processes footage and
cares about speed and quality. These are constraints, not suggestions. When a rule and
your instinct disagree, follow the rule and say so in your summary.

## The one rule that matters most

**Stream copy by default. Re-encode only when the output must differ from the input in
content, format, or correctness, and then exactly once per pipeline.**

A correct FFmpeg pipeline runs at the speed of copying a file. If a command is running
at 1–5x realtime on a copy-able job, it is wrong, not slow.

## Use the toolkit, not raw ffmpeg

`ffkit` lives in this repo and already encodes these rules. Reach for it first:

```
python -m ffkit probe   <files...>           # codec, size, fps, duration, GOP, VFR
python -m ffkit trim    IN OUT -s 0:10 -e 1:20 [--exact]
python -m ffkit concat  OUT a.mp4 b.mp4 ...  [--normalize]
python -m ffkit merge   OUT --video v.mp4 --audio a.m4a
python -m ffkit remux   IN OUT                # container swap
python -m ffkit timelapse IN OUT --every 30 --fps 30 [--mode auto|keyframe|select]
python -m ffkit run     job.yaml [--dry-run] [--force]
```

If a job needs something ffkit cannot do, add an op to `ffkit/ops.py` following the
pattern there (probe → build command → run → verify) rather than writing a one-off
command. One-off commands do not get the dry-run, logging, or verification.

Run with `-n` / `--dry-run` first on anything that touches more than a couple of files,
and show the user the commands before the live run.

## Hard rules, in order of how often they get broken

1. **Never add `-c:v libx264` (or any encoder) to a cut, join, mux, or container swap.**
   Those are `-c copy` jobs. If copy fails, report *why* (the probe will say) instead of
   silently re-encoding.

2. **`-ss` goes BEFORE `-i`.** After `-i` it decodes from the start of the file to the
   cut point, which is the single most common cause of "why is this trim taking a minute".
   For copy trims use `-ss` before `-i`, `-t DURATION` after, and
   `-avoid_negative_ts make_zero`. Copy trims land on keyframes and may start up to one
   GOP early; that is expected. Only use `--exact` (re-encode) when the user asks for
   frame accuracy.

3. **Probe before concat.** Clips can only be concat-copied when codec, width, height,
   pixel format, frame rate, audio codec, sample rate, and channel count all match and
   none are variable frame rate. `ffkit concat` checks this and refuses otherwise. Do not
   work around a refusal with the `concat` *filter* (which re-encodes everything); use
   `--normalize`, which encodes only the mismatched clips, once.

4. **An audio mismatch is not a reason to re-encode video.** Missing audio → add silence
   with `-c:v copy`. Wrong sample rate/codec → re-encode audio only. ffkit does this.

5. **Timelapse is the one step that must decode.** Keep it cheap:
   - no audio (`-an`) unless the user asks;
   - if the source GOP equals the frame interval, use `-skip_frame nokey` so non-key
     frames are never decoded (ffkit's `auto` mode checks the GOP and does this);
   - otherwise `select='not(mod(n,N))',setpts=N/(FPS*TB)` with hardware decode;
   - do it **once** on the original. Never timelapse a timelapse; never re-encode the
     result afterwards. Everything downstream must be copy.

6. **One encode per pipeline, placed as early as possible.** If something must be
   re-encoded (mismatched sources, VFR, filters, delivery format), do it in a single
   step to a mezzanine, and make every step after it copy-only. Two encodes in one
   pipeline is a bug unless the user explicitly asked for a delivery encode at the end.

7. **Hardware first for the encode you are allowed.** On Apple Silicon:
   `-hwaccel videotoolbox` before `-i`, `h264_videotoolbox` / `hevc_videotoolbox` /
   `prores_videotoolbox` for output. Fall back to libx264 `-crf 18` (mezzanine) or
   `-crf 23` (delivery) elsewhere. `ffkit caps` shows what the current machine can do;
   it tests the encoder rather than trusting the listing.

8. **Verify every output with ffprobe** before calling a step done: it exists, is not
   empty, has the expected streams, and its duration is within tolerance of what the
   step promised. ffkit does this and refuses to mark a step complete otherwise.

9. **Never modify or overwrite source files.** Sources are read-only. Outputs go to
   `work/` (intermediates) and `out/` (deliverables). Mezzanines go to `out/mezzanine/`.

10. **Phone and screen-recording footage is often variable frame rate.** The probe flags
    it. VFR into concat or timelapse causes drift and stutter; normalize to CFR once
    (`ffkit normalize` or `concat --normalize`) and then copy.

## Conventions

- Containers: `.mp4` for deliverables and intermediates, `-movflags +faststart` always.
- HEVC in MP4 gets `-tag:v hvc1` so Apple apps open it.
- Mezzanine codec: H.264 high quality by default (`--encoder h264`, crf 18 / VT q 75);
  `--encoder prores` when the result is going into an NLE for a long edit.
- Delivery: H.264 yuv420p, AAC 128–192k, 48 kHz.
- Job files live in `jobs/`, named for the project, YAML. Globs are relative to the
  job file. Outputs that already exist and verify are skipped on re-run (resume);
  `--force` redoes them.
- Logs: `ffkit-logs/<timestamp>-<job>.log` with every command and its exit code and
  timing. Point the user at the log, don't paste ffmpeg output into chat.

## When the rules don't apply

Re-encode, and say which of these is the reason:

- the destination cannot play the source (web, phone, projector) → delivery encode, last;
- frame-accurate cuts are required → `trim --exact`;
- inputs do not match and must be joined → `concat --normalize` (one encode each);
- variable frame rate source → normalize to CFR once;
- any filter, overlay, scale, speed change → by definition decodes; do it once;
- a master for a long edit → encode once to ProRes, then every later step is fast;
- the file is broken (timestamps, index) → try `remux --fix-ts` first, re-encode last.

## What "done" looks like

A summary that says, per step: copy or encode (and why), the command (or a pointer to
the log), and the ffprobe line of the output. If any step encoded when it could have
copied, that is not done.

## Multi-camera shows: `ffkit switch`

Choosing which camera to be on is the one decision an agent should NOT make: it
can't hear a solo or see someone walk into frame, and every attempt to encode the
rules swings between extremes. The person makes those calls live, with three keys,
and the computer does the rest.

```
ffkit switch init        # switch.yaml: camera files, which one carries the audio, intro, output
ffkit switch sync        # offsets from the audio (needs numpy); writes switch.offsets.json
ffkit switch grid        # side-by-side proxy (one preview-quality encode)
ffkit switch mark        # the human presses a key per shot (browser, or mpv for plain cameras); marks go to switch.log
                         # a key is a camera plus a framing they set up on the page (switch.shots.json), or a plain camera
ffkit switch zoom        # second pass: the human touches up framing per camera (browser); changes go to zoom.log
ffkit switch preview     # fast low-res render of what they just marked
ffkit switch render      # frame-accurate final, crossfades, intro + fade to black  (the one encode)
ffkit switch render --from T --to T   # one stretch at full quality, no intro; its pieces are kept and reused later
```

Your job is everything except `mark` and `zoom`: find the camera files, write `switch.yaml`,
run `sync` and `grid`, free disk space, and print the
one command the person runs next. Do not try to generate marks, shots or zooms yourself unless asked.
`switch.log`, `zoom.log` and `switch.shots.json` are theirs; never edit or delete them. Zoom is
applied inside the same per-shot encode as the cut (a crop from the source's full resolution),
never as an extra pass over the finished file. A shot's framing is resolved at render time from
`switch.shots.json`, so changing a shot changes every cut that used it.
