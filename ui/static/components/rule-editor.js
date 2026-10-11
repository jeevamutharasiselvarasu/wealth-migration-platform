import { state, processStageApproval, revokeStageApproval } from '../prototype.js';

function normalizeKey(str) {
  return (str || '').toLowerCase().replace(/[^a-z0-9]/g, '');
}

function generateClientSqlPackage(rules) {
  const lines = [
    `-- ====================================================================`,
    `-- Data Migration Control Plane · Transformation Rules Package`,
    `-- Exported UTC: ${new Date().toISOString()}`,
    `-- ====================================================================\n`
  ];

  const rulesByEnt = {};
  rules.forEach(r => {
    if (!rulesByEnt[r.entity]) rulesByEnt[r.entity] = [];
    rulesByEnt[r.entity].push(r);
  });

  Object.entries(rulesByEnt).forEach(([ent, entRules]) => {
    const viewName = `stg_${ent.toLowerCase().replace(/\s+/g, '_')}`;
    lines.push(`-- Entity Transformation View: ${viewName}`);
    lines.push(`CREATE OR REPLACE VIEW ${viewName} AS`);
    lines.push(`SELECT`);

    const selectExprs = [];
    const sourceFiles = Array.from(new Set(
      entRules.flatMap(r => (r.sourceFile || '').split(',').map(s => s.trim())).filter(Boolean)
    ));
    const primaryFile = sourceFiles[0] || 'raw_table';

    entRules.forEach(r => {
      const targetField = r.targetField;
      let expr = (r.expression || '').trim();

      let sqlExpr = expr.replace(/\{\{\s*(?:[a-zA-Z0-9_\.]+\.)?([a-zA-Z0-9_]+)\s*\}\}/g, '$1');
      if (expr.includes('PERIOD_START(')) {
        const innerVar = expr.replace(/PERIOD_START\(\s*\{\{\s*(?:[a-zA-Z0-9_\.]+\.)?([a-zA-Z0-9_]+)\s*\}\}\s*\)/, '$1');
        selectExprs.push(`    DATE_TRUNC('month', CAST(${innerVar} AS DATE)) AS ${targetField}`);
      } else {
        selectExprs.push(`    ${sqlExpr} AS ${targetField}`);
      }
    });

    lines.push(selectExprs.join(',\n'));
    lines.push(`FROM ${primaryFile.replace('.csv', '').replace('.psv', '')}`);

    if (sourceFiles.length > 1) {
      sourceFiles.slice(1).forEach(extraFile => {
        const cleanExtra = extraFile.replace('.csv', '').replace('.psv', '');
        lines.push(`LEFT JOIN ${cleanExtra} ON ${primaryFile.replace('.csv', '').replace('.psv', '')}.account_ref = ${cleanExtra}.account_ref`);
      });
    }

    lines.push(`;\n`);
  });

  return lines.join('\n');
}

export async function renderRuleEditor(mainContainer, hitlContainer, appState) {
  if (!state.columnApprovals) {
    state.columnApprovals = {};
  }

  if (state.isPackageGenerated === undefined) state.isPackageGenerated = false;
  if (state.isPackageVerified === undefined) state.isPackageVerified = false;
  if (state.generatedSqlContent === undefined) state.generatedSqlContent = '';

  function getRuleStatus(ruleKey) {
    const val = state.columnApprovals[ruleKey];
    if (val === true || val === 'APPROVED') return 'APPROVED';
    if (val === 'REJECTED') return 'REJECTED';
    return 'PENDING';
  }

  // 1. Fetch live active file contracts to resolve column-to-file provenance
  let activeFileContracts = [];
  const fileColumnsMap = {};

  try {
    const fcRes = await fetch(`/api/migrations/${appState.migrationId}/knowledge/file_contracts`);
    if (fcRes.ok) {
      const fcData = await fcRes.json();
      if (fcData.content && Array.isArray(fcData.content.fileContracts)) {
        activeFileContracts = fcData.content.fileContracts;
        
        activeFileContracts.forEach(fc => {
          if (Array.isArray(fc.columns)) {
            fc.columns.forEach(col => {
              const normCol = col.trim().toLowerCase();
              if (!fileColumnsMap[normCol]) {
                fileColumnsMap[normCol] = [];
              }
              if (!fileColumnsMap[normCol].includes(fc.fileName)) {
                fileColumnsMap[normCol].push(fc.fileName);
              }
            });
          }
        });
      }
    }
  } catch (err) {
    console.warn('Failed to load file contracts for mapper', err);
  }

  // 2. Fetch live Target Contract
  let targetContractEntities = {};
  try {
    const tcRes = await fetch(`/api/migrations/${appState.migrationId}/knowledge/target_contract`);
    if (tcRes.ok) {
      const tcData = await tcRes.json();
      if (tcData.content && tcData.content.entities) {
        targetContractEntities = tcData.content.entities;
      }
    }
  } catch (err) {
    console.warn('Failed to load target contract', err);
  }

  // 3. Fetch live compiled rulebook
  let fieldRules = [];
  try {
    const rbRes = await fetch(`/api/migrations/${appState.migrationId}/rulebook`);
    if (rbRes.ok) {
      const data = await rbRes.json();
      appState.rulebookParsed = data.parsed;
      fieldRules = (data.parsed && data.parsed.fieldRules) ? data.parsed.fieldRules : [];
    }
  } catch (err) {
    console.warn('Failed to load rulebook for mapper', err);
  }

  // Group rules by Entity
  const rulesByEntity = {};
  fieldRules.forEach(rule => {
    const key = `${rule.entity}::${rule.targetField}`;
    if (state.columnApprovals[key] === undefined) {
      state.columnApprovals[key] = 'PENDING';
    }
    if (!rulesByEntity[rule.entity]) {
      rulesByEntity[rule.entity] = [];
    }
    rulesByEntity[rule.entity].push(rule);
  });

  function renderView() {
    const isStageApproved = !!state.stageApprovals['04_mapper'];
    const totalRules = fieldRules.length;

    let approvedCount = 0;
    let rejectedCount = 0;
    let pendingCount = 0;

    fieldRules.forEach(r => {
      const st = getRuleStatus(`${r.entity}::${r.targetField}`);
      if (st === 'APPROVED') approvedCount++;
      else if (st === 'REJECTED') rejectedCount++;
      else pendingCount++;
    });

    const areAllRulesApproved = totalRules > 0 && approvedCount === totalRules && rejectedCount === 0;
    const canHandoffControlPlane = areAllRulesApproved && state.isPackageGenerated && state.isPackageVerified;

    let entitySectionsHtml = '';

    if (Object.keys(rulesByEntity).length > 0) {
      const sections = Object.entries(rulesByEntity).map(([entityName, rules]) => {
        let entApproved = 0;
        let entRejected = 0;
        let entPending = 0;

        rules.forEach(r => {
          const st = getRuleStatus(`${r.entity}::${r.targetField}`);
          if (st === 'APPROVED') entApproved++;
          else if (st === 'REJECTED') entRejected++;
          else entPending++;
        });

        const isEntityFullyApproved = entApproved === rules.length;
        const isEntityFullyRejected = entRejected === rules.length;

        const contributingSourceFiles = Array.from(new Set(
          rules.flatMap(r => (r.sourceFile || '').split(',').map(s => s.trim())).filter(Boolean)
        ));

        const targetSpecFields = targetContractEntities[entityName] || [];
        const requiredSpecFields = targetSpecFields.filter(f => f.required).map(f => f.name);
        const mappedTargetFields = rules.map(r => r.targetField);
        const missingRequiredFields = requiredSpecFields.filter(f => !mappedTargetFields.includes(f));
        const isTargetContractCompliant = missingRequiredFields.length === 0;

        const cardsHtml = rules.map(rule => {
          const ruleKey = `${rule.entity}::${rule.targetField}`;
          const status = getRuleStatus(ruleKey);
          const exprStr = (rule.expression || '').trim();

          const fieldSpec = targetSpecFields.find(f => f.name === rule.targetField);
          const isRequiredInTarget = fieldSpec ? fieldSpec.required : false;
          const targetDataType = fieldSpec ? fieldSpec.type.toUpperCase() : 'STRING';

          const varsFound = Array.from(exprStr.matchAll(/\{\{\s*([^\}]+)\s*\}\}/g)).map(m => m[1].trim());
          const columnProvenance = [];

          varsFound.forEach(v => {
            if (v.includes('.')) {
              const lastDot = v.lastIndexOf('.');
              const filePart = v.substring(0, lastDot);
              const colPart = v.substring(lastDot + 1);
              columnProvenance.push({ column: colPart, file: filePart });
            } else {
              const normV = v.toLowerCase();
              const matchingFiles = fileColumnsMap[normV] || [];
              const assignedFile = matchingFiles.length > 0 ? matchingFiles[0] : (rule.sourceFile.split(',')[0].trim());
              columnProvenance.push({ column: v, file: assignedFile });
            }
          });

          let mappingType = 'Complex Transformation';
          if (exprStr.startsWith('{{') && exprStr.endsWith('}}') && !exprStr.includes(' ') && !exprStr.includes('+')) {
            mappingType = 'Direct Field';
          } else if (rule.sourceFile && rule.sourceFile.includes(',')) {
            mappingType = 'Multi-Source Join / Lookup';
          } else if (exprStr.includes('CASE WHEN')) {
            mappingType = 'Conditional Rule (CASE/WHEN)';
          } else if (exprStr.includes('COALESCE')) {
            mappingType = 'Fallback Expression (COALESCE)';
          }

          const safeExpr = exprStr.replace(/"/g, '&quot;');

          let borderTopColor = 'var(--blue)';
          let statusPillClass = '';
          let statusPillText = 'Pending';

          if (status === 'APPROVED') {
            borderTopColor = 'var(--teal)';
            statusPillClass = 'ok';
            statusPillText = '✓ Approved';
          } else if (status === 'REJECTED') {
            borderTopColor = 'var(--coral)';
            statusPillClass = 'error';
            statusPillText = '✕ Rejected';
          }

          return `
            <div class="rule-card" style="border-top: 3px solid ${borderTopColor}; background: ${status === 'REJECTED' ? 'var(--coral-bg)' : 'var(--panel)'}; border: 1px solid ${status === 'REJECTED' ? 'var(--coral-line)' : 'var(--line)'}; border-radius: 10px; padding: 14px;">
              <div class="card-header" style="display:flex; justify-content:space-between; align-items:center;">
                <div>
                  <span class="field-name" style="font-weight: 700; color: var(--navy); font-size: 12.5px;">${rule.targetField}</span>
                  ${isRequiredInTarget ? '<span class="status-pill pending" style="font-size:8px; margin-left:6px; background:var(--amber-bg); color:var(--amber);">REQUIRED</span>' : ''}
                </div>
                <span class="status-pill ${statusPillClass}" style="${status === 'REJECTED' ? 'background:var(--coral-bg); color:var(--coral-deep); border:1px solid var(--coral-line);' : ''}">${statusPillText}</span>
              </div>
              
              <div class="card-body" style="margin: 10px 0;">
                <div class="form-group" style="margin-bottom: 6px;">
                  <label style="font-size: 9px; font-weight: 700; color: var(--faint); display:block; margin-bottom: 2px;">CONTRIBUTING SOURCE FILE(S):</label>
                  <input type="text" class="input-sourcefile-edit" data-rule-key="${ruleKey}" value="${rule.sourceFile || ''}" placeholder="e.g. file1.csv, file2.csv" style="width: 100%; font-family: 'IBM Plex Mono', monospace; font-size: 10.5px; padding: 5px; border: 1px solid var(--line-2); border-radius: 4px; background: #FCFDFF;" />
                </div>

                <div style="margin: 6px 0; padding: 6px; background: var(--panel-2); border: 1px solid var(--line-2); border-radius: 6px;">
                  <span style="font-size: 8.5px; font-weight: 700; color: var(--faint); display: block; margin-bottom: 4px;">FIELD-LEVEL SOURCE PROVENANCE:</span>
                  <div style="display:flex; gap: 4px; flex-wrap: wrap;">
                    ${columnProvenance.map(cp => `
                      <span class="mono-tag" style="background:#fff; color:var(--navy); border:1px solid var(--line); font-size:9px; padding: 2px 6px;">
                        <code>${cp.column}</code> ➔ <b>${cp.file}</b>
                      </span>
                    `).join('')}
                  </div>
                </div>

                <div style="display:flex; justify-content:space-between; align-items:center; margin-top:6px;">
                  <p style="font-size: 10px; color: var(--faint); margin:0;">Mapping Type: <b style="color:var(--navy);">${mappingType}</b></p>
                  <p style="font-size: 9.5px; color: var(--faint); margin:0; font-family:'IBM Plex Mono',monospace;">Target Type: <b>${targetDataType}</b></p>
                </div>
                
                <div class="expression-edit-box" style="margin-top: 6px;">
                  <label style="font-size: 9px; font-weight: 700; color: var(--faint); display:block; margin-bottom: 2px;">SQL TRANSFORMATION EXPRESSION:</label>
                  <textarea class="input-expression-edit" data-rule-key="${ruleKey}" style="width: 100%; height: 52px; font-family: 'IBM Plex Mono', monospace; font-size: 11px; padding: 6px; border: 1px solid var(--line-2); border-radius: 4px; background: #FCFDFF; resize: vertical;">${safeExpr}</textarea>
                </div>
              </div>

              <div class="card-actions" style="display:grid; grid-template-columns: 1fr 1fr 1fr; gap: 6px; margin-top: 10px;">
                <button class="action-btn-sm btn-save-expr" data-rule-key="${ruleKey}" style="font-size: 10px; padding: 4px 6px;">
                  Save Edits
                </button>
                <button class="action-btn-sm btn-approve-col" data-rule-key="${ruleKey}" style="font-size: 10px; padding: 4px 6px; background: ${status === 'APPROVED' ? 'var(--teal-bg)' : '#fff'}; color: ${status === 'APPROVED' ? 'var(--teal-deep)' : 'var(--navy)'};">
                  ${status === 'APPROVED' ? '✓ Approved' : 'Approve'}
                </button>
                <button class="action-btn-sm btn-reject-col" data-rule-key="${ruleKey}" style="font-size: 10px; padding: 4px 6px; background: ${status === 'REJECTED' ? 'var(--coral-bg)' : '#fff'}; color: ${status === 'REJECTED' ? 'var(--coral-deep)' : 'var(--coral)'}; border-color: var(--coral-line);">
                  ${status === 'REJECTED' ? '✕ Rejected' : 'Reject'}
                </button>
              </div>
            </div>
          `;
        }).join('');

        return `
          <div class="entity-section-card" style="border: 1px solid var(--line); border-radius: 12px; background: var(--panel); padding: 18px; margin-bottom: 20px;">
            <div class="entity-section-header" style="display:flex; justify-content:space-between; align-items:center; margin-bottom: 10px;">
              <div>
                <span class="entity-badge" style="font-size: 11px;">${entityName} Entity</span>
                <span style="font-size: 11.5px; color: var(--muted); margin-left: 8px;">
                  (${entApproved} Approved, <span style="color:var(--coral-deep);">${entRejected} Rejected</span>, ${entPending} Pending)
                </span>
              </div>
              <div style="display:flex; gap: 8px;">
                <button class="action-btn-sm btn-approve-entity-bulk" data-entity="${entityName}" style="background: ${isEntityFullyApproved ? 'var(--teal-bg)' : '#fff'}; color: ${isEntityFullyApproved ? 'var(--teal-deep)' : 'var(--navy)'};">
                  ${isEntityFullyApproved ? '✓ All Approved' : `Approve All ${entityName}`}
                </button>
                <button class="action-btn-sm btn-reject-entity-bulk" data-entity="${entityName}" style="background: ${isEntityFullyRejected ? 'var(--coral-bg)' : '#fff'}; color: ${isEntityFullyRejected ? 'var(--coral-deep)' : 'var(--coral)'}; border-color: var(--coral-line);">
                  ${isEntityFullyRejected ? '✕ All Rejected' : `Reject All ${entityName}`}
                </button>
              </div>
            </div>

            <div style="display:flex; justify-content:space-between; align-items:center; background: var(--panel-2); border: 1px solid var(--line); border-radius: 8px; padding: 8px 12px; margin-bottom: 14px; font-size: 11px; color: var(--muted);">
              <div>
                <b>Multi-Source Composition:</b> Joining <code>${contributingSourceFiles.join('</code> + <code>')}</code>[cite: 1, 8, 10].
              </div>
              <div>
                ${isTargetContractCompliant 
                  ? '<span class="status-pill ok" style="font-size:9px;">✓ Target Contract Compliant (100% Required Fields)</span>' 
                  : `<span class="status-pill error" style="font-size:9px; background:var(--coral-bg); color:var(--coral-deep);">⚠ Missing Required: ${missingRequiredFields.join(', ')}</span>`
                }
              </div>
            </div>

            <div class="entity-rule-grid" style="display:grid; grid-template-columns: repeat(auto-fill, minmax(320px, 1fr)); gap: 12px;">
              ${cardsHtml}
            </div>
          </div>
        `;
      }).join('');

      entitySectionsHtml = `<div class="mapper-entity-sections">${sections}</div>`;
    } else {
      entitySectionsHtml = `
        <div class="placeholder-box">
          <p><b>No Compiled Rules Found</b></p>
          <p class="sub-text">Please go to Stage 03 (Rulebook Editor) and click "Save & Compile Rulebook" first.</p>
        </div>
      `;
    }

    // Step 3: Inline Package Inspection Box
    let packageInspectionHtml = '';
    if (state.isPackageGenerated) {
      packageInspectionHtml = `
        <div class="results-card" style="border: 2px solid ${state.isPackageVerified ? 'var(--teal)' : 'var(--blue)'}; margin-top: 20px;">
          <div style="display:flex; justify-content:space-between; align-items:center; margin-bottom: 10px;">
            <div>
              <span class="mono-tag" style="background:var(--navy); color:#fff;">STEP 3: PACKAGE INSPECTION</span>
              <h3 style="display:inline-block; margin-left:8px;">Generated SQL Transformation Package</h3>
            </div>
            <span class="status-pill ${state.isPackageVerified ? 'ok' : 'pending'}">
              ${state.isPackageVerified ? '✓ Package Verified & Confirmed' : '⚠ Verification Pending'}
            </span>
          </div>

          <p class="section-sub" style="margin-bottom:8px;">
            Inspect the executable ANSI SQL view definitions compiled for all target entities before performing Control Plane hand-off:
          </p>

          <textarea id="sqlPackageTextArea" readonly style="width: 100%; height: 260px; font-family: 'IBM Plex Mono', monospace; font-size: 11px; padding: 12px; border: 1px solid var(--line); border-radius: 8px; background: #1E222D; color: #79C0FF; resize: vertical; line-height: 1.5;"></textarea>

          <div style="display:flex; justify-content:space-between; align-items:center; margin-top: 12px;">
            <p style="font-size: 11px; color: var(--muted); margin:0;">
              Status: <b>LOCAL_PACKAGE_EXPORTS_READY</b>
            </p>
            <button id="verifyPackageContentBtn" class="action-btn-sm" style="padding: 8px 16px; background: ${state.isPackageVerified ? 'var(--teal-bg)' : 'var(--blue-bg)'}; color: ${state.isPackageVerified ? 'var(--teal-deep)' : 'var(--blue-deep)'}; font-weight: 700;">
              ${state.isPackageVerified ? '✓ Package Content Verified' : '▶ Verify & Confirm Package Content'}
            </button>
          </div>
        </div>
      `;
    }

    const handoffBannerHtml = isStageApproved ? `
      <div class="results-card" style="border-left: 4px solid var(--teal); background: var(--teal-bg); margin-top: 16px;">
        <h3 style="color: var(--teal-deep);">✓ Control Plane Hand-Off Complete</h3>
        <p style="margin-top: 4px;">All Control Plane stages (00–04) have been compiled, rules approved, package verified, and signed off[cite: 8, 12, 13].</p>
        <p class="section-sub" style="margin-top: 8px;">
          <b>Next Step:</b> Switch role to <b>Validation Specialist</b> in the header to execute Data Plane stages (05 & 06)[cite: 8, 12, 13].
        </p>
      </div>
    ` : '';

    mainContainer.innerHTML = `
      <div class="stagehead">
        <div class="eyebrow">Control Plane · Stage 04</div>
        <h2>Schema Mapper & Multi-Source Lineage</h2>
        <p>Approve field rules (Step 1), generate SQL package (Step 2), inspect & verify package (Step 3), and hand off Control Plane (Step 4)[cite: 1, 2, 8, 10, 14].</p>
      </div>

      ${entitySectionsHtml}

      <div id="packageInspectionArea">
        ${packageInspectionHtml}
      </div>

      <div id="exportStatusArea">
        ${handoffBannerHtml}
      </div>
    `;

    // Direct DOM binding for Textarea content
    const sqlTextEl = document.getElementById('sqlPackageTextArea');
    if (sqlTextEl && state.generatedSqlContent) {
      sqlTextEl.value = state.generatedSqlContent;
    }

    // Render HITL Side Panel
    hitlContainer.innerHTML = `
      <div class="gatecard ${isStageApproved ? 'approved-card' : ''}">
        <h3>HITL Gate · Mapper Review</h3>
        <p>Authorized: <b>Migration Analyst</b>[cite: 8, 12]</p>
      </div>
      <div class="stat-list">
        <div class="stat"><span>Total Column Rules:</span> <b>${totalRules}</b></div>
        <div class="stat"><span>Approved Columns:</span> <b style="color:var(--teal-deep);">${approvedCount} / ${totalRules}</b></div>
        <div class="stat"><span>Rejected Columns:</span> <b style="color:var(--coral-deep);">${rejectedCount}</b></div>
        <div class="stat"><span>Pending Columns:</span> <b>${pendingCount}</b></div>
        <div class="stat"><span>1. Rules Sign-Off:</span> <b>${areAllRulesApproved ? '100% APPROVED' : 'PENDING'}</b></div>
        <div class="stat"><span>2. Package Status:</span> <b>${state.isPackageGenerated ? 'GENERATED' : 'PENDING'}</b></div>
        <div class="stat"><span>3. Content Verified:</span> <b>${state.isPackageVerified ? 'VERIFIED' : 'PENDING'}</b></div>
        <div class="stat"><span>4. Control Plane Sign-Off:</span> <b>${isStageApproved ? 'APPROVED' : 'PENDING'}</b></div>
      </div>

      ${!areAllRulesApproved ? `
        <button id="approveAllGlobalBtn" class="action-btn" style="background:var(--teal); color:#fff; font-weight:700; margin-bottom:8px; border:none; cursor:pointer;">
          ▶ 1. Approve All (${pendingCount + rejectedCount} Remaining Rules)
        </button>
      ` : `
        <button id="exportAdfBtn" class="action-btn" style="margin-bottom:8px;">
          ${state.isPackageGenerated ? '↻ Re-Generate Package (.sql)' : '▶ 2. Generate Package (.sql)'}
        </button>
      `}

      <button id="approveMapperBtn" class="approve-btn ${isStageApproved ? 'btn-approved' : ''}" ${!canHandoffControlPlane && !isStageApproved ? 'disabled style="opacity:0.5; cursor:not-allowed;"' : ''}>
        ${isStageApproved 
          ? '✓ Control Plane Signed Off' 
          : (!areAllRulesApproved 
              ? '1. Approve All Rules First' 
              : (!state.isPackageGenerated 
                  ? '2. Generate Package First' 
                  : (!state.isPackageVerified 
                      ? '3. Verify Package Content First' 
                      : '4. Approve & Hand Off Control Plane')))
        }
      </button>
    `;

    const invalidatePackageState = async () => {
      state.isPackageGenerated = false;
      state.isPackageVerified = false;
      state.generatedSqlContent = '';
      if (state.stageApprovals['04_mapper']) {
        await revokeStageApproval('04_mapper');
      }
    };

    const approveAllGlobalBtn = document.getElementById('approveAllGlobalBtn');
    if (approveAllGlobalBtn) {
      approveAllGlobalBtn.addEventListener('click', async () => {
        fieldRules.forEach(r => {
          state.columnApprovals[`${r.entity}::${r.targetField}`] = 'APPROVED';
        });
        await invalidatePackageState();
        renderView();
      });
    }

    mainContainer.querySelectorAll('.btn-approve-col').forEach(btn => {
      btn.addEventListener('click', async () => {
        const ruleKey = btn.getAttribute('data-rule-key');
        state.columnApprovals[ruleKey] = 'APPROVED';
        await invalidatePackageState();
        renderView();
      });
    });

    mainContainer.querySelectorAll('.btn-reject-col').forEach(btn => {
      btn.addEventListener('click', async () => {
        const ruleKey = btn.getAttribute('data-rule-key');
        state.columnApprovals[ruleKey] = 'REJECTED';
        await invalidatePackageState();
        renderView();
      });
    });

    mainContainer.querySelectorAll('.btn-approve-entity-bulk').forEach(btn => {
      btn.addEventListener('click', async () => {
        const entityName = btn.getAttribute('data-entity');
        fieldRules.forEach(r => {
          if (normalizeKey(r.entity) === normalizeKey(entityName)) {
            state.columnApprovals[`${r.entity}::${r.targetField}`] = 'APPROVED';
          }
        });

        await invalidatePackageState();
        renderView();
      });
    });

    mainContainer.querySelectorAll('.btn-reject-entity-bulk').forEach(btn => {
      btn.addEventListener('click', async () => {
        const entityName = btn.getAttribute('data-entity');
        fieldRules.forEach(r => {
          if (normalizeKey(r.entity) === normalizeKey(entityName)) {
            state.columnApprovals[`${r.entity}::${r.targetField}`] = 'REJECTED';
          }
        });

        await invalidatePackageState();
        renderView();
      });
    });

    mainContainer.querySelectorAll('.btn-save-expr').forEach(btn => {
      btn.addEventListener('click', async () => {
        const card = btn.closest('.rule-card');
        if (!card) return;

        const exprInput = card.querySelector('.input-expression-edit');
        const sourceInput = card.querySelector('.input-sourcefile-edit');

        const ruleKey = btn.getAttribute('data-rule-key');
        const [entity, targetField] = ruleKey.split('::');
        const targetRule = fieldRules.find(r => normalizeKey(r.entity) === normalizeKey(entity) && r.targetField === targetField);

        if (targetRule) {
          if (exprInput) targetRule.expression = exprInput.value.trim();
          if (sourceInput) targetRule.sourceFile = sourceInput.value.trim();

          state.columnApprovals[ruleKey] = 'PENDING';
          await invalidatePackageState();
          renderView();
        }
      });
    });

    // Step 2: Generate Package Event with Fallback Generator
    const exportBtn = document.getElementById('exportAdfBtn');
    if (exportBtn && areAllRulesApproved) {
      exportBtn.addEventListener('click', async () => {
        let sqlPackage = '';
        try {
          const res = await fetch(`/api/runs/${state.runId}/export-package?mode=local`);
          if (res.ok) {
            const data = await res.json();
            sqlPackage = data.sqlContent || '';
          }
        } catch (err) {
          console.warn('Backend export fetch issue, generating client fallback package', err);
        }

        if (!sqlPackage || sqlPackage.trim().length < 50) {
          sqlPackage = generateClientSqlPackage(fieldRules);
        }

        state.isPackageGenerated = true;
        state.isPackageVerified = false;
        state.generatedSqlContent = sqlPackage;
        renderView();
      });
    }

    // Step 3: Verify Package Content Event
    const verifyBtn = document.getElementById('verifyPackageContentBtn');
    if (verifyBtn) {
      verifyBtn.addEventListener('click', () => {
        state.isPackageVerified = true;
        renderView();
      });
    }

    // Step 4: Final Control Plane Sign-Off Event
    const approveBtn = document.getElementById('approveMapperBtn');
    if (approveBtn && (canHandoffControlPlane || isStageApproved)) {
      approveBtn.addEventListener('click', async () => {
        await processStageApproval('04_mapper');
      });
    }
  }

  renderView();
}