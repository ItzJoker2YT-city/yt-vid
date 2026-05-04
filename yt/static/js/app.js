/**
 * YT-MP3 Downloader — Frontend Application Logic
 * Handles UI interactions, API calls, and real-time download status polling.
 */

// ─── State ───────────────────────────────────────────────────────────────
let pollingInterval = null;
let currentTab = 'download';

// ─── DOM Ready ───────────────────────────────────────────────────────────
document.addEventListener('DOMContentLoaded', () => {
    initTabs();
    initDownloadForm();
    initSearchForm();
    startPolling();
});

// ─── Tab Navigation ──────────────────────────────────────────────────────
function initTabs() {
    document.querySelectorAll('.nav-tab').forEach(tab => {
        tab.addEventListener('click', () => {
            const target = tab.dataset.tab;
            switchTab(target);
        });
    });
}

function switchTab(tabName) {
    currentTab = tabName;

    document.querySelectorAll('.nav-tab').forEach(t => t.classList.remove('active'));
    document.querySelectorAll('.tab-panel').forEach(p => p.classList.remove('active'));

    document.querySelector(`.nav-tab[data-tab="${tabName}"]`)?.classList.add('active');
    document.getElementById(`panel-${tabName}`)?.classList.add('active');

    // Load data for certain tabs
    if (tabName === 'history') loadHistory();
}

// ─── Download Form ───────────────────────────────────────────────────────
function initDownloadForm() {
    const form = document.getElementById('download-form');
    form.addEventListener('submit', async (e) => {
        e.preventDefault();
        await submitDownload();
    });
}

async function submitDownload() {
    const urlsRaw = document.getElementById('url-input').value.trim();
    const quality = document.getElementById('quality-select').value;
    const trimStart = document.getElementById('trim-start').value.trim();
    const trimEnd = document.getElementById('trim-end').value.trim();

    if (!urlsRaw) {
        showToast('Please enter at least one URL', 'error');
        return;
    }

    // Split by newlines or commas
    const urls = urlsRaw.split(/[\n,]+/).map(u => u.trim()).filter(Boolean);

    const submitBtn = document.getElementById('submit-btn');
    submitBtn.disabled = true;
    submitBtn.innerHTML = '<span class="spinner"></span> Starting...';

    try {
        const res = await fetch('/api/download', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
                urls,
                quality,
                trim_start: trimStart || null,
                trim_end: trimEnd || null,
            })
        });

        const data = await res.json();
        if (data.error) {
            showToast(data.error, 'error');
        } else {
            const count = data.tasks?.length || 0;
            showToast(`${count} download${count !== 1 ? 's' : ''} started!`, 'success');
            document.getElementById('url-input').value = '';
            updateQueue(data.tasks);
        }
    } catch (err) {
        showToast('Network error — is the server running?', 'error');
    } finally {
        submitBtn.disabled = false;
        submitBtn.innerHTML = '⬇️ Download MP3';
    }
}

// ─── Download Queue Rendering ────────────────────────────────────────────
function updateQueue(tasks) {
    const container = document.getElementById('download-queue');

    if (!tasks || tasks.length === 0) {
        if (!container.querySelector('.queue-empty')) {
            container.innerHTML = `
                <div class="queue-empty">
                    <div class="empty-icon">📭</div>
                    <p>No active downloads — paste a YouTube URL above to get started.</p>
                </div>`;
        }
        return;
    }

    // Separate playlist tasks from solo tasks
    const groups = {};   // playlist_id -> { title, track_total, tasks[] }
    const solos  = [];   // tasks with no playlist_id

    for (const task of tasks) {
        if (task.playlist_id) {
            if (!groups[task.playlist_id]) {
                groups[task.playlist_id] = {
                    id: task.playlist_id,
                    title: task.playlist_title || 'Playlist',
                    total: task.track_total || '?',
                    tasks: []
                };
            }
            groups[task.playlist_id].tasks.push(task);
        } else {
            solos.push(task);
        }
    }

    // Remove empty state if present
    const emptyState = container.querySelector('.queue-empty');
    if (emptyState) emptyState.remove();

    // 1. Update Solo Tasks
    solos.forEach(task => {
        let el = document.getElementById(`task-${task.id}`);
        if (!el) {
            // Insert at the beginning or before first playlist
            const firstPlaylist = container.querySelector('.playlist-group');
            const newHtml = renderDownloadItem(task);
            if (firstPlaylist) {
                firstPlaylist.insertAdjacentHTML('beforebegin', newHtml);
            } else {
                container.insertAdjacentHTML('beforeend', newHtml);
            }
        } else {
            updateTaskInPlace(el, task);
        }
    });

    // 2. Update Playlists
    Object.entries(groups).forEach(([pid, group]) => {
        // ... (existing logic remains, just update group header ZIP button)
        let groupEl = document.getElementById(`pl-${pid}`);
        const doneCount = group.tasks.filter(t => t.status === 'done').length;
        const allDone = doneCount === group.tasks.length && group.tasks.length > 0;

        if (!groupEl) {
            const groupHtml = `
            <div class="playlist-group" id="pl-${pid}">
                <div class="playlist-header">
                    <span class="playlist-icon">📂</span>
                    <div class="playlist-meta">
                        <div class="playlist-name">${escapeHtml(group.title)}</div>
                        <div class="playlist-stats" id="pl-stats-${pid}">
                            ${doneCount}/${group.tasks.length} done
                        </div>
                    </div>
                    <div class="playlist-actions" id="pl-actions-${pid}">
                        ${allDone ? `<a href="/api/download-playlist/${pid}" class="btn btn-sm btn-success">📦 Download ZIP</a>` : ''}
                        <button class="btn btn-sm btn-secondary" onclick="togglePlaylistGroup('pl-${pid}')" id="pl-toggle-${pid}">
                            ▾ Collapse
                        </button>
                    </div>
                </div>
                <div class="playlist-tracks" id="pl-tracks-${pid}">
                    ${group.tasks.map(t => renderDownloadItem(t)).join('')}
                </div>
            </div>`;
            container.insertAdjacentHTML('beforeend', groupHtml);
        } else {
            const statsEl = document.getElementById(`pl-stats-${pid}`);
            const actionsEl = document.getElementById(`pl-actions-${pid}`);
            if (statsEl) statsEl.innerHTML = `${doneCount}/${group.tasks.length} done`;
            
            if (allDone && actionsEl && !actionsEl.querySelector('.btn-success')) {
                actionsEl.insertAdjacentHTML('afterbegin', `<a href="/api/download-playlist/${pid}" class="btn btn-sm btn-success">📦 Download ZIP</a>`);
            } else if (!allDone && actionsEl) {
                actionsEl.querySelector('.btn-success')?.remove();
            }

            const tracksContainer = document.getElementById(`pl-tracks-${pid}`);
            group.tasks.forEach(task => {
                let el = document.getElementById(`task-${task.id}`);
                if (!el) {
                    tracksContainer.insertAdjacentHTML('beforeend', renderDownloadItem(task));
                } else {
                    updateTaskInPlace(el, task);
                }
            });
        }
    });

    // 3. Update Global Queue Actions
    const totalDone = tasks.filter(t => t.status === 'done').length;
    const actionsContainer = document.getElementById('queue-actions');
    if (totalDone > 1) {
        if (!actionsContainer.querySelector('.btn-success')) {
            actionsContainer.innerHTML = `<a href="/api/download-all" class="btn btn-sm btn-success">📦 Download All ZIP</a>`;
        }
    } else {
        actionsContainer.innerHTML = '';
    }

    // 4. Remove stale tasks
    // ...
    const currentTaskIds = new Set(tasks.map(t => `task-${t.id}`));
    container.querySelectorAll('.download-item').forEach(el => {
        if (!currentTaskIds.has(el.id)) {
            // Don't remove if it's currently being removed animation
            if (!el.classList.contains('removing')) {
                el.remove();
            }
        }
    });

    // Remove empty playlist groups
    const currentPlaylistIds = new Set(Object.keys(groups).map(pid => `pl-${pid}`));
    container.querySelectorAll('.playlist-group').forEach(el => {
        if (!currentPlaylistIds.has(el.id)) el.remove();
    });
}

function updateTaskInPlace(el, task) {
    // Update status badge
    const badge = el.querySelector('.status-badge');
    if (badge) {
        const statusLabel = {
            queued: '⏳ Queued',
            downloading: '⬇️ Downloading',
            converting: '🔄 Converting',
            done: '✅ Done',
            error: '❌ Error',
            paused: '⏸️ Paused',
        }[task.status] || task.status;
        
        if (badge.textContent !== statusLabel) {
            badge.textContent = statusLabel;
            badge.className = `status-badge status-${task.status}`;
        }
    }

    // Update progress bar
    const bar = el.querySelector('.progress-bar-fill');
    if (bar) {
        bar.style.width = `${task.progress}%`;
        if (task.status === 'done') bar.parentElement.style.display = 'none';
    } else if (task.status === 'downloading' || task.status === 'converting') {
        // If it was added later
        const infoEl = el.querySelector('.download-info');
        if (infoEl && !infoEl.querySelector('.progress-bar-container')) {
             infoEl.insertAdjacentHTML('beforeend', `
                <div class="progress-bar-container">
                    <div class="progress-bar-fill" style="width: ${task.progress}%"></div>
                </div>`);
        }
    }

    // Update meta info (speed, eta)
    const metaEl = el.querySelector('.download-meta');
    if (metaEl) {
        // We just refresh this to keep it simple, it's small
        const speedHtml = task.speed ? `<span>🚀 ${task.speed}</span>` : '';
        const etaHtml = task.eta ? `<span>⏱️ ETA ${task.eta}</span>` : '';
        const durHtml = task.duration ? `<span>🕐 ${task.duration}</span>` : '';
        
        let typeHtml = '';
        if (task.dl_type === 'video') {
            typeHtml = `<span>🎥 Video</span>`;
        } else {
            typeHtml = `<span>🎚️ ${task.quality || '192'}kbps</span>`;
        }
        
        // Only update if changed to avoid too many DOM ops
        const currentMeta = metaEl.innerHTML;
        const newMeta = `<span class="status-badge status-${task.status}">${badge ? badge.textContent : ''}</span>${durHtml}${speedHtml}${etaHtml}${typeHtml}`;
        if (currentMeta.replace(/\s/g, '') !== newMeta.replace(/\s/g, '')) {
            metaEl.innerHTML = newMeta;
        }
    }

    // Update actions (the most complex part to diff, so we check status)
    const actionsEl = el.querySelector('.download-actions');
    const currentStatus = el.dataset.status;
    if (currentStatus !== task.status || (task.status === 'done' && !actionsEl.querySelector('.btn-success'))) {
        el.dataset.status = task.status;
        
        let actionsHtml = '';
        const saveLabel = task.dl_type === 'video' ? '💾 Save MP4' : '💾 Save MP3';

        if (task.status === 'downloading') {
            actionsHtml = `
                <button class="btn btn-sm btn-secondary btn-icon" onclick="pauseDownload('${task.id}')" title="Pause">⏸️</button>
                <button class="btn btn-sm btn-danger btn-icon" onclick="cancelDownload('${task.id}')" title="Cancel">✖️</button>`;
        } else if (task.status === 'paused') {
            actionsHtml = `
                <button class="btn btn-sm btn-success btn-icon" onclick="resumeDownload('${task.id}')" title="Resume">▶️</button>
                <button class="btn btn-sm btn-danger btn-icon" onclick="cancelDownload('${task.id}')" title="Cancel">✖️</button>`;
        } else if (task.status === 'done') {
            actionsHtml = `
                ${task.has_file ? `<a href="/api/download-file/${task.id}" class="btn btn-sm btn-success" download="${escapeHtml(task.filename)}">${saveLabel}</a>` : ''}
                <button class="btn btn-sm btn-secondary btn-icon" onclick="removeDownload('${task.id}')" title="Remove">🗑️</button>`;
        } else if (task.status === 'error') {
            actionsHtml = `
                <button class="btn btn-sm btn-secondary btn-icon" onclick="removeDownload('${task.id}')" title="Remove">🗑️</button>`;
        } else if (task.status === 'queued') {
            actionsHtml = `
                <button class="btn btn-sm btn-danger btn-icon" onclick="cancelDownload('${task.id}')" title="Cancel">✖️</button>`;
        }
        actionsEl.innerHTML = actionsHtml;
    }


    // Update filename display if done
    if (task.status === 'done' && task.filename) {
        const fileDisplay = el.querySelector('.file-display');
        if (!fileDisplay) {
            el.querySelector('.download-info').insertAdjacentHTML('beforeend', `<div class="file-display" style="font-size:0.78rem;color:var(--success);margin-top:0.3rem">📁 ${escapeHtml(task.filename)}</div>`);
        }
    }
}

function renderDownloadItem(task) {
    const statusClass = `status-${task.status}`;
    const statusLabel = {
        queued: '⏳ Queued',
        downloading: '⬇️ Downloading',
        converting: '🔄 Converting',
        done: '✅ Done',
        error: '❌ Error',
        paused: '⏸️ Paused',
    }[task.status] || task.status;

    const thumbHtml = task.thumbnail
        ? `<img src="${task.thumbnail}" alt="" loading="lazy">`
        : `<span style="display:flex;align-items:center;justify-content:center;width:100%;height:100%;font-size:1.5rem;">🎵</span>`;

    // Track index badge for playlist items
    const trackBadge = task.track_index
        ? `<span class="track-badge">${task.track_index}/${task.track_total}</span>`
        : '';

    const errorHtml = task.error_message
        ? `<div style="color:var(--danger);font-size:0.78rem;margin-top:0.3rem">${escapeHtml(task.error_message)}</div>`
        : '';

    const saveLabel = task.dl_type === 'video' ? '💾 Save MP4' : '💾 Save MP3';

    // Action buttons based on state
    let actionsHtml = '';
    if (task.status === 'downloading') {
        actionsHtml = `
            <button class="btn btn-sm btn-secondary btn-icon" onclick="pauseDownload('${task.id}')" title="Pause">⏸️</button>
            <button class="btn btn-sm btn-danger btn-icon" onclick="cancelDownload('${task.id}')" title="Cancel">✖️</button>`;
    } else if (task.status === 'paused') {
        actionsHtml = `
            <button class="btn btn-sm btn-success btn-icon" onclick="resumeDownload('${task.id}')" title="Resume">▶️</button>
            <button class="btn btn-sm btn-danger btn-icon" onclick="cancelDownload('${task.id}')" title="Cancel">✖️</button>`;
    } else if (task.status === 'done') {
        actionsHtml = `
            ${task.has_file ? `<a href="/api/download-file/${task.id}" class="btn btn-sm btn-success" download="${escapeHtml(task.filename)}">${saveLabel}</a>` : ''}
            <button class="btn btn-sm btn-secondary btn-icon" onclick="removeDownload('${task.id}')" title="Remove">🗑️</button>`;
    } else if (task.status === 'error') {
        actionsHtml = `
            <button class="btn btn-sm btn-secondary btn-icon" onclick="removeDownload('${task.id}')" title="Remove">🗑️</button>`;
    } else if (task.status === 'queued') {
        actionsHtml = `
            <button class="btn btn-sm btn-danger btn-icon" onclick="cancelDownload('${task.id}')" title="Cancel">✖️</button>`;
    }

    let typeHtml = '';
    if (task.dl_type === 'video') {
        typeHtml = `<span>🎥 Video (${task.quality || 'best'})</span>`;
    } else {
        typeHtml = `<span>🎚️ ${task.quality || '192'}kbps</span>`;
    }

    return `
        <div class="download-item" id="task-${task.id}" data-status="${task.status}">
            <div class="download-thumb">${thumbHtml}</div>
            <div class="download-info">
                <div class="download-title">${trackBadge}${escapeHtml(task.title || task.url)}</div>
                <div class="download-meta">
                    <span class="status-badge ${statusClass}">${statusLabel}</span>
                    ${task.duration ? `<span>🕐 ${task.duration}</span>` : ''}
                    ${task.speed ? `<span>🚀 ${task.speed}</span>` : ''}
                    ${task.eta ? `<span>⏱️ ETA ${task.eta}</span>` : ''}
                    ${typeHtml}
                </div>
                ${(task.status === 'downloading' || task.status === 'converting') ? `
                <div class="progress-bar-container">
                    <div class="progress-bar-fill" style="width: ${task.progress}%"></div>
                </div>` : ''}
                ${errorHtml}
                ${task.status === 'done' && task.filename ? `<div class="file-display" style="font-size:0.78rem;color:var(--success);margin-top:0.3rem">📁 ${escapeHtml(task.filename)}</div>` : ''}
            </div>
            <div class="download-actions">${actionsHtml}</div>
        </div>`;
}

const AUDIO_OPTIONS = `
    <option value="128">128 kbps — Compact</option>
    <option value="192" selected>192 kbps — Balanced</option>
    <option value="320">320 kbps — High Quality</option>
`;

const VIDEO_OPTIONS = `
    <option value="best" selected>Best Quality</option>
    <option value="1080p">1080p Full HD</option>
    <option value="720p">720p HD</option>
    <option value="480p">480p SD</option>
`;

function handleTypeChange() {
    const type = document.getElementById('dl-type').value;
    const qualitySelect = document.getElementById('quality') || document.getElementById('quality-select');
    const qualityLabel = document.getElementById('quality-label');
    
    if (type === 'video') {
        if (qualityLabel) qualityLabel.textContent = 'Video Quality';
        if (qualitySelect) qualitySelect.innerHTML = VIDEO_OPTIONS;
    } else {
        if (qualityLabel) qualityLabel.textContent = 'Audio Quality';
        if (qualitySelect) qualitySelect.innerHTML = AUDIO_OPTIONS;
    }
}

// ─── Download Actions ────────────────────────────────────────────────────
async function pauseDownload(id) {
    await fetch(`/api/pause/${id}`, { method: 'POST' });
}

async function resumeDownload(id) {
    await fetch(`/api/resume/${id}`, { method: 'POST' });
}

async function cancelDownload(id) {
    await fetch(`/api/cancel/${id}`, { method: 'POST' });
}

async function removeDownload(id) {
    await fetch(`/api/remove/${id}`, { method: 'POST' });
    pollStatus(); // Refresh immediately
}

// ─── Polling ─────────────────────────────────────────────────────────────
function startPolling() {
    pollingInterval = setInterval(pollStatus, 1500);
}

async function pollStatus() {
    try {
        const res = await fetch('/api/status');
        const data = await res.json();
        updateQueue(data.tasks || []);
    } catch {
        // Silently ignore polling errors
    }
}

// ─── YouTube Search ──────────────────────────────────────────────────────
function initSearchForm() {
    const input = document.getElementById('search-input');
    const btn = document.getElementById('search-btn');

    btn.addEventListener('click', () => doSearch());
    input.addEventListener('keydown', (e) => {
        if (e.key === 'Enter') doSearch();
    });
}

async function doSearch() {
    const query = document.getElementById('search-input').value.trim();
    if (!query) return;

    const btn = document.getElementById('search-btn');
    const container = document.getElementById('search-results');

    btn.disabled = true;
    btn.innerHTML = '<span class="spinner"></span>';
    container.innerHTML = '<div style="text-align:center;padding:2rem;color:var(--text-muted)"><span class="spinner"></span> Searching YouTube...</div>';

    try {
        const res = await fetch('/api/search', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ query })
        });

        const data = await res.json();
        if (data.error) {
            container.innerHTML = `<div style="text-align:center;padding:2rem;color:var(--danger)">${data.error}</div>`;
        } else {
            renderSearchResults(data.results || []);
        }
    } catch {
        container.innerHTML = '<div style="text-align:center;padding:2rem;color:var(--danger)">Search failed — check your connection.</div>';
    } finally {
        btn.disabled = false;
        btn.innerHTML = '🔍 Search';
    }
}

function renderSearchResults(results) {
    const container = document.getElementById('search-results');

    if (results.length === 0) {
        container.innerHTML = '<div style="text-align:center;padding:2rem;color:var(--text-muted)">No results found.</div>';
        return;
    }

    container.innerHTML = results.map(r => {
        const viewsStr = r.views ? formatNumber(r.views) + ' views' : '';
        return `
        <div class="search-result-card">
            <div class="search-thumb">
                ${r.thumbnail ? `<img src="${r.thumbnail}" alt="" loading="lazy">` : ''}
                <div class="search-duration">${r.duration}</div>
            </div>
            <div class="search-info">
                <div class="title">${escapeHtml(r.title)}</div>
                <div class="channel">${escapeHtml(r.channel)}${viewsStr ? ' · ' + viewsStr : ''}</div>
                <button class="btn btn-primary btn-sm btn-full"
                        onclick="downloadFromSearch('${escapeHtml(r.url)}')">
                    ⬇️ Download MP3
                </button>
            </div>
        </div>`;
    }).join('');
}

function downloadFromSearch(url) {
    // Switch to download tab and prefill the URL
    document.getElementById('url-input').value = url;
    switchTab('download');
    showToast('URL added — click Download to start!', 'info');
}

// ─── History ─────────────────────────────────────────────────────────────
async function loadHistory() {
    const container = document.getElementById('history-list');
    container.innerHTML = '<div style="text-align:center;padding:2rem;color:var(--text-muted)"><span class="spinner"></span> Loading...</div>';

    try {
        const res = await fetch('/api/history');
        const data = await res.json();
        renderHistory(data.history || []);
    } catch {
        container.innerHTML = '<div style="text-align:center;padding:2rem;color:var(--danger)">Failed to load history.</div>';
    }
}

function renderHistory(items) {
    const container = document.getElementById('history-list');

    if (items.length === 0) {
        container.innerHTML = '<div style="text-align:center;padding:2rem;color:var(--text-muted)">No downloads yet.</div>';
        return;
    }

    container.innerHTML = items.map(item => `
        <div class="history-item">
            <div class="history-thumb">🎵</div>
            <div class="history-info">
                <div class="title">${escapeHtml(item.title)}</div>
                <div class="meta">${escapeHtml(item.artist)} · ${item.quality}kbps · ${formatDate(item.downloaded_at)}</div>
            </div>
            <button class="btn btn-sm btn-secondary"
                    onclick="downloadFromSearch('${escapeHtml(item.url)}')">
                🔄 Re-download
            </button>
        </div>`).join('');
}

async function clearHistory() {
    if (!confirm('Clear all download history?')) return;
    await fetch('/api/history/clear', { method: 'POST' });
    loadHistory();
    showToast('History cleared', 'info');
}

// ─── Toast Notifications ─────────────────────────────────────────────────
function showToast(message, type = 'info') {
    const container = document.getElementById('toast-container');
    const id = 'toast-' + Date.now();

    const iconMap = { success: '✅', error: '❌', info: 'ℹ️' };

    const el = document.createElement('div');
    el.className = `toast toast-${type}`;
    el.id = id;
    el.innerHTML = `
        <span class="toast-icon">${iconMap[type] || 'ℹ️'}</span>
        <span class="toast-message">${escapeHtml(message)}</span>
        <button class="toast-close" onclick="dismissToast('${id}')">✕</button>`;

    container.appendChild(el);

    // Auto-dismiss after 4s
    setTimeout(() => dismissToast(id), 4000);
}

function dismissToast(id) {
    const el = document.getElementById(id);
    if (el) {
        el.style.animation = 'toastOut 0.3s var(--ease-out) forwards';
        setTimeout(() => el.remove(), 300);
    }
}

// ─── Utilities ───────────────────────────────────────────────────────────
function escapeHtml(str) {
    if (!str) return '';
    const div = document.createElement('div');
    div.textContent = str;
    return div.innerHTML;
}

function formatNumber(n) {
    if (n >= 1_000_000) return (n / 1_000_000).toFixed(1) + 'M';
    if (n >= 1_000) return (n / 1_000).toFixed(1) + 'K';
    return n.toString();
}

function formatDate(iso) {
    if (!iso) return '';
    try {
        const d = new Date(iso);
        return d.toLocaleDateString(undefined, { month: 'short', day: 'numeric', year: 'numeric' });
    } catch {
        return iso;
    }
}
