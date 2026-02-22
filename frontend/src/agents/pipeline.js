/**
 * Client-side mock pipeline — runs immediately in the browser.
 *
 * Used when:
 *  a) The user has not provided a GitHub token
 *  b) As a fast preview while waiting for the real Actions result
 *
 * Each agent function returns a structured payload matching the
 * exact schema used by the Python backend, so the UI rendering
 * code is identical for both mock and real results.
 */

// ── Ticker-aware mock data ────────────────────────────────────────────────────

const TICKER_PROFILES = {
  NVDA: { bias: 'BULLISH', action: 'LONG',  price: 875.00, stop: 850.00, tp: 920.00, sharpe: 1.8 },
  AAPL: { bias: 'BEARISH', action: 'SHORT', price: 189.50, stop: 193.00, tp: 182.00, sharpe: 1.4 },
  SPY:  { bias: 'BEARISH', action: 'SHORT', price: 502.00, stop: 512.00, tp: 488.00, sharpe: 1.1 },
  MSFT: { bias: 'BULLISH', action: 'LONG',  price: 415.00, stop: 402.00, tp: 435.00, sharpe: 1.6 },
  TSLA: { bias: 'BEARISH', action: 'SHORT', price: 265.00, stop: 278.00, tp: 248.00, sharpe: 0.9 },
  META: { bias: 'BULLISH', action: 'LONG',  price: 510.00, stop: 495.00, tp: 535.00, sharpe: 1.7 },
};

function getProfile(ticker) {
  return TICKER_PROFILES[ticker.toUpperCase()] ?? {
    bias: 'NEUTRAL', action: 'HOLD',
    price: 100.00, stop: 95.00, tp: 108.00, sharpe: 0.8,
  };
}

// ── Individual agent simulators ───────────────────────────────────────────────

function runResearcher(event) {
  const profile = getProfile(event.ticker);
  const confidence = parseFloat((0.65 + Math.random() * 0.28).toFixed(2));
  return {
    signal:     profile.bias,
    confidence,
    summary:    buildResearchSummary(event, profile),
    sources:    [event.source, 'Macro Desk', 'SEC Filings'],
    regime:     profile.bias === 'BULLISH' ? 'RISK_ON' : 'RISK_OFF',
    key_risks:  buildRisks(profile),
  };
}

function runSignalAgent(event, researchPayload) {
  const profile = getProfile(event.ticker);
  const confidence = researchPayload.confidence;
  const size = parseFloat(Math.min(confidence * 12, 8).toFixed(1));
  return {
    action:             researchPayload.signal === 'BULLISH' ? 'LONG' : researchPayload.signal === 'BEARISH' ? 'SHORT' : 'HOLD',
    ticker:             event.ticker.toUpperCase(),
    size_pct:           size,
    entry_price:        profile.price,
    stop_loss:          profile.stop,
    take_profit:        profile.tp,
    rationale:          `${researchPayload.summary.slice(0, 80)}... Pattern match: 5 similar events → ${profile.bias === 'BULLISH' ? '+' : '-'}1.2% median 3-day move.`,
    backtest_sharpe:    profile.sharpe,
    expected_return_pct: parseFloat(((profile.tp - profile.price) / profile.price * 100).toFixed(2)),
    based_on_signal_id: 'mock-res-001',
  };
}

function runRiskManager(signalPayload) {
  const rawSize = signalPayload.size_pct;
  const MAX_POS = 5.0;
  const veto = rawSize > 20 || signalPayload.action === 'HOLD';
  const adjusted = veto ? null : Math.min(rawSize, MAX_POS);
  const verdict  = veto ? 'VETOED'
    : rawSize > MAX_POS ? 'APPROVED_WITH_CONDITIONS'
    : 'APPROVED';

  return {
    verdict,
    veto,
    adjusted_size_pct: adjusted,
    reason: veto
      ? `Position ${rawSize}% exceeds hard limit or HOLD signal — trade rejected.`
      : rawSize > MAX_POS
        ? `Scaled ${rawSize}% → ${adjusted}% (single-stock 5% cap). Drawdown headroom: 3.8%.`
        : `Position ${rawSize}% within risk limits. Drawdown headroom: 6.2%.`,
    risk_metrics: {
      position_limit_ok: rawSize <= MAX_POS,
      drawdown_ok:       true,
      liquidity_ok:      true,
    },
  };
}

function runExecutionAgent(signalPayload, riskPayload) {
  if (riskPayload.veto) {
    return { status: 'REJECTED', reason: 'Vetoed by Risk Manager.' };
  }
  const effectiveSize = riskPayload.adjusted_size_pct ?? signalPayload.size_pct;
  const strategy = effectiveSize > 3 ? 'TWAP' : 'LIMIT';
  return {
    strategy,
    duration_min:          30,
    child_orders:          6,
    limit_price:           signalPayload.entry_price,
    expected_slippage_bps: parseFloat((3.5 + Math.random() * 2).toFixed(1)),
    venue:                 'PAPER_EXCHANGE',
    status:                'SIMULATED_FILL',
    notes:                 `${strategy} over 30 min. Size: ${effectiveSize}% of NAV.`,
  };
}

function runSupervisor(all) {
  const logId = `TRD-${new Date().toISOString().slice(0,10).replace(/-/g,'')}-${String(Math.floor(Math.random()*9999)).padStart(4,'0')}`;
  const hasResearch = Boolean(all.researcher);
  const hasRisk     = Boolean(all.risk_manager);
  const chainOk     = hasResearch && hasRisk;

  return {
    audit_status:              chainOk ? 'COMPLIANT' : 'NON_COMPLIANT',
    circuit_breaker_triggered: false,
    human_review_required:     Boolean(all.risk_manager?.veto),
    flags:                     chainOk ? [] : ['Missing research basis or risk review'],
    compliance_notes:          chainOk
      ? 'Full decision chain verified. Risk limits honored. No regulatory flags detected.'
      : 'Incomplete decision chain — manual review required.',
    log_id:                    logId,
    decision_chain_complete:   chainOk,
    total_messages_audited:    Object.keys(all).length,
  };
}

// ── Orchestrator ──────────────────────────────────────────────────────────────

/**
 * Run the full mock pipeline, calling onStep after each agent.
 *
 * @param {object} event   { headline, ticker, source }
 * @param {function} onStep  (agentKey, payload, done) callback for streaming UI
 * @returns {Promise<object>} full results map
 */
export async function runMockPipeline(event, onStep) {
  const results = {};

  // ── 1. Researcher ──
  await delay(600 + jitter());
  const researchPayload = runResearcher(event);
  results.researcher = researchPayload;
  onStep('researcher', researchPayload);

  // ── 2. Signal Agent ──
  await delay(700 + jitter());
  const signalPayload = runSignalAgent(event, researchPayload);
  results.signal_agent = signalPayload;
  onStep('signal_agent', signalPayload);

  // ── 3. Risk Manager ──
  await delay(800 + jitter());
  const riskPayload = runRiskManager(signalPayload);
  results.risk_manager = riskPayload;
  onStep('risk_manager', riskPayload);

  if (riskPayload.veto) {
    // Early exit — run supervisor for audit then stop
    await delay(500 + jitter());
    const supervisorPayload = runSupervisor(results);
    results.supervisor = supervisorPayload;
    onStep('supervisor', supervisorPayload, true);
    return { ...results, _vetoed: true };
  }

  // ── 4. Execution Agent ──
  await delay(700 + jitter());
  const execPayload = runExecutionAgent(signalPayload, riskPayload);
  results.execution_agent = execPayload;
  onStep('execution_agent', execPayload);

  // ── 5. Supervisor ──
  await delay(600 + jitter());
  const supervisorPayload = runSupervisor(results);
  results.supervisor = supervisorPayload;
  onStep('supervisor', supervisorPayload, true);

  return { ...results, status: 'complete', _vetoed: false };
}

// ── Helpers ───────────────────────────────────────────────────────────────────

function delay(ms) { return new Promise(r => setTimeout(r, ms)); }
function jitter()  { return Math.random() * 400; }

function buildResearchSummary(event, profile) {
  const dir = profile.bias === 'BULLISH' ? 'upside' : profile.bias === 'BEARISH' ? 'downside' : 'sideways';
  return `${event.headline} Points to ${dir} momentum for ${event.ticker}. Macro regime: ${profile.bias === 'BULLISH' ? 'RISK_ON' : 'RISK_OFF'}.`;
}

function buildRisks(profile) {
  const base = ['Macro regime shift', 'Fed policy surprise'];
  return profile.bias === 'BULLISH'
    ? [...base, 'Overbought technicals', 'Earnings miss risk']
    : [...base, 'Short squeeze potential', 'Sector rotation inflows'];
}
