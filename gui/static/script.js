// Wait for DOM to be ready
document.addEventListener('DOMContentLoaded', function () {
    // DOM Elements
    const torrentListEl = document.getElementById('torrent-list');
    const selectFileBtn = document.getElementById('select-file-btn');
    const downloadPathEl = document.getElementById('download-path');
    const changeDirBtn = document.getElementById('change-dir-btn');
    const themeToggleBtn = document.getElementById('theme-toggle');

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

    // Initial Load
    fetchTorrents();

    // Poll for updates (in a real app, use WebSockets or SSE)
    setInterval(fetchTorrents, 2000);

    async function fetchTorrents() {
        try {
            const response = await fetch('/api/torrents');
            const torrents = await response.json();
            renderTorrents(torrents);
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
                addTorrent(file.name);
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
            await fetch(`/api/torrents/${id}/action`, {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ action })
            });
            fetchTorrents();
        } catch (error) {
            console.error('Error performing action:', error);
        }
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
