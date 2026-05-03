const { app, BrowserWindow, ipcMain } = require('electron');
const path = require('path');
const { spawn } = require('child_process');
const http = require('http');

let flaskProcess = null;
let mainWindow = null;

const FLASK_PORT = 5000;
const FLASK_URL = `http://127.0.0.1:${FLASK_PORT}`;

function startFlask() {
    let scriptPath;
    let pythonPath;
    let args;

    if (app.isPackaged) {
        // In production, use the bundled executable
        // api.exe is placed in resources/api/dist/api/api.exe or similar depending on pyinstaller
        // Based on build_windows.bat: distpath gui/resources -> gui/resources/api.exe
        // Electron builder extraResources: resources/api -> api
        // So final path: contents/resources/api/api.exe
        scriptPath = path.join(process.resourcesPath, 'api', 'api.exe');
        console.log(`Starting Packaged Flask from: ${scriptPath}`);

        flaskProcess = spawn(scriptPath, [], {
            stdio: 'inherit'
        });
    } else {
        // In development, run python script
        scriptPath = path.join(__dirname, '../app.py');
        pythonPath = process.platform === 'win32' ? 'python' : 'python3'; // Default fallback

        // Check for venv
        const venvPythonPath = process.platform === 'win32' ? '../venv/Scripts/python.exe' : '../venv/bin/python';
        const venvPython = path.join(__dirname, venvPythonPath);
        const fs = require('fs');
        if (fs.existsSync(venvPython)) {
            pythonPath = venvPython;
        }

        console.log(`Starting Flask from: ${scriptPath} using ${pythonPath}`);

        flaskProcess = spawn(pythonPath, [scriptPath], {
            stdio: 'inherit'
        });
    }

    flaskProcess.on('error', (err) => {
        console.error('Failed to start Flask process:', err);
    });
}

function stopFlask() {
    if (flaskProcess) {
        console.log('Stopping Flask process...');
        flaskProcess.kill();
        flaskProcess = null;
    }
}

function createWindow() {
    mainWindow = new BrowserWindow({
        width: 1200,
        height: 800,
        icon: path.join(__dirname, '..', 'icon.ico'),
        webPreferences: {
            preload: path.join(__dirname, 'preload.js'),
            nodeIntegration: false,
            contextIsolation: true,
            sandbox: false // Disable sandbox to ensure preload works easily in this environment
        },
    });

    // Poll Flask server until it's ready
    const checkServer = () => {
        http.get(FLASK_URL, (res) => {
            if (res.statusCode === 200) {
                mainWindow.loadURL(FLASK_URL);
                // mainWindow.webContents.openDevTools();
            } else {
                setTimeout(checkServer, 100);
            }
        }).on('error', () => {
            setTimeout(checkServer, 100);
        });
    };

    checkServer();

    mainWindow.on('closed', () => {
        mainWindow = null;
    });
}

app.whenReady().then(() => {
    startFlask();
    createWindow();

    app.on('activate', () => {
        if (BrowserWindow.getAllWindows().length === 0) {
            createWindow();
        }
    });
});

app.on('window-all-closed', () => {
    if (process.platform !== 'darwin') {
        app.quit();
    }
});

app.on('will-quit', () => {
    stopFlask();
});

// IPC Handlers
ipcMain.handle('select-file', async () => {
    console.log('IPC: select-file called');
    const { dialog } = require('electron');
    try {
        const result = await dialog.showOpenDialog({
            properties: ['openFile'],
            filters: [{ name: 'Torrent Files', extensions: ['torrent'] }]
        });
        console.log('IPC: select-file result:', result);
        return result;
    } catch (err) {
        console.error('IPC: select-file error:', err);
        throw err;
    }
});

ipcMain.handle('select-directory', async () => {
    const { dialog } = require('electron');
    const result = await dialog.showOpenDialog({
        properties: ['openDirectory']
    });
    return result;
});
