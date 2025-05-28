// static/js/main.js
// ... (前回提示した full static/js/main.js の内容をここに記述) ...
function updateCurrentTime() {
    const timeDisplay = document.getElementById('current-time-display');
    if (timeDisplay) {
        const now = new Date();
        const options = {year: 'numeric', month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit', second: '2-digit', hour12: false, timeZone: 'Asia/Tokyo'};
        try {
            const formatter = new Intl.DateTimeFormat('ja-JP', options);
            const parts = formatter.formatToParts(now);
            let year, month, day, hour, minute, second;
            parts.forEach(part => {
                if (part.type === 'year') year = part.value; if (part.type === 'month') month = part.value; if (part.type === 'day') day = part.value;
                if (part.type === 'hour') hour = part.value; if (part.type === 'minute') minute = part.value; if (part.type === 'second') second = part.value;
            });
            if (year && month && day && hour && minute && second) {
                timeDisplay.textContent = `${year}-${month.padStart(2, '0')}-${day.padStart(2, '0')} ${hour.padStart(2, '0')}:${minute.padStart(2, '0')}:${second.padStart(2, '0')} JST`;
            } else { timeDisplay.textContent = now.toLocaleString('sv-SE', { timeZone: 'Asia/Tokyo' }) + " JST"; }
        } catch (e) {
            console.warn("Intl.DateTimeFormat not fully supported, falling back.");
            let jstHours = (now.getUTCHours() + 9) % 24;
            timeDisplay.textContent = `${now.getUTCFullYear()}-${('0' + (now.getUTCMonth() + 1)).slice(-2)}-${('0' + now.getUTCDate()).slice(-2)} ${('0' + jstHours).slice(-2)}:${('0' + now.getUTCMinutes()).slice(-2)}:${('0' + now.getUTCSeconds()).slice(-2)} JST`;
        }
    }
}
setInterval(updateCurrentTime, 1000); updateCurrentTime();

const themeToggleButton = document.getElementById('theme-toggle-button');
const userPreferredTheme = localStorage.getItem('theme');
const osPreferredTheme = window.matchMedia && window.matchMedia('(prefers-color-scheme: dark)').matches ? 'dark-mode' : '';
if (userPreferredTheme) { document.body.classList.add(userPreferredTheme); } else if (osPreferredTheme) { document.body.classList.add(osPreferredTheme); }
if (themeToggleButton) { themeToggleButton.addEventListener('click', () => { document.body.classList.toggle('dark-mode'); localStorage.setItem('theme', document.body.classList.contains('dark-mode') ? 'dark-mode' : ''); }); }

let currentDirectionGroup = localStorage.getItem('busDirectionGroup') || 'to_station_area';
const directionSwitchButton = document.getElementById('direction-switch-button');
const currentDirectionGroupDisplaySpan = document.getElementById('current-direction-group-display');

function updateDirectionGroupDisplay() {
    if (currentDirectionGroupDisplaySpan) {
        if (currentDirectionGroup === 'to_station_area') {
            currentDirectionGroupDisplaySpan.textContent = '大学・石倉 ⇒ 駅';
            document.title = "バス接近情報 (駅方面)";
        } else {
            currentDirectionGroupDisplaySpan.textContent = '駅 ⇒ 大学・石倉';
            document.title = "バス接近情報 (大学・石倉方面)";
        }
    }
}
updateDirectionGroupDisplay();
if (directionSwitchButton) { directionSwitchButton.addEventListener('click', () => { currentDirectionGroup = (currentDirectionGroup === 'to_station_area') ? 'to_university_area' : 'to_station_area'; localStorage.setItem('busDirectionGroup', currentDirectionGroup); updateDirectionGroupDisplay(); fetchAndUpdateData(); }); }

function getWeatherIconClass(conditionCode) {
    if (!conditionCode) return 'fa-question-circle'; const code = parseInt(conditionCode, 10);
    if (code >= 200 && code < 300) return 'fa-bolt'; if (code >= 300 && code < 400) return 'fa-cloud-rain';
    if (code >= 500 && code < 600) return 'fa-cloud-showers-heavy'; if (code >= 600 && code < 700) return 'fa-snowflake';
    if (code >= 700 && code < 800) return 'fa-smog'; if (code === 800) return 'fa-sun';
    if (code === 801) return 'fa-cloud-sun'; if (code > 801 && code < 805) return 'fa-cloud';
    return 'fa-question-circle';
}

let activeRoutesData = {}; let countdownIntervalId = null;

function formatSecondsToCountdown(seconds, originStopNameShort) {
    if (seconds < 0) return ""; // 出発済みの場合、カウントダウンは空
    if (seconds === 0) return "発車時刻";

    const isIshikuraOrigin = originStopNameShort === '石倉';
    const detailCountdownThreshold = isIshikuraOrigin ? 600 : 180;

    const minutes = Math.floor(seconds / 60);
    const remainingSeconds = seconds % 60;

    if (seconds < 60) { // 1分未満
        return `あと${remainingSeconds}秒`;
    } else if (seconds <= detailCountdownThreshold) { // 各出発地の詳細カウントダウン閾値以内
        return `あと${minutes}分${remainingSeconds}秒`;
    } else { // 詳細カウントダウン閾値より大きい場合
        const minutesRoundedUp = Math.ceil(seconds / 60);
        return `あと${minutesRoundedUp}分`;
    }
}

function updateAllBusCountdowns() {
    const currentTime = new Date();
    for (const displayGroupId in activeRoutesData) {
        if (activeRoutesData.hasOwnProperty(displayGroupId)) {
            activeRoutesData[displayGroupId].forEach((bus, index) => {
                const countdownElement = document.getElementById(`bus-countdown-${displayGroupId}-${index}`);
                const busItemElement = document.getElementById(`bus-item-${displayGroupId}-${index}`);
                const statusBadge = busItemElement ? busItemElement.querySelector('.status-badge') : null;

                if (countdownElement && busItemElement) {
                    if (bus.seconds_until_departure > -1 || bus.display_seconds > -1) {
                        let newSecondsUntil;
                        if (bus.departure_time_iso) {
                            const departureTime = new Date(bus.departure_time_iso);
                            newSecondsUntil = Math.max(-1, Math.floor((departureTime.getTime() - currentTime.getTime()) / 1000));
                        } else { newSecondsUntil = Math.max(-1, bus.display_seconds - 1); }
                        bus.display_seconds = newSecondsUntil;
                        
                        const countdownText = formatSecondsToCountdown(newSecondsUntil, bus.origin_stop_name_short);
                        countdownElement.textContent = countdownText;

                        if (newSecondsUntil < 0) {
                            busItemElement.classList.add('departed-bus');
                            busItemElement.classList.remove('urgent');
                            if(statusBadge && statusBadge.textContent !== '出発済み') {
                                statusBadge.textContent = '出発済み';
                                statusBadge.className = 'status-badge status-type-departed';
                            }
                            countdownElement.textContent = ""; // 出発済みならカウントダウン表示は完全に消す
                        } else {
                            busItemElement.classList.remove('departed-bus');
                            const shouldBeUrgent = (newSecondsUntil > 0 && newSecondsUntil <= 180) || bus.is_urgent_from_server;
                            if (shouldBeUrgent) busItemElement.classList.add('urgent');
                            else busItemElement.classList.remove('urgent');
                        }
                    } else if (bus.time_until_departure) {
                        countdownElement.textContent = bus.time_until_departure;
                        if (bus.time_until_departure === "出発済み" || (bus.time_until_departure && bus.time_until_departure.includes("発車済みの恐れあり"))) {
                            busItemElement.classList.add('departed-bus');
                            busItemElement.classList.remove('urgent');
                            countdownElement.textContent = "";
                        }
                    }
                }
            });
        }
    }
}

async function fetchAndUpdateData() {
    try {
        const response = await fetch(`/api/data?direction_group=${currentDirectionGroup}`);
        if (!response.ok) throw new Error(`HTTP error! status: ${response.status}`);
        const data = await response.json();

        const statusIndicator = document.getElementById('server-status-indicator');
        const statusText = document.getElementById('server-status-text');
        if (statusIndicator && statusText) {
             if (data.system_status) {
                if (data.system_status.healthy) { statusIndicator.className = 'indicator green'; statusText.textContent = '作動中'; }
                else if (data.system_status.warning) { statusIndicator.className = 'indicator yellow'; statusText.textContent = '一部注意あり'; }
                else { statusIndicator.className = 'indicator red'; statusText.textContent = '停止またはエラー'; }
            } else { statusIndicator.className = 'indicator'; statusText.textContent = '状態不明'; }
        }
        const weatherInfoArea = document.getElementById('weather-info-area');
        let weatherHtml = '';
        if (data.weather_data) {
            if (data.weather_data.error_message) { weatherHtml = `<p class="error-message"><i class="fas fa-exclamation-triangle"></i> 天気情報: ${data.weather_data.error_message}</p>`; }
            else if (data.weather_data.condition || data.weather_data.description) {
                const iconClass = getWeatherIconClass(data.weather_data.condition_code);
                weatherHtml = `<p><strong><i class="fas ${iconClass}"></i> 伊勢原の天気:</strong> ${data.weather_data.description || data.weather_data.condition}${data.weather_data.temp_c !== null ? ` (${data.weather_data.temp_c.toFixed(1)}℃)` : ''}${data.weather_data.is_rain ? ` <span class="urgent-text"><i class="fas fa-umbrella"></i> 傘を忘れずに！</span>` : ''}</p>`;
            } else { weatherHtml = '<p><i class="fas fa-question-circle"></i> 天気情報なし</p>'; }
        } else { weatherHtml = '<p class="error-message"><i class="fas fa-exclamation-triangle"></i> 天気情報取得エラー</p>'; }
        if (weatherInfoArea) { weatherInfoArea.innerHTML = weatherHtml; }

        const multiRouteBusInfoContainer = document.getElementById('multi-route-bus-info-container');
        if (!multiRouteBusInfoContainer) { console.error("multi-route-bus-info-containerが見つかりません。"); return; }
        multiRouteBusInfoContainer.innerHTML = ''; activeRoutesData = {};

        if (data.routes_bus_data) {
            for (const displayGroupId in data.routes_bus_data) {
                if (data.routes_bus_data.hasOwnProperty(displayGroupId)) {
                    const routeDisplayData = data.routes_bus_data[displayGroupId];
                    activeRoutesData[displayGroupId] = [];
                    let routeHtml = `<div class="route-section" id="route-section-${displayGroupId}">`;
                    routeHtml += `<h3 class="route-header">${routeDisplayData.from_stop_name} 発 <i class="fas fa-long-arrow-alt-right"></i> ${routeDisplayData.to_stop_name} 行き</h3>`;
                    routeHtml += `<p><small><i class="fas fa-sync-alt"></i> バス情報最終更新: <span class="bus-last-updated-route">${routeDisplayData.bus_last_updated_str || "N/A"}</span></small></p>`;
                    if (routeDisplayData.bus_error_message) { routeHtml += `<p class="error-message"><i class="fas fa-exclamation-circle"></i> バス情報取得エラー: ${routeDisplayData.bus_error_message}</p>`; }
                    else if (routeDisplayData.buses_to_display && routeDisplayData.buses_to_display.length > 0) {
                        routeHtml += '<ul class="bus-list">';
                        routeDisplayData.buses_to_display.forEach((bus, index) => {
                            activeRoutesData[displayGroupId].push({
                                departure_time_iso: bus.departure_time_iso, seconds_until_departure: bus.seconds_until_departure,
                                display_seconds: bus.seconds_until_departure, time_until_departure: bus.time_until_departure,
                                departure_time: bus.departure_time, is_urgent_from_server: bus.is_urgent,
                                destination_name: bus.destination_name, via_info: bus.via_info,
                                is_ishikura_stop_only: bus.is_ishikura_stop_only, origin_stop_name_short: bus.origin_stop_name_short,
                                vehicle_no: bus.vehicle_no, duration_text: bus.duration_text, delay_info: bus.delay_info
                            });
                            let departureTimeMain = bus.departure_time ? bus.departure_time.replace(/\(予定通り\)|\(予定\)|\(遅延可能性あり\)|まもなく発車します|出発しました|通過しました|発車済みの恐れあり|\(\s*\d+分遅れ\s*\)/gi, '').trim() : "時刻不明";
                            let statusLabel = ''; let statusType = ''; let isTrulyUrgent = bus.is_urgent; let isDeparted = false;

                            if (bus.delay_info) {
                                statusLabel = bus.delay_info; statusType = 'delayed-explicit'; isTrulyUrgent = false;
                            } else if (bus.time_until_departure === "出発済み" || (bus.departure_time && (bus.departure_time.includes("出発しました") || bus.departure_time.includes("通過しました") || bus.departure_time.includes("発車済みの恐れあり")))) {
                                statusLabel = '出発済み'; statusType = 'departed'; isTrulyUrgent = false; isDeparted = true;
                            } else if (isTrulyUrgent && (bus.seconds_until_departure <= 15 || (bus.departure_time && bus.departure_time.toLowerCase().includes("まもなく")))) {
                                statusLabel = 'まもなく発車'; statusType = 'soon';
                            } else if (bus.departure_time && bus.departure_time.includes("(予定通り)")) {
                                statusLabel = '予定通り'; statusType = 'on-time';
                            } else if (bus.departure_time && bus.departure_time.includes("(遅延可能性あり)")) {
                                statusLabel = '遅延可能性あり'; statusType = 'delayed-possible'; isTrulyUrgent = false;
                            } else if (bus.departure_time && bus.departure_time.includes("(予定)")) {
                                statusLabel = '予定'; statusType = 'scheduled';
                            }
                            if(bus.is_urgent && statusType !== 'soon' && statusType !== 'delayed-explicit' && statusType !== 'departed'){
                                 if (bus.seconds_until_departure > 0 && bus.seconds_until_departure <= 180) { isTrulyUrgent = true; if (!statusLabel){ statusLabel = '接近中'; statusType = 'soon';} }
                                 else if (bus.seconds_until_departure > 180) { isTrulyUrgent = false; }
                            }
                            let itemAdditionalClass = ''; let itemDestinationNote = ''; let originIndicatorHtml = '';
                            if (currentDirectionGroup === 'to_station_area' && bus.origin_stop_name_short === '石倉') {
                                itemAdditionalClass += ' ishikura-origin-bus';
                                originIndicatorHtml = '<span class="origin-chip"><i class="fas fa-map-pin"></i> 石倉</span>';
                            }
                            if (currentDirectionGroup === 'to_university_area' && bus.is_ishikura_stop_only) {
                                itemAdditionalClass += ' ishikura-stop-bus'; itemDestinationNote = ' (石倉止まり)';
                            }
                            const busItemId = `bus-item-${displayGroupId}-${index}`; const busCountdownId = `bus-countdown-${displayGroupId}-${index}`;
                            const durationDisplay = bus.duration_text && bus.duration_text !== "不明" ? `<span class="duration-info">(所要 ${bus.duration_text})</span>` : "";
                            const countdownDisplayHtml = isDeparted ? '' : `<span class="realtime-countdown" id="${busCountdownId}">${formatSecondsToCountdown(bus.seconds_until_departure, bus.origin_stop_name_short)}</span>`;

                            routeHtml += `
                                <li class="bus-item ${isTrulyUrgent ? 'urgent' : ''}${itemAdditionalClass} ${isDeparted ? 'departed-bus' : ''} status-${statusType}" id="${busItemId}">
                                    <div class="bus-item-main">
                                        <span class="bus-number">${isTrulyUrgent && statusType === 'soon' && !isDeparted ? '<i class="fas fa-exclamation-triangle"></i> ' : ''}${index + 1}.</span>
                                        ${originIndicatorHtml}
                                        <span class="departure-time">${departureTimeMain}</span>
                                        <span class="destination-note">${itemDestinationNote}</span>
                                        ${statusLabel ? `<span class="status-badge status-type-${statusType}">${statusLabel}</span>` : ''}
                                        ${countdownDisplayHtml}
                                    </div>
                                    <div class="bus-item-sub">
                                        ${bus.via_info && bus.via_info !== "不明" ? `<span class="via-info">経由: ${bus.via_info}</span>` : ""}
                                        ${durationDisplay}
                                        ${(bus.status_text && bus.status_text.includes("予定通り発車します") && statusType !== 'on-time' && statusType !== 'departed' && statusType !== 'delayed-explicit') ? `<span class="details status-on-time-detail"><i class="fas fa-check-circle"></i> 予定通り発車します</span>` : ''}
                                    </div>
                                </li>`;
                        });
                        routeHtml += '</ul>';
                    } else { routeHtml += `<p class="info-message"><i class="far fa-clock"></i> 現在、このルートで利用可能なバス情報はありません。</p>`; }
                    routeHtml += `</div>`;
                    multiRouteBusInfoContainer.innerHTML += routeHtml;
                }
            }
        } else { multiRouteBusInfoContainer.innerHTML = `<p class="info-message"><i class="fas fa-info-circle"></i> 表示するバス情報がありません。</p>`; }
        if (countdownIntervalId) clearInterval(countdownIntervalId);
        let hasActiveBuses = Object.keys(activeRoutesData).some(groupId => activeRoutesData[groupId] && activeRoutesData[groupId].length > 0);
        if (hasActiveBuses) { updateAllBusCountdowns(); countdownIntervalId = setInterval(updateAllBusCountdowns, 1000); }
    } catch (error) {
        console.error('データ更新に失敗しました:', error);
        if (countdownIntervalId) clearInterval(countdownIntervalId);
        const mrbic = document.getElementById('multi-route-bus-info-container'); if (mrbic) mrbic.innerHTML = `<p class="error-message"><i class="fas fa-broadcast-tower"></i> データ更新に失敗しました。</p>`;
        const si = document.getElementById('server-status-indicator'); const st = document.getElementById('server-status-text');
        if (si) si.className = 'indicator red'; if (st) st.textContent = '通信エラー';
        const wia = document.getElementById('weather-info-area'); if (wia) wia.innerHTML = '<p class="error-message"><i class="fas fa-exclamation-triangle"></i> 天気情報更新エラー</p>';
    }
}
const effectiveDataUpdateInterval = typeof DATA_UPDATE_INTERVAL !== 'undefined' ? DATA_UPDATE_INTERVAL : 10000;
fetchAndUpdateData();
if (effectiveDataUpdateInterval > 0) {
    setInterval(fetchAndUpdateData, effectiveDataUpdateInterval);
    const nextFetchInfoEl = document.getElementById('next-fetch-info-debug');
    if(nextFetchInfoEl) {
        if (effectiveDataUpdateInterval > 1000) { nextFetchInfoEl.textContent = `サーバーデータは約${effectiveDataUpdateInterval/1000}秒間隔で再取得します。`; }
        else { nextFetchInfoEl.textContent = `サーバーデータは高頻度で再取得設定です。`; }
    }
} else {
    const nextFetchInfoEl = document.getElementById('next-fetch-info-debug');
    if(nextFetchInfoEl) { nextFetchInfoEl.textContent = 'サーバーデータの自動更新は無効です。'; }
}
