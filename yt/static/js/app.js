/**
 * YT-MP3 Downloader — Frontend Application Logic
 * Handles UI interactions, API calls, and real-time download status polling.
 */

// ─── Ghana Artists List ──────────────────────────────────────────────────
const GHANA_ARTISTS = [
    "Black Sherif", "KiDi", "Kuami Eugene", "Stonebwoy", "Sarkodie",
    "King Promise", "Shatta Wale", "Gyakie", "Kelvyn Boy", "Lasmid",
    "Kwesi Arthur", "Darkovibes", "Medikal", "Fameye", "Mr Drew",
    "Bosom P-Yung", "Yaw Tog", "Jay Bahd", "O'Kenneth", "Kwaku DMC",
    "City Boy", "Skyface SDW", "Beeztrap KOTM", "Camidoh", "Amerado",
    "Kofi Kinaata", "Wendy Shay", "Eno Barony", "Efya", "Sefa",
    "Adina", "MzVee", "Diana Hamilton", "Celestine Donkor", "Piesie Esther",
    "Joey B", "Okese1", "Tulenkey", "Quamina MP", "Deon Boakye",
    "Okyeame Kwame", "Bisa Kdei", "Kwabena Kwabena", "Nacee", "Samini",
    "Edem", "Worlasi", "M.anifest", "Mugeez", "R2Bees",
    "Pappy Kojo", "Dead Peepol", "Rich Kent", "Malcolm Nuna", "Reggie",
    "Rigiid", "Oseikrom Sikanii", "Kojo Blak", "Juls", "Smallgod",
    "Teephlow", "Yaw Berk", "Kweku Flick", "Lil Win", "Brella",
    "Cina Soul", "Abiana", "Kweku Darlington", "B4Bonah", "AratheJay",
    "Minz", "Killbeatz", "Guilty Beatz", "MOG Music", "Empress Gifty",
    "Joe Mettle", "Ohemaa Mercy", "Nathaniel Bassey", "Kofi Nti", "Akwaboah",
    "Ko-Jo Cue", "Lyrical Joe", "Kwame Yogot", "Ypee", "Lil Melody",
    "Freda Rhymz", "Strongman", "A.I (Akan)", "Tic Tac", "Lord Kenya",
    "Shaker", "Flowking Stone", "Bra Eddie", "King Paluta", "Joeboy",
    "DarkoVibes", "Efia Odo", "Nana Ama McBrown", "Kofi Kinata", "Kwame Yogot"
];

// ─── Trending Artists (pinned at top of grid) ────────────────────────────
const TRENDING_ARTISTS = [
    "Black Sherif", "Sarkodie", "Stonebwoy", "KiDi", "King Promise",
    "Shatta Wale", "Kuami Eugene", "Gyakie", "Medikal", "Kelvyn Boy"
];

// ─── State ───────────────────────────────────────────────────────────────
let pollingInterval = null;
let currentTab = 'download';
let activeArtist = null;
let downloadedIds = new Set();       // video IDs already in history
let downloadedUrls = new Set();      // video URLs already in history
let artistDownloadCounts = {};       // artist name -> count from history
let _searchAbortCtrl  = null;        // AbortController for doSearch
let _albumsAbortCtrl  = null;        // AbortController for fetchArtistAlbums

// ─── DOM Ready ───────────────────────────────────────────────────────────
document.addEventListener('DOMContentLoaded', () => {
    initTabs();
    initDownloadForm();
    initSearchForm();
    initArtistBrowser();
    loadHistoryIds();   // pre-load so badges are ready
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

    if (tabName === 'history') loadHistory();
    if (tabName === 'search') loadHistoryIds(); // refresh badges
    if (tabName === 'ghana-music') loadGhanaMusic();
}

// ─── History IDs (for Already-Downloaded badges) ──────────────────────────
async function loadHistoryIds() {
    try {
        const res = await fetch('/api/history-ids');
        const data = await res.json();
        downloadedIds = new Set(data.ids || []);
        downloadedUrls = new Set(data.urls || []);

        // Count per artist from full history for pill badges
        const hRes = await fetch('/api/history');
        const hData = await hRes.json();
        artistDownloadCounts = {};
        for (const item of (hData.history || [])) {
            const title = (item.title || '').toLowerCase();
            for (const artist of GHANA_ARTISTS) {
                if (title.includes(artist.toLowerCase())) {
                    artistDownloadCounts[artist] = (artistDownloadCounts[artist] || 0) + 1;
                }
            }
        }
        // Refresh pill badges if grid already rendered
        refreshArtistBadges();
    } catch { /* silently ignore */ }
}

function refreshArtistBadges() {
    document.querySelectorAll('.artist-pill').forEach(pill => {
        const name = pill.dataset.artist;
        const count = artistDownloadCounts[name] || 0;
        const badgeEl = pill.querySelector('.dl-count');
        if (count > 0) {
            if (badgeEl) {
                badgeEl.textContent = count;
            } else {
                pill.insertAdjacentHTML('beforeend',
                    `<span class="dl-count">${count}</span>`);
            }
        } else if (badgeEl) {
            badgeEl.remove();
        }
    });
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
    const qualityEl = document.getElementById('quality') || document.getElementById('quality-select');
    const quality = qualityEl ? qualityEl.value : '192';
    const trimStart = document.getElementById('trim-start').value.trim();
    const trimEnd = document.getElementById('trim-end').value.trim();
    const dlTypeEl = document.getElementById('dl-type');
    const dl_type = dlTypeEl ? dlTypeEl.value : 'audio';

    if (!urlsRaw) {
        showToast('Please enter at least one URL', 'error');
        return;
    }

    // Split by newlines or commas
    const urls = urlsRaw.split(/[\n,]+/).map(u => u.trim()).filter(Boolean);

    // If more than 5 URLs or looks like a playlist, show a quick confirmation if video
    if (dl_type === 'video' && urls.length > 5) {
        if (!confirm(`You are about to queue ${urls.length} video downloads. This may use a lot of disk space. Continue?`)) return;
    }

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
                dl_type
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
        submitBtn.innerHTML = dl_type === 'video' ? '⬇️ Download MP4' : '⬇️ Download MP3';
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
    const submitBtn = document.getElementById('submit-btn');
    
    if (type === 'video') {
        if (qualityLabel) qualityLabel.textContent = 'Video Quality';
        if (qualitySelect) qualitySelect.innerHTML = VIDEO_OPTIONS;
        if (submitBtn) submitBtn.innerHTML = '⬇️ Download MP4';
    } else {
        if (qualityLabel) qualityLabel.textContent = 'Audio Quality';
        if (qualitySelect) qualitySelect.innerHTML = AUDIO_OPTIONS;
        if (submitBtn) submitBtn.innerHTML = '⬇️ Download MP3';
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
let _searchDebounce = null;
function initSearchForm() {
    const input = document.getElementById('search-input');
    const btn   = document.getElementById('search-btn');

    btn.addEventListener('click', () => doSearch());

    // Enter key fires immediately; typing debounces 500ms
    input.addEventListener('keydown', (e) => {
        if (e.key === 'Enter') { clearTimeout(_searchDebounce); doSearch(); }
    });
    input.addEventListener('input', () => {
        clearTimeout(_searchDebounce);
        _searchDebounce = setTimeout(() => {
            if (input.value.trim().length > 2) doSearch();
        }, 500);
    });
}


// ─── Ghana Artist Browser ────────────────────────────────────────────────
function initArtistBrowser() {
    const grid = document.getElementById('artist-grid');
    const filterInput = document.getElementById('artist-filter');
    if (!grid) return;

    const unique = [...new Set(GHANA_ARTISTS)];

    // Trending strip — NO string literals in onclick, uses data-artist attribute
    const trendingHtml = `
        <div class="trending-row" id="trending-row">
            <span class="trending-label">🔥 Trending</span>
            ${TRENDING_ARTISTS.map(name => `
                <button class="artist-pill trending-pill"
                        data-artist="${escapeHtml(name)}"
                        onclick="searchArtist(this.dataset.artist, this)">
                    <span>${escapeHtml(name)}</span>
                </button>`).join('')}
        </div>
        <div class="artist-divider">🎵 All Artists</div>`;

    // Full list — safe onclick via data attribute
    const allHtml = unique.map((name, i) => `
        <button class="artist-pill"
                data-artist="${escapeHtml(name)}"
                id="pill-${i}"
                onclick="searchArtist(this.dataset.artist, this)"
                title="Search latest songs by ${escapeHtml(name)}">
            <span class="artist-num">${i + 1}</span>
            <span>${escapeHtml(name)}</span>
        </button>`).join('');

    grid.innerHTML = trendingHtml + allHtml;

    // Filter: hide trending row while typing
    filterInput.addEventListener('input', () => {
        const q = filterInput.value.toLowerCase().trim();
        const trendRow = document.getElementById('trending-row');
        if (trendRow) trendRow.style.display = q ? 'none' : '';
        grid.querySelectorAll('.artist-divider').forEach(d => d.style.display = q ? 'none' : '');
        grid.querySelectorAll('.artist-pill:not(.trending-pill)').forEach(pill => {
            pill.classList.toggle('hidden', q.length > 0 && !pill.dataset.artist.toLowerCase().includes(q));
        });
    });

    refreshArtistBadges();
}


function searchArtist(name, pillEl) {
    try {
        // Highlight active pill
        document.querySelectorAll('.artist-pill').forEach(p => p.classList.remove('active'));
        if (pillEl) pillEl.classList.add('active');
        activeArtist = name;

        const year = new Date().getFullYear();
        const latestOnly = document.getElementById('latest-toggle')?.checked;
        const query = latestOnly ? `${name} ${year} latest song` : `${name} songs`;

        const searchInput = document.getElementById('search-input');
        if (searchInput) searchInput.value = query;

        if (currentTab !== 'search') switchTab('search');

        // Show album card loading state — null-guard every element individually
        const albumsCard     = document.getElementById('albums-card');
        const albumsGrid     = document.getElementById('albums-grid');
        const albumsArtist   = document.getElementById('albums-artist-name');
        const albumsStatus   = document.getElementById('albums-status');

        if (albumsCard)   albumsCard.style.display = '';
        if (albumsGrid)   albumsGrid.innerHTML = '<div style="text-align:center;padding:1.5rem;color:var(--text-muted)"><span class="spinner"></span> Loading albums...</div>';
        if (albumsArtist) albumsArtist.textContent = `· ${name}`;
        if (albumsStatus) albumsStatus.textContent = '';

        // Fire songs search + album fetch in parallel
        Promise.all([
            doSearch(query, name),
            fetchArtistAlbums(name)
        ]);

    } catch (err) {
        console.error('[searchArtist] error:', err);
        showToast('Could not start search — see console for details.', 'error');
    }
}

async function fetchArtistAlbums(artist) {
    const albumsCard = document.getElementById('albums-card');
    const albumsGrid = document.getElementById('albums-grid');
    const albumsStatus = document.getElementById('albums-status');

    // Cancel any previous album fetch — the latest artist click wins
    if (_albumsAbortCtrl) _albumsAbortCtrl.abort();
    _albumsAbortCtrl = new AbortController();
    const { signal } = _albumsAbortCtrl;

    try {
        const res = await fetch('/api/artist-albums', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ artist }),
            signal
        });
        if (signal.aborted) return;

        const data = await res.json();
        const albums = data.albums || [];

        if (albums.length === 0) {
            if (albumsCard) albumsCard.style.display = 'none';
            return;
        }

        if (albumsStatus) albumsStatus.textContent = `${albums.length} found`;
        renderAlbums(albums);
    } catch (err) {
        if (err.name === 'AbortError') return;  // user switched artist — discard
        if (albumsCard) albumsCard.style.display = 'none';
    }
}

function renderAlbums(albums) {
    const grid = document.getElementById('albums-grid');
    if (!grid) return;

    grid.innerHTML = albums.map(a => {
        const trackInfo = a.track_count ? `${a.track_count} tracks` : (a.duration || '');
        return `
        <div class="album-card">
            <div class="album-thumb">
                <img src="${escapeHtml(a.thumbnail)}" alt="" loading="lazy" onerror="this.style.display='none'">
                <div class="album-type-badge">💿 Album</div>
            </div>
            <div class="album-info">
                <div class="album-title" title="${escapeHtml(a.title)}">${escapeHtml(a.title)}</div>
                <div class="album-meta">${escapeHtml(a.channel)}${trackInfo ? ' · ' + trackInfo : ''}</div>
                <div class="album-actions">
                    <button class="btn btn-sm btn-secondary"
                            onclick="window.open('${escapeHtml(a.url)}','_blank')">
                        👁️ View
                    </button>
                    <button class="btn btn-sm btn-primary"
                            onclick="downloadAlbum('${escapeHtml(a.url)}', this)">
                        ⬇️ Download All
                    </button>
                </div>
            </div>
        </div>`;
    }).join('');
}


async function downloadAlbum(url, btnEl) {
    if (btnEl) { btnEl.disabled = true; btnEl.textContent = '⏳ Queuing...'; }

    try {
        const res = await fetch('/api/download', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ urls: [url], quality: '320', dl_type: 'audio' })
        });
        const data = await res.json();
        if (data.error) {
            showToast(data.error, 'error');
            if (btnEl) { btnEl.disabled = false; btnEl.textContent = '⬇️ Download All'; }
        } else {
            const count = (data.tasks || []).length;
            showToast(`✅ Queued ${count} track${count !== 1 ? 's' : ''} at 320kbps!`, 'success');
            if (btnEl) { btnEl.textContent = `✅ Queued ${count}`; }
            // Switch to download tab so user can watch progress
            setTimeout(() => switchTab('download'), 1500);
        }
    } catch {
        showToast('Network error — is the server running?', 'error');
        if (btnEl) { btnEl.disabled = false; btnEl.textContent = '⬇️ Download All'; }
    }
}



async function doSearch(queryOverride, artistLabel) {
    const rawQuery = queryOverride || document.getElementById('search-input')?.value.trim();
    if (!rawQuery) return;

    // Cancel any previous in-flight search — clicking a new artist always wins
    if (_searchAbortCtrl) _searchAbortCtrl.abort();
    _searchAbortCtrl = new AbortController();
    const { signal } = _searchAbortCtrl;

    const btn = document.getElementById('search-btn');
    const container = document.getElementById('search-results');
    if (!btn || !container) return;

    btn.disabled = true;
    btn.innerHTML = '<span class="spinner"></span>';
    container.innerHTML = '<div style="text-align:center;padding:2rem;color:var(--text-muted)"><span class="spinner"></span> Searching YouTube...</div>';

    try {
        const res = await fetch('/api/search', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ query: rawQuery }),
            signal
        });
        if (signal.aborted) return;

        const data = await res.json();
        if (data.error) {
            container.innerHTML = `<div style="text-align:center;padding:2rem;color:var(--danger)">${data.error}</div>`;
        } else {
            renderSearchResults(data.results || [], artistLabel || null);
        }
    } catch (err) {
        if (err.name === 'AbortError') return;  // user switched artist — silently discard
        container.innerHTML = '<div style="text-align:center;padding:2rem;color:var(--danger)">Search failed — check your connection.</div>';
    } finally {
        if (!signal.aborted) {
            btn.disabled = false;
            btn.innerHTML = '🔍 Search';
        }
    }
}

function renderSearchResults(results, artistLabel) {
    const container = document.getElementById('search-results');

    if (results.length === 0) {
        container.innerHTML = '<div style="text-align:center;padding:2rem;color:var(--text-muted)">No results found.</div>';
        return;
    }

    const heading = artistLabel
        ? `<div class="artist-results-heading">
               🎵 Latest songs &nbsp;·&nbsp;
               <span class="artist-badge">${escapeHtml(artistLabel)}</span>
               <span style="margin-left:auto;font-size:0.76rem;color:var(--text-muted)">${results.length} results</span>
           </div>`
        : '';

    container.innerHTML = heading + results.map(r => {
        const viewsStr = r.views ? formatNumber(r.views) + ' views' : '';
        // Extract video ID from URL for history check
        const vidId = (r.url.match(/[?&]v=([^&]+)/) || [])[1] || r.id || '';
        const alreadyDl = downloadedIds.has(vidId) || downloadedUrls.has(r.url);
        const dlBadge = alreadyDl
            ? `<span class="already-dl-badge">✅ Already Downloaded</span>`
            : '';
        return `
        <div class="search-result-card">
            <div class="search-thumb">
                ${r.thumbnail ? `<img src="${r.thumbnail}" alt="" loading="lazy">` : ''}
                <div class="search-duration">${r.duration}</div>
                ${alreadyDl ? '<div class="already-dl-overlay">✅</div>' : ''}
            </div>
            <div class="search-info">
                <div class="title">${escapeHtml(r.title)}</div>
                <div class="channel">${escapeHtml(r.channel)}${viewsStr ? ' · ' + viewsStr : ''}</div>
                ${dlBadge}
                <div class="dl-row">
                    <select class="fmt-select" id="fmt-${escapeHtml(vidId || r.url)}">
                        <option value="audio">🎵 MP3 320kbps</option>
                        <option value="video">🎥 MP4 HD</option>
                    </select>
                    <button class="btn btn-primary btn-sm" style="flex:1"
                            onclick="downloadFromSearch('${escapeHtml(r.url)}', '${escapeHtml(vidId || r.url)}')"
                    >⬇️ Download</button>
                </div>
            </div>
        </div>`;
    }).join('');
}

async function downloadFromSearch(url, fmtKey) {
    // Determine format from the select in this card
    const sel = document.getElementById(`fmt-${fmtKey}`);
    const dlType = sel ? sel.value : 'audio';
    const quality = dlType === 'video' ? 'best' : '320'; // always highest

    try {
        const res = await fetch('/api/download', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ urls: [url], quality, dl_type: dlType })
        });
        const data = await res.json();
        if (data.error) {
            showToast(data.error, 'error');
        } else {
            const label = dlType === 'video' ? 'MP4' : 'MP3 (320kbps)';
            showToast(`⬇️ Queued ${label} download!`, 'success');
            // Refresh history IDs so badge appears
            loadHistoryIds();
        }
    } catch {
        showToast('Network error — is the server running?', 'error');
    }
}


// ─── Playlist Group Toggle ───────────────────────────────────────────────
function togglePlaylistGroup(groupId) {
    const group = document.getElementById(groupId);
    if (!group) return;
    const tracks = group.querySelector('.playlist-tracks');
    const toggleBtn = document.getElementById(`pl-toggle-${groupId.replace('pl-', '')}`);
    if (!tracks) return;

    const isCollapsed = tracks.style.display === 'none';
    tracks.style.display = isCollapsed ? '' : 'none';
    if (toggleBtn) toggleBtn.textContent = isCollapsed ? '▾ Collapse' : '▸ Expand';
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

// ─── Ghana Music (Halmblog.com) ──────────────────────────────────────────
let _ghanaSongs = [];
let _ghanaSelected = new Set();
let _ghanaLoading = false;
let currentGhanaPage = 1;
let totalGhanaPages = 1;
let _totalGhanaSongs = 0;
let _ghanaSearchQuery = "";
let _ghanaSearchTotal = 0;
let _ghanaRefreshTimer = null;    // auto-refresh timer when on Ghana Music tab
let _lastGhanaSongCount = 0;       // detect new additions
let _lastGhanaTopUrl = "";         // compare first song to detect new posts

// ─── Auto-refresh Ghana Music every 30s when on the tab ─────────────────
function startGhanaAutoRefresh() {
    stopGhanaAutoRefresh();
    // Only refresh in browse mode (no search query) on page 1
    if (_ghanaSearchQuery || currentGhanaPage !== 1) return;
    _ghanaRefreshTimer = setInterval(async () => {
        try {
            const res = await fetch('/api/ghana-music?page=1&limit=20');
            const data = await res.json();
            const songs = data.songs || [];
            if (songs.length > 0) {
                const topUrl = songs[0].page_url;
                // If the first song changed, something new was posted!
                if (topUrl !== _lastGhanaTopUrl && _lastGhanaTopUrl !== "") {
                    // Flash the status line
                    const statusEl = document.getElementById('ghana-cache-status');
                    if (statusEl) {
                        statusEl.innerHTML = `<span style="color:var(--success)">🔥 New song detected! Refreshing...</span>`;
                    }
                    // Re-render
                    _ghanaSongs = songs;
                    renderGhanaMusic(songs, true); // true = show "NEW!" flash
                }
                _lastGhanaTopUrl = topUrl;
            }
        } catch { /* ignore polling errors */ }
    }, 30000); // every 30 seconds
}
function stopGhanaAutoRefresh() {
    if (_ghanaRefreshTimer) { clearInterval(_ghanaRefreshTimer); _ghanaRefreshTimer = null; }
}

// Restart auto-refresh on tab switch
const _origSwitchTab = switchTab;
switchTab = function(tabName) {
    _origSwitchTab(tabName);
    if (tabName === 'ghana-music') {
        startGhanaAutoRefresh();
    } else {
        stopGhanaAutoRefresh();
    }
};

async function loadGhanaMusic() {
    _ghanaSearchQuery = "";
    document.getElementById('ghana-search-input').value = '';
    await loadGhanaMusicPage(1);
}

async function loadGhanaMusicPage(page) {
    if (page < 1) page = 1;
    const grid = document.getElementById('ghana-music-grid');
    if (!grid) return;
    if (_ghanaLoading) return;
    _ghanaLoading = true;

    currentGhanaPage = page;
    const btn = document.getElementById('ghana-refresh-btn');
    if (btn) btn.disabled = true;

    // Show Super Search loading state when searching
    if (grid && _ghanaSearchQuery && page === 1) {
        grid.innerHTML = '<div style="text-align:center;padding:2rem;color:var(--ghana-green)"><span class="spinner"></span> ⚡ Super Search: 80 workers hunting for "' + escapeHtml(_ghanaSearchQuery) + '"...</div>';
    }
    const prevBtn = document.getElementById('ghana-page-prev');
    const nextBtn = document.getElementById('ghana-page-next');
    if (prevBtn) prevBtn.disabled = true;
    if (nextBtn) nextBtn.disabled = true;

    try {
        if (_ghanaSearchQuery) {
            // ─── Search mode ───
            const res = await fetch(`/api/ghana-music/search?page=${page}&limit=20`, {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ query: _ghanaSearchQuery })
            });
            const data = await res.json();
            if (data.error) {
                grid.innerHTML = `<div style="text-align:center;padding:2rem;color:var(--danger)">${escapeHtml(data.error)}</div>`;
                _ghanaLoading = false;
                if (btn) btn.disabled = false;
                return;
            }
            _ghanaSongs = data.songs || [];
            _ghanaSearchTotal = data.total_results || 0;
            totalGhanaPages = Math.max(1, Math.ceil(_ghanaSearchTotal / 20));
            renderGhanaMusic(_ghanaSongs);
        } else {
            // ─── Browse mode ───
            const res = await fetch(`/api/ghana-music?page=${page}&limit=20`);
            const data = await res.json();
            if (data.error) {
                grid.innerHTML = `<div style="text-align:center;padding:2rem;color:var(--danger)">${escapeHtml(data.error)}</div>`;
                _ghanaLoading = false;
                if (btn) btn.disabled = false;
                return;
            }
            _ghanaSongs = data.songs || [];
            renderGhanaMusic(_ghanaSongs);
            const infoRes = await fetch('/api/ghana-music/info');
            const info = await infoRes.json();
            if (!info.error) {
                totalGhanaPages = info.total_pages;
                _totalGhanaSongs = info.total_songs;
            }
        }
        updateGhanaPagination();
        updateGhanaCacheStatus();
        // Remember the top song for auto-refresh comparison
        if (_ghanaSongs.length > 0 && _lastGhanaTopUrl === "") {
            _lastGhanaTopUrl = _ghanaSongs[0].page_url;
        }
    } catch (e) {
        if (!grid.querySelector('.spinner')) {
            grid.innerHTML = '<div style="text-align:center;padding:2rem;color:var(--danger)">Failed to load Ghana music. Check your connection.</div>';
        }
    } finally {
        _ghanaLoading = false;
        if (btn) btn.disabled = false;
    }
}

function updateGhanaPagination() {
    const prevBtn = document.getElementById('ghana-page-prev');
    const nextBtn = document.getElementById('ghana-page-next');
    const info = document.getElementById('ghana-page-info');
    if (info) {
        if (_ghanaSearchQuery) {
            info.textContent = `🔍 "${escapeHtml(_ghanaSearchQuery)}" — Page ${currentGhanaPage} / ${totalGhanaPages}`;
        } else {
            info.textContent = `Page ${currentGhanaPage} / ${totalGhanaPages}`;
        }
    }
    if (prevBtn) { prevBtn.disabled = currentGhanaPage <= 1; }
    if (nextBtn) { nextBtn.disabled = currentGhanaPage >= totalGhanaPages; }
}

function updateGhanaCacheStatus() {
    const el = document.getElementById('ghana-cache-status');
    if (!el) return;
    if (_ghanaSearchQuery) {
            el.innerHTML = `⚡ Super Search: ${_ghanaSearchTotal} results for "${escapeHtml(_ghanaSearchQuery)}" <span style="color:var(--ghana-green);">(80 workers scanned)</span>`;
        } else {
            el.innerHTML = `🇬🇭 ${_totalGhanaSongs} songs cached — <span style="color:var(--ghana-green);">auto-updating live</span> <span class="live-dot"></span>`;
        }
}

async function triggerDeepCache() {
    const btn = document.getElementById('ghana-deep-btn');
    if (btn) { btn.disabled = true; btn.textContent = '⏳ Building deep cache...'; }
    showToast('🔄 Deep cache building in background (up to 100 pages)...', 'info');
    try {
        await fetch('/api/ghana-music/deep-cache', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ max_pages: 100 })
        });
    } catch { /* start is enough */ }
}

function renderGhanaMusic(songs) {
    const grid = document.getElementById('ghana-music-grid');
    if (!grid) return;

    if (songs.length === 0) {
        grid.innerHTML = '<div style="text-align:center;padding:2rem;color:var(--text-muted)">No songs found.</div>';
        return;
    }

    grid.innerHTML = songs.map((s, idx) => {
        const hasMp3 = Boolean(s.mp3_url);
        const selected = _ghanaSelected.has(s.page_url);

        return `
        <div class="ghana-song-card ${selected ? 'selected' : ''}" data-idx="${idx}" data-url="${escapeHtml(s.page_url)}">
            <div class="ghana-thumb">
                ${s.thumbnail ? `<img src="${escapeHtml(s.thumbnail)}" alt="" loading="lazy" onerror="this.style.display='none'">` : ''}
            </div>
            <div class="ghana-info">
                <div class="ghana-artist">${s.artist ? escapeHtml(s.artist) : ''}</div>
                <div class="ghana-title">${escapeHtml(s.title)}</div>
                <div class="ghana-meta">${escapeHtml(s.date)}</div>
                <div class="ghana-actions">
                    <label class="ghana-check">
                        <input type="checkbox" ${selected ? 'checked' : ''}
                               onchange="toggleGhanaSelect('${escapeHtml(s.page_url)}')">
                        <span>Select</span>
                    </label>
                    ${hasMp3
                        ? `<button class="btn btn-sm btn-primary" onclick="downloadGhanaSong('${escapeHtml(s.mp3_url)}',
                            '${escapeHtml(s.title)}',
                            '${escapeHtml(s.artist || '')}',
                            '${escapeHtml(s.thumbnail)}', this)">⬇️ Download</button>`
                        : `<button class="btn btn-sm btn-primary" onclick="fetchGhanaMp3('${escapeHtml(s.page_url)}',
                            '${escapeHtml(s.title)}',
                            '${escapeHtml(s.artist || '')}',
                            '${escapeHtml(s.thumbnail)}', this)">
                            🔍 Fetch MP3
                           </button>`
                    }
                    <button class="btn btn-sm btn-secondary" onclick="window.open('${escapeHtml(s.page_url)}','_blank')">👁️ View</button>
                </div>
            </div>
        </div>`;
    }).join('');

    updateGhanaSelectCount();
}

function toggleGhanaSelect(pageUrl) {
    if (_ghanaSelected.has(pageUrl)) {
        _ghanaSelected.delete(pageUrl);
    } else {
        _ghanaSelected.add(pageUrl);
    }

    // Update visual state
    document.querySelectorAll('.ghana-song-card').forEach(card => {
        if (card.dataset.url === pageUrl) {
            card.classList.toggle('selected', _ghanaSelected.has(pageUrl));
            const cb = card.querySelector('input[type="checkbox"]');
            if (cb) cb.checked = _ghanaSelected.has(pageUrl);
        }
    });

    updateGhanaSelectCount();
}

function toggleSelectAllGhana(select) {
    if (select) {
        _ghanaSongs.forEach(s => _ghanaSelected.add(s.page_url));
    } else {
        _ghanaSelected.clear();
    }
    renderGhanaMusic(_ghanaSongs);
}

function filterGhanaSongs(query) {
    const q = query.toLowerCase().trim();
    if (!q) {
        renderGhanaMusic(_ghanaSongs);
        return;
    }
    const filtered = _ghanaSongs.filter(s =>
        (s.title || '').toLowerCase().includes(q) ||
        (s.artist || '').toLowerCase().includes(q)
    );
    renderGhanaMusic(filtered);
}

async function doGhanaSearch() {
    const input = document.getElementById('ghana-search-input');
    const btn = document.getElementById('ghana-search-btn');
    const query = input?.value.trim() || '';
    if (!query) {
        loadGhanaMusic();
        return;
    }
    if (btn) { btn.disabled = true; btn.innerHTML = '<span class="spinner"></span>'; }
    _ghanaSearchQuery = query;
    currentGhanaPage = 1;
    await loadGhanaMusicPage(1);
    if (btn) { btn.disabled = false; btn.innerHTML = '⚡ Super Search'; }
}

function updateGhanaSelectCount() {
    const countEl = document.getElementById('ghana-selected-count');
    const btn = document.getElementById('ghana-download-selected-btn');
    const count = _ghanaSelected.size;
    if (countEl) countEl.textContent = count;
    if (btn) btn.disabled = count === 0;
}

async function downloadGhanaSong(mp3Url, title, artist, thumbnail, btnEl) {
    if (!mp3Url) return;
    if (btnEl) { btnEl.disabled = true; btnEl.textContent = '⏳ Queuing...'; }

    try {
        const res = await fetch('/api/ghana-music/download', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ mp3_url: mp3Url, title, artist, thumbnail, quality: '320', dl_type: 'audio' })
        });
        const data = await res.json();
        if (data.error) {
            showToast(data.error, 'error');
            if (btnEl) { btnEl.disabled = false; btnEl.textContent = '⬇️ Download'; }
        } else {
            showToast('⬇️ Queued halmblog MP3 (320kbps)!', 'success');
            if (btnEl) { btnEl.textContent = '✅ Queued'; }
            loadHistoryIds();
            setTimeout(() => switchTab('download'), 800);
        }
    } catch {
        showToast('Network error — is the server running?', 'error');
        if (btnEl) { btnEl.disabled = false; btnEl.textContent = '⬇️ Download'; }
    }
}

async function fetchGhanaMp3(pageUrl, title, artist, thumbnail, btnEl) {
    if (!pageUrl) return;
    if (btnEl) { btnEl.disabled = true; btnEl.textContent = '⏳ Fetching...'; }

    try {
        const res = await fetch('/api/ghana-music/detail', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ url: pageUrl })
        });
        const data = await res.json();
        if (data.error) {
            showToast(data.error, 'error');
            if (btnEl) { btnEl.disabled = false; btnEl.textContent = '🔍 Fetch MP3'; }
            return;
        }
        if (!data.mp3_url) {
            showToast('No MP3 link found on this page. Try visiting the page manually.', 'error');
            if (btnEl) { btnEl.disabled = false; btnEl.textContent = '🔍 Fetch MP3'; }
            return;
        }
        // Update the song in _ghanaSongs so future renders show the Download button
        const song = _ghanaSongs.find(s => s.page_url === pageUrl);
        if (song) song.mp3_url = data.mp3_url;
        // Re-render with the new MP3
        renderGhanaMusic(_ghanaSongs);
        // Auto-download
        downloadGhanaSong(data.mp3_url, data.title || title, data.artist || artist, data.thumbnail || thumbnail, null);
        showToast('MP3 found! Queuing download...', 'success');
    } catch (e) {
        showToast('Failed to fetch MP3 link', 'error');
        if (btnEl) { btnEl.disabled = false; btnEl.textContent = '🔍 Fetch MP3'; }
    }
}

// ─── Batch MP3 fetcher (for selected songs without MP3s) ────────────────────
async function _batchFetchMp3s(songsToFetch, maxConc = 4) {
    const out = [];
    let i = 0;
    async function worker() {
        while (i < songsToFetch.length) {
            const song = songsToFetch[i++];
            try {
                const res = await fetch('/api/ghana-music/detail', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ url: song.page_url })
                });
                const data = await res.json();
                if (data.mp3_url) {
                    song.mp3_url = data.mp3_url;
                    song.title = data.title || song.title;
                    song.artist = data.artist || song.artist;
                    song.thumbnail = data.thumbnail || song.thumbnail;
                    out.push(song);
                }
            } catch { /* ignore individual failures */ }
        }
    }
    const workers = Array.from({ length: maxConc }, () => worker());
    await Promise.all(workers);
    return out;
}

async function downloadSelectedGhana() {
    if (_ghanaSelected.size === 0) return;

    const songsWithMp3 = [];
    const songsMissingMp3 = [];
    for (const pageUrl of _ghanaSelected) {
        const song = _ghanaSongs.find(s => s.page_url === pageUrl);
        if (song) {
            if (song.mp3_url) songsWithMp3.push(song);
            else songsMissingMp3.push(song);
        }
    }

    const btn = document.getElementById('ghana-download-selected-btn');
    if (btn) { btn.disabled = true; btn.innerHTML = '<span class="spinner"></span> Starting...'; }

    // If some selected songs are missing MP3 links, batch-fetch them first
    if (songsMissingMp3.length > 0) {
        showToast(`Fetching MP3 links for ${songsMissingMp3.length} song${songsMissingMp3.length !== 1 ? 's' : ''}...`, 'info');
        const fetched = await _batchFetchMp3s(songsMissingMp3, 4);
        songsWithMp3.push(...fetched);
        renderGhanaMusic(_ghanaSongs); // update UI with new MP3s
    }

    if (songsWithMp3.length === 0) {
        showToast('No direct MP3 links found for selected songs.', 'error');
        if (btn) {
            btn.disabled = _ghanaSelected.size === 0;
            btn.innerHTML = `⬇️ Download Selected (<span id="ghana-selected-count">${_ghanaSelected.size}</span>)`;
        }
        return;
    }

    let okCount = 0;
    for (const song of songsWithMp3) {
        try {
            const res = await fetch('/api/ghana-music/download', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ mp3_url: song.mp3_url, title: song.title, artist: song.artist || '', thumbnail: song.thumbnail, quality: '320', dl_type: 'audio' })
            });
            const data = await res.json();
            if (!data.error) { okCount++; }
        } catch { /* ignore individual failures in batch */ }
    }

    showToast(`✅ Queued ${okCount}/${songsWithMp3.length} song${okCount !== 1 ? 's' : ''} from Halmblog!`, 'success');
    _ghanaSelected.clear();
    updateGhanaSelectCount();
    renderGhanaMusic(_ghanaSongs);
    loadHistoryIds();
    setTimeout(() => switchTab('download'), 800);

    if (btn) {
        btn.disabled = _ghanaSelected.size === 0;
        btn.innerHTML = `⬇️ Download Selected (<span id="ghana-selected-count">${_ghanaSelected.size}</span>)`;
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

