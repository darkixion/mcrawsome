import numpy as np
import numpy.typing as npt

def reduce_cfa(raw: npt.NDArray[np.uint16], factor: int) -> npt.NDArray[np.uint16]: ...
def remosaic(
    raw: npt.NDArray[np.uint16], pattern: str, black: npt.NDArray[np.float32]
) -> npt.NDArray[np.uint16]: ...
def remosaic_green(
    phases: list[npt.NDArray[np.float32]], green_mask: int
) -> list[npt.NDArray[np.float32]]: ...
def remosaic_difference(
    grid: npt.NDArray[np.float32],
    guide: npt.NDArray[np.float32],
    here: npt.NDArray[np.float32],
    ty: int,
    tx: int,
    fy: float,
    fx: float,
) -> npt.NDArray[np.float32]: ...
def decode(data: bytes, width: int, height: int, compression_type: int) -> npt.NDArray[np.uint16]: ...
def bake_cfa(
    raw: npt.NDArray[np.uint16],
    black: npt.NDArray[np.float32],
    gains: npt.NDArray[np.float32] | None,
    gain: float = 1.0,
    white: float = 65535.0,
) -> npt.NDArray[np.uint16]: ...
def encode_ljpeg(tile: npt.NDArray[np.uint16], bits: int) -> bytes: ...
def decode_ljpeg(data: bytes) -> npt.NDArray[np.uint16]: ...
def apply_look(
    frame: npt.NDArray[np.float32], table: npt.NDArray[np.float32], bgr: bool = False
) -> npt.NDArray[np.uint8]: ...
def develop_preview(
    raw: npt.NDArray[np.uint16],
    black: npt.NDArray[np.float32],
    gain: npt.NDArray[np.float32],
    cfa: str,
    step: int = 2,
    rotate: bool = False,
    max_width: int = 65536,
    white: float = 65535.0,
    recover: bool = False,
) -> tuple[npt.NDArray[np.float32], npt.NDArray[np.uint8]]: ...
def encode_preview(linear: npt.NDArray[np.float32], stops: float) -> npt.NDArray[np.float32]: ...
def neutral_preview(rgb: npt.NDArray[np.float32], mask: npt.NDArray[np.float32]) -> None: ...
def apply_preview(
    frame: npt.NDArray[np.float32], table: npt.NDArray[np.float32], stops: float
) -> npt.NDArray[np.uint8]: ...
def recover_preview(rgb: npt.NDArray[np.float32], masks: npt.NDArray[np.uint8]) -> None: ...
