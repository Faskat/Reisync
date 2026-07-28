"""
Отдельное консольное окно в стиле NERV/Evangelion для показа субтитров и
эквалайзера. Запускается как дочерний процесс из main.py, команды через stdin:

  TITLE:<артист>|<название>       — трек сменился (перерисовывает заголовок)
  LYRIC:<текущая>\t<следующая>    — строка субтитров + следующая за ней
  EQ:<уровень>;<b1,b2,...>        — общий уровень и полосы спектра (0..1)
  POS:<позиция>;<длительность>;<играет 0/1> — позиция в треке для прогресс-бара
  EXIT                            — завершение процесса

Тема оформления передаётся аргументом командной строки:
  --theme nerv|eva01|eva02|eva08|mass
  --swap-accent — поменять местами цвета интерфейса и арта (две семьи темы)
  --monitor-rect L,T,R,B — развернуть окно без рамки на весь указанный монитор
                           (виртуальные координаты экрана; задаёт
                           record_session.py, в обычном режиме main.py не используется)

Компоновка кадра:
  ┌ шапка
  │ артист обычной строкой (акцентом), под ним название трека артом
  │ во всю ширину (основным цветом)
  ├ сплошная линия во всю ширину
  │ зона: слева прошлая/текущая/следующая строки текста или спектр (по центру),
  │       справа Рей (по центру зоны)
  ├ сплошная линия во всю ширину
  │ прогресс трека (время + шкала)
  │ статус-строка
  └ эквалайзер во всю ширину

Шрифты: обычные FIGlet-шрифты не содержат кириллических глифов и молча выдают
мусор вместо русского текста; кириллицу поддерживает только семейство
TOIlet-шрифтов (mono9/smmono9 и т.д.) — поэтому используются именно они.

Устройство: поток-читатель принимает команды из stdin и складывает их в общее
состояние под локом, а главный поток крутит цикл отрисовки ~20 кадров в
секунду. Полосы эквалайзера на каждом кадре подтягиваются к целевым значениям
экспоненциально — поэтому движение плавное, а не ступеньками по мере прихода
EQ-команд. Позиция трека между POS-обновлениями (раз в ~3 сек) интерполируется
локально.
"""
import ctypes
from ctypes import wintypes
import math
import re
import shutil
import sys
import threading
import time

from pyfiglet import Figlet

sys.stdin.reconfigure(encoding='utf-8')
sys.stdout.reconfigure(encoding='utf-8', errors='replace')

STD_OUTPUT_HANDLE = -11
ENABLE_VIRTUAL_TERMINAL_PROCESSING = 0x0004
HWND_TOPMOST = -1
SWP_NOMOVE = 0x0002
SWP_NOSIZE = 0x0001
SWP_FRAMECHANGED = 0x0020
SWP_SHOWWINDOW = 0x0040
GWL_STYLE = -16
WS_POPUP = 0x80000000
WS_VISIBLE = 0x10000000

# Без явных argtypes/restype ctypes по умолчанию трактует параметры как
# 32-битный c_int — на 64-битной Windows HWND занимает 64 бита, и такая
# молчаливая усечённая маршализация ломает раскладку остальных параметров
# по регистрам: результат — не ошибка вызова, а тихо неверные аргументы
# (проверено: SetWindowPos возвращал 0 / ERROR_INVALID_WINDOW_HANDLE на
# заведомо валидном hwnd, пока сигнатуры не были объявлены явно).
_kernel32 = ctypes.windll.kernel32
_user32 = ctypes.windll.user32
_kernel32.GetConsoleWindow.restype = wintypes.HWND
_user32.SetWindowPos.argtypes = [wintypes.HWND, wintypes.HWND, ctypes.c_int, ctypes.c_int,
                                 ctypes.c_int, ctypes.c_int, ctypes.c_uint]
_user32.SetWindowPos.restype = wintypes.BOOL
_user32.SetWindowLongW.argtypes = [wintypes.HWND, ctypes.c_int, ctypes.c_long]
_user32.SetWindowLongW.restype = ctypes.c_long


class _WINDOWPLACEMENT(ctypes.Structure):
    _fields_ = [('length', ctypes.c_uint), ('flags', ctypes.c_uint),
                ('showCmd', ctypes.c_uint), ('ptMinPosition', wintypes.POINT),
                ('ptMaxPosition', wintypes.POINT), ('rcNormalPosition', wintypes.RECT)]


_user32.SetWindowPlacement.argtypes = [wintypes.HWND, ctypes.POINTER(_WINDOWPLACEMENT)]
_user32.SetWindowPlacement.restype = wintypes.BOOL

SW_SHOWNORMAL = 1

HOME = '\x1b[H'
CLEAR_BELOW = '\x1b[J'
CLEAR_EOL = '\x1b[K'
HIDE_CURSOR = '\x1b[?25l'
SHOW_CURSOR = '\x1b[?25h'
RESET = '\x1b[0m'

# ---------------------------------------------------------------- темы
# у каждой темы два акцента (номера цветов xterm-256):
#   ui / ui_dim / ui_accent — основное меню: шапка (bg), название, линии,
#                             прогресс, статус;
#   art / art_dim / art_bright + lyric — ASCII-арт текста, эквалайзер, спектр.
THEMES = {
    # текущая оранжевая NERV, арт и эквалайзер — ледяная синева Рей
    'nerv':  {'bg': 202, 'ui': 208, 'ui_dim': 130, 'ui_accent': 214,
              'art': 117, 'art_dim': 67, 'art_bright': 51, 'lyric': 159},
    # Ева-01: фиолетовый с зелёными акцентами (Синдзи)
    'eva01': {'bg': 93, 'ui': 141, 'ui_dim': 97, 'ui_accent': 177,
              'art': 40, 'art_dim': 28, 'art_bright': 118, 'lyric': 120},
    # Ева-02: красный (Аска), арт — оранжево-янтарный
    'eva02': {'bg': 160, 'ui': 196, 'ui_dim': 88, 'ui_accent': 203,
              'art': 208, 'art_dim': 130, 'art_bright': 214, 'lyric': 216},
    # Ева-08: розово-фиолетовый (Мари)
    'eva08': {'bg': 168, 'ui': 212, 'ui_dim': 132, 'ui_accent': 218,
              'art': 135, 'art_dim': 97, 'art_bright': 183, 'lyric': 225},
    # Серийные Евы: белая броня, Dummy Plug
    'mass':  {'bg': 252, 'ui': 252, 'ui_dim': 244, 'ui_accent': 255,
              'art': 250, 'art_dim': 240, 'art_bright': 231, 'lyric': 255},
}


def _pick_theme():
    if '--theme' in sys.argv:
        i = sys.argv.index('--theme')
        if i + 1 < len(sys.argv) and sys.argv[i + 1] in THEMES:
            return sys.argv[i + 1]
    return 'nerv'


def _pick_monitor_rect():
    """--monitor-rect L,T,R,B — координаты монитора для полноэкранного
    показа без рамки (передаёт record_session.py; в обычном запуске из
    main.py отсутствует, и окно ведёт себя как раньше)."""
    if '--monitor-rect' in sys.argv:
        i = sys.argv.index('--monitor-rect')
        if i + 1 < len(sys.argv):
            try:
                l, t, r, b = (int(x) for x in sys.argv[i + 1].split(','))
                return (l, t, r, b)
            except ValueError:
                pass
    return None


THEME_NAME = _pick_theme()
MONITOR_RECT = _pick_monitor_rect()
# --swap-accent меняет местами две цветовые семьи темы: ui (шапка, название,
# прогресс, статус) и art (арт персонажа, текст песни, спектр). Просто менять
# ui и ui_accent бессмысленно — это близкие оттенки одного цвета, и разницы
# не видно; акцент темы — именно art-семья (у eva01 ui фиолетовый, art зелёный)
SWAP_ACCENT = '--swap-accent' in sys.argv
_T = dict(THEMES[THEME_NAME])
if SWAP_ACCENT:
    _T = dict(_T,
              bg=_T['art'],
              ui=_T['art'], ui_dim=_T['art_dim'], ui_accent=_T['art_bright'],
              art=_T['ui'], art_dim=_T['ui_dim'], art_bright=_T['ui_accent'],
              lyric=_T['ui_accent'])


def _fg(n):
    return f'\x1b[38;5;{n}m'


def _xterm256_to_rgb(n):
    """Раскладывает индекс xterm-256 в приблизительный RGB — нужен, чтобы
    считать непрерывный градиент между цветами темы (сами цвета в THEMES
    заданы индексами, а не RGB, чтобы не дублировать палитру)."""
    if n < 16:
        basic = [(0, 0, 0), (128, 0, 0), (0, 128, 0), (128, 128, 0),
                  (0, 0, 128), (128, 0, 128), (0, 128, 128), (192, 192, 192),
                  (128, 128, 128), (255, 0, 0), (0, 255, 0), (255, 255, 0),
                  (0, 0, 255), (255, 0, 255), (0, 255, 255), (255, 255, 255)]
        return basic[n]
    if n >= 232:
        g = 8 + (n - 232) * 10
        return (g, g, g)
    n -= 16
    r, g, b = n // 36, (n // 6) % 6, n % 6
    level = lambda v: 0 if v == 0 else 55 + v * 40
    return (level(r), level(g), level(b))


def _lerp_rgb(c0, c1, t):
    return tuple(round(a + (b - a) * t) for a, b in zip(c0, c1))


def _rgb_fg(rgb):
    return f'\x1b[38;2;{rgb[0]};{rgb[1]};{rgb[2]}m'


def _gradient(stops):
    """Непрерывный градиент по 0..1 через несколько RGB-точек (в отличие от
    прежних дискретных порогов value<0.35/0.7, дающих видимые ступеньки)."""
    n = len(stops) - 1

    def color(value):
        value = max(0.0, min(value, 1.0))
        seg = min(int(value * n), n - 1)
        local_t = value * n - seg
        return _rgb_fg(_lerp_rgb(stops[seg], stops[seg + 1], local_t))
    return color


PRIMARY = _fg(_T['ui'])
PRIMARY_DIM = _fg(_T['ui_dim'])
ACCENT = _fg(_T['ui_accent'])
ART = _fg(_T['art'])
ART_DIM = _fg(_T['art_dim'])
ART_BRIGHT = _fg(_T['art_bright'])
ALERT = _fg(196)
LYRIC_C = _fg(_T['lyric'])
ACCENT2 = ART_BRIGHT
GRAY = _fg(242)
HEADER_BG = f'\x1b[48;5;{_T["bg"]}m\x1b[38;5;16m'

# нижняя строка эквалайзера — цвета основного меню (той же темы, что шапка
# и статус-строка), большой спектр-анализатор — цвета арта/текста песни
_UI_GRADIENT = _gradient([_xterm256_to_rgb(_T['ui_dim']),
                          _xterm256_to_rgb(_T['ui']),
                          _xterm256_to_rgb(_T['ui_accent'])])
_ART_GRADIENT = _gradient([_xterm256_to_rgb(_T['art_dim']),
                           _xterm256_to_rgb(_T['art']),
                           _xterm256_to_rgb(_T['art_bright'])])

FPS = 20
REVEAL_SECONDS = 0.5    # за сколько секунд строка "проявляется" слева направо
BAND_EASE = 0.38        # доля пути до целевого значения полосы за кадр

TITLE_FONT = 'mono9'    # кириллица поддерживается только TOIlet-шрифтами
SMALL_FONT = 'smmono9'

EQ_CHARS = ' ▁▂▃▄▅▆▇█'
SPECTRUM_HEIGHT = 9     # высота большого спектра (в строках), когда текста нет

ANSI_RE = re.compile(r'\x1b\[[0-9;?]*[A-Za-z]')


def _braille_bits(ch):
    if '⠀' <= ch <= '⣿':
        return bin(ord(ch) - 0x2800).count('1')
    return 0


def _clean_rei(raw_lines):
    """
    Убирает шумовой фон из Braille-арта. Простой порог по плотности символа
    не работает: он оставляет одиночные 3-точечные крапинки по бокам (⡘, ⢃)
    и при этом срезает лёгкие пряди волос (⣀, ⡀ у макушки). Правила:
    1-точечные символы — всегда шум; 2-3-точечные выживают, только если в
    окрестности 3x3 есть плотный (4+ точек) сосед — пряди, примыкающие к
    фигуре, остаются, а изолированная рябь и одиночные точки удаляются.
    Плотные символы не трогаем. Затем срезаем опустевшие поля.
    """
    grid = [list(line) for line in raw_lines]
    h = len(grid)
    dense = [[_braille_bits(ch) >= 4 for ch in row] for row in grid]

    cleaned = []
    for y, row in enumerate(grid):
        new_row = []
        for x, ch in enumerate(row):
            b = _braille_bits(ch)
            if b == 1:
                ch = '⠀'
            elif 1 < b <= 3:
                near_dense = any(
                    dense[ny][nx]
                    for ny in range(max(y - 1, 0), min(y + 2, h))
                    for nx in range(max(x - 1, 0), min(x + 2, len(grid[ny])))
                )
                if not near_dense:
                    ch = '⠀'
            new_row.append(ch)
        cleaned.append(''.join(new_row))

    left = min((len(l) - len(l.lstrip('⠀ ')) for l in cleaned if l.strip('⠀ ')), default=0)
    right = max((len(l.rstrip('⠀ ')) for l in cleaned), default=0)
    return [l[left:right].ljust(right - left, '⠀') for l in cleaned]


def _colorize_art(lines, shades):
    """
    Красит арт по плотности braille-символов: чем больше точек в символе, тем
    светлее цвет — плотные участки фигуры выходят яркими, разреженные уходят
    в тень. Считается один раз при старте, чтобы не жечь CPU на кадрах;
    одинаковые цвета подряд склеиваются в одну ANSI-последовательность.
    """
    colored = []
    for line in lines:
        parts = []
        current = None
        for ch in line:
            if '⠀' < ch <= '⣿':
                color = shades[_braille_bits(ch)]
            else:
                color = None
            if color != current:
                parts.append(RESET if color is None else f'\x1b[38;5;{color}m')
                current = color
            parts.append(ch)
        parts.append(RESET)
        colored.append(''.join(parts))
    return colored


def _figure_span(lines):
    """Горизонтальные границы самой фигуры (не рамки арта)."""
    first = None
    last = None
    for line in lines:
        stripped = line.rstrip('⠀ ')
        if not stripped.strip('⠀ '):
            continue
        lead = len(line) - len(line.lstrip('⠀ '))
        first = lead if first is None else min(first, lead)
        last = len(stripped) if last is None else max(last, len(stripped))
    return (first or 0), (last or 0)


import theme_arts

# персонаж угла — по теме: (арт, нужна ли чистка фона, подпись, имя юнита)
THEME_CHARACTER = {
    'nerv':  (theme_arts.REI, True, 'AYANAMI REI · EVA UNIT-00', 'UNIT-00'),
    'eva01': (theme_arts.SHINJI, False, 'IKARI SHINJI · EVA UNIT-01', 'UNIT-01'),
    'eva02': (theme_arts.ASUKA, False, 'SORYU ASUKA LANGLEY · EVA UNIT-02', 'UNIT-02'),
    'eva08': (theme_arts.MARI, False, 'MAKINAMI MARI · EVA UNIT-08', 'UNIT-08'),
    'mass':  (theme_arts.MASS, False, 'MASS PRODUCTION MODEL · DUMMY PLUG', 'MP-EVA'),
}

# все персонажи красятся арт-акцентом своей темы (в nerv это и есть
# сине-стальная палитра Рей) — так --swap-accent перекрашивает и арт
_theme_shades = {1: _T['art_dim'], 2: _T['art_dim'], 3: _T['art_dim'],
                 4: _T['art'], 5: _T['art'], 6: _T['art'],
                 7: _T['art_bright'], 8: _T['art_bright']}

_char_raw, _char_clean, CHAR_CAPTION, UNIT_LABEL = THEME_CHARACTER[THEME_NAME]
CHAR_ART = _clean_rei(_char_raw) if _char_clean else list(_char_raw)
CHAR_COLORED = _colorize_art(CHAR_ART, _theme_shades)
CHAR_WIDTH = max(len(line) for line in CHAR_ART)
CHAR_BLOCK_HEIGHT = len(CHAR_ART) + 2  # арт + отступ + строка подписи
# подпись центруем по самой фигуре, а не по прямоугольнику арта
_span_l, _span_r = _figure_span(CHAR_ART)
_caption_pad = max((_span_l + _span_r - len(CHAR_CAPTION)) // 2, 0)
CHAR_CAPTION_ROW = (' ' * _caption_pad + CHAR_CAPTION).ljust(CHAR_WIDTH)[:CHAR_WIDTH]


def enable_console():
    kernel32 = ctypes.windll.kernel32
    try:
        kernel32.SetConsoleOutputCP(65001)
        kernel32.SetConsoleCP(65001)
    except OSError:
        pass

    handle = kernel32.GetStdHandle(STD_OUTPUT_HANDLE)
    mode = ctypes.c_uint32()
    kernel32.GetConsoleMode(handle, ctypes.byref(mode))
    kernel32.SetConsoleMode(handle, mode.value | ENABLE_VIRTUAL_TERMINAL_PROCESSING)


def _to_signed32(value):
    """WinAPI-стилевые флаги (WS_POPUP и т.п.) выше 0x7FFFFFFF ctypes не
    примет как обычный python int для LONG-параметра — нужно явно свернуть
    в знаковое 32-битное представление (двоичное дополнение)."""
    value &= 0xFFFFFFFF
    return value - 0x100000000 if value & 0x80000000 else value


class _COORD(ctypes.Structure):
    _fields_ = [('X', ctypes.c_short), ('Y', ctypes.c_short)]


class _SMALL_RECT(ctypes.Structure):
    _fields_ = [('Left', ctypes.c_short), ('Top', ctypes.c_short),
                ('Right', ctypes.c_short), ('Bottom', ctypes.c_short)]


class _CONSOLE_FONT_INFOEX(ctypes.Structure):
    _fields_ = [('cbSize', ctypes.c_ulong), ('nFont', ctypes.c_ulong),
                ('dwFontSize', _COORD), ('FontFamily', ctypes.c_uint),
                ('FontWeight', ctypes.c_uint), ('FaceName', ctypes.c_wchar * 32)]


_FF_MODERN_TRUETYPE = 0x36
_FULLSCREEN_FONT_HEIGHT_PX = 18

# те же соображения о явных argtypes/restype, что и у SetWindowPos выше —
# все эти функции принимают/возвращают HANDLE (64-битный указатель)
_kernel32.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
                                  wintypes.LPVOID, wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
_kernel32.CreateFileW.restype = wintypes.HANDLE
_kernel32.SetCurrentConsoleFontEx.argtypes = [wintypes.HANDLE, wintypes.BOOL,
                                              ctypes.POINTER(_CONSOLE_FONT_INFOEX)]
_kernel32.SetCurrentConsoleFontEx.restype = wintypes.BOOL
_kernel32.GetCurrentConsoleFontEx.argtypes = [wintypes.HANDLE, wintypes.BOOL,
                                              ctypes.POINTER(_CONSOLE_FONT_INFOEX)]
_kernel32.GetCurrentConsoleFontEx.restype = wintypes.BOOL
_kernel32.SetConsoleWindowInfo.argtypes = [wintypes.HANDLE, wintypes.BOOL,
                                           ctypes.POINTER(_SMALL_RECT)]
_kernel32.SetConsoleWindowInfo.restype = wintypes.BOOL
_kernel32.SetConsoleScreenBufferSize.argtypes = [wintypes.HANDLE, _COORD]
_kernel32.SetConsoleScreenBufferSize.restype = wintypes.BOOL


def _console_out_handle():
    GENERIC_READ_WRITE = 0x80000000 | 0x40000000
    FILE_SHARE_READ_WRITE = 1 | 2
    OPEN_EXISTING = 3
    handle = _kernel32.CreateFileW(
        'CONOUT$', GENERIC_READ_WRITE, FILE_SHARE_READ_WRITE, None,
        OPEN_EXISTING, 0, None
    )
    invalid = ctypes.cast(-1, wintypes.HANDLE).value
    return None if not handle or handle == invalid else handle


def _fit_buffer_to_pixels(handle, width_px, height_px):
    """
    Окно консоли нельзя растянуть больше, чем позволяет текущий буфер (строки
    x столбцы) при текущем шрифте — просто передвинуть/увеличить окно
    недостаточно, Windows тихо подожмёт его обратно под старый буфер. Поэтому
    сначала подбираем шрифт нужной высоты и считаем, сколько строк/столбцов
    нужно буферу, чтобы в пикселях получить примерно width x height, и только
    потом можно двигать/растягивать само окно.
    """
    kernel32 = ctypes.windll.kernel32

    font = _CONSOLE_FONT_INFOEX()
    font.cbSize = ctypes.sizeof(_CONSOLE_FONT_INFOEX)
    font.dwFontSize = _COORD(0, _FULLSCREEN_FONT_HEIGHT_PX)
    font.FontFamily = _FF_MODERN_TRUETYPE
    font.FontWeight = 400
    font.FaceName = 'Consolas'
    kernel32.SetCurrentConsoleFontEx(handle, False, ctypes.byref(font))

    applied = _CONSOLE_FONT_INFOEX()
    applied.cbSize = ctypes.sizeof(_CONSOLE_FONT_INFOEX)
    kernel32.GetCurrentConsoleFontEx(handle, False, ctypes.byref(applied))
    cell_w = max(applied.dwFontSize.X, 1)
    cell_h = max(applied.dwFontSize.Y, 1)

    cols = max(width_px // cell_w, 10)
    rows = max(height_px // cell_h, 10)

    # сначала схлопываем окно — иначе запрос буфера меньше текущего окна
    # (частый случай при уменьшении) завершится ERROR_INVALID_PARAMETER
    kernel32.SetConsoleWindowInfo(handle, True, ctypes.byref(_SMALL_RECT(0, 0, 0, 0)))
    kernel32.SetConsoleScreenBufferSize(handle, _COORD(cols, rows))
    kernel32.SetConsoleWindowInfo(handle, True,
                                  ctypes.byref(_SMALL_RECT(0, 0, cols - 1, rows - 1)))
    return cols, rows, cell_w, cell_h


def _place(hwnd, rect):
    placement = _WINDOWPLACEMENT()
    placement.length = ctypes.sizeof(_WINDOWPLACEMENT)
    placement.showCmd = SW_SHOWNORMAL
    placement.rcNormalPosition = wintypes.RECT(*rect)
    _user32.SetWindowPlacement(hwnd, ctypes.byref(placement))


def make_borderless_fullscreen(hwnd, rect):
    """
    Две неочевидные ловушки консольного окна, найденные опытным путём:

    1. SetWindowPos тут не годится — у консольного окна свой
       WM_WINDOWPOSCHANGING, который переигрывает X/Y по-своему (возврат
       SetWindowPos был "успех", но итоговое окно оказывалось не там, где
       просили). SetWindowPlacement идёт другим путём и этой перезаписи не
       подвержен.
    2. Если сразу поставить SetWindowPlacement на большой (во весь монитор)
       прямоугольник, ПОЗИЦИЯ тоже сбивается — но если сначала поставить
       маленький прямоугольник ровно на целевом мониторе, а потом ВТОРЫМ
       вызовом растянуть до полного размера, позиция держится верно. Похоже,
       Windows валидирует позицию актуальным (на момент вызова) монитором
       окна, а его исходно нет на целевом мониторе.

    Известное ограничение: SetWindowPlacement кладёт "нормальное" (не
    развёрнутое) окно в границы рабочей области монитора, то есть за вычетом
    панели задач, если она показана на этом мониторе — тогда внизу останется
    небольшая (высота панели задач) чёрная полоса. Прячьте панель задач на
    втором мониторе, если нужна честная заливка на весь физический экран.
    """
    left, top, right, bottom = rect
    width_px, height_px = right - left, bottom - top

    style = _to_signed32(WS_POPUP | WS_VISIBLE)
    _user32.SetWindowLongW(hwnd, GWL_STYLE, style)

    handle = _console_out_handle()
    if handle is not None:
        _fit_buffer_to_pixels(handle, width_px, height_px)

    _place(hwnd, (left, top, left + 200, top + 200))
    _place(hwnd, rect)


def pin_on_top(monitor_rect=None):
    kernel32 = ctypes.windll.kernel32
    user32 = ctypes.windll.user32
    kernel32.SetConsoleTitleW("NERV — MAGI LYRIC SYNC")
    hwnd = kernel32.GetConsoleWindow()
    if not hwnd:
        return
    if monitor_rect:
        make_borderless_fullscreen(hwnd, monitor_rect)
    else:
        user32.SetWindowPos(hwnd, HWND_TOPMOST, 0, 0, 0, 0, SWP_NOMOVE | SWP_NOSIZE)


def term_size():
    size = shutil.get_terminal_size(fallback=(120, 34))
    return size.columns, size.lines


class ArtCache:
    """Арт пересчитывается только когда меняется текст или ширина окна, а не
    на каждом кадре. Объект Figlet передаётся снаружи и разделяется между
    кэшами: загрузка TOIlet-шрифта занимает ~0.75 сек, и грузить один шрифт
    дважды незачем."""
    def __init__(self, figlet):
        self.figlet = figlet
        self.key = None
        self.lines = []

    def get(self, text, width):
        key = (text, width)
        if key != self.key:
            self.key = key
            self.lines = self._render(text, width)
        return self.lines

    def _render(self, text, width):
        if not text:
            return []
        try:
            self.figlet.width = max(width, 20)
            art = self.figlet.renderText(text)
            lines = [line.rstrip() for line in art.split('\n')]
            while lines and not lines[0].strip():
                lines.pop(0)
            while lines and not lines[-1].strip():
                lines.pop()
            if lines:
                return lines
        except Exception:
            pass
        return [text]  # фолбэк — обычный текст, если шрифт не справился


def center_art(lines, width):
    """
    Центрирует figlet-арт по горизонтали. Длинный текст figlet заворачивает
    в несколько блоков, разделённых пустыми строками, — каждый блок центруем
    отдельно по его собственной ширине, иначе вторая строка текста прилипает
    к левому краю.
    """
    result = []
    block = []

    def flush():
        if not block:
            return
        block_w = max(len(l) for l in block)
        pad = ' ' * max((width - block_w) // 2, 0)
        result.extend(pad + l for l in block)
        block.clear()

    for line in lines:
        if line.strip():
            block.append(line)
        else:
            flush()
            result.append('')
    flush()
    return result


class State:
    def __init__(self):
        self.lock = threading.Lock()
        self.artist = ''
        self.title = ''
        self.lyric = ''
        self.prev_lyric = ''
        self.next_lyric = ''
        self.lyric_since = 0.0
        self.level = 0.0
        self.bands = [0.0] * 24
        self.position = 0.0
        self.duration = 0.0
        self.playing = False
        self.position_at = 0.0
        self.alive = True

    def snapshot(self):
        with self.lock:
            return {
                'artist': self.artist, 'title': self.title,
                'lyric': self.lyric, 'prev_lyric': self.prev_lyric,
                'next_lyric': self.next_lyric, 'lyric_since': self.lyric_since,
                'level': self.level, 'bands': list(self.bands),
                'position': self.position, 'duration': self.duration,
                'playing': self.playing, 'position_at': self.position_at,
                'alive': self.alive,
            }


def stdin_reader(state):
    for raw_line in sys.stdin:
        line = raw_line.rstrip('\n')
        if not line:
            continue
        if line == 'EXIT':
            break
        command, _, payload = line.partition(':')
        with state.lock:
            if command == 'TITLE':
                artist, _, title = payload.partition('|')
                state.artist = artist
                state.title = title or artist
                state.lyric = ''
                state.prev_lyric = ''
                state.next_lyric = ''
            elif command == 'LYRIC':
                current, _, nxt = payload.partition('\t')
                state.prev_lyric = state.lyric
                state.lyric = current
                state.next_lyric = nxt
                state.lyric_since = time.monotonic()
            elif command == 'EQ':
                level_str, _, bands_str = payload.partition(';')
                try:
                    state.level = float(level_str)
                    if bands_str:
                        state.bands = [float(x) for x in bands_str.split(',')]
                except ValueError:
                    pass
            elif command == 'POS':
                parts = payload.split(';')
                try:
                    state.position = float(parts[0])
                    state.duration = float(parts[1])
                    state.playing = parts[2] == '1'
                    state.position_at = time.monotonic()
                except (ValueError, IndexError):
                    pass
    with state.lock:
        state.alive = False


def visible_pad(text, width):
    return text[:width].ljust(width)


def visible_len(text):
    """Длина строки без учёта ANSI-кодов цвета."""
    return len(ANSI_RE.sub('', text))


def header_line(width, now):
    label = ' N E R V ▸ MAGI LYRIC-SYNC SYSTEM ▸ REISYNC '
    blink = '●' if int(now * 2) % 2 else '○'
    right = f' {blink} SOUND ONLY '
    fill = max(width - len(label) - len(right), 0)
    return f"{HEADER_BG}{label}{' ' * fill}{right}{RESET}"


def eq_line(width, bands, now):
    """Однострочный спектр во всю ширину — реальные полосы из FFT."""
    if not bands:
        bands = [0.0]
    chars = []
    n = len(bands)
    span = max(width - 1, 1)
    for x in range(span):
        # плавная интерполяция между соседними полосами вместо ступенек
        f = x * (n - 1) / max(span - 1, 1)
        i = int(f)
        frac = f - i
        v = bands[i] * (1 - frac) + bands[min(i + 1, n - 1)] * frac
        v *= 0.94 + 0.06 * math.sin(now * 2.5 + x * 0.2)
        v = max(0.0, min(v, 1.0))
        idx = min(int(v * len(EQ_CHARS)), len(EQ_CHARS) - 1)
        chars.append(f"{_UI_GRADIENT(v)}{EQ_CHARS[idx]}")
    return ''.join(chars) + RESET


def spectrum_rows(width, bands):
    """Большой вертикальный спектр-анализатор (по центру левой зоны)."""
    n_bands = max(min(len(bands), (width - 4) // 3), 1)
    block_w = n_bands * 3
    pad = ' ' * max((width - block_w) // 2, 0)
    rows = []
    for row in range(SPECTRUM_HEIGHT):
        cells = []
        threshold_top = SPECTRUM_HEIGHT - row
        # непрерывный градиент по высоте столбика — не 3 дискретные полосы
        color = _ART_GRADIENT(1 - row / max(SPECTRUM_HEIGHT - 1, 1))
        for b in range(n_bands):
            v = bands[b * len(bands) // n_bands] * SPECTRUM_HEIGHT * 1.3
            if v >= threshold_top:
                ch = '██'
            elif v > threshold_top - 1:
                ch = EQ_CHARS[min(int((v - (threshold_top - 1)) * 8) + 1, 8)] * 2
            else:
                ch = '  '
            cells.append(f"{color}{ch}{RESET} ")
        rows.append(pad + ''.join(cells))
    return rows


def format_time(seconds):
    seconds = max(int(seconds), 0)
    return f"{seconds // 60:02d}:{seconds % 60:02d}"


def progress_line(width, snap, now):
    """Строка прогресса трека: текущее время, шкала, длительность."""
    duration = snap['duration']
    position = snap['position']
    if snap['playing']:
        position += now - snap['position_at']
    if duration > 0:
        position = min(position, duration)
        ratio = position / duration
    else:
        ratio = 0.0

    cur_str = format_time(position)
    dur_str = format_time(duration)
    bar_w = max(width - len(cur_str) - len(dur_str) - 6, 10)
    exact = ratio * bar_w
    filled = int(exact)
    if filled < bar_w:
        # дробный край шкалы — движется плавно, а не скачками по целой клетке
        partials = ' ▏▎▍▌▋▊▉'
        edge = partials[min(int((exact - filled) * 8), 7)]
        empty = bar_w - filled - 1
    else:
        edge = ''
        empty = 0
    bar = f"{PRIMARY}{'█' * filled}{edge}{GRAY}{'░' * empty}{RESET}"
    return f" {ACCENT}{cur_str}{RESET}  {bar}  {ACCENT}{dur_str}{RESET}"


def status_line(level, has_lyrics, now):
    sync = max(0.0, min(40 + level * 55 + 4 * math.sin(now * 1.3), 99.9))
    sync_color = ACCENT if sync > 30 else ALERT
    pattern = f"{ACCENT2}PATTERN: BLUE{RESET}" if has_lyrics else f"{PRIMARY}PATTERN: ORANGE{RESET}"
    sep = f"{PRIMARY_DIM} ▌ {RESET}"
    parts = [
        f"{sync_color}SYNC RATE: {sync:04.1f}%{RESET}",
        pattern,
        f"{GRAY}A.T. FIELD: NOMINAL{RESET}",
        f"{LYRIC_C}{UNIT_LABEL}: ACTIVE{RESET}",
    ]
    return ' ' + sep.join(parts)


def compose_frame(snap, caches, disp_bands, now):
    width, height = term_size()
    left_w = max(width - CHAR_WIDTH - 4, 30)

    # --- верхняя зона: артист — название одной строкой у левого края ---
    artist = snap['artist']
    title = snap['title']
    title_w = width - 2
    # TITLE-команда при пустом названии дублирует артиста в title —
    # в этом случае не пишем одно и то же дважды
    two_part = bool(artist and title and title != artist)
    headline = f"{artist} - {title}" if two_part else (title or artist)

    def plain_headline():
        if two_part:
            return f"  {ACCENT}♪ {artist}{RESET}{PRIMARY_DIM} — {RESET}{PRIMARY}{title}"
        return f"  {ACCENT}♪ {headline}"

    def build_top(as_art):
        rows = [('', '')]
        if not headline:
            rows.append((ACCENT, "  ♪ ОЖИДАНИЕ СИГНАЛА..."))
        elif as_art:
            # артист обычной строкой сверху, название артом во всю ширину
            if two_part:
                rows.append((ACCENT, f"  ♪ {artist}"))
                rows.append(('', ''))
            text = title if two_part else headline
            size = 'big' if len(text) * 7 <= title_w else 'small'
            for line in caches[f'title_{size}'].get(text, title_w)[:12]:
                rows.append((PRIMARY, '  ' + line))
        else:
            rows.append(('', plain_headline()))
        rows.append(('', ''))
        return rows

    top_rows = build_top(as_art=True)
    # в невысоком окне арт заголовка съедает зону и персонаж обрезается
    # снизу — тогда заголовок остаётся обычной строкой
    if height - 5 - len(top_rows) - 1 < CHAR_BLOCK_HEIGHT:
        top_rows = build_top(as_art=False)

    # --- средняя зона: прошлая/текущая/следующая строки или спектр ---
    lyric = snap['lyric']
    mid_rows = []
    if lyric:
        if snap['prev_lyric']:
            prev = snap['prev_lyric']
            pad = ' ' * max((left_w - len(prev)) // 2, 0)
            mid_rows.append((GRAY, pad + prev))
            mid_rows.append(('', ''))
        reveal = int(left_w * min((now - snap['lyric_since']) / REVEAL_SECONDS, 1.0))
        for line in center_art(caches['lyric'].get(lyric, left_w), left_w):
            mid_rows.append((LYRIC_C, line[:reveal]))
        if snap['next_lyric']:
            nxt = snap['next_lyric']
            pad = ' ' * max((left_w - len(nxt)) // 2, 0)
            mid_rows.append(('', ''))
            mid_rows.append((GRAY, pad + nxt))
    else:
        label = 'AUDIO SPECTRUM ANALYSIS'
        pad = ' ' * max((left_w - len(label)) // 2, 0)
        mid_rows.append((ART_DIM, pad + label))
        mid_rows.append(('', ''))
        for spec_row in spectrum_rows(left_w, disp_bands):
            mid_rows.append((None, spec_row))

    # --- сборка кадра ---
    # низ: линия, прогресс, статус, эквалайзер = 4 строки; шапка = 1
    zone_height = max(height - 5 - len(top_rows) - 1, CHAR_BLOCK_HEIGHT)
    rei_offset = max((height - 5 - len(top_rows) - 1 - CHAR_BLOCK_HEIGHT) // 2, 0)
    mid_offset = max((zone_height - len(mid_rows)) // 2, 0)

    frame_lines = [header_line(width, now)]

    for color, text in top_rows:
        frame_lines.append(f"{color}{text}{RESET}" if text else '')

    frame_lines.append(f"{PRIMARY_DIM}{'─' * (width - 1)}{RESET}")

    for i in range(zone_height):
        m = i - mid_offset
        if 0 <= m < len(mid_rows):
            color, text = mid_rows[m]
            if color is None:
                left = text + ' ' * max(left_w - visible_len(text), 0)
            elif text:
                left = f"{color}{visible_pad(text, left_w)}{RESET}"
            else:
                left = ' ' * left_w
        else:
            left = ' ' * left_w

        j = i - rei_offset
        if 0 <= j < len(CHAR_ART):
            frame_lines.append(f"{left}  {CHAR_COLORED[j]}")
        elif j == len(CHAR_ART) + 1:
            # +1, а не сразу после арта — отступ, чтобы подпись не липла к фигуре
            frame_lines.append(f"{left}  {ACCENT2}{CHAR_CAPTION_ROW}{RESET}")
        else:
            frame_lines.append(left)

    frame_lines = frame_lines[:height - 4]
    frame_lines.append(f"{PRIMARY_DIM}{'─' * (width - 1)}{RESET}")
    frame_lines.append(progress_line(width, snap, now))
    frame_lines.append(status_line(snap['level'], bool(lyric), now))
    frame_lines.append(eq_line(width, disp_bands, now))

    return HOME + (CLEAR_EOL + '\n').join(frame_lines) + CLEAR_BELOW


def main():
    enable_console()
    pin_on_top(MONITOR_RECT)
    sys.stdout.write(HIDE_CURSOR)

    # загрузка шрифтов ниже занимает ~1.5 сек — показываем заставку сразу,
    # чтобы окно не висело чёрным
    boot = [
        header_line(term_size()[0], 0.0),
        '',
        f"{PRIMARY}  MAGI SYSTEM BOOT SEQUENCE{RESET}",
        f"{GRAY}  CASPER-3 ... OK{RESET}",
        f"{GRAY}  BALTHASAR-2 ... OK{RESET}",
        f"{GRAY}  MELCHIOR-1 ... OK{RESET}",
        '',
        f"{ACCENT}  LOADING GLYPH MATRICES ...{RESET}",
    ]
    sys.stdout.write(HOME + (CLEAR_EOL + '\n').join(boot) + CLEAR_BELOW)
    sys.stdout.flush()

    state = State()
    reader = threading.Thread(target=stdin_reader, args=(state,), daemon=True)
    reader.start()

    # два шрифта грузим по одному разу и разделяем между кэшами
    big_figlet = Figlet(font=TITLE_FONT)
    small_figlet = Figlet(font=SMALL_FONT)
    caches = {
        'title_big': ArtCache(big_figlet),
        'title_small': ArtCache(small_figlet),
        'lyric': ArtCache(small_figlet),
    }

    # отображаемые полосы тянутся к целевым каждый кадр — плавное движение
    disp_bands = [0.0] * 24

    try:
        while True:
            snap = state.snapshot()
            if not snap['alive']:
                break

            targets = snap['bands']
            if len(disp_bands) != len(targets):
                disp_bands = [0.0] * len(targets)
            for i, target in enumerate(targets):
                disp_bands[i] += (target - disp_bands[i]) * BAND_EASE

            now = time.monotonic()
            sys.stdout.write(compose_frame(snap, caches, disp_bands, now))
            sys.stdout.flush()
            time.sleep(1 / FPS)
    finally:
        sys.stdout.write(SHOW_CURSOR + RESET)
        sys.stdout.flush()


if __name__ == '__main__':
    try:
        main()
    except (KeyboardInterrupt, EOFError):
        pass
