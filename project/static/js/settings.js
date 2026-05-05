// settings.js – настройки пользователя (город, тип диаграммы)

const SETTINGS_KEY = 'userSettings';

// Загрузка настроек
function loadSettings() {
    const saved = localStorage.getItem(SETTINGS_KEY);
    if (saved) {
        try {
            return JSON.parse(saved);
        } catch {
            return {};
        }
    }
    return {};
}

// Сохранение настроек
function saveSettings(settings) {
    localStorage.setItem(SETTINGS_KEY, JSON.stringify(settings));
}

// Получить текущий город
function getDefaultCity() {
    const settings = loadSettings();
    return settings.defaultCity || '';
}

// Получить текущий тип диаграммы
function getChartType() {
    const settings = loadSettings();
    return settings.chartType || 'pie';
}

// Инициализация элементов управления настройками
function setupSettingsUI() {
    const citySelect = document.getElementById('defaultCity');
    const chartSelect = document.getElementById('chartType');

    if (!citySelect || !chartSelect) return;

    // Устанавливаем сохранённые значения
    const settings = loadSettings();
    if (settings.defaultCity) {
        citySelect.value = settings.defaultCity;
    }
    if (settings.chartType) {
        chartSelect.value = settings.chartType;
    }

    // Обработчики изменений
    citySelect.addEventListener('change', () => {
        const current = loadSettings();
        current.defaultCity = citySelect.value;
        saveSettings(current);
    });

    chartSelect.addEventListener('change', () => {
        const current = loadSettings();
        current.chartType = chartSelect.value;
        saveSettings(current);
    });
}

// Глобальный доступ
window.getDefaultCity = getDefaultCity;
window.getChartType = getChartType;

// Вызываем инициализацию при загрузке DOM
document.addEventListener('DOMContentLoaded', setupSettingsUI);