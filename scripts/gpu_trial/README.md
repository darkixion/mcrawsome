# GPU development trial

Standalone experiments, never imported by the product. There is no new product dependency.
All generated RAW, images, timing logs and extracted runtimes belong in a disk-backed cache folder.
The server binds only to loopback and accepts only `261004_174122_VIDEO_26mm.mcraw` as its input.
Do not expose this development server to other users or hosts.

## Run offline

From the repository root, with the existing dependencies available:

```sh
export UV_OFFLINE=1
cache="$HOME/.cache/mcrawsome-gpu-trial"
clip="$HOME/Downloads/motioncam/261004_174122_VIDEO_26mm.mcraw"
uv run python -m scripts.gpu_trial.bench "$clip" --output "$cache/results" --engine-only
uv run python -m scripts.gpu_trial.run "$clip" --output "$cache/window-chromium" --browser chromium
# Extract an existing AppImage in the cache folder using --appimage-extract first.
uv run python -m scripts.gpu_trial.run "$clip" --output "$cache/window-webkit" \
  --browser webkit --appdir "$cache/squashfs-root"
MCRAWSOME_GPU_TESTS=1 MCRAWSOME_TRIAL_GPU=1 uv run pytest -q -W error \
  tests/test_gpu_trial.py --basetemp "$cache/pytest-gpu"
```

Run the browsers separately, leaving the test window visible. The default is 120 requests per phase,
1280 x 720 for this clip, fixed first-frame white balance, zero exposure, gorgeous SDR and a 33-point
cube. Playback repeatedly traverses frames 0 to 59; scrubbing follows 0 to 59 to 0. Each request opens
fresh frame data through the kept Clip, with a warm filesystem cache. No developed-frame cache is used.
RAW decompression remains on the CPU. The server's CPU baseline calls the actual native fast path.
Compute uses the full 3840 x 2160 sensor frame. WebGL receives 1920 x 1080 16-bit Bayer, averaged at
same-colour sites, with a static black/gain calibration texture and the engine-baked look table.

The Intel OpenCL ICD is installed on the trial machine, but pyopencl is absent and cannot be fetched
under this task's network restriction. `compute.py` binds the installed OpenCL C API using ctypes.
It deliberately selects an Intel GPU and refuses a software fallback. This is a deviation from the
requested pyopencl wrapper, not a Vulkan implementation. No system package is installed.

`run.py` retains AppRun's library paths, WebKit helper launcher and GTK environment. It substitutes
only the final Python command with `window.py` in a separate generated launcher. Chromium uses its
own temporary profile and ANGLE/OpenGL, with software rendering rejected by the page. The harness performs only loopback requests.
Chromium background services are disabled by flags.

For whole-GPU counters, start this in another terminal before starting the browser harness.
Root access is needed only when the host denies performance-counter access:

```sh
mkdir -p "$cache/window-webkit"
sudo .venv/bin/python scripts/gpu_trial/profile.py --output "$cache/window-webkit" --seconds 90
# After the browser and counter captures finish:
uv run python -m scripts.gpu_trial.analyse "$cache/window-webkit" --browser webkit
```

## Interpret measurements

- `engine.json`: warm, serial decode, GPU upload, demosaic/colour/resize/look, readback and JPEG encode.
  GPU stages are OpenCL profiling event durations; total elapsed also includes host API and BGR copy.
- `<browser>.json`: timings for the same engine routes through WebSocket/JPEG decode/canvas, and for RAW
  through WebSocket/texture upload/shader/canvas. Timer queries measure GPU upload and draw where available; a fence waits for GPU completion.
  WebKit without the timer extension reports submission and completion-wait wall times separately. One
  `requestAnimationFrame` follows each completed frame, limiting throughput to the display cadence.
  These are canvas-ready rates, not proof that every frame reached the physical display.
- `fetch_ms` includes server work. `transfer_overhead_ms` subtracts reported server time and includes
  request scheduling, WebSocket delivery, body copies and browser overhead. It is not a wire-only bandwidth test.
  RAW packing and same-colour reduction are timed separately; RAW has no JPEG encode or normal
  readback. JPEG decode and canvas draw are separate on the engine route.
- `<browser>-resources.json`: 100 ms process-tree CPU/RSS samples, phases and readable DRM client
  engine counters. RSS sums shared mappings across processes; it is not unique physical memory.
  CPU is cumulative user/system seconds, with 100% meaning one core. Sampling adds some host work.
  DRM client counters are supplementary, not whole-GPU utilisation or an intel_gpu_top replacement.
- Colour: absolute 8-bit RGB code difference, mean and 99th percentile across channels. Three frames
  compare each demosaic with full Malvar development, highlight recovery, orientation, ACEScct and
  the same 33-point table. References are resized in ACEScct before the table as the normal preview
  does. Film effects, temporal denoise and stabilisation are disabled to isolate development.
  Compute colour is measured before JPEG; the page is read back only for these parity samples.

These shaders do not implement highlight recovery or all export processing. The compute prototype
uses exact box area resizing of linear RGB; the page uses bilinear RGB resampling and reduced RAW.
They are speed/quality experiments, not an approved replacement pipeline. Full reference images
are generated one at a time and are not committed. The page reports errors instead of accepting a
software renderer. Results and licence/driver conclusions are in the research report.
