"""The window and the page share the product name in brand.json."""

import json
from pathlib import Path

PRODUCT: str = json.loads((Path(__file__).parent / "brand.json").read_text())["product"]
DESKTOP_AGENT = "mcrawsome-pywebview"
