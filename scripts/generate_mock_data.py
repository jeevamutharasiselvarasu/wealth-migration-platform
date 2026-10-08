import os
import pandas as pd

def generate_mock_files():
    storage_dir = os.path.abspath(
        os.path.join(os.path.dirname(__file__), "..", "data_plane", "storage")
    )
    os.makedirs(storage_dir, exist_ok=True)

    # 1. Sample Amplify Sleeve Extracts
    sleeves_df = pd.DataFrame([
        {"REP_CODE": "REP-9981", "ACCT_NUM": "ACC-99812", "sleeve_id": "SLV-01", "model_code": "MOD-GROWTH", "target_pct": 70.0},
        {"REP_CODE": "REP-9981", "ACCT_NUM": "ACC-99812", "sleeve_id": "SLV-02", "model_code": "MOD-INCOME", "target_pct": 30.0},
        {"REP_CODE": "REP-1001", "ACCT_NUM": "ACC-10010", "sleeve_id": "SLV-03", "model_code": "MOD-BALANCED", "target_pct": 100.0}
    ])
    sleeves_path = os.path.join(storage_dir, "amplify_sleeves.csv")
    sleeves_df.to_csv(sleeves_path, index=False)

    # 2. Sample Amplify Positions Snapshots
    positions_df = pd.DataFrame([
        {"REP_CODE": "REP-9981", "ACCT_NUM": "ACC-99812", "CUSIP": "594918104", "TICKER": "MSFT", "QUANTITY": 150, "market_value": 63000.00},
        {"REP_CODE": "REP-9981", "ACCT_NUM": "ACC-99812", "CUSIP": "037833100", "TICKER": "AAPL", "QUANTITY": 200, "market_value": 45000.00},
        {"REP_CODE": "REP-1001", "ACCT_NUM": "ACC-10010", "CUSIP": "02079K305", "TICKER": "GOOGL", "QUANTITY": 300, "market_value": 520000.00}
    ])
    positions_path = os.path.join(storage_dir, "amplify_positions.csv")
    positions_df.to_csv(positions_path, index=False)

    # 3. Sample Black Diamond Performance History
    perf_df = pd.DataFrame([
        {"REP_CODE": "REP-9981", "ACCT_NUM": "ACC-99812", "PERIOD": "2026-Q1", "BMV": 105000.00, "EMV": 108000.00, "NET_RETURN": 2.8571},
        {"REP_CODE": "REP-1001", "ACCT_NUM": "ACC-10010", "PERIOD": "2026-Q1", "BMV": 500000.00, "EMV": 520000.00, "NET_RETURN": 4.0000}
    ])
    perf_path = os.path.join(storage_dir, "blackdiamond_performance.csv")
    perf_df.to_csv(perf_path, index=False)

    print(f"Mock source data generated successfully at: {storage_dir}")

if __name__ == "__main__":
    generate_mock_files()