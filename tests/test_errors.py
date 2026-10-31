import ast
import pickle
from pathlib import Path

import mcrawsome
from mcrawsome.errors import ERROR_CODES, McError
from mcrawsome.output.export import ExportError
from mcrawsome.results import ClipResult, ErrorInfo


def test_every_raise_site_passes_a_code() -> None:
    root = Path(mcrawsome.__file__).parent
    missing: list[str] = []
    for path in root.rglob("*.py"):
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            name = func.id if isinstance(func, ast.Name) else getattr(func, "attr", "")
            if name in ("ExportError", "McError") and not any(k.arg == "code" for k in node.keywords):
                missing.append(f"{path.relative_to(root)}:{node.lineno}")
    assert missing == []


def test_error_carries_message_and_code() -> None:
    err = ExportError("clip.mcraw does not exist", code="input-missing")
    assert isinstance(err, McError)
    assert str(err) == "clip.mcraw does not exist"
    assert (err.message, err.code) == ("clip.mcraw does not exist", "input-missing")


def test_error_survives_pickling() -> None:
    again = pickle.loads(pickle.dumps(ExportError("boom", code="encode-failed")))
    assert isinstance(again, ExportError)
    assert (str(again), again.code) == ("boom", "encode-failed")


def test_code_list_is_complete() -> None:
    assert len(ERROR_CODES) == 24
    assert "client-not-allowed" in ERROR_CODES
    assert "clip-missing" in ERROR_CODES
    assert "origin-not-allowed" in ERROR_CODES
    assert "recipe-changed" in ERROR_CODES
    assert "job-not-waiting" not in ERROR_CODES


def test_clip_result_round_trips_as_json() -> None:
    result = ClipResult(
        clip=Path("a.mcraw"), error=ErrorInfo(code="input-missing", message="a.mcraw does not exist")
    )
    again = ClipResult.model_validate_json(result.model_dump_json())
    assert again == result
    assert again.output is None
    assert again.notes == []


def test_a_full_disk_is_disk_full_before_it_is_unreadable() -> None:
    import errno

    from mcrawsome.errors import error_code

    assert error_code(OSError(errno.ENOSPC, "No space left on device", "/out/x")) == "disk-full"
    assert error_code(RuntimeError("write failed: No space left on device")) == "disk-full"
