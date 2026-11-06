import shutil
from pathlib import Path

import pytest

from engine.limits import LimitNotFound, Limits
from engine.models import ModelConfigError, ModelRouting
from engine.settings import load_settings

ROOT = Path(__file__).resolve().parent.parent
PACK = ROOT / "packs" / "websites"


def test_instance_settings_resolve_paths_and_region(instance_file):
    settings = load_settings(instance_file)
    assert settings.pack == "websites"
    assert settings.database == instance_file.parent / "town.db"
    assert settings.killswitch_file == instance_file.parent / "state" / "killswitch.json"
    assert settings.overture.radius_miles == 15


def test_limits_file_holds_blueprint_values():
    limits = Limits(PACK / "limits.yaml")
    assert limits.get("money.api_monthly_cap_usd") == 9.00
    assert limits.get("money.total_monthly_cap_usd") == 25.00
    assert limits.get("email.sends_per_day") == 20
    assert limits.get("leads.new_leads_per_day") == 50
    assert limits.get("building.sites_per_day") == 5
    assert limits.get("previews.expire_days") == 21


def test_limit_change_keeps_comments_and_refuses_new_keys(tmp_path):
    copy = tmp_path / "limits.yaml"
    shutil.copy(PACK / "limits.yaml", copy)
    limits = Limits(copy)

    old = limits.set("money.api_monthly_cap_usd", 9.50)

    assert old == 9.00
    assert limits.get("money.api_monthly_cap_usd") == 9.50
    assert "Sits just under the Console's $10 hard limit" in copy.read_text(encoding="utf-8")
    with pytest.raises(LimitNotFound):
        limits.set("money.made_up_limit", 1)


def test_scout_routes_to_haiku_on_the_api():
    choice = ModelRouting(PACK / "models.yaml").for_agent("scout")
    assert choice.route == "api"
    assert "haiku" in choice.model
    assert choice.input_usd_per_mtok > 0


def test_builder_routes_to_claude_cli():
    assert ModelRouting(PACK / "models.yaml").for_agent("builder").route == "claude_cli"


def test_api_route_refuses_non_haiku_models(tmp_path):
    bad = tmp_path / "models.yaml"
    bad.write_text(
        "tiers:\n  fast:\n    model: claude-opus-5-5\n    route: api\n"
        "    input_usd_per_mtok: 4\n    output_usd_per_mtok: 20\n"
        "agents:\n  scout: fast\n",
        encoding="utf-8",
    )
    with pytest.raises(ModelConfigError):
        ModelRouting(bad).for_agent("scout")
