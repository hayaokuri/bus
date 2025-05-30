// static/js/main.js
function updateCurrentTime() {
    // (変更なし)
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
// (テーマ切り替え、方向切り替え、天気アイコン取得は変更なし)
const userPreferredTheme = localStorage.getItem('theme');
const osPreferredTheme = window.matchMedia && window.matchMedia('(prefers-color-scheme: dark)').matches ? 'dark-mode' : '';
if (userPreferredTheme) { document.body.classList.add(userPreferredTheme); } else if (osPreferredTheme) { document.body.classList.add(osPreferredTheme); }
if (themeToggleButton) { themeToggleButton.addEventListener('click', () => { document.body.classList.toggle('dark-mode'); localStorage.setItem('theme', document.body.classList.contains('dark-mode') ? 'dark-mode' : ''); }); }
let currentDirectionGroup = localStorage.getItem('busDirectionGroup') || 'to_station_area';
const directionSwitchButton = document.getElementById('direction-switch-button');
const currentDirectionGroupDisplaySpan = document.getElementById('current-direction-group-display');
function updateDirectionGroupDisplay() {
    if (currentDirectionGroupDisplaySpan) {
        if (currentDirectionGroup === 'to_station_area') { currentDirectionGroupDisplaySpan.textContent = '大学・石倉 ⇒ 駅'; document.title = "バス接近情報 (駅方面)"; }
        else { currentDirectionGroupDisplaySpan.textContent = '駅 ⇒ 大学・石倉'; document.title = "バス接近情報 (大学・石倉方面)"; }
    }
}
updateDirectionGroupDisplay();
if (directionSwitchButton) { directionSwitchButton.addEventListener('click', () => { currentDirectionGroup = (currentDirectionGroup === 'to_station_area') ? 'to_university_area' : 'to_station_area'; localStorage.setItem('busDirectionGroup', currentDirectionGroup); updateDirectionGroupDisplay(); fetchAndUpdateData(); }); }
function getWeatherIconClass(conditionCode) { if (!conditionCode) return 'fa-question-circle'; const code = parseInt(conditionCode, 10); if (code >= 200 && code < 300) return 'fa-bolt'; if (code >= 300 && code < 400) return 'fa-cloud-rain'; if (code >= 500 && code < 600) return 'fa-cloud-showers-heavy'; if (code >= 600 && code < 700) return 'fa-snowflake'; if (code >= 700 && code < 800) return 'fa-smog'; if (code === 800) return 'fa-sun'; if (code === 801) return 'fa-cloud-sun'; if (code > 801 && code < 805) return 'fa-cloud'; return 'fa-question-circle'; }


let activeRoutesData = {}; let countdownIntervalId = null;

function formatSecondsToCountdown(seconds, originStopNameShort) {
    // (変更なし)
    if (seconds < 0) return ""; if (seconds === 0) return "発車時刻";
    const isIshikuraOrigin = originStopNameShort === '石倉';
    const detailCountdownThreshold = isIshikuraOrigin ? 600 : 180;
    const minutes = Math.floor(seconds / 60); const remainingSeconds = seconds % 60;
    if (seconds < 60) { return `あと${remainingSeconds}秒`; }
    else if (seconds <= detailCountdownThreshold) { return `あと${minutes}分${remainingSeconds}秒`; }
    else { const minutesRoundedUp = Math.ceil(seconds / 60); return `あと${minutesRoundedUp}分`; }
}

function updateAllBusCountdowns() {
    // (緊急表示ロジックは前回提案のまま)
    const currentTime = new Date();
    for (const displayGroupId in activeRoutesData) {
        if (activeRoutesData.hasOwnProperty(displayGroupId)) {
            activeRoutesData[displayGroupId].forEach((bus, index) => {
                const countdownElement = document.getElementById(`bus-countdown-${displayGroupId}-${index}`);
                const busItemElement = document.getElementById(`bus-item-${displayGroupId}-${index}`);
                if (countdownElement && busItemElement) {
                    if (bus.seconds_until_departure > -1 || bus.display_seconds > -1) { // カウントダウン対象か
                        let newSecondsUntil;
                        if (bus.departure_time_iso) { // ISO時刻があればそれを基準
                            const departureTime = new Date(bus.departure_time_iso);
                            newSecondsUntil = Math.max(-1, Math.floor((departureTime.getTime() - currentTime.getTime()) / 1000));
                        } else { // なければ表示用秒数をデクリメント
                            newSecondsUntil = Math.max(-1, bus.display_seconds - 1);
                        }
                        bus.display_seconds = newSecondsUntil; // 次のインターバル用に更新
                        const countdownText = formatSecondsToCountdown(newSecondsUntil, bus.origin_stop_name_short);
                        countdownElement.textContent = countdownText;
                        const subStatusBadge = busItemElement.querySelector('.bus-item-sub .status-badge');
                        if (newSecondsUntil < 0) { // 出発済み
                            busItemElement.classList.add('departed-bus'); busItemElement.classList.remove('urgent');
                            if(subStatusBadge && subStatusBadge.textContent !== '出発済み') { subStatusBadge.textContent = '出発済み'; subStatusBadge.className = 'status-badge status-type-departed';}
                            const delayChip = busItemElement.querySelector('.bus-item-sub .delay-chip');
                            if (delayChip) delayChip.style.display = 'none'; // 出発したら遅延チップは消す
                            countdownElement.textContent = "";
                        } else { // 未出発
                            busItemElement.classList.remove('departed-bus');
                            const isIshikuraOriginForUrgent = bus.origin_stop_name_short === '石倉';
                            let shouldBeUrgent = bus.is_urgent_from_server; // サーバーからの緊急フラグを尊重
                            if (isIshikuraOriginForUrgent && newSecondsUntil > 0 && newSecondsUntil <= 420) { shouldBeUrgent = true; }
                            else if (!isIshikuraOriginForUrgent && newSecondsUntil > 0 && newSecondsUntil <= 180) { shouldBeUrgent = true; }
                            if (bus.status_text_for_urgent_check && bus.status_text_for_urgent_check.toLowerCase().includes("まもなく")) { shouldBeUrgent = true; }
                            else if (bus.delay_info) { shouldBeUrgent = false; }
                            if (shouldBeUrgent) { busItemElement.classList.add('urgent'); } else { busItemElement.classList.remove('urgent'); }
                        }
                    } else if (bus.time_until_departure) { // カウントダウン対象外だが表示テキストがある場合
                        countdownElement.textContent = bus.time_until_departure; // 例: "(2分遅れ)" など
                        if (bus.time_until_departure === "出発済み" || (bus.time_until_departure && bus.time_until_departure.includes("発車済みの恐れあり"))) {
                            busItemElement.classList.add('departed-bus'); busItemElement.classList.remove('urgent'); countdownElement.textContent = "";
                        }
                    }
                }
            });
        }
    }
}

async function fetchAndUpdateData() {
    // (天気情報取得、サーバー状態表示などは変更なし)
    try {
        const response = await fetch(`/api/data?direction_group=${currentDirectionGroup}`);
        if (!response.ok) throw new Error(`HTTP error! status: ${response.status}`);
        const data = await response.json();
        const serverStatusIndicator = document.getElementById('server-status-indicator');
        const serverStatusText = document.getElementById('server-status-text');
        if (data.system_status) {
            if (data.system_status.healthy) { serverStatusIndicator.className = 'indicator green'; serverStatusText.textContent = '正常'; }
            else if (data.system_status.warning) { serverStatusIndicator.className = 'indicator yellow'; serverStatusText.textContent = '一部注意'; }
            else { serverStatusIndicator.className = 'indicator red'; serverStatusText.textContent = 'エラー'; }
        } else { serverStatusIndicator.className = 'indicator red'; serverStatusText.textContent = '状態不明'; }
        const weatherInfoArea = document.getElementById('weather-info-area');
        let weatherHtml = '';
        if (data.weather_data) {
            if (data.weather_data.error_message) { weatherHtml = `<p class="error-message"><i class="fas fa-exclamation-triangle"></i> 天気情報取得エラー: ${data.weather_data.error_message}</p>`; }
            else if (data.weather_data.condition && data.weather_data.temp_c !== undefined) {
                const weatherIconClass = getWeatherIconClass(data.weather_data.condition_code);
                weatherHtml = `<p><i class="fas ${weatherIconClass}"></i> <strong>現在の天気:</strong> ${data.weather_data.description} (${data.weather_data.temp_c}°C)</p>`;
                if (data.weather_data.is_rain) { weatherHtml += `<p class="urgent-text"><i class="fas fa-umbrella"></i> 雨が降っています。お出かけの際は足元にご注意ください。</p>`; }
            } else { weatherHtml = `<p><i class="fas fa-question-circle"></i> 天気情報を取得できませんでした。</p>`; }
        } else { weatherHtml = `<p><i class="fas fa-spinner fa-spin"></i> 天気情報取得中...</p>`; }
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

                    if (routeDisplayData.bus_error_message) {
                        routeHtml += `<p class="error-message"><i class="fas fa-exclamation-circle"></i> バス情報取得エラー: ${routeDisplayData.bus_error_message}</p>`;
                    } else if (routeDisplayData.buses_to_display && routeDisplayData.buses_to_display.length > 0) {
                        routeHtml += '<ul class="bus-list">';
                        routeDisplayData.buses_to_display.forEach((bus, index) => {
                            activeRoutesData[displayGroupId].push({
                                departure_time_iso: bus.departure_time_iso,
                                seconds_until_departure: bus.seconds_until_departure,
                                display_seconds: bus.seconds_until_departure, // 初期値
                                time_until_departure: bus.time_until_departure, // フォールバック表示用
                                is_urgent_from_server: bus.is_urgent,
                                origin_stop_name_short: bus.origin_stop_name_short,
                                delay_info: bus.delay_info,
                                status_text_for_urgent_check: bus.status_text // 生のステータス文
                            });

                            // --- 時刻表示 (departureTimeMain) の整形 ---
                            let departureTimeMain = bus.departure_time || "時刻不明"; // バックエンドからの時刻文字列
                            // 「まもなく」や「出発済み」は statusLabel で表示するため、時刻表示からは削除または時刻のみ抽出
                            if (bus.status_text && (bus.status_text.toLowerCase().includes("まもなく") || bus.status_text.toLowerCase().includes("出発しました") || bus.status_text.toLowerCase().includes("通過しました"))) {
                                const timeMatch = departureTimeMain.match(/\d{1,2}:\d{2}/);
                                departureTimeMain = timeMatch ? timeMatch[0] + "発" : ""; // 時刻があればそれだけ、なければ空
                            } else if (departureTimeMain === "情報なし" || departureTimeMain === "時刻不明") {
                                // そのまま表示
                            } else if (!departureTimeMain.includes("発") && !departureTimeMain.includes("着予定") && /\d{1,2}:\d{2}/.test(departureTimeMain)) {
                                departureTimeMain += "発"; // "発" がなければ補う (ただし "着予定" は除く)
                            }
                            // --- ここまで時刻表示の整形 ---

                            let statusLabel = ''; let statusType = ''; let isTrulyUrgent = bus.is_urgent; let isDeparted = false;

                            // --- statusLabel の生成 (生の status_text を使う) ---
                            if (bus.status_text) {
                                if (bus.status_text.includes("出発しました") || bus.status_text.includes("通過しました") || bus.status_text.includes("発車済みの恐れあり")) {
                                    statusLabel = '出発済み'; statusType = 'departed'; isDeparted = true;
                                } else if (bus.status_text.toLowerCase().includes("まもなく")) {
                                    statusLabel = 'まもなく発車'; statusType = 'soon'; isTrulyUrgent = true;
                                } else if (!bus.delay_info) { // 遅延情報がない場合のみ、他のステータスを考慮
                                    if (bus.status_text.includes("(予定通り)")) { statusLabel = '予定通り'; statusType = 'on-time'; }
                                    else if (bus.status_text.includes("(遅延可能性あり)")) { statusLabel = '遅延可能性あり'; statusType = 'delayed-possible'; }
                                    else if (bus.status_text.includes("(予定)")) { statusLabel = '予定'; statusType = 'scheduled'; }
                                } else if (bus.delay_info && statusLabel === '') { // 遅延があり、他の主要ステータスがない場合
                                     // statusLabelは空のまま (遅延チップで表示するため)
                                     // isTrulyUrgent は updateAllBusCountdowns で遅延を考慮して設定される
                                }
                            }
                            // --- ここまで statusLabel の生成 ---

                            // isTrulyUrgent の最終調整は updateAllBusCountdowns で行う

                            let itemAdditionalClass = '';
                            let destinationDisplay = bus.destination_name ? `${bus.destination_name}行` : '行き先不明';
                            let sannoOriginChipHtml = ''; let originIndicatorHtml = ''; let ishikuraRelatedChipHtml = ''; let delayChipHtml = '';

                            if (currentDirectionGroup === 'to_station_area') {
                                if (bus.origin_stop_name_short === '大学') { sannoOriginChipHtml = `<span class="chip sanno-chip"><i class="fas fa-university"></i> 大学</span>`; }
                                else if (bus.origin_stop_name_short === '石倉') { originIndicatorHtml = `<span class="chip ishikura-chip ishikura-origin-chip"><i class="fas fa-sign-out-alt"></i> 石倉</span>`; }
                            }
                            if (currentDirectionGroup === 'to_university_area') {
                                const destinationName = bus.destination_name || "";
                                if (!destinationName.includes("産業能率大学")) {
                                    ishikuraRelatedChipHtml = `<span class="chip ishikura-chip ishikura-via-chip"><i class="fas fa-map-pin"></i> 石倉</span>`;
                                    itemAdditionalClass += ' ishikura-via-target';
                                }
                            }
                            if (bus.delay_info && !isDeparted) { delayChipHtml = `<span class="chip delay-chip"><i class="fas fa-exclamation-triangle"></i> ${bus.delay_info}</span>`; }

                            const busItemId = `bus-item-${displayGroupId}-${index}`; const busCountdownId = `bus-countdown-${displayGroupId}-${index}`;
                            const durationDisplay = bus.duration_text && bus.duration_text !== "不明" ? `<span class="duration-info">(所要 ${bus.duration_text})</span>` : "";
                            
                            // カウントダウン表示HTML (seconds_until_departure が -1 なら空、そうでなければカウントダウン要素)
                            // bus.time_until_departure は、バックエンドからのフォールバック文字列（例：「(X分遅れ)」）
                            const countdownDisplayHtml = (bus.seconds_until_departure > -1 || bus.departure_time_iso) // カウントダウン可能か
                                ? `<span class="realtime-countdown" id="${busCountdownId}">${formatSecondsToCountdown(bus.seconds_until_departure, bus.origin_stop_name_short)}</span>`
                                : (bus.time_until_departure && bus.time_until_departure !== "出発済み" ? `<span class="realtime-countdown">${bus.time_until_departure}</span>` : ""); // バックエンドからのテキスト表示

                            const busNumberIcon = (isTrulyUrgent && !isDeparted && !bus.delay_info && statusType === 'soon') ? '<i class="fas fa-exclamation-triangle"></i> ' : '';

                            routeHtml += `
                                <li class="bus-item ${isTrulyUrgent ? 'urgent' : ''} ${itemAdditionalClass} ${isDeparted ? 'departed-bus' : ''}" id="${busItemId}">
                                    <div class="bus-item-main">
                                        <span class="bus-number">${busNumberIcon}${index + 1}.</span>
                                        <span class="departure-time">${departureTimeMain}</span>
                                        <span class="destination-name-display">${destinationDisplay}</span>
                                        ${countdownDisplayHtml}
                                    </div>
                                    <div class="bus-item-sub">
                                        ${sannoOriginChipHtml} ${originIndicatorHtml} ${ishikuraRelatedChipHtml} ${delayChipHtml}
                                        ${statusLabel ? `<span class="status-badge status-type-${statusType}">${statusLabel}</span>` : ''}
                                        ${bus.via_info && bus.via_info !== "不明" ? `<span class="via-info">経由: ${bus.via_info}</span>` : ""}
                                        ${durationDisplay}
                                        ${(bus.status_text && bus.status_text.includes("予定通り発車します") && statusType === 'on-time' && !bus.delay_info) ? `<span class="details status-on-time-detail"><i class="fas fa-check-circle"></i> 予定通り発車します</span>` : ''}
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
        const mrbic = document.getElementById('multi-route-bus-info-container');
        if (mrbic) mrbic.innerHTML = `<p class="error-message"><i class="fas fa-broadcast-tower"></i> データ更新に失敗しました。</p>`;
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
