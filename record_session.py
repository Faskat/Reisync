"""
Запись лирик-видео вручную: ты сам открываешь окно субтитров, ставишь его на
нужный монитор/в полноэкранный режим и сам включаешь треки в Spotify — скрипт
только следит за Spotify (как main.py) и, как только видит новый трек, пишет
экран выбранного монитора + системный звук в отдельный mp4, пока трек не
сменится или не остановится.

Запуск:
    python record_session.py [--monitor N] [--outdir output]

Никакого плейлиста заранее готовить не нужно — имя файла берётся из данных
трека, которые Spotify отдаёт в момент воспроизведения.

Требования: ffmpeg в PATH, открытый Spotify.
"""
import argparse
import os
import re
import shutil
import subprocess
import threading
import time

from reisync_core import (
    DisplayConsole,
    POLL_INTERVAL,
    PositionAnchor,
    RETRY_DELAY,
    fetch_lyrics,
    get_playback,
    get_spotify_client,
    interpolate_plain_lyrics,
    level_monitor,
    log_history,
    lyrics_player,
    parse_lrc,
    pick_monitor,
)

_INVALID_FILENAME_CHARS = re.compile(r'[<>:"/\\|?*]')


class AudioSink:
    """
    Потокобезопасный "текущий приёмник" сырых PCM-байт с WASAPI-петли.
    level_monitor слушает звуковое устройство один раз на всю сессию
    (не открываем/закрываем его между треками) и на каждый чанк вызывает
    write() сюда; attach/detach переключают, куда эти байты идут прямо
    сейчас — в stdin ffmpeg текущего трека или никуда (между треками).
    """
    def __init__(self):
        self._lock = threading.Lock()
        self._target = None
        self.format = None

    def set_format(self, samplerate, channels):
        self.format = (samplerate, channels)

    def attach(self, target):
        with self._lock:
            self._target = target

    def detach(self):
        with self._lock:
            self._target = None

    def write(self, data):
        with self._lock:
            target = self._target
        if target is None:
            return
        try:
            target.write(data)
        except (BrokenPipeError, OSError, ValueError):
            self.detach()


def sanitize_filename(name):
    return _INVALID_FILENAME_CHARS.sub('_', name).strip()


def wait_for_audio_format(audio_sink, timeout=10):
    deadline = time.monotonic() + timeout
    while audio_sink.format is None and time.monotonic() < deadline:
        time.sleep(0.1)
    if audio_sink.format is None:
        raise SystemExit("Не удалось определить параметры звукового устройства "
                          "(эквалайзер/аудио-петля не запустились).")


def start_ffmpeg(monitor_rect, out_path, samplerate, channels):
    left, top, right, bottom = monitor_rect
    width, height = right - left, bottom - top
    cmd = [
        'ffmpeg', '-y',
        '-f', 'gdigrab', '-framerate', '30',
        '-offset_x', str(left), '-offset_y', str(top),
        '-video_size', f'{width}x{height}', '-i', 'desktop',
        '-thread_queue_size', '1024',
        '-f', 's16le', '-ar', str(samplerate), '-ac', str(channels), '-i', 'pipe:0',
        '-c:v', 'libx264', '-preset', 'veryfast', '-pix_fmt', 'yuv420p',
        # -shortest: как только звуковой вход закончится (мы закрываем его
        # сами, когда трек сменился), ffmpeg сразу и аккуратно завершает
        # файл — не нужно ни знать длительность трека заранее, ни грубо
        # убивать процесс (grubое убийство портит mp4 — не пишется трейлер)
        '-c:a', 'aac', '-shortest',
        out_path,
    ]
    return subprocess.Popen(cmd, stdin=subprocess.PIPE,
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def stop_ffmpeg(proc, audio_sink):
    audio_sink.detach()
    try:
        proc.stdin.close()
    except OSError:
        pass
    try:
        proc.wait(timeout=15)
    except subprocess.TimeoutExpired:
        proc.terminate()
        proc.wait(timeout=5)


def record_track(sp, display, audio_sink, monitor_rect, playback, index, outdir):
    filename = (f"{index:02d}_{sanitize_filename(playback['artist'])} - "
                f"{sanitize_filename(playback['title'])}.mp4")
    out_path = os.path.join(outdir, filename)
    print(f"[{index}] {playback['artist']} - {playback['title']} -> {filename}")

    samplerate, channels = audio_sink.format
    proc = start_ffmpeg(monitor_rect, out_path, samplerate, channels)
    audio_sink.attach(proc.stdin)

    display.title(playback['artist'], playback['title'])
    display.pos(playback['position_sec'], playback['duration_sec'], playback['is_playing'])
    log_history(playback['artist'], playback['title'])

    lyrics_data = fetch_lyrics(playback['artist'], playback['title'], playback['duration_sec'])
    synced_text = lyrics_data.get('syncedLyrics') if lyrics_data else None
    plain_text = lyrics_data.get('plainLyrics') if lyrics_data else None
    if synced_text:
        lyrics_lines = parse_lrc(synced_text)
    elif plain_text:
        lyrics_lines = interpolate_plain_lyrics(plain_text, playback['duration_sec'])
    else:
        lyrics_lines = None

    anchor = PositionAnchor(playback['position_sec'], playback['at'], playback['is_playing'])
    stop_event = threading.Event()
    worker = None
    if lyrics_lines:
        worker = threading.Thread(target=lyrics_player,
                                  args=(lyrics_lines, anchor, stop_event, display),
                                  daemon=True)
        worker.start()

    # следим, пока играет этот же трек — ровно как в main.py; смена трека
    # или остановка воспроизведения заканчивает эту запись
    next_playback = None
    while True:
        time.sleep(POLL_INTERVAL)
        current = get_playback(sp)
        if current is None or current['track_id'] != playback['track_id']:
            next_playback = current
            break
        anchor.update(current['position_sec'], current['at'], current['is_playing'])
        display.pos(current['position_sec'], current['duration_sec'], current['is_playing'])

    stop_event.set()
    if worker:
        worker.join(timeout=1)

    stop_ffmpeg(proc, audio_sink)
    return next_playback


def main():
    parser = argparse.ArgumentParser(
        description="Запись лирик-видео: треки включаешь вручную в Spotify, "
                     "окно субтитров и его позицию тоже настраиваешь сам")
    parser.add_argument('--monitor', type=int, default=None,
                        help="Номер монитора для записи (по умолчанию — "
                             "автопоиск второго Full HD монитора)")
    parser.add_argument('--outdir', default='output',
                        help="Папка для готовых mp4 (по умолчанию output/)")
    args = parser.parse_args()

    if shutil.which('ffmpeg') is None:
        raise SystemExit(
            "ffmpeg не найден в PATH. Установите ffmpeg и добавьте его в PATH "
            "(например: winget install ffmpeg)."
        )

    sp = get_spotify_client()
    monitor_index, monitor_rect = pick_monitor(args.monitor)
    print(f"Монитор для записи #{monitor_index}: {monitor_rect}")
    os.makedirs(args.outdir, exist_ok=True)

    # окно субтитров открывается как в обычном режиме — без принудительного
    # позиционирования; на нужный монитор и в полноэкранный режим его
    # разворачиваешь сам
    display = DisplayConsole()
    audio_sink = AudioSink()
    threading.Thread(target=level_monitor, args=(display, audio_sink), daemon=True).start()
    wait_for_audio_format(audio_sink)

    print("Готово. Разверни окно субтитров на нужный монитор и включи трек "
          "в Spotify — запись каждого трека начнётся и закончится сама.")

    index = 0
    playback = None
    try:
        while True:
            if playback is None:
                playback = get_playback(sp)
            if playback is None:
                time.sleep(RETRY_DELAY)
                continue
            index += 1
            playback = record_track(sp, display, audio_sink, monitor_rect,
                                    playback, index, args.outdir)
    finally:
        display.close()


if __name__ == '__main__':
    try:
        main()
    except KeyboardInterrupt:
        pass
