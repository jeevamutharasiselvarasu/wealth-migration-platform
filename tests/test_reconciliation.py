import pytest
from data_plane.src.validation.reconciler import ReconciliationEngine

def test_reconciliation_pass_within_tolerance():
    reconciler = ReconciliationEngine(tolerance_pct=0.5)
    source_pos = [{"market_value": 1000000.00}]
    target_pos = [{"market_value": 1002000.00}]
    
    result = reconciler.reconcile_aum(source_pos, target_pos)
    assert result["status"] == "PASS"
    assert result["variance_pct"] == pytest.approx(0.2, 0.01)

def test_reconciliation_fail_exceeds_tolerance():
    reconciler = ReconciliationEngine(tolerance_pct=0.5)
    source_pos = [{"market_value": 1000000.00}]
    target_pos = [{"market_value": 1010000.00}]
    
    result = reconciler.reconcile_aum(source_pos, target_pos)
    assert result["status"] == "FAIL_HOLD"
    assert result["variance_pct"] == pytest.approx(1.0, 0.01)

def test_reconciliation_zero_source_aum():
    reconciler = ReconciliationEngine(tolerance_pct=0.5)
    source_pos = []
    target_pos = []
    
    result = reconciler.reconcile_aum(source_pos, target_pos)
    assert result["status"] == "PASS"
    assert result["variance_pct"] == 0.0