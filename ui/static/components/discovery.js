import { state, processStageApproval, revokeStageApproval } from '../prototype.js';

export async function renderDiscoveryStage(mainContainer, hitlContainer) {
  let isProfileRun = state.stageApprovals['02_discovery'];
  let discoveryData = null;

  if (!state.entityDiscoveryStatus) {
    state.entityDiscoveryStatus = {};
  }

  function getEntityStatus(uniqueKey) {
    return state.entityDiscoveryStatus[uniqueKey] || 'PENDING';
  }

  async function executeProfiling() {
    const res = await fetch(`/api/runs/${state.runId}/discovery`, { method: 'POST' });
    if (res.ok) {
      discoveryData = await res.json();
      isProfileRun = true;

      if (discoveryData.discoveredEntities) {
        discoveryData.discoveredEntities.forEach(ent => {
          const key = `${ent.entity}::${ent.sourceFile}`;
          if (!state.entityDiscoveryStatus[key]) {
            state.entityDiscoveryStatus[key] = 'PENDING';
          }
        });
      }
    }
  }

  function renderView() {
    const isStageApproved = !!state.stageApprovals['02_discovery'];

    let totalEntities = 0;
    let approvedEntities = 0;
    let rejectedEntities = 0;
    let pendingEntities = 0;

    if (discoveryData && discoveryData.discoveredEntities) {
      totalEntities = discoveryData.discoveredEntities.length;
      discoveryData.discoveredEntities.forEach(ent => {
        const key = `${ent.entity}::${ent.sourceFile}`;
        const st = getEntityStatus(key);
        if (st === 'APPROVED') approvedEntities++;
        else if (st === 'REJECTED') rejectedEntities++;
        else pendingEntities++;
      });
    }

    const canApproveStage = totalEntities > 0 && approvedEntities === totalEntities && rejectedEntities === 0;

    let resultsHtml = '';
    if (isProfileRun && discoveryData) {
      resultsHtml = `
        <div class="results-card">
          <h3>Discovered Entity Schemas (${discoveryData.entityCount} Source Extract Files Inferred)</h3>
          <div class="entity-schema-grid">
            ${discoveryData.discoveredEntities.map(ent => {
              const uniqueKey = `${ent.entity}::${ent.sourceFile}`;
              const status = getEntityStatus(uniqueKey);
              let statusPillClass = '';
              let statusPillText = 'Pending';
              let borderColor = 'var(--line)';
              let bgColor = '#FCFDFF';

              if (status === 'APPROVED') {
                statusPillClass = 'ok';
                statusPillText = '✓ Approved';
                borderColor = 'var(--teal)';
              } else if (status === 'REJECTED') {
                statusPillClass = 'error';
                statusPillText = '✕ Rejected';
                borderColor = 'var(--coral)';
                bgColor = 'var(--coral-bg)';
              }

              return `
                <div class="entity-block" style="margin-top:12px; padding:14px; border:1px solid ${borderColor}; border-top: 3px solid ${borderColor}; border-radius:10px; background:${bgColor};">
                  <div style="display:flex; justify-content:space-between; align-items:center; margin-bottom:8px;">
                    <h4 style="color:var(--navy); margin:0;">${ent.entity} Entity (<code>${ent.sourceFile}</code>)</h4>
                    <span class="status-pill ${statusPillClass}" style="${status === 'REJECTED' ? 'background:var(--coral-bg); color:var(--coral-deep); border:1px solid var(--coral-line);' : ''}">${statusPillText}</span>
                  </div>

                  <ul class="field-list" style="list-style:none; padding-left:0; margin-bottom:12px;">
                    ${ent.fields.map(f => `
                      <li style="font-size:11.5px; margin-bottom:4px; color:var(--muted);">
                        <code>${f.column}</code> (${f.type}) 
                        ${f.candidateKey ? ' → <b style="color:var(--teal-deep);">[Candidate Key]</b>' : ''}
                        ${f.requiresCrosswalk ? ' → <b style="color:var(--amber);">[Requires Crosswalk]</b>' : ''}
                      </li>
                    `).join('')}
                  </ul>

                  <div style="display:flex; gap:8px; justify-content:flex-end;">
                    <button class="action-btn-sm btn-edit-discovery-entity" data-source-file="${ent.sourceFile}" style="background:#fff; color:var(--navy);">
                      ✏ Edit Schema
                    </button>
                    <button class="action-btn-sm btn-approve-discovery-entity" data-unique-key="${uniqueKey}" style="background:${status === 'APPROVED' ? 'var(--teal-bg)' : '#fff'}; color:${status === 'APPROVED' ? 'var(--teal-deep)' : 'var(--navy)'};">
                      ${status === 'APPROVED' ? '✓ Approved' : 'Approve Schema'}
                    </button>
                    <button class="action-btn-sm btn-reject-discovery-entity" data-unique-key="${uniqueKey}" style="background:${status === 'REJECTED' ? 'var(--coral-bg)' : '#fff'}; color:${status === 'REJECTED' ? 'var(--coral-deep)' : 'var(--coral)'}; border-color:var(--coral-line);">
                      ${status === 'REJECTED' ? '✕ Rejected' : 'Reject Schema'}
                    </button>
                  </div>
                </div>
              `;
            }).join('')}
          </div>
        </div>
      `;
    } else {
      resultsHtml = `
        <div class="placeholder-box">
          <p><b>Profiling Pending</b></p>
          <p class="sub-text">Click "▶ Run Discovery Agent Profiling" above to profile source extracts and discover entity schemas across knowledge base domains[cite: 8, 10].</p>
        </div>
      `;
    }

    mainContainer.innerHTML = `
      <div class="stagehead">
        <div class="eyebrow">Control Plane · Stage 02</div>
        <h2>Discovery Agent & Profiler</h2>
        <p>Profiles source columns, detects data types, candidate keys, and crosswalks dynamically from knowledge file contracts[cite: 1, 6, 8, 10].</p>
      </div>

      <div class="run-banner">
        <button id="runDiscoveryBtn" class="run-pipeline-btn">
          ${isProfileRun ? '↻ Re-Run Discovery Agent Profiling' : '▶ Run Discovery Agent Profiling'}
        </button>
      </div>

      <div id="discoveryResults" class="output-results">
        ${resultsHtml}
      </div>
    `;

    hitlContainer.innerHTML = `
      <div class="gatecard ${isStageApproved ? 'approved-card' : ''}">
        <h3>HITL Gate · Discovery Approval</h3>
        <p>Authorized: <b>Migration Analyst</b>[cite: 8, 12]</p>
      </div>
      <div class="stat-list">
        <div class="stat"><span>Profile Status:</span> <b>${isProfileRun ? 'PROFILED' : 'PENDING'}</b></div>
        <div class="stat"><span>Source Extract Files:</span> <b>${totalEntities} Files</b></div>
        <div class="stat"><span>Approved Schemas:</span> <b style="color:var(--teal-deep);">${approvedEntities}</b></div>
        <div class="stat"><span>Rejected Schemas:</span> <b style="color:var(--coral-deep);">${rejectedEntities}</b></div>
        <div class="stat"><span>Pending Schemas:</span> <b>${pendingEntities}</b></div>
        <div class="stat"><span>Approval Status:</span> <b>${isStageApproved ? 'APPROVED' : 'PENDING'}</b></div>
      </div>

      <button id="approveDiscoveryBtn" class="approve-btn ${isStageApproved ? 'btn-approved' : ''}" ${(!canApproveStage || !isProfileRun) && !isStageApproved ? 'disabled style="opacity:0.5;cursor:not-allowed;"' : ''} style="margin-bottom:8px;">
        ${isStageApproved ? '✓ Discovered Model Approved' : (rejectedEntities > 0 ? '✕ Clear Rejected Schemas First' : 'Approve Model & Continue ->')}
      </button>

      <button id="rejectDiscoveryStageBtn" class="action-btn" style="background:var(--coral-bg); color:var(--coral-deep); border:1px solid var(--coral-line);">
        ✕ Reject Discovery Model
      </button>
    `;

    // Bind Entity Approval / Rejection / Edit Events
    mainContainer.querySelectorAll('.btn-approve-discovery-entity').forEach(btn => {
      btn.addEventListener('click', async () => {
        const uniqueKey = btn.getAttribute('data-unique-key');
        state.entityDiscoveryStatus[uniqueKey] = 'APPROVED';

        if (state.stageApprovals['02_discovery']) {
          await revokeStageApproval('02_discovery');
        }
        renderView();
      });
    });

    mainContainer.querySelectorAll('.btn-reject-discovery-entity').forEach(btn => {
      btn.addEventListener('click', async () => {
        const uniqueKey = btn.getAttribute('data-unique-key');
        state.entityDiscoveryStatus[uniqueKey] = 'REJECTED';

        if (state.stageApprovals['02_discovery']) {
          await revokeStageApproval('02_discovery');
        }
        renderView();
      });
    });

    mainContainer.querySelectorAll('.btn-edit-discovery-entity').forEach(btn => {
      btn.addEventListener('click', () => {
        const sourceFile = btn.getAttribute('data-source-file');
        const entityObj = discoveryData.discoveredEntities.find(e => e.sourceFile === sourceFile);
        if (entityObj) {
          openEditDiscoveryModal(entityObj);
        }
      });
    });

    // Profiling Trigger Event
    document.getElementById('runDiscoveryBtn').addEventListener('click', async () => {
      await executeProfiling();
      if (state.stageApprovals['02_discovery']) {
        await revokeStageApproval('02_discovery');
      }
      renderView();
    });

    // Stage Sign-Off Event
    const approveBtn = document.getElementById('approveDiscoveryBtn');
    if (approveBtn && canApproveStage) {
      approveBtn.addEventListener('click', async () => {
        await processStageApproval('02_discovery');
      });
    }

    // Stage Rejection Event
    const rejectStageBtn = document.getElementById('rejectDiscoveryStageBtn');
    if (rejectStageBtn) {
      rejectStageBtn.addEventListener('click', async () => {
        if (discoveryData && discoveryData.discoveredEntities) {
          discoveryData.discoveredEntities.forEach(ent => {
            const uniqueKey = `${ent.entity}::${ent.sourceFile}`;
            state.entityDiscoveryStatus[uniqueKey] = 'REJECTED';
          });
        }
        await revokeStageApproval('02_discovery');
        renderView();
      });
    }
  }

  // Interactive Edit Schema Modal for Stage 02
  function openEditDiscoveryModal(entityObj) {
    const modalContainer = document.getElementById('modalContainer');

    modalContainer.innerHTML = `
      <div class="kb-modal-overlay">
        <div class="kb-modal-card" style="max-width: 580px; height: auto; border-radius: 16px;">
          <div class="kb-modal-header" style="background: var(--navy); color: #fff; padding: 16px 20px;">
            <div class="kb-modal-title">
              <span class="mono-tag" style="background: var(--blue);">SCHEMA EDITOR</span>
              <h3 style="font-size: 15px; margin-left: 8px;">Edit ${entityObj.entity} Schema (${entityObj.sourceFile})</h3>
            </div>
            <button id="closeDiscoveryModalBtn" class="close-modal-btn">✕</button>
          </div>

          <div class="kb-modal-body" style="padding: 20px;">
            <p class="kb-modal-sub" style="margin-bottom: 14px; font-size: 12px; color: var(--muted);">
              Modify candidate keys and column definitions for <b>${entityObj.entity}</b> (<code>${entityObj.sourceFile}</code>)[cite: 1, 6, 8, 10, 20].
            </p>

            <div class="form-group" style="margin-bottom: 14px;">
              <label style="font-size: 9px; font-weight: 700; color: var(--faint); display: block; margin-bottom: 4px;">CANDIDATE KEYS (Comma-separated)</label>
              <input type="text" id="editCandidateKeysInput" value="${entityObj.candidateKeys.join(', ')}" style="width: 100%; padding: 8px; border: 1.5px solid var(--line); border-radius: 6px; font-size: 12px;" />
            </div>

            <div class="form-group" style="margin-bottom: 14px;">
              <label style="font-size: 9px; font-weight: 700; color: var(--faint); display: block; margin-bottom: 4px;">DISCOVERED FIELDS & TYPES (One per line: column, type)</label>
              <textarea id="editFieldsTextarea" style="width: 100%; height: 160px; font-family: 'IBM Plex Mono', monospace; font-size: 11px; padding: 10px; border: 1.5px solid var(--line); border-radius: 6px; resize: vertical;">${entityObj.fields.map(f => `${f.column},${f.type}`).join('\n')}</textarea>
            </div>
          </div>

          <div class="kb-modal-footer" style="padding: 14px 20px; background: var(--panel-2); border-top: 1px solid var(--line); display: flex; justify-content: flex-end; gap: 10px;">
            <button id="cancelDiscoveryModalBtn" class="btn-secondary">Cancel</button>
            <button id="saveDiscoveryEditsBtn" class="btn-primary-action">Save Schema Edits</button>
          </div>
        </div>
      </div>
    `;

    const closeFn = () => { modalContainer.innerHTML = ''; };
    document.getElementById('closeDiscoveryModalBtn').addEventListener('click', closeFn);
    document.getElementById('cancelDiscoveryModalBtn').addEventListener('click', closeFn);

    document.getElementById('saveDiscoveryEditsBtn').addEventListener('click', async () => {
      const newKeysStr = document.getElementById('editCandidateKeysInput').value;
      const newFieldsStr = document.getElementById('editFieldsTextarea').value;

      entityObj.candidateKeys = newKeysStr.split(',').map(s => s.trim()).filter(Boolean);

      const parsedFields = [];
      newFieldsStr.split('\n').forEach(line => {
        const parts = line.split(',').map(s => s.trim());
        if (parts[0]) {
          const colName = parts[0];
          const colType = parts[1] || 'string';
          parsedFields.push({
            column: colName,
            type: colType,
            candidateKey: entityObj.candidateKeys.includes(colName),
            nullRate: '0.00%',
            requiresCrosswalk: ['custodian', 'reg_type', 'txn_code'].includes(colName)
          });
        }
      });

      entityObj.fields = parsedFields;
      const uniqueKey = `${entityObj.entity}::${entityObj.sourceFile}`;
      state.entityDiscoveryStatus[uniqueKey] = 'PENDING';

      if (state.stageApprovals['02_discovery']) {
        await revokeStageApproval('02_discovery');
      }

      closeFn();
      renderView();
    });
  }

  if (isProfileRun && !discoveryData) {
    executeProfiling().then(() => renderView());
  } else {
    renderView();
  }
}