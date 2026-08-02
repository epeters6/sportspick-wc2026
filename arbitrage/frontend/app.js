/**
 * Arbitrage Scanner — Frontend Logic v3
 *
 * Handles: data fetching, table rendering, sorting, filtering,
 *          auto-refresh with countdown, suspicious match detection,
 *          and settings persistence.
 */

// ── State ────────────────────────────────────────────────
let opportunities = [];
let sortColumn = 'roi_pct';
let sortAsc = false;
let refreshInterval = 30;
let countdownTimer = null;
let countdownValue = 0;

// ── Init ─────────────────────────────────────────────────
document.addEventListener('DOMContentLoaded', () => {
    loadSettings();
    fetchData();
    fetchStats();
    fetchShadowBets();
    startCountdown();
    setupSortHandlers();
    setupFilterHandlers();
});

// ── Data Fetching ────────────────────────────────────────
async function fetchData() {
    const statusDot = document.getElementById('statusDot');
    const statusText = document.getElementById('statusText');
    statusDot.className = 'status-dot loading';
    statusText.textContent = 'Fetching…';

    try {
        const resp = await fetch('/api/opportunities');
        if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
        const data = await resp.json();

        opportunities = data.opportunities || [];
        refreshInterval = data.refresh_interval || 30;

        statusDot.className = 'status-dot';
        const lastRefresh = data.last_refresh
            ? new Date(data.last_refresh).toLocaleTimeString()
            : 'never';
        statusText.textContent = `Live · ${lastRefresh}`;

        updateStats(data);
        renderTable();
        fetchShadowBets();
        resetCountdown();
    } catch (err) {
        statusDot.className = 'status-dot loading';
        statusText.textContent = `Error: ${err.message}`;
        console.error('Fetch error:', err);
    }
}

async function fetchStats() {
    try {
        const resp = await fetch('/api/stats');
        if (!resp.ok) return;
        const stats = await resp.json();

        const bestEver = stats.best_roi_ever || 0;
        const el = document.getElementById('statHistBest');
        if (el) {
            el.textContent = bestEver > 0 ? `${bestEver.toFixed(1)}%` : '—';
            el.className = `stat-value ${bestEver > 0 ? 'positive' : 'neutral'}`;
        }

        const detailEl = document.getElementById('statHistDetail');
        if (detailEl) {
            detailEl.textContent = stats.total_records > 0
                ? `${stats.total_records} records · ${stats.total_cycles} cycles`
                : 'No history yet';
        }
    } catch (e) { /* ignore */ }
}

async function fetchShadowBets() {
    try {
        const resp = await fetch('/api/shadow-bets');
        if (!resp.ok) return;
        const data = await resp.json();

        const curBal = data.current_balance || 10000.0;
        const pnl = data.realized_pnl || 0.0;
        const expectedOpen = data.expected_open_pnl || 0.0;
        const count = data.total_paper_bets || 0;

        const balEl = document.getElementById('statPaperBalance');
        if (balEl) {
            balEl.textContent = `$${curBal.toLocaleString('en-US', {minimumFractionDigits: 2, maximumFractionDigits: 2})}`;
            balEl.className = `stat-value ${pnl >= 0 ? 'positive' : 'negative'}`;
        }

        const detEl = document.getElementById('statPaperDetail');
        if (detEl) {
            detEl.textContent = `${count} verified bet${count !== 1 ? 's' : ''} Â· $${pnl.toFixed(2)} realized Â· $${expectedOpen.toFixed(2)} expected open`;
        }

        const sCountEl = document.getElementById('shadowCount');
        if (sCountEl) {
            sCountEl.textContent = `${count} paper position${count !== 1 ? 's' : ''}`;
        }

        renderShadowTable(data.recent_bets || []);
    } catch (e) { console.error('Shadow fetch error:', e); }
}

function renderShadowTable(bets) {
    const tbody = document.getElementById('shadowBody');
    if (!tbody) return;

    if (!bets || bets.length === 0) {
        tbody.innerHTML = `<tr><td colspan="9" style="text-align:center; padding: 20px; color: var(--text-muted);">
            No shadow bets placed yet. Verified high-confidence signals (≥80%) automatically generate paper trades.
        </td></tr>`;
        return;
    }

    tbody.innerHTML = bets.map(b => {
        const pnl = b.realized_pnl || 0;
        const pnlClass = pnl > 0 ? 'profit-positive' : pnl < 0 ? 'profit-negative' : '';
        const timeStr = b.timestamp ? new Date(b.timestamp).toLocaleTimeString() : '—';

        return `<tr>
            <td class="cell-mono" style="font-size:0.75rem; color:var(--text-muted);">${timeStr}</td>
            <td class="cell-event">
                <div class="event-name" style="font-size:0.8rem;">${esc(b.event_name)}</div>
            </td>
            <td>
                <span class="direction-badge ${b.buy_yes_on}">
                    YES → ${b.buy_yes_on === 'kalshi' ? 'K' : 'PM'}
                </span>
            </td>
            <td class="cell-mono">${b.size_contracts} contracts</td>
            <td class="cell-mono">$${(b.entry_capital || 0).toFixed(2)}</td>
            <td class="cell-mono">${cents(b.expected_net_gap)}</td>
            <td>
                <span class="roi-chip positive">+${(b.expected_roi || 0).toFixed(1)}%</span>
            </td>
            <td>
                <span style="font-size:0.75rem; font-weight:600;">${b.status}</span>
            </td>
            <td class="cell-mono ${pnlClass}">
                ${b.status === 'SETTLED' && pnl > 0 ? '+' : ''}$${pnl.toFixed(2)}
            </td>
        </tr>`;
    }).join('');
}

async function triggerRefresh() {
    const btn = document.getElementById('refreshBtn');
    btn.disabled = true;
    btn.textContent = '↻ Refreshing…';
    clearInterval(countdownTimer);
    try {
        await fetch('/api/refresh', { method: 'POST' });
        await fetchData();
        await fetchStats();
        await fetchShadowBets();
    } catch (err) {
        console.error('Refresh failed:', err);
    } finally {
        btn.disabled = false;
        btn.textContent = '↻ Refresh';
        startCountdown();
    }
}

// ── Stats ────────────────────────────────────────────────
function updateStats(data) {
    const opps = data.opportunities || [];
    // Count non-suspicious profitable
    const trustworthy = opps.filter(o => !o.suspicious);
    const profitable = trustworthy.filter(o => o.net_gap > 0);
    const bestRoi = trustworthy.length > 0
        ? Math.max(...trustworthy.map(o => o.roi_pct))
        : 0;

    document.getElementById('statPairs').textContent = trustworthy.length;
    document.getElementById('statPairsDetail').textContent =
        `from scan #${data.scan_count || 0} (${opps.length - trustworthy.length} suspicious hidden)`;

    const profEl = document.getElementById('statProfitable');
    profEl.textContent = profitable.length;
    profEl.className = `stat-value ${profitable.length > 0 ? 'positive' : 'neutral'}`;

    const roiEl = document.getElementById('statBestRoi');
    roiEl.textContent = bestRoi !== 0 ? `${bestRoi.toFixed(1)}%` : '—';
    roiEl.className = `stat-value ${bestRoi > 0 ? 'positive' : bestRoi < 0 ? 'negative' : 'neutral'}`;

    document.getElementById('statScans').textContent = data.scan_count || 0;
}

// ── Table Rendering ──────────────────────────────────────
function renderTable() {
    const tbody = document.getElementById('oppBody');
    const countEl = document.getElementById('tableCount');

    // Apply filters
    const minRoi = parseFloat(document.getElementById('filterMinRoi').value) || -999;
    const minConf = parseFloat(document.getElementById('filterConfidence').value) || 0;
    const profOnly = document.getElementById('filterProfitable').checked;
    const hideSus = document.getElementById('filterSuspicious')
        ? document.getElementById('filterSuspicious').checked
        : true;

    let filtered = opportunities.filter(o => {
        if (o.roi_pct < minRoi) return false;
        if (o.match_confidence < minConf) return false;
        if (profOnly && o.net_gap <= 0) return false;
        if (hideSus && o.suspicious) return false;
        return true;
    });

    // Sort
    filtered.sort((a, b) => {
        let va = a[sortColumn];
        let vb = b[sortColumn];
        if (typeof va === 'string') va = va.toLowerCase();
        if (typeof vb === 'string') vb = vb.toLowerCase();
        if (va < vb) return sortAsc ? -1 : 1;
        if (va > vb) return sortAsc ? 1 : -1;
        return 0;
    });

    countEl.textContent = `${filtered.length} result${filtered.length !== 1 ? 's' : ''}`;

    if (filtered.length === 0) {
        tbody.innerHTML = `
            <tr><td colspan="10">
                <div class="empty-state">
                    <div class="icon">📊</div>
                    <h3>No opportunities found</h3>
                    <p>Adjust your filters or wait for the next scan cycle.
                       The scanner compares live prices across Kalshi and Polymarket
                       to find matched events with price differences.</p>
                </div>
            </td></tr>`;
        return;
    }

    tbody.innerHTML = filtered.map(o => {
        const isProfitable = o.net_gap > 0;
        const rowClass = [
            isProfitable ? 'profitable' : '',
            o.suspicious ? 'suspicious-row' : ''
        ].filter(Boolean).join(' ');

        const confLevel = o.match_confidence >= 80 ? 'high'
            : o.match_confidence >= 65 ? 'medium' : 'low';

        const roiClass = o.roi_pct > 0 ? 'positive'
            : o.roi_pct < 0 ? 'negative' : 'zero';

        const suspIcon = o.suspicious
            ? '<span class="suspicious-badge" title="Large gap + low confidence = likely wrong match">⚠️</span> '
            : '';

        return `<tr class="${rowClass}">
            <td class="cell-event">
                <div class="event-name">
                    ${suspIcon}${esc(o.event_name)}
                </div>
                <div class="event-titles">
                    <span class="platform-label k">K:</span> ${esc(o.kalshi_title).substring(0, 80)}
                </div>
                <div class="event-titles">
                    <span class="platform-label p">PM:</span> ${esc(o.polymarket_title).substring(0, 80)}
                </div>
                <div class="event-links">
                    ${o.kalshi_url ? `<a href="${esc(o.kalshi_url)}" target="_blank">Kalshi ↗</a>` : ''}
                    ${o.polymarket_url ? `<a href="${esc(o.polymarket_url)}" target="_blank">Polymarket ↗</a>` : ''}
                </div>
            </td>
            <td class="cell-price">
                <span class="price-yes">${cents(o.kalshi_yes)}</span>
            </td>
            <td class="cell-price">
                <span class="price-yes">${cents(o.polymarket_yes)}</span>
            </td>
            <td>
                <span class="direction-badge ${o.buy_yes_on}">
                    YES → ${o.buy_yes_on === 'kalshi' ? 'K' : 'PM'}
                </span>
            </td>
            <td class="cell-mono">${cents(o.gross_gap)}</td>
            <td class="cell-mono" style="color: var(--text-muted)">
                ${cents(o.total_fees)}
                <div style="font-size:0.65rem; color:var(--text-muted); margin-top:2px;">
                    K:${cents(o.kalshi_fee)} P:${cents(o.polymarket_fee)}
                    Risk:${cents(o.execution_buffer || 0)}
                </div>
            </td>
            <td class="cell-mono ${isProfitable ? 'profit-positive' : 'profit-negative'}">
                ${isProfitable ? '+' : ''}${cents(o.net_gap)}
            </td>
            <td>
                <span class="roi-chip ${roiClass}">
                    ${o.roi_pct > 0 ? '+' : ''}${o.roi_pct.toFixed(1)}%
                </span>
            </td>
            <td class="cell-mono">${cents(o.capital_required)}<div style="font-size:0.65rem; color:var(--text-muted);">${(o.executable_size || 0).toFixed(1)} visible</div></td>
            <td>
                <div class="confidence-bar">
                    <div class="confidence-track">
                        <div class="confidence-fill ${confLevel}"
                             style="width:${o.match_confidence}%"></div>
                    </div>
                    <span style="font-size:0.75rem; color:var(--text-muted)">
                        ${o.match_confidence.toFixed(0)}%
                    </span>
                </div>
            </td>
        </tr>`;
    }).join('');
}

// ── Sorting ──────────────────────────────────────────────
function setupSortHandlers() {
    document.querySelectorAll('thead th[data-sort]').forEach(th => {
        th.addEventListener('click', () => {
            const col = th.dataset.sort;
            if (sortColumn === col) {
                sortAsc = !sortAsc;
            } else {
                sortColumn = col;
                sortAsc = false;
            }
            // Update UI
            document.querySelectorAll('thead th').forEach(h => h.classList.remove('sorted'));
            th.classList.add('sorted');
            th.querySelector('.sort-arrow').textContent = sortAsc ? '▲' : '▼';
            renderTable();
        });
    });
}

// ── Filters ──────────────────────────────────────────────
function setupFilterHandlers() {
    ['filterMinRoi', 'filterConfidence', 'filterProfitable', 'filterSuspicious'].forEach(id => {
        const el = document.getElementById(id);
        if (!el) return;
        el.addEventListener('change', renderTable);
        if (el.type === 'number') el.addEventListener('input', renderTable);
    });
}

// ── Countdown ────────────────────────────────────────────
function startCountdown() {
    countdownValue = refreshInterval;
    clearInterval(countdownTimer);
    countdownTimer = setInterval(() => {
        countdownValue--;
        document.getElementById('countdown').textContent =
            `Next refresh in ${countdownValue}s`;
        if (countdownValue <= 0) {
            fetchData();
            fetchStats();
            fetchShadowBets();
            countdownValue = refreshInterval;
        }
    }, 1000);
}

function resetCountdown() {
    countdownValue = refreshInterval;
}

// ── Settings ─────────────────────────────────────────────
function toggleSettings() {
    document.getElementById('settingsPanel').classList.toggle('open');
    document.getElementById('overlay').classList.toggle('open');
}

function loadSettings() {
    try {
        const saved = JSON.parse(localStorage.getItem('arbSettings') || '{}');
        if (saved.refreshInterval) {
            refreshInterval = saved.refreshInterval;
            document.getElementById('settingRefresh').value = saved.refreshInterval;
        }
        if (saved.minRoi !== undefined) {
            document.getElementById('filterMinRoi').value = saved.minRoi;
            document.getElementById('settingMinRoi').value = saved.minRoi;
        }
        if (saved.minConf !== undefined) {
            document.getElementById('filterConfidence').value = saved.minConf;
            document.getElementById('settingMinConf').value = saved.minConf;
        }
    } catch (e) { /* ignore */ }
}

function saveSettings() {
    const ri = parseInt(document.getElementById('settingRefresh').value) || 30;
    const mr = parseFloat(document.getElementById('settingMinRoi').value) || 0;
    const mc = parseFloat(document.getElementById('settingMinConf').value) || 70;

    refreshInterval = Math.max(5, ri);
    document.getElementById('filterMinRoi').value = mr;
    document.getElementById('filterConfidence').value = mc;

    localStorage.setItem('arbSettings', JSON.stringify({
        refreshInterval, minRoi: mr, minConf: mc,
    }));

    // Update server config
    fetch('/api/config', {
        method: 'POST',
        headers: { 'Content-Type': 'application/x-www-form-urlencoded' },
        body: `refresh_interval=${refreshInterval}&min_roi_pct=${mr}`,
    }).catch(() => {});

    renderTable();
    startCountdown();
    toggleSettings();
}

// ── Helpers ──────────────────────────────────────────────
function esc(str) {
    if (!str) return '';
    const div = document.createElement('div');
    div.textContent = str;
    return div.innerHTML;
}

function cents(dollars) {
    if (dollars === undefined || dollars === null) return '—';
    const val = Number(dollars);
    if (isNaN(val)) return '—';
    const c = (val * 100).toFixed(1);
    return `${c}¢`;
}
