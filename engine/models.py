"""Model routing: which model each agent uses, read from the pack's models.yaml.

Agent code asks `routing.for_agent("scout")` and never names a model itself.
"""
from dataclasses import dataclass
from pathlib import Path

from engine.yamlio import load_yaml

API_ROUTE = "api"
CLI_ROUTE = "claude_cli"


class ModelConfigError(ValueError):
    pass


@dataclass(frozen=True)
class ModelChoice:
    tier: str
    model: str
    route: str
    input_usd_per_mtok: float = 0.0
    output_usd_per_mtok: float = 0.0


class ModelRouting:
    def __init__(self, path: Path) -> None:
        self.path = Path(path)

    def for_agent(self, agent: str) -> ModelChoice:
        raw = load_yaml(self.path)
        agents = raw.get("agents") or {}
        if agent not in agents:
            raise ModelConfigError(f"No model tier set for agent '{agent}' in {self.path.name}")
        tier_name = str(agents[agent])
        tier = (raw.get("tiers") or {}).get(tier_name)
        if tier is None:
            raise ModelConfigError(f"Agent '{agent}' uses unknown tier '{tier_name}'")
        choice = ModelChoice(
            tier=tier_name,
            model=str(tier["model"]),
            route=str(tier["route"]),
            input_usd_per_mtok=float(tier.get("input_usd_per_mtok", 0.0)),
            output_usd_per_mtok=float(tier.get("output_usd_per_mtok", 0.0)),
        )
        _check_route(choice)
        return choice


def _check_route(choice: ModelChoice) -> None:
    """The API is Haiku-only. Sonnet and Opus must go through `claude -p`.

    A plain code check, so a config edit can't quietly put an expensive model on the API key.
    """
    if choice.route not in (API_ROUTE, CLI_ROUTE):
        raise ModelConfigError(f"Unknown route '{choice.route}' for tier '{choice.tier}'")
    if choice.route == API_ROUTE and "haiku" not in choice.model:
        raise ModelConfigError(
            f"Tier '{choice.tier}' puts {choice.model} on the API. The API is Haiku-only."
        )
    if choice.route == API_ROUTE and (choice.input_usd_per_mtok <= 0 or choice.output_usd_per_mtok <= 0):
        raise ModelConfigError(f"Tier '{choice.tier}' is on the API but has no prices for cost logging")
