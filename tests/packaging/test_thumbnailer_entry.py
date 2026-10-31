"""File managers find the thumbnailer through its .thumbnailer entry, for the MIME type the package
installs. GNOME runs it in a sandbox holding only /usr, so the entry names programs there."""

import configparser
import re
from pathlib import Path

ROOT = Path(__file__).parents[2]


def test_the_entry_runs_the_thumbnailer_for_mcraw_recordings() -> None:
    entry = configparser.ConfigParser(interpolation=None)
    entry.read(ROOT / "packaging/linux/mcrawsome.thumbnailer")
    section = entry["Thumbnailer Entry"]
    assert section["TryExec"] == "/usr/bin/mcrawsome"
    assert section["Exec"] == "/usr/bin/python3 -I -m mcrawsome.thumbnailer %s %i %o"
    mime = re.search(r'<mime-type type="([^"]+)"', (ROOT / "packaging/linux/mcrawsome-mime.xml").read_text())
    assert mime is not None
    assert section["MimeType"] == f"{mime.group(1)};"
    assert '"thumbnailers/mcrawsome.thumbnailer"' in (ROOT / "packaging/deb/build_deb.py").read_text()
