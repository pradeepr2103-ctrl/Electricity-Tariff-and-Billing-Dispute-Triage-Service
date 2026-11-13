/**
 * Tariff Dispute Triage Service — Web Client
 * Handles dispute submission, polling, and result display.
 */

const API_BASE = 'http://localhost:8000';
const POLL_INTERVAL_MS = 3000;
const MAX_POLL_ATTEMPTS = 60; // 3 min total

let runs = {}; // run_id -> { run_id, status, created_at, poll_count }
let pollTimers = {};

// ---------------------------------------------------------------------------
// API Health Check
// ---------------------------------------------------------------------------
async function checkHealth() {
    const dot = document.getElementById('status-dot');
    const text = document.getElementById('status-text');
    try {
        const resp = await fetch(`${API_BASE}/health`, { signal: AbortSignal.timeout(5000) });
        const data = await resp.json();
        const isHealthy = data.status === 'healthy' || data.services?.redis === 'ok';
        dot.className = `status-dot ${isHealthy ? 'healthy' : 'unhealthy'}`;
        text.textContent = isHealthy ? 'API healthy' : `API ${data.status}`;
    } catch {
        dot.className = 'status-dot unhealthy';
        text.textContent = 'API unreachable';
    }
}

// ---------------------------------------------------------------------------
// Form submission
// ---------------------------------------------------------------------------
document.getElementById('dispute-form').addEventListener('submit', async (e) => {
    e.preventDefault();
    await submitDispute();
});

async function submitDispute() {
    const btn = document.getElementById('submit-btn');
    const payload = buildPayload();

    if (!validatePayload(payload)) return;

    btn.disabled = true;
    btn.innerHTML = '<div class="spinner"></div> Submitting...';

    try {
        const resp = await fetch(`${API_BASE}/disputes/submit`, {
            method: 'POST',
            headers: {
                'Content-Type': 'application/json',
                'X-Correlation-ID': crypto.randomUUID(),
            },
            body: JSON.stringify(payload),
        });

        const data = await resp.json();

        if (!resp.ok) {
            const msg = data.detail?.message || data.detail || 'Submission failed';
            const errors = data.detail?.validation_errors;
            showToast(errors ? `Validation error: ${errors[0]}` : msg, 'error');
            return;
        }

        const run_id = data.run_id;
        runs[run_id] = {
            run_id,
            dispute_id: data.dispute_id,
            status: 'queued',
            created_at: new Date().toISOString(),
            consumer_name: payload.consumer_name,
            dispute_reason: payload.dispute_reason,
            poll_count: 0,
        };

        renderRuns();
        startPolling(run_id);
        showToast(`Dispute submitted! Run ID: ${run_id.slice(0, 8)}...`, 'success');

    } catch (err) {
        showToast(`Network error: ${err.message}`, 'error');
    } finally {
        btn.disabled = false;
        btn.innerHTML = '⚡ Submit Dispute for Triage';
    }
}

function buildPayload() {
    const val = (id) => document.getElementById(id).value.trim();
    const numVal = (id) => parseFloat(document.getElementById(id).value);

    return {
        consumer_id: val('consumer_id'),
        consumer_name: val('consumer_name'),
        premises_type: val('premises_type'),
        connected_load_kw: numVal('connected_load_kw'),
        billing_month: val('billing_month'),
        disputed_amount: numVal('disputed_amount'),
        tariff_category_applied: val('tariff_category_applied'),
        dispute_reason: val('dispute_reason'),
        description: val('description'),
        additional_premises_use: val('additional_premises_use') || null,
    };
}

function validatePayload(p) {
    if (!p.consumer_id) { showToast('Consumer ID is required', 'error'); return false; }
    if (!p.consumer_name) { showToast('Consumer Name is required', 'error'); return false; }
    if (!p.premises_type) { showToast('Please select Premises Type', 'error'); return false; }
    if (!p.connected_load_kw || p.connected_load_kw <= 0) { showToast('Connected load must be > 0', 'error'); return false; }
    if (!p.billing_month) { showToast('Billing month is required', 'error'); return false; }
    if (!p.disputed_amount || p.disputed_amount <= 0) { showToast('Disputed amount must be > 0', 'error'); return false; }
    if (!p.tariff_category_applied) { showToast('Please select Tariff Category', 'error'); return false; }
    if (!p.dispute_reason) { showToast('Please select Dispute Reason', 'error'); return false; }
    if (!p.description || p.description.length < 10) { showToast('Description must be at least 10 characters', 'error'); return false; }
    return true;
}

// ---------------------------------------------------------------------------
// Polling
// ---------------------------------------------------------------------------
function startPolling(run_id) {
    if (pollTimers[run_id]) clearInterval(pollTimers[run_id]);

    pollTimers[run_id] = setInterval(async () => {
        const run = runs[run_id];
        if (!run) { clearInterval(pollTimers[run_id]); return; }

        run.poll_count++;
        if (run.poll_count > MAX_POLL_ATTEMPTS) {
            run.status = 'timeout';
            renderRuns();
            clearInterval(pollTimers[run_id]);
            return;
        }

        try {
            const resp = await fetch(`${API_BASE}/disputes/${run_id}/status`);
            const data = await resp.json();

            run.status = data.status;
            if (data.result) {
                run.result = data.result;
            }
            if (data.error) {
                run.error = data.error;
            }

            renderRuns();

            if (['completed', 'failed', 'escalated', 'timeout'].includes(data.status)) {
                clearInterval(pollTimers[run_id]);
                if (data.result) {
                    renderResult(run_id, data.result);
                }
            }
        } catch (err) {
            console.error('Poll error:', err);
        }
    }, POLL_INTERVAL_MS);
}

// ---------------------------------------------------------------------------
// Render active runs
// ---------------------------------------------------------------------------
function renderRuns() {
    const panel = document.getElementById('runs-panel');
    const runList = Object.values(runs).reverse();

    if (runList.length === 0) {
        panel.innerHTML = '<div class="status-empty"><div class="empty-icon">📭</div><p>No active runs.</p></div>';
        return;
    }

    panel.innerHTML = runList.map(run => {
        const statusBadge = getStatusBadge(run.status);
        const shortId = run.run_id.slice(0, 8);
        const time = new Date(run.created_at).toLocaleTimeString();
        const isPolling = ['queued', 'processing'].includes(run.status);

        return `
            <div class="run-card">
                <div class="run-header" onclick="toggleResult('${run.run_id}')">
                    <div>
                        <div style="display:flex;align-items:center;gap:8px;margin-bottom:4px;">
                            ${statusBadge}
                            ${isPolling ? '<div class="spinner" style="border-color:rgba(0,0,0,0.15);border-top-color:#1a56db;width:12px;height:12px;"></div>' : ''}
                        </div>
                        <div class="run-id">${shortId}... · ${run.consumer_name} · ${run.dispute_reason}</div>
                        <div style="font-size:11px;color:var(--text-muted);margin-top:2px;">${time}</div>
                    </div>
                    <div style="font-size:18px;color:var(--text-muted);">›</div>
                </div>
                <div id="result-${run.run_id}" class="run-body">
                    ${run.result ? renderMiniResult(run.result) : '<p style="color:var(--text-muted);font-size:13px;">Processing...</p>'}
                </div>
            </div>
        `;
    }).join('');
}

function toggleResult(run_id) {
    const body = document.getElementById(`result-${run_id}`);
    if (body) body.classList.toggle('open');
}

function renderMiniResult(result) {
    return `
        <div class="${routingClass(result.routing_decision)} routing-badge">${routingLabel(result.routing_decision)}</div>
        <div style="font-size:13px;color:var(--text-muted);margin-bottom:8px;">${result.tariff_position || ''}</div>
        <div style="font-size:12px;color:var(--text-muted);">
            ${result.citations?.length || 0} citations · ${result.latency_ms || 0}ms · \$${(result.cost_usd || 0).toFixed(6)}
        </div>
    `;
}

// ---------------------------------------------------------------------------
// Render full result
// ---------------------------------------------------------------------------
function renderResult(run_id, result) {
    const container = document.getElementById('results-container');
    const citations = result.citations || [];
    const usage = result.token_usage || {};

    const existingEmpty = container.querySelector('.status-empty');
    if (existingEmpty) existingEmpty.remove();

    const el = document.createElement('div');
    el.className = 'run-card';
    el.style.marginBottom = '20px';
    el.innerHTML = `
        <div class="run-header">
            <div>
                <div style="font-weight:600;margin-bottom:4px;">${result.dispute_id || run_id}</div>
                <div class="run-id">${run_id}</div>
            </div>
            <span class="badge ${verificationBadge(result.verification_status)}">${result.verification_status || 'unknown'}</span>
        </div>
        <div class="run-body open">
            <div class="${routingClass(result.routing_decision)} routing-badge">
                ${routingIcon(result.routing_decision)} ${routingLabel(result.routing_decision)}
            </div>

            <div class="section-title">Tariff Position</div>
            <div style="font-size:14px;line-height:1.6;margin-bottom:8px;">${escapeHtml(result.tariff_position || 'N/A')}</div>

            ${citations.length > 0 ? `
            <div class="section-title">Citations (${citations.length})</div>
            ${citations.map(c => `
                <div class="citation">
                    <div class="clause">📄 ${escapeHtml(c.clause_id || 'Clause')} — ${escapeHtml(c.document || '')}</div>
                    <div class="excerpt">"${escapeHtml((c.excerpt || '').slice(0, 200))}${c.excerpt?.length > 200 ? '...' : ''}"</div>
                    <div class="score">Relevance: ${((c.relevance_score || 0) * 100).toFixed(0)}%</div>
                </div>
            `).join('')}` : ''}

            <div class="section-title">Agent Reasoning</div>
            <div class="reasoning-box">${escapeHtml(result.reasoning || 'No reasoning provided')}</div>

            <div class="metrics-grid">
                <div class="metric-item">
                    <div class="metric-value">${result.latency_ms || 0}ms</div>
                    <div class="metric-label">Latency</div>
                </div>
                <div class="metric-item">
                    <div class="metric-value">${usage.total_tokens || 0}</div>
                    <div class="metric-label">Tokens</div>
                </div>
                <div class="metric-item">
                    <div class="metric-value">\$${(result.cost_usd || 0).toFixed(6)}</div>
                    <div class="metric-label">Cost</div>
                </div>
            </div>

            <div style="margin-top:16px;display:flex;gap:8px;align-items:center;">
                <span style="font-size:12px;color:var(--text-muted);">Was this result helpful?</span>
                <button class="btn btn-secondary" style="font-size:12px;padding:5px 12px;" onclick="submitFeedback('${run_id}', true)">👍 Yes</button>
                <button class="btn btn-secondary" style="font-size:12px;padding:5px 12px;" onclick="submitFeedback('${run_id}', false)">👎 No</button>
            </div>
        </div>
    `;

    container.prepend(el);
}

async function submitFeedback(run_id, satisfactory) {
    try {
        await fetch(`${API_BASE}/feedback/${run_id}`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ run_id, satisfactory, reviewer_id: 'web-client' }),
        });
        showToast(satisfactory ? 'Thanks for your positive feedback!' : 'Feedback recorded — we\'ll improve!', 'info');
    } catch {
        showToast('Could not submit feedback', 'error');
    }
}

// ---------------------------------------------------------------------------
// Quick fill presets
// ---------------------------------------------------------------------------
window.quickFill = function (preset) {
    const presets = {
        domestic: {
            consumer_id: 'BES-2024-100001', consumer_name: 'Ravi Kumar',
            premises_type: 'domestic', connected_load_kw: '3.5', billing_month: '2024-03',
            disputed_amount: '850', tariff_category_applied: 'LT-1',
            dispute_reason: 'tariff_category',
            description: 'I am being billed under LT-1 domestic category but I feel my bill amount is too high for 120 units consumption.',
            additional_premises_use: '',
        },
        tailoring: {
            consumer_id: 'BES-2024-100003', consumer_name: 'Meena Devi',
            premises_type: 'domestic', connected_load_kw: '4.0', billing_month: '2024-03',
            disputed_amount: '2200', tariff_category_applied: 'LT-2',
            dispute_reason: 'tariff_category',
            description: 'I am a homemaker who does tailoring work from home using 2 sewing machines. I employ one part-time helper. BESCOM has categorized me under LT-2 commercial rates but I believe I should be under LT-1 domestic as my tailoring is home-based.',
            additional_premises_use: 'home tailoring shop with 2 sewing machines, 1 part-time helper',
        },
        meter: {
            consumer_id: 'HES-2024-200005', consumer_name: 'Precision Parts Manufacturing',
            premises_type: 'industrial', connected_load_kw: '45.0', billing_month: '2024-01',
            disputed_amount: '85000', tariff_category_applied: 'LT-3',
            dispute_reason: 'meter_accuracy',
            description: 'Our electricity bill has spiked by 40% this month compared to same period last year. We suspect our three-phase meter is recording incorrectly. Production levels have remained constant throughout.',
            additional_premises_use: '',
        },
        commercial: {
            consumer_id: 'MES-2024-300011', consumer_name: 'Sunrise Clinic',
            premises_type: 'commercial', connected_load_kw: '8.0', billing_month: '2024-02',
            disputed_amount: '3200', tariff_category_applied: 'LT-1',
            dispute_reason: 'tariff_category',
            description: 'We are a private medical clinic and have been billed under LT-1 domestic category since our connection was set up 3 years ago. We want to understand if we should be under LT-2 commercial.',
            additional_premises_use: '',
        },
    };

    const p = presets[preset];
    if (!p) return;

    Object.entries(p).forEach(([key, val]) => {
        const el = document.getElementById(key);
        if (el) el.value = val;
    });
};

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------
function getStatusBadge(status) {
    const map = {
        queued: ['badge-gray', '⏳ Queued'],
        processing: ['badge-info', '🔄 Processing'],
        completed: ['badge-success', '✅ Completed'],
        failed: ['badge-danger', '❌ Failed'],
        escalated: ['badge-warning', '🔔 Escalated'],
        timeout: ['badge-danger', '⏱ Timeout'],
    };
    const [cls, label] = map[status] || ['badge-gray', status];
    return `<span class="badge ${cls}">${label}</span>`;
}

function routingClass(decision) {
    const map = {
        'bill-stands-with-reason': 'routing-bill-stands',
        'revision-recommended': 'routing-revision',
        'site-inspection-required': 'routing-inspection',
        'escalated-to-human': 'routing-escalated',
    };
    return map[decision] || 'routing-bill-stands';
}

function routingLabel(decision) {
    const map = {
        'bill-stands-with-reason': 'Bill Stands',
        'revision-recommended': 'Revision Recommended',
        'site-inspection-required': 'Site Inspection Required',
        'escalated-to-human': 'Escalated to Human',
    };
    return map[decision] || decision;
}

function routingIcon(decision) {
    return { 'bill-stands-with-reason': '✅', 'revision-recommended': '📝', 'site-inspection-required': '🔍', 'escalated-to-human': '👤' }[decision] || '❓';
}

function verificationBadge(status) {
    return status === 'verified' ? 'badge-success' : 'badge-warning';
}

function escapeHtml(text) {
    return String(text).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;');
}

function showToast(message, type = 'info') {
    const existing = document.querySelector('.toast');
    if (existing) existing.remove();

    const toast = document.createElement('div');
    toast.className = `toast toast-${type}`;
    toast.textContent = message;
    document.body.appendChild(toast);
    setTimeout(() => toast.remove(), 4000);
}

window.clearRuns = function () {
    Object.values(pollTimers).forEach(t => clearInterval(t));
    pollTimers = {};
    runs = {};
    renderRuns();
    document.getElementById('results-container').innerHTML = '<div class="status-empty"><div class="empty-icon">🔍</div><p>Results will appear here after agent processing completes.</p></div>';
};

// ---------------------------------------------------------------------------
// Init
// ---------------------------------------------------------------------------
checkHealth();
setInterval(checkHealth, 30000);

// Set default billing month to current month
const now = new Date();
const monthStr = `${now.getFullYear()}-${String(now.getMonth() + 1).padStart(2, '0')}`;
document.getElementById('billing_month').value = monthStr;
