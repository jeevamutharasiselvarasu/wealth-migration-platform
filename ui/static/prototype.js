import { renderLogin } from './components/login.js';
import { renderRoleSelection } from './components/role-selection.js';
import { renderSidebar, applyPersonaPermissions } from './components/workspace-shell.js';
import { renderMigrationSetupStage } from './components/migration-setup.js';
import { renderIngestionStage } from './components/ingestion.js';
import { renderDiscoveryStage } from './components/discovery.js';
import { renderRulebookEditor } from './components/rulebook-editor.js';
import { renderRuleEditor } from './components/rule-editor.js';
import { renderExecutionStage } from './components/execution.js';
import { renderReconciliationStage } from './components/reconciliation.js';

// Read persisted knowledge asset selections from localStorage across page refreshes
const storedKb = localStorage.getItem('selectedKnowledgeAssets');
const initialKnowledgeAssets = storedKb
  ? JSON.parse(storedKb)
  : ['source_profile', 'file_contracts', 'mapping_set', 'markdown_rulebook', 'validation_rules', 'target_contract'];

export const state = {
  currentUser: null,
  currentPersona: 'MA',
  activeStage: '00_setup',
  migrationId: 'morningstar-multicustodian',
  runId: 'run-morningstar-multicustodian',
  rulebookParsed: null,
  selectedKnowledgeAssets: initialKnowledgeAssets,
  stageApprovals: {
    '00_setup': true,
    '01_ingestion': false,
    '02_discovery': false,
    '03_rules': false,
    '04_mapper': false,
    '05_execute': false,
    '06_reconcile': false
  },
  columnApprovals: {},
  entityDiscoveryStatus: {},
  customAuxiliaryFiles: {}
};

const STAGE_ORDER = [
  '00_setup',
  '01_ingestion',
  '02_discovery',
  '03_rules',
  '04_mapper',
  '05_execute',
  '06_reconcile'
];

document.addEventListener('DOMContentLoaded', async () => {
  setupGlobalEvents();
  renderLogin(document.getElementById('modalContainer'), onLoginSuccess);
});

function onLoginSuccess(user) {
  state.currentUser = user;
  
  const sanitizedUser = user.toLowerCase().replace(/[^a-z0-9]/g, '');
  state.runId = `run-${sanitizedUser}-${state.migrationId}`;
  
  renderRoleSelection(document.getElementById('modalContainer'), onPersonaSelected);
}

async function onPersonaSelected(persona) {
  state.currentPersona = persona;
  document.getElementById('modalContainer').innerHTML = '';
  
  state.activeStage = persona === 'MA' ? '00_setup' : '05_execute';
  await syncApprovalsFromBackend();
  initWorkspace();
}

export async function syncApprovalsFromBackend() {
  state.stageApprovals = {
    '00_setup': true,
    '01_ingestion': false,
    '02_discovery': false,
    '03_rules': false,
    '04_mapper': false,
    '05_execute': false,
    '06_reconcile': false
  };

  try {
    const res = await fetch(`/api/runs/${state.runId}/approvals`);
    if (res.ok) {
      const data = await res.json();
      data.approvals.forEach(appr => {
        if (appr.decision === 'APPROVED') {
          state.stageApprovals[appr.stage] = true;
        }
      });
    }
  } catch (err) {
    console.warn('Backend approval sync fallback');
  }
}

export function initWorkspace() {
  renderSidebar(document.getElementById('sidebar'), state);
  renderPersonaSwitcher();
  applyPersonaPermissions(state.currentPersona);
  navigateToStage(state.activeStage);
}

export async function navigateToStage(stageId) {
  state.activeStage = stageId;
  
  await syncApprovalsFromBackend();
  renderSidebar(document.getElementById('sidebar'), state);
  
  const mainEl = document.getElementById('mainWorkspace');
  const hitlEl = document.getElementById('hitlPanel');

  if (stageId === '00_setup') {
    await renderMigrationSetupStage(mainEl, hitlEl);
  } else if (stageId === '01_ingestion') {
    await renderIngestionStage(mainEl, hitlEl);
  } else if (stageId === '02_discovery') {
    await renderDiscoveryStage(mainEl, hitlEl);
  } else if (stageId === '03_rules') {
    await renderRulebookEditor(mainEl, hitlEl, state);
  } else if (stageId === '04_mapper') {
    await renderRuleEditor(mainEl, hitlEl, state);
  } else if (stageId === '05_execute') {
    await renderExecutionStage(mainEl, hitlEl);
  } else if (stageId === '06_reconcile') {
    await renderReconciliationStage(mainEl, hitlEl);
  }
}

export async function processStageApproval(stageId) {
  const res = await fetch(`/api/runs/${state.runId}/approvals`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({
      stage: stageId,
      decision: 'APPROVED',
      reviewer: state.currentUser || 'Migration Analyst'
    })
  });

  if (res.ok) {
    state.stageApprovals[stageId] = true;
    await syncApprovalsFromBackend();

    if (state.currentPersona === 'MA' && stageId === '04_mapper') {
      await navigateToStage('04_mapper');
      return;
    }

    const currentIndex = STAGE_ORDER.indexOf(stageId);
    const nextStageId = (currentIndex >= 0 && currentIndex < STAGE_ORDER.length - 1) 
      ? STAGE_ORDER[currentIndex + 1] 
      : null;

    if (nextStageId) {
      await navigateToStage(nextStageId);
    } else {
      renderSidebar(document.getElementById('sidebar'), state);
    }
  }
}

export async function revokeStageApproval(stageId) {
  try {
    await fetch(`/api/runs/${state.runId}/approvals/${stageId}`, { method: 'DELETE' });
    state.stageApprovals[stageId] = false;
    await syncApprovalsFromBackend();
    renderSidebar(document.getElementById('sidebar'), state);
  } catch (err) {
    console.warn('Failed to revoke approval on edit', err);
  }
}

function renderPersonaSwitcher() {
  const container = document.getElementById('personaSwitcher');
  const isMA = state.currentPersona === 'MA';
  
  container.innerHTML = `
    <div class="user-badge" title="Active Role Scope">
      <span class="avatar">${isMA ? 'MA' : 'VS'}</span>
      <span class="user-name">${state.currentUser || 'User'} (${isMA ? 'Analyst' : 'Specialist'})</span>
      <button id="switchRoleBtn" class="switch-btn">Switch Role</button>
    </div>
  `;

  document.getElementById('switchRoleBtn').addEventListener('click', () => {
    renderRoleSelection(document.getElementById('modalContainer'), onPersonaSelected);
  });
}

function setupGlobalEvents() {
  document.getElementById('navToggleBtn').addEventListener('click', () => {
    document.getElementById('app').classList.toggle('nav-collapsed');
  });

  document.getElementById('resetBtn').addEventListener('click', () => {
    if (confirm('Reset workbench state?')) {
      localStorage.removeItem('selectedKnowledgeAssets');
      location.reload();
    }
  });
}