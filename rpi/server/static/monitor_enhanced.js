// Initialize SocketIO connection
const socket = io();

// Global chart variable
let waveformChart = null;

// Connection status
socket.on('connect', function () {
    console.log('SocketIO connected');
    document.getElementById('connectionStatus').textContent = 'Connected';
    document.getElementById('connectionStatus').className = 'connection-status connected';
});

socket.on('disconnect', function () {
    console.log('SocketIO disconnected');
    document.getElementById('connectionStatus').textContent = 'Disconnected';
    document.getElementById('connectionStatus').className = 'connection-status disconnected';
});

// Initialize Chart.js when page loads
document.addEventListener('DOMContentLoaded', function () {
    console.log('Initializing waveform chart...');

    const ctx = document.getElementById('waveformChart');
    if (!ctx) {
        console.error('Canvas element not found!');
        return;
    }

    waveformChart = new Chart(ctx, {
        type: 'line',
        data: {
            labels: [],
            datasets: [
                {
                    label: 'Current (A)',
                    data: [],
                    borderColor: '#ff6b35',
                    backgroundColor: function (context) {
                        const chart = context.chart;
                        const { ctx: c, chartArea } = chart;
                        if (!chartArea) return 'rgba(255,107,53,0.08)';
                        const gradient = c.createLinearGradient(0, chartArea.top, 0, chartArea.bottom);
                        gradient.addColorStop(0, 'rgba(255,107,53,0.35)');
                        gradient.addColorStop(0.5, 'rgba(255,107,53,0.10)');
                        gradient.addColorStop(1, 'rgba(255,107,53,0.01)');
                        return gradient;
                    },
                    fill: true,
                    tension: 0.35,
                    borderWidth: 3,
                    pointRadius: 0,
                    pointHoverRadius: 5,
                    pointHoverBackgroundColor: '#ff6b35',
                    pointHoverBorderColor: '#fff',
                    pointHoverBorderWidth: 2,
                    shadowOffsetX: 0,
                    shadowOffsetY: 0,
                    shadowBlur: 12,
                    shadowColor: 'rgba(255,107,53,0.6)'
                }
            ]
        },
        options: {
            responsive: true,
            maintainAspectRatio: false,
            animation: {
                duration: 400,
                easing: 'easeOutQuart'
            },
            interaction: {
                mode: 'index',
                intersect: false,
            },
            plugins: {
                legend: {
                    display: false
                },
                tooltip: {
                    enabled: true,
                    backgroundColor: 'rgba(20,20,30,0.92)',
                    borderColor: '#ff6b35',
                    borderWidth: 1,
                    titleColor: '#ff6b35',
                    bodyColor: '#fff',
                    padding: 10,
                    callbacks: {
                        label: function (ctx) {
                            return '  ' + ctx.parsed.y.toFixed(1) + ' A';
                        }
                    }
                }
            },
            scales: {
                x: {
                    type: 'linear',
                    position: 'bottom',
                    title: {
                        display: true,
                        text: 'Time (ms)',
                        color: '#888',
                        font: {
                            size: 14,
                            weight: 'bold'
                        }
                    },
                    ticks: {
                        color: '#888',
                        maxTicksLimit: 12,
                        callback: function (value) {
                            return Number(value).toFixed(2);
                        }
                    },
                    grid: {
                        color: 'rgba(136, 136, 136, 0.1)'
                    }
                },
                y: {
                    type: 'linear',
                    position: 'left',
                    title: {
                        display: true,
                        text: 'Current (A)',
                        color: '#ff6b35',
                        font: {
                            size: 14,
                            weight: 'bold'
                        }
                    },
                    min: 0,
                    max: 2000,
                    ticks: {
                        color: '#ff6b35',
                        stepSize: 250,
                        callback: function (value) {
                            return Number(value).toFixed(0) + ' A';
                        }
                    },
                    grid: {
                        color: 'rgba(255, 107, 53, 0.1)'
                    }
                }
            }
        }
    });

    console.log('Waveform chart initialized successfully');
});

// Live status updates (STATUS + STATUS2 merged)
socket.on('status_update', function (data) {
    // Capacitor voltage
    const vcap = data.vcap !== undefined ? data.vcap : data.vpack;
    if (vcap !== undefined) {
        const el = document.getElementById('vcap');
        if (el) el.textContent = parseFloat(vcap).toFixed(2) + ' V';
    }

    // Pack voltage
    if (data.vpack !== undefined) {
        const el = document.getElementById('vpack');
        if (el) el.textContent = parseFloat(data.vpack).toFixed(2) + ' V';
    }

    // Cell voltages
    ['cell1', 'cell2', 'cell3'].forEach(function (key) {
        if (data[key] !== undefined) {
            const el = document.getElementById(key);
            if (el) el.textContent = parseFloat(data[key]).toFixed(3) + ' V';
        }
    });

    // Temperature
    if (data.temp !== undefined) {
        const el = document.getElementById('temp');
        if (el) el.textContent = parseFloat(data.temp).toFixed(1) + ' °C';
    }

    // Charge current
    if (data.ichg !== undefined) {
        const el = document.getElementById('ichg');
        if (el) el.textContent = parseFloat(data.ichg).toFixed(2) + ' A';
    }

    // Armed / ready state
    if (data.armed !== undefined) {
        const el = document.getElementById('armedStatus');
        if (el) el.textContent = data.armed ? 'ARMED' : 'DISARMED';
    }

    // Weld count
    if (data.weld_count !== undefined) {
        const el = document.getElementById('weldCount');
        if (el) el.textContent = data.weld_count;
    }

    // ESP connection badge
    const connEl = document.getElementById('connectionStatus');
    if (connEl && data.esp_connected !== undefined) {
        connEl.textContent = data.esp_connected ? 'Connected' : 'Disconnected';
        connEl.className = 'connection-status ' + (data.esp_connected ? 'connected' : 'disconnected');
    }
});

// Weld complete — uses fields from Python's weld_payload dict
socket.on('weld_complete', function (data) {
    console.log('Weld complete:', data);

    // Python emits: peak_current_amps, avg_current_amps, duration_ms,
    //               energy_joules (use energy_j from STM32 if present),
    //               vcap_before, vcap_after, voltage_drop

    const peakA = data.peak_current_amps;
    const avgA = data.avg_current_amps;
    const durMs = data.duration_ms;
    // Prefer backend-corrected energy_joules, fallback to raw energy_j if needed
    const energyJ = data.energy_joules !== undefined ? data.energy_joules : data.energy_j;
    const vDrop = data.voltage_drop !== undefined
        ? data.voltage_drop
        : (data.vcap_before - data.vcap_after);

    const set = function (id, val, decimals) {
        const el = document.getElementById(id);
        if (el) el.textContent = val !== undefined && val !== null
            ? parseFloat(val).toFixed(decimals)
            : '--';
    };

    set('peakCurrent', peakA, 1);
    set('avgCurrent', avgA, 1);
    set('energy', energyJ, 2);
    set('duration', durMs, 1);
    set('voltageDrop', vDrop, 3);

    // Vcap before/after/drop row
    const vcapB = data.vcap_before;
    const vcapA = data.vcap_after;
    const vcapDrop = vDrop;

    set('vcapBefore', vcapB, 3);
    set('vcapAfter', vcapA, 3);
    set('vcapDrop', vcapDrop, 3);

    // Animate the drop bar (drop as % of before voltage, capped at 100%)
    if (vcapB !== undefined && vcapB > 0 && vcapDrop !== undefined) {
        const pct = Math.min((vcapDrop / vcapB) * 100 * 8, 100); // ×8 to exaggerate small drops visually
        const bar = document.getElementById('vcapBar');
        if (bar) bar.style.width = pct.toFixed(1) + '%';
    }

    // Tip voltage if forwarded
    if (data.v_tips !== undefined) {
        set('vTips', data.v_tips, 3);
    }

    // Last weld timestamp
    const now = new Date();
    const el = document.getElementById('lastWeldTime');
    if (el) el.textContent = now.toLocaleTimeString();
});

// Weld start flash
socket.on('weld_event', function (data) {
    if (data.active) {
        console.log('Weld started');
        const el = document.getElementById('weldStatus');
        if (el) {
            el.textContent = 'WELDING';
            el.className = 'weld-status welding';
            setTimeout(function () {
                el.textContent = 'IDLE';
                el.className = 'weld-status idle';
            }, 600);
        }
    }
});

// Waveform data — backend emits {samples: [{voltage, current, time_ms}], count: N}
socket.on('waveform_data', function (data) {
    console.log('\n' + '='.repeat(60));
    console.log('WAVEFORM DEBUG - FRONTEND');
    console.log('='.repeat(60));

    const samples = Array.isArray(data?.samples) ? data.samples : [];
    console.log('Received waveform data:', data);
    console.log('Sample count:', samples.length);

    if (!waveformChart) {
        console.error('❌ Chart not initialized!');
        console.log('='.repeat(60) + '\n');
        return;
    }

    if (samples.length > 0) {
        const currents = samples.map(s => Number(s.current)).filter(Number.isFinite);
        const times = samples.map(s => Number(s.time_ms)).filter(Number.isFinite);

        console.log('\nFirst 3 samples:');
        for (let i = 0; i < Math.min(3, samples.length); i++) {
            console.log(`  Sample ${i}:`, samples[i]);
        }

        if (currents.length > 0 && times.length > 0) {
            const minCurrent = Math.min(...currents);
            const maxCurrent = Math.max(...currents);
            const minTime = Math.min(...times);
            const maxTime = Math.max(...times);

            console.log('\nData ranges:');
            console.log('  Time:', minTime.toFixed(2), '-', maxTime.toFixed(2), 'ms');
            console.log('  Current:', minCurrent.toFixed(1), '-', maxCurrent.toFixed(1), 'A');
            console.log('  Variation:', (maxCurrent - minCurrent).toFixed(1), 'A');

            const variation = maxCurrent - minCurrent;
            if (variation < 10) {
                console.warn('⚠️  WARNING: Waveform appears FLAT (variation < 10 A)');
            } else {
                console.log('✅ Waveform has good variation');
            }
        }

        const currentData = samples
            .map(sample => ({
                x: Number(sample.time_ms),
                y: Number(sample.current)
            }))
            .filter(point => Number.isFinite(point.x) && Number.isFinite(point.y));

        console.log('\nChart data points:', currentData.length);
        if (currentData.length > 0) {
            console.log('First chart point:', currentData[0]);
            console.log('Last chart point:', currentData[currentData.length - 1]);

            waveformChart.data.datasets[0].data = currentData;
            waveformChart.update('active');
            console.log('\n✅ Chart updated');
        } else {
            console.error('❌ No valid chart points after parsing!');
        }
    } else {
        console.error('❌ No samples in waveform data!');
    }

    console.log('='.repeat(60) + '\n');
});

// Update waveform chart — current vs time
function updateWaveformChart(samples) {
    if (!samples || samples.length === 0) {
        console.warn('No samples to display');
        return;
    }

    console.log('Updating chart with', samples.length, 'samples');

    const currentData = samples.map(function (sample, i) {
        const timeMs = sample.time_ms !== undefined ? Number(sample.time_ms) : (i * 0.2);
        const currentA = Number(sample.current);
        return { x: timeMs, y: currentA };
    }).filter(function (point) {
        return Number.isFinite(point.x) && Number.isFinite(point.y);
    });

    if (currentData.length === 0) {
        console.warn('No valid waveform points after parsing');
        return;
    }

    const currents = currentData.map(function (d) { return d.y; });
    const minCurrent = Math.min(...currents);
    const maxCurrent = Math.max(...currents);
    console.log('Current range:', minCurrent, 'to', maxCurrent, 'A');

    waveformChart.data.datasets[0].data = currentData;
    waveformChart.update('active');

    console.log('Chart updated');
}