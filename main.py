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

# --- アプリケーション設定 ---
OPENWEATHERMAP_API_KEY = "28482976c81657127a816a47f53cc3d2" # OpenWeatherMap APIキー
DISCORD_WEBHOOK_URL = "https://discord.com/api/webhooks/1375497603466395749/4QtOWTUk-_44xc8-RVmhm3imPatU4yiEuRj1NR1j5PryEkbik98A204uJ3069nye_GNI" # Discord Webhook URL

WEATHER_LOCATION = "Isehara,JP" # 天気情報を取得する地域
BASE_URL = "http://real.kanachu.jp/pc/displayapproachinfo" # 神奈中バスロケのベースURL

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
        "to_stop_no_sanno": "18137",
        "to_stop_no_ishikura": "18124",
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
                    format='%(asctime)s - %(levelname)s - %(module)s:%(lineno)d - %(funcName)s - %(message)s',
                    datefmt='%Y-%m-%d %H:%M:%S')

weather_cache = {"data": None, "timestamp": 0, "error": None}
bus_data_cache = {}

weather_fetched_today_g = False
last_date_weather_checked_g = None

WEATHER_CACHE_DURATION_SECONDS = 30 * 60
BUS_DATA_CACHE_DURATION_SECONDS = 10

def send_discord_notification(message):
    if not DISCORD_WEBHOOK_URL or "YOUR_DISCORD_WEBHOOK_URL_HERE" in DISCORD_WEBHOOK_URL:
        logging.warning("Discord Webhook URLが未設定またはプレースホルダーのため、通知は送信されません。")
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
        logging.warning("OpenWeatherMap APIキーが未設定またはプレースホルダーのため、天気情報は取得できません。")
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
        logging.warning(f"天気APIからのレスポンス形式が不正です: {data}")
        return None, None, None, None, "APIレスポンス形式不正"
    except requests.exceptions.Timeout:
        logging.warning(f"天気情報取得タイムアウト ({location_query})")
        return None, None, None, None, "タイムアウト"
    except requests.exceptions.HTTPError as http_err:
        error_message = f"HTTPエラー {http_err.response.status_code}"
        if http_err.response.status_code == 401: error_message = "天気APIキーが無効か認証エラーです。"
        logging.error(f"天気情報取得HTTPエラー ({location_query}): {http_err}")
        return None, None, None, None, error_message
    except Exception as e:
        logging.exception(f"天気情報取得中に予期せぬエラー ({location_query})")
        return None, None, None, None, "予期せぬエラー"

def parse_bus_info_from_html(html_content):
    soup = BeautifulSoup(html_content, 'html.parser')
    bus_departure_list = []
    bus_wrappers = soup.select('div.inner2.pa01 > div.wrap')

    # ページ全体のエラーメッセージや遅延メッセージを取得
    # general_error_message_element = soup.select_one('div.frameattention01.mb10 p') # 例: 「現在、運行情報を提供しておりません」など
    # general_error_message = general_error_message_element.get_text(strip=True) if general_error_message_element else None
    # if general_error_message:
    #     logging.info(f"ページ全体のメッセージ: {general_error_message}")
        # これをどのように活用するかは別途検討 (全バスに影響する場合など)

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
        status_text_from_title01 = "情報なし"; departure_time_from_notes = None; parsed_delay_info = None

        if col02:
            frameBox03 = col02.find('div', class_='frameBox03')
            if frameBox03:
                title_element = frameBox03.find('p', class_='title01')
                if title_element:
                    status_text_from_title01 = title_element.get_text(strip=True)
                    # "title01" から遅延情報を抽出: 「(現在、約X分遅れ)」「(X分程度遅れて通過しました)」など
                    delay_match_title = re.search(r'(?:現在、?約?|約)?(\d+\s*分(?:程度)?(?:遅れ|遅れております|遅れて到着する見込み|遅れて通過しました))', status_text_from_title01, re.IGNORECASE)
                    if delay_match_title:
                        parsed_delay_info = delay_match_title.group(1).strip()
                        logging.info(f"遅延情報(title01より): {parsed_delay_info}")
                    elif "遅れて到着する見込みです" in status_text_from_title01 and not parsed_delay_info : # より曖昧な遅延
                        parsed_delay_info = "遅延"
                        logging.info(f"遅延情報(title01より、曖昧): {parsed_delay_info}")


                departure_area = frameBox03.find('div', class_='placeArea01 departure')
                if departure_area:
                    notes_span = departure_area.find('span', class_='notes')
                    if notes_span:
                        notes_text = notes_span.get_text(strip=True)
                        match_time_in_notes = re.search(r'（(\d{1,2}:\d{2})着予定）', notes_text) # これは到着予定なので注意
                        if match_time_in_notes:
                             # departure_time_from_notes = f"{match_time_in_notes.group(1)}発予定" # 発車時刻とは異なる
                             pass # 到着予定時刻は現時点では使用しない

                        # "notes" からも遅延情報を抽出 (より具体的な場合があるため)
                        delay_match_notes = re.search(r'現在\s*(\d+\s*分(?:程度)?遅れ)', notes_text)
                        if delay_match_notes:
                            # title01から取れた情報よりもnotesの方が詳細なら上書き、または併記を検討
                            # ここでは、より具体的な "X分遅れ" を優先する（title01が "遅延" の場合など）
                            if parsed_delay_info == "遅延" or not parsed_delay_info:
                                parsed_delay_info = delay_match_notes.group(1).strip()
                            logging.info(f"遅延情報(notesより): {parsed_delay_info}")


        # 発車時刻文字列の整形
        # 神奈中サイトの表示時刻は、多くの場合「遅延を考慮した予想時刻」となっている。
        # そのため、status_text_from_title01 から時刻部分を抽出し、parsed_delay_info は補足情報として扱う。
        departure_time_str = status_text_from_title01 # 基本はそのまま使う

        # 時刻とそれ以外の情報を分離する試み
        time_match = re.search(r'(\d{1,2}:\d{2})\s*(発|着|通過)?', status_text_from_title01)
        actual_display_time = time_match.group(1) if time_match else None
        status_suffix = status_text_from_title01.replace(time_match.group(0), '').strip() if time_match else status_text_from_title01

        if actual_display_time:
            departure_time_str = f"{actual_display_time}発" # 基本は「発」をつける
            # status_suffix から不要な情報を削る（例：(予定通り), (遅延可能性あり)など）
            # これらは statusLabel で別途判定するため、時刻表示からは除く
            status_suffix_cleaned = status_suffix
            status_phrases_to_remove_from_time = [
                r"を\s*発車します", r"\s*発車します", r"\s*ごろ発車します", r"\s*に発車します",
                r"\s*予定通り発車します", r"\s*予定です", r"\s*予定", r"を通過しました", r"で通過しました",
                r"\s*遅れて到着する見込みです", r"に到着する見込みです", r"\s*到着しました"
            ]
            for phrase in status_phrases_to_remove_from_time:
                status_suffix_cleaned = re.sub(phrase, '', status_suffix_cleaned, flags=re.IGNORECASE).strip()

            # カッコ内の遅延情報は parsed_delay_info で扱うので、時刻表示からは除く
            status_suffix_cleaned = re.sub(r'\s*\(現在、?約?\d+\s*分(?:程度)?(?:遅れ|遅れております|遅れて到着する見込み|遅れて通過しました)\)', '', status_suffix_cleaned, flags=re.IGNORECASE).strip()
            status_suffix_cleaned = re.sub(r'\s*\(\s*\d+\s*分遅れ\s*\)', '', status_suffix_cleaned).strip() # カッコのみの遅延も
            status_suffix_cleaned = re.sub(r'\s*\(遅延可能性あり\)', '', status_suffix_cleaned).strip()
            status_suffix_cleaned = re.sub(r'\s*\(予定通り\)', '', status_suffix_cleaned).strip()
            status_suffix_cleaned = re.sub(r'\s*\(予定\)', '', status_suffix_cleaned).strip()


            if status_suffix_cleaned and status_suffix_cleaned != "発": # 何か補足情報があればつける
                departure_time_str += f" {status_suffix_cleaned}"
            departure_time_str = departure_time_str.replace("発 発", "発").strip() # "発"の重複を避ける
        else: # 時刻が抽出できなかった場合
            departure_time_str = status_text_from_title01 # 元のテキストをそのまま使う


        # 特殊な状態の判定 (まもなく発車, 出発済み) は departure_time_str の最終形で行う
        if "まもなく発車します" in departure_time_str or "まもなく到着" in departure_time_str : # status_text_from_title01 に基づく
             # 時刻部分が残るように調整
            if actual_display_time: departure_time_str = f"{actual_display_time}発" # 「まもなく」でも時刻は表示
            else: departure_time_str = "まもなく発車" # 時刻不明の場合
        elif "出発しました" in status_text_from_title01 or "通過しました" in status_text_from_title01:
            if actual_display_time: departure_time_str = f"{actual_display_time}発" # 出発済みでも時刻は表示
            else: departure_time_str = "出発済み" # 時刻不明の場合


        if not departure_time_str and parsed_delay_info: # 時刻不明だが遅延情報はある場合
            departure_time_str = f"時刻不明 ({parsed_delay_info})"
        elif not departure_time_str:
             departure_time_str = "情報なし"


        bus_departure_list.append({
            KEY_DEPARTURE_TIME: departure_time_str.strip(), # 整形後の時刻文字列
            KEY_STATUS_TEXT: status_text_from_title01,      # 元のステータス文字列 (JSでの判定用)
            KEY_SYSTEM_ROUTE_NAME: system_route_name, KEY_DESTINATION_NAME: destination_name,
            KEY_VIA_INFO: via_info, KEY_VEHICLE_NO: vehicle_no, KEY_DURATION: duration_text,
            KEY_DELAY_INFO: parsed_delay_info # 抽出した遅延情報を格納
        })
    return bus_departure_list


def calculate_and_format_time_until(departure_str, status_text_raw, current_dt_tokyo, delay_info=None):
    is_urgent = False; time_until_str = ""; seconds_until = -1; departure_datetime_tokyo = None

    # status_text_raw (神奈中サイトの元々の文字列) を使って「まもなく」や「出発済み」を判定
    if "まもなく発車します" in status_text_raw or "まもなく到着" in status_text_raw:
        time_until_str = "まもなく"; is_urgent = True; seconds_until = 10 # 仮の秒数
        # 時刻がdeparture_strにあればそれをdeparture_datetime_tokyoのベースに
        match_time_for_soon = re.search(r'(\d{1,2}:\d{2})', departure_str)
        if match_time_for_soon:
            try:
                bus_hour, bus_minute = map(int, match_time_for_soon.group(1).split(':'))
                departure_datetime_tokyo = current_dt_tokyo.replace(hour=bus_hour, minute=bus_minute, second=0, microsecond=0)
                if departure_datetime_tokyo < current_dt_tokyo and (current_dt_tokyo.hour >= 20 and bus_hour <=5):
                    departure_datetime_tokyo += datetime.timedelta(days=1)
                # まもなくの場合、seconds_until は実際の時刻との差ではなく、固定値や非常に小さい値でも良い
                # seconds_until = max(0, int((departure_datetime_tokyo - current_dt_tokyo).total_seconds())) # これだとカウントダウンしてしまう
            except: pass # 時刻パース失敗時は何もしない
        return time_until_str, is_urgent, seconds_until, departure_datetime_tokyo

    elif "出発しました" in status_text_raw or "通過しました" in status_text_raw or "発車済みの恐れあり" in departure_str :
        time_until_str = "出発済み"
        # 出発済みの場合も時刻があれば設定
        match_time_for_departed = re.search(r'(\d{1,2}:\d{2})', departure_str)
        if match_time_for_departed:
            try:
                bus_hour, bus_minute = map(int, match_time_for_departed.group(1).split(':'))
                departure_datetime_tokyo = current_dt_tokyo.replace(hour=bus_hour, minute=bus_minute, second=0, microsecond=0)
                # 日付跨ぎは通常不要だが、深夜バスなら考慮
            except: pass
        return time_until_str, is_urgent, seconds_until, departure_datetime_tokyo

    # 上記以外の場合、departure_str から時刻をパースしてカウントダウン計算
    match = re.search(r'(\d{1,2}:\d{2})発?', departure_str) # departure_strは整形後の時刻文字列を期待
    if not match:
        if delay_info : time_until_str = f"({delay_info})" # 時刻不明だが遅延情報はある場合
        elif "(予定通り)" in departure_str : time_until_str = ""
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
            time_until_str = "出発済み" # カウントダウンの結果、過ぎている
            # status_text_raw に基づく判定が優先されるべきだが、念のため
        else:
            delta = bus_dt_today_tokyo - current_dt_tokyo; total_seconds = int(delta.total_seconds()); seconds_until = total_seconds
            # 緊急度の判定 (3分以内)
            if total_seconds <= 180: is_urgent = True
            if total_seconds <=15: is_urgent = True # 特に15秒以内は強調

            # time_until_str はここでは設定しない (JS側で秒数から生成)
            time_until_str = "" # formatSecondsToCountdown に任せるための初期値

    except ValueError: time_until_str = f"時刻形式エラー ({departure_str})"
    except Exception: time_until_str = "計算エラー"; logging.exception(f"calculate_and_format_time_untilでエラー: dep={departure_str}, status={status_text_raw}")

    # 遅延情報があれば緊急度を下げる (ただし「まもなく」の場合は除く)
    if delay_info and not ("まもなく発車します" in status_text_raw or "まもなく到着" in status_text_raw):
        is_urgent = False

    return time_until_str, is_urgent, seconds_until, departure_datetime_tokyo


def fetch_and_cache_bus_data(route_id, from_stop_no, to_stop_no_for_request, current_time_unix):
    # (キャッシュロジックは変更なし)
    if route_id not in bus_data_cache:
        bus_data_cache[route_id] = {"data": [], "timestamp": 0, "error": None, "data_valid": True}
    active_bus_cache = bus_data_cache[route_id]
    if current_time_unix - active_bus_cache.get("timestamp", 0) > BUS_DATA_CACHE_DURATION_SECONDS \
       or not active_bus_cache.get("data_valid", False):
        logging.info(f"バス情報({route_id})を神奈中サイトから更新します。")
        params = {'fNO': from_stop_no}
        if to_stop_no_for_request: params['tNO'] = to_stop_no_for_request
        try:
            response = requests.get(BASE_URL, params=params, timeout=10)
            response.raise_for_status()
            html_content = response.content.decode('shift_jis', errors='replace')
            parsed_buses = parse_bus_info_from_html(html_content)
            active_bus_cache["data"] = parsed_buses; active_bus_cache["error"] = None
        except requests.exceptions.Timeout: active_bus_cache["error"] = "バス情報取得タイムアウト"; active_bus_cache["data"] = []
        except requests.exceptions.RequestException as e: active_bus_cache["error"] = f"バス情報取得リクエストエラー: {e}"; active_bus_cache["data"] = []
        except Exception as e: active_bus_cache["error"] = f"バス情報処理中に予期せぬエラー: {e}"; active_bus_cache["data"] = []; logging.exception(f"バス情報処理エラー ({route_id})")
        active_bus_cache["timestamp"] = current_time_unix
        active_bus_cache["data_valid"] = not active_bus_cache.get("error")
    return active_bus_cache.get("data", []), active_bus_cache.get("error")

@app.route('/')
def index():
    # (変更なし)
    app.config['ACTIVE_DATA_FETCH_INTERVAL'] = BUS_DATA_CACHE_DURATION_SECONDS
    return render_template('index.html', config=app.config)

@app.route('/api/data')
def api_data():
    # (天気情報取得、エラーハンドリングなどは変更なし)
    global weather_cache, weather_fetched_today_g, last_date_weather_checked_g
    requested_direction_group = request.args.get('direction_group', 'to_station_area')
    all_routes_bus_data = {}
    current_dt_tokyo = datetime.datetime.now(TOKYO_TZ)
    current_time_unix = time.time()

    # 各ルートのバス情報処理
    if requested_direction_group == 'to_station_area' or requested_direction_group == 'to_university_area':
        processed_buses_for_display_group = []
        combined_errors_for_group = []
        latest_bus_update_time_for_group = 0
        
        routes_to_process_for_group = []
        if requested_direction_group == 'to_station_area':
            routes_to_process_for_group = [("sanno_to_station", ROUTE_DEFINITIONS["sanno_to_station"]), ("ishikura_to_station", ROUTE_DEFINITIONS["ishikura_to_station"])]
            display_group_id = 'to_station_combined'
            display_from_name = "大学・石倉"
            display_to_name = "駅"
        else: # to_university_area
            routes_to_process_for_group = [("station_to_university_ishikura", ROUTE_DEFINITIONS["station_to_university_ishikura"])]
            display_group_id = "station_to_university_ishikura"
            display_from_name = ROUTE_DEFINITIONS["station_to_university_ishikura"]["from_stop_name_short"]
            display_to_name = ROUTE_DEFINITIONS["station_to_university_ishikura"]["to_stop_name_short"]

        sanno_vehicle_numbers_if_applicable = set()
        if requested_direction_group == 'to_station_area': # 重複排除用
             sanno_buses_for_filter, _ = fetch_and_cache_bus_data("sanno_to_station", ROUTE_DEFINITIONS["sanno_to_station"]["from_stop_no"], ROUTE_DEFINITIONS["sanno_to_station"]["to_stop_no"], current_time_unix)
             sanno_vehicle_numbers_if_applicable = {bus.get(KEY_VEHICLE_NO) for bus in sanno_buses_for_filter if bus.get(KEY_VEHICLE_NO)}


        for route_key, route_config in routes_to_process_for_group:
            to_stop_no_req = route_config.get("to_stop_no") # to_station_area
            if route_key == "station_to_university_ishikura": # to_university_area
                to_stop_no_req = route_config.get("to_stop_no_ishikura")

            buses_raw, error = fetch_and_cache_bus_data(route_key, route_config["from_stop_no"], to_stop_no_req, current_time_unix)
            if error: combined_errors_for_group.append(f"{route_config['from_stop_name_short']}発: {error}")
            latest_bus_update_time_for_group = max(latest_bus_update_time_for_group, bus_data_cache.get(route_key, {}).get("timestamp", 0))

            for bus_info_original in buses_raw:
                if route_key == "ishikura_to_station": # 石倉発の時だけ重複チェック
                    vehicle_no = bus_info_original.get(KEY_VEHICLE_NO)
                    if vehicle_no and vehicle_no in sanno_vehicle_numbers_if_applicable:
                        continue # 大学発と重複ならスキップ
                
                bus_info = bus_info_original.copy()
                time_until_str, is_urgent, seconds_until, departure_dt = calculate_and_format_time_until(
                    bus_info.get(KEY_DEPARTURE_TIME, ""),
                    bus_info.get(KEY_STATUS_TEXT, ""), # 元のステータスを渡す
                    current_dt_tokyo,
                    bus_info.get(KEY_DELAY_INFO) # 遅延情報も渡す
                )
                bus_info.update({
                    KEY_TIME_UNTIL: time_until_str, # calculate_and_format_time_until からのフォールバック用
                    KEY_IS_URGENT: is_urgent,
                    KEY_SECONDS_UNTIL_DEPARTURE: seconds_until,
                    KEY_DEPARTURE_TIME_ISO: departure_dt.isoformat() if departure_dt else None,
                    KEY_ORIGIN_STOP_NAME_SHORT: route_config["from_stop_name_short"], # これが「大学」または「石倉」になる
                    # KEY_DELAY_INFO は bus_info_original から引き継がれている
                })

                if route_key == "station_to_university_ishikura":
                    dest_name = bus_info.get(KEY_DESTINATION_NAME, "").strip()
                    is_ishikura_stop_only = False; is_oyama_for_ishikura = False
                    if dest_name == "石倉": is_ishikura_stop_only = True
                    elif "大山ケーブル" in dest_name: is_oyama_for_ishikura = True
                    bus_info[KEY_IS_ISHIKURA_STOP_ONLY] = is_ishikura_stop_only
                    bus_info[KEY_IS_OYAMA_FOR_ISHIKURA] = is_oyama_for_ishikura
                
                processed_buses_for_display_group.append(bus_info)

        if requested_direction_group == 'to_station_area': # 駅方面はソート
             processed_buses_for_display_group.sort(key=lambda b: (b[KEY_SECONDS_UNTIL_DEPARTURE] == -1, b[KEY_SECONDS_UNTIL_DEPARTURE]))

        all_routes_bus_data[display_group_id] = {
            "from_stop_name": display_from_name,
            "to_stop_name": display_to_name,
            "buses_to_display": processed_buses_for_display_group[:MAX_BUSES_TO_FETCH],
            "bus_error_message": "、".join(combined_errors_for_group) if combined_errors_for_group else None,
            "bus_last_updated_str": datetime.datetime.fromtimestamp(latest_bus_update_time_for_group, TOKYO_TZ).strftime('%H:%M:%S') if latest_bus_update_time_for_group > 0 else "N/A",
        }


    # 天気情報 (変更なし)
    weather_data_to_display = {}
    if last_date_weather_checked_g != current_dt_tokyo.date():
        weather_fetched_today_g = False; last_date_weather_checked_g = current_dt_tokyo.date()
        logging.info(f"日付変更 ({current_dt_tokyo.date()})。天気取得フラグ解除。")
    if (current_dt_tokyo.hour == WEATHER_FETCH_HOUR and not weather_fetched_today_g) or \
       (not weather_cache.get("data") and not weather_cache.get("error")):
        logging.info(f"{WEATHER_FETCH_HOUR}時台の天気情報更新、または初回取得試行。")
        condition, description, temp, condition_code, error = get_weather_info(OPENWEATHERMAP_API_KEY, WEATHER_LOCATION)
        weather_cache["data"] = {"condition": condition, "description": description, "temp_c": temp, "condition_code": condition_code, "is_rain": (condition and "rain" in condition.lower()) if condition else False}
        weather_cache["error"] = error; weather_cache["timestamp"] = current_time_unix
        if not error and condition: weather_fetched_today_g = True
    weather_data_to_display = weather_cache.get("data", {}); weather_data_to_display["error_message"] = weather_cache.get("error")

    # システム健全性 (変更なし)
    system_healthy = True; system_warning = False
    if combined_errors_for_group : # ルートごとのエラーではなく、そのグループのエラーで判定
        system_healthy = False
        logging.warning(f"システム状態: バス情報取得エラーのため不健康 - {', '.join(combined_errors_for_group)}")
    current_weather_error = weather_data_to_display.get("error_message")
    if current_weather_error:
        if "APIキー" in current_weather_error or "認証エラー" in current_weather_error : system_healthy = False
        else: system_warning = True
        logging.warning(f"システム状態: 天気情報に問題 - {current_weather_error}")

    return jsonify(
        weather_data=weather_data_to_display,
        routes_bus_data=all_routes_bus_data,
        system_status={'healthy': system_healthy, 'warning': system_warning}
    )

if __name__ == '__main__':
    # (変更なし)
    if "YOUR_OPENWEATHERMAP_API_KEY_HERE" in OPENWEATHERMAP_API_KEY: logging.warning("ローカルテスト: OpenWeatherMap APIキーが設定されていません。")
    else: logging.info("ローカルテスト: OpenWeatherMap APIキーが設定されています。")
    if "YOUR_DISCORD_WEBHOOK_URL_HERE" in DISCORD_WEBHOOK_URL: logging.warning("ローカルテスト: Discord Webhook URLが設定されていません。")
    else: logging.info("ローカルテスト: Discord Webhook URLが設定されています。")
    logging.info("アプリケーションを開始します。")
    port = int(os.environ.get("PORT", 8080))
    app.run(host='0.0.0.0', port=port, debug=True)
