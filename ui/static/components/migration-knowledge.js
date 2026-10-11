import { state } from '../prototype.js';

export async function openKnowledgePreviewModal(assetId, migrationId = state.migrationId) {
  const modalContainer = document.getElementById('modalContainer');

  const assetMeta = {
    source_profile: { title: 'Source System Profile (source-profile.json)', type: 'json' },
    file_contracts: { title: 'Source File Contracts (file-contract.json)', type: 'json' },
    mapping_set: { title: 'Field Mapping Set (mapping-set.json)', type: 'json' },
    markdown_rulebook: { title: 'Executable Rulebook (migration-rulebook.md)', type: 'markdown' },
    validation_rules: { title: 'Validation Rules (validation-rules.json)', type: 'json' },
    target_contract: { title: 'AMK Target Contract (target-contract.json)', type: 'json' },
    reconciliation_spec: { title: 'Reconciliation Spec (reconciliation-spec.json)', type: 'json' }
  };

  const meta = assetMeta[assetId] || { title: `${assetId}`, type: 'json' };

  let initialText = '';
  try {
    const res = await fetch(`/api/migrations/${migrationId}/knowledge/${assetId}`);
    if (res.ok) {
      const data = await res.json();
      initialText = meta.type === 'json'
        ? JSON.stringify(data.content, null, 2)
        : data.content;
    }
  } catch (err) {
    console.warn('Failed to load asset from backend', err);
  }

  modalContainer.innerHTML = `
    <div class="kb-modal-overlay">
      <div class="kb-modal-card">
        <div class="kb-modal-header">
          <div class="kb-modal-title">
            <span class="mono-tag">${meta.type.toUpperCase()}</span>
            <h3>${meta.title}</h3>
          </div>
          <button id="closeKbModalBtn" class="close-modal-btn">✕</button>
        </div>

        <div class="kb-modal-body">
          <p class="kb-modal-sub">
            Review and edit the knowledge asset before loading it into the run workspace.
          </p>
          <textarea id="kbTextareaEditor" class="kb-code-editor" spellcheck="false">${initialText}</textarea>
        </div>

        <div class="kb-modal-footer">
          <button id="cancelKbModalBtn" class="btn-secondary">Cancel</button>
          <button id="saveKbAssetBtn" class="btn-primary-action">Save Knowledge Edits</button>
        </div>
      </div>
    </div>
  `;

  document.getElementById('closeKbModalBtn').addEventListener('click', closeKbModal);
  document.getElementById('cancelKbModalBtn').addEventListener('click', closeKbModal);

  document.getElementById('saveKbAssetBtn').addEventListener('click', async () => {
    const updatedContent = document.getElementById('kbTextareaEditor').value;
    let payloadContent = updatedContent;

    if (meta.type === 'json') {
      try {
        payloadContent = JSON.parse(updatedContent);
      } catch (err) {
        alert('Invalid JSON syntax. Please correct the syntax before saving.');
        return;
      }
    }

    await fetch(`/api/migrations/${migrationId}/knowledge/${assetId}`, {
      method: 'PUT',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ content: payloadContent })
    });

    closeKbModal();
  });
}

function closeKbModal() {
  document.getElementById('modalContainer').innerHTML = '';
}