// ============================================================
// monitor_enhanced.js  —  Spot Welder Monitor
// ============================================================

const socket = io();
let waveformChart = null;

const DEFAULT_SAMPLE_INTERVAL_MS = 0.1;

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

    const meta = (data && typeof data.meta === 'object' && data.meta) ? data.meta : {};
    const sampleIntervalMs = Number.isFinite(Number(meta.sample_interval_ms))
        ? Number(meta.sample_interval_ms)
        : DEFAULT_SAMPLE_INTERVAL_MS;

    samples.forEach(function (s, i) {
        let t = NaN;

        if (Number.isFinite(Number(s.t))) {
            t = Number(s.t);
        } else if (Number.isFinite(Number(s.time_ms))) {
            t = Number(s.time_ms);
        } else if (Number.isFinite(Number(s.timestamp_us))) {
            t = Number(s.timestamp_us) / 1000.0;
        } else {
            t = i * sampleIntervalMs;
        }

        const c = Number(s.current);
        if (Number.isFinite(c) && Number.isFinite(t)) currentPts.push({ x: t, y: c });
    });

    if (currentPts.length === 0) { console.warn('No valid current points'); return; }

    const pointMinT = Math.min(...currentPts.map(p => p.x));
    const pointMaxT = Math.max(...currentPts.map(p => p.x));

    const metaMinT = Number(meta.axis_min_time_ms);
    const metaMaxT = Number(meta.axis_max_time_ms);

    const minT = Number.isFinite(metaMinT) ? Math.min(pointMinT, metaMinT) : pointMinT;
    let maxT = Number.isFinite(metaMaxT) ? Math.max(pointMaxT, metaMaxT) : pointMaxT;

    // Backward-compatible fallback for legacy packets that only provide wf_samples.
    const wfSamples = Number(meta.wf_samples);
    const pulseStartSample = Number(meta.pulse_start_sample);
    if (Number.isFinite(wfSamples) && Number.isFinite(pulseStartSample) && wfSamples > pulseStartSample) {
        const computedMax = ((wfSamples - pulseStartSample) - 1) * sampleIntervalMs;
        if (Number.isFinite(computedMax)) {
            maxT = Math.max(maxT, computedMax);
        }
    }
    waveformChart.options.scales.x.min = minT;
    waveformChart.options.scales.x.max = maxT;

    // Auto-scale Y directly from waveform peak (no fixed 2000A floor).
    const peakA = Math.max(...currentPts.map(p => p.y));
    const yHeadroom = Math.max(5, peakA * 0.12);
    const yMax = Math.ceil((peakA + yHeadroom) / 10) * 10;
    waveformChart.options.scales.y.max = Math.max(20, yMax);

    waveformChart.data.labels = [];
    waveformChart.data.datasets[0].data = currentPts;
    waveformChart.update('active');

    const currents = currentPts.map(p => p.y);
    console.log('Chart updated | time ' + minT.toFixed(2) + '–' + maxT.toFixed(2) +
        ' ms | current ' + Math.min(...currents).toFixed(0) + '–' + Math.max(...currents).toFixed(0) + ' A');
});