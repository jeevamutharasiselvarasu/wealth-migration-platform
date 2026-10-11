import { state, processStageApproval, revokeStageApproval } from '../prototype.js';

export async function renderRulebookEditor(mainContainer, hitlContainer, appState) {
  let isCompiled = false;

  if (!state.rulebookStatus) {
    state.rulebookStatus = 'PENDING';
  }

  // Fetch active rulebook from FastAPI backend
  const response = await fetch(`/api/migrations/${appState.migrationId}/rulebook`);
  const data = await response.json();
  appState.rulebookParsed = data.parsed;

  function renderView() {
    const isApprovedNow = !!state.stageApprovals['03_rules'];
    const currentStatus = isApprovedNow ? 'APPROVED' : (state.rulebookStatus || 'PENDING');

    let gateCardClass = '';
    let statusColor = 'var(--ink)';
    if (currentStatus === 'APPROVED') {
      gateCardClass = 'approved-card';
      statusColor = 'var(--teal-deep)';
    } else if (currentStatus === 'REJECTED') {
      statusColor = 'var(--coral-deep)';
    }

    mainContainer.innerHTML = `
      <div class="stagehead">
        <div class="eyebrow">Control Plane · Stage 03</div>
        <h2>Markdown Rulebook Authoring</h2>
        <p>Human-authored business logic parsed into executable transformations, joins, and crosswalks[cite: 1, 11].</p>
      </div>

      <div class="rulebook-container">
        <div class="editor-pane">
          <div class="pane-header">
            <span>migration-rulebook.md</span>
            <div style="display:flex;align-items:center;gap:8px;">
              <span id="saveStatusIndicator" class="status-pill ok" style="${isCompiled ? 'display:inline-block;' : 'display:none;'}">✓ Compiled & Saved</span>
              <button id="saveRulebookBtn" class="save-btn">Save & Compile Rulebook</button>
            </div>
          </div>
          <textarea id="rulebookMarkdownText" class="code-editor" spellcheck="false">${data.content}</textarea>
        </div>

        <div class="compiled-preview-pane">
          <div class="pane-header">Compiled Rules (Live Inspection)</div>
          <pre class="json-preview" id="compiledPreviewArea">${JSON.stringify(data.parsed, null, 2)}</pre>
        </div>
      </div>
    `;

    hitlContainer.innerHTML = `
      <div class="gatecard ${gateCardClass}" style="${currentStatus === 'REJECTED' ? 'border-color:var(--coral-line); background:var(--coral-bg);' : ''}">
        <h3 style="${currentStatus === 'REJECTED' ? 'color:var(--coral-deep);' : ''}">HITL Gate · Rulebook Review</h3>
        <p>Authorized: <b>Migration Analyst</b></p>
      </div>
      <div class="stat-list">
        <div class="stat"><span>Field Rules:</span> <b id="statFieldRules">${data.parsed.fieldRules ? data.parsed.fieldRules.length : 0}</b></div>
        <div class="stat"><span>Crosswalks:</span> <b id="statCrosswalks">${data.parsed.crosswalks ? data.parsed.crosswalks.length : 0}</b></div>
        <div class="stat"><span>Entity Joins:</span> <b id="statJoins">${data.parsed.joins ? data.parsed.joins.length : 0}</b></div>
        <div class="stat"><span>Validations:</span> <b id="statValidations">${data.parsed.validationRules ? data.parsed.validationRules.length : 0}</b></div>
        <div class="stat"><span>Approval Status:</span> <b style="color:${statusColor};">${currentStatus}</b></div>
      </div>

      <button id="approveRulebookBtn" class="approve-btn ${isApprovedNow ? 'btn-approved' : ''}" ${(!isCompiled && !isApprovedNow) || currentStatus === 'REJECTED' ? 'disabled style="opacity:0.5;cursor:not-allowed;"' : ''} style="margin-bottom:8px;">
        ${isApprovedNow ? '✓ Rulebook Package Approved' : (currentStatus === 'REJECTED' ? '✕ Re-Compile Rulebook First' : 'Approve Rulebook & Continue ->')}
      </button>

      <button id="rejectRulebookBtn" class="action-btn" style="background:var(--coral-bg); color:var(--coral-deep); border:1px solid var(--coral-line);">
        ✕ Reject Rulebook Package
      </button>
    `;

    // Bind Save & Compile Event
    document.getElementById('saveRulebookBtn').addEventListener('click', async () => {
      const updatedContent = document.getElementById('rulebookMarkdownText').value;
      const putRes = await fetch(`/api/migrations/${appState.migrationId}/rulebook`, {
        method: 'PUT',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ content: updatedContent, updatedBy: appState.currentUser || 'migration-analyst' })
      });
      
      if (putRes.ok) {
        const resData = await putRes.json();
        appState.rulebookParsed = resData.parsed;
        data.content = updatedContent;
        data.parsed = resData.parsed;
        isCompiled = true;
        state.rulebookStatus = 'PENDING';

        if (state.stageApprovals['03_rules']) {
          await revokeStageApproval('03_rules');
        }
        
        renderView();
      }
    });

    // Bind Approval Event
    const approveBtn = document.getElementById('approveRulebookBtn');
    if (approveBtn && (isCompiled || isApprovedNow) && currentStatus !== 'REJECTED') {
      approveBtn.addEventListener('click', async () => {
        state.rulebookStatus = 'APPROVED';
        await processStageApproval('03_rules');
      });
    }

    // Bind Rejection Event
    const rejectBtn = document.getElementById('rejectRulebookBtn');
    if (rejectBtn) {
      rejectBtn.addEventListener('click', async () => {
        state.rulebookStatus = 'REJECTED';
        if (state.stageApprovals['03_rules']) {
          await revokeStageApproval('03_rules');
        }
        renderView();
      });
    }
  }

  renderView();
}