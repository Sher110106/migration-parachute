"""Cost ledger inputs. Prices are explicit assumptions, not market data."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal


@dataclass(frozen=True)
class CostModel:
    # Planner model prices in USD per million tokens: claude-opus-5-5 first-party
    # list price ($4 in / $20 out). Bedrock prices differ. Rule planners use no tokens.
    input_per_mtok: Decimal = Decimal("4.00")
    output_per_mtok: Decimal = Decimal("20.00")
    # Database runner time, priced as a small managed Postgres instance.
    db_usd_per_hour: Decimal = Decimal("0.50")

    def cost(self, tokens_in: int = 0, tokens_out: int = 0, db_ms: int = 0) -> Decimal:
        llm = (Decimal(tokens_in) * self.input_per_mtok + Decimal(tokens_out) * self.output_per_mtok) / Decimal(1_000_000)
        db = Decimal(db_ms) * self.db_usd_per_hour / Decimal(3_600_000)
        return (llm + db).quantize(Decimal("0.0000001"))
