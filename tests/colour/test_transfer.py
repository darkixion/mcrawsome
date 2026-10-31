import pytest
from hypothesis import given
from hypothesis import strategies as st

from mcrawsome.colour import transfer as tf

linear = st.floats(min_value=0.0, max_value=100.0, allow_nan=False)
unit = st.floats(min_value=0.0, max_value=1.0, allow_nan=False)


@pytest.mark.parametrize(
    ("fn", "x", "expected"),
    [
        (tf.bt709_oetf, 0.18, 0.4090),
        (tf.acescct_encode, 0.18, 0.4135),
        (tf.davinci_intermediate_encode, 0.18, 0.3360),
        (tf.pq_encode, 100.0, 0.5081),
        (tf.pq_encode, 10000.0, 1.0),
    ],
)
def test_known_values(fn, x, expected) -> None:  # type: ignore[no-untyped-def]
    assert float(fn(x)) == pytest.approx(expected, abs=5e-4)


@given(linear)
def test_acescct_round_trip(x: float) -> None:
    assert float(tf.acescct_decode(tf.acescct_encode(x))) == pytest.approx(x, rel=1e-9, abs=1e-12)


@given(linear)
def test_davinci_intermediate_round_trip(x: float) -> None:
    assert float(tf.davinci_intermediate_decode(tf.davinci_intermediate_encode(x))) == pytest.approx(
        x, rel=1e-6, abs=1e-9
    )


@given(unit)
def test_bt709_round_trip(x: float) -> None:
    assert float(tf.bt709_oetf_inverse(tf.bt709_oetf(x))) == pytest.approx(x, rel=1e-9, abs=1e-12)


@given(st.floats(min_value=0.0, max_value=10000.0, allow_nan=False))
def test_pq_round_trip(nits: float) -> None:
    assert float(tf.pq_decode(tf.pq_encode(nits))) == pytest.approx(nits, rel=1e-6, abs=1e-6)
