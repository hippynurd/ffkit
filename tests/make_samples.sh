#!/usr/bin/env bash
# Generates the sample clips the example job expects (tests/src/). Needs ffmpeg.
set -euo pipefail
cd "$(dirname "$0")" && mkdir -p src
ffmpeg -v error -y -f lavfi -i "testsrc2=size=1280x720:rate=30" -f lavfi -i "sine=frequency=440:sample_rate=48000" -t 12 -c:v libx264 -preset veryfast -g 30 -keyint_min 30 -sc_threshold 0 -pix_fmt yuv420p -c:a aac -b:a 128k src/a.mp4
ffmpeg -v error -y -f lavfi -i "testsrc2=size=1280x720:rate=30" -f lavfi -i "sine=frequency=660:sample_rate=48000" -t 8  -c:v libx264 -preset veryfast -g 30 -keyint_min 30 -sc_threshold 0 -pix_fmt yuv420p -c:a aac -b:a 128k src/b.mp4
ffmpeg -v error -y -f lavfi -i "testsrc2=size=1920x1080:rate=25" -f lavfi -i "sine=frequency=880:sample_rate=44100" -t 6 -c:v libx264 -preset veryfast -g 60 -pix_fmt yuv420p -c:a aac -b:a 128k src/c_mismatch.mov
ffmpeg -v error -y -f lavfi -i "sine=frequency=220:sample_rate=48000" -t 30 -c:a aac -b:a 128k src/music.m4a
echo "samples in $(pwd)/src; now: python -m ffkit run tests/job.yaml"
