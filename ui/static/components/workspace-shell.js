import { navigateToStage } from '../prototype.js';

export function renderSidebar(container, state) {
  const isMA = state.currentPersona === 'MA';

  const getBadge = (stageId) => {
    if (state.stageApprovals[stageId]) {
      return '<span class="status-badge approved">approved</span>';
    }
    return '<span class="status-badge pending">pending</span>';
  };

  container.innerHTML = `
    <div class="nav-group">
      <h4>Control Plane (Migration Analyst)</h4>
      <div class="stage ${state.activeStage==='00_setup'?'active':''}" data-stage="00_setup">
        <span class="ix">00</span><span class="label">Migration Setup</span>
        <span class="status-badge approved">created</span>
      </div>
      <div class="stage ${state.activeStage==='01_ingestion'?'active':''}" data-stage="01_ingestion">
        <span class="ix">01</span><span class="label">Loader & Inspector</span>
        ${getBadge('01_ingestion')}
      </div>
      <div class="stage ${state.activeStage==='02_discovery'?'active':''}" data-stage="02_discovery">
        <span class="ix">02</span><span class="label">Discovery Agent</span>
        ${getBadge('02_discovery')}
      </div>
      <div class="stage ${state.activeStage==='03_rules'?'active':''}" data-stage="03_rules">
        <span class="ix">03</span><span class="label">Rulebook Editor</span>
        ${getBadge('03_rules')}
      </div>
      <div class="stage ${state.activeStage==='04_mapper'?'active':''}" data-stage="04_mapper">
        <span class="ix">04</span><span class="label">Schema Mapper</span>
        ${getBadge('04_mapper')}
      </div>
    </div>

    <div class="nav-group">
      <h4>Data Plane (Validation Specialist)</h4>
      <div class="stage ${state.activeStage==='05_execute'?'active':''}" data-stage="05_execute">
        <span class="ix">05</span><span class="label">ADF Execution</span>
        ${getBadge('05_execute')}
      </div>
      <div class="stage ${state.activeStage==='06_reconcile'?'active':''}" data-stage="06_reconcile">
        <span class="ix">06</span><span class="label">Reconciliation</span>
        ${getBadge('06_reconcile')}
      </div>
    </div>
  `;

  container.querySelectorAll('.stage').forEach(el => {
    el.addEventListener('click', () => {
      navigateToStage(el.getAttribute('data-stage'));
    });
  });
}

export function applyPersonaPermissions(persona) {
  const isMA = persona === 'MA';
  document.querySelectorAll('.nav-group').forEach((group, idx) => {
    if ((idx === 0 && !isMA) || (idx === 1 && isMA)) {
      group.classList.add('disabled-group');
    } else {
      group.classList.remove('disabled-group');
    }
  });
}