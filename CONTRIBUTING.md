# Contributing

Bug reports, fixes and camera profiles are all welcome. Code changes come with tests. During
development, run the tests and static checks relevant to the change. A full `./gates.sh` run (Ruff,
strict mypy and the full test suite) is required before each push, rather than after every task or
local commit. CI keeps its existing full selection, including `slow` tests. Local samples and private
fixtures need an owner-machine check when relevant because they may be absent in CI.

`./gates.sh --quick` is available when a broader local check is useful. It runs the same static and web
checks, with pytest `-m 'not slow and not local'`. It does not replace the full gate before a push.

Give either command a disk-backed scratch folder, for example
`./gates.sh --quick --basetemp ~/.cache/mcrawsome-check/pytest` (create the parent folder first).
Run `./gates.sh --help` for the command options; extra arguments go to pytest.

## Contributing a camera profile

Stabilisation works best when MCRAWsome knows the camera: its focal length, its rolling-shutter readout,
how the gyro's axes map to the picture and roughly how far the gyro log is out of sync. Without a profile
these come from the clip's metadata and a fit on each clip, which is slower and fails more often on
difficult footage. A profile measured once helps everyone with the same phone.

### 1. Record a calibration clip

Use the camera, lens and resolution you want to profile, at the frame rate you normally use.

- Film a **static, distant, detailed scene**: a city skyline, a hillside with trees, a building
  across a square. Avoid anything that moves (traffic, water, people, trees in wind) and anything close
  to the camera.
- Turn the phone's own stabilisation off, and record the gyro log (MotionCam Pro writes a `.gcsv`
  next to each RAW clip; Direct Log videos carry it inside the file).
- Keep the exposure short (a fast shutter) so the picture stays sharp.
- Record about 30 to 60 seconds of **slow** movement in every direction: pan left and right, tilt up
  and down, and roll the phone clockwise and anticlockwise, a few times each. Smooth, steady movements
  of a second or two each work better than shaking.

### 2. Calibrate

```
mcrawsome calibrate CLIP
```

This measures every frame, fits the sync and the axes with the widest search, measures the readout from
the difference between the top and bottom thirds of the frame, and prints the profile with a confidence.
Nothing is written until you add `--save`:

```
mcrawsome calibrate CLIP --save --name pixel_10_pro_xl_main_4k
```

The profile goes to `~/.config/mcrawsome/cameras/` and is used from then on for clips from the same
camera at the same resolution. MCRAWsome refuses to save a calibration it does not trust; the message
says why (usually too little movement, movement in only one direction, or a moving scene). Record
again with slower, fuller movements.

Check the result on a normal clip with `mcrawsome gyro CLIP`: the confidence should be high and the
sync offset close to the profile's.

### 3. Or start from a Gyroflow lens profile

Gyroflow's lens profile database (`gyroflow/lens_profiles` on GitHub, released under CC0) has many
phones. Download the JSON file for your phone and lens (MCRAWsome never goes online itself), then:

```
mcrawsome calibrate CLIP --from-gyroflow PROFILE.json --save --name my_phone_main_4k
```

This takes the focal length (scaled to the clip) and the readout from the lens profile. Existing gyro
and gravity mappings are preserved when replacing a matching camera profile; sync is still fitted per
clip. Measured OpenCV standard or fisheye lens geometry, including its principal point and distortion
coefficients, is retained for Gyroflow interchange. MCRAWsome does not yet apply it to image pixels.
Use a calibration for the same physical lens and recording mode. Uniform resizing preserves the
calibration, but a different aspect ratio needs a known crop mapping before it can be exported.
Digital lens transforms and interpolated calibrations are refused rather than partially imported.

To measure the lens itself, use Gyroflow's checkerboard calibration with the lens, resolution and
sensor crop used for your footage. The distant-scene gyro recording above measures sync, axes and
readout, not optical distortion. No lens coefficients are inferred from a phone model or focal length.

### 4. Send it in

Open a pull request that adds the TOML file from `~/.config/mcrawsome/cameras/` to
`src/mcrawsome/camera/profiles/`, with:

- the phone model, the camera (main, ultrawide, telephoto) and the resolution and frame rate;
- the confidence `mcrawsome calibrate` reported;
- the output of `mcrawsome gyro` on one or two ordinary clips from the same camera.

Please do not attach the calibration clip itself unless asked: the numbers are enough to review the
profile, and clips are large.
