// static/js/main.js
// ... (updateCurrentTime, theme toggle, direction toggle, getWeatherIconClass, formatSecondsToCountdown, updateAllBusCountdowns は変更なし) ...

async function fetchAndUpdateData() {
    try {
        const response = await fetch(`/api/data?direction_group=${currentDirectionGroup}`);
        if (!response.ok) throw new Error(`HTTP error! status: ${response.status}`);
        const data = await response.json();

        // (サーバー状態、天気情報更新は変更なし)
        const statusIndicator = document.getElementById('server-status-indicator'); /* ... */
        const weatherInfoArea = document.getElementById('weather-info-area'); /* ... */

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
                            activeRoutesData[displayGroupId].push({ /* ... (データ格納は変更なし) ... */ });
                            let departureTimeMain = bus.departure_time ? bus.departure_time.replace(/\(予定通り\)|\(予定\)|\(遅延可能性あり\)|まもなく発車します|出発しました|通過しました|発車済みの恐れあり|\(\s*\d+分遅れ\s*\)/gi, '').trim() : "時刻不明";
                            let statusLabel = ''; let statusType = ''; let isTrulyUrgent = bus.is_urgent; let isDeparted = false;
                            // (ステータス判定は前回同様)
                            if (bus.delay_info) { /* ... */ } else if (bus.time_until_departure === "出発済み" || /* ... */) { /* ... */ isDeparted = true; }
                            // ... (他のステータス判定) ...
                            
                            let itemAdditionalClass = '';
                            let destinationDisplay = bus.destination_name ? `${bus.destination_name}行` : '行き先不明';
                            let originIndicatorHtml = ''; // 石倉発バッジ (駅方面行き用)
                            let ishikuraRelatedIndicatorHtml = ''; // 石倉関連バッジ (大学方面行き用)

                            // 「駅方面行き」の場合の石倉発バッジ
                            if (currentDirectionGroup === 'to_station_area' && bus.origin_stop_name_short === '石倉') {
                                // itemAdditionalClass += ' ishikura-origin-bus'; // 色での区別は解除
                                originIndicatorHtml = '<span class="location-badge ishikura-badge">石倉</span>';
                            }
                            
                            // 「大学方面行き」の場合の表示制御
                            if (currentDirectionGroup === 'to_university_area') {
                                if (bus.is_ishikura_stop_only) { // 純粋な石倉止まり
                                    itemAdditionalClass += ' ishikura-stop-target'; // スタイル用クラス (背景色など)
                                    ishikuraRelatedIndicatorHtml = '<span class="location-badge ishikura-badge">石倉</span>';
                                    destinationDisplay = ''; // チップで行き先を示すのでメインの行き先は空
                                } else if (bus.is_oyama_for_ishikura) { // 大山ケーブル行き（石倉で下車可）
                                    itemAdditionalClass += ' oyama-via-ishikura-target'; // スタイル用クラス
                                    ishikuraRelatedIndicatorHtml = '<span class="location-badge ishikura-badge">石倉</span>'; // 石倉で降りることを示すバッジ
                                    destinationDisplay = `${bus.destination_name}行`; // 「大山ケーブル行」を表示
                                }
                                // 産業能率大学行きは destinationDisplay がそのまま使われる
                            }
                            
                            const busItemId = `bus-item-${displayGroupId}-${index}`;
                            const busCountdownId = `bus-countdown-${displayGroupId}-${index}`;
                            const durationDisplay = bus.duration_text && bus.duration_text !== "不明" ? `<span class="duration-info">(所要 ${bus.duration_text})</span>` : "";
                            const countdownDisplayHtml = isDeparted ? '' : `<span class="realtime-countdown" id="${busCountdownId}">${formatSecondsToCountdown(bus.seconds_until_departure, bus.origin_stop_name_short)}</span>`;

                            routeHtml += `
                                <li class="bus-item ${isTrulyUrgent ? 'urgent' : ''}${itemAdditionalClass} ${isDeparted ? 'departed-bus' : ''} status-${statusType}" id="${busItemId}">
                                    <div class="bus-item-main">
                                        <span class="bus-number">${isTrulyUrgent && statusType === 'soon' && !isDeparted ? '<i class="fas fa-exclamation-triangle"></i> ' : ''}${index + 1}.</span>
                                        ${originIndicatorHtml}
                                        ${ishikuraRelatedIndicatorHtml}
                                        <span class="departure-time">${departureTimeMain}</span>
                                        <span class="destination-name-display">${destinationDisplay}</span>
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
                        // ... (ループ終了後の処理、エラー処理は変更なし) ...
                    } else { routeHtml += `<p class="info-message"><i class="far fa-clock"></i> 現在、このルートで利用可能なバス情報はありません。</p>`; }
                    routeHtml += `</div>`;
                    multiRouteBusInfoContainer.innerHTML += routeHtml;
                }
            }
        } else { multiRouteBusInfoContainer.innerHTML = `<p class="info-message"><i class="fas fa-info-circle"></i> 表示するバス情報がありません。</p>`; }
        if (countdownIntervalId) clearInterval(countdownIntervalId);
        let hasActiveBuses = Object.keys(activeRoutesData).some(groupId => activeRoutesData[groupId] && activeRoutesData[groupId].length > 0);
        if (hasActiveBuses) { updateAllBusCountdowns(); countdownIntervalId = setInterval(updateAllBusCountdowns, 1000); }
    } catch (error) { /* ... */ }
}
// ... (残りのJSは前回同様)
