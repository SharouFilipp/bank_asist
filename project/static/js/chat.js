// chat.js – управление историей чатов и отображение сообщений

const CHATS_KEY = 'ragChatSessions';
const ACTIVE_CHAT_KEY = 'ragActiveChatId';
window.allChats = {};
window.activeChatId = null;

// ====== Вспомогательная функция получения геолокации ======
async function getGeolocation() {
    if (!navigator.geolocation) {
        console.warn('⚠️ Браузер не поддерживает геолокацию');
        return { latitude: null, longitude: null };
    }
    try {
        const position = await new Promise((resolve, reject) => {
            navigator.geolocation.getCurrentPosition(resolve, reject, {
                timeout: 5000,
                maximumAge: 600000,        // использовать кэшированную позицию до 10 мин
                enableHighAccuracy: false  // не требуется высокая точность
            });
        });
        const { latitude, longitude } = position.coords;
        console.log('✅ Геолокация получена:', latitude, longitude);
        return { latitude, longitude };
    } catch (err) {
        if (err.code === 1) {
            console.warn('⛔ Пользователь отклонил запрос геолокации');
        } else if (err.code === 2) {
            console.warn('📡 Позиция недоступна');
        } else if (err.code === 3) {
            console.warn('⏰ Таймаут геолокации');
        } else {
            console.warn('⚠️ Неизвестная ошибка геолокации:', err);
        }
        return { latitude: null, longitude: null };
    }
}

// ====== Управление чатами ======
function loadChats() {
    const saved = localStorage.getItem(CHATS_KEY);
    window.allChats = saved ? JSON.parse(saved) : {};
    if (Object.keys(window.allChats).length === 0) {
        createNewChat(false);
    }
    const savedId = localStorage.getItem(ACTIVE_CHAT_KEY);
    window.activeChatId = (savedId && window.allChats[savedId])
        ? savedId
        : Object.keys(window.allChats)[0];
    saveChats();
    renderChatList();
    renderActiveChat();
    showWelcomeIfNeeded();
}

function saveChats() {
    localStorage.setItem(CHATS_KEY, JSON.stringify(window.allChats));
    if (window.activeChatId) localStorage.setItem(ACTIVE_CHAT_KEY, window.activeChatId);
}

function createNewChat(ask = true) {
    if (ask && !confirm('Начать новый чат?')) return;
    const id = `chat-${Date.now()}`;
    window.allChats[id] = [{ sender: 'assistant', text: 'Здравствуйте! Чем могу помочь сегодня?' }];
    window.activeChatId = id;
    saveChats();
    renderChatList();
    renderActiveChat();
    document.getElementById('userInput').focus();
}

function switchChat(id) {
    if (id && window.allChats[id] && id !== window.activeChatId) {
        window.activeChatId = id;
        saveChats();
        renderChatList();
        renderActiveChat();
    }
}

function deleteChat(id) {
    if (!window.allChats[id]) return;
    if (Object.keys(window.allChats).length <= 1) {
        alert('Нельзя удалить единственный чат.');
        return;
    }
    if (!confirm(`Удалить чат "${getChatTitle(id)}"?`)) return;
    delete window.allChats[id];
    if (window.activeChatId === id) {
        window.activeChatId = Object.keys(window.allChats)[0];
    }
    saveChats();
    renderChatList();
    renderActiveChat();
}

function renderChatList() {
    const ul = document.getElementById('chatList');
    ul.innerHTML = '';
    const ids = Object.keys(window.allChats).sort((a, b) => parseInt(b.split('-')[1]) - parseInt(a.split('-')[1]));
    ids.forEach(id => {
        const li = document.createElement('li');
        li.className = 'chat-list-item';
        li.dataset.chatId = id;
        if (id === window.activeChatId) li.classList.add('active');

        const title = document.createElement('span');
        title.className = 'chat-title';
        title.textContent = getChatTitle(id);

        const delBtn = document.createElement('button');
        delBtn.className = 'delete-chat-button';
        delBtn.innerHTML = '×';
        delBtn.onclick = e => { e.stopPropagation(); deleteChat(id); };

        li.append(title, delBtn);
        ul.appendChild(li);
    });
}

function getChatTitle(id) {
    const msgs = window.allChats[id] || [];
    if (!msgs.length) return 'Новый чат';
    const firstUser = msgs.find(m => m.sender === 'user');
    if (firstUser) {
        return firstUser.text.slice(0, 25) + (firstUser.text.length > 25 ? '...' : '');
    }
    return 'Новый чат';
}

function renderActiveChat() {
    const box = document.getElementById('chatBox');
    box.innerHTML = '';
    if (window.activeChatId && window.allChats[window.activeChatId]) {
        window.allChats[window.activeChatId].forEach(msg => displayMessage(msg, false));
    }
}

// Универсальная функция отображения сообщения
function displayMessage(msg, saveToHistory = true) {
    if (saveToHistory && window.activeChatId) {
        window.allChats[window.activeChatId].push(msg);
        saveChats();
    }

    const div = document.createElement('div');
    div.className = `message ${msg.sender}`;

    const content = document.createElement('div');
    content.className = 'message-content';

    if (msg.type === 'img') {
        const img = document.createElement('img');
        img.src = msg.text;
        img.onclick = () => window.open(img.src);
        content.appendChild(img);
    } else {
        let text = msg.text || '';
        text = text.replace(/\*\*(.*?)\*\*/g, '<strong>$1</strong>');
        text = text.replace(/\n/g, '<br>');
        content.innerHTML = text;
    }

    div.appendChild(content);
    document.getElementById('chatBox').appendChild(div);
    document.getElementById('chatBox').scrollTop = document.getElementById('chatBox').scrollHeight;
}

async function sendMessageToAPI(query) {
    if (!window.activeChatId || !query.trim()) return;

    // Проверяем перехватчик (загрузка чека, диаграмма и т.п.)
    if (window.interceptChatMessage && window.interceptChatMessage(query.trim())) {
        document.getElementById('userInput').value = '';
        return; // перехвачено — не отправляем на сервер
    }

    window.displayMessage({ sender: 'user', text: query.trim() }, true);
    document.getElementById('userInput').value = '';
    showTyping();

    // Всегда пытаемся получить геолокацию
    const { latitude, longitude } = await getGeolocation();
    console.log('📤 Отправляемые координаты:', latitude, longitude);

    // Последние 6 сообщений как контекст для сервера
    const chatMsgs = window.allChats[window.activeChatId] || [];
    const client_history = chatMsgs.slice(-7, -1)  // без только что добавленного
        .filter(m => m.sender === 'user' || m.sender === 'assistant')
        .map(m => ({ role: m.sender, text: (m.text || '').slice(0, 400) }));

    try {
        const resp = await fetch('/ask', {
            method: 'POST',
            headers: {
                'Content-Type': 'application/json',
                'Authorization': `Bearer ${window.accessToken || ''}`
            },
            body: JSON.stringify({ user_query: query, latitude, longitude, client_history })
        });
        hideTyping();
        if (!resp.ok) throw new Error('Ошибка сервера');
        const data = await resp.json();
        if (data.assistant_answer) {
            window.displayMessage({ sender: 'assistant', text: data.assistant_answer }, true);
        }
        if (data.image_url) {
            window.displayMessage({ sender: 'system', type: 'img', text: data.image_url }, true);
        }
        if (data.map_url) {
            window.open(data.map_url, '_blank');
        }
        if (data.follow_ups && data.follow_ups.length) {
            showFollowUpButtons(data.follow_ups);
        }
    } catch (e) {
        hideTyping();
        window.displayMessage({ sender: 'error', text: `❌ ${e.message}` }, true);
    }
}

function showTyping() {
    const typingDiv = document.createElement('div');
    typingDiv.className = 'message assistant';
    typingDiv.id = 'typing-indicator';
    typingDiv.innerHTML = '<div class="message-content"><span class="typing-dots">...</span></div>';
    document.getElementById('chatBox').appendChild(typingDiv);
}

function hideTyping() {
    const el = document.getElementById('typing-indicator');
    if (el) el.remove();
}

// Обработчики событий чата
function setupChatEvents() {
    document.getElementById('sendButton').onclick = () => sendMessageToAPI(document.getElementById('userInput').value);
    document.getElementById('userInput').addEventListener('keydown', e => {
        if (e.key === 'Enter' && !e.shiftKey) {
            e.preventDefault();
            sendMessageToAPI(document.getElementById('userInput').value);
        }
    });
    document.getElementById('newChatButton').onclick = () => createNewChat();
    document.getElementById('chatList').addEventListener('click', e => {
        const li = e.target.closest('.chat-list-item');
        if (li && li.dataset.chatId) switchChat(li.dataset.chatId);
    });
}

// Автоматическая высота textarea
document.addEventListener('DOMContentLoaded', function() {
    const textarea = document.getElementById('userInput');
    if (textarea) {
        textarea.addEventListener('input', function() {
            this.style.height = 'auto';
            this.style.height = Math.min(this.scrollHeight, 120) + 'px';
        });
    }
});

function showFollowUpButtons(suggestions) {
    const old = document.querySelector('.follow-up-container');
    if (old) old.remove();
    if (!suggestions || !suggestions.length) return;

    const container = document.createElement('div');
    container.className = 'follow-up-container';
    suggestions.forEach(text => {
        const btn = document.createElement('button');
        btn.textContent = text;
        btn.className = 'follow-up-btn';
        btn.addEventListener('click', () => {
            document.getElementById('userInput').value = text;
            sendMessageToAPI(text);
        });
        container.appendChild(btn);
    });
    document.getElementById('chatBox').appendChild(container);
    document.getElementById('chatBox').scrollTop = document.getElementById('chatBox').scrollHeight;
}

// Глобальный доступ
window.showFollowUpButtons = showFollowUpButtons;
window.displayMessage = displayMessage;
window.loadChats = loadChats;
window.setupChatEvents = setupChatEvents;
window.sendMessageToAPI = sendMessageToAPI;
// ─── Приветственное окно при первом входе ────────────────
function showWelcomeIfNeeded() {
    if (!localStorage.getItem('welcomed_v2')) {
        document.getElementById('welcomeModal').style.display = 'flex';
    }
}

document.addEventListener('DOMContentLoaded', () => {
    const closeBtn = document.getElementById('closeWelcomeBtn');
    if (closeBtn) {
        closeBtn.addEventListener('click', () => {
            document.getElementById('welcomeModal').style.display = 'none';
            localStorage.setItem('welcomed_v2', '1');
        });
    }
});