"""Open the trial page using the extracted AppImage's normal pywebview/GTK backend."""

from __future__ import annotations

import argparse
import importlib
import time
from pathlib import Path
from typing import Any


def main() -> None:
    webview = importlib.import_module("webview")

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=18861)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    window = webview.create_window(
        "GPU development trial", f"http://127.0.0.1:{args.port}/?browser=webkit", width=1360, height=980
    )

    def exercise() -> None:
        try:
            for attempt in range(2400):
                if attempt % 50 == 0:
                    print(window.evaluate_js("document.querySelector('#status')?.textContent"), flush=True)
                if window.evaluate_js("window.trialDone === true"):
                    break
                time.sleep(0.1)
            else:
                raise RuntimeError("WebKit trial did not complete in four minutes")
            # Snapshot through the same bundled WebKit view used by the product.
            import threading

            captured = threading.Event()

            def finished(source: Any, response: Any, *unused: Any) -> None:
                try:
                    source.get_snapshot_finish(response).write_to_png(str(args.output / "webkit.png"))
                finally:
                    captured.set()

            def snapshot() -> bool:
                backend = window.gui
                view = backend.BrowserView.instances[window.uid].webview
                view.get_snapshot(
                    backend.webkit.SnapshotRegion.VISIBLE,
                    backend.webkit.SnapshotOptions.NONE,
                    None,
                    finished,
                    None,
                )
                return False

            window.gui.glib.idle_add(snapshot)
            captured.wait(5)
        finally:
            window.destroy()

    window.events.loaded += lambda: None
    webview.start(exercise, gui="gtk")


if __name__ == "__main__":
    main()
