import { state, processStageApproval, revokeStageApproval } from '../prototype.js';

function formatDomainLabel(domain) {
  return domain
    .split('_')
    .map(word => word.charAt(0).toUpperCase() + word.slice(1))
    .join(' ');
}

function normalizeKey(str) {
  return (str || '').toLowerCase().replace(/[^a-z0-9]/g, '');
}

export async function renderIngestionStage(mainContainer, hitlContainer) {
  let mode = 'cloud';

  // 1. Fetch live knowledge assets directly from backend REST API[cite: 14]
  let profileDomains = ['clients_households', 'accounts', 'fees', 'billing', 'positions', 'transactions'];
  let fileContracts = [];

  try {
    const spRes = await fetch(`/api/migrations/${state.migrationId}/knowledge/source_profile`);
    if (spRes.ok) {
      const spData = await spRes.json();
      if (spData.content && Array.isArray(spData.content.domains)) {
        profileDomains = spData.content.domains;
      }
    }

    const fcRes = await fetch(`/api/migrations/${state.migrationId}/knowledge/file_contracts`);
    if (fcRes.ok) {
      const fcData = await fcRes.json();
      if (fcData.content && Array.isArray(fcData.content.fileContracts)) {
        fileContracts = fcData.content.fileContracts;
      }
    }
  } catch (err) {
    console.warn('Fallback to default ingestion domain list', err);
  }

  // 2. Dynamically derive all entities from BOTH source_profile and file_contracts[cite: 14]
  const entitiesMap = {}; // normKey -> displayLabel

  profileDomains.forEach(dom => {
    const label = formatDomainLabel(dom);
    const norm = normalizeKey(label);
    if (!entitiesMap[norm]) {
      entitiesMap[norm] = label;
    }
  });

  fileContracts.forEach(fc => {
    if (fc.entity) {
      const label = fc.entity.trim();
      const norm = normalizeKey(label);
      if (!entitiesMap[norm]) {
        entitiesMap[norm] = label;
      }
    }
  });

  if (!state.customAuxiliaryFiles) {
    state.customAuxiliaryFiles = {};
  }

  // 3. Rebuild entity file slots dynamically on EVERY render
  const dynamicEntitySlots = {};
  const entityEntries = Object.entries(entitiesMap);

  entityEntries.forEach(([normKey, displayLabel]) => {
    const matchingContracts = fileContracts.filter(fc => {
      const fcNorm = normalizeKey(fc.entity);
      return fcNorm === normKey || fcNorm.includes(normKey) || normKey.includes(fcNorm);
    });

    if (matchingContracts.length > 0) {
      dynamicEntitySlots[normKey] = matchingContracts.map((fc, idx) => ({
        fileId: `${normKey}_contract_${idx}`,
        label: idx === 0 ? `Primary Contract (${fc.fileName})` : `Auxiliary Contract (${fc.fileName})`,
        file: fc.fileName,
        type: idx === 0 ? 'PRIMARY' : 'AUXILIARY',
        format: fc.format || 'csv',
        delimiter: fc.delimiter || ','
      }));
    } else {
      dynamicEntitySlots[normKey] = [
        {
          fileId: `${normKey}_primary`,
          label: `Primary Extract (${normKey}.csv)`,
          file: `morningstar_${normKey}.csv`,
          type: 'PRIMARY',
          format: 'csv',
          delimiter: ','
        }
      ];
    }

    if (state.customAuxiliaryFiles[normKey]) {
      dynamicEntitySlots[normKey].push(...state.customAuxiliaryFiles[normKey]);
    }
  });

  state.entityFileSlots = dynamicEntitySlots;

  function renderView() {
    const isApproved = state.stageApprovals['01_ingestion'];
    let totalFileCount = 0;

    Object.values(state.entityFileSlots).forEach(files => { totalFileCount += files.length; });

    const entitySectionsHtml = entityEntries.map(([normKey, displayLabel]) => {
      const files = state.entityFileSlots[normKey] || [];

      const filesHtml = mode === 'cloud' ? `
        <div class="file-slots-grid">
          ${files.map(f => `
            <div class="slot-card">
              <div class="slot-header">
                <span class="slot-title">${f.label}</span>
                <span class="status-pill ${f.type === 'PRIMARY' ? 'ok' : 'pending'}">${f.type}</span>
              </div>
              <div class="slot-body">
                <p class="file-path">abfss://raw-landing@xodusamp.dfs.core.windows.net/morningstar/${f.file}</p>
                <div class="slot-status" style="display:flex; justify-content:space-between; align-items:center;">
                  <span class="status-pill ok">✓ ADLS Linked</span>
                  <span style="font-size:10px; color:var(--faint); font-family:'IBM Plex Mono',monospace;">Format: ${f.format.toUpperCase()} (${f.delimiter})</span>
                </div>
              </div>
            </div>
          `).join('')}
        </div>
      ` : `
        <div class="multi-upload-grid">
          ${files.map(f => `
            <div class="local-upload-card" id="card-${f.fileId}">
              <div class="slot-header">
                <span class="slot-title">${f.label}</span>
                <span class="status-pill ${f.type === 'PRIMARY' ? 'ok' : 'pending'}">${f.type}</span>
              </div>
              <p class="section-sub" style="margin-bottom: 6px;">Contract File: <code>${f.file}</code> (${f.format.toUpperCase()})</p>
              
              <div class="upload-dropzone-compact">
                <input type="file" id="fileInput-${f.fileId}" class="file-input-hidden" />
                <button class="action-btn-sm btn-upload-trigger" data-slot-id="${f.fileId}" data-entity="${displayLabel}">
                  📁 Pick Extract for ${f.file}
                </button>
              </div>
              <div id="status-${f.fileId}" class="upload-msg-sm"></div>
            </div>
          `).join('')}
        </div>
      `;

      return `
        <div class="entity-ingestion-card" style="border: 1px solid var(--line); border-radius: 12px; background: var(--panel); padding: 18px; margin-bottom: 20px;">
          <div class="card-header" style="display:flex; justify-content:space-between; align-items:center; border-bottom: 1px solid var(--line); padding-bottom: 10px; margin-bottom: 14px;">
            <div>
              <span class="entity-badge" style="font-size: 11px;">${displayLabel} Entity</span>
              <span style="font-size: 11.5px; color: var(--muted); margin-left: 8px;">
                (${files.length} contract ${files.length === 1 ? 'file' : 'files'} configured)
              </span>
            </div>
            <button class="action-btn-sm btn-add-aux-file" data-norm-key="${normKey}" data-display-label="${displayLabel}">
              + Add Auxiliary Source File
            </button>
          </div>

          ${filesHtml}
        </div>
      `;
    }).join('');

    mainContainer.innerHTML = `
      <div class="stagehead">
        <div class="eyebrow">Control Plane · Stage 01</div>
        <h2>Loader & File Contracts Inspector</h2>
        <p>Ingest primary extracts and auxiliary lookup feeds derived directly from knowledge file contracts.</p>
      </div>

      <div class="toggle-mode-bar">
        <span class="toggle-label">STORAGE SOURCE:</span>
        <button id="modeCloudBtn" class="toggle-btn ${mode === 'cloud' ? 'active' : ''}">☁ Cloud Storage (ADLS / AWS)</button>
        <button id="modeLocalBtn" class="toggle-btn ${mode === 'local' ? 'active' : ''}">📁 Local File Upload (${totalFileCount} Contract Files)</button>
      </div>

      <div class="entity-domain-ingestion-list">
        ${entitySectionsHtml}
      </div>

      <div class="run-banner">
        <button id="runIngestionBtn" class="run-pipeline-btn">▶ Execute Contract File Ingestion (${totalFileCount} Files Across ${entityEntries.length} Entities)</button>
      </div>

      <div id="ingestionResults" class="output-results"></div>
    `;

    hitlContainer.innerHTML = `
      <div class="gatecard ${isApproved ? 'approved-card' : ''}">
        <h3>HITL Gate · Stage 01 Approval</h3>
        <p>Authorized: <b>Migration Analyst</b>[cite: 8, 12]</p>
      </div>
      <div class="stat-list">
        <div class="stat"><span>Ingestion Mode:</span> <b>${mode.toUpperCase()}</b></div>
        <div class="stat"><span>Entity Domains:</span> <b>${entityEntries.length}</b></div>
        <div class="stat"><span>Contract Files:</span> <b>${totalFileCount}</b></div>
        <div class="stat"><span>Approval Status:</span> <b>${isApproved ? 'APPROVED' : 'PENDING'}</b></div>
      </div>

      <button id="approveIngestionBtn" class="approve-btn ${isApproved ? 'btn-approved' : ''}">
        ${isApproved ? '✓ File Contracts Ingestion Approved' : 'Approve Ingestion & Continue ->'}
      </button>
    `;

    document.getElementById('modeCloudBtn').addEventListener('click', () => { mode = 'cloud'; renderView(); });
    document.getElementById('modeLocalBtn').addEventListener('click', () => { mode = 'local'; renderView(); });

    // Open Auxiliary File Mapper Modal
    mainContainer.querySelectorAll('.btn-add-aux-file').forEach(btn => {
      btn.addEventListener('click', () => {
        const normKey = btn.getAttribute('data-norm-key');
        const displayLabel = btn.getAttribute('data-display-label');
        openAuxiliaryMapperModal(displayLabel, normKey);
      });
    });

    // Local File Upload Handlers
    if (mode === 'local') {
      mainContainer.querySelectorAll('.btn-upload-trigger').forEach(btn => {
        btn.addEventListener('click', () => {
          const slotId = btn.getAttribute('data-slot-id');
          const entity = btn.getAttribute('data-entity');
          const inputEl = document.getElementById(`fileInput-${slotId}`);

          if (!inputEl) return;

          inputEl.onchange = async (e) => {
            const file = e.target.files[0];
            if (!file) return;

            const statusEl = document.getElementById(`status-${slotId}`);
            if (statusEl) {
              statusEl.innerHTML = `<span class="status-pill pending">Uploading ${file.name}...</span>`;
            }

            const formData = new FormData();
            formData.append('file', file);
            formData.append('entity', entity);

            const res = await fetch(`/api/runs/${state.runId}/upload-file`, {
              method: 'POST',
              body: formData
            });

            if (res.ok) {
              const data = await res.json();
              if (statusEl) {
                statusEl.innerHTML = `<span class="status-pill ok">✓ Stored: ${data.filename} (${data.sizeBytes} B)</span>`;
              }
              if (state.stageApprovals['01_ingestion']) {
                await revokeStageApproval('01_ingestion');
              }
            }
          };

          inputEl.click();
        });
      });
    }

    // Execute Ingestion Event
    document.getElementById('runIngestionBtn').addEventListener('click', () => {
      const summaryRows = [];
      entityEntries.forEach(([normKey, displayLabel]) => {
        const files = state.entityFileSlots[normKey] || [];
        files.forEach(f => {
          summaryRows.push(`
            <tr>
              <td class="mono">${f.file}</td>
              <td>${displayLabel}</td>
              <td><span class="status-pill ${f.type === 'PRIMARY' ? 'ok' : 'pending'}">${f.type}</span></td>
              <td class="mono">${f.format.toUpperCase()} (${f.delimiter})</td>
              <td><span class="status-pill ok">✓ RAW Stored</span></td>
            </tr>
          `);
        });
      });

      document.getElementById('ingestionResults').innerHTML = `
        <div class="results-card">
          <h3>File Contracts Ingestion Summary (${totalFileCount} Files)</h3>
          <table class="data-table">
            <thead>
              <tr><th>Source File Name</th><th>Target Entity</th><th>File Role</th><th>Format</th><th>Status</th></tr>
            </thead>
            <tbody>
              ${summaryRows.join('')}
            </tbody>
          </table>
        </div>
      `;
    });

    document.getElementById('approveIngestionBtn').addEventListener('click', async () => {
      await processStageApproval('01_ingestion');
    });
  }

  // Interactive Auxiliary File Mapper Modal
  function openAuxiliaryMapperModal(targetEntityLabel, normKey) {
    const modalContainer = document.getElementById('modalContainer');

    modalContainer.innerHTML = `
      <div class="kb-modal-overlay">
        <div class="kb-modal-card" style="max-width: 540px; height: auto; border-radius: 16px;">
          <div class="kb-modal-header" style="background: var(--navy); color: #fff; padding: 16px 20px;">
            <div class="kb-modal-title">
              <span class="mono-tag" style="background: var(--teal);">AUX FILE MAPPER</span>
              <h3 style="font-size: 15px; margin-left: 8px;">Attach / Re-map Auxiliary File</h3>
            </div>
            <button id="closeAuxModalBtn" class="close-modal-btn">✕</button>
          </div>

          <div class="kb-modal-body" style="padding: 20px;">
            <p class="kb-modal-sub" style="margin-bottom: 14px; font-size: 12px; color: var(--muted);">
              Define source contract details to attach or re-map an auxiliary file directly to <b>${targetEntityLabel}</b>. Edits automatically persist to <code>file-contract.json</code> and <code>mapping-set.json</code>.
            </p>

            <div class="form-group" style="margin-bottom: 12px;">
              <label style="font-size: 9px; font-weight: 700; color: var(--faint); display: block; margin-bottom: 4px;">AUXILIARY SOURCE FILE NAME</label>
              <input type="text" id="auxFileNameInput" value="morningstar_performance.csv" placeholder="e.g. custodian_lookup.csv" style="width: 100%; padding: 8px; border: 1.5px solid var(--line); border-radius: 6px; font-size: 12px;" />
            </div>

            <div class="form-group" style="margin-bottom: 12px;">
              <label style="font-size: 9px; font-weight: 700; color: var(--faint); display: block; margin-bottom: 4px;">TARGET ENTITY ASSIGNMENT</label>
              <input type="text" id="auxTargetEntityInput" value="${targetEntityLabel}" readonly style="width: 100%; padding: 8px; border: 1.5px solid var(--line); border-radius: 6px; font-size: 12px; background: var(--panel-2);" />
            </div>

            <div style="display: flex; gap: 12px; margin-bottom: 12px;">
              <div class="form-group" style="flex: 1;">
                <label style="font-size: 9px; font-weight: 700; color: var(--faint); display: block; margin-bottom: 4px;">FORMAT</label>
                <select id="auxFormatInput" style="width: 100%; padding: 8px; border: 1.5px solid var(--line); border-radius: 6px; font-size: 12px; background: var(--panel-2);">
                  <option value="csv">CSV</option>
                  <option value="psv">PSV</option>
                  <option value="json">JSON</option>
                </select>
              </div>
              <div class="form-group" style="flex: 1;">
                <label style="font-size: 9px; font-weight: 700; color: var(--faint); display: block; margin-bottom: 4px;">DELIMITER</label>
                <input type="text" id="auxDelimiterInput" value="," style="width: 100%; padding: 8px; border: 1.5px solid var(--line); border-radius: 6px; font-size: 12px;" />
              </div>
            </div>

            <div class="form-group" style="margin-bottom: 12px;">
              <label style="font-size: 9px; font-weight: 700; color: var(--faint); display: block; margin-bottom: 4px;">NATURAL KEYS (Comma-separated)</label>
              <input type="text" id="auxKeysInput" value="account_ref, period_end" style="width: 100%; padding: 8px; border: 1.5px solid var(--line); border-radius: 6px; font-size: 12px;" />
            </div>

            <div class="form-group" style="margin-bottom: 12px;">
              <label style="font-size: 9px; font-weight: 700; color: var(--faint); display: block; margin-bottom: 4px;">FILE COLUMNS (Comma-separated)</label>
              <input type="text" id="auxColsInput" value="account_ref, period_end, return_gross, return_net" style="width: 100%; padding: 8px; border: 1.5px solid var(--line); border-radius: 6px; font-size: 12px;" />
            </div>
          </div>

          <div class="kb-modal-footer" style="padding: 14px 20px; background: var(--panel-2); border-top: 1px solid var(--line); display: flex; justify-content: flex-end; gap: 10px;">
            <button id="cancelAuxModalBtn" class="btn-secondary">Cancel</button>
            <button id="saveAuxContractBtn" class="btn-primary-action">Save Contract & Update Knowledge Base</button>
          </div>
        </div>
      </div>
    `;

    const closeFn = () => { modalContainer.innerHTML = ''; };
    document.getElementById('closeAuxModalBtn').addEventListener('click', closeFn);
    document.getElementById('cancelAuxModalBtn').addEventListener('click', closeFn);

    document.getElementById('saveAuxContractBtn').addEventListener('click', async () => {
      const fileName = document.getElementById('auxFileNameInput').value.trim();
      const targetEntity = document.getElementById('auxTargetEntityInput').value.trim();
      const format = document.getElementById('auxFormatInput').value.trim();
      const delimiter = document.getElementById('auxDelimiterInput').value.trim();
      const keys = document.getElementById('auxKeysInput').value.split(',').map(s => s.trim()).filter(Boolean);
      const cols = document.getElementById('auxColsInput').value.split(',').map(s => s.trim()).filter(Boolean);

      if (!fileName) {
        alert('File name is required');
        return;
      }

      // 1. Update file-contract.json in Knowledge Base[cite: 14]
      try {
        const fcRes = await fetch(`/api/migrations/${state.migrationId}/knowledge/file_contracts`);
        let fcData = { content: { fileContracts: [] } };
        if (fcRes.ok) {
          fcData = await fcRes.json();
        }

        const fileContractsList = (fcData.content && Array.isArray(fcData.content.fileContracts))
          ? fcData.content.fileContracts
          : [];

        const existingIdx = fileContractsList.findIndex(c => c.fileName === fileName);
        const newContract = {
          fileName,
          entity: targetEntity,
          format,
          delimiter,
          naturalKey: keys,
          columns: cols
        };

        if (existingIdx >= 0) {
          fileContractsList[existingIdx] = newContract;
        } else {
          fileContractsList.push(newContract);
        }

        fcData.content = { fileContracts: fileContractsList };

        await fetch(`/api/migrations/${state.migrationId}/knowledge/file_contracts`, {
          method: 'PUT',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ content: fcData.content })
        });

        // 2. Update mapping-set.json in Knowledge Base[cite: 14]
        const msRes = await fetch(`/api/migrations/${state.migrationId}/knowledge/mapping_set`);
        if (msRes.ok) {
          const msData = await msRes.json();
          if (msData.content && Array.isArray(msData.content.fieldMappings)) {
            const existingMsIdx = msData.content.fieldMappings.findIndex(m => m.sourceFile === fileName);
            const fieldPairs = cols.map(c => ({ source: c, target: c }));
            const newMapping = {
              entity: targetEntity,
              sourceFile: fileName,
              targetEntity: `AMK_${targetEntity.replace(/\s+/g, '')}`,
              fieldPairs: fieldPairs
            };

            if (existingMsIdx >= 0) {
              msData.content.fieldMappings[existingMsIdx] = newMapping;
            } else {
              msData.content.fieldMappings.push(newMapping);
            }

            await fetch(`/api/migrations/${state.migrationId}/knowledge/mapping_set`, {
              method: 'PUT',
              headers: { 'Content-Type': 'application/json' },
              body: JSON.stringify({ content: msData.content })
            });
          }
        }

        if (state.stageApprovals['01_ingestion']) {
          await revokeStageApproval('01_ingestion');
        }

        closeFn();
        await renderIngestionStage(mainContainer, hitlContainer);
      } catch (err) {
        console.error('Failed to update auxiliary contract knowledge asset', err);
        alert('Failed to save auxiliary file mapping to knowledge base.');
      }
    });
  }

  renderView();
}