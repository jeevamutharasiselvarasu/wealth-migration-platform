import { state, processStageApproval } from '../prototype.js';

export async function renderReconciliationStage(mainContainer, hitlContainer) {
  let isApproved = state.stageApprovals['06_reconcile'];
  let reconData = null;

  async function executeReconciliation() {
    const res = await fetch(`/api/runs/${state.runId}/reconciliation`, { method: 'POST' });
    if (res.ok) {
      reconData = await res.json();
    }
  }

  function renderView() {
    isApproved = state.stageApprovals['06_reconcile'];

    let resultsHtml = '';
    if (reconData) {
      resultsHtml = `
        <div class="results-card">
          <h3>Reconciliation Summary Report</h3>
          <div class="metrics-grid">
            <div class="metric-card ok">
              <div class="m-val">$${reconData.sourceTotalAum.toLocaleString('en-US', {minimumFractionDigits: 2})}</div>
              <div class="m-lbl">Source Total Market Value</div>
            </div>
            <div class="metric-card ok">
              <div class="m-val">$${reconData.targetTotalAum.toLocaleString('en-US', {minimumFractionDigits: 2})}</div>
              <div class="m-lbl">Target Total Market Value</div>
            </div>
            <div class="metric-card ok">
              <div class="m-val">$${reconData.aumVariance.toFixed(2)} (${reconData.aumVariancePct.toFixed(2)}%)</div>
              <div class="m-lbl">AUM Variance (${reconData.withinTolerance ? 'Within Tolerance' : 'Exceeds Tolerance'})</div>
            </div>
          </div>

          <h4 style="margin-top: 16px;">Domain Population Control Totals</h4>
          <table class="data-table">
            <thead>
              <tr><th>Domain Entity</th><th>Source Count</th><th>Target Count</th><th>Variance</th><th>Status</th></tr>
            </thead>
            <tbody>
              ${reconData.domainResults.map(d => `
                <tr>
                  <td>${d.domain}</td>
                  <td>${d.sourceCount}</td>
                  <td>${d.targetCount}</td>
                  <td>${d.variance}</td>
                  <td><span class="status-pill ok">${d.matched ? '✓ Matched' : '⚠ Discrepancy'}</span></td>
                </tr>
              `).join('')}
            </tbody>
          </table>
        </div>
      `;
    } else {
      resultsHtml = `
        <div class="placeholder-box">
          <p><b>Reconciliation Pending</b></p>
          <p class="sub-text">Click "▶ Run Financial & Population Reconciliation" to compare source and target totals dynamically[cite: 8, 10].</p>
        </div>
      `;
    }

    mainContainer.innerHTML = `
      <div class="stagehead">
        <div class="eyebrow">Data Plane · Stage 06</div>
        <h2>Financial Reconciliation Agent</h2>
        <p>Independent audit comparing source extracts against loaded target entities across all knowledge base domains[cite: 8, 10].</p>
      </div>

      <button id="runReconciliationBtn" class="run-pipeline-btn">▶ Run Financial & Population Reconciliation</button>

      <div id="reconResults" class="output-results">
        ${resultsHtml}
      </div>
    `;

    hitlContainer.innerHTML = `
      <div class="gatecard ${isApproved ? 'approved-card' : ''}">
        <h3>HITL Gate · Final Reconciliation</h3>
        <p>Authorized: <b>Validation Specialist</b>[cite: 8, 12]</p>
      </div>
      <div class="stat-list">
        <div class="stat"><span>Drift Tolerance:</span> <b>±0.05%</b></div>
        <div class="stat"><span>AUM Variance:</span> <b>$${reconData ? reconData.aumVariance.toFixed(2) : '0.00'}</b></div>
        <div class="stat"><span>Reconciliation Status:</span> <b>${isApproved ? 'APPROVED' : (reconData ? 'MATCHED' : 'PENDING')}</b></div>
      </div>
      <button id="finalSignOffBtn" class="approve-btn ${isApproved ? 'btn-approved' : ''}">
        ${isApproved ? '✓ Final Sign-Off Complete (LOAD_READY)' : 'Final Sign-Off (Mark LOAD_READY)'}
      </button>
    `;

    document.getElementById('runReconciliationBtn').addEventListener('click', async () => {
      await executeReconciliation();
      renderView();
    });

    document.getElementById('finalSignOffBtn').addEventListener('click', async () => {
      await processStageApproval('06_reconcile');
    });
  }

  renderView();
}