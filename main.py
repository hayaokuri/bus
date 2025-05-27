from flask import Flask, render_template, jsonify, request
import requests
from bs4 import BeautifulSoup
import json
import re
import datetime
import time
import os
import pytz
import logging

OPENWEATHERMAP_API_KEY = "28482976c81657127a816a47f53cc3d2"
DISCORD_WEBHOOK_URL = "https://discord.com/api/webhooks/1375497603466395749/4QtOWTUk-_44xc8-RVmhm3imPatU4yiEuRj1NR1j5PryEkbik98A204uJ3069nye_GNI"

WEATHER_LOCATION = "Isehara,JP"
BASE_URL = "http://real.kanachu.jp/pc/displayapproachinfo"

ROUTE_DEFINITIONS = {
    "sanno_to_station": {
        "id": "sanno_to_station", "from_stop_no": "18137", "to_stop_no": "18100",
        "from_stop_name_short": "大学", "to_stop_name_short": "駅",
        "from_stop_name_full": "産業能率大学", "to_stop_name_full": "伊勢原駅北口",
        "group": "to_station_area"
    },
    "ishikura_to_station": {
        "id": "ishikura_to_station", "from_stop_no": "18124", "to_stop_no": "18100",
        "from_stop_name_short": "石倉", "to_stop_name_short": "駅",
        "from_stop_name_full": "石倉", "to_stop_name_full": "伊勢原駅北口",
        "group": "to_station_area"
    },
    "station_to_university_ishikura": {
        "id": "station_to_university_ishikura", "from_stop_no": "18100",
        "to_stop_no_sanno": "18137", "to_stop_no_ishikura": "18124",
        "from_stop_name_short": "駅", "to_stop_name_short": "大学方面", # UIの方向ボタン表示用
        "from_stop_name_full": "伊勢原駅北口", "to_stop_name_full": "大学・石倉方面",
        "group": "to_university_area"
    }
}

MAX_BUSES_TO_FETCH = 10
# ... (他の定数は前回同様) ...
KEY_VEHICLE_NO = "vehicle_no" # 車両番号用のキー

# (logging, cache, send_discord, get_weather_info の設定は前回同様)
# ... (前回提示の logging, weather_cache, bus_data_cache, weather_fetched_today_g, last_date_weather_checked_g, WEATHER_CACHE_DURATION_SECONDS, BUS_DATA_CACHE_DURATION_SECONDS, send_discord_notification, get_weather_info をここに挿入) ...
app = Flask(__name__)
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(module)s - %(funcName)s - %(message)s', datefmt='%Y-%m-%d %H:%M:%S')
weather_cache = {"data": None, "timestamp": 0, "error": None}
bus_data_cache = {}
weather_fetched_today_g = False
last_date_weather_checked_g = None
WEATHER_CACHE_DURATION_SECONDS = 30 * 60
BUS_DATA_CACHE_DURATION_SECONDS = 10

KEY_DEPARTURE_TIME = "departure_time"
KEY_STATUS_TEXT = "status_text"
KEY_TIME_UNTIL = "time_until_departure"
KEY_IS_URGENT = "is_urgent"
KEY_DEPARTURE_TIME_ISO = "departure_time_iso"
KEY_SECONDS_UNTIL_DEPARTURE = "seconds_until_departure"
KEY_SYSTEM_ROUTE_NAME = "system_route_name"
KEY_DESTINATION_NAME = "destination_name"
KEY_VIA_INFO = "via_info"
KEY_IS_ISHIKURA_STOP_ONLY = "is_ishikura_stop_only"
KEY_ORIGIN_STOP_NAME_SHORT = "origin_stop_name_short"
TOKYO_TZ = pytz.timezone('Asia/Tokyo')


def parse_bus_info_from_html(html_content):
    """
    神奈中バスのHTMLからバス情報をパースする補助関数。
    より複雑なHTML構造に対応するため、この関数を呼び出す側で from_stop_name を渡すなどして、
    パースのコンテキストを与える必要があるかもしれない。
    """
    soup = BeautifulSoup(html_content, 'html.parser')
    bus_departure_list = []
    
    bus_wrappers = soup.select('div.inner2.pa01 > div.wrap')

    for wrap_element in bus_wrappers:
        if len(bus_departure_list) >= MAX_BUSES_TO_FETCH: # この制限は呼び出し側でかけるべきか検討
            break

        col01 = wrap_element.find('div', class_='col01')
        system_route_name = "不明"
        destination_name = "不明"
        via_info = "不明"
        vehicle_no = None # 車両番号

        if col01:
            table_rows = col01.select('table.table01 tr')
            for row in table_rows:
                th_tag = row.find('th')
                td_tag = row.find('td')
                if th_tag and td_tag:
                    th_text = th_tag.get_text(strip=True)
                    span_point = td_tag.find('span', class_='point')
                    td_text = span_point.get_text(strip=True) if span_point else td_tag.get_text(strip=True)
                    
                    if "系統" in th_text:
                        if span_point:
                            text_parts = [s for s in span_point.stripped_strings if "バスルートを表示" not in s]
                            system_route_name = "".join(text_parts).strip()
                        else:
                            system_route_name = td_text
                    elif "行き先" in th_text:
                        destination_name = td_text
                    elif "経由" in th_text:
                        via_info = td_text
                    elif "車両番号" in th_text: # 車両番号の取得
                        vehicle_no_match = re.search(r'([いす盛おつひ平やまた])\s*(\d+)', td_text) # 神奈中の車両番号のパターン（営業所記号 + 数字）
                        if vehicle_no_match:
                            vehicle_no = vehicle_no_match.group(0).strip().replace(" ","") # "い 19" -> "い19"
                        else:
                            vehicle_no = td_text.split("※")[0].split("★")[0].split("Ｔ")[0].strip() # 特記号を除去


        col02 = wrap_element.find('div', class_='col02')
        status_text_from_title01 = "情報なし" # p.title01 から取得する発車時刻/状況
        departure_time_from_notes = None # 石倉などの場合、div.placeArea01.departure span.notes から取得

        if col02:
            frameBox03 = col02.find('div', class_='frameBox03')
            if frameBox03:
                title_element = frameBox03.find('p', class_='title01')
                if title_element:
                    status_text_from_title01 = title_element.get_text(strip=True)

                # 石倉のような中間バス停の場合、詳細な時刻は notes にあることが多い
                departure_area = frameBox03.find('div', class_='placeArea01 departure')
                if departure_area:
                    notes_span = departure_area.find('span', class_='notes')
                    if notes_span:
                        notes_text = notes_span.get_text(strip=True) #例: （8:25着予定） (発車から1分)
                        # ここから時刻を抽出するロジックが必要
                        # 例: "（8:25着予定）" -> "8:25発予定" (推定)
                        # 例: "（現在 XX分遅れ）" -> "XX分遅れ"
                        # このパースは非常に複雑になるため、status_text_from_title01 を優先しつつ、補足情報として使う
                        match_time_in_notes = re.search(r'（(\d{1,2}:\d{2})着予定）', notes_text)
                        if match_time_in_notes:
                            departure_time_from_notes = f"{match_time_in_notes.group(1)}発予定" # 着予定を参考に発予定とする
                        
                        # 遅延情報の抽出
                        match_delay_in_notes = re.search(r'（現在\s*(\d+)\s*分遅れ）', notes_text)
                        if match_delay_in_notes:
                            # status_text_from_title01 に遅延情報を付加、または上書き
                            status_text_from_title01 = f"{departure_time_from_notes if departure_time_from_notes else status_text_from_title01.split('を')[-1].strip()} ({match_delay_in_notes.group(1)}分遅れ)"


        # status_text_from_title01 を元に departure_time_str を決定
        # 石倉の場合、status_text_from_title01 は始発の情報、departure_time_from_notes が石倉の時刻のヒント
        final_status_text = status_text_from_title01
        if departure_time_from_notes and "発予定" in departure_time_from_notes : # notes から時刻が取れたらそれを優先
             # ただし、元のstatus_text_from_title01 が「まもなく」など具体的な状況を示している場合はそちらも考慮
            if "まもなく" in status_text_from_title01 or "出発しました" in status_text_from_title01 or "遅れ" in status_text_from_title01:
                # 状況を優先し、時刻は notes からのものを参考にする
                # ここでは、status_text_from_title01 の時刻部分を notes の時刻で置き換える試み
                time_part_match = re.search(r'\d{1,2}:\d{2}', status_text_from_title01)
                notes_time_part_match = re.search(r'\d{1,2}:\d{2}', departure_time_from_notes)
                if time_part_match and notes_time_part_match:
                    final_status_text = status_text_from_title01.replace(time_part_match.group(0), notes_time_part_match.group(0))
                elif notes_time_part_match: # 元のstatusに時刻がないがnotesに時刻がある場合
                    final_status_text = departure_time_from_notes + " " + status_text_from_title01 # 組み合わせる（要調整）
            else:
                final_status_text = departure_time_from_notes # notesの時刻を優先

        departure_time_str = None
        if "まもなく発車します" in final_status_text or "まもなく到着" in final_status_text: # title01が優先される
            departure_time_str = "まもなく発車します"
        elif "通過しました" in final_status_text or "出発しました" in final_status_text:
            departure_time_str = "出発しました"
        else:
            match_time_candidate = re.search(r'(\d{1,2}:\d{2})発?', final_status_text)
            time_part = None
            if match_time_candidate: time_part = match_time_candidate.group(1)

            if "予定通り発車します" in final_status_text:
                if time_part: departure_time_str = f"{time_part}発 (予定通り)"
                else: departure_time_str = "状態不明 (予定通り情報あり)"
            elif "遅れ" in final_status_text: #「XX分遅れ」という表現を検出
                if time_part: departure_time_str = f"{time_part}発 ({final_status_text[final_status_text.find('('):final_status_text.find(')')+1].strip() if '(' in final_status_text else '遅延'})"
                else: departure_time_str = "状態不明 (遅延情報あり)"

            elif "頃発車します" in final_status_text: # これは通常通り
                if time_part: departure_time_str = f"{time_part}発 (遅延可能性あり)"
                else: departure_time_str = "状態不明 (遅延情報あり)"
            elif "発予定" in final_status_text:
                if time_part: departure_time_str = f"{time_part}発 (予定)"
                else: departure_time_str = "状態不明 (予定情報あり)"
            elif time_part:
                departure_time_str = f"{time_part}発"
        
        if departure_time_str:
            bus_departure_list.append({
                KEY_DEPARTURE_TIME: departure_time_str,
                KEY_STATUS_TEXT: final_status_text, # 最終的なステータステキスト
                KEY_SYSTEM_ROUTE_NAME: system_route_name,
                KEY_DESTINATION_NAME: destination_name,
                KEY_VIA_INFO: via_info,
                KEY_VEHICLE_NO: vehicle_no
            })
    return bus_departure_list


def fetch_and_cache_bus_data(route_id, from_stop_no, to_stop_no_for_request, current_time_unix):
    """指定されたルートのバス情報を取得またはキャッシュから返す"""
    if route_id not in bus_data_cache:
        bus_data_cache[route_id] = {"data": [], "timestamp": 0, "error": None, "data_valid": True}
    
    active_bus_cache = bus_data_cache[route_id]

    if current_time_unix - active_bus_cache.get("timestamp", 0) > BUS_DATA_CACHE_DURATION_SECONDS \
       or not active_bus_cache.get("data_valid", False):
        logging.info(f"バス情報({route_id})を更新します。")
        # HTMLを直接取得
        params = {'fNO': from_stop_no}
        if to_stop_no_for_request:
            params['tNO'] = to_stop_no_for_request
        
        try:
            response = requests.get(BASE_URL, params=params, timeout=10)
            response.raise_for_status()
            html_content = response.content.decode('shift_jis', errors='replace')
            parsed_buses = parse_bus_info_from_html(html_content) # 新しいパース関数を使用

            active_bus_cache["data"] = parsed_buses
            active_bus_cache["error"] = None # 成功時はエラーなし
        except requests.exceptions.Timeout:
            active_bus_cache["error"] = "バス情報取得タイムアウト"
            active_bus_cache["data"] = []
        except requests.exceptions.RequestException as e:
            active_bus_cache["error"] = f"バス情報取得リクエストエラー: {e}"
            active_bus_cache["data"] = []
        except Exception as e:
            active_bus_cache["error"] = f"バス情報パース中に予期せぬエラー: {e}"
            active_bus_cache["data"] = []
            logging.exception(f"バス情報パースエラー ({route_id})")

        active_bus_cache["timestamp"] = current_time_unix
        active_bus_cache["data_valid"] = not active_bus_cache.get("error")
    
    return active_bus_cache.get("data", []), active_bus_cache.get("error")


@app.route('/api/data')
def api_data():
    global weather_cache, weather_fetched_today_g, last_date_weather_checked_g
    requested_direction_group = request.args.get('direction_group', 'to_station_area')
    all_routes_bus_data = {}
    current_dt_tokyo = datetime.datetime.now(TOKYO_TZ)
    current_time_unix = time.time()

    processed_buses_for_display_group = [] # このグループで表示するバスの最終リスト
    combined_errors_for_group = []
    latest_bus_update_time_for_group = 0

    if requested_direction_group == 'to_station_area':
        # 大学発と石倉発を取得し、マージ・ソート
        sanno_buses_raw, sanno_error = fetch_and_cache_bus_data(
            "sanno_to_station", 
            ROUTE_DEFINITIONS["sanno_to_station"]["from_stop_no"],
            ROUTE_DEFINITIONS["sanno_to_station"]["to_stop_no"],
            current_time_unix
        )
        ishikura_buses_raw, ishikura_error = fetch_and_cache_bus_data(
            "ishikura_to_station",
            ROUTE_DEFINITIONS["ishikura_to_station"]["from_stop_no"],
            ROUTE_DEFINITIONS["ishikura_to_station"]["to_stop_no"],
            current_time_unix
        )

        if sanno_error: combined_errors_for_group.append(f"大学発: {sanno_error}")
        if ishikura_error: combined_errors_for_group.append(f"石倉発: {ishikura_error}")
        
        # 最終更新時刻の取得
        sanno_ts = bus_data_cache.get("sanno_to_station", {}).get("timestamp", 0)
        ishikura_ts = bus_data_cache.get("ishikura_to_station", {}).get("timestamp", 0)
        latest_bus_update_time_for_group = max(sanno_ts, ishikura_ts)

        # 車両番号で重複排除（大学発を優先）と情報付加
        sanno_vehicle_numbers = {bus.get(KEY_VEHICLE_NO) for bus in sanno_buses_raw if bus.get(KEY_VEHICLE_NO)}
        
        for bus_info_original in sanno_buses_raw:
            bus_info = bus_info_original.copy()
            time_until_str, is_urgent, seconds_until, departure_dt = calculate_and_format_time_until(
                bus_info.get(KEY_DEPARTURE_TIME, ""), bus_info.get(KEY_STATUS_TEXT, ""), current_dt_tokyo)
            bus_info.update({KEY_TIME_UNTIL: time_until_str, KEY_IS_URGENT: is_urgent, 
                             KEY_SECONDS_UNTIL_DEPARTURE: seconds_until,
                             KEY_DEPARTURE_TIME_ISO: departure_dt.isoformat() if departure_dt else None,
                             KEY_ORIGIN_STOP_NAME_SHORT: ROUTE_DEFINITIONS["sanno_to_station"]["from_stop_name_short"]})
            processed_buses_for_display_group.append(bus_info)

        for bus_info_original in ishikura_buses_raw:
            vehicle_no = bus_info_original.get(KEY_VEHICLE_NO)
            if vehicle_no and vehicle_no in sanno_vehicle_numbers:
                continue # 大学発と重複する車両番号のバスはスキップ
            bus_info = bus_info_original.copy()
            time_until_str, is_urgent, seconds_until, departure_dt = calculate_and_format_time_until(
                bus_info.get(KEY_DEPARTURE_TIME, ""), bus_info.get(KEY_STATUS_TEXT, ""), current_dt_tokyo)
            bus_info.update({KEY_TIME_UNTIL: time_until_str, KEY_IS_URGENT: is_urgent,
                             KEY_SECONDS_UNTIL_DEPARTURE: seconds_until,
                             KEY_DEPARTURE_TIME_ISO: departure_dt.isoformat() if departure_dt else None,
                             KEY_ORIGIN_STOP_NAME_SHORT: ROUTE_DEFINITIONS["ishikura_to_station"]["from_stop_name_short"]})
            processed_buses_for_display_group.append(bus_info)
        
        processed_buses_for_display_group.sort(key=lambda b: (b[KEY_SECONDS_UNTIL_DEPARTURE] == -1, b[KEY_SECONDS_UNTIL_DEPARTURE]))
        
        all_routes_bus_data['to_station_combined'] = {
            "from_stop_name": "大学・石倉", "to_stop_name": "駅",
            "buses_to_display": processed_buses_for_display_group[:MAX_BUSES_TO_FETCH],
            "bus_error_message": "、".join(combined_errors_for_group) if combined_errors_for_group else None,
            "bus_last_updated_str": datetime.datetime.fromtimestamp(latest_bus_update_time_for_group, TOKYO_TZ).strftime('%H:%M:%S') if latest_bus_update_time_for_group > 0 else "N/A",
        }

    elif requested_direction_group == 'to_university_area':
        route_id = "station_to_university_ishikura"
        route_config = ROUTE_DEFINITIONS[route_id]
        
        buses_raw, error = fetch_and_cache_bus_data(
            route_id,
            route_config["from_stop_no"],
            route_config["to_stop_no_ishikura"], # リクエスト時は石倉を指定
            current_time_unix
        )
        if error: combined_errors_for_group.append(f"駅発: {error}")
        latest_bus_update_time_for_group = bus_data_cache.get(route_id, {}).get("timestamp", 0)

        for bus_info_original in buses_raw:
            bus_info = bus_info_original.copy()
            time_until_str, is_urgent, seconds_until, departure_dt = calculate_and_format_time_until(
                bus_info.get(KEY_DEPARTURE_TIME, ""), bus_info.get(KEY_STATUS_TEXT, ""), current_dt_tokyo)
            bus_info.update({KEY_TIME_UNTIL: time_until_str, KEY_IS_URGENT: is_urgent,
                             KEY_SECONDS_UNTIL_DEPARTURE: seconds_until,
                             KEY_DEPARTURE_TIME_ISO: departure_dt.isoformat() if departure_dt else None})
            
            dest_name = bus_info.get(KEY_DESTINATION_NAME, "")
            is_ishikura_stop_only = False
            if "石倉" == dest_name.strip() and "産業能率大学" not in dest_name :
                is_ishikura_stop_only = True
            elif "産業能率大学" in dest_name:
                is_ishikura_stop_only = False
            elif "大山ケーブル" in dest_name : # 大山ケーブル行きは石倉止まりではない
                is_ishikura_stop_only = False
                # ここで、もし大山ケーブル行きを表示したくない場合はフィルタリングする
                # continue 
            bus_info[KEY_IS_ISHIKURA_STOP_ONLY] = is_ishikura_stop_only
            processed_buses_for_display_group.append(bus_info)
        
        # 駅発の場合はソートは不要（神奈中が近い順で返すと仮定）だが、件数制限は行う
        all_routes_bus_data[route_id] = {
            "from_stop_name": route_config["from_stop_name_short"],
            "to_stop_name": route_config["to_stop_name_short"],
            "from_stop_name_full": route_config["from_stop_name_full"],
            "to_stop_name_full": route_config["to_stop_name_full"],
            "buses_to_display": processed_buses_for_display_group[:MAX_BUSES_TO_FETCH],
            "bus_error_message": "、".join(combined_errors_for_group) if combined_errors_for_group else None,
            "bus_last_updated_str": datetime.datetime.fromtimestamp(latest_bus_update_time_for_group, TOKYO_TZ).strftime('%H:%M:%S') if latest_bus_update_time_for_group > 0 else "N/A",
        }
    
    # (天気情報取得、システム健全性判定は前回と同様のロジック)
    weather_data_to_display = {} 
    if last_date_weather_checked_g != current_dt_tokyo.date():
        weather_fetched_today_g = False
        last_date_weather_checked_g = current_dt_tokyo.date()
        logging.info(f"日付変更 ({current_dt_tokyo.date()})。天気取得フラグ解除。")
    if (current_dt_tokyo.hour == WEATHER_FETCH_HOUR and not weather_fetched_today_g) or \
       (not weather_cache.get("data") and not weather_cache.get("error")):
        logging.info(f"{WEATHER_FETCH_HOUR}時台の天気情報更新、または初回取得試行。")
        condition, description, temp, condition_code, error = get_weather_info(OPENWEATHERMAP_API_KEY, WEATHER_LOCATION)
        weather_cache["data"] = {
            "condition": condition, "description": description, "temp_c": temp,
            "condition_code": condition_code, "is_rain": (condition and "rain" in condition.lower()) if condition else False
        }
        weather_cache["error"] = error
        weather_cache["timestamp"] = current_time_unix
        if not error and condition: weather_fetched_today_g = True
    weather_data_to_display = weather_cache.get("data", {})
    weather_data_to_display["error_message"] = weather_cache.get("error")

    system_healthy = True
    system_warning = False
    # エラーチェックは combined_errors_for_group を参照する
    if combined_errors_for_group:
        system_healthy = False
        logging.warning(f"システム状態: バス情報取得エラーのため不健康 - {', '.join(combined_errors_for_group)}")

    current_weather_error = weather_data_to_display.get("error_message")
    if current_weather_error:
        if "APIキー" in current_weather_error or "認証エラー" in current_weather_error :
            system_healthy = False
        else:
            system_warning = True
        logging.warning(f"システム状態: 天気情報に問題 - {current_weather_error}")

    return jsonify(
        weather_data=weather_data_to_display,
        routes_bus_data=all_routes_bus_data,
        system_status={'healthy': system_healthy, 'warning': system_warning}
    )

if __name__ == '__main__':
    # (APIキーチェックなどは前回同様)
    if "YOUR_OPENWEATHERMAP_API_KEY_HERE" in OPENWEATHERMAP_API_KEY:
        logging.warning("ローカルテスト: OpenWeatherMap APIキーが設定されていません。")
    else:
        logging.info("ローカルテスト: OpenWeatherMap APIキーが設定されています。")
    if "YOUR_DISCORD_WEBHOOK_URL_HERE" in DISCORD_WEBHOOK_URL:
        logging.warning("ローカルテスト: Discord Webhook URLが設定されていません。")
    else:
        logging.info("ローカルテスト: Discord Webhook URLが設定されています。")
    logging.info("アプリケーションを開始します。")
    port = int(os.environ.get("PORT", 8080))
    app.run(host='0.0.0.0', port=port, debug=True)
