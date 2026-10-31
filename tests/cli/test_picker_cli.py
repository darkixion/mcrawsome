import json
from pathlib import Path

from click.testing import CliRunner

from mcrawsome.cli.main import cli


def test_export_picker_option(white_balance_raw: Path) -> None:
    result = CliRunner().invoke(
        cli,
        [
            "export",
            str(white_balance_raw),
            "--to",
            "dng",
            "--pick-neutral",
            "1,16,16,24,24",
            "--develop.lens_shading",
            "0",
            "--dry-run",
            "--json",
        ],
    )
    assert result.exit_code == 0, result.output
    develop = json.loads(result.output)["results"][0]["recipe"]["develop"]
    assert develop["white_balance"] == "custom"
    assert 2000 <= develop["temperature"] <= 12000
    assert -100 <= develop["tint"] <= 100
