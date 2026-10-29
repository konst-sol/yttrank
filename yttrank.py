#!/usr/bin/env python3
#!/usr/bin/env -S uv run
# -*- mode: python; coding: utf-8; -*-
# /// script
# requires-python = ">=3.10"
# dependencies = [
#     "requests",
# ]
# ///
import sys
import os
import re
# time
import time
from datetime import datetime, date, timedelta
from zoneinfo import ZoneInfo
# files
from pathlib import Path
import stat
# options
import argparse
import configparser
# download
import requests
# multithreading
from concurrent.futures import ThreadPoolExecutor, as_completed
import threading
# sql
import sqlite3
import csv
# debug
import traceback
from pprint import pprint


API_BASE_URL = 'https://www.googleapis.com/youtube/v3/'

if os.name == 'posix':
    YTTRANK_DIR = Path.home()/'.yttrank'
elif os.name == 'nt':
    YTTRANK_DIR = Path.home()/'yttrank'
#YTTRANK_DIR.mkdir(parents=True, exist_ok=True)

API_KEY_FILE = YTTRANK_DIR/'apikey.txt'
UNIT_COUNT_FILE = YTTRANK_DIR/'units.txt'
CONFIG_FILE = YTTRANK_DIR/'config.ini'

def _logger(level, *args):
    if config.LOG_FILE:
        with config.LOG_FILE.open('a') as f:
            print(config.DB_NAME, '-', datetime.now(), '-', *args, file=f)
    if level in ('info', 'critical') or config.VERBOSE:
        print(*args)

def log_critical(*args): _logger('critical', *args)
def log_error(*args): _logger('error', *args)
def log_info(*args): _logger('info', *args)
def log_debug(*args): _logger('debug', *args)

__ = '—'*78 # Горизонтальная линия


class Config:
    def __init__(self, db_name):
        self.DB_NAME = db_name
        self.DB_FILE = YTTRANK_DIR/(self.DB_NAME+'.db')
        self.LOG_FILE = YTTRANK_DIR/(self.DB_NAME+'.log')

        config = configparser.ConfigParser()
        try:
            config.read(CONFIG_FILE)
            if db_name in config:
                section = config[db_name]
            else:
                section = config['DEFAULT']

            self.LOG_FILE = section.get('LOG_FILE', self.LOG_FILE)
            self.MAX_CONCURRENT = section.getint('MAX_CONCURRENT', 8) # Количество потоков
            self.REQUEST_TIMEOUT = section.getfloat('REQUEST_TIMEOUT', 15.0) # секунд
            self.SCAN_ALL_VIDEOS = section.getboolean('SCAN_ALL_VIDEOS', False) # True = сканировать все видео, False = сканировать за текущий и предыдущий месяцы
            self.SHOW_SHORTS = section.getboolean('SHOW_SHORTS', False)
            self.VERBOSE = section.getboolean('VERBOSE', False)

        except (ValueError, configparser.Error) as e:
            sys.exit(f'Config file error: {e}')


API_KEY = None
def load_api_key():
    global API_KEY
    api_key_help = f'''
=======================================================
 КАК ПОЛУЧИТЬ API KEY ДЛЯ GOOGLE API YOUTUBE CLIENT:
=======================================================
1. Перейдите в Google Cloud Console: https://console.cloud.google.com/
2. Создайте новый проект (или выберите существующий).
3. В меню выберите «APIs & Services» → «Library» и включите «YouTube Data API v3».
4. Перейдите в раздел «APIs & Services» → «Credentials» в левом меню.
5. Нажмите «+ Create Credentials» и выберите «API key».
6. Создайте файл {API_KEY_FILE} и скопируйте в него ваш полученный ключ.
7. Выполните в терминале команду: chmod 600 {API_KEY_FILE}.

Подробная официальная инструкция от Google:
👉 https://docs.cloud.google.com/docs/authentication/api-keys
=======================================================
'''

    if not API_KEY_FILE.exists():
        print(f"❌ Ошибка: Файл {API_KEY_FILE} с api key не найден")
        print(api_key_help)
        sys.exit()

    if os.name == 'posix':
        if API_KEY_FILE.stat().st_mode & (stat.S_IRWXG | stat.S_IRWXO):
            print(f'❌ Ошибка безопасности: '
                  f'файл {API_KEY_FILE} имеет слишком открытые права '
                  f'{oct(stat.S_IMODE(API_KEY_FILE.stat().st_mode))}'
                  )
            print(f'Выполните в терминале команду: chmod 600 {API_KEY_FILE}')
            sys.exit()
    elif os.name == 'nt':
        # FIXME
        pass

    with API_KEY_FILE.open() as f:
        API_KEY = f.read().strip()

# ----------------------------------------------------------------------

def get_youtube_id(url):
    pattern = r"(?:youtube(?:-nocookie)?\.com/(?:watch\?.*v=|embed/|shorts/|live/|v/|e/)|youtu\.be/)([A-Za-z0-9_-]{11})"
    match = re.search(pattern, url)
    return match.group(1) if match else None

# ----------------------------------------------------------------------

def _check_channel(c):
    if c.startswith('UC'):
        if len(c) != 24:
            raise ValueError(f'некорректный ID канала: "{c}" (длина)')
    elif c.startswith('PL'):
        if len(c) != 34:
            raise ValueError(f'некорректный ID плейлиста: "{c}" (длина)')
    else:
        raise ValueError(f'некорректный ID: "{c}"')

def add_channel(ch):
    if ch in channels:
        print(f'Канал {ch} уже в БД.')
        return

    if ch[:2] in ('UC', 'PL'):
        db_cursor.execute(
            'UPDATE channels SET status = 1 WHERE id = ? RETURNING id', (ch,))
        if db_cursor.fetchone():
            print(f'Канал {ch} существовал, статус обновлен в БД.')
            return
    if ch.startswith('@'):
        db_cursor.execute(
            'UPDATE channels SET status = 1 WHERE customUrl = ? RETURNING id', (ch,))
        row = db_cursor.fetchone()
        if row:
            print(f'Канал {row[0]} существовал, статус обновлен в БД.')
            return

    status = 1
    is_playlist = 0
    if ch.startswith('https://www.youtube.com/playlist?list='):
        url = ch
        is_playlist = 1
    elif ch.startswith('https://'):
        url = ch
    elif ch.startswith('UC'):
        url = 'https://www.youtube.com/channel/'+ch
    elif ch.startswith('PL'):
        url = 'https://www.youtube.com/playlist?list='+ch
        is_playlist = 1
    elif ch.startswith('@'):
        url = 'https://www.youtube.com/'+ch
    else:
        raise ValueError(f'некорректный ID канала: "{ch}"')
    id, title = get_channel_id_from_url(url)

    if not id:
        print(f'Не удалось получить ID: {ch}')
        return

    if id in channels:
        print(f'Канал {id} уже в БД.')
        return

    _check_channel(id)
    db_cursor.execute(
        'UPDATE channels SET status = 1 WHERE id = ? RETURNING id', (id,))
    if db_cursor.fetchone():
        print(f'Канал {id} существовал, статус обновлен в БД.')
        return

    db_cursor.execute("""INSERT INTO channels
    (id, title, status, is_playlist, added)
    VALUES (:id, :title, :status, :is_playlist, datetime('now'))""", locals())
    print(f'Канал {id} ({title}) добавлен в БД.')
    return id


def get_channel_id_from_url(url):
    channel_id = title = None
    data = session.get(url, timeout=30.0).text
    matchobj = re.search(r'playlist\?list=(PL[\w-]{32})"', url)
    if matchobj:
        playlist_id = matchobj.group(1)
        matchobj = re.search(r'property="og:title" content="([^"]+)"', data)
        if matchobj:
            title = matchobj.group(1)
            return playlist_id, title
    # Ищем externalId в метаданных страницы
    matchobj = re.search(r'"externalId":"(UC[\w-]{22})"', data)
    if matchobj:
        channel_id = matchobj.group(1)
        matchobj = re.search(r'"channelMetadataRenderer":\{"title":"([^"]+)"', data)
        if matchobj:
            title = matchobj.group(1)
    return channel_id, title

# ----------------------------------------------------------------------

def get_json_data(endpoint, params):
    url = API_BASE_URL + endpoint
    params['key'] = API_KEY
    if endpoint == 'playlistItems':
        # "UU..." -> "UC..."
        id = 'UC' + params['playlistId'][2:]
    else:
        id = endpoint
    try:
        data = session.get(url, params=params, timeout=config.REQUEST_TIMEOUT).json()
    except requests.exceptions.Timeout:
        raise ValueError(f'‼️ Ошибка соединения [{id}]: timeout')
    except requests.exceptions.ConnectionError as e:
        raise ValueError(f'‼️ Ошибка соединения [{id}]: {e}')

    if 'error' in data:
        raise ValueError(f'‼️ Ошибка [{id}]: {data["error"]["message"]}')
    if 'items' not in data:
        raise ValueError(f'‼️ Ошибка [{id}]: отсутствуют items. Ответ сервера: {data}')
    # if len(data['items']) == 0:
    #     raise ValueError(f'‼️ Ошибка [{id}]: youtube вернул пустой список')

    increment_units()
    return data

# ----------------------------------------------------------------------

def parse_duration(duration_str):
    # Функция для конвертации ISO 8601 (PT1H23M45S) в формат ЧЧ:ММ:СС или ММ:СС
    if not duration_str.startswith('PT'):
        return 0, duration_str
    pattern = re.compile(r'PT(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?')
    matchobj = pattern.match(duration_str)
    if not matchobj:
        return 0, duration_str
    hours, minutes, seconds = matchobj.groups()
    # Заменяем None на нули и переводим в числа
    hours = int(hours) if hours else 0
    minutes = int(minutes) if minutes else 0
    seconds = int(seconds) if seconds else 0

    secs = hours*60*60 + minutes*60 + seconds
    if hours > 0:
        return secs, f'{hours}:{minutes:02d}:{seconds:02d}'
    else:
        return secs, f'{minutes}:{seconds:02d}'

def format_duration(secs):
    # секунды в часы:минуты:секунды
    mins, secs = divmod(secs, 60)
    hrs, mins = divmod(mins, 60)
    if hrs:
        ret = f'{hrs}:{mins:02d}:{secs:02d}'
    elif mins:
        ret = f'{mins}:{secs:02d}'
    else:
        ret = str(secs)
    return ret

def format_dt(s):
    # ISO в читаемый формат
    dt = datetime.fromisoformat(s)
    dt = dt.astimezone() # используем локальное время
    dt_str = (dt.strftime('сегодня %H:%M') if dt.date() == date.today()
              else dt.strftime('%d.%m.%Y %H:%M'))
    return dt_str

def split_list(ids_list):
    # разбиваем ids_list на несколько списков по 50 элементов
    maxn = 50
    for ids in (ids_list[i : i + maxn] for i in range(0, len(ids_list), maxn)):
        yield ids

# ----------------------------------------------------------------------

def check_shorts():
    num_shorts = 0

    def video_is_shorts(video_id):
        nonlocal num_shorts
        url = f'https://www.youtube.com/shorts/{video_id}'
        try:
            response = session.head(url, allow_redirects=False,
                                    timeout=config.REQUEST_TIMEOUT)
            is_shorts = (response.status_code == 200)
            num_shorts += is_shorts
            return video_id, is_shorts

        except (requests.exceptions.Timeout,
                requests.exceptions.ConnectionError) as e:
            log_error(f'Error check_shorts: {e}')
            return video_id, None

    db_cursor.execute('SELECT id FROM videos '
                      'WHERE status IS NULL AND is_shorts IS NULL')
    all_rows = db_cursor.fetchall()
    log_debug(f'найдено {len(all_rows)} видео для проверки на шортс')
    with ThreadPoolExecutor(max_workers=config.MAX_CONCURRENT) as executor:
        # Отправляем все задачи в пул
        futures = [executor.submit(video_is_shorts, row['id']) for row in all_rows]
        # Получаем результаты по мере их готовности
        for fut in as_completed(futures):
            video_id, is_shorts = fut.result()
            db_cursor.execute('UPDATE videos SET is_shorts = ? WHERE id = ?',
                              (is_shorts, video_id))
    log_debug(f'{num_shorts} видео являются шортсами')

# ----------------------------------------------------------------------

# Счетчик юнитов
# У самого простого бесплатного аккаунта (Google Cloud проекта по
# умолчанию) действует квота в 10 000 единиц (units) в день.

def load_units():
    global units_counter
    units_counter = {}
    if not UNIT_COUNT_FILE.exists():
        return
    with UNIT_COUNT_FILE.open() as f:
        for line in f:
            date, count = line.split()
            units_counter[date] = int(count)

def save_units():
    with UNIT_COUNT_FILE.open('w') as f:
        for date, count in sorted(units_counter.items()):
            print(date, count, file=f)
    today = str(datetime.now(ZoneInfo("America/Los_Angeles")).date())
    if today in units_counter:
        log_debug(f'Сегодня ({today}) потрачено {units_counter[today]} '
                  f'юнитов из 10 000')

_counter_lock = threading.Lock()
def increment_units():
    with _counter_lock:
        # Юниты обнуляются в 00:00 по Лос-Анджелесу
        today = str(datetime.now(ZoneInfo("America/Los_Angeles")).date())
        if today in units_counter:
            units_counter[today] += 1
        else:
            units_counter[today] = 1

# ----------------------------------------------------------------------

def create_table(table, columns, primary_key=None):
    sql_columns = ', '.join(' '.join(c) for c in columns)
    if primary_key:
        sql_columns = f'{sql_columns}, PRIMARY KEY ({primary_key})'
    db_cursor.execute(f'CREATE TABLE IF NOT EXISTS {table} ({sql_columns})')
    # Добавляем колонку если ее еще нет в таблице
    db_cursor.execute(f"PRAGMA table_info({table})")
    exists_columns = [row[1] for row in db_cursor.fetchall()]
    for col in columns:
        if col[0] not in exists_columns:
            print(f"Колонка '{col}' не найдена. Добавляем")
            db_cursor.execute(f'ALTER TABLE {table} ADD COLUMN {" ".join(col)}')


def create_videos_table():
    global sql_insert

    columns = (
        ('id', 'TEXT NOT NULL PRIMARY KEY'), # id видео на youtube
        ('status', 'TEXT'), # NULL если видео не стрим или если стрим закончился
        ('is_shorts', 'INTEGER'),
        ('time', 'TEXT'), # время публикации видео или время начала стрима
        # snippet
        ('publishedAt', 'TEXT'), # время публикации видео или анонса стрима
        ('title', 'TEXT'),
        ('description', 'TEXT'),
        ('channelId', 'TEXT'),
        ('channelTitle', 'TEXT'),
        # contentDetails
        ('duration', 'INTEGER'),
        # liveStreamingDetails
        ('actualStartTime', 'TEXT'),
        ('actualEndTime', 'TEXT'),
        ('scheduledStartTime', 'TEXT'),
        # statistics
        ('viewCount', 'INTEGER'),
        ('likeCount', 'INTEGER'),
        ('commentCount', 'INTEGER'),
    )
    columns_names = [c[0] for c in columns]
    # строка с перечислением колонок
    sql_columns = ', '.join(columns_names)
    # то же, но с добавлением ":" перед колонкой
    sql_placeholders = ', '.join(':'+key for key in columns_names)
    # строка для добавления видео
    sql_insert = (f'INSERT OR REPLACE INTO videos ({sql_columns}) '
                  f'VALUES ({sql_placeholders})')
    create_table('videos', columns)

def create_channels_table():
    columns = (
        ('id', 'TEXT NOT NULL PRIMARY KEY'), # id канала или плейлиста на youtube
        # "snippet"
        ('title', 'TEXT'),
        ('customUrl', 'TEXT'), # "@googledevelopers",
        ('description', 'TEXT'), # описание на youtube
        ('publishedAt', 'TEXT'), # "2007-08-23T00:34:43Z",
        # "statistics"
        ('viewCount', 'INTEGER'), # "364554328",
        ('subscriberCount', 'INTEGER'), # "2680000",
        ('hiddenSubscriberCount', 'INTEGER'), # false,
        ('videoCount', 'INTEGER'), # "6120"
        #
        ('status', 'INTEGER'), # NULL - disabled, 1 - enabled
        ('is_playlist', 'INTEGER'), # 0 - канал, 1 - плейлист
        ('last_updates', 'TEXT'), # время последнего видео
        #('category', 'TEXT'), # наука, политика и т.д.
        #('comment', 'TEXT'), # свободный комментарий
        ('added', 'TEXT'), # время добавления канала в БД
        ('del_time', 'TEXT'), # время удаления
        ('del_comment', 'TEXT'), # причина удаления
    )
    create_table('channels', columns)


def create_videos_history_table():
    columns = (
        ('id', 'TEXT NOT NULL REFERENCES videos(id)'),
        ('recordDate', 'TEXT NOT NULL'),  # День фиксации метрик
        ('recordTime', 'TEXT'),  # Время фиксации метрик
        ('viewCount', 'INTEGER'),
        ('likeCount', 'INTEGER'),
        ('commentCount', 'INTEGER'),
    )
    create_table('videos_history', columns, 'id, recordDate')


def create_channels_history_table():
    columns = (
        ('id', 'TEXT NOT NULL REFERENCES channels(id)'),
        ('recordDate', 'TEXT NOT NULL'),  # День фиксации метрик
        ('recordTime', 'TEXT'),  # Время фиксации метрик
        ('viewCount', 'INTEGER'), # Текущее общее число просмотров по данным youtube
        ('sumViewCount', 'INTEGER'), # Сумма просмотров всех видео в базе данных
        ('videoCount', 'INTEGER'), # Количество видео по данным youtube
        ('sumVideoCount', 'INTEGER'), # Количество видео в базе данных
        ('subscriberCount', 'INTEGER'),
    )
    create_table('channels_history', columns, 'id, recordDate')

def create_history_table():
    columns = (
        ('id', 'TEXT NOT NULL REFERENCES channels(id)'),
        ('recordDate', 'TEXT NOT NULL'),
        ('recordTime', 'TEXT'),
        #('startDate', 'TEXT'), # за какой период. NULL если за все время
        #('endDate', 'TEXT'), # --//--
        ('period', 'TEXT'), # формат 'YYYY-MM-DD YYYY-MM-DD'. NULL если за все время
        ('total', 'INTEGER'),
        ('shorts', 'INTEGER'),
        ('views', 'INTEGER'),
        ('shortsViews', 'INTEGER'),
        ('likes', 'INTEGER'),
        ('shortsLikes', 'INTEGER'),
        ('comments', 'INTEGER'),
        ('shortsComments', 'INTEGER'),
    )
    create_table('history', columns, 'id, recordDate, period')


channels = []
processed_ids = set()
upcoming_ids = set()
def init_db():
    global db_connect, db_cursor

    log_debug(f'❕ db connect to {config.DB_FILE}')
    if not config.DB_FILE.exists():
        log_info(f'⚠️  File {config.DB_FILE} does not exists')

    db_connect = sqlite3.connect(config.DB_FILE)
    # Настраиваем фабрику строк для получения результатов как словарь
    db_connect.row_factory = sqlite3.Row

    db_cursor = db_connect.cursor()

    create_videos_table()
    create_channels_table()
    create_videos_history_table()
    create_channels_history_table()
    create_history_table()

    db_cursor.execute('SELECT * FROM channels WHERE status IS NOT NULL')
    for row in db_cursor.fetchall():
        channels.append(row['id'])
    log_debug(f'Количество активных каналов: {len(channels)}')

    db_cursor.execute('SELECT id, status FROM videos')
    for row in db_cursor.fetchall():
        if row['status'] is None:
            processed_ids.add(row['id'])
        else:
            upcoming_ids.add(row['id'])

def disconnect_db():
    # Сохранение изменений в файле БД
    db_connect.commit()
    # Закрытие соединения
    db_connect.close()

# ----------------------------------------------------------------------

def video_from_item(item):
    # Создает dict и устанавливает дефолтные значения для sqlite3
    snippet = item['snippet']
    video = snippet.copy()
    video['id'] = item['id']
    video['time'] = video['publishedAt']
    video['duration'] = None
    video['status'] = None
    video['is_shorts'] = None
    video['actualStartTime'] = None
    video['actualEndTime'] = None
    video['scheduledStartTime'] = None
    video['viewCount'] = None
    video['commentCount'] = None
    video['likeCount'] = None
    if 'liveStreamingDetails' in item:
        video.update(item['liveStreamingDetails'])
        if video['actualStartTime']:
            video['time'] = video['actualStartTime']
    #if 'statistics' in item:
    video.update(item['statistics'])
    return video

def add_video(item):
    # выводит инфо о видео и сохраняет в БД
    video_id = item['id']
    video = video_from_item(item)

    title = f'{video["channelTitle"]} - {video["title"]}'

    ###
    live_status = item['snippet']['liveBroadcastContent']
    if live_status == 'none':
        # видео или завершенные стримы
        video['status'] = None
        raw_duration = item['contentDetails']['duration']
        duration_sec, duration_formated = parse_duration(raw_duration) 
        video['duration'] = duration_sec
        if duration_sec == 0:
            log_error(f'❗ Странное видео: https://youtube.com/watch?v={video_id}')
            return None
        if duration_sec > 190:
            video['is_shorts'] = False
        if config.SHOW_SHORTS or duration_sec > 190:
            # Игнорируем короткие видео
            print(f'🎬 {title}')
            print(f'   🔗 https://youtube.com/watch?v={video_id}')
            print(f'   ⏱ Длительность: {duration_formated}')
            print(__)

    elif live_status in ('upcoming', 'live'):
        # Запланированные или прямая трансляция
        video['status'] = live_status
        if video['actualStartTime']:
            print(f'📺 {title}')
            print(f'   🔗 https://youtube.com/watch?v={video_id}')
            print(f'   🔴 Идет прямая трансляция')
            print(__)
        elif video_id not in upcoming_ids:
            if video['scheduledStartTime']:
                dt_str = format_dt(video['scheduledStartTime'])
                print(f'⏳ {title}')
                print(f'   🔗 https://youtube.com/watch?v={video_id}')
                print(f'   📢 Запланировано на {dt_str}')
            else:
                video['status'] = None
                print(f'❌ {title}')
                print(f'   🔗 https://youtube.com/watch?v={video_id}')
                print('   ❗ Забытый стрим')
            print(__)

    else:
        # Мы никогда не должны сюда попасть
        log_error(f'❗ {live_status}: https://youtube.com/watch?v={video_id}')
        return None

    # Сохраняем в базу данных
    db_cursor.execute(sql_insert, video)
    # Обновляем last_updates
    db_cursor.execute('''
    UPDATE channels SET last_updates = :time WHERE id = :channelId
    AND :time > COALESCE(last_updates, '') -- COALESCE для сравнения с NULL
    ''', video)

    return video


# Список видео полученные в playlistItems, но недоступные в videos
failed_videos = []
failed_lock = threading.Lock()
def get_new_videos(channel_id, new_video_ids=None, next_page_token=None):
    # Возвращает список id 50 последних видео на канале
    if new_video_ids is None:
        new_video_ids = []
    if channel_id.startswith('PL'):
        playlist_id = channel_id
    else:
        playlist_id = 'UU' + channel_id[2:]

    params = {
        'playlistId': playlist_id,
        'part': 'snippet',
        'maxResults': 50,
        }
    if next_page_token:
        params['pageToken'] = next_page_token

    data = get_json_data('playlistItems', params)

    min_date = str(date.today())
    for item in data['items']:
        snippet = item['snippet']
        video_id = snippet['resourceId']['videoId']
        published_at = snippet['publishedAt']
        if video_id in processed_ids:
            # остальные уже в БД
            return new_video_ids
        if (snippet['description'] == 'This video is private.'
            and snippet['title'] == 'Private video'):
            print(f'❗ Private video: {video_id}')
            #pprint(item)
            video = video_from_item(item)
            video['id'] = video_id
            processed_ids.add(video_id)
            with failed_lock:
                failed_videos.append(video)
            continue
        if published_at:
            min_date = min(min_date, published_at)
        new_video_ids.append(video_id)

    ###
    next_page_token = data.get('nextPageToken')
    if not next_page_token:
        return new_video_ids
    # видео за текущий и предыдущий месяцы
    last_day_of_prev_month = date.today().replace(day=1) - timedelta(days=1)
    first_day_of_prev_month = str(last_day_of_prev_month.replace(day=1))
    if not config.SCAN_ALL_VIDEOS and min_date < first_day_of_prev_month:
        return new_video_ids

    # рекурсивный вызов
    return get_new_videos(channel_id, new_video_ids, next_page_token)


def tracking():
    print(time.strftime('%d.%m.%Y %H:%M'))
    print(__)
    new_video_ids = []

    with ThreadPoolExecutor(max_workers=config.MAX_CONCURRENT) as executor:
        futures = [executor.submit(get_new_videos, cid) for cid in channels]
        for fut in as_completed(futures):
            try:
                new_video_ids += fut.result()
            except ValueError as e:
                log_error(e)

    log_debug(f'Найдено {len(new_video_ids)} видео для обновления (new+upcoming)')

    if failed_videos:
        log_debug(f'Обрабатываем {len(failed_videos)} проблемных видео')
        for video in failed_videos:
            #pprint(video)
            db_cursor.execute(sql_insert, video)

    if not new_video_ids:
        return

    process_videos(new_video_ids)

    if config.VERBOSE:
        db_cursor.execute('SELECT COUNT(status) FROM videos')
        count = db_cursor.fetchone()[0]
        if count:
            log_debug(f'upcoming: {count}')


def process_videos(video_ids, func=add_video):
    # получить инфо видео из списка video_ids и обработать функцией func
    with ThreadPoolExecutor(max_workers=config.MAX_CONCURRENT) as executor:
        futures = {}
        for ids in split_list(video_ids):
            params = {
                'id': ','.join(ids),
                'part': 'snippet,contentDetails,liveStreamingDetails,statistics',
                }
            futures[executor.submit(get_json_data, 'videos', params)] = ids

        for fut in as_completed(futures):
            ids = futures[fut]
            try:
                data = fut.result()
            except ValueError as e:
                log_error(e)
                continue

            for item in data['items']:
                # add_video() или update_video()
                video = func(item)
                if not video:
                    continue
                ids.remove(item['id'])

            if ids:
                log_debug(f'❗ ОБРАБОТАНЫ НЕ ВСЕ ВИДЕО\n{ids}')

    # обработка шортсов
    check_shorts()

# ----------------------------------------------------------------------

def update(all=False):
    update_videos(all)
    update_channels_info()

def update_videos(all=False):
    # Обновить просмотры, лайки и комментарии
    if all:
        db_cursor.execute("SELECT id FROM videos")
    else:
        # все видео за предыдущий и текущий месяцы
        db_cursor.execute("SELECT id FROM videos WHERE "
                          "time >= strftime('%Y-%m', 'now', '-1 month')")
    videos_ids = [row['id'] for row in db_cursor.fetchall()]

    len_videos_ids = len(videos_ids)
    log_info(f'UPDATE VIDEOS. num of videos: {len(videos_ids)}')
    processed_video_list = []
    def update_video(item):
        video = video_from_item(item)
        processed_video_list.append((video['viewCount'], video['likeCount'],
                                     video['commentCount'], video['id']))
        return True

    process_videos(videos_ids, update_video)

    db_cursor.executemany('''UPDATE videos SET
    viewCount = ?, likeCount = ?, commentCount = ? WHERE id = ?''',
                          processed_video_list)

    db_cursor.executemany("""
INSERT OR REPLACE INTO videos_history
(viewCount, likeCount, commentCount, id, recordDate, recordTime)
VALUES (?, ?, ?, ?, date('now'), time('now'))""", processed_video_list)

    log_info(f'updated {len(processed_video_list)} videos')

# ----------------------------------------------------------------------

def update_channels_info():
    log_info(f'UPDATE CHANNELS. num of channels: {len(channels)}')
    for ids in split_list(channels):
        params = {
            'id': ','.join(ids),
            'part': 'snippet,contentDetails,statistics',
        }
        try:
            data = get_json_data('channels', params)
        except ValueError as e:
            log_error(e)
            continue

        for item in data['items']:
            channel = item['snippet'].copy()
            channel['id'] = item['id']
            channel.update(item['statistics'])
            #pprint(channel)
            columns = ('title', 'description', 'customUrl', 'publishedAt', 'viewCount',
                       'subscriberCount', 'hiddenSubscriberCount', 'videoCount',)
            c_str = ', '.join(f'{c} = :{c}' for c in columns)
            db_cursor.execute(f'UPDATE channels SET {c_str} WHERE id = :id', channel)
            db_cursor.execute("""
INSERT OR REPLACE INTO channels_history
(id, viewCount, subscriberCount, videoCount, sumViewCount, sumVideoCount, recordDate, recordTime)
SELECT
:id, :viewCount, :subscriberCount, :videoCount, SUM(viewCount), COUNT(*), date('now'), time('now')
FROM videos WHERE channelId = :id
""", channel)

    log_info('done.')

# ----------------------------------------------------------------------

def update_history(period=None):
    sql_template = """
    INSERT OR REPLACE INTO history
    SELECT
        channelId AS id,
        date('now') AS recordDate,
        time('now') AS recordTime,
        ? AS period,
        COUNT(*) AS total,
        SUM(is_shorts) AS shorts,
        SUM(viewCount) AS views,
        SUM(CASE WHEN is_shorts = 1 THEN viewCount ELSE 0 END) AS shortsViews,
        SUM(likeCount) AS likes,
        SUM(CASE WHEN is_shorts = 1 THEN likeCount ELSE 0 END) AS shortsLikes,
        SUM(commentCount) AS comments,
        SUM(CASE WHEN is_shorts = 1 THEN commentCount ELSE 0 END) AS shortsComments
    FROM videos
    {} --WHERE date(time) BETWEEN ? AND ?
    GROUP BY channelId
"""
    if period:
        sql = sql_template.format('WHERE date(time) BETWEEN ? AND ?')
        db_cursor.execute(sql, (' '.join(period), *period))
    else:
        sql = sql_template.format('')
        db_cursor.execute(sql, (None,))



def ranking(method='sum', period=None, shorts_factor=None):
    if not period:
        # Текущий месяц
        period = (datetime.now().strftime("%Y-%m-01"),
                  datetime.now().strftime("%Y-%m-%d"))
    if shorts_factor is None:
        shorts_factor = 1
    sql = """
WITH RankedVideos AS (
    SELECT -- выборка и группировка
        channelId, channelTitle,
        {} AS result -- формула
    FROM videos
    WHERE {} -- дополнительное условие
        date(time) BETWEEN ? AND ?
    GROUP BY channelId
)
SELECT -- сортировка и нумерация
    ROW_NUMBER() OVER (ORDER BY result DESC) AS row_num,
    channelId, channelTitle, result
FROM RankedVideos
ORDER BY result"""

    if shorts_factor == 1:
        arg = 'viewCount'
        params = period
    else:
        arg = 'CASE WHEN is_shorts = 1 THEN viewCount * ? ELSE viewCount END'
        params = (shorts_factor,)+tuple(period)

    if method == 'avg':
        formula = f'SUM({arg}) / COUNT(*)'
        db_cursor.execute(sql.format(formula, ''), params)

    elif method == 'sum':
        formula = f'SUM({arg})'
        db_cursor.execute(sql.format(formula, ''), params)

    elif method in ('erv', 'erv+shorts'):
        # ERV (Engagement Rate by Views)
        # (лайки + комментарии) / просмотры
        formula = 'CAST(SUM(likeCount + commentCount) AS REAL) / SUM(viewCount) * 100'
        if method == 'erv':
            where = 'is_shorts = 0 AND viewCount > 0 AND' # исключаем шортсы и видео без просмотров
        else:
            where = 'viewCount > 0 AND'
        db_cursor.execute(sql.format(formula, where), period)

    if method in ('erv', 'erv+shorts'):
        # вычисляются проценты - оставляем две цифры после запятой
        f_str = '{:.02f}'
    else: # Округляем до единиц и добавляем пробелы
        f_str = '{:_.0f}'

    for row in db_cursor.fetchall():
        result = row['result'] if row['result'] else 0
        print(f'{row["row_num"]:<3} {row["channelTitle"]:<16}',
              f_str.format(result).replace('_', ' '))


def last_videos(values, type):
    # values - [число] или [число, id канала, ...]
    if not values[0].isdigit():
        log_critical(f'❌ Первое значение должно быть целым числом: {values[0]}')
        return
    nums = int(values[0])
    del values[0]
    if type == 'last_nums':
        sec_par = nums
    else:
        sec_par = f'-{nums} hours'

    if type == 'last_nums':
        sql_template = """
SELECT id, date, duration, title FROM (
    SELECT
        id, time, date(time) as date, time(duration, 'unixepoch') as duration,
        {} -- "title" для одного канала, "channelTitle - title" для всех
    FROM videos
    {} -- WHERE
    ORDER BY time DESC
    LIMIT ?
) ORDER BY time ASC
"""
    else:
        sql_template = """
SELECT
    id, datetime(time) as time, time(duration, 'unixepoch') as duration,
    {} -- "title" для одного канала, "channelTitle - title" для всех
FROM videos
WHERE {}  --
    time >= strftime('%Y-%m-%dT%H:%M:%SZ', 'now', ?)
ORDER BY time
"""
    if values:
        for cid in values:
            db_cursor.execute('SELECT title FROM channels WHERE id = ?', (cid,))
            row = db_cursor.fetchone()
            if not row:
                log_critical(f'❗️ Канал {cid} не найден')
                continue
            if type == 'last_nums':
                sql = sql_template.format('title', 'WHERE channelId = ?')
            else:
                sql = sql_template.format('title', 'channelId = ? AND')
            db_cursor.execute(sql, (cid, sec_par))
            print(f'Видео с канала "{row["title"]}"')
            print_table(db_cursor)
            print()
    else:
        sql = sql_template.format("channelTitle || ' - ' || title as title", '')
        db_cursor.execute(sql, (sec_par,))
        print_table(db_cursor)
        print()

# ----------------------------------------------------------------------

def loop(interval=60):
    if interval == 0:
        interval = 60
    try:
        while True:
            current_time = time.localtime()
            hour = current_time.tm_hour
            minute = current_time.tm_min
            second = current_time.tm_sec

            # Считаем, сколько секунд прошло от текущего часа
            seconds_passed = (minute * 60) + second

            if 0 <= hour <= 9:
                seconds_to_wait = 2*interval*60 - seconds_passed
                to_hour = hour+2
            else:
                # Считаем, сколько секунд осталось до следующего часа
                # (в часе 3600 секунд)
                seconds_to_wait = interval*60 - seconds_passed
                to_hour = hour+1
            log_info(f'Сейчас {hour:02d}:{minute:02d}. Спим {seconds_to_wait} сек. до {to_hour}:00.')
            # Спим ровно до начала следующего часа
            time.sleep(seconds_to_wait)
            # Как только проснулись — выполняем функцию
            print(__)
            tracking()

    except KeyboardInterrupt:
        print()

# ----------------------------------------------------------------------

def print_table(cursor, title=None):
    # Красиво выводит результаты SQL-запроса на экран в виде таблицы
    if cursor.description is None:
        # Если это DELETE, UPDATE или INSERT
        db_connect.commit()
        if cursor.rowcount == -1:
            print('Запрос успешно выполнен')
        else:
            print(f'Обработано {cursor.rowcount} строк')
        return
    # Получаем данные и заголовки
    rows = cursor.fetchall()
    if not rows:
        print('Запрос вернул пустой результат.')
        return
    # Заголовки берем из cursor.description (работает при любом row_factory)
    headers = [col[0] for col in cursor.description]
    # Переводим все элементы строк в текст для расчета ширины
    str_rows = [[str(item) for item in row] for row in rows]
    # Считаем максимальную ширину для каждой колонки (с учетом длины заголовка)
    col_widths = []
    for i, head in enumerate(headers):
        max_len = len(head)
        for row in str_rows:
            len_row = len(row[i])
            len_row = min(len_row, 32)
            max_len = max(max_len, len_row)
        col_widths.append(max_len)

    # Шаблон для форматирования строки (например, "{:<5} | {:<10} | {:<3}")
    #format_str = ' | '.join(f'{{:<{w}}}' for w in col_widths[:-1])
    format_str = ' | '.join(f'{{:<{w}.32}}' for w in col_widths[:-1])
    # Последнюю колонку выводим без добавления пробелов в конце строки
    format_str += ' | {}'
    # Рисуем линию-разделитель
    separator = '—+—'.join('—' * w for w in col_widths)
    line = '———'.join('—' * w for w in col_widths)
    # Выводим на экран
    if title:
        print(title)
        print(line)
    print(format_str.format(*headers))
    print(separator)
    for row in str_rows:
        print(format_str.format(*row))
    print(line)

# ----------------------------------------------------------------------

def parse_args():
    parser = argparse.ArgumentParser(description='Отслеживать появление новых видео')

    parser.add_argument('--db', metavar='NAME', default='default',
                        help=f'имя базы данных (файла без расширения '
                        f'в каталоге {YTTRANK_DIR}, по умолчанию `%(default)s`)')
    parser.add_argument('-c', '--channels', nargs='+', metavar='CHANNELID',
                        help='проверить только указанные каналы')
    parser.add_argument('-a', '--add', metavar='CHANNEL',
                        help='добавить канал (URL or ID)')
    parser.add_argument('-A', '--add-and-scan', metavar='CHANNEL',
                        help='добавить канал и 50 последних видео с него (URL or ID)')
    parser.add_argument('-d', '--del', nargs='+', dest='delete',
                        metavar=('CHANNELID', 'COMMENT'),
                        help='пометить канал disabled '
                        '(можно добавить причину удаления)')
    parser.add_argument('--delete-from-db',
                        help='удалить канал и все его видео из базы данных')
    parser.add_argument('-i', '--channel-info', metavar='CHANNEL',
                        help='показать инфо канала (ID or Title)')
    parser.add_argument('-L', '--channel-videos', metavar='CHANNEL',
                        help='показать список видео канала (ID or Title)')
    parser.add_argument('-r', '--delete-video', metavar='VIDEOID',
                        help='удалить видео из БД')
    parser.add_argument('-v', '--video', metavar='VIDEO',
                        help='показать инфо видео (URL or ID)')
    parser.add_argument('--no-null', action='store_true',
                        help='не показывать в инфо о канале или видео '
                        'колонки со значением NULL')
    parser.add_argument('--no-description', action='store_true',
                        help='не показывать description в инфо о видео ')
    parser.add_argument('-u', '--upcoming', action='store_true',
                        help='показать запланированные стримы')
    parser.add_argument('-N', '--last-nums', nargs='+',
                        metavar=('NUMS', 'CHANNELID'),
                        help='показать последние NUMS видео; можно указать ID каналов')
    parser.add_argument('-H', '--last-hours', nargs='+',
                        metavar=('HOURS', 'CHANNELID'),
                        help='показать видео за последние HOURS часов; '
                        'можно указать ID каналов')
    parser.add_argument('-l', '--loop', type=int, nargs='?', metavar='MINUTES',
                        const=0,      # Значение, если передали просто --loop
                        default=None, # Значение, если вообще НЕ передали флаг --loop
                        help='Запустить в бесконечном цикле. '
                        'Можно указать интервал в минутах')
    parser.add_argument('--update', action='store_true',
                        help='обновить статистику видео за текущий и предыдущий '
                        'месяцы и информацию и статистику всех каналов')
    parser.add_argument('--update-videos', action='store_true',
                        help='обновить статистику видео за текущий и предыдущий '
                        'месяцы')
    parser.add_argument('--update-all', action='store_true',
                        help='обновить статистику ВСЕХ видео в БД')
    parser.add_argument('--update-channels-info', action='store_true',
                        help='обновить информацию и статистику всех каналов')
    parser.add_argument('--update-history', action='store_true',
                        help='обновить summary')
    parser.add_argument('--ranking', nargs='?', choices=['avg', 'sum',
                                                        'erv', 'erv+shorts'],
                        const='sum', default=None, help='рейтинг (дефолт `sum`). '
                        '`sum` = сумма просмотров; `avg` = среднее арифметическое '
                        'просмотров; `erv` = коэффициент вовлеченности в процентах '
                        '(без шортсов); `erv+shorts` = то же с шортсами')
    parser.add_argument('--period', nargs=2, metavar=('BEGIN', 'END'),
                        help='Промежуток времени вычисления рейтинга (например: '
                        '`--period 2026-01-01 2026-01-31` = рейтинг за январь 2026)')
    parser.add_argument('--shorts-factor', type=float, metavar='FACTOR',
                        help='коэффициент для шортсов при вычислении '
                        'рейтингов `sum` и `avg`. `--shorts-factor 0.1` = '
                        'количество просмотров шортсов умножать на 0.1; '
                        '`--shorts-factor 0` = не учитывать шортсы. '
                        'По умолчанию 1')
    parser.add_argument('--sql', help='выполнить SQL запрос. '
                        '`--sql help` покажет самые распространенные случаи')
    parser.add_argument('--verbose', action='store_true')

    options = parser.parse_args()
    return options

# ----------------------------------------------------------------------

def main():
    global config, session, channels

    load_units() # ! в самом начале чтобы в случае чего не потерять информацию
    session = requests.Session()
    options = parse_args()
    config = Config(options.db)
    if options.verbose:
        config.VERBOSE = True
    load_api_key()
    init_db()

    if options.sql:
        if options.sql == 'help':
            print('''\
  pragma table_info('videos') -- структура таблицы видео
  select id, added, title from channels -- список каналов
  select count(*) from videos where time between '2026-09-01' and '2026-09-31' -- количество видео за сентябрь 2026
  select channelId, channelTitle, count(*) as total from videos group by channelId order by total -- показать количество видео для каждого канала
  select id, date(last_updates), title from channels where last_updates < date('now', '-6 month') -- найти каналы у которых последнее обновление было более 6 месяцев назад
  select id, date(time) as date, viewCount, channelTitle, title from videos where date(time) between '2026-01-01' and '2026-01-31' order by viewCount desc limit 10 -- десять видео в январе 2026 с самым большим количеством просмотров'''
  #select id from videos where not exists (select 1 from channels where channels.id = videos.channelId) -- поиск сирот - видео у которых указан несуществующий channel ID
'''
  select id, date(time) as date, time(duration, 'unixepoch') as duration, channelTitle, title from videos order by duration desc limit 10 -- 10 самых длинных видео
  select replace(format('%,d', viewCount), ',', ' ') as [view count], title from channels order by viewCount -- отсортировать каналы по общему количеству просмотров
  select channels.id, videos.ChannelTitle as [old title], channels.title as [new title] from channels left join videos on channels.id = videos.channelId where channels.title != videos.channelTitle group by channels.id, videos.channelTitle -- посмотреть старые имена каналов
''')
        else:
            db_cursor.execute(options.sql)
            if 1:
                print_table(db_cursor)
            else:
                # Выводим заголовки
                columns = [col[0] for col in db_cursor.description]
                print(' | '.join(columns))
                print(__)
                for row in db_cursor:
                    #print(' | '.join(str(s) for s in row))
                    print(*row, sep=' | ')
        return

    if options.ranking:
        ranking(options.ranking, options.period, options.shorts_factor)
        return

    if options.update:
        update()
        return

    if options.update_videos:
        update_videos()
        return

    if options.update_all:
        update_videos(True)
        return

    if options.update_channels_info:
        update_channels_info()
        return

    if options.update_history:
        update_history(options.period)
        return

    if options.add:
        add_channel(options.add)
        return

    if options.add_and_scan:
        cid = add_channel(options.add_and_scan)
        if cid:
            channels = [cid]
            tracking()
            update_channels_info()
        return

    if options.delete:
        ch = options.delete[0]
        comm = None
        if len(options.delete) > 1:
            comm = ' '.join(options.delete[1:])
        if ch not in channels:
            print('Канал не найден или уже disabled')
            return
        db_cursor.execute("UPDATE channels SET "
                          "status = NULL, del_time = datetime('now'), del_comment = ? "
                          "WHERE id = ?", (comm, ch))
        print(f'Канал {ch} помечен disabled.')
        return

    if options.delete_from_db:
        cid = options.delete_from_db
        db_cursor.execute('DELETE FROM channels WHERE id = ?', (cid,))
        print(f'Удалено каналов: {db_cursor.rowcount}')
        db_cursor.execute('DELETE FROM videos WHERE channelId = ?', (cid,))
        print(f'Удалено видео: {db_cursor.rowcount}')
        return

    if options.delete_video:
        vid = options.delete_video
        db_cursor.execute('DELETE FROM videos WHERE id = ?', (vid,))
        print(f'Удалено видео: {db_cursor.rowcount}')
        return

    if options.video:
        vid = options.video
        if vid.startswith('https://'):
            vid = get_youtube_id(vid)
            if not vid:
                print(f'Непонятная ссылка: {options.video}')
                return
        db_cursor.execute('SELECT * FROM videos WHERE id = ?', (vid,))
        row = db_cursor.fetchone()
        if not row:
            print(f'Видео {vid} не найдено')
            return
        print(row['channelTitle'], '-', row['title'])
        print(__)
        if not options.no_description:
            print(row['description'])
            print(__)
        for key in row.keys():
            if key not in ('title', 'description'):
                value = row[key]
                if options.no_null and value is None:
                    continue
                if value and key == 'duration':
                    value = format_duration(value)
                print(f'{key+":":<16} {value}')
        return

    if options.channel_info:
        cid = options.channel_info
        if cid.startswith(('UC', 'PL')):
            db_cursor.execute('SELECT * FROM channels WHERE id = ?', (cid,))
        elif cid.startswith('@'):
            db_cursor.execute('SELECT * FROM channels WHERE customUrl = ?', (cid,))
        else:
            db_cursor.execute('SELECT * FROM channels WHERE title = ?', (cid,))
        row = db_cursor.fetchone()
        if row:
            for key in row.keys():
                value = row[key]
                if options.no_null and value is None:
                    continue
                if options.no_description and key == 'description':
                    continue
                print(f'{key+":":<14} {value}')
            db_cursor.execute("""
SELECT
    COUNT(*) AS [Всего видео:],
    COUNT(CASE WHEN strftime('%Y-%m', time) = strftime('%Y-%m', 'now')
        THEN 1 END) AS [Видео в этом месяце:],
    SUM(viewCount) AS [Всего просмотров:],
    SUM(CASE WHEN strftime('%Y-%m', time) = strftime('%Y-%m', 'now')
        THEN viewCount END) AS [Просмотров в этом месяце:]
FROM videos WHERE channelId = ?
""", (row['id'],))
            row = db_cursor.fetchone()
            for key in row.keys():
                print(f'{key:<14}', f'{row[key] or 0:_}'.replace('_', ' '))
        else:
            print(f'Канал {cid} не найден')
        return

    if options.channel_videos:
        cid = options.channel_videos
        if cid.startswith(('UC', 'PL')):
            db_cursor.execute('SELECT *, datetime(time) FROM videos '
                              'WHERE channelId = ? ORDER BY time', (cid,))
        else:
            db_cursor.execute('SELECT *, datetime(time) FROM videos '
                              'WHERE channelTitle = ? ORDER BY time', (cid,))
        for row in db_cursor.fetchall():
            print(row['id'], ':', row['datetime(time)'], ':', row['title'])
        return

    if options.upcoming:
        db_cursor.execute('SELECT * FROM videos WHERE status IS NOT NULL')
        for row in db_cursor.fetchall():
            start_time = row['scheduledStartTime']
            if start_time:
                start_time = format_dt(start_time)
            print(row['id'], ':', start_time, ':',
                  row['channelTitle'], ':', row['title'])
        return

    if options.last_nums:
        last_videos(options.last_nums, 'last_nums')
        return

    if options.last_hours:
        last_videos(options.last_hours, 'last_hours')
        return

    if options.channels:
        # FIXME: проверить
        channels = options.channels

    tracking()
    if options.loop is not None:
        loop(options.loop)


if __name__ == '__main__':
    try:
        main()
    finally:
        if 'db_connect' in globals(): # если был выполнен init_db()
            save_units()
            disconnect_db()

# end
