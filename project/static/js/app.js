// app.js – точка входа после загрузки всех модулей

document.addEventListener('DOMContentLoaded', () => {
    // setupUploadModal уже вызывается в upload.js при DOMContentLoaded,
    // поэтому повторно вызывать не нужно.
    if (typeof setupChatEvents === 'function') setupChatEvents();

    // Проверка токена и запуск
    checkToken().then(valid => {
        if (valid) {
            if (typeof loadChats === 'function') loadChats();
            startHealthCheck();
        }
    });
    window.initApp = function() {
    if (typeof loadChats === 'function') loadChats();
    fetchStartSuggestions();
};
async function fetchStartSuggestions() {
    try {
        const resp = await fetch('/suggestions', {
            headers: { 'Authorization': `Bearer ${window.accessToken}` }
        });
        if (resp.ok) {
            const data = await resp.json();
            showStartCards(data.suggestions);
        }
    } catch(e) {}
}

function showStartCards(items) {
    const existing = document.querySelector('.start-cards');
    if (existing) existing.remove();
    if (!items || !items.length) return;

    const container = document.createElement('div');
    container.className = 'start-cards';
    items.forEach(text => {
        const card = document.createElement('div');
        card.className = 'start-card';
        card.textContent = text;
        card.addEventListener('click', () => {
            document.getElementById('userInput').value = text;
            if (typeof sendMessageToAPI === 'function') sendMessageToAPI(text);
        });
        container.appendChild(card);
    });
    const inputArea = document.querySelector('.input-area');
    inputArea.parentNode.insertBefore(container, inputArea);
}

    // Кнопки графиков
        document.getElementById('loadChartButton').onclick = async () => {
        try {
            const chartType = window.getChartType ? window.getChartType() : 'pie';
            const resp = await fetch(`/expenses/chart?chart_type=${chartType}`, {
                headers: { 'Authorization': `Bearer ${window.accessToken}` }
            });
            if (resp.ok) {
                const data = await resp.json();
                window.open(data.chart_url + '?t=' + Date.now(), '_blank');
            }
        } catch {}
    };

    document.getElementById('kursButton').onclick = () => {
        window.open('/static/currency.png?t=' + Date.now(), '_blank');
    };
    // Получение геопозиции
function getCurrentPosition() {
    return new Promise((resolve, reject) => {
        if (!navigator.geolocation) {
            reject(new Error('Геолокация не поддерживается'));
        } else {
            navigator.geolocation.getCurrentPosition(resolve, reject, {
                timeout: 10000,
                maximumAge: 60000
            });
        }
    });
}

// Обработчик кнопки "Ближайший филиал"
document.getElementById('nearestFilialButton').onclick = async () => {
    try {
        const position = await getCurrentPosition();
        const lat = position.coords.latitude;
        const lon = position.coords.longitude;
        const resp = await fetch('/ask', {
            method: 'POST',
            headers: {
                'Content-Type': 'application/json',
                'Authorization': `Bearer ${window.accessToken || ''}`
            },
            body: JSON.stringify({
                user_query: 'ближайший филиал',
                latitude: lat,
                longitude: lon
            })
        });
        if (resp.ok) {
            const data = await resp.json();
            // Отображаем ответ в чате
            if (typeof displayMessage === 'function') {
                displayMessage({ sender: 'assistant', text: data.assistant_answer }, true);
            }
            if (data.follow_ups && typeof showFollowUpButtons === 'function') {
                showFollowUpButtons(data.follow_ups);
            }
        } else {
            alert('Не удалось найти ближайший филиал');
        }
    } catch (err) {
        console.error('Ошибка геолокации:', err);
        alert('Не удалось получить геопозицию. Разрешите доступ к местоположению в браузере.');
    }
};
    // Health check
    function startHealthCheck() {
        const indicator = document.getElementById('status-indicator');
        setInterval(async () => {
            try {
                const r = await fetch('/health');
                indicator.className = 'status-indicator ' + (r.ok ? 'status-online' : 'status-offline');
            } catch {
                indicator.className = 'status-indicator status-offline';
            }
        }, 30000);
    }
});