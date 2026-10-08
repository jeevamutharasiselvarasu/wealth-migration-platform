// --- Interactive Role Switcher ---
async function switchRole(newRole) {
    const formData = new FormData();
    formData.append('role', newRole);

    try {
        const response = await fetch('/api/auth/role', {
            method: 'POST',
            body: formData
        });
        const result = await response.json();
        if (result.status === 'SUCCESS') {
            console.log('Role updated to:', result.active_role);
            window.location.reload();
        }
    } catch (error) {
        console.error('Failed to update role:', error);
    }
}

// --- Interactive Entity Scope Dropdown ---
async function changeEntityScope(entityName, newScope) {
    const formData = new FormData();
    formData.append('entity_name', entityName);
    formData.append('new_scope', newScope);

    try {
        const response = await fetch('/api/entities/update-scope', {
            method: 'POST',
            body: formData
        });
        const result = await response.json();
        if (result.status === 'SUCCESS') {
            showToast(`Scope for '${entityName}' updated to ${newScope}`);
        }
    } catch (error) {
        console.error('Failed to update entity scope:', error);
    }
}

// --- Interactive File Ingestion Upload ---
async function handleFileUpload(event) {
    event.preventDefault();
    const form = event.target;
    const formData = new FormData(form);
    const alertBox = document.getElementById('upload-status-alert');

    try {
        const response = await fetch('/api/knowledge/upload', {
            method: 'POST',
            body: formData
        });
        const result = await response.json();

        if (result.status === 'SUCCESS') {
            alertBox.className = "p-4 rounded-xl text-xs font-semibold bg-emerald-500/10 border border-emerald-500/30 text-emerald-400";
            alertBox.textContent = `Successfully uploaded and parsed '${result.file_record.filename}'!`;
            alertBox.classList.remove('hidden');
            setTimeout(() => { window.location.reload(); }, 1200);
        }
    } catch (error) {
        console.error('File upload failed:', error);
    }
}

// --- Interactive Agent Pipeline Execution ---
async function runAgentPipeline(repCode, waveId) {
    const statusBox = document.getElementById('pipeline-execution-status');
    const formData = new FormData();
    formData.append('rep_code', repCode);
    formData.append('wave_id', waveId);

    if (statusBox) {
        statusBox.classList.remove('hidden');
        statusBox.textContent = `Executing 9-Agent Pipeline for Advisor ${repCode}...`;
    }

    try {
        const response = await fetch('/api/pipeline/run', {
            method: 'POST',
            body: formData
        });
        const result = await response.json();

        if (result.status === 'SUCCESS') {
            if (statusBox) {
                statusBox.className = "p-4 rounded-xl text-xs font-semibold bg-emerald-500/10 border border-emerald-500/30 text-emerald-400";
                statusBox.textContent = `Pipeline Execution Complete! Invariants: PASS | Drift: ${result.result.reconciliation_summary.variance_pct}%`;
            }
            setTimeout(() => { window.location.href = '/hitl'; }, 1500);
        }
    } catch (error) {
        console.error('Pipeline execution error:', error);
    }
}

// --- Interactive HITL Approval / Rejection ---
async function submitHitl(action) {
    const form = document.getElementById('hitl-form');
    const alertBox = document.getElementById('hitl-alert');
    const formData = new FormData(form);

    const endpoint = action === 'approve' ? '/api/hitl/approve' : '/api/hitl/reject';

    if (action === 'reject') {
        const reason = prompt("Enter rejection reason for analyst review:", "Sleeve target weight variance require manual override.");
        if (!reason) return;
        formData.append('reason', reason);
    }

    try {
        const response = await fetch(endpoint, {
            method: 'POST',
            body: formData
        });

        const result = await response.json();
        alertBox.classList.remove('hidden');

        if (result.status === 'SUCCESS') {
            alertBox.className = "p-4 rounded-xl text-sm font-medium bg-[#064e3b]/40 border border-[#059669]/40 text-[#34d399] transition-all";
            alertBox.textContent = result.message;
        } else {
            alertBox.className = "p-4 rounded-xl text-sm font-medium bg-amber-500/10 border border-amber-500/20 text-amber-400 transition-all";
            alertBox.textContent = result.message;
        }
    } catch (error) {
        console.error('Submission error:', error);
    }
}

// Notification Helper
function showToast(message) {
    const toast = document.createElement('div');
    toast.className = "fixed bottom-5 right-5 bg-indigo-600 text-white text-xs px-4 py-2.5 rounded-lg shadow-xl z-50 transition-all font-medium";
    toast.textContent = message;
    document.body.appendChild(toast);
    setTimeout(() => { toast.remove(); }, 3000);
}