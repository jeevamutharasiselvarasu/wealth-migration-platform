import pytest
from decimal import Decimal
from data_plane.core.sleeve_engine import SleeveMathEngine

def test_sleeve_math_exact_hundred_percent():
    raw_sleeves = [
        {"sleeve_id": "S1", "model_code": "MOD-A", "target_pct": 60.0},
        {"sleeve_id": "S2", "model_code": "MOD-B", "target_pct": 40.0}
    ]
    result = SleeveMathEngine.normalize_and_validate("ACC-101", raw_sleeves)
    
    assert result["status"] == "VALIDATED"
    total = sum(item["target_weight"] for item in result["compositions"])
    assert total == Decimal("1.0000")

def test_sleeve_math_decimal_fraction_input():
    raw_sleeves = [
        {"sleeve_id": "S1", "model_code": "MOD-A", "target_pct": 0.70},
        {"sleeve_id": "S2", "model_code": "MOD-B", "target_pct": 0.30}
    ]
    result = SleeveMathEngine.normalize_and_validate("ACC-102", raw_sleeves)
    
    total = sum(item["target_weight"] for item in result["compositions"])
    assert total == Decimal("1.0000")

def test_sleeve_math_rounding_drift_adjustment():
    raw_sleeves = [
        {"sleeve_id": "S1", "model_code": "MOD-A", "target_pct": 33.3333},
        {"sleeve_id": "S2", "model_code": "MOD-B", "target_pct": 33.3333},
        {"sleeve_id": "S3", "model_code": "MOD-C", "target_pct": 33.3334}
    ]
    result = SleeveMathEngine.normalize_and_validate("ACC-103", raw_sleeves)
    
    total = sum(item["target_weight"] for item in result["compositions"])
    assert total == Decimal("1.0000")

def test_sleeve_math_invariant_failure():
    invalid_sleeves = [
        {"sleeve_id": "S1", "model_code": "MOD-A", "target_pct": 50.0},
        {"sleeve_id": "S2", "model_code": "MOD-B", "target_pct": 40.0}
    ]
    with pytest.raises(ValueError, match="Sleeve allocation invariant failed"):
        SleeveMathEngine.normalize_and_validate("ACC-104", invalid_sleeves)