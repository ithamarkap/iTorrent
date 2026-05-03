// Wait for DOM to be ready
document.addEventListener('DOMContentLoaded', function () {
    // DOM Elements
    const torrentListEl = document.getElementById('torrent-list');
    const selectFileBtn = document.getElementById('select-file-btn');
    const downloadPathEl = document.getElementById('download-path');
    const changeDirBtn = document.getElementById('change-dir-btn');
    const themeToggleBtn = document.getElementById('theme-toggle');
    const tabBtns = document.querySelectorAll('.tab-btn');
    const tabContents = document.querySelectorAll('.tab-content');

    let selectedTorrentId = null;
    let allTorrents = [];

    // Theme toggle functionality (dark mode by default)
    const currentTheme = localStorage.getItem('theme') || 'dark';
    if (currentTheme === 'light') {
        document.body.classList.add('light-mode');
        themeToggleBtn.textContent = '☀️';
    }

    themeToggleBtn.addEventListener('click', () => {
        document.body.classList.toggle('light-mode');
        const isLight = document.body.classList.contains('light-mode');
        themeToggleBtn.textContent = isLight ? '☀️' : '🌙';
        localStorage.setItem('theme', isLight ? 'light' : 'dark');
    });

    // Tab Switching
    tabBtns.forEach(btn => {
        btn.addEventListener('click', () => {
            const tabName = btn.getAttribute('data-tab');

            tabBtns.forEach(b => b.classList.remove('active'));
            tabContents.forEach(c => c.classList.remove('active'));

            btn.classList.add('active');
            document.getElementById(`${tabName}-view`).classList.add('active');

            if (tabName === 'statistics') {
                updateStatistics();
            }
        });
    });

    // Initial Load
    fetchTorrents();

    // Poll for updates (in a real app, use WebSockets or SSE)
    setInterval(fetchTorrents, 1000);

    async function fetchTorrents() {
        try {
            const response = await fetch('/api/torrents');
            allTorrents = await response.json();
            renderTorrents(allTorrents);
            if (selectedTorrentId !== null) {
                updateStatistics();
            }
        } catch (error) {
            console.error('Error fetching torrents:', error);
        }
    }

    function renderTorrents(torrents) {
        torrentListEl.innerHTML = '';

        if (torrents.length === 0) {
            torrentListEl.innerHTML = `
            <div style="text-align: center; padding: 40px; color: var(--text-secondary); border: 2px dashed var(--border-color); border-radius: 8px;">
                No active torrents
            </div>
        `;
            return;
        }

        torrents.forEach(torrent => {
            const item = document.createElement('div');
            item.className = 'torrent-item';
            if (torrent.id === selectedTorrentId) {
                item.classList.add('selected');
            }

            // Click to select torrent
            item.addEventListener('click', (e) => {
                // Don't select if clicking a button
                if (e.target.tagName === 'BUTTON') return;
                
                selectedTorrentId = torrent.id;
                document.querySelectorAll('.torrent-item').forEach(i => i.classList.remove('selected'));
                item.classList.add('selected');
                updateStatistics();
            });

            const isPaused = torrent.status === 'Paused';
            const actionBtnText = isPaused ? 'Start' : 'Pause';
            const actionBtnClass = isPaused ? 'primary-btn' : 'secondary-btn';
            const action = isPaused ? 'start' : 'pause';

            item.innerHTML = `
            <div class="torrent-info">
                <div class="name">${torrent.name}</div>
                <div class="meta">
                    ${torrent.status} • ${torrent.progress}% • ↓ ${torrent.downloadSpeed} • ↑ ${torrent.uploadSpeed}
                </div>
                <div class="progress-bar">
                    <div class="progress-fill" style="width: ${torrent.progress}%"></div>
                </div>
            </div>
            <div class="torrent-actions">
                <button class="${actionBtnClass}" onclick="handleTorrentAction(${torrent.id}, '${action}')">${actionBtnText}</button>
                <button class="secondary-btn remove-btn" onclick="handleTorrentAction(${torrent.id}, 'remove')">X</button>
            </div>
        `;
            torrentListEl.appendChild(item);
        });
    }

    // Event Listeners for File Selection
    if (selectFileBtn) {
        selectFileBtn.addEventListener('click', () => {
            const fileInput = document.getElementById('file-input');
            if (fileInput) {
                fileInput.click();
            }
        });
    }

    // Handle file input change
    const fileInput = document.getElementById('file-input');
    if (fileInput) {
        fileInput.addEventListener('change', (e) => {
            if (e.target.files.length > 0) {
                const file = e.target.files[0];
                let path = file.path || file.name;
                if (window.electronAPI && window.electronAPI.getFilePath) {
                    path = window.electronAPI.getFilePath(file);
                }
                addTorrent(path);
                e.target.value = ''; // clear input
            }
        });
    }

    // Handle magnet link addition
    const addMagnetBtn = document.getElementById('add-magnet-btn');
    const magnetLinkInput = document.getElementById('magnet-link');
    if (addMagnetBtn && magnetLinkInput) {
        addMagnetBtn.addEventListener('click', () => {
            const magnetUrl = magnetLinkInput.value.trim();
            if (magnetUrl) {
                addTorrent(magnetUrl);
                magnetLinkInput.value = ''; // clear input
            }
        });
    }

    // Event Listeners for Directory Selection
    changeDirBtn.addEventListener('click', async () => {
        // Use hidden directory input
        const dirInput = document.getElementById('dir-input');
        if (dirInput) {
            dirInput.click();
        }
    });

    // Handle directory input change
    const dirInput = document.getElementById('dir-input');
    if (dirInput) {
        dirInput.addEventListener('change', (e) => {
            if (e.target.files.length > 0) {
                const path = e.target.files[0].webkitRelativePath.split('/')[0];
                downloadPathEl.innerText = path || e.target.files[0].name;
            }
        });
    }

    async function addTorrent(path) {
        try {
            const response = await fetch('/api/torrents/add', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ path })
            });
            const result = await response.json();
            if (result.success) {
                fetchTorrents();
            }
        } catch (error) {
            console.error('Error adding torrent:', error);
        }
    }

    // Global function to be called from HTML onclick
    window.handleTorrentAction = async (id, action) => {
        try {
            let deleteFiles = false;
            if (action === 'remove') {
                deleteFiles = await showCustomModal({
                    title: 'Delete Files?',
                    message: 'Do you want to delete the downloaded files as well?',
                    detail: 'This will permanently remove the files from your disk.'
                });
            }

            const response = await fetch(`/api/torrents/${id}/action`, {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ action, delete_files: deleteFiles })
            });
            const result = await response.json();
            if (result.removed && selectedTorrentId === id) {
                selectedTorrentId = null;
                updateStatistics();
            }
            fetchTorrents();
        } catch (error) {
            console.error('Error performing action:', error);
        }
    }

    function showCustomModal(options) {
        return new Promise((resolve) => {
            const overlay = document.getElementById('modal-overlay');
            const title = document.getElementById('modal-title');
            const message = document.getElementById('modal-message');
            const confirmBtn = document.getElementById('modal-confirm-btn');
            const cancelBtn = document.getElementById('modal-cancel-btn');

            title.textContent = options.title || 'Confirm';
            message.innerHTML = `${options.message}${options.detail ? '<br><small style="color: var(--text-secondary);">' + options.detail + '</small>' : ''}`;

            overlay.classList.add('active');

            const handleConfirm = () => {
                cleanup();
                resolve(true);
            };

            const handleCancel = () => {
                cleanup();
                resolve(false);
            };

            const cleanup = () => {
                overlay.classList.remove('active');
                confirmBtn.removeEventListener('click', handleConfirm);
                cancelBtn.removeEventListener('click', handleCancel);
            };

            confirmBtn.addEventListener('click', handleConfirm);
            cancelBtn.addEventListener('click', handleCancel);
        });
    }

    function updateStatistics() {
        const noSelection = document.getElementById('no-torrent-selected');
        const statsContainer = document.getElementById('stats-container');

        if (selectedTorrentId === null) {
            noSelection.style.display = 'flex';
            statsContainer.style.display = 'none';
            return;
        }

        const torrent = allTorrents.find(t => t.id === selectedTorrentId);
        if (!torrent) {
            selectedTorrentId = null;
            noSelection.style.display = 'flex';
            statsContainer.style.display = 'none';
            return;
        }

        noSelection.style.display = 'none';
        statsContainer.style.display = 'block';

        // Update Text Info
        document.getElementById('stats-torrent-name').textContent = torrent.name;
        document.getElementById('stat-status').textContent = torrent.status;
        document.getElementById('stat-progress').textContent = `${torrent.progress}%`;
        document.getElementById('stat-download-speed').textContent = torrent.downloadSpeed;
        document.getElementById('stat-upload-speed').textContent = torrent.uploadSpeed;
        document.getElementById('stat-total-downloaded').textContent = formatBytes(torrent.totalDownloaded);
        document.getElementById('stat-total-uploaded').textContent = formatBytes(torrent.totalUploaded);
        
        const ratio = torrent.totalDownloaded > 0 ? (torrent.totalUploaded / torrent.totalDownloaded).toFixed(2) : '0.00';
        document.getElementById('stat-ratio').textContent = ratio;
        document.getElementById('stat-peers').textContent = torrent.peerCount;
        document.getElementById('stat-elapsed').textContent = formatTime(torrent.elapsedTime);
        
        // Calculate ETA
        if (torrent.progress >= 100 || torrent.status === 'Paused' || parseFloat(torrent.downloadSpeed) === 0) {
            document.getElementById('stat-eta').textContent = '∞';
        } else {
            let speedBytes = parseFloat(torrent.downloadSpeed);
            if (torrent.downloadSpeed.includes('GB/s')) speedBytes *= 1073741824;
            else if (torrent.downloadSpeed.includes('MB/s')) speedBytes *= 1048576;
            else if (torrent.downloadSpeed.includes('KB/s')) speedBytes *= 1024;
            
            const remainingBytes = (100 - torrent.progress) / 100 * torrent.totalSize;
            const etaSeconds = remainingBytes / speedBytes;
            document.getElementById('stat-eta').textContent = formatTime(etaSeconds);
        }

        renderPieceChart(torrent.bitfield);
    }

    function renderPieceChart(bitfield) {
        const chart = document.getElementById('piece-chart');
        if (!bitfield || bitfield.length === 0) {
            chart.innerHTML = '<div style="grid-column: 1/-1; text-align: center; color: var(--text-secondary);">No piece data available</div>';
            return;
        }

        // Optimization: only redraw if length changed or first time
        if (chart.children.length !== bitfield.length) {
            chart.innerHTML = '';
            const fragment = document.createDocumentFragment();
            for (let i = 0; i < bitfield.length; i++) {
                const box = document.createElement('div');
                box.className = 'piece-box ' + (bitfield[i] ? 'downloaded' : 'missing');
                box.title = `Piece ${i}: ${bitfield[i] ? 'Downloaded' : 'Missing'}`;
                fragment.appendChild(box);
            }
            chart.appendChild(fragment);
        } else {
            // Just update classes
            for (let i = 0; i < bitfield.length; i++) {
                const box = chart.children[i];
                const isDownloaded = bitfield[i] === 1;
                if (isDownloaded && !box.classList.contains('downloaded')) {
                    box.classList.remove('missing');
                    box.classList.add('downloaded');
                } else if (!isDownloaded && !box.classList.contains('missing')) {
                    box.classList.remove('downloaded');
                    box.classList.add('missing');
                }
            }
        }
    }

    function formatBytes(bytes) {
        if (bytes === 0) return '0 Bytes';
        const k = 1024;
        const sizes = ['Bytes', 'KB', 'MB', 'GB', 'TB'];
        const i = Math.floor(Math.log(bytes) / Math.log(k));
        return parseFloat((bytes / Math.pow(k, i)).toFixed(2)) + ' ' + sizes[i];
    }

    function formatTime(seconds) {
        if (!seconds || seconds === Infinity) return '∞';
        const h = Math.floor(seconds / 3600);
        const m = Math.floor((seconds % 3600) / 60);
        const s = Math.floor(seconds % 60);
        
        let result = '';
        if (h > 0) result += h + 'h ';
        if (m > 0 || h > 0) result += m + 'm ';
        result += s + 's';
        return result;
    }

    // Peer List Viewer functionality
    const selectTorrentBtn = document.getElementById('select-torrent-btn');
    const torrentFileInput = document.getElementById('torrent-file-input');
    const magnetPeerLink = document.getElementById('magnet-peer-link');
    const getPeersBtn = document.getElementById('get-peers-btn');
    const peerStatus = document.getElementById('peer-status');
    const peerResults = document.getElementById('peer-results');

    let selectedTorrentPath = null;

    // File selection for peer viewer
    if (selectTorrentBtn && torrentFileInput) {
        selectTorrentBtn.addEventListener('click', () => {
            torrentFileInput.click();
        });

        torrentFileInput.addEventListener('change', (e) => {
            if (e.target.files.length > 0) {
                const file = e.target.files[0];
                // Use Electron API to get full path if available, otherwise fallback to name (which will fail for backend)
                if (window.electronAPI && window.electronAPI.getFilePath) {
                    selectedTorrentPath = window.electronAPI.getFilePath(file);
                } else {
                    selectedTorrentPath = file.path || file.name;
                }

                peerStatus.textContent = `Selected: ${file.name}`;
                peerStatus.style.color = 'var(--success-color)';
                e.target.value = ''; // clear input so identical files can be selected again
            }
        });
    }

    // Get peers button
    if (getPeersBtn) {
        getPeersBtn.addEventListener('click', async () => {
            const magnetUrl = magnetPeerLink.value.trim();

            if (!selectedTorrentPath && !magnetUrl) {
                peerStatus.textContent = 'Please select a torrent file or paste a magnet link';
                peerStatus.style.color = 'var(--error-color)';
                return;
            }

            peerStatus.textContent = 'Fetching peers...';
            peerStatus.style.color = 'var(--text-secondary)';
            peerResults.style.display = 'none';

            try {
                const payload = magnetUrl
                    ? { type: 'magnet', url: magnetUrl }
                    : { type: 'file', path: selectedTorrentPath };

                const response = await fetch('/api/get-peers', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify(payload)
                });

                const result = await response.json();

                if (result.success) {
                    displayPeers(result);
                    peerStatus.textContent = 'Peers retrieved successfully!';
                    peerStatus.style.color = 'var(--success-color)';
                } else {
                    peerStatus.textContent = `Error: ${result.error}`;
                    peerStatus.style.color = 'var(--error-color)';
                }
            } catch (error) {
                peerStatus.textContent = `Error: ${error.message}`;
                peerStatus.style.color = 'var(--error-color)';
            }
        });
    }

    function displayPeers(data) {
        document.getElementById('peer-torrent-name').textContent = data.torrent_name;
        document.getElementById('peer-count').textContent = `${data.peer_count} peers found`;

        const tbody = document.getElementById('peer-table-body');
        tbody.innerHTML = '';

        data.peers.forEach(peer => {
            const row = document.createElement('tr');
            row.innerHTML = `
                <td>${peer.ip}</td>
                <td>${peer.port}</td>
            `;
            tbody.appendChild(row);
        });

        peerResults.style.display = 'block';
    }

}); // End DOMContentLoaded
