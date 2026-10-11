import logging
from decimal import Decimal, ROUND_HALF_UP
from typing import Dict, Any, List

logger = logging.getLogger("SleeveMathEngine")


class SleeveMathEngine:
    """Engine enforcing Decimal rounding drift resolution and 100% sleeve sum invariants[cite: 42]."""

    @staticmethod
    def normalize_and_validate(account_id: str, sleeves: List[Dict[str, Any]]) -> Dict[str, Any]:
        if not sleeves:
            return {"account_id": account_id, "status": "EMPTY", "compositions": []}

        compositions = []
        total_weight = Decimal("0.0000")

        for item in sleeves:
            raw_pct = Decimal(str(item.get("target_pct", item.get("TARGET_WEIGHT_PCT", 0))))
            if raw_pct > Decimal("1.0"):
                weight = (raw_pct / Decimal("100.0")).quantize(Decimal("0.0001"), rounding=ROUND_HALF_UP)
            else:
                weight = raw_pct.quantize(Decimal("0.0001"), rounding=ROUND_HALF_UP)

            compositions.append({
                "sleeve_id": item.get("sleeve_id") or item.get("SUB_SLEEVE_ID"),
                "model_code": item.get("model_code") or item.get("STRATEGY_MODEL_ID"),
                "target_weight": weight
            })
            total_weight += weight

        diff = Decimal("1.0000") - total_weight
        if Decimal("0.0000") < abs(diff) <= Decimal("0.0005"):
            compositions[0]["target_weight"] += diff
            total_weight = Decimal("1.0000")

        if total_weight != Decimal("1.0000"):
            raise ValueError(f"Sleeve allocation invariant failed for account {account_id}: sum={total_weight}")

        return {
            "account_id": account_id,
            "status": "VALIDATED",
            "total_weight": float(total_weight),
            "compositions": compositions
        }