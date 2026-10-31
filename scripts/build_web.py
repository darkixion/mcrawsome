"""Build the interface: `npm ci` and `npm run build` in web/, into src/mcrawsome/server/web/ (git-ignored,
shipped in wheels and the AppImage). Needs Node 22 or later; running the interface does not."""

import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
WEB = ROOT / "web"
BUNDLE = ROOT / "src" / "mcrawsome" / "server" / "web"


def main() -> int:
    npm = shutil.which("npm")
    if npm is None:
        print("npm is not installed: install Node 22 or later to build the interface", file=sys.stderr)
        return 1
    for step in (["ci"], ["run", "build"]):
        if subprocess.run([npm, *step], cwd=WEB, check=False).returncode != 0:
            print(f"npm {' '.join(step)} failed in {WEB}", file=sys.stderr)
            return 1
    if not (BUNDLE / "index.html").is_file():
        print(f"the build left no {BUNDLE / 'index.html'}", file=sys.stderr)
        return 1
    print(f"Built the interface into {BUNDLE}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
