"""Check the installed numerical runtime and the application's supported OpenCV API."""

from __future__ import annotations

import ast
import importlib
import importlib.metadata
import json
import runpy
from pathlib import Path
from typing import Any

# Looked up by name where OpenCV builds differ (cv2.fisheye in some, cv2 in others); reviewed, and each
# checked below to resolve one way or the other.
REVIEWED_DYNAMIC = {
    "camera/calibrate_lens.py": ("CALIB_RECOMPUTE_EXTRINSIC", "CALIB_CHECK_COND", "CALIB_FIX_SKEW"),
}


def dotted(node: ast.AST) -> str:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        parent = dotted(node.value)
        return f"{parent}.{node.attr}" if parent else ""
    return ""


def requirements(package: Path) -> dict[str, list[str]]:
    policy = runpy.run_path(str(Path(__file__).with_name("prune_runtime.py")))
    unused = set(policy["SCIPY_UNUSED"])
    opencv: set[str] = set()
    scipy: set[str] = set()
    for path in sorted(package.rglob("*.py")):
        tree = ast.parse(path.read_text(), filename=str(path))
        aliases: dict[str, str] = {}
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    aliases[alias.asname or alias.name.split(".")[0]] = alias.name
                    if alias.name.startswith("scipy"):
                        scipy.add(alias.name)
            elif isinstance(node, ast.ImportFrom) and node.module:
                for alias in node.names:
                    name = f"{node.module}.{alias.name}"
                    aliases[alias.asname or alias.name] = name
                    if name.startswith("cv2."):
                        opencv.add(name[4:])
                    if name.startswith("scipy."):
                        scipy.add(name)
        for node in ast.walk(tree):
            if isinstance(node, ast.Attribute):
                name = dotted(node)
                head, _, tail = name.partition(".")
                full = aliases.get(head, head) + (f".{tail}" if tail else "")
                if full.startswith("cv2."):
                    opencv.add(full[4:])
                elif full.startswith("scipy."):
                    scipy.add(full)
            elif isinstance(node, ast.Call) and node.args:
                if dotted(node.func) in ("importlib.import_module", "__import__"):
                    arg = node.args[0]
                    if (
                        isinstance(arg, ast.Constant)
                        and isinstance(arg.value, str)
                        and arg.value.startswith("scipy.")
                    ):
                        scipy.add(arg.value)
                elif dotted(node.func) == "getattr" and len(node.args) > 1:
                    target = aliases.get(dotted(node.args[0]), "")
                    if target == "cv2":
                        arg = node.args[1]
                        if not isinstance(arg, ast.Constant) or not isinstance(arg.value, str):
                            if path.relative_to(package).as_posix() in REVIEWED_DYNAMIC:
                                continue
                            raise ValueError(f"Review dynamic OpenCV requirement in {path}:{node.lineno}")
                        opencv.add(arg.value)
    rejected = sorted(name for name in scipy if len(name.split(".")) > 1 and name.split(".")[1] in unused)
    if rejected:
        raise ValueError(f"Review the SciPy runtime profile for new dependencies: {rejected}")
    return {"opencv": sorted(opencv), "scipy": sorted(scipy)}


def audit() -> dict[str, Any]:
    import cv2
    import numpy as np
    from scipy.linalg import solve_banded
    from scipy.ndimage import gaussian_filter1d, uniform_filter1d
    from scipy.spatial.transform import Rotation

    import mcrawsome

    cv2.setNumThreads(2)
    used = requirements(Path(mcrawsome.__file__).parent)
    for name in used["opencv"]:
        obj = cv2
        for part in name.split("."):
            obj = getattr(obj, part)
    for name in used["scipy"]:  # from what is left after pruning
        parts = name.split(".")
        cut = len(parts)
        while True:
            try:
                found: Any = importlib.import_module(".".join(parts[:cut]))
                break
            except ImportError:
                cut -= 1
                if cut == 0:
                    raise
        for part in parts[cut:]:
            found = getattr(found, part)
    for names in REVIEWED_DYNAMIC.values():
        for name in names:
            assert getattr(cv2.fisheye, name, None) is not None or getattr(cv2, name, None) is not None, name
    # What MCRAWsome itself calls (motion.sync, motion.optical, resolve.camera_gyro).
    assert np.allclose(uniform_filter1d([1.0, 2, 3, 4], 3), [4 / 3, 2, 3, 11 / 3])
    assert np.allclose(gaussian_filter1d([0.0, 0, 1, 0, 0], 1.0).sum(), 1.0)
    band = np.array([[0.0, -1, -1], [4.0, 4, 4], [-1.0, -1, 0]])
    assert np.allclose(solve_banded((1, 1), band, [3.0, 2, 3]), [1, 1, 1])
    quarter = Rotation.from_matrix([[0.0, -1, 0], [1, 0, 0], [0, 0, 1]]).inv().as_quat()
    assert np.allclose(np.abs(quarter), [0, 0, np.sqrt(0.5), np.sqrt(0.5)])
    pixels = np.arange(64 * 64, dtype=np.uint8).reshape(64, 64)
    flow = cv2.DISOpticalFlow.create(cv2.DISOPTICAL_FLOW_PRESET_MEDIUM).calc(
        pixels, pixels, np.zeros((64, 64, 2), dtype=np.float32)
    )
    assert np.isfinite(flow).all()
    assert np.max(np.abs(flow)) < 1e-5
    for extension in (".png", ".jpg", ".tiff"):
        ok, encoded = cv2.imencode(extension, pixels)
        decoded = cv2.imdecode(encoded, cv2.IMREAD_UNCHANGED)
        assert ok
        assert decoded is not None
        assert decoded.shape == pixels.shape
    return {
        "requirements": used,
        "versions": {
            name: importlib.metadata.version(name)
            for name in (
                "numpy",
                "scipy",
                "opencv-python-headless",
            )
        },
        "numerical_checks": "passed",
    }


if __name__ == "__main__":
    print(json.dumps(audit(), indent=2))
