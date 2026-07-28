"""
Общая логика Reisync: клиент Spotify, чтение позиции плеера, загрузка и
парсинг субтитров с lrclib.net, окно субтитров (DisplayConsole) и звуковая
петля для эквалайзера. Используется и живым режимом (main.py), и пакетной
записью (record_session.py), поэтому вынесено сюда, а не продублировано.
"""
import bisect
import ctypes
from ctypes import wintypes
import json
import os
import re
import subprocess
import sys
import threading
import time

import numpy as np
import pyaudiowpatch as pyaudio
import requests
import spotipy
from spotipy.oauth2 import SpotifyOAuth

RETRY_DELAY = 10
POLL_INTERVAL = 4      # как часто сверяем позицию с Spotify во время игры трека
LYRIC_LEAD = 0.25       # строки показываем чуть раньше: компенсирует задержку
                        # вывода и анимацию проявления
# Тема окна субтитров, поменяй и перезапусти:
#   nerv  — оранжевый интерфейс NERV, арт/эквалайзер — синева Рей (текущая)
#   eva01 — фиолетовый с зелёными акцентами (Ева-01, Синдзи)
#   eva02 — красный, арт янтарный (Ева-02, Аска)
#   eva08 — розово-фиолетовый (Ева-08, Мари)
#   mass  — белый (серийные Евы, Dummy Plug)
THEME = 'nerv'
# Поменять местами цвета интерфейса и акцент арта выбранной темы: шапка,
# название и прогресс красятся цветом арта, а арт и текст песни — цветом
# интерфейса (True/False), поменяй и перезапусти:
SWAP_ACCENT = False
EQ_BANDS = 24
EQ_CHUNK = 2048
HISTORY_FILE = "history.log"
SPOTIFY_CONFIG_FILE = "spotify_config.json"
SPOTIFY_TOKEN_CACHE = ".spotify_token_cache"
SPOTIFY_SCOPE = "user-read-playback-state"
LYRICS_CONSOLE_SCRIPT = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'lyrics_console.py')


def get_spotify_client():
    client_id = os.environ.get('SPOTIPY_CLIENT_ID')
    client_secret = os.environ.get('SPOTIPY_CLIENT_SECRET')
    redirect_uri = os.environ.get('SPOTIPY_REDIRECT_URI')

    if not client_id or not client_secret:
        try:
            with open(SPOTIFY_CONFIG_FILE, encoding='utf-8') as f:
                cfg = json.load(f)
            client_id = client_id or cfg.get('client_id')
            client_secret = client_secret or cfg.get('client_secret')
            redirect_uri = redirect_uri or cfg.get('redirect_uri')
        except (FileNotFoundError, json.JSONDecodeError):
            pass

    if not client_id or not client_secret:
        raise SystemExit(
            "Нужны данные Spotify-приложения.\n"
            "1. Создайте приложение на https://developer.spotify.com/dashboard\n"
            "2. В настройках приложения добавьте Redirect URI: http://127.0.0.1:8888/callback\n"
            f"3. Впишите client_id и client_secret в {SPOTIFY_CONFIG_FILE}\n"
            "   (или задайте переменные окружения SPOTIPY_CLIENT_ID / SPOTIPY_CLIENT_SECRET)."
        )

    auth = SpotifyOAuth(
        client_id=client_id,
        client_secret=client_secret,
        redirect_uri=redirect_uri or 'http://127.0.0.1:8888/callback',
        scope=SPOTIFY_SCOPE,
        cache_path=SPOTIFY_TOKEN_CACHE,
    )
    return spotipy.Spotify(auth_manager=auth)


def get_playback(sp):
    """
    Текущее состояние плеера Spotify: что играет, точная позиция (progress_ms)
    и момент времени 'at', в который эта позиция была получена. Позиция без
    привязки к моменту измерения бессмысленна — вся синхронизация дальше
    строится на паре (позиция, at).
    """
    before = time.monotonic()
    try:
        pb = sp.current_playback()
    except Exception as exc:
        print(f"Spotify API недоступен: {exc}")
        return None
    # progress_ms соответствует состоянию сервера где-то в середине
    # запроса — берём середину, а не момент получения ответа, иначе вся
    # синхронизация сдвигается на половину сетевой задержки
    at = (before + time.monotonic()) / 2

    if not pb or not pb.get('item'):
        return None

    item = pb['item']
    return {
        'track_id': item['id'],
        'artist': item['artists'][0]['name'],
        'title': item['name'],
        'duration_sec': item['duration_ms'] / 1000,
        'position_sec': (pb.get('progress_ms') or 0) / 1000,
        'is_playing': bool(pb.get('is_playing')),
        'at': at,
    }


def get_loopback_device(p):
    wasapi_info = p.get_host_api_info_by_type(pyaudio.paWASAPI)
    default_speakers = p.get_device_info_by_index(wasapi_info["defaultOutputDevice"])

    if not default_speakers["isLoopbackDevice"]:
        for loopback in p.get_loopback_device_info_generator():
            if default_speakers["name"] in loopback["name"]:
                default_speakers = loopback
                break
        else:
            raise RuntimeError("Loopback-устройство не найдено")

    return default_speakers


def fetch_lyrics(artist, title, duration_sec):
    headers = {
        'User-Agent': 'LyricSync/1.0 (https://github.com/yourname/lyricsync)'
    }
    params = {
        'artist_name': artist,
        'track_name': title,
        'duration': int(duration_sec),
    }

    try:
        response = requests.get('https://lrclib.net/api/get', params=params,
                                headers=headers, timeout=10)
    except requests.RequestException as exc:
        print(f"lrclib.net недоступен: {exc}")
        return None

    if response.status_code != 200:
        return None

    return response.json()


def parse_lrc(lrc_text):
    pattern = r'\[(\d+):(\d+\.\d+)\](.*)'
    lines = []

    for raw_line in lrc_text.split('\n'):
        match = re.match(pattern, raw_line)
        if match:
            minutes, seconds, text = match.groups()
            timestamp = int(minutes) * 60 + float(seconds)
            lines.append((timestamp, text.strip()))

    return lines


def interpolate_plain_lyrics(plain_text, duration_sec):
    """
    lrclib иногда отдаёт только обычный текст без таймкодов. Раскладываем строки
    равномерно по длительности трека — тайминг получится приблизительным, но
    лучше, чем совсем ничего.
    """
    lines = [line.strip() for line in plain_text.split('\n') if line.strip()]
    if not lines:
        return []

    step = duration_sec / len(lines)
    return [(i * step, line) for i, line in enumerate(lines)]


class PositionAnchor:
    """
    Тройка (позиция_в_треке, time.monotonic(), играет_ли) с потокобезопасным
    обновлением. Поток субтитров читает её постоянно, а основной поток
    поправляет после каждого опроса Spotify — без лока эти два потока могли бы
    увидеть рассогласованные значения (например, новую позицию со старым
    временем), что и есть ошибка синхронизации.
    """
    def __init__(self, position, timestamp, playing=True):
        self._lock = threading.Lock()
        self._position = position
        self._timestamp = timestamp
        self._playing = playing

    def update(self, position, timestamp, playing=True):
        with self._lock:
            self._position = position
            self._timestamp = timestamp
            self._playing = playing

    def estimate_now(self):
        with self._lock:
            if not self._playing:
                return self._position  # на паузе позиция не движется
            return self._position + (time.monotonic() - self._timestamp)


def lyrics_player(lyrics_lines, anchor, stop_event, display):
    """
    Показывает строки (timestamp, text) в момент, когда позиция трека доходит
    до timestamp. Ждёт короткими интервалами и каждый раз пересчитывает
    позицию из anchor — поэтому пауза, перемотка и поправки от опроса Spotify
    подхватываются на лету, без перезапуска потока.
    """
    timestamps = [t for t, _ in lyrics_lines]

    def next_text_after(i):
        for t in lyrics_lines[i + 1:]:
            if t[1]:
                return t[1]
        return ''

    position = anchor.estimate_now() + LYRIC_LEAD
    idx = bisect.bisect_left(timestamps, position)

    while not stop_event.is_set() and idx < len(lyrics_lines):
        position = anchor.estimate_now() + LYRIC_LEAD

        # перемотка назад — возвращаемся к нужной строке
        if idx > 0 and position < timestamps[idx - 1] - 2:
            idx = bisect.bisect_left(timestamps, position)
            continue

        # перемотка вперёд — пропускаем прошедшие строки молча,
        # а не выплёвываем их все разом
        if position - timestamps[idx] > 3:
            idx = bisect.bisect_left(timestamps, position)
            continue

        wait = timestamps[idx] - position
        if wait > 0:
            stop_event.wait(min(wait, 0.5))
            if wait > 0.5:
                continue  # позиция могла измениться — пересчитываем
            if stop_event.is_set():
                return

        text = lyrics_lines[idx][1]
        if text:
            print(f">> {text}")
            display.lyric(text, next_text_after(idx))
        idx += 1


class DisplayConsole:
    """
    Управляет отдельным консольным окном (lyrics_console.py), закреплённым
    поверх остальных окон, и стилизованно показывает в нём субтитры/эквалайзер
    (ASCII-арт для заголовка, "печатная машинка" для строк текста) — вместо
    обычного print() в основной консоли с логами. Общается с дочерним
    процессом через stdin, поэтому запись защищена локом на случай, если два
    потока попробуют писать одновременно.

    monitor_rect — необязательный (left, top, right, bottom) виртуального
    экрана: если задан, окно разворачивается без рамки на весь этот монитор
    (используется пакетной записью, main.py его не передаёт).
    """
    def __init__(self, monitor_rect=None):
        self._lock = threading.Lock()
        self._proc = None
        args = [sys.executable, LYRICS_CONSOLE_SCRIPT, '--theme', THEME]
        if SWAP_ACCENT:
            args.append('--swap-accent')
        if monitor_rect:
            l, t, r, b = monitor_rect
            args += ['--monitor-rect', f'{l},{t},{r},{b}']
            # conhost.exe спереди обязателен именно для полноэкранного режима:
            # если у пользователя терминалом по умолчанию стоит Windows
            # Terminal, CREATE_NEW_CONSOLE без conhost.exe открывает окно
            # через него, и GetConsoleWindow() внутри дочернего процесса
            # возвращает скрытое прокси-окно, а не реальное видимое —
            # позиционирование на монитор тогда молча не действует. В обычном
            # (не полноэкранном) режиме эта надстройка не нужна и не
            # добавляется — она не бесплатна: у части систем запуск через
            # неё приводил к падению python.exe с 0xC0000142 при старте.
            args = ['conhost.exe'] + args
        try:
            self._proc = subprocess.Popen(
                args,
                stdin=subprocess.PIPE,
                creationflags=subprocess.CREATE_NEW_CONSOLE,
                text=True,
                encoding='utf-8',
                bufsize=1,
            )
        except OSError as exc:
            print(f"Не удалось открыть окно субтитров: {exc}")

    def _send(self, message):
        if self._proc is None:
            return
        with self._lock:
            try:
                self._proc.stdin.write(message + '\n')
                self._proc.stdin.flush()
            except (BrokenPipeError, ValueError, OSError):
                pass  # окно закрыли вручную — просто перестаём в него писать

    def title(self, artist, title):
        self._send(f"TITLE:{artist}|{title}")

    def lyric(self, text, next_text=''):
        self._send(f"LYRIC:{text}\t{next_text}")

    def eq(self, level, bands):
        bands_str = ','.join(f'{b:.3f}' for b in bands)
        self._send(f"EQ:{level:.3f};{bands_str}")

    def pos(self, position, duration, playing):
        self._send(f"POS:{position:.1f};{duration:.1f};{1 if playing else 0}")

    def close(self):
        self._send("EXIT")


def level_monitor(display, sink=None):
    """
    Фоновый поток: постоянно слушает loopback-устройство, раскладывает звук
    по частотным полосам через FFT и шлёт их в окно субтитров. Работает всегда,
    независимо от того, есть у трека текст или нет: с текстом окно рисует
    тонкий спектр внизу, без текста — большой спектр-анализатор.

    Нормировка адаптивная (бегущий максимум по каждой полосе): абсолютный
    масштаб FFT зависит от громкости системы и материала, и фиксированный
    порог давал бы то пустой, то вечно зашкаливающий эквалайзер.

    sink — необязательный объект с .write(raw_bytes): используется пакетной
    записью, чтобы тот же самый открытый loopback-стрим одновременно кормил
    и эквалайзер, и звуковую дорожку ffmpeg (не открываем звуковое устройство
    второй раз ради записи).
    """
    smoothed = np.zeros(EQ_BANDS)
    band_peaks = np.full(EQ_BANDS, 1e-6)
    level_peak = 1e-6

    while True:
        try:
            with pyaudio.PyAudio() as p:
                device = get_loopback_device(p)
                channels = device["maxInputChannels"]
                samplerate = int(device["defaultSampleRate"])
                if sink is not None:
                    sink.set_format(samplerate, channels)
                window = np.hanning(EQ_CHUNK)
                freqs = np.fft.rfftfreq(EQ_CHUNK, 1 / samplerate)
                edges = np.logspace(np.log10(50), np.log10(min(16000, samplerate / 2)),
                                    EQ_BANDS + 1)
                band_masks = [(freqs >= edges[i]) & (freqs < edges[i + 1])
                              for i in range(EQ_BANDS)]

                stream = p.open(format=pyaudio.paInt16,
                                channels=channels,
                                rate=samplerate,
                                frames_per_buffer=EQ_CHUNK,
                                input=True,
                                input_device_index=device["index"])
                try:
                    while True:
                        data = stream.read(EQ_CHUNK, exception_on_overflow=False)
                        if sink is not None:
                            sink.write(data)
                        samples = np.frombuffer(data, dtype=np.int16)
                        if channels > 1:
                            samples = samples.reshape(-1, channels).mean(axis=1)
                        samples = samples / 32768.0

                        rms = float(np.sqrt(np.mean(samples ** 2)))
                        level_peak = max(level_peak * 0.999, rms, 1e-6)
                        level = min(rms / level_peak, 1.0)

                        spectrum = np.abs(np.fft.rfft(samples[:EQ_CHUNK] * window))
                        raw = np.array([spectrum[m].mean() if m.any() else 0.0
                                        for m in band_masks])
                        band_peaks = np.maximum(band_peaks * 0.999, raw)
                        bands = np.minimum(raw / band_peaks, 1.0)

                        # атака мгновенная, спад плавный — так полосы "дышат"
                        smoothed = np.maximum(bands, smoothed * 0.8)
                        display.eq(level, smoothed)
                finally:
                    stream.close()
        except Exception as exc:
            print(f"Эквалайзер недоступен ({exc}), повтор через 5 сек")
            time.sleep(5)


def log_history(artist, title):
    with open(HISTORY_FILE, 'a', encoding='utf-8') as f:
        f.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')}\t{artist} - {title}\n")


# ---------------------------------------------------------------- мониторы

class _RECT(ctypes.Structure):
    _fields_ = [('left', ctypes.c_long), ('top', ctypes.c_long),
                ('right', ctypes.c_long), ('bottom', ctypes.c_long)]


class _MONITORINFO(ctypes.Structure):
    _fields_ = [('cbSize', wintypes.DWORD), ('rcMonitor', _RECT),
                ('rcWork', _RECT), ('dwFlags', wintypes.DWORD)]


_MONITORINFOF_PRIMARY = 0x1
_user32 = ctypes.windll.user32

# тот же класс бага, что и в lyrics_console.py: без явных argtypes/restype
# ctypes подставляет размеры параметров "на глаз", и LPARAM здесь ошибочно
# стоял как c_double (не как указательного размера целое) — на одних сборках
# Python это проходило незаметно, на других вызывало настоящий access
# violation в нативном колбэке EnumDisplayMonitors
_user32.GetMonitorInfoW.argtypes = [wintypes.HMONITOR, ctypes.POINTER(_MONITORINFO)]
_user32.GetMonitorInfoW.restype = wintypes.BOOL
_user32.EnumDisplayMonitors.argtypes = [wintypes.HDC, ctypes.POINTER(_RECT),
                                        ctypes.c_void_p, wintypes.LPARAM]
_user32.EnumDisplayMonitors.restype = wintypes.BOOL
_MonitorEnumProc = ctypes.WINFUNCTYPE(
    wintypes.BOOL, wintypes.HMONITOR, wintypes.HDC,
    ctypes.POINTER(_RECT), wintypes.LPARAM
)


def list_monitors():
    """Список мониторов: [{'rect': (l, t, r, b), 'is_primary': bool}, ...],
    координаты — виртуального экрана (могут быть отрицательными)."""
    monitors = []

    def callback(hmonitor, hdc, rect_ptr, data):
        info = _MONITORINFO()
        info.cbSize = ctypes.sizeof(_MONITORINFO)
        _user32.GetMonitorInfoW(hmonitor, ctypes.byref(info))
        rect = (info.rcMonitor.left, info.rcMonitor.top,
                info.rcMonitor.right, info.rcMonitor.bottom)
        monitors.append({
            'rect': rect,
            'is_primary': bool(info.dwFlags & _MONITORINFOF_PRIMARY),
        })
        return 1

    _user32.EnumDisplayMonitors(None, None, _MonitorEnumProc(callback), 0)
    return monitors


def pick_monitor(index=None):
    """
    Возвращает (index, rect) монитора для полноэкранного окна субтитров и
    записи. Без явного index берёт первый не-основной монитор с разрешением
    1920x1080 (условие пользователя: "второй монитор Full HD").
    """
    monitors = list_monitors()
    if not monitors:
        raise RuntimeError("Не удалось получить список мониторов")

    if index is not None:
        if index < 0 or index >= len(monitors):
            raise RuntimeError(
                f"Монитор #{index} не найден, всего мониторов: {len(monitors)}"
            )
        return index, monitors[index]['rect']

    for i, m in enumerate(monitors):
        if m['is_primary']:
            continue
        l, t, r, b = m['rect']
        if (r - l, b - t) == (1920, 1080):
            return i, m['rect']

    raise RuntimeError(
        "Не нашёл второй монитор 1920x1080 автоматически. "
        f"Обнаруженные мониторы: {monitors}. Укажите номер явно флагом --monitor N."
    )
