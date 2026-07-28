import threading
import time

from reisync_core import (
    DisplayConsole,
    PositionAnchor,
    RETRY_DELAY,
    POLL_INTERVAL,
    fetch_lyrics,
    get_playback,
    get_spotify_client,
    interpolate_plain_lyrics,
    level_monitor,
    log_history,
    lyrics_player,
    parse_lrc,
)


def main_loop():
    display = DisplayConsole()
    threading.Thread(target=level_monitor, args=(display,), daemon=True).start()
    try:
        _main_loop(display)
    finally:
        display.close()


def _main_loop(display):
    sp = get_spotify_client()

    while True:
        playback = get_playback(sp)

        if playback is None:
            print(f"В Spotify ничего не играет. Проверю через {RETRY_DELAY} сек.")
            time.sleep(RETRY_DELAY)
            continue

        print(f"Сейчас играет: {playback['artist']} - {playback['title']} "
              f"(позиция {playback['position_sec']:.0f} сек)")
        display.title(playback['artist'], playback['title'])
        display.pos(playback['position_sec'], playback['duration_sec'], playback['is_playing'])
        log_history(playback['artist'], playback['title'])

        lyrics_data = fetch_lyrics(playback['artist'], playback['title'], playback['duration_sec'])
        synced_text = lyrics_data.get('syncedLyrics') if lyrics_data else None
        plain_text = lyrics_data.get('plainLyrics') if lyrics_data else None

        if synced_text:
            lyrics_lines = parse_lrc(synced_text)
            print(f"Получено {len(lyrics_lines)} строк субтитров")
        elif plain_text:
            lyrics_lines = interpolate_plain_lyrics(plain_text, playback['duration_sec'])
            print(f"Синхротекст не найден, использую обычный текст с приблизительным таймингом "
                  f"({len(lyrics_lines)} строк)")
        else:
            lyrics_lines = None
            print("Текст песни не найден — окно покажет спектр-анализатор.")

        anchor = PositionAnchor(playback['position_sec'], playback['at'], playback['is_playing'])
        stop_event = threading.Event()
        worker_thread = None

        if lyrics_lines:
            worker_thread = threading.Thread(
                target=lyrics_player,
                args=(lyrics_lines, anchor, stop_event, display),
                daemon=True
            )
            worker_thread.start()

        # следим за плеером, пока играет этот же трек: каждый опрос поправляет
        # anchor точной позицией из Spotify (тем самым отрабатываются пауза,
        # перемотка и накопившийся дрейф), а смена трека завершает цикл
        while True:
            time.sleep(POLL_INTERVAL)
            current = get_playback(sp)
            if current is None or current['track_id'] != playback['track_id']:
                break
            anchor.update(current['position_sec'], current['at'], current['is_playing'])
            display.pos(current['position_sec'], current['duration_sec'], current['is_playing'])

        stop_event.set()
        if worker_thread:
            worker_thread.join(timeout=1)


if __name__ == '__main__':
    try:
        main_loop()
    except KeyboardInterrupt:
        pass
