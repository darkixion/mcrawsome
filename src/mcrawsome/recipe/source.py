"""Saved source colour interpretation for neutral video thumbnails and preview startup."""

from pathlib import Path

from mcrawsome.recipe.clip_layer import path_for
from mcrawsome.recipe.load import build_recipe, load_toml, lower_layer_paths
from mcrawsome.recipe.schema import Recipe


def saved_video_recipe(clip: Path, rotation: int = 0, first_16mib: str | None = None) -> Recipe:
    """Read source interpretation independently of creative settings and look availability."""
    develop: dict[str, object] = {"exposure": 0, "rotation": rotation}
    camera: dict[str, object] = {}
    for path in [*lower_layer_paths(clip), path_for(clip, first_16mib)]:
        if not path.is_file():
            continue
        layer = load_toml(path)
        values = layer.get("develop", {})
        if isinstance(values, dict):
            develop.update(
                {
                    key: value
                    for key, value in values.items()
                    if key
                    in (
                        "input_encoding",
                        "input_gamut",
                        "input_transfer",
                        "input_ei",
                        "native_as",
                    )
                }
            )
        values = layer.get("camera", {})
        if isinstance(values, dict) and "colour_matrix" in values:
            camera["colour_matrix"] = values["colour_matrix"]
    return build_recipe([{"develop": develop, "camera": camera, "motion": {"stabilise": "off"}}])
