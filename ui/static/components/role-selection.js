export function renderRoleSelection(container, onSelect) {
  container.innerHTML = `
    <div class="role-modal">
      <div class="role-card-container">
        <h2>Select Workspace Persona</h2>
        <p>Your persona determines stage access and approval permissions.</p>
        
        <div class="role-cards">
          <!-- Migration Analyst (Control Plane) -->
          <div class="role-card ma" id="cardMA">
            <div class="avatar">MA</div>
            <h3>Migration Analyst</h3>
            <p>Control Plane Authoring: Markdown rulebooks, entity mappings, single-rule reruns[cite: 8, 10, 12].</p>
            <span class="tag">Stages 01–04</span>
          </div>

          <!-- Validation Specialist (Data Plane) -->
          <div class="role-card vs" id="cardVS">
            <div class="avatar">VS</div>
            <h3>Validation Specialist</h3>
            <p>Data Plane Execution: ADF pipeline runs, quarantine review, financial reconciliation[cite: 8, 10, 12].</p>
            <span class="tag">Stages 05–06</span>
          </div>
        </div>
      </div>
    </div>
  `;

  document.getElementById('cardMA').addEventListener('click', () => onSelect('MA'));
  document.getElementById('cardVS').addEventListener('click', () => onSelect('VS'));
}