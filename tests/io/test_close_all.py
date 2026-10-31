import pytest

from mcrawsome.io.clip import close_all


class _Clip:
    def __init__(self, fails: bool = False) -> None:
        self.fails = fails
        self.closed = False

    def close(self) -> None:
        self.closed = True
        if self.fails:
            raise OSError("cannot close")


def test_one_clip_that_fails_to_close_does_not_leave_the_rest_open() -> None:
    clips = [_Clip(), _Clip(fails=True), _Clip()]
    with pytest.raises(OSError, match="cannot close"):
        close_all(clips)  # type: ignore[arg-type]
    assert all(clip.closed for clip in clips)
