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
                    pointRadius: 0
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

    const energyJ = data.energy_joules !== undefined ? data.energy_joules : data.energy_j;
    const vDrop = data.voltage_drop !== undefined
        ? data.voltage_drop
        : (data.vcap_before - data.vcap_after);

    setText('peakCurrent', data.peak_current_amps, 1);
    setText('avgCurrent', data.avg_current_amps, 1);
    setText('energy', energyJ, 2);
    setText('duration', data.duration_ms, 1);
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
    console.log('waveform_data: ' + samples.length + ' samples');

    if (!waveformChart) { console.error('Chart not ready'); return; }
    if (samples.length === 0) { console.warn('Empty waveform payload'); return; }

    const currentPts = [];

    samples.forEach(function (s, i) {
        const t = Number.isFinite(Number(s.time_ms)) ? Number(s.time_ms) : i * 0.2;
        const c = Number(s.current);
        if (Number.isFinite(c)) currentPts.push({ x: t, y: c });
    });

    if (currentPts.length === 0) { console.warn('No valid current points'); return; }

    const minT = currentPts[0].x;
    const maxT = currentPts[currentPts.length - 1].x;
    waveformChart.options.scales.x.min = minT;
    waveformChart.options.scales.x.max = maxT;

    // Auto-scale Y to peak with 10% headroom
    const peakA = Math.max(...currentPts.map(p => p.y));
    waveformChart.options.scales.y.max = Math.max(2000, Math.ceil(peakA * 1.1 / 100) * 100);

    waveformChart.data.labels = [];
    waveformChart.data.datasets[0].data = currentPts;
    waveformChart.update('active');

    const currents = currentPts.map(p => p.y);
    console.log('Chart updated | time ' + minT.toFixed(2) + '–' + maxT.toFixed(2) +
        ' ms | current ' + Math.min(...currents).toFixed(0) + '–' + Math.max(...currents).toFixed(0) + ' A');
});