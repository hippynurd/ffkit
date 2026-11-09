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

## Multi-camera: `ffkit switch`

A live switch over recorded footage. Three cameras side by side in one window, you press
1, 2 or 3 as the show plays, the computer renders the cut.

```
ffkit switch init      # fill in switch.yaml: camera files, audio camera, intro, output
ffkit switch sync      # line the cameras up from their audio (pip install numpy)
ffkit switch grid      # build the side-by-side proxy you watch
ffkit switch mark      # your browser (or mpv): press a key per shot as the show plays; shift+key sets up what a key does
ffkit switch zoom      # second pass, in your browser: touch up a camera's framing, push in, follow someone
ffkit switch preview   # 360p render of what you just marked, opens in mpv
ffkit switch render    # final: frame-accurate cuts, T-bar style crossfades, fade through black after the intro
ffkit switch render --from 0:00 --to 9:25   # one stretch at full quality (no intro), to check a song; its pieces are kept for reuse
```

Quit any time; `mark` resumes 3 seconds before your last press. To redo a stretch,
`ffkit switch mark --from 1:09:00` and press keys again; new marks replace old ones in
that range. A camera that wasn't rolling when you pressed it falls back to the audio
camera. HDR phone footage is tone-mapped. Nothing to install for marking: without mpv it
serves the grid to your normal browser from a local page (no sudo needed). After the first
run, `ffkit switch ...` remembers which switch.yaml you used, so no paths to retype. `tests/switch/make_samples.sh` builds three
fake cameras to try it on.

**A key is a shot.** Cameras that never move are PTZ cameras without the motors: set up a
few framings once and punch them in. On the mark page, hold shift and press a key to set up
that key: press a camera's number to pick it, drag a box around what you want to see (the
wheel or `-`/`+` fine-tune the zoom, a click re-centres, `a`/`d`/`w`/`s` nudge), Enter to
save and name it ("vocals", "keys"). A slider under the picture moves through the show, on
both pages, so you can find a moment where everyone is on stage. From then on that key
cuts to that camera framed that way, and the strip along the bottom shows what every key
does. A key with nothing set up is the plain camera of that number, as it is. Two shots on
the same camera (main wide, main vocals) don't cut: pressing the second moves the picture
there over a second, the way a PTZ camera does. Change a shot later (shift+key again) and
every place you used it follows on the next render. Shots live in `switch.shots.json`; a
mark made with one is written as `s3` in `switch.log`.

The mark page is laid out like a switcher: each camera in its tile, the one on air outlined
in red with the crop the viewer gets drawn on it, and in the spare tile the **program view**:
what the viewer actually sees at this moment, framing and all. (When the grid is full, two
cameras whose footage never overlaps, which is one camera that was restarted, share a tile
to make room.) Keys whose camera has no footage at that moment are greyed on the strip, and
clicking a camera's tile cuts to that camera as it is.

**Zoom is a second pass**, after the cameras are chosen. `ffkit switch zoom` renders the cut
so far at 540p (pieces are cached, so only new ones take time) and plays it in your browser
with a box showing what the viewer will see. `0` is the full frame; `1`–`9` are your shots,
for the camera on screen (shift + a key saves the current framing as that key's shot here
too); `-`/`+` or the mouse wheel change the zoom in 0.1× steps; click the picture to aim,
drag to follow someone, `a`/`d`/`w`/`s` nudge the view by 8% (hold the key to keep going,
shift for a big step), `v` flips between the whole frame and the result. Every move eases
over a second (`zoom_ease`); a shot pressed in the first few seconds after a cut applies
from the cut itself. You don't have to get it right first time: changes made within a few
seconds of each other (`zoom_settle`, default 4) render as one direct move from where the
picture was to where you ended up, so trial and error on the page doesn't show in the
result; `shift-u` undoes the whole last adjustment. To follow someone across the stage,
shift-drag: that records the path as you move. Each camera keeps its zoom until a shot or a
change sets it again, so a push-in on the wide camera is still there when you cut back to
it with the plain camera key, and gone when you cut back with a shot. The render crops from
the camera's full resolution, so a 4K camera stays sharp to 2× at 1080p; the page says when
a zoom is softer than that. Changes go to `zoom.log` (one `seconds,camera,zoom,cx,cy,ease`
line each); `u` undoes, and `zoom --from T` redoes a stretch the same way `mark --from`
does. Preview and render apply the zoom automatically.

## Why it's built this way

See `AGENTS.md` for the rules, written for a coding agent but true for people too.

The reference page (each method, why each flag is where it is, where it breaks, the
wrong way) is `docs/index.html`.



## Try it

```
tests/make_samples.sh          # generates sample clips with ffmpeg
python -m ffkit run tests/job.yaml
```
