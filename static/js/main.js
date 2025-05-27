// static/js/main.js
function updateCurrentTime() {
    const timeDisplay = document.getElementById('current-time-display');
    if (timeDisplay) {
        const now = new Date();
        const options = {
            year: 'numeric', month: '2-digit', day: '2-digit',
            hour: '2-digit', minute: '2-digit', second: '2-digit',
            hour12: false, timeZone: 'Asia/Tokyo'
        };
        try {
            const formatter = new Intl.DateTimeFormat('ja-JP', options);
            const parts = formatter.formatToParts(now);
            let year, month, day, hour, minute, second;
            parts.forEach(part => {
                if (part.type === 'year') year = part.value;
                if (part.type === 'month') month = part.value;
                if (part.type === 'day') day = part.value;
                if (part.type === 'hour') hour = part.value;
                if (part.type === 'minute') minute = part.value;
                if (part.type === 'second') second = part.value;
            });
            if (year && month && day && hour && minute && second) {
                timeDisplay.textContent = `${year}-${month.padStart(2, '0')}-${day.padStart(2, '0')} ${hour.padStart(2, '0')}:${minute.padStart(2, '0')}:${second.padStart(2, '0')} JST`;
            } else {
                timeDisplay.textContent = now.toLocaleString('sv-SE', { timeZone: 'Asia/Tokyo' }) + " JST";
            }
        } catch (e) {
            console.warn("Intl.DateTimeFormat not fully supported, falling back.");
            let jstHours = (now.getUTCHours() + 9) % 24;
            timeDisplay.textContent =
                now.getUTCFullYear() + '-' +
                ('0' + (now.getUTCMonth() + 1)).slice(-2) + '-' +
                ('0' + now.getUTCDate()).slice(-2) + ' ' +
                ('0' + jstHours).slice(-2) + ':' +
                ('0' + now.getUTCMinutes()).slice(-2) + ':' +
                ('0' + now.getUTCSeconds()).slice(-2) + ' JST';
        }
    }
}
setInterval(updateCurrentTime, 1000);
updateCurrentTime();

const themeToggleButton = document.getElementById('theme-toggle-button');
const userPreferredTheme = localStorage.getItem('theme');
const osPreferredTheme = window.matchMedia && window.matchMedia('(prefers-color-scheme: dark)').matches ? 'dark-mode' : '';

if (userPreferredTheme) {
    document.body.classList.add(userPreferredTheme);
} else if (osPreferredTheme) {
    document.body.classList.add(osPreferredTheme);
}

themeToggleButton.addEventListener('click', () => {
    document.body.classList.toggle('dark-mode');
    let theme = document.body.classList.contains('dark-mode') ? 'dark-mode' : '';
    localStorage.setItem('theme', theme);
});

let currentDirectionGroup = localStorage.getItem('busDirectionGroup') || 'to_station_area';
const directionSwitchButton = document.getElementById('direction-switch-button');
const currentDirectionGroupDisplaySpan = document.getElementById('current-direction-group-display');

function updateDirectionGroupDisplay() {
    if (currentDirectionGroupDisplaySpan) {
        if (currentDirectionGroup === 'to_station_area') {
            currentDirectionGroupDisplaySpan.textContent = '駅方面行き';
            document.title = "バス接近情報 (駅方面)";
        } else {
            currentDirectionGroupDisplaySpan.textContent = '大学方面行き';
            document.title = "バス接近情報 (大学方面)";
        }
    }
}
updateDirectionGroupDisplay(); // 初期表示

if (directionSwitchButton) {
    directionSwitchButton.addEventListener('click', () => {
        currentDirectionGroup = (currentDirectionGroup === 'to_station_area') ? 'to_university_area' : 'to_station_area';
        localStorage.setItem('busDirectionGroup', currentDirectionGroup);
        updateDirectionGroupDisplay();
        fetchAndUpdateData();
    });
}

function getWeatherIconClass(conditionCode) {
    if (!conditionCode) return 'fa-question-circle';
    const code = parseInt(conditionCode, 10);
    if (code >= 200 && code < 300) return 'fa-bolt';
    if (code >= 300 && code < 400) return 'fa-cloud-rain';
    if (code >= 500 && code < 600) return 'fa-cloud-showers-heavy';
    if (code >= 600 && code < 700) return 'fa-snowflake';
    if (code >= 700 && code < 800) return 'fa-smog';
    if (code === 800) return 'fa-sun';
    if (code === 801) return 'fa-cloud-sun';
    if (code > 801 && code < 805) return 'fa-cloud';
    return 'fa-question-circle';
}

// DATA_UPDATE_INTERVAL はHTML側で<script>タグ経由でグローバル変数として定義される想定
// const dataUpdateInterval = DATA_UPDATE_INTERVAL; (HTML側でグローバル変数DATA_UPDATE_INTERVALを定義)

let activeRoutesData = {}; // { "route_id": [{bus_data}, ...], ... }
let countdownIntervalId = null;

function formatSecondsToCountdown(seconds) {
    if (seconds < 0) return "出発済み";
    if (seconds <= 15) return ""; // 15秒以内は隣のバッジに任せる
    const minutes = Math.floor(seconds / 60);
    const remainingSeconds = seconds % 60;
    if (seconds <= 180) { // 3分 (180秒) 以内 (かつ15秒より大きい)
        if (minutes > 0) {
            return `あと${minutes}分${remainingSeconds}秒`;
        } else {
            return `あと${remainingSeconds}秒`;
        }
    } else { // 3分より大きい場合
        const minutesRoundedUp = Math.ceil(seconds / 60);
        return `あと${minutesRoundedUp}分`;
    }
}

function updateAllBusCountdowns() {
    const currentTime = new Date();
    for (const routeId in activeRoutesData) {
        if (activeRoutesData.hasOwnProperty(routeId)) {
            activeRoutesData[routeId].forEach((bus, index) => {
                const countdownElement = document.getElementById(`bus-countdown-${routeId}-${index}`);
                if (countdownElement) {
                    if (bus.seconds_until_departure > -1) { // サーバーから有効な秒数が来ているか
                        let newSecondsUntil;
                        if (bus.departure_time_iso) {
                            const departureTime = new Date(bus.departure_time_iso);
                            newSecondsUntil = Math.max(0, Math.floor((departureTime.getTime() - currentTime.getTime()) / 1000));
                        } else {
                            // ISO時刻がない場合は、前回の表示用秒数から減算（簡易的）
                            newSecondsUntil = Math.max(0, bus.display_seconds - 1);
                        }
                        bus.display_seconds = newSecondsUntil; // 表示用の秒数を更新
                        countdownElement.textContent = formatSecondsToCountdown(newSecondsUntil);

                        const busItemElement = document.getElementById(`bus-item-${routeId}-${index}`);
                        if (busItemElement) {
                            // urgentクラスの制御（サーバーからのis_urgentも加味）
                            const shouldBeUrgent = (newSecondsUntil > 0 && newSecondsUntil <= 180) || bus.is_urgent_from_server;
                            if (shouldBeUrgent) {
                                busItemElement.classList.add('urgent');
                            } else {
                                busItemElement.classList.remove('urgent');
                            }

                            if (newSecondsUntil === 0 && formatSecondsToCountdown(newSecondsUntil) !== "") { // 空でない場合のみ「発車時刻です」
                                countdownElement.textContent = "発車時刻です";
                            }
                        }
                    } else if (bus.time_until_departure) { // サーバーから秒数情報がなく、初期文字列がある場合
                        countdownElement.textContent = bus.time_until_departure; // これは変更しない
                    }
                }
            });
        }
    }
}

async function fetchAndUpdateData() {
    try {
        const response = await fetch(`/api/data?direction_group=${currentDirectionGroup}`);
        if (!response.ok) {
            throw new Error(`HTTP error! status: ${response.status}`);
        }
        const data = await response.json();

        // サーバー状態表示の更新
        const statusIndicator = document.getElementById('server-status-indicator');
        const statusText = document.getElementById('server-status-text');
        if (statusIndicator && statusText) {
            if (data.system_status) {
                if (data.system_status.healthy) {
                    statusIndicator.className = 'indicator green';
                    statusText.textContent = '作動中';
                } else if (data.system_status.warning) {
                    statusIndicator.className = 'indicator yellow';
                    statusText.textContent = '一部注意あり';
                } else {
                    statusIndicator.className = 'indicator red';
                    statusText.textContent = '停止またはエラー';
                }
            } else {
                statusIndicator.className = 'indicator';
                statusText.textContent = '状態不明';
            }
        }

        // 天気情報表示の更新
        const weatherInfoArea = document.getElementById('weather-info-area');
        let weatherHtml = '';
        if (data.weather_data) {
            if (data.weather_data.error_message) {
                weatherHtml = `<p class="error-message"><i class="fas fa-exclamation-triangle"></i> 天気情報: ${data.weather_data.error_message}</p>`;
            } else if (data.weather_data.condition || data.weather_data.description) {
                const iconClass = getWeatherIconClass(data.weather_data.condition_code);
                weatherHtml = `<p><strong><i class="fas ${iconClass}"></i> 伊勢原の天気:</strong> ${data.weather_data.description || data.weather_data.condition}${data.weather_data.temp_c !== null ? ` (${data.weather_data.temp_c.toFixed(1)}℃)` : ''}${data.weather_data.is_rain ? ` <span class="urgent-text"><i class="fas fa-umbrella"></i> 傘を忘れずに！</span>` : ''}</p>`;
            } else {
                weatherHtml = '<p><i class="fas fa-question-circle"></i> 天気情報なし</p>';
            }
        } else {
            weatherHtml = '<p class="error-message"><i class="fas fa-exclamation-triangle"></i> 天気情報取得エラー</p>';
        }
        weatherInfoArea.innerHTML = weatherHtml;

        // バス情報コンテナのクリアと描画
        const multiRouteBusInfoContainer = document.getElementById('multi-route-bus-info-container');
        multiRouteBusInfoContainer.innerHTML = ''; // コンテナをクリア
        activeRoutesData = {}; // アクティブデータをリセット

        if (data.routes_bus_data) {
            for (const routeId in data.routes_bus_data) {
                if (data.routes_bus_data.hasOwnProperty(routeId)) {
                    const routeData = data.routes_bus_data[routeId];
                    activeRoutesData[routeId] = []; // このルートのバスデータを初期化

                    let routeHtml = `<div class="route-section" id="route-section-${routeId}">`;
                    routeHtml += `<h3 class="route-header">${routeData.from_stop_name} 発 <i class="fas fa-long-arrow-alt-right"></i> ${routeData.to_stop_name} 行き</h3>`;
                    routeHtml += `<p><small><i class="fas fa-sync-alt"></i> バス情報最終更新: <span class="bus-last-updated-route">${routeData.bus_last_updated_str || "N/A"}</span></small></p>`;
                    
                    if (routeData.bus_error_message) {
                        routeHtml += `<p class="error-message" data-bus-error-${routeId}><i class="fas fa-exclamation-circle"></i> バス情報取得エラー: ${routeData.bus_error_message}</p>`;
                    } else if (routeData.buses_to_display && routeData.buses_to_display.length > 0) {
                        routeHtml += '<ul class="bus-list">';
                        routeData.buses_to_display.forEach((bus, index) => {
                            activeRoutesData[routeId].push({ // 各ルートのバス情報を保存
                                departure_time_iso: bus.departure_time_iso,
                                seconds_until_departure: bus.seconds_until_departure,
                                display_seconds: bus.seconds_until_departure,
                                time_until_departure: bus.time_until_departure, // 初期表示用文字列
                                departure_time: bus.departure_time, // 元の出発時刻文字列
                                is_urgent_from_server: bus.is_urgent, // サーバーからのis_urgent
                                system_route_name: bus.system_route_name,
                                destination_name: bus.destination_name,
                                via_info: bus.via_info,
                                is_ishikura_stop_only: bus.is_ishikura_stop_only
                            });

                            let departureTimeMain = bus.departure_time.replace(/\(予定通り\)|\(予定\)|\(遅延可能性あり\)|まもなく発車します|出発しました|通過しました/gi, '').trim();
                            let statusLabel = '';
                            let statusType = '';
                            let isTrulyUrgent = bus.is_urgent; // サーバーからのis_urgentを初期値とする

                            // ステータスラベルとタイプの決定ロジック
                            if (isTrulyUrgent && (bus.seconds_until_departure <= 15 || (bus.departure_time && bus.departure_time.toLowerCase().includes("まもなく")))) {
                                statusLabel = 'まもなく発車'; statusType = 'soon';
                            } else if (bus.departure_time && (bus.departure_time.includes("出発しました") || bus.departure_time.includes("通過しました")) || (bus.time_until_departure && bus.time_until_departure === "出発済み")) {
                                statusLabel = '出発済み'; statusType = 'departed'; isTrulyUrgent = false;
                            } else if (bus.departure_time && bus.departure_time.includes("(予定通り)")) {
                                statusLabel = '予定通り'; statusType = 'on-time';
                            } else if (bus.departure_time && bus.departure_time.includes("(遅延可能性あり)")) {
                                statusLabel = '遅延可能性あり'; statusType = 'delayed-possible'; isTrulyUrgent = false;
                            } else if (bus.departure_time && bus.departure_time.includes("(予定)")) {
                                statusLabel = '予定'; statusType = 'scheduled';
                            }
                            // サーバーis_urgentがtrueで、上記で「まもなく」以外、かつ未出発の場合の再評価
                            if(bus.is_urgent && !statusLabel.includes("まもなく") && statusType !== 'departed'){
                                 if (bus.seconds_until_departure > 0 && bus.seconds_until_departure <= 180) { // 3分以内
                                    isTrulyUrgent = true;
                                    if (!statusLabel){ statusLabel = '接近中'; statusType = 'soon';} // 他のラベルがなければ「接近中」など
                                 } else {
                                    isTrulyUrgent = false; // 3分以上先なら表示上の緊急度は下げる
                                 }
                            }
                            
                            let additionalClass = '';
                            let destinationNote = '';
                            if (currentDirectionGroup === 'to_university_area' && bus.is_ishikura_stop_only) {
                                additionalClass = ' ishikura-stop-bus';
                                destinationNote = ' (石倉止まり)';
                            }
                            const systemRouteDisplay = bus.system_route_name && bus.system_route_name !== "不明" ? ` <span class="system-route">[${bus.system_route_name}]</span>` : "";

                            routeHtml += `
                                <li class="bus-item ${isTrulyUrgent ? 'urgent' : ''}${additionalClass} status-${statusType}" id="bus-item-${routeId}-${index}">
                                    <div class="bus-item-main">
                                        <span class="bus-number">${isTrulyUrgent && statusType === 'soon' ? '<i class="fas fa-exclamation-triangle"></i> ' : ''}${index + 1}.</span>
                                        <span class="departure-time">${departureTimeMain}</span>
                                        ${systemRouteDisplay}
                                        <span class="destination-note">${destinationNote}</span>
                                        ${statusLabel ? `<span class="status-badge status-type-${statusType}">${statusLabel}</span>` : ''}
                                        <span class="realtime-countdown" id="bus-countdown-${routeId}-${index}">
                                            ${formatSecondsToCountdown(bus.seconds_until_departure)}
                                        </span>
                                    </div>
                                    <div class="bus-item-sub">
                                        ${bus.via_info && bus.via_info !== "不明" ? `<span class="via-info">経由: ${bus.via_info}</span>` : ""}
                                        ${(bus.status_text && bus.status_text.includes("予定通り発車します") && statusType !== 'on-time' && statusType !== 'departed') ? `<span class="details status-on-time-detail"><i class="fas fa-check-circle"></i> 予定通り発車します</span>` : ''}
                                    </div>
                                </li>`;
                        });
                        routeHtml += '</ul>';
                    } else {
                        routeHtml += `<p class="info-message" data-bus-info-${routeId}><i class="far fa-clock"></i> 現在、このルートで利用可能なバス情報はありません。</p>`;
                    }
                    routeHtml += `</div>`; // route-section の閉じタグ
                    multiRouteBusInfoContainer.innerHTML += routeHtml;
                }
            }
        } else {
             multiRouteBusInfoContainer.innerHTML = `<p class="info-message"><i class="fas fa-info-circle"></i> 表示するバス情報がありません。</p>`;
        }

        // カウントダウン処理を開始またはリセット
        if (countdownIntervalId) {
            clearInterval(countdownIntervalId);
        }
        let hasActiveBuses = Object.keys(activeRoutesData).some(routeId => activeRoutesData[routeId].length > 0);
        if (hasActiveBuses) {
            updateAllBusCountdowns(); // 初回表示を即時更新
            countdownIntervalId = setInterval(updateAllBusCountdowns, 1000); // 1秒ごとに更新
        }

    } catch (error) {
        console.error('データ更新に失敗しました:', error);
        if (countdownIntervalId) { // エラー時はタイマー停止
            clearInterval(countdownIntervalId);
        }
        const multiRouteBusInfoContainer = document.getElementById('multi-route-bus-info-container');
        if (multiRouteBusInfoContainer) {
            multiRouteBusInfoContainer.innerHTML = `<p class="error-message"><i class="fas fa-broadcast-tower"></i> データ更新に失敗しました。</p>`;
        }
        // 他のエラー表示も適切に行う (サーバー状態、天気など)
        const statusIndicator = document.getElementById('server-status-indicator');
        const statusText = document.getElementById('server-status-text');
        if (statusIndicator) statusIndicator.className = 'indicator red';
        if (statusText) statusText.textContent = '通信エラー';

        const weatherInfoArea = document.getElementById('weather-info-area');
        if(weatherInfoArea) weatherInfoArea.innerHTML = '<p class="error-message"><i class="fas fa-exclamation-triangle"></i> 天気情報更新エラー</p>';
    }
}

// DATA_UPDATE_INTERVAL はHTML側で定義されることを期待
// この値が未定義の場合のフォールバック
const effectiveDataUpdateInterval = typeof DATA_UPDATE_INTERVAL !== 'undefined' ? DATA_UPDATE_INTERVAL : 10000; // デフォルト10秒

fetchAndUpdateData(); // 初回実行
if (effectiveDataUpdateInterval > 0) {
    setInterval(fetchAndUpdateData, effectiveDataUpdateInterval);
    const nextFetchInfoEl = document.getElementById('next-fetch-info-debug');
    if(nextFetchInfoEl) { // デバッグ表示が必要な場合のみ有効にする
        if (effectiveDataUpdateInterval > 1000) {
             nextFetchInfoEl.textContent = `サーバーデータは約${effectiveDataUpdateInterval/1000}秒間隔で再取得します。`;
        } else {
             nextFetchInfoEl.textContent = `サーバーデータは高頻度で再取得設定です。`;
        }
    }
} else {
    const nextFetchInfoEl = document.getElementById('next-fetch-info-debug');
    if(nextFetchInfoEl) {
        nextFetchInfoEl.textContent = 'サーバーデータの自動更新は無効です。';
    }
}
