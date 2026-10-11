import { state, processStageApproval } from '../prototype.js';

export async function renderExecutionStage(mainContainer, hitlContainer) {
  let exportMode = 'local'; // 'local' or 'adls'

  function renderView() {
    const isApproved = state.stageApprovals['05_execute'];

    mainContainer.innerHTML = `
      <div class="stagehead">
        <div class="eyebrow">Data Plane · Stage 05</div>
        <h2>ADF & Transformation Execution</h2>
        <p>Export approved transformation packages to Local Storage or Cloud ADLS paths[cite: 8, 10].</p>
      </div>

      <div class="toggle-mode-bar">
        <span class="toggle-label">PACKAGE EXPORT TARGET:</span>
        <button id="expLocalBtn" class="toggle-btn ${exportMode === 'local' ? 'active' : ''}">💾 Local Storage / File Download</button>
        <button id="expAdlsBtn" class="toggle-btn ${exportMode === 'adls' ? 'active' : ''}">☁ ADLS Gen2 Cloud Path</button>
      </div>

      <div class="execution-actions-card">
        <div class="card-section-title">PACKAGE EXPORT & EXECUTION</div>
        <h3>Generate Transformation Package</h3>
        <p class="section-sub">
          ${exportMode === 'local' 
            ? 'Package outputs <code>transformation_rules.sql</code> to local <code>App_Data/Storage/</code> directory for local execution[cite: 2, 7].'
            : 'Package exports directly to ADLS <code>abfss://reconciled-gold@...</code> for Azure Data Factory[cite: 8, 10].'}
        </p>
        
        <button id="exportPackageBtn" class="run-pipeline-btn">
          ▶ Export Transformation Package (${exportMode.toUpperCase()})
        </button>
      </div>

      <div id="executionResults" class="output-results"></div>
    `;

    hitlContainer.innerHTML = `
      <div class="gatecard ${isApproved ? 'approved-card' : ''}">
        <h3>HITL Gate · Stage 05 Approval</h3>
        <p>Authorized: <b>Validation Specialist</b>[cite: 8, 12]</p>
      </div>
      <div class="stat-list">
        <div class="stat"><span>Export Target:</span> <b>${exportMode.toUpperCase()}</b></div>
        <div class="stat"><span>Approval Status:</span> <b>${isApproved ? 'APPROVED' : 'PENDING'}</b></div>
      </div>
      <button id="approveExecutionBtn" class="approve-btn ${isApproved ? 'btn-approved' : ''}">
        ${isApproved ? '✓ Execution Stage Approved' : 'Approve Stage 05 & Continue ->'}
      </button>
    `;

    document.getElementById('expLocalBtn').addEventListener('click', () => { exportMode = 'local'; renderView(); });
    document.getElementById('expAdlsBtn').addEventListener('click', () => { exportMode = 'adls'; renderView(); });

    document.getElementById('exportPackageBtn').addEventListener('click', async () => {
      const res = await fetch(`/api/runs/${state.runId}/export-package?mode=${exportMode}`);
      const data = await res.json();

      document.getElementById('executionResults').innerHTML = `
        <div class="results-card">
          <h3>Transformation Package Exported</h3>
          <p class="file-path">${data.targetLocation}</p>

          <table class="data-table">
            <thead>
              <tr><th>Artifact</th><th>Export Location</th><th>Status</th></tr>
            </thead>
            <tbody>
              <tr><td class="mono">transformation_rules.sql</td><td class="mono">${data.targetLocation}</td><td><span class="status-pill ok">✓ Exported</span></td></tr>
              <tr><td class="mono">validation_plan.json</td><td class="mono">${data.targetLocation}</td><td><span class="status-pill ok">✓ Exported</span></td></tr>
            </tbody>
          </table>
        </div>
      `;
    });

    document.getElementById('approveExecutionBtn').addEventListener('click', async () => {
      await processStageApproval('05_execute');
    });
  }

  renderView();
}