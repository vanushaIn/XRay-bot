// Инициализация Telegram Web App
const tg = window.Telegram.WebApp;
tg.expand();

// Получаем initData
const initData = tg.initData;

// URL вашего бэкенда (замените на реальный)
const API_BASE = ''; // например, https://ваш-домен.ru

// Элементы DOM
const statusIcon = document.getElementById('status-icon');
const statusText = document.getElementById('status-text');
const countryElem = document.getElementById('country');
const serverIpElem = document.getElementById('server-ip');
const downloadSpeed = document.getElementById('download-speed');
const uploadSpeed = document.getElementById('upload-speed');
const pingElem = document.getElementById('ping');
const jitterElem = document.getElementById('jitter');
const testBtn = document.getElementById('test-btn');

// Состояние пользователя
let userStatus = {
    connected: false,
    country: 'United Kingdom',
    serverIp: '212.36.956.87',
    download: 0,
    upload: 0,
    ping: 0,
    jitter: 0
};

// Обновление интерфейса
function updateUI() {
    statusIcon.innerText = userStatus.connected ? '🟢' : '🔴';
    statusText.innerText = userStatus.connected ? 'Connected' : 'Not Connected';
    countryElem.innerText = userStatus.country;
    serverIpElem.innerText = userStatus.serverIp;
    downloadSpeed.innerText = userStatus.download.toFixed(2);
    uploadSpeed.innerText = userStatus.upload.toFixed(2);
    pingElem.innerText = userStatus.ping;
    jitterElem.innerText = userStatus.jitter;
}

// Получение статуса с сервера
// async function fetchStatus() {
//     try {
//         const response = await fetch(`${API_BASE}/api/user/status`, {
//             headers: {
//                 'Authorization': `tma ${initData}`
//             }
//         });
//         if (response.ok) {
//             const data = await response.json();
//             userStatus = { ...userStatus, ...data };
//             updateUI();
//         } else {
//             console.error('Failed to fetch status');
//         }
//     } catch (error) {
//         console.error('Error fetching status:', error);
//     }
// }

// Тест download
async function testDownload() {
    const startTime = performance.now();
    try {
        const response = await fetch(`${API_BASE}/api/speedtest/download`, {
            headers: {
                'Authorization': `tma ${initData}`
            },
            cache: 'no-store'
        });
        if (!response.ok) throw new Error('Download test failed');
        await response.blob(); // читаем всё тело
        const endTime = performance.now();
        const durationSec = (endTime - startTime) / 1000;
        // Размер файла 10 MB = 80 Mbit
        const speedMbps = (10 * 8) / durationSec;
        return speedMbps;
    } catch (error) {
        console.error('Download error:', error);
        return null;
    }
}

// Тест upload (генерирует 5 МБ данных)
async function testUpload() {
    const sizeMB = 5;
    const dataSize = sizeMB * 1024 * 1024;
    // Создаём буфер, заполненный нулями (можно и случайными данными)
    const buffer = new ArrayBuffer(dataSize);
    const startTime = performance.now();
    try {
        const response = await fetch(`${API_BASE}/api/speedtest/upload`, {
            method: 'POST',
            headers: {
                'Authorization': `tma ${initData}`,
                'Content-Type': 'application/octet-stream',
                'Content-Length': dataSize.toString()
            },
            body: buffer,
            cache: 'no-store'
        });
        if (!response.ok) throw new Error('Upload test failed');
        await response.json();
        const endTime = performance.now();
        const durationSec = (endTime - startTime) / 1000;
        const speedMbps = (sizeMB * 8) / durationSec;
        return speedMbps;
    } catch (error) {
        console.error('Upload error:', error);
        return null;
    }
}

// Тест ping и jitter (делает 5 запросов)
async function testPing(count = 5) {
    let pings = [];
    for (let i = 0; i < count; i++) {
        const start = performance.now();
        try {
            await fetch(`${API_BASE}/api/speedtest/ping`, {
                headers: {
                    'Authorization': `tma ${initData}`
                },
                cache: 'no-store'
            });
            const end = performance.now();
            pings.push(end - start);
        } catch (error) {
            console.error('Ping error:', error);
        }
        // небольшая задержка между запросами
        await new Promise(resolve => setTimeout(resolve, 100));
    }
    if (pings.length === 0) return { ping: 0, jitter: 0 };
    const avgPing = pings.reduce((a, b) => a + b, 0) / pings.length;
    // Jitter - среднеквадратическое отклонение
    const jitter = Math.sqrt(pings.map(p => Math.pow(p - avgPing, 2)).reduce((a, b) => a + b, 0) / pings.length);
    return { ping: avgPing, jitter };
}

// Обработчик кнопки GO
testBtn.addEventListener('click', async () => {
    testBtn.disabled = true;
    testBtn.innerText = 'Testing...';

    // Сброс значений
    userStatus.download = 0;
    userStatus.upload = 0;
    userStatus.ping = 0;
    userStatus.jitter = 0;
    updateUI();

    // Последовательно выполняем тесты
    const download = await testDownload();
    if (download !== null) {
        userStatus.download = download;
        updateUI();
    }

    const upload = await testUpload();
    if (upload !== null) {
        userStatus.upload = upload;
        updateUI();
    }

    const { ping, jitter } = await testPing();
    userStatus.ping = Math.round(ping);
    userStatus.jitter = Math.round(jitter);
    updateUI();

    testBtn.disabled = false;
    testBtn.innerText = 'GO';
});

// Загружаем статус при старте
fetchStatus();

// Обработка вкладок (просто переключение классов)
document.querySelectorAll('.tab').forEach(tab => {
    tab.addEventListener('click', () => {
        document.querySelectorAll('.tab').forEach(t => t.classList.remove('active'));
        tab.classList.add('active');
        // Здесь можно переключать контент, если нужно
    });
});