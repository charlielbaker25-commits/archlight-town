from typer.testing import CliRunner

from launcher import app


def test_town_command_runs():
    result = CliRunner().invoke(app, ["version"])
    assert result.exit_code == 0
    assert "The Town" in result.stdout
