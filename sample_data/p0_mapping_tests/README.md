# P0 mapping review test pack

These are synthetic test files only. They contain no real client or account data.

## Files
- `account_master_approve.csv`: source fields already close to canonical Account fields; use to exercise straightforward suggestions.
- `account_master_review.csv`: alternate source column names; use to inspect alias candidates and reject uncertain suggestions.
- `account_name_address.csv` and `account_type.csv`: separate Account source roles. Upload each under its own role; do not join them without documented join keys.
- `client_master.csv` and `client_model.csv`: separate Client files with different source schemas.
- `household_master.csv` and `household_mapping.csv`: separate Household files with alternate naming.

## Manual approval/rejection walkthrough
1. Open **Entity Sample Files** and upload the relevant file under its matching entity and source role.
2. Open **Agent Pipeline Studio** and run source-to-canonical mapping. Confirm proposals appear in **Pending per-column decisions**.
3. For a confident, semantically correct proposal, enter a reviewer and optionally a comment, then select **Approve**.
4. For a wrong or ambiguous proposal, enter a reviewer and a reason, then select **Reject**.
5. Refresh the page or query `GET /api/mapping-suggestions?status_filter=ALL`. Approved suggestions should have status `APPROVED`; rejected ones should have status `REJECTED`.
6. Only approved suggestions create reusable confirmed canonical mappings. Rejected suggestions remain in the audit trail and do not become reusable mappings. Re-reviewing a decided suggestion should return HTTP 409.

## Important
Approval means a reviewer accepts the proposed source-field meaning and supported transformation—not merely that the column names look similar. Do not approve joins based only on matching-looking values. P0 is preview/review only; it does not load canonical output or assign migration waves.
