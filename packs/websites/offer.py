"""Example Web Studio's offer (offer.yaml): the preview clause and the prices, read in one place.

The Messenger puts the prices in call scripts (never emails) and checks them in plain code; the Guard's soft check
gets the same words as approved facts, so Haiku doesn't flag them as unapproved claims about us.
"""
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

from engine.yamlio import load_yaml

OFFER_FILE = Path(__file__).resolve().parent / "offer.yaml"
# An approved fact about us (Charlie, 2026-11-03): saying it in a draft is fine, and the Guard's soft check knows it.
NO_OBLIGATION = ("No obligation: looking at the preview is free and commits them to nothing (\"no obligation\", "
                 "\"no strings attached\" and close rewordings are fine).")


@dataclass(frozen=True)
class Offer:
    preview_clause: str
    tiers: tuple[tuple[str, int, str], ...]     # (name, price, what it includes), cheapest first
    recommended: str                            # the tier marked "Recommended"
    care_monthly: int
    care_includes: str
    no_upfront_tier: str
    no_upfront_monthly: int
    no_upfront_months: int
    booking: str = ""

    def price(self, tier: str) -> int:
        return next(p for name, p, _ in self.tiers if name == tier)

    def amounts(self) -> set[int]:
        """Every dollar amount a call script may say."""
        return {p for _, p, _ in self.tiers} | {self.care_monthly, self.no_upfront_monthly}

    def cost_answer(self) -> str:
        """The answer to "What does it cost?", in Charlie's order (2026-11-02)."""
        basic, premium = self.tiers[0], self.tiers[-1]
        rec = next(t for t in self.tiers if t[0] == self.recommended)
        # "We recommend", never "most people go with": nobody has bought yet (Charlie, 2026-11-03).
        return (f"Sites start at ${basic[1]}. We recommend {rec[0]} at ${rec[1]}: {rec[2]}. {premium[0]} is "
                f"${premium[1]}. The care plan is ${self.care_monthly}/month. Or there's a no-upfront option: "
                f"${self.no_upfront_monthly}/month for {self.no_upfront_months} months.")

    def facts(self) -> list[str]:
        """The offer as approved facts about Example Web Studio (for the Guard's soft check)."""
        return ([f"Every email and call script says what the preview is: {self.preview_clause}.",
                 NO_OBLIGATION,
                 f"Prices (said on calls only, never in emails): {self.cost_answer()}"]
                + [f"{name}: ${price} ({includes})." for name, price, includes in self.tiers]
                + [f"Care plan: ${self.care_monthly}/month ({self.care_includes})."]
                + ([f"Online booking: {self.booking}"] if self.booking else []))


@lru_cache(maxsize=4)
def load_offer(path: Path = OFFER_FILE) -> Offer:
    data = load_yaml(path)
    tiers = tuple((str(t["name"]), int(t["price_usd"]), str(t["includes"])) for t in data["tiers"])
    recommended = next(str(t["name"]) for t in data["tiers"] if t.get("recommended"))
    return Offer(" ".join(str(data["preview_clause"]).split()), tiers, recommended,
                 int(data["care_plan"]["monthly_usd"]), str(data["care_plan"]["includes"]),
                 str(data["no_upfront"]["tier"]), int(data["no_upfront"]["monthly_usd"]),
                 int(data["no_upfront"]["months"]), " ".join(str(data.get("booking") or "").split()))
