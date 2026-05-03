// ============================================================
// monitor_enhanced.js  —  Spot Welder Monitor
// ============================================================

const socket = io();
let waveformChart = null;

// ── Connection status ────────────────────────────────────────
socket.on('connect', function () {
    console.log('SocketIO connected');
    const el = document.getElementById('connectionStatus');
    if (el) { el.textContent = 'Connected'; el.className = 'connection-status connected'; }
});

socket.on('disconnect', function () {
    console.log('SocketIO disconnected');
    const el = document.getElementById('connectionStatus');
    if (el) { el.textContent = 'Disconnected'; el.className = 'connection-status disconnected'; }
});

function isFiniteNum(v) {
    const n = Number(v);
    return Number.isFinite(n) ? n : null;
}

function toConfiguredMs(v) {
    if (v === null || v === undefined || v === '') return null;
    const n = isFiniteNum(v);
    if (n === null || n < 0) return null;
    return Math.round(n);
}


// ── Chart initialisation ─────────────────────────────────────
document.addEventListener('DOMContentLoaded', function () {
    const ctx = document.getElementById('waveformChart');
    if (!ctx) { console.error('Canvas #waveformChart not found'); return; }

    // Orange gradient fill for the waveform
    const gradient = ctx.getContext('2d').createLinearGradient(0, 0, 0, 340);
    gradient.addColorStop(0, 'rgba(255, 107, 53, 0.45)');
    gradient.addColorStop(1, 'rgba(255, 107, 53, 0.02)');

    waveformChart = new Chart(ctx, {
        type: 'line',
        data: {
            labels: [],
            datasets: [
                {
                    label: 'Current (A)',
                    data: [],
                    borderColor: '#ff6b35',
                    backgroundColor: gradient,
                    fill: true,
                    tension: 0.3,
                    borderWidth: 2.5,
                    pointRadius: 0,
                    pointHoverRadius: 0,
                    pointHitRadius: 0
                }
            ]
        },
        options: {
            responsive: true,
            maintainAspectRatio: false,
            animation: false,
            interaction: { mode: 'index', intersect: false },
            plugins: {
                legend: { display: false },
                tooltip: { enabled: true }
            },
            elements: {
                point: {
                    radius: 0,
                    hoverRadius: 0,
                    hitRadius: 0
                }
            },
            scales: {
                x: {
                    type: 'linear',
                    title: { display: true, text: 'Time (ms)', color: '#aaa' },
                    ticks: { color: '#aaa', callback: v => Number(v).toFixed(2) },
                    grid: { color: 'rgba(255,255,255,0.05)' }
                },
                y: {
                    type: 'linear',
                    position: 'left',
                    min: 0,
                    max: 2000,
                    title: { display: true, text: 'Current (A)', color: '#ff6b35' },
                    ticks: { color: '#ff6b35' },
                    grid: { color: 'rgba(255, 107, 53, 0.08)' }
                }
            }
        }
    });

    console.log('Waveform chart initialised');
});

// ── Helper: safe set element text ────────────────────────────
function setText(id, val, decimals) {
    const el = document.getElementById(id);
    if (!el) return;
    el.textContent = (val !== undefined && val !== null && !isNaN(parseFloat(val)))
        ? parseFloat(val).toFixed(decimals)
        : '--';
}

// ── Weld complete ─────────────────────────────────────────────
socket.on('weld_complete', function (data) {
    console.log('weld_complete:', data);

    const energyJ = data.energy_weld_j !== undefined ? data.energy_weld_j : (data.energy_joules !== undefined ? data.energy_joules : data.energy_j);
    const vDrop = data.voltage_drop !== undefined
        ? data.voltage_drop
        : (data.vcap_before - data.vcap_after);

    setText('peakCurrent', data.peak_current_amps, 1);
    setText('avgCurrent', data.avg_current_amps, 1);
    setText('energy', energyJ, 2);

    const configuredMainDurationMs = toConfiguredMs(data.configured_main_ms ?? data.d1);
    if (configuredMainDurationMs !== null) {
        setText('duration', configuredMainDurationMs, 0);
    } else {
        setText('duration', null, 0);
    }

    setText('voltageDrop', vDrop, 3);

    setText('vcapBefore', data.vcap_before, 3);
    setText('vcapAfter', data.vcap_after, 3);
    setText('vcapDrop', vDrop, 3);

    // Animate drop bar (×8 to make small drops visible)
    if (data.vcap_before > 0 && vDrop !== undefined) {
        const pct = Math.min((vDrop / data.vcap_before) * 100 * 8, 100);
        const bar = document.getElementById('vcapBar');
        if (bar) bar.style.width = pct.toFixed(1) + '%';
    }

    const timeEl = document.getElementById('lastWeldTime');
    if (timeEl) timeEl.textContent = new Date().toLocaleTimeString();
});

// ── Weld start flash ──────────────────────────────────────────
socket.on('weld_event', function (data) {
    if (!data.active) return;
    const el = document.getElementById('weldStatus');
    if (!el) return;
    el.textContent = 'WELDING';
    el.className = 'weld-status welding';
    setTimeout(function () {
        el.textContent = 'IDLE';
        el.className = 'weld-status idle';
    }, 600);
});

// ── Waveform data ─────────────────────────────────────────────
socket.on('waveform_data', function (data) {
    const samples = Array.isArray(data && data.samples) ? data.samples : [];
    if (!waveformChart || samples.length === 0) return;

    const chartData = samples
        .map(function (s) {
            const t = Number(s.timestamp_us);
            const i = Number(s.current);
            if (!Number.isFinite(t) || !Number.isFinite(i)) return null;
            return { x: t / 1000.0, y: i };
        })
        .filter(Boolean);

    if (chartData.length === 0) return;

    const peakA = Math.max(...chartData.map(p => p.y));
    const yMax = Math.ceil((peakA * 1.12) / 10) * 10;
    waveformChart.options.scales.y.max = Math.max(20, yMax);
    waveformChart.options.scales.x.min = 0;
    waveformChart.options.scales.x.max = undefined;
    waveformChart.data.labels = [];
    waveformChart.data.datasets[0].data = chartData;

    waveformChart.update('none');
});
