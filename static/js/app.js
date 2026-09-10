const csrfToken = document.querySelector('meta[name="csrf-token"]')?.content;
const nativeFetch = window.fetch.bind(window);
window.fetch = (resource, options = {}) => {
    const headers = new Headers(options.headers || {});
    if (csrfToken) headers.set('X-CSRFToken', csrfToken);
    return nativeFetch(resource, {...options, headers});
};

const offlineQueueKey = 'routineTrackerOfflineQueue';
const syncStatus = document.getElementById('syncStatus');
const setSyncStatus = (text) => { if (syncStatus) syncStatus.textContent = text; };
const queueOfflineOperation = (url, options) => {
    const queue = JSON.parse(localStorage.getItem(offlineQueueKey) || '[]');
    if (!queue.some((operation) => operation.url === url && operation.options.body === options.body)) {
        const operationId = crypto.randomUUID();
        options.headers = {...options.headers, 'X-Idempotency-Key': operationId};
        queue.push({id: operationId, url, options, attempts: 0});
        localStorage.setItem(offlineQueueKey, JSON.stringify(queue));
    }
    setSyncStatus('Offline — changes will sync automatically.');
};
const syncOfflineQueue = async () => {
    const queue = JSON.parse(localStorage.getItem(offlineQueueKey) || '[]');
    if (!queue.length || !navigator.onLine) return;
    setSyncStatus('Syncing...');
    const remaining = [];
    for (const operation of queue) {
        try {
            const response = await nativeFetch(operation.url, {...operation.options, headers: {...operation.options.headers, 'X-CSRFToken': csrfToken}});
            if (response.status >= 400 && response.status < 500) continue;
            if (!response.ok) throw new Error('temporary failure');
        } catch (error) {
            operation.attempts += 1;
            if (operation.attempts < 5) remaining.push(operation);
        }
    }
    localStorage.setItem(offlineQueueKey, JSON.stringify(remaining));
    setSyncStatus(remaining.length ? 'Some changes are waiting to sync.' : 'All changes synced.');
};
window.addEventListener('online', syncOfflineQueue);
window.addEventListener('offline', () => setSyncStatus('Offline — changes will sync automatically.'));
syncOfflineQueue();

const modal = document.getElementById('addRoutineModal');

const volumeChart = document.getElementById('volumeChart');
const chartRange = document.getElementById('chartRange');
const chartState = document.getElementById('chartState');
const volumeTooltip = document.getElementById('volumeTooltip');
const volumeHoverDot = document.getElementById('volumeHoverDot');
const volumeHoverLine = document.getElementById('volumeHoverLine');
let volumeSeries = [];
let volumePoints = [];
let volumeChartWidth = 0;
const drawVolumeChart = (series) => {
    if (!volumeChart) return;
    const context = volumeChart.getContext('2d');
    const width = volumeChart.clientWidth || 640;
    const height = 150;
    const ratio = window.devicePixelRatio || 1;
    volumeChart.width = width * ratio;
    volumeChart.height = height * ratio;
    context.setTransform(ratio, 0, 0, ratio, 0, 0);
    context.clearRect(0, 0, width, height);
    const chartTop = 18;
    const chartBottom = height - 18;
    const rawValues = series.map((item) => item.expected ? (item.completed / item.expected) * 100 : 0);
    const smoothValues = rawValues.map((value, index) => {
        const weights = [0.08, 0.12, 0.16, 0.28, 0.16, 0.12, 0.08];
        return weights.reduce((total, weight, offset) => {
            const source = rawValues[Math.max(0, Math.min(rawValues.length - 1, index + offset - 3))] ?? value;
            return total + source * weight;
        }, 0);
    });
    const points = series.map((item, index) => [
        index * (width / Math.max(1, series.length - 1)),
        chartBottom - (smoothValues[index] / 100) * (chartBottom - chartTop),
    ]);
    const coordinates = points;
    volumeSeries = series;
    volumePoints = coordinates;
    volumeChartWidth = width;
    const drawWave = () => {
        if (!coordinates.length) return;
        context.moveTo(coordinates[0][0], coordinates[0][1]);
        coordinates.slice(1).forEach(([x, y], index) => {
            const currentIndex = index + 1;
            const previous = coordinates[currentIndex - 1];
            const beforePrevious = coordinates[Math.max(0, currentIndex - 2)];
            const next = coordinates[Math.min(coordinates.length - 1, currentIndex + 1)];
            const delta = y - previous[1];
            const limitTangent = (tangent) => {
                if (!delta || tangent * delta <= 0) return 0;
                return Math.sign(tangent) * Math.min(Math.abs(tangent), Math.abs(delta) * 3);
            };
            const tangentBefore = limitTangent((y - beforePrevious[1]) / 2);
            const tangentAfter = limitTangent((next[1] - previous[1]) / 2);
            const controlOne = [previous[0] + (x - previous[0]) / 3, previous[1] + (delta ? tangentBefore / 3 : 0)];
            const controlTwo = [x - (x - previous[0]) / 3, y - (delta ? tangentAfter / 3 : 0)];
            context.bezierCurveTo(
                controlOne[0], controlOne[1],
                controlTwo[0], controlTwo[1],
                x, y,
            );
        });
    };
    context.strokeStyle = '#e8ebfa';
    context.lineWidth = 1;
    [0, .25, .5, .75, 1].forEach((line) => {
        const y = 12 + line * (height - 24);
        context.beginPath();
        context.moveTo(0, y);
        context.lineTo(width, y);
        context.stroke();
    });
    const gradient = context.createLinearGradient(0, 0, 0, height);
    gradient.addColorStop(0, 'rgba(99,102,241,.24)');
    gradient.addColorStop(1, 'rgba(99,102,241,0)');
    context.beginPath();
    drawWave();
    context.lineTo(width, height);
    context.lineTo(0, height);
    context.closePath();
    context.fillStyle = gradient;
    context.fill();
    context.beginPath();
    drawWave();
    context.strokeStyle = '#6366f1';
    context.lineWidth = 2.5;
    context.lineJoin = 'round';
    context.lineCap = 'round';
    context.stroke();
};
const showVolumeTooltip = (event) => {
    if (!volumeTooltip || !volumeChart || !volumeSeries.length) return;
    const bounds = volumeChart.getBoundingClientRect();
    const x = Math.max(0, Math.min(volumeChartWidth, event.clientX - bounds.left));
    const index = Math.max(0, Math.min(volumeSeries.length - 1, Math.round((x / volumeChartWidth) * (volumeSeries.length - 1))));
    const item = volumeSeries[index];
    const point = volumePoints[index];
    const completed = Number(item.completed || 0);
    const expected = Number(item.expected || 0);
    const percent = expected ? Math.round((completed / expected) * 100) : 0;
    const wrapBounds = volumeChart.parentElement.getBoundingClientRect();
    const visualX = bounds.left - wrapBounds.left + point[0];
    const visualY = bounds.top - wrapBounds.top + point[1];
    volumeTooltip.textContent = `${item.date} · ${completed}/${expected} minutes · ${percent}%`;
    volumeTooltip.hidden = false;
    volumeTooltip.style.left = `${Math.min(Math.max(8, visualX - 70), Math.max(8, wrapBounds.width - 150))}px`;
    if (volumeHoverDot) {
        volumeHoverDot.hidden = false;
        volumeHoverDot.style.left = `${visualX - 5}px`;
        volumeHoverDot.style.top = `${visualY - 5}px`;
    }
    if (volumeHoverLine) {
        volumeHoverLine.hidden = false;
        volumeHoverLine.style.left = `${visualX}px`;
    }
};
const hideVolumeTooltip = () => {
    if (volumeTooltip) volumeTooltip.hidden = true;
    if (volumeHoverDot) volumeHoverDot.hidden = true;
    if (volumeHoverLine) volumeHoverLine.hidden = true;
};
volumeChart?.addEventListener('pointermove', showVolumeTooltip);
volumeChart?.addEventListener('pointerdown', showVolumeTooltip);
volumeChart?.addEventListener('pointerleave', hideVolumeTooltip);
const loadAnalytics = async () => {
    if (!volumeChart || !chartRange) return;
    chartState.textContent = 'Loading activity...';
    const response = await fetch(`/api/analytics?days=${chartRange.value}&premium=1`);
    if (!response.ok) { chartState.textContent = 'Unable to load activity.'; return; }
    const data = await response.json();
    if (!data.series.some((item) => item.expected)) { chartState.textContent = 'No scheduled activity in this period yet.'; return; }
    chartState.textContent = '';
    drawVolumeChart(data.series);
};
chartRange?.addEventListener('change', loadAnalytics);
window.addEventListener('resize', loadAnalytics);
loadAnalytics();

const insightChart = document.getElementById('insightChart');
const insightPercent = document.getElementById('insightPercent');
const insightRing = document.getElementById('insightRing');
const insightDays = document.getElementById('insightDays');
const insightTitle = document.getElementById('insightTitle');
const insightSubtitle = document.getElementById('insightSubtitle');
const insightPeriod = document.getElementById('insightPeriod');
const insightAxis = document.getElementById('insightAxis');
const insightTooltip = document.getElementById('insightTooltip');
const insightHoverDot = document.getElementById('insightHoverDot');
const insightHoverLine = document.getElementById('insightHoverLine');
let insightPoints = [];
let insightSeries = [];
let insightChartWidth = 0;
let insightSelectedDays = 30;
const drawInsight = (series, days) => {
    if (!insightChart) return;
    const context = insightChart.getContext('2d');
    const width = insightChart.clientWidth || 620;
    const height = insightChart.clientHeight || 190;
    const ratio = window.devicePixelRatio || 1;
    insightChart.width = width * ratio;
    insightChart.height = height * ratio;
    context.setTransform(ratio, 0, 0, ratio, 0, 0);
    context.clearRect(0, 0, width, height);
    const rawValues = series.map((item) => item.expected ? (item.completed / item.expected) * 100 : 0);
    const points = rawValues.length > 1 ? rawValues : [0, 0];
    const step = width / Math.max(1, points.length - 1);
    const chartTop = 20;
    const chartBottom = height - 20;
    const coords = points.map((value, index) => [index * step, chartBottom - (value / 100) * (chartBottom - chartTop)]);
    insightPoints = coords;
    insightSeries = series;
    insightChartWidth = width;
    context.strokeStyle = '#e8ebfa';
    context.lineWidth = 1;
    [0, .25, .5, .75, 1].forEach((line) => {
        const y = chartTop + line * (chartBottom - chartTop);
        context.beginPath();
        context.moveTo(0, y);
        context.lineTo(width, y);
        context.stroke();
    });
    const drawWave = () => {
        context.moveTo(coords[0][0], coords[0][1]);
        coords.slice(1).forEach(([x, y], index) => {
            const currentIndex = index + 1;
            const previous = coords[currentIndex - 1];
            const beforePrevious = coords[Math.max(0, currentIndex - 2)];
            const next = coords[Math.min(coords.length - 1, currentIndex + 1)];
            const delta = y - previous[1];
            const limitTangent = (tangent) => {
                if (!delta || tangent * delta <= 0) return 0;
                return Math.sign(tangent) * Math.min(Math.abs(tangent), Math.abs(delta) * 3);
            };
            const tangentBefore = limitTangent((y - beforePrevious[1]) / 2);
            const tangentAfter = limitTangent((next[1] - previous[1]) / 2);
            context.bezierCurveTo(
                previous[0] + (x - previous[0]) / 3,
                previous[1] + tangentBefore / 3,
                x - (x - previous[0]) / 3,
                y - tangentAfter / 3,
                x,
                y,
            );
        });
    };
    const showInsightTooltip = (event) => {
        if (!insightTooltip || !insightChart || !insightSeries.length) return;
        const bounds = insightChart.getBoundingClientRect();
        const x = Math.max(0, Math.min(insightChartWidth, event.clientX - bounds.left));
        const index = Math.max(0, Math.min(insightSeries.length - 1, Math.round((x / insightChartWidth) * (insightSeries.length - 1))));
        const item = insightSeries[index];
        const completed = Number(item.completed || 0);
        const total = Number(item.expected || 0);
        const chartBounds = insightChart.getBoundingClientRect();
        const wrapBounds = insightChart.parentElement.getBoundingClientRect();
        const visualX = chartBounds.left - wrapBounds.left + insightPoints[index][0];
        const visualY = chartBounds.top - wrapBounds.top + insightPoints[index][1];
        insightTooltip.textContent = `${item.date} · ${completed}/${total} minutes · ${total ? Math.round(completed / total * 100) : 0}%`;
        insightTooltip.hidden = false;
        insightTooltip.style.left = `${Math.min(Math.max(8, visualX - 70), Math.max(8, wrapBounds.width - 150))}px`;
        if (insightHoverDot) {
            insightHoverDot.hidden = false;
            insightHoverDot.style.left = `${visualX - 5}px`;
            insightHoverDot.style.top = `${visualY - 5}px`;
        }
        if (insightHoverLine) {
            insightHoverLine.hidden = false;
            insightHoverLine.style.left = `${visualX}px`;
        }
    };
    insightChart?.addEventListener('pointermove', showInsightTooltip);
    insightChart?.addEventListener('pointerleave', () => { if (insightTooltip) insightTooltip.hidden = true; if (insightHoverDot) insightHoverDot.hidden = true; if (insightHoverLine) insightHoverLine.hidden = true; });
    insightChart?.addEventListener('pointerdown', showInsightTooltip);
    const gradient = context.createLinearGradient(0, 0, 0, height);
    gradient.addColorStop(0, 'rgba(99,102,241,.24)');
    gradient.addColorStop(1, 'rgba(99,102,241,0)');
    context.beginPath();
    drawWave();
    context.lineTo(width, height); context.lineTo(0, height); context.closePath(); context.fillStyle = gradient; context.fill();
    context.beginPath();
    drawWave();
    context.strokeStyle = '#6366f1'; context.lineWidth = 2.5; context.lineJoin = 'round'; context.stroke();
    insightAxis.replaceChildren(...[series[0]?.date, series[Math.floor(series.length / 2)]?.date, series[series.length - 1]?.date].filter(Boolean).map((value) => { const label = document.createElement('span'); label.textContent = value.slice(5); return label; }));
    const expected = series.reduce((sum, item) => sum + item.expected, 0);
    const completed = series.reduce((sum, item) => sum + item.completed, 0);
    const percent = expected ? Math.round((completed / expected) * 100) : 0;
    insightPercent.textContent = `${percent}%`;
    insightRing.style.setProperty('--insight-progress', `${percent}%`);
    insightDays.textContent = `${completed} / ${expected} minutes`;
    insightTitle.textContent = days === 7 ? 'Week Insight' : 'Month Insight';
    insightSubtitle.textContent = days === 7 ? 'Your progress this week at a glance.' : 'Your progress this month at a glance.';
    insightPeriod.innerHTML = `${days === 7 ? 'WEEK' : 'MONTH'}<br>COMPLETED`;
};
const loadInsight = async (days = 30) => {
    if (!insightChart) return;
    insightSelectedDays = days;
    const query = days === 30 ? `month=${new Date().toISOString().slice(0, 7)}` : `days=${days}`;
    const response = await fetch(`/api/analytics?${query}`);
    if (!response.ok) return;
    const data = await response.json();
    drawInsight(data.series || [], days);
};
document.querySelectorAll('[data-insight-range]').forEach((button) => button.addEventListener('click', () => {
    const days = Number(button.dataset.insightRange);
    document.querySelectorAll('[data-insight-range]').forEach((item) => item.classList.toggle('is-active', item === button));
    loadInsight(days);
}));
window.addEventListener('resize', () => loadInsight(insightSelectedDays));
loadInsight();

const openButton = document.getElementById('openAddModal');
const closeButton = document.getElementById('closeAddModal');
const routineCategorySelect = document.getElementById('routineCategorySelect');
const routineCategoryValue = document.getElementById('routineCategoryValue');
const newRoutineCategoryField = document.getElementById('newRoutineCategoryField');
const newRoutineCategory = document.getElementById('newRoutineCategory');

const updateRoutineCategoryState = (focusNewCategory = false) => {
    const isNewCategory = routineCategorySelect?.value === '__new__';
    if (routineCategoryValue && !isNewCategory) routineCategoryValue.value = routineCategorySelect.value;
    if (newRoutineCategoryField) newRoutineCategoryField.hidden = !isNewCategory;
    if (newRoutineCategory) {
        newRoutineCategory.required = isNewCategory;
        if (isNewCategory) routineCategoryValue.value = '';
        if (isNewCategory && focusNewCategory) newRoutineCategory.focus();
    }
};

routineCategorySelect?.addEventListener('change', () => updateRoutineCategoryState(true));
updateRoutineCategoryState();

newRoutineCategory?.closest('form')?.addEventListener('submit', (event) => {
    if (routineCategorySelect?.value === '__new__') {
        const value = newRoutineCategory.value.trim();
        if (!value) {
            event.preventDefault();
            newRoutineCategory.focus();
            return;
        }
        routineCategoryValue.value = value;
    }
});

if (modal && openButton) {
    const openModal = () => {
        modal.classList.remove('hidden');
        modal.setAttribute('aria-hidden', 'false');
        updateRoutineCategoryState(routineCategorySelect?.value === '__new__');
    };

    const closeModal = () => {
        modal.classList.add('hidden');
        modal.setAttribute('aria-hidden', 'true');
    };

    openButton.addEventListener('click', openModal);
    closeButton?.addEventListener('click', closeModal);

    modal.addEventListener('click', (event) => {
        if (event.target.matches('[data-close-modal]')) {
            closeModal();
        }
    });

    document.addEventListener('keydown', (event) => {
        if (event.key === 'Escape' && !modal.classList.contains('hidden')) {
            closeModal();
        }
    });
}

if ('serviceWorker' in navigator) {
    window.addEventListener('load', () => navigator.serviceWorker.register('/static/sw.js'));
}

const vapidPublicKey = document.querySelector('meta[name="vapid-public-key"]')?.content;
const urlBase64ToUint8Array = (value) => Uint8Array.from(atob(value.replace(/-/g, '+').replace(/_/g, '/')), (char) => char.charCodeAt(0));
const registerPushSubscription = async () => {
    if (!vapidPublicKey || !('serviceWorker' in navigator) || !('PushManager' in window) || Notification.permission !== 'granted') return;
    const registration = await navigator.serviceWorker.ready;
    const subscription = await registration.pushManager.subscribe({userVisibleOnly: true, applicationServerKey: urlBase64ToUint8Array(vapidPublicKey)});
    await fetch('/api/push-subscriptions', {method: 'POST', headers: {'Content-Type': 'application/json'}, body: subscription.toJSON()});
};

const updateGlobalStats = (stats) => {
    if (!stats) return;

    const weekGoal = Math.max(1, stats.week_expected || 0);
    const weekBadge = document.getElementById('weekProgressBadge');
    const weekFill = document.getElementById('weekProgressFill');
    const weekPercent = document.getElementById('weekProgressPercent');
    const weekToday = document.getElementById('weekProgressToday');
    const weekRing = document.getElementById('weekProgressRing');
    const weekRingValue = document.getElementById('weekProgressRingValue');

    if (weekBadge) {
        weekBadge.textContent = `${stats.week_total} / ${weekGoal} done`;
    }
    if (weekFill) {
        weekFill.style.width = `${stats.week_percent}%`;
    }
    if (weekPercent) {
        weekPercent.textContent = `${stats.week_percent}% completed`;
    }
    if (weekToday) {
        weekToday.textContent = `${stats.today_completed} today`;
    }
    if (weekRing) {
        weekRing.style.setProperty('--progress', `${stats.week_percent}%`);
    }
    if (weekRingValue) {
        weekRingValue.textContent = `${stats.week_percent}%`;
    }
};

const updateRoutineCard = (card, payload) => {
    if (!card || !payload?.routine) return;

    const { routine, day } = payload;
    const targetMinutes = Number(routine.today_goal_minutes || card.dataset.targetMinutes || 0);
    if (day?.iso === new Date().toISOString().slice(0, 10)) {
        card.dataset.todayMinutes = String(Math.max(0, Number(routine.today_minutes || 0)));
    } else if (routine.today_minutes !== undefined) {
        card.dataset.todayMinutes = String(Math.max(0, Number(routine.today_minutes || 0) - Number(routine.task_minutes || 0)));
    }

    card.classList.toggle('done', routine.today_done);

    const statusLabel = card.querySelector('[data-status-label]');
    if (statusLabel) {
        statusLabel.textContent = routine.today_done ? 'Done today' : 'Open';
        statusLabel.classList.toggle('status-done', routine.today_done);
    }

    const progressLabel = card.querySelector('[data-week-progress-label]');
    if (progressLabel) {
        progressLabel.textContent = `${targetMinutes} min goal • ${routine.week_progress}%`;
    }
    const trackButton = card.querySelector('[data-track-routine-id]');
    if (trackButton) {
        trackButton.dataset.trackGoalMinutes = String(targetMinutes);
        trackButton.dataset.trackTodayMinutes = String(Math.max(
            0,
            Number(routine.today_minutes || 0) - Number(routine.task_minutes || 0),
        ));
    }

    const progressValue = card.querySelector('[data-week-progress-value]');
    if (progressValue) {
        progressValue.textContent = `${routine.week_progress}%`;
    }

    if (day?.iso) {
        const dayBox = card.querySelector(`.day-box[data-day="${day.iso}"]`);
        const checkbox = dayBox?.querySelector('.day-checkbox');
        const hiddenInput = dayBox?.querySelector('.day-minute-input');
        const timeButton = dayBox?.querySelector('.mini-time-btn');
        const progressLabel = dayBox?.querySelector('[data-day-progress]');

        if (dayBox) {
            dayBox.classList.toggle('is-done', day.checked);
        }
        if (checkbox) {
            checkbox.checked = day.checked;
        }
        if (hiddenInput) {
            hiddenInput.value = String(day.minutes_done || 0);
        }
        if (timeButton) {
            timeButton.dataset.value = String(day.minutes_done || 0);
        }
        if (progressLabel) {
            progressLabel.textContent = `${day.progress_percent || 0}%`;
        }
    }
};

const playCheckboxAnimation = (checkbox) => {
    if (!checkbox) return;
    checkbox.classList.remove('animate-check');
    void checkbox.offsetWidth;
    checkbox.classList.add('animate-check');
};

const saveDayEntry = async (card, dayIso, checked, minutes) => {
    const routineId = card?.dataset.routineId;
    if (!routineId || !dayIso) return null;

    const response = await fetch(`/api/log_day/${routineId}`, {
        method: 'POST',
        headers: {
            'Content-Type': 'application/json',
        },
        body: JSON.stringify({
            date: dayIso,
            checked,
            minutes,
        }),
    });

    if (!response.ok) {
        queueOfflineOperation(`/api/log_day/${routineId}`, {
            method: 'POST',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({date: dayIso, checked, minutes}),
        });
        throw new Error('Failed to save day entry');
    }

    return response.json();
};

const updateTaskCount = (list) => {
    const count = list?.querySelector('[data-task-count]');
    const tasks = list ? [...list.querySelectorAll('.task-checkbox')] : [];
    if (count) {
        count.textContent = `${tasks.filter((task) => task.checked).length} / ${tasks.length}`;
    }
};
const syncRoutineTasks = (card, completed) => {
    card?.querySelectorAll('.task-checkbox').forEach((checkbox) => {
        checkbox.checked = completed;
        checkbox.closest('.task-item')?.classList.toggle('is-complete', completed);
    });
    updateTaskCount(card?.querySelector('[data-task-list]'));
};

const updateRoutineTaskProgress = (card) => {
    const list = card?.querySelector('[data-task-list]');
    const progress = card?.querySelector('[data-track-progress]');
    if (!list || !progress) return;
    const todayIso = new Date().toISOString().slice(0, 10);
    const todayInput = card.querySelector(`.day-box[data-day="${todayIso}"] .day-minute-input`);
    const baseMinutes = todayInput ? Number(todayInput.value || 0) : Number(card.dataset.todayMinutes || 0);
    const routineTarget = Number(card.dataset.targetMinutes || 0);
    const taskTarget = [...list.querySelectorAll('.task-item')]
        .reduce((sum, item) => sum + (Number(item.dataset.taskMinutes || 0) || routineTarget), 0);
    const taskMinutes = [...list.querySelectorAll('.task-item')]
        .filter((item) => item.querySelector('.task-checkbox')?.checked)
        .reduce((sum, item) => sum + (Number(item.dataset.taskMinutes || 0) || routineTarget), 0);
    const target = taskTarget > 0 ? taskTarget : routineTarget;
    const total = taskTarget > 0 ? taskMinutes : baseMinutes;
    const percent = target ? Math.min(100, Math.round((total / target) * 100)) : 0;
    const trackButton = card.querySelector('[data-track-routine-id]');
    if (trackButton) {
        trackButton.dataset.trackGoalMinutes = String(target);
        trackButton.dataset.trackTodayMinutes = String(total);
    }
    progress.textContent = `Today: ${total} / ${target} min (${percent}%)`;
};

const taskModal = document.getElementById('taskModal');
const taskForm = document.getElementById('taskForm');
const getCardDate = (card) => card?.dataset.selectedDate || document.querySelector('[data-selected-date]')?.value;
const fetchRoutineCard = async (routineId, selectedDate) => {
    const url = new URL(window.location.href);
    url.searchParams.set('date', selectedDate);
    const response = await fetch(url.toString(), {headers: {'X-Requested-With': 'XMLHttpRequest'}});
    if (!response.ok) throw new Error('Unable to load the selected date.');
    const documentHtml = new DOMParser().parseFromString(await response.text(), 'text/html');
    return documentHtml.querySelector(`.routine-card[data-routine-id="${routineId}"]`);
};
const replaceRoutineCard = (routineId, selectedDate, card) => {
    const replacement = card?.cloneNode(true);
    const current = document.querySelector(`.routine-card[data-routine-id="${routineId}"]`);
    if (!replacement || !current) return;
    replacement.dataset.dynamicDateCard = 'true';
    replacement.dataset.selectedDate = selectedDate;
    current.replaceWith(replacement);
};

document.querySelector('[data-selected-date]')?.addEventListener('change', async (event) => {
    const selectedDate = event.target.value;
    if (!selectedDate) return;
    const grid = document.querySelector('.routine-grid');
    if (!grid) return;
    const url = new URL(window.location.href);
    url.searchParams.set('date', selectedDate);
    window.history.replaceState({}, '', url);
    const response = await fetch(url.toString(), {headers: {'X-Requested-With': 'XMLHttpRequest'}});
    if (!response.ok) return;
    const page = new DOMParser().parseFromString(await response.text(), 'text/html');
    const replacementGrid = page.querySelector('.routine-grid');
    if (!replacementGrid) return;
    replacementGrid.querySelectorAll('.routine-card').forEach((card) => {
        card.dataset.dynamicDateCard = 'true';
        card.dataset.selectedDate = selectedDate;
    });
    grid.replaceWith(replacementGrid);
});

document.addEventListener('click', async (event) => {
    const dayLink = event.target.closest('.day-box');
    if (!dayLink) return;
    const card = dayLink.closest('.routine-card');
    if (!card) return;
    event.preventDefault();
    try {
        const selectedDate = dayLink.dataset.day;
        const replacement = await fetchRoutineCard(card.dataset.routineId, selectedDate);
        replaceRoutineCard(card.dataset.routineId, selectedDate, replacement);
    } catch (error) {
        setSyncStatus(error.message);
    }
});
const closeTaskModal = () => {
    taskModal?.classList.add('hidden');
    taskModal?.setAttribute('aria-hidden', 'true');
};
document.querySelectorAll('[data-open-task-modal]').forEach((button) => {
    button.addEventListener('click', () => {
        document.getElementById('taskRoutineId').value = button.dataset.routineId;
        document.getElementById('taskTitle').value = '';
        document.getElementById('taskMinutes').value = '20';
        taskModal?.classList.remove('hidden');
        taskModal?.setAttribute('aria-hidden', 'false');
        document.getElementById('taskTitle')?.focus();
    });
});
document.getElementById('closeTaskModal')?.addEventListener('click', closeTaskModal);
document.querySelector('[data-close-task-modal]')?.addEventListener('click', closeTaskModal);
taskForm?.addEventListener('submit', async (event) => {
        event.preventDefault();
        const title = document.getElementById('taskTitle')?.value.trim();
        const targetMinutes = Number(document.getElementById('taskMinutes')?.value || 0);
        const routineId = document.getElementById('taskRoutineId')?.value;
        if (!title || !routineId) return;
        const card = document.querySelector(`.routine-card[data-routine-id="${routineId}"]`);
        if (!card) return;

        const response = await fetch(`/api/routines/${routineId}/tasks`, {
            method: 'POST',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({
                title,
                target_minutes: targetMinutes,
                task_date: getCardDate(card),
            }),
        });
        if (!response.ok) return;

        const payload = await response.json();
        const list = card.querySelector('[data-task-list]');
        const empty = list?.querySelector('.task-empty');
        if (empty) empty.remove();
        if (list && payload.task) {
            const item = document.createElement('div');
            item.className = 'task-item';
            item.dataset.taskId = payload.task.id;
            item.dataset.taskMinutes = payload.task.target_minutes;
            item.innerHTML = '<label><input type="checkbox" class="task-checkbox"><span></span></label>';
            item.querySelector('span').textContent = `${payload.task.title}${payload.task.target_minutes ? ` (${payload.task.target_minutes} min)` : ''}`;
            list.appendChild(item);
            updateTaskCount(list);
            updateRoutineTaskProgress(card);
            closeTaskModal();
        }
});

document.querySelectorAll('.task-list').forEach((list) => {
    list.addEventListener('click', async (event) => {
        const button = event.target.closest('.task-move');
        if (!button) return;
        const task = button.closest('.task-item');
        if (!task) return;
        const response = await fetch(`/api/tasks/${task.dataset.taskId}/move`, {
            method: 'POST',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({direction: button.dataset.direction}),
        });

        document.addEventListener('click', (event) => {
            const button = event.target.closest('[data-open-task-modal]');
            if (!button || !button.closest('.routine-card[data-dynamic-date-card]')) return;
            document.getElementById('taskRoutineId').value = button.dataset.routineId;
            document.getElementById('taskTitle').value = '';
            document.getElementById('taskMinutes').value = '20';
            taskModal?.classList.remove('hidden');
            taskModal?.setAttribute('aria-hidden', 'false');
            document.getElementById('taskTitle')?.focus();
        });

        if (response.ok) window.location.reload();
    });

    list.addEventListener('change', async (event) => {
        if (!event.target.matches('.task-checkbox') || event.target.closest('.routine-card[data-dynamic-date-card]')) return;
        const checkbox = event.target;
        const item = checkbox.closest('.task-item');
        const taskId = item?.dataset.taskId;
        if (!taskId) return;
        const intendedCompleted = checkbox.checked;
        checkbox.disabled = true;
        const response = await fetch(`/api/tasks/${taskId}/toggle`, {
            method: 'POST',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({
                completed: checkbox.checked,
                date: document.querySelector('[data-selected-date]')?.value,
            }),
        });
        checkbox.disabled = false;
        if (!response.ok) {
            checkbox.checked = !checkbox.checked;
            queueOfflineOperation(`/api/tasks/${taskId}/toggle`, {
                method: 'POST',
                headers: {'Content-Type': 'application/json'},
                body: JSON.stringify({
                    completed: intendedCompleted,
                    date: document.querySelector('[data-selected-date]')?.value,
                }),
            });
            return;
        }
        const payload = await response.json();
        item.classList.toggle('is-complete', checkbox.checked);
        updateTaskCount(list);
        const card = item.closest('.routine-card');
        if (payload.routine) updateRoutineCard(card, payload);
        updateRoutineTaskProgress(card);
        updateGlobalStats(payload.stats);
        await loadInsight(insightSelectedDays);
        await loadAnalytics();
    });
});

document.addEventListener('change', async (event) => {
    if (!event.target.matches('.task-checkbox') || !event.target.closest('.routine-card[data-dynamic-date-card]')) return;
    const checkbox = event.target;
    const item = checkbox.closest('.task-item');
    const card = checkbox.closest('.routine-card');
    const taskId = item?.dataset.taskId;
    if (!taskId || !card) return;
    checkbox.disabled = true;
    const response = await fetch(`/api/tasks/${taskId}/toggle`, {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({completed: checkbox.checked, date: getCardDate(card)}),
    });
    checkbox.disabled = false;
    if (!response.ok) {
        checkbox.checked = !checkbox.checked;
        return;
    }
    const payload = await response.json();
    item.classList.toggle('is-complete', checkbox.checked);
    updateTaskCount(card.querySelector('[data-task-list]'));
    if (payload.routine) updateRoutineCard(card, payload);
    updateRoutineTaskProgress(card);
    updateGlobalStats(payload.stats);
    await loadInsight(insightSelectedDays);
    await loadAnalytics();
});

const editRoutineModal = document.getElementById('editRoutineModal');
const editRoutineForm = document.getElementById('editRoutineForm');
const editRoutineButtons = document.querySelectorAll('[data-routine-action="edit"]');

if (editRoutineModal && editRoutineForm) {
    const closeEditModal = () => {
        editRoutineModal.classList.add('hidden');
        editRoutineModal.setAttribute('aria-hidden', 'true');
    };
    const openEditModal = (button) => {
        document.getElementById('editRoutineId').value = button.dataset.routineId;
        document.getElementById('editRoutineTitle').value = button.dataset.routineTitle || '';
        document.getElementById('editRoutineDescription').value = button.dataset.routineDescription || '';
        document.getElementById('editRoutineCategory').value = button.dataset.routineCategory || '';
        document.getElementById('editRoutineTarget').value = button.dataset.routineTarget || '20';
        const schedule = button.dataset.routineSchedule ? JSON.parse(button.dataset.routineSchedule) : null;
        document.getElementById('editScheduleFrequency').value = schedule?.frequency || 'daily';
        document.getElementById('editScheduleTime').value = schedule?.time_of_day || '';
        document.getElementById('editScheduleWeekdays').value = schedule?.weekdays || '';
        document.getElementById('editScheduleTimezone').value = schedule?.timezone || Intl.DateTimeFormat().resolvedOptions().timeZone || 'UTC';
        document.getElementById('editScheduleEnabled').checked = schedule ? Boolean(schedule.enabled) : true;
        editRoutineModal.classList.remove('hidden');
        editRoutineModal.setAttribute('aria-hidden', 'false');
        document.getElementById('editRoutineTitle').focus();
    };

    editRoutineButtons.forEach((button) => button.addEventListener('click', () => openEditModal(button)));
    document.getElementById('closeEditModal')?.addEventListener('click', closeEditModal);
    editRoutineModal.addEventListener('click', (event) => {
        if (event.target.matches('[data-close-edit-modal]')) closeEditModal();
    });
    editRoutineForm.addEventListener('submit', async (event) => {
        event.preventDefault();
        const id = document.getElementById('editRoutineId').value;
        const response = await fetch(`/api/routines/${id}`, {
            method: 'PATCH',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({
                title: document.getElementById('editRoutineTitle').value,
                description: document.getElementById('editRoutineDescription').value,
                category: document.getElementById('editRoutineCategory').value,
                target_minutes: document.getElementById('editRoutineTarget').value,
            }),
        });
        if (response.ok) {
            await fetch(`/api/routines/${id}/schedule`, {
                method: 'POST',
                headers: {'Content-Type': 'application/json'},
                body: JSON.stringify({
                    frequency: document.getElementById('editScheduleFrequency').value,
                    time_of_day: document.getElementById('editScheduleTime').value,
                    weekdays: document.getElementById('editScheduleWeekdays').value.split(',').map((value) => value.trim()).filter(Boolean),
                    timezone: document.getElementById('editScheduleTimezone').value.trim() || 'UTC',
                    enabled: document.getElementById('editScheduleEnabled').checked,
                }),
            });
            window.location.reload();
        }
    });
}

const categoryModal = document.getElementById('categoryModal');
const categoryForm = document.getElementById('categoryForm');
if (categoryModal && categoryForm) {
    const closeCategoryModal = () => {
        categoryModal.classList.add('hidden');
        categoryModal.setAttribute('aria-hidden', 'true');
    };
    document.getElementById('openCategoryModal')?.addEventListener('click', () => {
        categoryModal.classList.remove('hidden');
        categoryModal.setAttribute('aria-hidden', 'false');
        document.getElementById('categoryName').focus();
    });
    document.getElementById('closeCategoryModal')?.addEventListener('click', closeCategoryModal);
    categoryModal.addEventListener('click', (event) => {
        if (event.target.matches('[data-close-category-modal]')) closeCategoryModal();
    });
    categoryForm.addEventListener('submit', async (event) => {
        event.preventDefault();
        const response = await fetch('/api/categories', {
            method: 'POST',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({name: document.getElementById('categoryName').value}),
        });
        if (response.ok) window.location.reload();
    });
}

document.querySelectorAll('[data-category-delete]').forEach((button) => {
    button.addEventListener('click', async () => {
        const categoryName = button.dataset.categoryName;
        if (!window.confirm(`Delete "${categoryName}"? Routines in this category will be moved to General.`)) return;
        const response = await fetch(`/api/categories/${button.dataset.categoryDelete}`, {
            method: 'DELETE',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({replacement: 'General'}),
        });
        if (response.ok) {
            window.location.reload();
        } else {
            const payload = await response.json().catch(() => ({}));
            window.alert(payload.error || 'The category could not be deleted.');
        }
    });
});

const reminderModal = document.getElementById('reminderModal');
const reminderForm = document.getElementById('reminderForm');
if (reminderModal && reminderForm) {
    const closeReminderModal = () => {
        reminderModal.classList.add('hidden');
        reminderModal.setAttribute('aria-hidden', 'true');
    };
    document.getElementById('openReminderModal')?.addEventListener('click', () => {
        reminderModal.classList.remove('hidden');
        reminderModal.setAttribute('aria-hidden', 'false');
        document.getElementById('reminderTitle').focus();
    });
    document.getElementById('closeReminderModal')?.addEventListener('click', closeReminderModal);
    reminderModal.addEventListener('click', (event) => {
        if (event.target.matches('[data-close-reminder-modal]')) closeReminderModal();
    });
    reminderForm.addEventListener('submit', async (event) => {
        event.preventDefault();
        const response = await fetch('/api/reminders', {
            method: 'POST',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({
                title: document.getElementById('reminderTitle').value,
                due_at: new Date(document.getElementById('reminderDueAt').value).toISOString(),
                recurrence: document.getElementById('reminderRecurrence').value,
                timezone: Intl.DateTimeFormat().resolvedOptions().timeZone || 'UTC',
            }),
        });
        if (response.ok) closeReminderModal();
    });
}

const syncNotificationPreference = async (permissionState) => {
    await fetch('/api/notification-preferences', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({enabled: permissionState === 'granted', permission_state: permissionState}),
    });
};

const pollReminders = async () => {
    if (!('Notification' in window)) return;
    const response = await fetch('/api/reminders');
    if (!response.ok) return;
    const payload = await response.json();
    const now = Date.now();
    const notified = JSON.parse(sessionStorage.getItem('notifiedReminders') || '[]');
    for (const reminder of payload.reminders || []) {
        const dueAt = new Date(reminder.snoozed_until || reminder.due_at).getTime();
        if (dueAt > now || Notification.permission !== 'granted' || notified.includes(reminder.id)) continue;
        notified.push(reminder.id);
        sessionStorage.setItem('notifiedReminders', JSON.stringify(notified.slice(-100)));
        const notification = new Notification(reminder.title, {body: 'Routine Tracker reminder'});
        notification.onclick = async () => {
            await fetch(`/api/reminders/${reminder.id}/complete`, {method: 'POST'});
            notification.close();
        };
    }
};

if ('Notification' in window) {
    document.addEventListener('click', async (event) => {
        if (!event.target.closest('#openReminderModal')) return;
        if (Notification.permission === 'default') {
            const permission = await Notification.requestPermission();
            await syncNotificationPreference(permission);
            if (permission === 'granted') await registerPushSubscription();
        }
    });
    window.setInterval(pollReminders, 30000);
    pollReminders();
}

const formatTimer = (seconds) => `${Math.floor(seconds / 60)}:${String(seconds % 60).padStart(2, '0')}`;
const updateRoutineProgress = (button, elapsedSeconds = 0) => {
    const card = button.closest('.routine-card');
    const routineTarget = Number(button.dataset.trackRoutineMinutes || 0);
    const items = [...(card?.querySelectorAll('.task-item') || [])];
    const taskTarget = items.reduce(
        (sum, item) => sum + (Number(item.dataset.taskMinutes || 0) || routineTarget),
        0,
    );
    const taskMinutes = items
        .filter((item) => item.querySelector('.task-checkbox')?.checked)
        .reduce((sum, item) => sum + (Number(item.dataset.taskMinutes || 0) || routineTarget), 0);
    const targetMinutes = taskTarget > 0 ? taskTarget : routineTarget;
    const savedMinutes = taskTarget > 0 ? taskMinutes : Number(button.dataset.trackTodayMinutes || 0);
    const totalMinutes = savedMinutes + (elapsedSeconds / 60);
    const percent = targetMinutes ? Math.min(100, Math.round((totalMinutes / targetMinutes) * 100)) : 0;
    const progress = button.parentElement?.querySelector('[data-track-progress]');
    if (progress) progress.textContent = `Today: ${Math.floor(totalMinutes)} / ${targetMinutes} min (${percent}%)`;
};
const applyTrackedSeconds = (button, seconds) => {
    const savedMinutes = Number(button.dataset.trackTodayMinutes || 0) + (Number(seconds || 0) / 60);
    button.dataset.trackTodayMinutes = String(savedMinutes);
    updateRoutineProgress(button);
};
const syncRoutineTimers = async () => {
    const response = await fetch('/api/timers');
    if (!response.ok) return;
    const payload = await response.json();
    const timers = payload.timers || [];
    for (const timer of payload.completed || []) {
        completeTimerNotification(timer);
        const button = document.querySelector(`[data-track-routine-id="${timer.routine_id}"]`);
        if (button) applyTrackedSeconds(button, timer.tracked_seconds || ((timer.tracked_minutes || 0) * 60));
    }
    document.querySelectorAll('[data-track-routine-id]').forEach((button) => {
        const pauseButton = button.parentElement?.querySelector('[data-pause-routine-id]');
        const timer = timers.find((item) => String(item.routine_id) === button.dataset.trackRoutineId);
        if (!timer) {
            button.dataset.timerId = '';
            button.dataset.timerStatus = '';
            button.textContent = 'Start tracking';
            if (pauseButton) pauseButton.hidden = true;
            updateRoutineProgress(button);
            return;
        }
        button.dataset.timerId = timer.id;
        button.dataset.timerStatus = timer.status;
        button.textContent = timer.status === 'paused' ? `Resume ${formatTimer(timer.elapsed_seconds)}` : `Stop ${formatTimer(timer.elapsed_seconds)}`;
        if (pauseButton) {
            pauseButton.hidden = timer.status !== 'running';
            pauseButton.dataset.timerId = timer.id;
        }
        updateRoutineProgress(button, timer.elapsed_seconds);
    });
};
setInterval(syncRoutineTimers, 1000);
syncRoutineTimers();

const notificationSound = () => {
    if (!window.AudioContext) return;
    const context = new AudioContext();
    const oscillator = context.createOscillator();
    const gain = context.createGain();
    oscillator.frequency.value = 880;
    gain.gain.setValueAtTime(0.001, context.currentTime);
    gain.gain.exponentialRampToValueAtTime(0.2, context.currentTime + 0.02);
    gain.gain.exponentialRampToValueAtTime(0.001, context.currentTime + 0.45);
    oscillator.connect(gain).connect(context.destination);
    oscillator.start();
    oscillator.stop(context.currentTime + 0.45);
};

const completeTimerNotification = (timer) => {
    if (!timer.goal_completed || sessionStorage.getItem(`completedTimer-${timer.id}`)) return;
    sessionStorage.setItem(`completedTimer-${timer.id}`, '1');
    notificationSound();
    if ('Notification' in window && Notification.permission === 'granted') {
        new Notification(`${timer.title} complete`, {body: 'Your goal time has been reached.'});
    }
};

document.querySelectorAll('[data-track-routine-id]').forEach((button) => {
    button.addEventListener('click', async () => {
        if (button.dataset.timerId) {
            const action = button.dataset.timerStatus === 'paused' ? 'resume' : 'complete';
            const response = await fetch(`/api/timers/${button.dataset.timerId}/${action}`, {method: 'POST'});
            if (action === 'complete' && response.ok) {
                const timer = (await response.json()).timer;
                completeTimerNotification(timer);
                applyTrackedSeconds(button, timer.tracked_seconds || ((timer.tracked_minutes || 0) * 60));
            }
            syncRoutineTimers();
            return;
        }
        const response = await fetch('/api/timers', {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({title: button.dataset.trackRoutineTitle, routine_id: Number(button.dataset.trackRoutineId), duration_seconds: Number(button.dataset.trackRoutineMinutes) * 60})});
        if (!response.ok) return;
        const timer = (await response.json()).timer;
        await fetch(`/api/timers/${timer.id}/start`, {method: 'POST'});
        syncRoutineTimers();
    });
});

document.querySelectorAll('[data-pause-routine-id]').forEach((button) => {
    button.addEventListener('click', async () => {
        if (!button.dataset.timerId) return;
        await fetch(`/api/timers/${button.dataset.timerId}/pause`, {method: 'POST'});
        syncRoutineTimers();
    });
});

const automationList = document.getElementById('automationList');
const loadAutomations = async () => {
    if (!automationList) return;
    const response = await fetch('/api/automations');
    if (!response.ok) return;
    const automations = (await response.json()).automations || [];
    automationList.hidden = automations.length === 0;
    automationList.replaceChildren(...automations.map((automation) => {
        const item = document.createElement('div');
        item.className = 'automation-item';
        const content = document.createElement('div');
        const trigger = document.createElement('strong');
        trigger.textContent = automation.trigger_type === 'task_completed'
            ? `When task ${automation.trigger_task_id} is completed`
            : `When ${automation.trigger_type.replaceAll('_', ' ')}`;
        const description = document.createElement('span');
        description.textContent = `Then ${automation.title} after ${automation.delay_minutes} minutes`;
        content.append(trigger, description);
        const edit = document.createElement('button');
        edit.dataset.automationEdit = automation.id;
        edit.dataset.title = automation.title;
        edit.dataset.delay = automation.delay_minutes;
        edit.textContent = 'Edit';
        const toggle = document.createElement('button');
        toggle.dataset.automationToggle = automation.id;
        toggle.dataset.enabled = automation.enabled;
        toggle.textContent = automation.enabled ? 'Disable' : 'Enable';
        const remove = document.createElement('button');
        remove.dataset.automationDelete = automation.id;
        remove.textContent = 'Delete';
        item.append(content, edit, toggle, remove);
        return item;
    }));
};
automationList?.addEventListener('click', async (event) => {
    const toggle = event.target.closest('[data-automation-toggle]');
    const remove = event.target.closest('[data-automation-delete]');
    const edit = event.target.closest('[data-automation-edit]');
    if (edit) {
        const title = prompt('Reminder text', edit.dataset.title);
        const delay = prompt('Delay in minutes', edit.dataset.delay);
        if (title !== null && delay !== null) await fetch(`/api/automations/${edit.dataset.automationEdit}`, {method: 'PATCH', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({title, delay_minutes: Number(delay)})});
    }
    if (toggle) await fetch(`/api/automations/${toggle.dataset.automationToggle}`, {method: 'PATCH', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({enabled: toggle.dataset.enabled !== 'true'})});
    if (remove) await fetch(`/api/automations/${remove.dataset.automationDelete}`, {method: 'DELETE'});
    loadAutomations();
});
loadAutomations();

const automationModal = document.getElementById('automationModal');
const automationForm = document.getElementById('automationForm');
if (automationModal && automationForm) {
    const loadAutomationTasks = async () => {
        const response = await fetch('/api/automation-tasks');
        if (!response.ok) return;
        const tasks = (await response.json()).tasks || [];
        ['automationTrigger', 'automationTarget'].forEach((id) => {
            const select = document.getElementById(id);
            select.replaceChildren(new Option('Choose a task', ''));
            tasks.forEach((task) => select.add(new Option(`${task.routine_title} — ${task.title}`, task.id)));
        });
    };
    document.getElementById('openAutomationModal')?.addEventListener('click', async () => {
        await loadAutomationTasks();
        automationModal.classList.remove('hidden');
    });
    document.getElementById('closeAutomationModal')?.addEventListener('click', () => automationModal.classList.add('hidden'));
    automationForm.addEventListener('submit', async (event) => {
        event.preventDefault();
        const response = await fetch('/api/automations', {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({trigger_type: 'task_completed', trigger_task_id: Number(document.getElementById('automationTrigger').value), action_type: 'create_reminder', target_task_id: Number(document.getElementById('automationTarget').value), delay_minutes: Number(document.getElementById('automationDelay').value), title: document.getElementById('automationTitle').value})});
        if (response.ok) automationModal.classList.add('hidden');
    });
}

document.querySelectorAll('[data-routine-action="pause"], [data-routine-action="duplicate"]').forEach((button) => {
    button.addEventListener('click', async () => {
        const action = button.dataset.routineAction;
        const response = await fetch(`/api/routines/${button.dataset.routineId}/${action}`, {method: 'POST'});
        if (response.ok) window.location.reload();
    });
});

const timeModal = document.getElementById('timeEntryModal');
const timeButtons = document.querySelectorAll('.mini-time-btn');

if (timeModal) {
    const timeField = document.getElementById('timeEntryMinutes');
    const timeDayInput = document.getElementById('timeEntryDay');
    const timeApplyButton = document.getElementById('timeEntryApply');
    const timeCancelButton = document.getElementById('timeEntryCancel');
    const timeLabel = document.getElementById('timeEntryLabel');
    let activeTimeCard = null;

    const formatDateLabel = (dayIso) => {
        if (!dayIso) return 'Day';
        const date = new Date(`${dayIso}T00:00:00`);
        return date.toLocaleDateString(undefined, {
            weekday: 'long',
            day: 'numeric',
            month: 'long'
        });
    };

    const openTimeModal = (button) => {
        const day = button.dataset.day;
        const value = Number(button.dataset.value || 0);
        activeTimeCard = button.closest('.routine-card');

        timeDayInput.value = day;
        timeField.value = value || '';
        timeLabel.textContent = formatDateLabel(day);
        timeModal.classList.remove('hidden');
        timeModal.setAttribute('aria-hidden', 'false');
        timeField.focus();
    };

    const closeTimeModal = () => {
        activeTimeCard = null;
        timeModal.classList.add('hidden');
        timeModal.setAttribute('aria-hidden', 'true');
    };

    timeButtons.forEach((button) => {
        button.addEventListener('click', () => openTimeModal(button));
    });

    timeCancelButton?.addEventListener('click', closeTimeModal);
    timeModal.addEventListener('click', (event) => {
        if (event.target.matches('[data-close-time-modal]')) {
            closeTimeModal();
        }
    });

    timeApplyButton?.addEventListener('click', async () => {
        const day = timeDayInput.value;
        const minutes = Number(timeField.value || 0);
        const box = document.querySelector(`.mini-time-btn[data-day="${day}"]`)?.closest('.day-box');
        const checkbox = box?.querySelector('.day-checkbox');
        const hiddenInput = box?.querySelector('.day-minute-input');
        const card = activeTimeCard || box?.closest('.routine-card');

        if (!box || !checkbox || !hiddenInput || !card) {
            closeTimeModal();
            return;
        }

        checkbox.checked = minutes > 0;
        hiddenInput.value = String(minutes || 0);
        box.classList.toggle('is-done', checkbox.checked);
        playCheckboxAnimation(checkbox);

        try {
            const payload = await saveDayEntry(card, day, checkbox.checked, minutes);
            updateRoutineCard(card, payload);
            syncRoutineTasks(card, checkbox.checked);
            updateRoutineTaskProgress(card);
            updateGlobalStats(payload?.stats);
            await loadInsight(insightSelectedDays);
        } catch (error) {
            console.error(error);
        }

        closeTimeModal();
    });

    document.addEventListener('keydown', (event) => {
        if (event.key === 'Escape' && !timeModal.classList.contains('hidden')) {
            closeTimeModal();
        }
    });
}

document.querySelectorAll('.week-form').forEach((form) => {
    form.addEventListener('submit', (event) => {
        event.preventDefault();
    });
});

const dayCheckboxes = document.querySelectorAll('.day-checkbox');
dayCheckboxes.forEach((checkbox) => {
    checkbox.addEventListener('change', async () => {
        const box = checkbox.closest('.day-box');
        const hiddenInput = box?.querySelector('.day-minute-input');
        const card = checkbox.closest('.routine-card');
        const dayIso = checkbox.dataset.day || checkbox.name.replace('day_', '');
        const targetMinutes = Number(card?.dataset.targetMinutes || 0);

        let minutes = Number(hiddenInput?.value || 0);
        if (checkbox.checked && minutes <= 0) {
            minutes = targetMinutes;
            if (hiddenInput) {
                hiddenInput.value = String(minutes);
            }
        } else if (!checkbox.checked) {
            minutes = 0;
            if (hiddenInput) {
                hiddenInput.value = '0';
            }
        }

        box?.classList.toggle('is-done', checkbox.checked);
        playCheckboxAnimation(checkbox);

        try {
            const payload = await saveDayEntry(card, dayIso, checkbox.checked, minutes);
            updateRoutineCard(card, payload);
            syncRoutineTasks(card, checkbox.checked);
            updateRoutineTaskProgress(card);
            updateGlobalStats(payload?.stats);
            await loadInsight(insightSelectedDays);
        } catch (error) {
            console.error(error);
        }
    });
});

document.querySelectorAll('[data-date-record]').forEach((record) => {
        const card = record.closest('.routine-card');
        const dateInput = record.querySelector('[data-record-date]');
        const checkbox = record.querySelector('[data-record-checkbox]');
        const minutesInput = record.querySelector('[data-record-minutes]');
        const status = record.querySelector('[data-record-status]');
        const saveButton = record.querySelector('[data-record-save]');
        const loadDate = async () => {
            if (!card || !dateInput.value) return;
            const response = await fetch(`/api/log_day/${card.dataset.routineId}?date=${encodeURIComponent(dateInput.value)}`);
            if (!response.ok) {
                if (status) status.textContent = 'Unable to load that date.';
                return;
            }
            const payload = await response.json();
            const day = payload.day || {};
            checkbox.checked = Boolean(day.checked);
            minutesInput.value = String(day.minutes_done || 0);
            if (status) status.textContent = `${day.progress_percent || 0}% recorded for ${dateInput.value}.`;
        };
        dateInput.addEventListener('change', loadDate);
        saveButton.addEventListener('click', async () => {
            if (!dateInput.value) return;
            const minutes = Number(minutesInput.value || 0);
            checkbox.checked = minutes > 0 || checkbox.checked;
            saveButton.disabled = true;
            const payload = await saveDayEntry(card, dateInput.value, checkbox.checked, minutes);
            saveButton.disabled = false;
            if (payload) {
                if (status) status.textContent = `${minutes > 0 ? Math.min(100, Math.round((minutes / Math.max(1, Number(card.dataset.targetMinutes || 0))) * 100)) : 0}% recorded for ${dateInput.value}.`;
                updateRoutineCard(card, payload);
                updateGlobalStats(payload.stats);
                await loadInsight(insightSelectedDays);
            }
        });
        loadDate();
});

const deleteConfirmModal = document.getElementById('deleteConfirmModal');
const deleteButtons = document.querySelectorAll('.delete-mini-btn');
let routineIdToDelete = null;

if (deleteConfirmModal) {
    const deleteName = document.getElementById('deleteRoutineName');
    const confirmDeleteBtn = document.getElementById('confirmDeleteBtn');
    const cancelDeleteBtn = document.getElementById('cancelDeleteBtn');
    const closeDeleteBtn = document.getElementById('closeDeleteModal');

    const openDeleteModal = (button) => {
        routineIdToDelete = button.dataset.routineId;
        deleteName.textContent = button.dataset.routineName || 'this routine';
        deleteConfirmModal.classList.remove('hidden');
        deleteConfirmModal.setAttribute('aria-hidden', 'false');
    };

    const closeDeleteModal = () => {
        routineIdToDelete = null;
        deleteConfirmModal.classList.add('hidden');
        deleteConfirmModal.setAttribute('aria-hidden', 'true');
    };

    deleteButtons.forEach((button) => {
        button.addEventListener('click', () => openDeleteModal(button));
    });

    confirmDeleteBtn?.addEventListener('click', () => {
        if (routineIdToDelete) {
            const form = document.createElement('form');
            form.method = 'POST';
            form.action = `/delete/${routineIdToDelete}`;
            const token = document.createElement('input');
            token.type = 'hidden';
            token.name = 'csrf_token';
            token.value = csrfToken || '';
            form.append(token);
            document.body.append(form);
            form.submit();
        }
    });

    cancelDeleteBtn?.addEventListener('click', closeDeleteModal);
    closeDeleteBtn?.addEventListener('click', closeDeleteModal);
    deleteConfirmModal.addEventListener('click', (event) => {
        if (event.target.matches('[data-close-delete-modal]')) {
            closeDeleteModal();
        }
    });

    document.addEventListener('keydown', (event) => {
        if (event.key === 'Escape' && !deleteConfirmModal.classList.contains('hidden')) {
            closeDeleteModal();
        }
    });
}
