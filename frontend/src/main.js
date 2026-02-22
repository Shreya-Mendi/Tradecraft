/**
 * Tradecraft — Main App Orchestrator
 *
 * Handles:
 *  - Agent thread UI (building, state transitions, payload display)
 *  - Pipeline dispatch (mock or real GitHub Actions)
 *  - Audit log (localStorage persistence + live append)
 *  - Token modal for GitHub PAT entry
 *  - Stats counters (runs, vetoes, audit entries)
 */

import { runMockPipeline } from './agents/pipeline.js';
import {
  dispatchPipeline,
  pollForResult,
  saveToken, loadToken, clearToken, hasToken,
} from './services/api.js';

// ── State ─────────────────────────────────────────────────────────────────────

let runCount   = 0;
let vetoCount  = 0;
let auditCount = 0;

const AUDIT_KEY = 'tradecraft_audit_log';
const STATS_KEY = 'tradecraft_stats';

const AGENT_CONFIG = {
  researcher:      { label: 'Researcher',   abbr: 'R' },
  signal_agent:    { label: 'Signal Agent', abbr: 'S' },
  risk_manager:    { label: 'Risk Manager', abbr: '⚡' },
  execution_agent: { label: 'Execution',    abbr: 'E' },
  supervisor:      { label: 'Supervisor',   abbr: '✓' },
};

const AGENT_ORDER = ['researcher', 'signal_agent', 'risk_manager', 'execution_agent', 'supervisor'];

const SAMPLE_EVENTS = [
  { headline: 'AAPL warns of 6-8 week supply chain delays due to Taiwan fab disruption.', ticker: 'AAPL', source: 'Reuters' },
  { headline: 'Fed minutes signal two additional rate hikes; inflation stickier than expected.',  ticker: 'SPY',  source: 'Federal Reserve' },
  { headline: 'NVDA beats earnings by 18%; data center revenue up 3x YoY.', ticker: 'NVDA', source: 'NASDAQ Filing' },
  { headline: 'MSFT Azure cloud revenue beats estimates; AI workloads drive 28% YoY growth.',    ticker: 'MSFT', source: 'MSFT Earnings' },
  { headline: 'TSLA deliveries miss Q1 estimates by 14%; demand concerns resurface.',            ticker: 'TSLA', source: 'Reuters' },
];

// ── Bootstrap ─────────────────────────────────────────────────────────────────

window.addEventListener('DOMContentLoaded', () => {
  loadPersistedStats();
  buildAgentThread();
  buildEventPills();
  restoreAuditLog();
  renderModeIndicator();
  updateStats();
});

// ── Persist / Restore ─────────────────────────────────────────────────────────

function loadPersistedStats() {
  const s = JSON.parse(localStorage.getItem(STATS_KEY) ?? '{}');
  runCount   = s.runCount   ?? 0;
  vetoCount  = s.vetoCount  ?? 0;
  auditCount = s.auditCount ?? 0;
}

function persistStats() {
  localStorage.setItem(STATS_KEY, JSON.stringify({ runCount, vetoCount, auditCount }));
}

function getAuditEntries() {
  return JSON.parse(localStorage.getItem(AUDIT_KEY) ?? '[]');
}

function saveAuditEntry(entry) {
  const entries = getAuditEntries();
  entries.unshift(entry);           // newest first
  if (entries.length > 200) entries.length = 200;
  localStorage.setItem(AUDIT_KEY, JSON.stringify(entries));
}

function restoreAuditLog() {
  const entries = getAuditEntries();
  if (entries.length === 0) return;
  const tbody = document.getElementById('audit-tbody');
  tbody.innerHTML = '';
  entries.forEach(e => tbody.appendChild(buildAuditRow(e)));
}

// ── Event Pills ───────────────────────────────────────────────────────────────

function buildEventPills() {
  const container = document.getElementById('event-pills');
  SAMPLE_EVENTS.forEach(ev => {
    const pill = document.createElement('div');
    pill.className = 'event-pill';
    pill.textContent = ev.ticker;
    pill.title = ev.headline;
    pill.addEventListener('click', () => {
      document.querySelectorAll('.event-pill').forEach(p => p.classList.remove('active'));
      pill.classList.add('active');
      document.getElementById('inp-headline').value = ev.headline;
      document.getElementById('inp-ticker').value   = ev.ticker;
      document.getElementById('inp-source').value   = ev.source;
    });
    container.appendChild(pill);
  });
}

// ── Agent Thread UI ───────────────────────────────────────────────────────────

function buildAgentThread() {
  const container = document.getElementById('agent-thread');
  container.innerHTML = '';
  AGENT_ORDER.forEach((key, i) => {
    if (i > 0) {
      const conn = document.createElement('div');
      conn.className = 'connector';
      container.appendChild(conn);
    }
    const cfg  = AGENT_CONFIG[key];
    const card = document.createElement('div');
    card.className = 'agent-card';
    card.id = `card-${key}`;
    card.innerHTML = `
      <div class="agent-header" data-key="${key}">
        <div class="agent-icon" id="icon-${key}">${cfg.abbr}</div>
        <div class="agent-name">${cfg.label}</div>
        <div class="agent-tag" id="tag-${key}">Waiting</div>
      </div>
      <div class="agent-body" id="body-${key}">
        <div class="agent-payload" id="payload-${key}"></div>
      </div>
    `;
    // Expand on header click
    card.querySelector('.agent-header').addEventListener('click', () => {
      card.classList.toggle('expanded');
    });
    container.appendChild(card);
  });
}

function setAgentState(key, state, tagText, tagClass = '', payload = null) {
  const card = document.getElementById(`card-${key}`);
  const tag  = document.getElementById(`tag-${key}`);
  if (!card || !tag) return;

  card.className    = `agent-card ${state}`;
  tag.className     = `agent-tag ${tagClass}`;
  tag.textContent   = tagText;

  if (payload) renderPayload(key, payload);
}

function renderPayload(key, payload) {
  const el = document.getElementById(`payload-${key}`);
  if (!el) return;
  el.innerHTML = '';
  const flat = flattenObj(payload);
  Object.entries(flat).slice(0, 10).forEach(([k, v]) => {
    const row = document.createElement('div');
    row.className = 'payload-row';
    const valClass = colorForValue(k, v);
    row.innerHTML = `<span class="payload-key">${k}</span><span class="payload-val ${valClass}">${String(v)}</span>`;
    el.appendChild(row);
  });
}

// ── Pipeline run (exported as global for onclick) ────────────────────────────

window.runPipeline = async function () {
  const headline = document.getElementById('inp-headline').value.trim();
  const ticker   = document.getElementById('inp-ticker').value.trim().toUpperCase();
  const source   = document.getElementById('inp-source').value.trim() || 'Unknown';

  if (!headline || !ticker) {
    showToast('Please enter a headline and ticker.');
    return;
  }

  const btn   = document.getElementById('run-btn');
  const label = document.getElementById('run-btn-label');
  btn.disabled = true;

  // Reset thread
  buildAgentThread();

  const event = { headline, ticker, source };
  const token = loadToken();

  if (token) {
    label.textContent = 'Dispatching to GitHub Actions…';
    await runRealPipeline(event, token, label);
  } else {
    label.textContent = 'Running (mock)…';
    await runLocalMock(event);
  }

  runCount++;
  persistStats();
  updateStats();
  btn.disabled = false;
  label.textContent = 'Run Again';
  showToast('Pipeline complete — audit log updated');
};

// ── Real pipeline (GitHub Actions + GitHub Models) ────────────────────────────

async function runRealPipeline(event, token, label) {
  try {
    const runId = await dispatchPipeline(event, token);
    label.textContent = 'Waiting for GitHub Actions…';

    // Show mock preview while polling
    runLocalMock(event, { preview: true });

    const result = await pollForResult(runId, {
      onPing: (secsLeft) => {
        label.textContent = `Actions running… (${secsLeft}s)`;
      },
    });

    // Overlay real results onto mock preview
    buildAgentThread();
    applyResultsToUI(result, event);

  } catch (err) {
    showToast(`Actions error: ${err.message}. Falling back to mock.`);
    await runLocalMock(event);
  }
}

// ── Local mock pipeline ───────────────────────────────────────────────────────

async function runLocalMock(event, { preview = false } = {}) {
  let vetoed = false;
  const allPayloads = {};
  const logId = `TRD-${new Date().toISOString().slice(0, 10).replace(/-/g, '')}-${String(Math.floor(Math.random() * 9999)).padStart(4, '0')}`;

  await runMockPipeline(event, (agentKey, payload) => {
    allPayloads[agentKey] = payload;
    const tagText  = deriveTagText(agentKey, payload);
    const tagClass = deriveTagClass(agentKey, payload);

    if (agentKey === 'risk_manager' && payload.veto) {
      vetoed = true;
      vetoCount++;
      setAgentState(agentKey, 'vetoed', 'VETOED', 'vetoed', payload);
      // Hide downstream cards
      ['execution_agent', 'supervisor'].forEach(k => {
        const c = document.getElementById(`card-${k}`);
        if (c) c.style.display = 'none';
      });
    } else {
      setAgentState(agentKey, 'done', tagText, tagClass, payload);
    }

    // Briefly expand last card
    const card = document.getElementById(`card-${agentKey}`);
    if (card) {
      card.classList.add('expanded');
      setTimeout(() => card.classList.remove('expanded'), 3000);
    }
  });

  if (vetoed) vetoCount = Math.max(vetoCount, 0);
  if (!preview) persistAuditEntries(event, allPayloads, logId, vetoed);
}

// ── Apply real Actions results to UI ─────────────────────────────────────────

function applyResultsToUI(result, event) {
  const agentMap = {
    researcher:      result.researcher,
    signal_agent:    result.signal_agent,
    risk_manager:    result.risk_manager,
    execution_agent: result.execution_agent,
    supervisor:      result.supervisor,
  };

  const vetoed = result.risk_manager?.veto ?? false;
  const logId  = result.supervisor?.log_id ?? `TRD-REAL-${Date.now()}`;

  AGENT_ORDER.forEach(key => {
    const payload = agentMap[key];
    if (!payload) return;
    const tagText  = deriveTagText(key, payload);
    const tagClass = deriveTagClass(key, payload);
    const state    = key === 'risk_manager' && vetoed ? 'vetoed' : 'done';
    setAgentState(key, state, tagText, tagClass, payload);
  });

  if (vetoed) {
    vetoCount++;
    ['execution_agent', 'supervisor'].forEach(k => {
      const c = document.getElementById(`card-${k}`);
      if (c) c.style.display = 'none';
    });
  }

  persistAuditEntries(event, agentMap, logId, vetoed);
}

// ── Audit log ─────────────────────────────────────────────────────────────────

function persistAuditEntries(event, payloads, logId, vetoed) {
  const now   = new Date().toLocaleTimeString();
  const tbody = document.getElementById('audit-tbody');

  if (tbody.querySelector('.empty-state')) tbody.innerHTML = '';

  const agentsToLog = vetoed
    ? ['researcher', 'signal_agent', 'risk_manager', 'supervisor']
    : AGENT_ORDER;

  const entries = agentsToLog.map((ag, i) => ({
    time:    now,
    agent:   ag,
    type:    agentTypeLabel(ag),
    id:      `${logId}-${i}`,
    summary: buildSummary(ag, payloads[ag], event),
  }));

  // Newest first in table
  entries.reverse().forEach(entry => {
    saveAuditEntry(entry);
    tbody.insertBefore(buildAuditRow(entry), tbody.firstChild);
  });

  auditCount += entries.length;
  document.getElementById('m-audit').textContent = auditCount;
}

function buildAuditRow(entry) {
  const tr = document.createElement('tr');
  const agClass = entry.agent.replace('_agent', '').replace('_manager', '');
  tr.innerHTML = `
    <td>${entry.time}</td>
    <td><span class="audit-tag ${agClass}">${entry.agent}</span></td>
    <td>${entry.type}</td>
    <td style="font-family:var(--font-mono);font-size:10px;color:var(--muted);">${entry.id}</td>
    <td style="color:var(--muted)">${entry.summary}</td>
  `;
  return tr;
}

// ── Stats ─────────────────────────────────────────────────────────────────────

function updateStats() {
  const set = (id, val) => { const el = document.getElementById(id); if (el) el.textContent = val; };
  set('stat-runs',   runCount);
  set('stat-vetoes', vetoCount);
  set('stat-msgs',   auditCount);
  set('m-runs',      runCount   || '—');
  set('m-vetoes',    vetoCount  || '—');
  set('m-audit',     auditCount || '—');
}

// ── Token modal ───────────────────────────────────────────────────────────────

function renderModeIndicator() {
  const badge = document.getElementById('mode-badge');
  if (!badge) return;
  if (hasToken()) {
    badge.textContent = 'GitHub Models';
    badge.classList.add('live');
  } else {
    badge.textContent = 'Mock Mode';
    badge.classList.remove('live');
  }
}

window.openTokenModal = function () {
  const modal = document.getElementById('token-modal');
  if (modal) modal.classList.add('open');
};

window.closeTokenModal = function () {
  const modal = document.getElementById('token-modal');
  if (modal) modal.classList.remove('open');
};

window.saveGhToken = function () {
  const inp = document.getElementById('inp-token');
  const val = inp?.value.trim() ?? '';
  if (!val) { showToast('Please enter a valid token.'); return; }
  saveToken(val);
  renderModeIndicator();
  window.closeTokenModal();
  showToast('GitHub token saved for this session.');
};

window.clearGhToken = function () {
  clearToken();
  renderModeIndicator();
  showToast('Token cleared — running in mock mode.');
};

// ── Helpers ───────────────────────────────────────────────────────────────────

function deriveTagText(agentKey, data) {
  const map = {
    researcher:      () => data.signal || 'DONE',
    signal_agent:    () => data.action || 'DONE',
    risk_manager:    () => (data.verdict || 'DONE').split('_')[0],
    execution_agent: () => data.status  || 'DONE',
    supervisor:      () => data.audit_status || 'DONE',
  };
  return (map[agentKey] ?? (() => 'DONE'))();
}

function deriveTagClass(agentKey, data) {
  if (agentKey === 'researcher')
    return data.signal === 'BEARISH' ? 'bearish' : data.signal === 'BULLISH' ? 'bullish' : '';
  if (agentKey === 'signal_agent')
    return data.action === 'SHORT' ? 'bearish' : data.action === 'LONG' ? 'bullish' : '';
  if (agentKey === 'risk_manager')
    return data.veto ? 'vetoed' : 'approved';
  if (agentKey === 'execution_agent')
    return data.status === 'REJECTED' ? 'vetoed' : 'approved';
  if (agentKey === 'supervisor')
    return data.audit_status === 'COMPLIANT' ? 'compliant' : 'warning';
  return '';
}

function agentTypeLabel(ag) {
  const map = {
    researcher:      'RESEARCH_SIGNAL',
    signal_agent:    'TRADE_PROPOSAL',
    risk_manager:    'RISK_DECISION',
    execution_agent: 'EXECUTION_PLAN',
    supervisor:      'AUDIT_COMPLETE',
  };
  return map[ag] ?? 'MESSAGE';
}

function buildSummary(ag, payload, event) {
  if (!payload) return '—';
  const t = event.ticker.toUpperCase();
  if (ag === 'researcher')      return `${payload.signal} on ${t} — ${payload.summary?.slice(0, 60) ?? ''}`;
  if (ag === 'signal_agent')    return `${payload.action} ${t} @ $${payload.entry_price}`;
  if (ag === 'risk_manager')    return payload.reason?.slice(0, 70) ?? payload.verdict;
  if (ag === 'execution_agent') return `${payload.strategy} ${payload.duration_min}min / ${payload.child_orders} orders`;
  if (ag === 'supervisor')      return `${payload.audit_status} — ${payload.log_id}`;
  return '—';
}

function flattenObj(obj, prefix = '') {
  const out = {};
  for (const [k, v] of Object.entries(obj ?? {})) {
    const key = prefix ? `${prefix}.${k}` : k;
    if (v !== null && typeof v === 'object' && !Array.isArray(v)) {
      Object.assign(out, flattenObj(v, key));
    } else {
      out[key] = Array.isArray(v) ? v.join(', ') : v;
    }
  }
  return out;
}

function colorForValue(key, val) {
  const k = key.toLowerCase();
  if (k.includes('veto') && val === true)      return 'red';
  if (k.includes('signal') && val === 'BEARISH') return 'red';
  if (k.includes('signal') && val === 'BULLISH') return 'green';
  if (k.includes('action') && val === 'SHORT')   return 'red';
  if (k.includes('action') && val === 'LONG')    return 'green';
  if (k.includes('status') && val === 'COMPLIANT') return 'green';
  if (k.includes('sharpe') || k.includes('confidence')) return 'amber';
  return '';
}

function showToast(msg) {
  const t = document.getElementById('toast');
  if (!t) return;
  t.textContent = msg;
  t.classList.add('show');
  setTimeout(() => t.classList.remove('show'), 3000);
}
