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

}); // End DOMContentLoaded
