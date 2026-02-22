/**
 * API Service — dispatches pipeline run requests to GitHub Actions
 * and polls for results stored as static JSON artifacts.
 *
 * Architecture:
 *   Browser → POST /api/dispatch (triggers GH Actions workflow)
 *          → polls /public/data/pipeline-{runId}.json until ready
 *
 * For the GitHub Pages + GitHub Models setup:
 *   - The frontend triggers a workflow_dispatch via GitHub REST API
 *   - The workflow runs the 5-agent pipeline using GITHUB_TOKEN + GitHub Models
 *   - Results are committed to /public/data/ and served as static JSON
 *   - Frontend polls until the result file appears
 */

const GITHUB_OWNER = window.TRADECRAFT_CONFIG?.owner ?? '';
const GITHUB_REPO  = window.TRADECRAFT_CONFIG?.repo  ?? '';
const API_VERSION  = '2022-11-28';

/**
 * Dispatch a pipeline run via GitHub Actions workflow_dispatch.
 * Returns a run reference ID the frontend can use to poll for results.
 *
 * @param {object} event  { headline, ticker, source }
 * @param {string} token  GitHub PAT with `actions:write` scope (stored in sessionStorage)
 * @returns {Promise<string>} runId
 */
export async function dispatchPipeline(event, token) {
  const runId = `run-${Date.now()}`;

  const resp = await fetch(
    `https://api.github.com/repos/${GITHUB_OWNER}/${GITHUB_REPO}/actions/workflows/pipeline-run.yml/dispatches`,
    {
      method: 'POST',
      headers: {
        Authorization: `Bearer ${token}`,
        Accept: `application/vnd.github+json`,
        'X-GitHub-Api-Version': API_VERSION,
        'Content-Type': 'application/json',
      },
      body: JSON.stringify({
        ref: 'main',
        inputs: {
          headline: event.headline,
          ticker:   event.ticker,
          source:   event.source,
          run_id:   runId,
        },
      }),
    }
  );

  if (!resp.ok) {
    const err = await resp.json().catch(() => ({}));
    throw new Error(err.message || `Dispatch failed: ${resp.status}`);
  }

  return runId;
}

/**
 * Poll for pipeline result JSON committed by the Actions workflow.
 * The workflow writes results to /public/data/pipeline-{runId}.json
 *
 * @param {string} runId
 * @param {object} opts   { maxWaitMs, intervalMs, onPing }
 * @returns {Promise<object>} pipeline result payload
 */
export async function pollForResult(runId, {
  maxWaitMs  = 120_000,
  intervalMs = 3_000,
  onPing     = () => {},
} = {}) {
  const url = `./data/pipeline-${runId}.json?t=${Date.now()}`;
  const deadline = Date.now() + maxWaitMs;

  while (Date.now() < deadline) {
    await sleep(intervalMs);
    onPing(Math.round((deadline - Date.now()) / 1000));

    try {
      const resp = await fetch(url + `&t=${Date.now()}`);
      if (resp.ok) {
        const data = await resp.json();
        if (data?.status === 'complete') return data;
      }
    } catch {
      // Not ready yet — keep polling
    }
  }

  throw new Error('Pipeline timed out after 2 minutes. Check Actions tab.');
}

/**
 * Load the latest pipeline results index.
 * The Actions workflow maintains public/data/runs-index.json
 */
export async function loadRunsIndex() {
  try {
    const resp = await fetch(`./data/runs-index.json?t=${Date.now()}`);
    if (!resp.ok) return [];
    return await resp.json();
  } catch {
    return [];
  }
}

/**
 * Load a specific pipeline result by runId.
 */
export async function loadRun(runId) {
  const resp = await fetch(`./data/pipeline-${runId}.json?t=${Date.now()}`);
  if (!resp.ok) throw new Error(`Run ${runId} not found`);
  return resp.json();
}

// ── Auth helpers ──────────────────────────────────────────────────────────────

const TOKEN_KEY = 'tradecraft_gh_token';

export function saveToken(token) {
  sessionStorage.setItem(TOKEN_KEY, token);
}

export function loadToken() {
  return sessionStorage.getItem(TOKEN_KEY) ?? '';
}

export function clearToken() {
  sessionStorage.removeItem(TOKEN_KEY);
}

export function hasToken() {
  return Boolean(loadToken());
}

// ── Utils ─────────────────────────────────────────────────────────────────────

function sleep(ms) {
  return new Promise(r => setTimeout(r, ms));
}
