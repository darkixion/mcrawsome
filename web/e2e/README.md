# Interface end-to-end tests

Run from the repository root (Linux, Node 22.12 or later and uv):

```sh
uv sync
npm --prefix web ci
(cd web && npx playwright install chromium)
uv run python scripts/fixtures.py fetch
npm --prefix web run e2e
```

The suite builds the interface and starts an isolated real engine per test with private settings,
state, cache and copies of the approved RAW fixture. It covers library and temporary references,
filters and ratings, portable sessions, notes, RAW overrides and inheritance, orientation/proxy/rate
settings and trim, calibration profiles/target, menus, preview sizing and fullscreen transport.
Native mounts are exercised where FUSE is available. It does not automate Resolve import or rendering.
Direct Log tests and CLI/API exports have separate Python coverage and require FFmpeg/ffprobe;
MKVToolNix is needed for the applicable HDR export paths.

The fixture fetch needs `MCRAWSOME_FIXTURES_TOKEN` for a private release. A missing fixture fails the
workflow. Tests run with one browser worker. Successful runs remove engine scratch files; failed runs
retain them and print their paths. Logs, screenshots and traces are in `~/.cache/mcrawsome-e2e/`.
Use `npx playwright show-trace PATH/trace.zip` from `web/` to inspect a failure.
Playwright stays outside `gates.sh`; CI runs it in the web job.
