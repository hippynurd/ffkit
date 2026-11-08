#!/usr/bin/env bash
# Three fake cameras of the same 90-second "show", started at different times, sharing the
# same noise as audio so `ffkit switch sync` has something to lock onto, plus a 5s intro.
# cam 1 (main) starts at show 0; cam 2 (front) starts 5s late; cam 3 (side) has 7s of pre-show
# and stops 5s before the end. Each frame shows the show clock so you can check alignment by eye.
set -euo pipefail
cd "$(dirname "$0")" && mkdir -p cams
ffmpeg -v error -y -f lavfi -i "anoisesrc=d=100:c=pink:seed=42:r=48000:a=0.5" -c:a pcm_s16le master.wav
ffmpeg -v error -y -f lavfi -i "anoisesrc=d=7:c=white:seed=7:r=48000:a=0.3" -c:a pcm_s16le pre.wav
DT="drawtext=fontsize=120:fontcolor=white:box=1:boxcolor=black@0.6:boxborderw=20:x=(w-tw)/2:y=(h-th)/2"
ffmpeg -v error -y -f lavfi -i "testsrc2=size=1280x720:rate=30" -i master.wav -t 90 \
  -vf "$DT:text='CAM1  SHOW %{eif\:t\:d}'" -c:v libx264 -preset veryfast -g 30 -pix_fmt yuv420p -c:a aac -b:a 128k -shortest cams/main.mp4
ffmpeg -v error -y -f lavfi -i "testsrc2=size=1280x720:rate=30" -ss 5 -i master.wav -t 85 \
  -vf "hue=h=120,$DT:text='CAM2  SHOW %{eif\:t+5\:d}'" -c:v libx264 -preset veryfast -g 30 -pix_fmt yuv420p -c:a aac -b:a 128k -shortest cams/front.mp4
ffmpeg -v error -y -f lavfi -i "testsrc2=size=1280x720:rate=30" -i pre.wav -i master.wav \
  -filter_complex "[1:a]atrim=0:7[p];[2:a]atrim=0:85[m];[p][m]concat=n=2:v=0:a=1[a];[0:v]hue=h=240,$DT:text='CAM3  SHOW %{eif\:t-7\:d}'[v]" \
  -map "[v]" -map "[a]" -t 92 -c:v libx264 -preset veryfast -g 30 -pix_fmt yuv420p -c:a aac -b:a 128k cams/side.mp4
ffmpeg -v error -y -f lavfi -i "color=c=orange:size=1280x720:rate=30" -f lavfi -i "sine=frequency=330:sample_rate=48000" -t 5 \
  -vf "$DT:text='INTRO'" -c:v libx264 -preset veryfast -pix_fmt yuv420p -c:a aac intro.mp4
rm -f master.wav pre.wav
echo "samples ready. Try:  ffkit switch -f tests/switch/switch.yaml sync && ffkit switch -f tests/switch/switch.yaml grid"
