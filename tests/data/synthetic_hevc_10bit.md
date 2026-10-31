# Synthetic HEVC decoder fixture

`synthetic_hevc_10bit.mp4` contains three 160x96, 10-bit 4:2:0 frames at 24 fps:
a white box on a dark background, plus a 440 Hz tone. No real recording was used.
It preserves the HEVC decoder and export coverage when the tested FFmpeg has no
software HEVC encoder. Shared fixture code copies it into the test's own folder.

Generated with the system FFmpeg 7.1.5:

```sh
ffmpeg -v error -f lavfi \
  -i 'color=c=0x202020:s=160x96:r=24,drawbox=x=8:y=12:w=24:h=16:color=white:t=fill' \
  -f lavfi -i sine=frequency=440:sample_rate=48000 \
  -t 0.125 -pix_fmt yuv420p10le -c:v libx265 -preset ultrafast \
  -x265-params pools=2:frame-threads=1:log-level=error \
  -c:a aac synthetic_hevc_10bit.mp4
```
