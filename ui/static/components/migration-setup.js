import { state, navigateToStage } from '../prototype.js';
import { openKnowledgePreviewModal } from './migration-knowledge.js';

export async function renderMigrationSetupStage(mainContainer, hitlContainer) {
  if (!state.selectedKnowledgeAssets || state.selectedKnowledgeAssets.length === 0) {
    state.selectedKnowledgeAssets = ['source_profile', 'file_contracts', 'mapping_set', 'markdown_rulebook', 'validation_rules', 'target_contract'];
  }

  const templates = [
    {
      id: 'morningstar-multicustodian',
      name: 'Morningstar multi-custodian',
      tag: 'Migration template',
      desc: 'Heterogeneous custodian files, historical domains, fees, tax lots, and performance.',
      source: 'Morningstar',
      target: 'AMK'
    },
    {
      id: 'amplify-orion',
      name: 'Amplify -> Orion Eclipse',
      tag: 'Reference scenario',
      desc: 'Rep-as-PM advisory book with current positions, historical performance, and target routing.',
      source: 'Amplify / Schwab / Black Diamond',
      target: 'SPARC + Orion Eclipse'
    },
    {
      id: 'efficient-advisors-bulk-factory',
      name: 'Efficient Advisors bulk factory',
      tag: 'Migration template',
      desc: 'Configuration-driven mappings, account-to-client joins, crosswalks, and bulk-load output.',
      source: 'Efficient Advisors',
      target: 'AMK Bulk Factory'
    }
  ];

  const allKnowledgeAssets = [
    { id: 'source_profile', name: 'SOURCE PROFILE', desc: 'SYSTEMS, DOMAINS, CUSTODIANS, DELIVERY PHASES' },
    { id: 'file_contracts', name: 'FILE CONTRACTS', desc: 'FORMATS, DELIMITERS, KEYS, AND SOURCE DELIVERY RULES' },
    { id: 'mapping_set', name: 'MAPPING SET', desc: 'DIRECT FIELD MAPPINGS AND CANONICAL ENTITIES' },
    { id: 'markdown_rulebook', name: 'MARKDOWN RULEBOOK', desc: 'BUSINESS INTENT, TRANSFORMATIONS, JOINS, AND VALIDATIONS' },
    { id: 'validation_rules', name: 'VALIDATION RULES', desc: 'REQUIRED FIELDS, INTEGRITY, CONTROL TOTALS, AND TOLERANCES' },
    { id: 'target_contract', name: 'TARGET CONTRACT', desc: 'TARGET FIELDS, TYPES, REQUIRED STATUS, AND VERSION' }
  ];

  let selectedTemplate = templates.find(t => t.id === state.migrationId) || templates[0];
  state.migrationId = selectedTemplate.id; // Synchronize global state immediately

  function updateSelectedCount() {
    const checked = state.selectedKnowledgeAssets.length;
    const countEl = document.getElementById('kbCountLabel');
    if (countEl) {
      countEl.textContent = `${checked} of ${allKnowledgeAssets.length} assets selected`;
    }
    localStorage.setItem('selectedKnowledgeAssets', JSON.stringify(state.selectedKnowledgeAssets));
  }

  const knowledgeAssets = allKnowledgeAssets.map(k => ({
    ...k,
    selected: state.selectedKnowledgeAssets.includes(k.id)
  }));

  mainContainer.innerHTML = `
    <div class="stagehead">
      <div class="eyebrow">CONTROL PLANE / STAGE 00</div>
      <h2>Migration Setup</h2>
      <p>Define the migration boundary, choose a starting template, and load the knowledge base before intake[cite: 1, 2, 7].</p>
    </div>

    <div class="setup-hero">
      <div class="eyebrow">CONTROL PLANE / STAGE 00</div>
      <h1>Create the migration before intake.</h1>
      <p>Start from a proven scenario or define a new source-to-target boundary[cite: 1, 2].</p>
    </div>

    <div class="setup-grid">
      <div class="setup-card">
        <div class="card-section-title">AVAILABLE SCENARIO TEMPLATES</div>
        <h3>Choose a starting point</h3>
        <p class="section-sub">Templates are preselected examples. You can change the source, target, and name before creating your migration[cite: 1].</p>
        
        <div class="template-list">
          ${templates.map(t => `
            <label class="template-option ${t.id === selectedTemplate.id ? 'active' : ''}" data-template-id="${t.id}">
              <div class="radio-col">
                <input type="radio" name="scenario_template" value="${t.id}" ${t.id === selectedTemplate.id ? 'checked' : ''} />
              </div>
              <div class="details-col">
                <div class="t-name">${t.name}</div>
                <div class="t-tag">${t.tag}</div>
                <div class="t-desc">${t.desc}</div>
              </div>
            </label>
          `).join('')}
        </div>
      </div>

      <div class="setup-card">
        <div class="card-section-title">MIGRATION DEFINITION</div>
        <h3>Source and target boundary</h3>
        
        <div class="form-group">
          <label>MIGRATION NAME</label>
          <input type="text" id="inputMigrationName" value="${selectedTemplate.name}" />
        </div>

        <div class="form-group">
          <label>SOURCE SYSTEM</label>
          <input type="text" id="inputSourceSystem" value="${selectedTemplate.source}" />
        </div>

        <div class="form-group">
          <label>TARGET SYSTEM</label>
          <input type="text" id="inputTargetSystem" value="${selectedTemplate.target}" />
        </div>

        <div class="next-banner">
          <div class="nb-num">01</div>
          <div class="nb-text">
            <b>What happens next?</b><br />
            Creating the migration snapshots this boundary, then unlocks Loader & Inspector[cite: 1, 2, 7].
          </div>
        </div>
      </div>
    </div>

    <div class="knowledge-setup-card">
      <div class="card-section-title">KNOWLEDGE BASE</div>
      <div class="kb-header">
        <h3>Load system knowledge</h3>
        <button id="selectAllKbBtn" class="action-btn-sm">Select all</button>
      </div>
      <p class="section-sub">Select the assets the control plane should use. All selected assets remain reviewable before execution[cite: 1, 2, 7].</p>

      <div class="kb-grid">
        ${knowledgeAssets.map(k => `
          <div class="kb-box ${k.selected ? 'active' : ''}" data-kb-id="${k.id}">
            <div style="display:flex;align-items:flex-start;gap:8px;margin-bottom:6px;">
              <input type="checkbox" id="kb_chk_${k.id}" ${k.selected ? 'checked' : ''} style="margin-top:2px;cursor:pointer;" />
              <div class="kb-name">${k.name}</div>
            </div>
            <div class="kb-desc">${k.desc}</div>
            <button class="kb-preview-btn" data-kb-id="${k.id}">Preview / edit</button>
          </div>
        `).join('')}
      </div>

      <div class="kb-footer">
        <span id="kbCountLabel" class="kb-count">${state.selectedKnowledgeAssets.length} of ${allKnowledgeAssets.length} assets selected</span>
        <button id="createMigrationContinueBtn" class="btn-primary-action">Create migration and continue -></button>
      </div>
    </div>
  `;

  hitlContainer.innerHTML = `
    <div class="gatecard">
      <h3>HITL GATE</h3>
      <p>Escalated: Choose a template, define the systems, and load the knowledge base before intake[cite: 1, 2, 7].</p>
    </div>
    <div class="stat-list">
      <div class="stat"><span>Run status:</span> <b>NOT CREATED</b></div>
    </div>
  `;

  // Bind Template Radio Options
  mainContainer.querySelectorAll('.template-option').forEach(el => {
    el.addEventListener('click', () => {
      const templateId = el.getAttribute('data-template-id');
      selectedTemplate = templates.find(t => t.id === templateId);
      state.migrationId = templateId; // Update state dynamically on template selection

      mainContainer.querySelectorAll('.template-option').forEach(opt => {
        const isMatch = opt.getAttribute('data-template-id') === templateId;
        opt.classList.toggle('active', isMatch);
        opt.querySelector('input[type="radio"]').checked = isMatch;
      });

      document.getElementById('inputMigrationName').value = selectedTemplate.name;
      document.getElementById('inputSourceSystem').value = selectedTemplate.source;
      document.getElementById('inputTargetSystem').value = selectedTemplate.target;
    });
  });

  // Bind Knowledge Asset Toggles
  mainContainer.querySelectorAll('.kb-box').forEach(box => {
    const kbId = box.getAttribute('data-kb-id');
    const chk = box.querySelector('input[type="checkbox"]');

    chk.addEventListener('change', () => {
      box.classList.toggle('active', chk.checked);

      if (chk.checked) {
        if (!state.selectedKnowledgeAssets.includes(kbId)) {
          state.selectedKnowledgeAssets.push(kbId);
        }
      } else {
        state.selectedKnowledgeAssets = state.selectedKnowledgeAssets.filter(id => id !== kbId);
      }

      updateSelectedCount();
    });
  });

  // Bind Select All
  document.getElementById('selectAllKbBtn').addEventListener('click', () => {
    state.selectedKnowledgeAssets = allKnowledgeAssets.map(k => k.id);

    mainContainer.querySelectorAll('.kb-box').forEach(box => {
      const chk = box.querySelector('input[type="checkbox"]');
      chk.checked = true;
      box.classList.add('active');
    });

    updateSelectedCount();
  });

  // Bind Modal Preview with dynamic template ID
  mainContainer.querySelectorAll('.kb-preview-btn').forEach(btn => {
    btn.addEventListener('click', (e) => {
      e.stopPropagation();
      openKnowledgePreviewModal(btn.getAttribute('data-kb-id'), state.migrationId);
    });
  });

  // Create Boundary & Advance
  document.getElementById('createMigrationContinueBtn').addEventListener('click', async () => {
    const name = document.getElementById('inputMigrationName').value.trim();
    const sourceSystem = document.getElementById('inputSourceSystem').value.trim();
    const targetSystem = document.getElementById('inputTargetSystem').value.trim();

    state.migrationId = selectedTemplate.id;
    const sanitizedUser = (state.currentUser || 'user').toLowerCase().replace(/[^a-z0-9]/g, '');
    state.runId = `run-${sanitizedUser}-${state.migrationId}`;

    const res = await fetch('/api/migrations', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        name,
        sourceSystem,
        targetSystem,
        templateId: selectedTemplate.id,
        entities: ['Clients Households', 'Accounts', 'Positions', 'Fees', 'Billing', 'Transactions', 'Performance'],
        createdBy: state.currentUser || 'Migration Analyst'
      })
    });

    if (res.ok || res.status === 400) {
      state.stageApprovals['00_setup'] = true;
      navigateToStage('01_ingestion');
    }
  });

  updateSelectedCount();
}