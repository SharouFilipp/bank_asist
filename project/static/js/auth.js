// auth.js – отвечает только за вход/регистрацию и хранение токена

window.accessToken = localStorage.getItem('access_token');
let authMode = 'login';

// Модальное окно авторизации
function showAuthModal() {
    document.getElementById('authModal').style.display = 'block';
    document.getElementById('appContainer').style.display = 'none';
}

function hideAuthModal() {
    document.getElementById('authModal').style.display = 'none';
    document.getElementById('appContainer').style.display = 'flex';
    // После входа нужно инициализировать чаты (вызовется из app.js)
    if (window.initApp) window.initApp();
}

// Проверка токена
async function checkToken() {
    if (!window.accessToken) {
        showAuthModal();
        return false;
    }
    try {
        const resp = await fetch('/users/me', {
            headers: { 'Authorization': `Bearer ${window.accessToken}` }
        });
        if (resp.ok) {
            hideAuthModal();
            return true;
        } else {
            localStorage.removeItem('access_token');
            window.accessToken = null;
            showAuthModal();
            return false;
        }
    } catch {
        showAuthModal();
        return false;
    }
}

document.addEventListener('DOMContentLoaded', () => {
    // Настройка вкладок
    document.getElementById('loginTab').onclick = () => {
        authMode = 'login';
        document.getElementById('loginTab').classList.add('active');
        document.getElementById('registerTab').classList.remove('active');
        document.getElementById('authSubmitBtn').textContent = 'Войти';
    };

    document.getElementById('registerTab').onclick = () => {
        authMode = 'register';
        document.getElementById('registerTab').classList.add('active');
        document.getElementById('loginTab').classList.remove('active');
        document.getElementById('authSubmitBtn').textContent = 'Зарегистрироваться';
    };

    // Закрытие окна
    document.getElementById('closeAuthModal').onclick = () => {
        document.getElementById('authModal').style.display = 'none';
    };

    // Отправка формы
    document.getElementById('authForm').onsubmit = async (e) => {
        e.preventDefault();
        const username = document.getElementById('username').value.trim();
        const password = document.getElementById('password').value.trim();
        const errorEl = document.getElementById('authError');
        errorEl.textContent = '';

        if (authMode === 'register') {
            const formData = new FormData();
            formData.append('username', username);
            formData.append('password', password);
            try {
                const resp = await fetch('/register', { method: 'POST', body: formData });
                if (resp.ok) {
                    alert('Регистрация успешна! Теперь войдите.');
                    document.getElementById('loginTab').click();
                } else {
                    const err = await resp.json();
                    errorEl.textContent = err.detail || 'Ошибка регистрации';
                }
            } catch {
                errorEl.textContent = 'Нет соединения с сервером';
            }
        } else {
            const formData = new URLSearchParams();
            formData.append('username', username);
            formData.append('password', password);
            try {
                const resp = await fetch('/token', {
                    method: 'POST',
                    body: formData,
                    headers: { 'Content-Type': 'application/x-www-form-urlencoded' }
                });
                if (resp.ok) {
                    const data = await resp.json();
                    window.accessToken = data.access_token;
                    localStorage.setItem('access_token', window.accessToken);
                    hideAuthModal();
                } else {
                    const err = await resp.json();
                    errorEl.textContent = err.detail || 'Неверный логин или пароль';
                }
            } catch {
                errorEl.textContent = 'Нет соединения с сервером';
            }
        }
    };

    // Кнопка "Выйти" (назначается в app.js, но можно и тут)
    document.getElementById('logoutButton').onclick = () => {
        localStorage.removeItem('access_token');
        window.accessToken = null;
        showAuthModal();
    };
});