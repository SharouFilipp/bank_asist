// upload.js — загрузка чеков и диаграмма расходов

// ─── Состояние загрузки ───────────────────────────────────
let selectedFile = null;
let uploadAbortController = null;

// ─── Открытие модалки загрузки ────────────────────────────
function openUploadModal() {
    resetUploadModal();
    document.getElementById('uploadModal').style.display = 'flex';
}

function closeUploadModal() {
    // Отменяем загрузку если идёт
    if (uploadAbortController) {
        uploadAbortController.abort();
        uploadAbortController = null;
    }
    document.getElementById('uploadModal').style.display = 'none';
    resetUploadModal();
}

function resetUploadModal() {
    selectedFile = null;
    document.getElementById('previewCard').style.display = 'none';
    document.getElementById('previewImage').src = '';
    document.getElementById('imageName').textContent = '';
    document.getElementById('progressBar').style.display = 'none';
    document.getElementById('progressFill').style.width = '0%';
    document.getElementById('progressText').textContent = '0%';
    document.getElementById('confirmUploadButton').disabled = true;
    document.getElementById('imageInput').value = '';
    // Показываем дроп-зону обратно
    document.getElementById('dropArea').style.display = '';
}

// ─── Выбор файла ─────────────────────────────────────────
function handleFileSelect(file) {
    if (!file || !file.type.startsWith('image/')) {
        alert('Пожалуйста, выберите изображение');
        return;
    }
    selectedFile = file;

    const reader = new FileReader();
    reader.onload = e => {
        document.getElementById('previewImage').src = e.target.result;
        document.getElementById('imageName').textContent = file.name;
        document.getElementById('previewCard').style.display = 'block';
        document.getElementById('dropArea').style.display = 'none';
        document.getElementById('confirmUploadButton').disabled = false;
    };
    reader.readAsDataURL(file);
}

// ─── Загрузка на сервер ───────────────────────────────────
async function uploadReceipt() {
    if (!selectedFile) return;

    const btn = document.getElementById('confirmUploadButton');
    const cancelBtn = document.getElementById('cancelUploadButton');
    const progressBar = document.getElementById('progressBar');
    const progressFill = document.getElementById('progressFill');
    const progressText = document.getElementById('progressText');

    btn.disabled = true;
    btn.textContent = 'Загружаем...';
    cancelBtn.style.display = 'inline-block';
    progressBar.style.display = 'flex';

    uploadAbortController = new AbortController();
    const formData = new FormData();
    formData.append('image', selectedFile);

    // Симуляция прогресса пока ждём ответа
    let prog = 0;
    const interval = setInterval(() => {
        prog = Math.min(prog + Math.random() * 15, 85);
        progressFill.style.width = prog + '%';
        progressText.textContent = Math.round(prog) + '%';
    }, 200);

    try {
        const resp = await fetch('/upload-image', {
            method: 'POST',
            headers: { 'Authorization': `Bearer ${window.accessToken || ''}` },
            body: formData,
            signal: uploadAbortController.signal,
        });

        clearInterval(interval);
        progressFill.style.width = '100%';
        progressText.textContent = '100%';

        if (!resp.ok) throw new Error(`Ошибка сервера: ${resp.status}`);
        const data = await resp.json();

        closeUploadModal();

        // Показываем результат в чате
        const resultText = data.assistant_answer || data.message || 'Чек успешно загружен!';
        window.displayMessage({ sender: 'assistant', text: resultText }, true);
        if (data.follow_ups && window.showFollowUpButtons) {
            window.showFollowUpButtons(data.follow_ups);
        }

    } catch (e) {
        clearInterval(interval);
        if (e.name === 'AbortError') {
            window.displayMessage({ sender: 'system', text: '⏹️ Загрузка отменена' }, true);
        } else {
            alert('Ошибка загрузки: ' + e.message);
        }
        closeUploadModal();
    } finally {
        uploadAbortController = null;
    }
}

// ─── Диаграмма расходов с периодом ───────────────────────
async function loadExpenseChart(period = null, chartType = null) {
    // Если период не передан — берём из селектора
    if (!period) {
        period = document.getElementById('chartPeriod')?.value || 'month';
    }
    if (!chartType) {
        chartType = document.getElementById('chartType')?.value || 'pie';
    }

    window.displayMessage({ sender: 'user', text: `📊 Диаграмма расходов (${getPeriodLabel(period)})` }, true);

    try {
        const resp = await fetch(
            `/expenses/chart?chart_type=${chartType}&period=${period}`,
            { headers: { 'Authorization': `Bearer ${window.accessToken || ''}` } }
        );
        if (!resp.ok) throw new Error('Ошибка загрузки диаграммы');
        const data = await resp.json();

        const label = data.period_label || getPeriodLabel(period);
        const count = data.count ?? '';
        const header = `**📊 Расходы: ${label}**${count ? ` (${count} чеков)` : ''}`;

        window.displayMessage({ sender: 'assistant', text: header }, true);
        window.displayMessage({ sender: 'system', type: 'img', text: data.chart_url }, true);

        // Follow-up кнопки для смены периода
        if (window.showFollowUpButtons) {
            window.showFollowUpButtons([
                'Диаграмма за сегодня',
                'Диаграмма за эту неделю',
                'Диаграмма за этот месяц',
                'Диаграмма за прошлый месяц',
            ]);
        }
    } catch (e) {
        window.displayMessage({ sender: 'error', text: `❌ ${e.message}` }, true);
    }
}

function getPeriodLabel(period) {
    const map = {
        all: 'Все время', today: 'Сегодня', week: 'Эта неделя',
        month: 'Этот месяц', last_month: 'Прошлый месяц', year: 'Этот год'
    };
    return map[period] || period;
}

// ─── Перехват запросов из чата ────────────────────────────
// Вызывается из sendMessageToAPI перед отправкой на сервер
window.interceptChatMessage = function(text) {
    const t = text.toLowerCase().trim();

    // Загрузка чека
    if (/загруз|сфотографир|добав.*чек|чек.*добав|upload.*receipt|фото.*чека/.test(t)) {
        openUploadModal();
        return true; // перехвачено, не отправлять на сервер
    }

    // Диаграмма расходов с периодом
    const chartMatch = t.match(
        /диаграмм|расходы.*граф|граф.*расход|покажи.*расход|chart.*expense/
    );
    if (chartMatch) {
        let period = 'month';
        if (/сегодня|today/.test(t)) period = 'today';
        else if (/недел|week/.test(t)) period = 'week';
        else if (/прошл.*месяц|last.month/.test(t)) period = 'last_month';
        else if (/этот.*месяц|текущ.*месяц|this.month/.test(t)) period = 'month';
        else if (/год|year/.test(t)) period = 'year';

        // YYYY-MM
        const ymMatch = t.match(/(\d{4})[- ](\d{1,2})/);
        if (ymMatch) period = `${ymMatch[1]}-${ymMatch[2].padStart(2, '0')}`;

        loadExpenseChart(period);
        return true;
    }

    return false; // не перехвачено — отправляем на сервер
};

// ─── Инициализация ────────────────────────────────────────
document.addEventListener('DOMContentLoaded', () => {
    // Кнопка загрузки чека
    document.getElementById('uploadImageButton')?.addEventListener('click', openUploadModal);

    // Закрытие — крестик
    document.getElementById('closeUploadModal')?.addEventListener('click', closeUploadModal);

    // Закрытие — клик по фону
    document.getElementById('uploadModal')?.addEventListener('click', e => {
        if (e.target.id === 'uploadModal') closeUploadModal();
    });

    // Кнопка отмены внутри модалки
    document.getElementById('cancelUploadButton')?.addEventListener('click', closeUploadModal);

    // Кнопка загрузки
    document.getElementById('confirmUploadButton')?.addEventListener('click', uploadReceipt);

    // Дроп-зона — клик
    document.getElementById('dropArea')?.addEventListener('click', () => {
        document.getElementById('imageInput').click();
    });

    // Дроп-зона — drag & drop
    const dropArea = document.getElementById('dropArea');
    if (dropArea) {
        dropArea.addEventListener('dragover', e => { e.preventDefault(); dropArea.classList.add('drag-over'); });
        dropArea.addEventListener('dragleave', () => dropArea.classList.remove('drag-over'));
        dropArea.addEventListener('drop', e => {
            e.preventDefault();
            dropArea.classList.remove('drag-over');
            const file = e.dataTransfer.files[0];
            if (file) handleFileSelect(file);
        });
    }

    // Файловый input
    document.getElementById('imageInput')?.addEventListener('change', e => {
        if (e.target.files[0]) handleFileSelect(e.target.files[0]);
    });

    // Кнопка диаграммы
    document.getElementById('loadChartButton')?.addEventListener('click', () => loadExpenseChart());

    // Закрытие по Escape
    document.addEventListener('keydown', e => {
        if (e.key === 'Escape' && document.getElementById('uploadModal')?.style.display !== 'none') {
            closeUploadModal();
        }
    });
});