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
        "from_stop_name_short": "駅", "to_stop_name_short": "大学方面",
        "from_stop_name_full": "伊勢原駅北口", "to_stop_name_full": "大学・石倉方面",
        "group": "to_university_area"
    }
}

MAX_BUSES_TO_FETCH = 10
WEATHER_FETCH_HOUR = 9
BUS_SERVICE_START_HOUR = 6
BUS_SERVICE_START_MINUTE = 20

TOKYO_TZ = pytz.timezone('Asia/Tokyo')

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
KEY_IS_OYAMA_FOR_ISHIKURA = "is_oyama_for_ishikura"
KEY_ORIGIN_STOP_NAME_SHORT = "origin_stop_name_short"
KEY_VEHICLE_NO = "vehicle_no"
KEY_DURATION = "duration_text"
KEY_DELAY_INFO = "delay_info"

app = Flask(__name__)

logging.basicConfig(level=logging.INFO,
                    format='%(asctime)s - %(levelname)s - %(module)s - %(funcName)s - %(message)s',
                    datefmt='%Y-%m-%d %H:%M:%S')

weather_cache = {"data": None, "timestamp": 0, "error": None}
bus_data_cache = {}

weather_fetched_today_g = False
last_date_weather_checked_g = None

WEATHER_CACHE_DURATION_SECONDS = 30 * 60
BUS_DATA_CACHE_DURATION_SECONDS = 10

def send_discord_notification(message):
    if not DISCORD_WEBHOOK_URL or "YOUR_DISCORD_WEBHOOK_URL_HERE" in DISCORD_WEBHOOK_URL:
        logging.warning("Discord Webhook URLが未設定またはプレースホルダーのままのため、通知は送信されません。")
        return
    payload = {"content": message, "username": "バス情報チェッカー"}
    headers = {"Content-Type": "application/json"}
    try:
        response = requests.post(DISCORD_WEBHOOK_URL, data=json.dumps(payload), headers=headers, timeout=5)
        response.raise_for_status()
        logging.info(f"Discord通知送信成功: {message[:50]}...")
    except requests.exceptions.RequestException as e:
        logging.error(f"Discord通知送信失敗: {e}")
    except Exception as e:
        logging.error(f"Discord通知送信中に予期せぬエラー: {e}")

def get_weather_info(api_key, location_query):
    global weather_fetched_today_g
    if not api_key or "YOUR_OPENWEATHERMAP_API_KEY_HERE" in api_key:
        logging.warning("OpenWeatherMap APIキーが未設定またはプレースホルダーのままです。")
        return None, None, None, None, "APIキー未設定"
    api_url = "http://api.openweathermap.org/data/2.5/weather"
    params = {"q": location_query, "appid": api_key, "units": "metric", "lang": "ja"}
    try:
        response = requests.get(api_url, params=params, timeout=10)
        response.raise_for_status()
        data = response.json()
        if data.get("weather") and len(data["weather"]) > 0:
            main_condition = data["weather"][0].get("main")
            description = data["weather"][0].get("description")
            condition_code = data["weather"][0].get("id")
            temp = data.get("main", {}).get("temp")
            logging.info(f"天気情報取得成功 ({location_query}): {main_condition} ({description}), 気温: {temp}°C, Code: {condition_code}")
            weather_fetched_today_g = True
            return main_condition, description, temp, condition_code, None
        return None, None, None, None, "APIレスポンス形式不正"
    except requests.exceptions.Timeout:
        logging.warning(f"天気情報取得タイムアウト ({location_query})")
        return None, None, None, None, "タイムアウト"
    except requests.exceptions.HTTPError as http_err:
        error_message = f"HTTPエラー {http_err.response.status_code}"
        if http_err.response.status_code == 401: error_message = "APIキーが無効か認証エラーです。"
        logging.error(f"天気情報取得HTTPエラー ({location_query}): {http_err}")
        return None, None, None, None, error_message
    except Exception as e:
        logging.exception(f"天気情報取得中に予期せぬエラー ({location_query})")
        return None, None, None, None, f"予期せぬエラー"

def parse_bus_info_from_html(html_content):
    soup = BeautifulSoup(html_content, 'html.parser')
    bus_departure_list = []
    bus_wrappers = soup.select('div.inner2.pa01 > div.wrap')
    for wrap_element in bus_wrappers:
        col01 = wrap_element.find('div', class_='col01')
        system_route_name = "不明"; destination_name = "不明"; via_info = "不明"; vehicle_no = None; duration_text = "不明"
        if col01:
            table_rows = col01.select('table.table01 tr')
            for row in table_rows:
                th_tag = row.find('th'); td_tag = row.find('td')
                if th_tag and td_tag:
                    th_text = th_tag.get_text(strip=True)
                    span_point = td_tag.find('span', class_='point')
                    td_text = span_point.get_text(strip=True) if span_point else td_tag.get_text(strip=True)
                    if "系統" in th_text:
                        if span_point: text_parts = [s for s in span_point.stripped_strings if "バスルートを表示" not in s]; system_route_name = "".join(text_parts).strip()
                        else: system_route_name = td_text
                    elif "行き先" in th_text: destination_name = td_text
                    elif "経由" in th_text: via_info = td_text
                    elif "車両番号" in th_text:
                        vm = re.search(r'([いす盛おつひ平やまた])\s*(\d+)', td_text); vehicle_no = vm.group(0).strip().replace(" ","") if vm else td_text.split("※")[0].split("★")[0].split("Ｔ")[0].strip()
                    elif "所要時分" in th_text: duration_text = td_text.replace("（通常）","").strip()
        col02 = wrap_element.find('div', class_='col02')
        status_text_from_title01 = "情報なし"; departure_time_from_notes = None; delay_info_from_notes = None
        if col02:
            frameBox03 = col02.find('div', class_='frameBox03')
            if frameBox03:
                title_element = frameBox03.find('p', class_='title01')
                if title_element: status_text_from_title01 = title_element.get_text(strip=True)
                departure_area = frameBox03.find('div', class_='placeArea01 departure')
                if departure_area:
                    notes_span = departure_area.find('span', class_='notes')
                    if notes_span:
                        notes_text = notes_span.get_text(strip=True)
                        match_time_in_notes = re.search(r'（(\d{1,2}:\d{2})着予定）', notes_text)
                        if match_time_in_notes: departure_time_from_notes = f"{match_time_in_notes.group(1)}発予定"
                        match_delay_in_notes = re.search(r'現在\s*(\d+\s*分遅れ)', notes_text)
                        if match_delay_in_notes: delay_info_from_notes = match_delay_in_notes.group(1).strip()
        final_status_text = status_text_from_title01; parsed_delay_info = delay_info_from_notes
        delay_match_title = re.search(r'\(現在、?約?(\d+分)遅れ\)?', status_text_from_title01)
        if delay_match_title and not parsed_delay_info: parsed_delay_info = delay_match_title.group(1)
        if departure_time_from_notes and "発予定" in departure_time_from_notes :
            if not ("まもなく" in status_text_from_title01 or "出発しました" in status_text_from_title01 or "遅れ" in status_text_from_title01 or parsed_delay_info): final_status_text = departure_time_from_notes
            else:
                time_part_match_original = re.search(r'\d{1,2}:\d{2}', status_text_from_title01); time_part_match_notes = re.search(r'\d{1,2}:\d{2}', departure_time_from_notes)
                current_status_part = re.sub(r'\S*を\d{1,2}:\d{2}発予定', '', status_text_from_title01, count=1).strip(); current_status_part = re.sub(r'\d{1,2}:\d{2}発', '', current_status_part).strip()
                if time_part_match_notes: final_status_text = f"{time_part_match_notes.group(0)}発 {current_status_part}"
                elif time_part_match_original: final_status_text = f"{time_part_match_original.group(0)}発 {current_status_part}"
                else: final_status_text = current_status_part
                if parsed_delay_info and parsed_delay_info not in final_status_text: final_status_text += f" ({parsed_delay_info})"
        departure_time_str = None
        if "まもなく発車します" in final_status_text or "まもなく到着" in final_status_text: departure_time_str = "まもなく発車します"
        elif "通過しました" in final_status_text or "出発しました" in final_status_text: departure_time_str = "出発しました"
        else:
            match_time_candidate = re.search(r'(\d{1,2}:\d{2})発?', final_status_text); time_part = None
            if match_time_candidate: time_part = match_time_candidate.group(1)
            if "予定通り発車します" in final_status_text:
                if time_part: departure_time_str = f"{time_part}発 (予定通り)"
                else: departure_time_str = "状態不明 (予定通り情報あり)"
            elif parsed_delay_info:
                 if time_part: departure_time_str = f"{time_part}発 ({parsed_delay_info})"
                 else: departure_time_str = f"時刻不明 ({parsed_delay_info})"
            elif "頃発車します" in final_status_text:
                if time_part: departure_time_str = f"{time_part}発 (遅延可能性あり)"
                else: departure_time_str = "状態不明 (遅延情報あり)"
            elif "発予定" in final_status_text:
                if time_part: departure_time_str = f"{time_part}発 (予定)"
                else: departure_time_str = "状態不明 (予定情報あり)"
            elif time_part: departure_time_str = f"{time_part}発"
        if departure_time_str:
            # logging.info(f"[DEBUG PARSE] 行き先: '{destination_name}', 系統: '{system_route_name}', 車両番号: '{vehicle_no}', 状況テキスト: '{final_status_text}', 遅延情報: '{parsed_delay_info}'")
            bus_departure_list.append({
                KEY_DEPARTURE_TIME: departure_time_str, KEY_STATUS_TEXT: final_status_text,
                KEY_SYSTEM_ROUTE_NAME: system_route_name, KEY_DESTINATION_NAME: destination_name,
                KEY_VIA_INFO: via_info, KEY_VEHICLE_NO: vehicle_no, KEY_DURATION: duration_text,
                KEY_DELAY_INFO: parsed_delay_info
            })
    return bus_departure_list

def calculate_and_format_time_until(departure_str, status_text_raw, current_dt_tokyo):
    is_urgent = False; time_until_str = ""; seconds_until = -1; departure_datetime_tokyo = None
    match_time_for_check = re.search(r'(\d{1,2}:\d{2})発', departure_str)
    if match_time_for_check:
        try:
            bus_hour_check, bus_minute_check = map(int, match_time_for_check.group(1).split(':'))
            bus_dt_check = current_dt_tokyo.replace(hour=bus_hour_check, minute=bus_minute_check, second=0, microsecond=0)
            if bus_dt_check < current_dt_tokyo and (current_dt_tokyo.hour >= 20 and bus_hour_check <= 5): bus_dt_check += datetime.timedelta(days=1)
            if bus_dt_check < current_dt_tokyo and "まもなく発車します" in departure_str:
                departure_str = departure_str.replace("まもなく発車します", f"{match_time_for_check.group(1)}発 (発車済みの恐れあり)")
                logging.info(f"発車時刻後の「まもなく」を修正: {departure_str}")
        except Exception as e: logging.warning(f"発車時刻後「まもなく」修正中のエラー: {e}")
    if "まもなく発車します" in departure_str: time_until_str = "まもなく"; is_urgent = True; seconds_until = 10
    elif "出発しました" in departure_str or "発車済みの恐れあり" in departure_str: time_until_str = "出発済み"
    else:
        match = re.search(r'(\d{1,2}:\d{2})発', departure_str)
        if not match:
            if "(予定通り)" in departure_str: time_until_str = ""
            elif "(遅延可能性あり)" in departure_str: time_until_str = "遅延可能性あり"
            elif "(予定)" in departure_str: time_until_str = "予定"
            return time_until_str, is_urgent, seconds_until, departure_datetime_tokyo
        bus_time_str = match.group(1)
        try:
            bus_hour, bus_minute = map(int, bus_time_str.split(':'))
            bus_dt_today_tokyo = current_dt_tokyo.replace(hour=bus_hour, minute=bus_minute, second=0, microsecond=0)
            departure_datetime_tokyo = bus_dt_today_tokyo
            if bus_dt_today_tokyo < current_dt_tokyo and (current_dt_tokyo.hour >= 20 and bus_hour <= 5):
                bus_dt_today_tokyo += datetime.timedelta(days=1); departure_datetime_tokyo = bus_dt_today_tokyo
            if bus_dt_today_tokyo < current_dt_tokyo:
                time_until_str = "出発済み"
                if "予定通り発車します" not in status_text_raw and "通過しました" not in status_text_raw and "出発しました" not in status_text_raw and not any(s in departure_str for s in ["(予定通り)", "(遅延", "(予定)"]):
                     time_until_str = "発車済みの恐れあり"
            else:
                delta = bus_dt_today_tokyo - current_dt_tokyo; total_seconds = int(delta.total_seconds()); seconds_until = total_seconds
                if total_seconds <= 180: is_urgent = True; time_until_str = f"あと{total_seconds // 60}分" if total_seconds >=60 else f"あと{total_seconds}秒"
                else: time_until_str = f"あと{total_seconds // 60}分"
                if total_seconds <=15: is_urgent = True
        except ValueError: time_until_str = f"時刻形式エラー ({departure_str})"
        except Exception: time_until_str = "計算エラー"; logging.exception(f"calculate_and_format_time_untilでエラー: dep={departure_str}, status={status_text_raw}")
    if ("遅延可能性あり" in departure_str or "分遅れ" in departure_str) and seconds_until > 0 : is_urgent = False
    if "まもなく" in time_until_str: is_urgent = True
    return time_until_str, is_urgent, seconds_until, departure_datetime_tokyo

def fetch_and_cache_bus_data(route_id, from_stop_no, to_stop_no_for_request, current_time_unix):
    if route_id not in bus_data_cache: bus_data_cache[route_id] = {"data": [], "timestamp": 0, "error": None, "data_valid": True}
    active_bus_cache = bus_data_cache[route_id]
    if current_time_unix - active_bus_cache.get("timestamp", 0) > BUS_DATA_CACHE_DURATION_SECONDS or not active_bus_cache.get("data_valid", False):
        logging.info(f"バス情報({route_id})を更新します。")
        params = {'fNO': from_stop_no};
        if to_stop_no_for_request: params['tNO'] = to_stop_no_for_request
        try:
            response = requests.get(BASE_URL, params=params, timeout=10); response.raise_for_status()
            html_content = response.content.decode('shift_jis', errors='replace')
            parsed_buses = parse_bus_info_from_html(html_content)
            active_bus_cache["data"] = parsed_buses; active_bus_cache["error"] = None
        except requests.exceptions.Timeout: active_bus_cache["error"] = "バス情報取得タイムアウト"; active_bus_cache["data"] = []
        except requests.exceptions.RequestException as e: active_bus_cache["error"] = f"バス情報取得リクエストエラー: {e}"; active_bus_cache["data"] = []
        except Exception as e: active_bus_cache["error"] = f"バス情報パース中に予期せぬエラー: {e}"; active_bus_cache["data"] = []; logging.exception(f"バス情報パースエラー ({route_id})")
        active_bus_cache["timestamp"] = current_time_unix; active_bus_cache["data_valid"] = not active_bus_cache.get("error")
    return active_bus_cache.get("data", []), active_bus_cache.get("error")

@app.route('/')
def index():
    app.config['ACTIVE_DATA_FETCH_INTERVAL'] = BUS_DATA_CACHE_DURATION_SECONDS
    return render_template('index.html', config=app.config)

@app.route('/api/data')
def api_data():
    global weather_cache, weather_fetched_today_g, last_date_weather_checked_g
    requested_direction_group = request.args.get('direction_group', 'to_station_area')
    all_routes_bus_data = {}; current_dt_tokyo = datetime.datetime.now(TOKYO_TZ)
    current_time_unix = time.time(); processed_buses_for_display_group = []
    combined_errors_for_group = []; latest_bus_update_time_for_group = 0

    if requested_direction_group == 'to_station_area':
        sanno_buses_raw, sanno_error = fetch_and_cache_bus_data("sanno_to_station", ROUTE_DEFINITIONS["sanno_to_station"]["from_stop_no"], ROUTE_DEFINITIONS["sanno_to_station"]["to_stop_no"], current_time_unix)
        ishikura_buses_raw, ishikura_error = fetch_and_cache_bus_data("ishikura_to_station", ROUTE_DEFINITIONS["ishikura_to_station"]["from_stop_no"], ROUTE_DEFINITIONS["ishikura_to_station"]["to_stop_no"], current_time_unix)
        if sanno_error: combined_errors_for_group.append(f"大学発: {sanno_error}")
        if ishikura_error: combined_errors_for_group.append(f"石倉発: {ishikura_error}")
        sanno_ts = bus_data_cache.get("sanno_to_station", {}).get("timestamp", 0); ishikura_ts = bus_data_cache.get("ishikura_to_station", {}).get("timestamp", 0)
        latest_bus_update_time_for_group = max(sanno_ts, ishikura_ts)
        sanno_vehicle_numbers = {bus.get(KEY_VEHICLE_NO) for bus in sanno_buses_raw if bus.get(KEY_VEHICLE_NO)}
        for bus_list_raw, origin_route_id in [(sanno_buses_raw, "sanno_to_station"), (ishikura_buses_raw, "ishikura_to_station")]:
            for bus_info_original in bus_list_raw:
                if origin_route_id == "ishikura_to_station":
                    vehicle_no = bus_info_original.get(KEY_VEHICLE_NO)
                    if vehicle_no and vehicle_no in sanno_vehicle_numbers: continue
                bus_info = bus_info_original.copy()
                time_until_str, is_urgent, seconds_until, departure_dt = calculate_and_format_time_until(bus_info.get(KEY_DEPARTURE_TIME, ""), bus_info.get(KEY_STATUS_TEXT, ""), current_dt_tokyo)
                bus_info.update({KEY_TIME_UNTIL: time_until_str, KEY_IS_URGENT: is_urgent, KEY_SECONDS_UNTIL_DEPARTURE: seconds_until, KEY_DEPARTURE_TIME_ISO: departure_dt.isoformat() if departure_dt else None, KEY_ORIGIN_STOP_NAME_SHORT: ROUTE_DEFINITIONS[origin_route_id]["from_stop_name_short"], KEY_DURATION: bus_info_original.get(KEY_DURATION, "不明"), KEY_DELAY_INFO: bus_info_original.get(KEY_DELAY_INFO)})
                processed_buses_for_display_group.append(bus_info)
        processed_buses_for_display_group.sort(key=lambda b: (b[KEY_SECONDS_UNTIL_DEPARTURE] == -1, b[KEY_SECONDS_UNTIL_DEPARTURE]))
        all_routes_bus_data['to_station_combined'] = {"from_stop_name": "大学・石倉", "to_stop_name": "駅", "buses_to_display": processed_buses_for_display_group[:MAX_BUSES_TO_FETCH], "bus_error_message": "、".join(combined_errors_for_group) if combined_errors_for_group else None, "bus_last_updated_str": datetime.datetime.fromtimestamp(latest_bus_update_time_for_group, TOKYO_TZ).strftime('%H:%M:%S') if latest_bus_update_time_for_group > 0 else "N/A"}
    elif requested_direction_group == 'to_university_area':
        route_id = "station_to_university_ishikura"; route_config = ROUTE_DEFINITIONS[route_id]
        buses_raw, error = fetch_and_cache_bus_data(route_id, route_config["from_stop_no"], route_config["to_stop_no_ishikura"], current_time_unix)
        if error: combined_errors_for_group.append(f"駅発: {error}")
        latest_bus_update_time_for_group = bus_data_cache.get(route_id, {}).get("timestamp", 0)
        for bus_info_original in buses_raw:
            bus_info = bus_info_original.copy()
            time_until_str, is_urgent, seconds_until, departure_dt = calculate_and_format_time_until(bus_info.get(KEY_DEPARTURE_TIME, ""), bus_info.get(KEY_STATUS_TEXT, ""), current_dt_tokyo)
            bus_info.update({KEY_TIME_UNTIL: time_until_str, KEY_IS_URGENT: is_urgent, KEY_SECONDS_UNTIL_DEPARTURE: seconds_until, KEY_DEPARTURE_TIME_ISO: departure_dt.isoformat() if departure_dt else None, KEY_DURATION: bus_info_original.get(KEY_DURATION, "不明"), KEY_DELAY_INFO: bus_info_original.get(KEY_DELAY_INFO)})
            dest_name = bus_info.get(KEY_DESTINATION_NAME, ""); is_ishikura_stop_only = False; is_oyama_for_ishikura = False
            if dest_name:
                if dest_name.strip() == "石倉": is_ishikura_stop_only = True
                elif "大山ケーブル" in dest_name: is_oyama_for_ishikura = True; is_ishikura_stop_only = False
                elif "産業能率大学" in dest_name : is_ishikura_stop_only = False; is_oyama_for_ishikura = False
            bus_info[KEY_IS_ISHIKURA_STOP_ONLY] = is_ishikura_stop_only
            bus_info[KEY_IS_OYAMA_FOR_ISHIKURA] = is_oyama_for_ishikura
            logging.info(f"駅発バス: 行先='{dest_name}', 石倉止まり='{is_ishikura_stop_only}', 大山(石倉経由)='{is_oyama_for_ishikura}', 系統='{bus_info.get(KEY_SYSTEM_ROUTE_NAME)}'")
            processed_buses_for_display_group.append(bus_info)
        all_routes_bus_data[route_id] = {"from_stop_name": route_config["from_stop_name_short"], "to_stop_name": route_config["to_stop_name_short"], "from_stop_name_full": route_config["from_stop_name_full"], "to_stop_name_full": route_config.get("to_stop_name_full", route_config["to_stop_name_short"]), "buses_to_display": processed_buses_for_display_group[:MAX_BUSES_TO_FETCH], "bus_error_message": "、".join(combined_errors_for_group) if combined_errors_for_group else None, "bus_last_updated_str": datetime.datetime.fromtimestamp(latest_bus_update_time_for_group, TOKYO_TZ).strftime('%H:%M:%S') if latest_bus_update_time_for_group > 0 else "N/A"}
    weather_data_to_display = {}
    if last_date_weather_checked_g != current_dt_tokyo.date(): weather_fetched_today_g = False; last_date_weather_checked_g = current_dt_tokyo.date(); logging.info(f"日付変更 ({current_dt_tokyo.date()})。天気取得フラグ解除。")
    if (current_dt_tokyo.hour == WEATHER_FETCH_HOUR and not weather_fetched_today_g) or (not weather_cache.get("data") and not weather_cache.get("error")):
        logging.info(f"{WEATHER_FETCH_HOUR}時台の天気情報更新、または初回取得試行。")
        condition, description, temp, condition_code, error = get_weather_info(OPENWEATHERMAP_API_KEY, WEATHER_LOCATION)
        weather_cache["data"] = {"condition": condition, "description": description, "temp_c": temp, "condition_code": condition_code, "is_rain": (condition and "rain" in condition.lower()) if condition else False}
        weather_cache["error"] = error; weather_cache["timestamp"] = current_time_unix
        if not error and condition: weather_fetched_today_g = True
    weather_data_to_display = weather_cache.get("data", {}); weather_data_to_display["error_message"] = weather_cache.get("error")
    system_healthy = True; system_warning = False
    if combined_errors_for_group: system_healthy = False; logging.warning(f"システム状態: バス情報取得エラーのため不健康 - {', '.join(combined_errors_for_group)}")
    current_weather_error = weather_data_to_display.get("error_message")
    if current_weather_error:
        if "APIキー" in current_weather_error or "認証エラー" in current_weather_error : system_healthy = False
        else: system_warning = True
        logging.warning(f"システム状態: 天気情報に問題 - {current_weather_error}")
    return jsonify(weather_data=weather_data_to_display, routes_bus_data=all_routes_bus_data, system_status={'healthy': system_healthy, 'warning': system_warning})

if __name__ == '__main__':
    if "YOUR_OPENWEATHERMAP_API_KEY_HERE" in OPENWEATHERMAP_API_KEY: logging.warning("ローカルテスト: OpenWeatherMap APIキーが設定されていません。")
    else: logging.info("ローカルテスト: OpenWeatherMap APIキーが設定されています。")
    if "YOUR_DISCORD_WEBHOOK_URL_HERE" in DISCORD_WEBHOOK_URL: logging.warning("ローカルテスト: Discord Webhook URLが設定されていません。")
    else: logging.info("ローカルテスト: Discord Webhook URLが設定されています。")
    logging.info("アプリケーションを開始します。")
    port = int(os.environ.get("PORT", 8080))
    app.run(host='0.0.0.0', port=port, debug=True)
