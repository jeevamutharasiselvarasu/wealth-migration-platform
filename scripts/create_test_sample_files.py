# scripts/create_test_sample_files.py
import os

os.makedirs("test_files", exist_ok=True)

# 1. Accounts Raw Source Extract (Schwab PAS Format)
accounts_csv = """LEGACY_ACCT_NO,CUST_CODE_SRC,ADVISOR_REP_NUM,REGISTRATION_SRC_TYPE,ACCOUNT_OPEN_DT
ACC-99812,SCHW,9981,INDV_TAXABLE,03/15/2021
ACC-99813,SCHW,9981,IRA_ROLL,06/20/2022
ACC-10010,SCHW,1001,JT_TEN,11/01/2019"""

# 2. Transactions Raw Source Extract (Schwab PAS Format)
transactions_csv = """TRANSACTION_REF_ID,LEGACY_ACCT_NO,TXN_ACTION_TYPE,SETTLEMENT_AMT,TRADE_EVENT_DATE
TXN-88001,ACC-99812,BUY_EQUITY,-15000.00,20260915
TXN-88002,ACC-99812,DIV_CASH,450.00,20260920
TXN-88003,ACC-99813,CLIENT_WITHDRAWAL,-2500.00,20260922"""

# 3. Terminated Accounts Raw Source Extract (Amplify Format)
terminated_csv = """LEGACY_ACCT_NO,TERMINATION_EFFECTIVE_DT,CLOSURE_CATEGORY
ACC-OLD-001,2025-01-10,CLIENT_TERMINATED
ACC-OLD-002,2025-05-18,FULL_TRANSFER_OUT"""

# 4. Sleeve Raw Source Extract (Amplify TAMP Format)
sleeves_csv = """REP_CODE_RAW,SRC_ACCOUNT_NO,SUB_SLEEVE_ID,STRATEGY_MODEL_ID,TARGET_WEIGHT_PCT
9981,ACC-99812,SLV-01,AMPLIFY-GROWTH-01,70.0
9981,ACC-99812,SLV-02,AMPLIFY-INCOME-02,30.0"""

with open("test_files/accounts.csv", "w") as f:
    f.write(accounts_csv)

with open("test_files/transactions.csv", "w") as f:
    f.write(transactions_csv)

with open("test_files/terminated_accounts.csv", "w") as f:
    f.write(terminated_csv)

with open("test_files/amplify_sleeves.csv", "w") as f:
    f.write(sleeves_csv)

print("Created realistic legacy test extracts in 'test_files/'.")