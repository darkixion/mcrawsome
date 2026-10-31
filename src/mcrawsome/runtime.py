"""Lightweight process defaults shared by the desktop, engine and command line."""

import os


def default_workers() -> int:
    """The worker count commands use unless --workers says otherwise."""
    return max(1, (os.cpu_count() or 2) // 3)
