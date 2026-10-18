"""
bio_checkin.py — оверлей "how are you" -> bio в Telegram.

Установка:  pip install pillow numpy telethon
Логин:      python bio_checkin.py --login
Запуск:     python bio_checkin.py              (спросит режим)
            python bio_checkin.py --preview    (сразу preview)
            python bio_checkin.py --working    (сразу рабочий режим)
            python bio_checkin.py --logo       (сразу карточка логотипа)

Режимы:
  preview         — оверлей показывается сразу, в Telegram ничего не уходит
  see in working  — оверлей по простою (idle_minutes из конфига), bio реально обновляется
  logo card       — карточка логотипа: наклоняется к курсору, клик открывает ссылку из links.json

Команды консоли: check_health, logo, close, status, bio, idle, help, quit
"""
import asyncio
import ctypes
import json
import math
import os
import queue
import re
import sys
import threading
import time
import webbrowser
import tkinter as tk
import tkinter.font as tkfont
from ctypes import wintypes
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw, ImageFont

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

# Свои размеры в пикселях -> нужен DPI-aware процесс (до создания Tk)
try:
    ctypes.windll.shcore.SetProcessDpiAwareness(2)
except Exception:
    pass

FROZEN = getattr(sys, "frozen", False)
BASE = Path(sys.executable).parent if FROZEN else Path(__file__).parent   # config.json, сессия, links.json, fonts/ — рядом с exe
RES = Path(getattr(sys, "_MEIPASS", BASE))                                 # ресурсы, вшитые в сборку (логотип, шрифты)
CONFIG_PATH = BASE / "config.json"
STATE_PATH = BASE / "state.json"
LOGO_PATH = BASE / "mentalizer_logo.png"
LINKS_PATH = BASE / "links.json"

DEFAULT_CONFIG = {
    "api_id": 0,                    # с my.telegram.org
    "api_hash": "",
    "session": "bio_session",
    "interval_minutes": 60,         # раз в сколько минут показывать оверлей (0 — выключить)
    "idle_minutes": 0,              # дополнительно: показать после N минут простоя (0 — выкл.)
    "game_processes": [],           # игры вне Steam по имени процесса, напр. ["Rhythia.exe"]
    "ignore_processes": ["telegram.exe", "discord.exe", "chrome.exe", "msedge.exe", "firefox.exe", "opera.exe",
                         "brave.exe", "vivaldi.exe", "vlc.exe", "mpv.exe", "powerpnt.exe", "zoom.exe", "teams.exe",
                         "ms-teams.exe", "wallpaper32.exe", "wallpaper64.exe", "wallpaperservice32.exe",
                         "wallpaperservice32_admin.exe", "ui32.exe", "losslessscaling.exe", "aseprite.exe",
                         "blender.exe", "krita.exe", "soundpad.exe"],   # никогда не считаются игрой
    "game_dirs": [],                # доп. папки с играми (подстрока пути), напр. "\\Epic Games\\" — Steam находится сам
    "fullscreen_busy": False,       # True — считать игрой любое полноэкранное приложение (видео и т.п.)
    "respect_dnd": True,            # не показывать при включённом «не беспокоить» (фокусировка) в Windows
    "game_grace_seconds": 20,       # сколько секунд после закрытия игры подождать перед показом
    "default_mode": "",             # "working" / "preview" / "logo" — не спрашивать режим при запуске
    "poll_seconds": 5,              # как часто проверять простой
    "blocks": 5,                    # длина полоски в bio
    "bio_limit": 70,                # 140 если Premium
    "position": "bottom-right",     # bottom-right | bottom-left | top-right | top-left | center
    "margin": 24,                   # отступ от края рабочей области
    "fps": 60,
    "tray": True,                   # рабочий режим живёт в трее; клик по значку — главное меню
    "hide_console": True,           # в собранном exe прятать окно консоли, когда запущен трей
    "log_file": True,               # дублировать вывод в mentalizer.log (рядом с exe) — пригодится при скрытой консоли
    "menu_on_start": False,         # показать главное меню сразу при запуске                      # частота анимации оверлея
    "proxy": None,                  # {"type":"socks5","host":"127.0.0.1","port":1080} или {"type":"mtproto","host":..,"port":..,"secret":..}
    "obfuscated": False,            # true — обфусцированный транспорт (нужен для tg-ws-proxy)
    "bio_style": "lines",           # "lines" — три строки, "oneline" — в одну через " | "
    "font": "",                     # свой шрифт: имя файла из папки скрипта или fonts/ (напр. "Inter-Medium.ttf") или путь
    "font_fallback": ["MontserratAlternates-SemiBold.ttf"],   # для строк с символами, которых нет в основном шрифте (кириллица)
    "font_fallback_scale": 0.9,     # масштаб текста запасного шрифта
    "font_scale": 1.0,              # масштаб текста интерфейса (у разных шрифтов разный «визуальный» размер)
    "font_gamma": 0.85,             # плотность текста: меньше — жирнее, 1.0 — как есть
    "scale": 1.0,                   # размер оверлея: 0.75 — компактнее, 1.25 — крупнее
}


def load_config():
    if not CONFIG_PATH.exists():
        CONFIG_PATH.write_text(json.dumps(DEFAULT_CONFIG, indent=2), encoding="utf-8")
        print(f"[config] создан {CONFIG_PATH.name}")
    return {**DEFAULT_CONFIG, **json.loads(CONFIG_PATH.read_text(encoding="utf-8"))}


def load_state():
    if STATE_PATH.exists():
        return json.loads(STATE_PATH.read_text(encoding="utf-8"))
    return {"mental": 70, "physical": 50, "social": 80}


# ---------- Windows: простой пользователя и рабочая область ----------
class LASTINPUTINFO(ctypes.Structure):
    _fields_ = [("cbSize", wintypes.UINT), ("dwTime", wintypes.DWORD)]


def idle_seconds():
    info = LASTINPUTINFO()
    info.cbSize = ctypes.sizeof(info)
    ctypes.windll.user32.GetLastInputInfo(ctypes.byref(info))
    return (ctypes.windll.kernel32.GetTickCount() - info.dwTime) / 1000


def work_area():
    """Экран без панели задач: (left, top, right, bottom)."""
    try:
        r = wintypes.RECT()
        ctypes.windll.user32.SystemParametersInfoW(48, 0, ctypes.byref(r), 0)  # SPI_GETWORKAREA
        return r.left, r.top, r.right, r.bottom
    except Exception:
        return 0, 0, 1920, 1040


# ---------- игра запущена? расписание показа ----------
class PROCESSENTRY32W(ctypes.Structure):
    _fields_ = [("dwSize", wintypes.DWORD), ("cntUsage", wintypes.DWORD), ("th32ProcessID", wintypes.DWORD),
                ("th32DefaultHeapID", ctypes.c_size_t), ("th32ModuleID", wintypes.DWORD),
                ("cntThreads", wintypes.DWORD), ("th32ParentProcessID", wintypes.DWORD),
                ("pcPriClassBase", ctypes.c_long), ("dwFlags", wintypes.DWORD),
                ("szExeFile", ctypes.c_wchar * 260)]


def process_map():
    """{pid: имя_exe_в_нижнем_регистре} — без сторонних библиотек."""
    try:
        k32 = ctypes.windll.kernel32
        k32.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
        k32.CreateToolhelp32Snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]
        for fn in (k32.Process32FirstW, k32.Process32NextW):
            fn.argtypes = [wintypes.HANDLE, ctypes.POINTER(PROCESSENTRY32W)]
            fn.restype = wintypes.BOOL
        k32.CloseHandle.argtypes = [wintypes.HANDLE]
        snap = k32.CreateToolhelp32Snapshot(0x2, 0)       # TH32CS_SNAPPROCESS
        if not snap or snap == ctypes.c_void_p(-1).value:
            return {}
        pm = {}
        try:
            e = PROCESSENTRY32W()
            e.dwSize = ctypes.sizeof(e)
            ok = k32.Process32FirstW(snap, ctypes.byref(e))
            while ok:
                pm[e.th32ProcessID] = e.szExeFile.lower()
                ok = k32.Process32NextW(snap, ctypes.byref(e))
        finally:
            k32.CloseHandle(snap)
        return pm
    except Exception:
        return {}


def foreground_pid():
    try:
        u32 = ctypes.windll.user32
        u32.GetForegroundWindow.restype = wintypes.HWND
        u32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
        pid = wintypes.DWORD(0)
        u32.GetWindowThreadProcessId(u32.GetForegroundWindow(), ctypes.byref(pid))
        return pid.value
    except Exception:
        return 0


def notification_state():
    """SHQueryUserNotificationState: 2 — полноэкранное приложение, 3 — полноэкранная D3D-игра,
    4 — режим презентации, 6 — «не беспокоить»; 5/7 — можно показывать уведомления."""
    try:
        st = ctypes.c_int(0)
        if ctypes.windll.shell32.SHQueryUserNotificationState(ctypes.byref(st)) == 0:
            return st.value
    except Exception:
        pass
    return 0


BUSY_STATES = {2: "полноэкранное приложение", 3: "полноэкранная игра", 4: "режим презентации", 6: "не беспокоить"}


def exe_path(pid):
    """Полный путь к exe процесса (нижний регистр) или пустая строка."""
    try:
        k32 = ctypes.windll.kernel32
        k32.OpenProcess.restype = wintypes.HANDLE
        k32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        k32.QueryFullProcessImageNameW.argtypes = [wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR,
                                                   ctypes.POINTER(wintypes.DWORD)]
        k32.CloseHandle.argtypes = [wintypes.HANDLE]
        h = k32.OpenProcess(0x1000, False, pid)            # PROCESS_QUERY_LIMITED_INFORMATION
        if not h:
            return ""
        try:
            buf, n = ctypes.create_unicode_buffer(1024), wintypes.DWORD(1024)
            return buf.value.lower() if k32.QueryFullProcessImageNameW(h, 0, buf, ctypes.byref(n)) else ""
        finally:
            k32.CloseHandle(h)
    except Exception:
        return ""


def steam_libraries():
    """Папки библиотек Steam: путь из реестра + libraryfolders.vdf (там все диски, куда ставишь игры)."""
    roots = []
    try:
        import winreg
        for hive, key in ((winreg.HKEY_CURRENT_USER, r"Software\Valve\Steam"),
                          (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\WOW6432Node\Valve\Steam")):
            try:
                with winreg.OpenKey(hive, key) as k:
                    for val in ("SteamPath", "InstallPath"):
                        try:
                            roots.append(Path(winreg.QueryValueEx(k, val)[0]))
                        except OSError:
                            pass
            except OSError:
                pass
    except ImportError:
        pass
    libs = list(roots)
    for r in roots:
        try:
            txt = (r / "steamapps" / "libraryfolders.vdf").read_text(encoding="utf-8", errors="ignore")
            libs += [Path(m.replace("\\\\", "\\")) for m in re.findall(r'"path"\s+"([^"]+)"', txt)]
        except OSError:
            pass
    seen, out = set(), []
    for lib in libs:
        key = str(lib).lower()
        if key not in seen and (lib / "steamapps" / "common").is_dir():
            seen.add(key)
            out.append(lib)
    return out


def steam_games(libs):
    """[(название, папка установки)] по appmanifest_*.acf — список установленных игр Steam."""
    games = []
    for lib in libs:
        for acf in sorted((lib / "steamapps").glob("appmanifest_*.acf")):
            try:
                txt = acf.read_text(encoding="utf-8", errors="ignore")
                name = re.search(r'"name"\s+"([^"]*)"', txt)
                inst = re.search(r'"installdir"\s+"([^"]*)"', txt)
                if name:
                    games.append((name.group(1), inst.group(1) if inst else ""))
            except OSError:
                pass
    return games


class GameWatch:
    """Игра запущена, если выполнено одно из:
      1) процесс есть в game_processes (твой список вне Steam);
      2) exe лежит в папке игр Steam (steamapps\\common, все библиотеки) или в game_dirs — это и есть список игр из Steam;
      3) полноэкранная D3D-игра (запоминаем процесс и считаем игру живой при alt-tab);
      4) fullscreen_busy: любое полноэкранное приложение;  5) respect_dnd: «не беспокоить» в Windows.
    Всё из ignore_processes (Telegram, браузеры, Wallpaper Engine…) игрой не считается."""

    def __init__(self, cfg):
        self.games = {n.lower() for n in cfg["game_processes"]}
        self.ignore = {n.lower() for n in cfg["ignore_processes"]}
        self.fullscreen_busy = bool(cfg["fullscreen_busy"])
        self.respect_dnd = bool(cfg["respect_dnd"])
        self.libs = steam_libraries()
        self.library = steam_games(self.libs)
        self.dirs = ["\\steamapps\\common\\"] + [d.lower().replace("/", "\\") for d in cfg["game_dirs"]]
        self.dirs += [str(lib / "steamapps" / "common").lower() + "\\" for lib in self.libs]
        self.latched = None
        self._paths = {}

    def describe(self):
        if self.libs:
            return (f"[games] Steam: {len(self.library)} игр в {len(self.libs)} библиотек(е/ах); "
                    f"игрой считается любой процесс из steamapps\\common")
        return "[games] Steam не найден — добавь игры в game_processes или папки в game_dirs"

    def _path_of(self, pid, name):
        key = (pid, name)
        if key not in self._paths:
            self._paths[key] = exe_path(pid)
        return self._paths[key]

    def evaluate(self, pm, state, fg_pid, path_of):
        own = os.getpid()
        for pid, name in pm.items():
            if name in self.games:
                return True, f"процесс {name}"
        for pid, name in pm.items():
            if pid == own or name in self.ignore:
                continue
            pth = path_of(pid, name)
            if pth and any(d in pth for d in self.dirs):
                return True, f"игра {name}"
        if state == 3:                                       # полноэкранная D3D-игра
            name = pm.get(fg_pid, "?")
            if fg_pid and fg_pid != own and name not in self.ignore:
                self.latched = (fg_pid, name)
            return True, f"{BUSY_STATES[3]}: {name}"
        if state in (2, 4) and self.fullscreen_busy:
            return True, f"{BUSY_STATES[state]}: {pm.get(fg_pid, '?')}"
        if state == 6 and self.respect_dnd:
            return True, BUSY_STATES[6]
        if self.latched:
            pid, name = self.latched
            if pm.get(pid) == name:
                return True, f"{name} (свёрнута)"
            self.latched = None
        return False, ""

    def check(self):
        pm = process_map()
        res = self.evaluate(pm, notification_state(), foreground_pid(), self._path_of)
        self._paths = {k: v for k, v in self._paths.items() if pm.get(k[0]) == k[1]}   # убрать закрытые процессы
        return res


class Scheduler:
    """Решает, когда показывать оверлей: раз в interval_minutes (и/или после простоя),
    но не во время игры — тогда ждёт её закрытия + game_grace_seconds."""

    def __init__(self, cfg, now):
        self.interval = max(float(cfg["interval_minutes"]), 0) * 60
        self.idle_limit = max(float(cfg["idle_minutes"]), 0) * 60
        self.grace = float(cfg["game_grace_seconds"])
        self.next_due = now + self.interval if self.interval else None
        self.pending = None
        self.free_since = now
        self.idle_fired = False
        self.waiting_logged = False
        self.paused_until = 0.0

    def pause(self, now, minutes):
        self.paused_until = now + minutes * 60

    def resume(self):
        self.paused_until = 0.0

    def is_paused(self, now):
        return now < self.paused_until

    def reset(self, now):
        self.next_due = now + self.interval if self.interval else None
        self.pending, self.waiting_logged = None, False

    def step(self, now, busy, why, idle, popup_open):
        """-> (причина показа или None, [сообщения для консоли])"""
        msgs = []
        if busy:
            self.free_since = None
        elif self.free_since is None:
            self.free_since = now
        if now < self.paused_until:              # пауза: ничего не показываем (накопившееся покажется после неё)
            return None, msgs
        if idle < 10:
            self.idle_fired = False
        elif self.idle_limit and idle >= self.idle_limit and not self.idle_fired:
            self.idle_fired = True
            self.pending = self.pending or f"простой {idle / 60:.0f} мин"
        if self.next_due is not None and now >= self.next_due:
            self.pending = self.pending or "по расписанию"
        if not self.pending or popup_open:
            return None, msgs
        if busy:
            if not self.waiting_logged:
                self.waiting_logged = True
                msgs.append(f"[schedule] пора ({self.pending}), но сейчас {why} — покажу после закрытия")
            return None, msgs
        if self.free_since is not None and now - self.free_since < self.grace:
            return None, msgs
        reason, self.pending, self.waiting_logged = self.pending, None, False
        self.next_due = now + self.interval if self.interval else None
        return reason, msgs


# ---------- bio ----------
def bar(percent, blocks):
    n = int(percent / 100 * blocks + 0.5)
    return "■" * n + "□" * (blocks - n)


def make_bio(state, blocks, style="lines"):
    parts = [
        f"mental : {bar(state['mental'], blocks)}",
        f"physical : {bar(state['physical'], blocks)}",
        f"social battery : {bar(state['social'], blocks)}",
    ]
    return " | ".join(parts) if style == "oneline" else "\n".join(parts)


def tg_client(cfg):
    from telethon import TelegramClient
    p = cfg.get("proxy")
    kw = {}
    if p and p.get("type") == "mtproto":          # ссылка tg://proxy?server=..&port=..&secret=..
        from telethon.network import ConnectionTcpMTProxyRandomizedIntermediate
        kw["connection"] = ConnectionTcpMTProxyRandomizedIntermediate
        kw["proxy"] = (p["host"], int(p["port"]), p["secret"])
    elif p:                                       # socks5 / socks4 / http
        kw["proxy"] = (p.get("type", "socks5"), p["host"], int(p["port"]))
        if cfg.get("obfuscated"):                 # для tg-ws-proxy: транспорт как у Telegram Desktop
            from telethon.network import ConnectionTcpObfuscated
            kw["connection"] = ConnectionTcpObfuscated
    return TelegramClient(str(BASE / cfg["session"]), cfg["api_id"], cfg["api_hash"], **kw)


_tg_lock = threading.Lock()


def push_bio(cfg, bio):
    if not (BASE / (cfg["session"] + ".session")).exists():
        print("[tg] нет сессии — сначала выполни: python bio_checkin.py --login")
        return
    from telethon.tl.functions.account import UpdateProfileRequest
    from telethon.tl.functions.users import GetFullUserRequest

    async def run():
        async with tg_client(cfg) as client:
            await client(UpdateProfileRequest(about=bio))
            full = await client(GetFullUserRequest("me"))
            print("[tg] сохранено у Telegram:", repr(full.full_user.about))

    with _tg_lock:
        try:
            asyncio.run(run())
            print("[tg] bio обновлено")
        except Exception as e:
            print("[tg] ошибка:", e)


def tg_login(cfg):
    async def run():
        async with tg_client(cfg) as c:
            me = await c.get_me()
            print("Залогинен как", me.first_name)

    asyncio.run(run())


# =====================================================================
#   Рендер "liquid glass" карточки (numpy -> RGB картинка)
# =====================================================================
KEY = np.array((1, 2, 3), np.float32)       # цвет прозрачности окна
KEY_HEX = "#010203"
KEY_U8 = (1, 2, 3)

ROWS = [  # key, подпись, основной цвет, светлый цвет
    ("mental", "mental health", (160, 107, 255), (190, 150, 255)),
    ("physical", "physical health", (90, 169, 255), (140, 198, 255)),
    ("social", "social life", (77, 227, 160), (130, 240, 196)),
]

C_EDGE = np.array((205, 214, 255), np.float32)    # свет на обводке
C_GLOW = np.array((115, 128, 255), np.float32)    # мягкое свечение у края внутрь
C_SPOT = np.array((140, 150, 255), np.float32)    # подсветка стекла под курсором
TRACK = np.array((52, 52, 84), np.float32)
BTN_COL = np.array((52, 56, 98), np.float32)
BTN_HOT = np.array((74, 80, 142), np.float32)
BTN_RING = np.array((140, 150, 230), np.float32)


def sdf_rrect(X, Y, cx, cy, hw, hh, r):
    qx = np.abs(X - cx) - (hw - r)
    qy = np.abs(Y - cy) - (hh - r)
    return np.hypot(np.maximum(qx, 0), np.maximum(qy, 0)) + np.minimum(np.maximum(qx, qy), 0) - r


def cov(sdf):
    return np.clip(0.5 - sdf, 0.0, 1.0)


def blend(dst, col, a):
    dst += (col - dst) * a[..., None]


class Glass:
    W_D, H_D = 700, 280
    ROW_Y = (116, 168, 220)
    TX0, TX1 = 196, 476
    BTN = (560, 140, 676, 196)
    HDR = (20, 12, 220, 72)             # карточка с логотипом в шапке

    def __init__(self, S, url="", status="now"):
        self.S = S
        self.status = status
        self.xh = False                      # курсор над крестиком
        self.W, self.H = int(round(self.W_D * S)), int(round(self.H_D * S))
        self.row_y = [v * S for v in self.ROW_Y]
        self.tx0, self.tx1 = self.TX0 * S, self.TX1 * S
        self.btn = tuple(v * S for v in self.BTN)
        self.disp = [0.0, 0.0, 0.0]       # то, что сейчас нарисовано на слайдерах (0..100)
        self.hot = [0.0, 0.0, 0.0]        # подсветка ползунка (hover/drag)
        self.bh = 0.0                     # hover кнопки
        self.bp = 0.0                     # нажатие кнопки

        H, W = self.H, self.W
        Yg, Xg = np.mgrid[0:H, 0:W]
        self.Xg = (Xg + 0.5).astype(np.float32)
        self.Yg = (Yg + 0.5).astype(np.float32)
        self.xs = (np.arange(W) + 0.5).astype(np.float32)
        self.ys = (np.arange(H) + 0.5).astype(np.float32)

        sdf = sdf_rrect(self.Xg, self.Yg, W / 2, H / 2, W / 2 - 1, H / 2 - 1, 28 * S)
        self.mask = cov(sdf)
        self.mask3 = self.mask[..., None]
        self.ring = np.clip(0.5 + 0.75 * S - np.abs(sdf + 0.75 * S), 0, 1).astype(np.float32)
        self.iglow = (np.exp(-np.clip(-sdf, 0, None) / (9 * S)) * self.mask).astype(np.float32)
        self.bg = self._build_bg()

        # карточка-логотип в шапке: наклоняется к курсору (состояние обновляет оверлей)
        self.hdr = CardGlass(S, url, mini=True)
        x0, y0, x1, y1 = (v * S for v in self.HDR)
        self.hdr_c = ((x0 + x1) / 2, (y0 + y1) / 2)
        self.hs = dict(ax=0.0, ay=0.0, h=0.0, p=0.0, flash=0.0, shine=0.0, u0=0.0,
                       quad=project_quad(self.hdr.Wc, self.hdr.Hc, *self.hdr_c, 420 * S, 0, 0, 1))

        self.close_rect = (self.W - 46 * S, 16 * S, self.W - 18 * S, 44 * S)
        self.src_quad = [(0, 0), (self.W, 0), (self.W, self.H), (0, self.H)]
        self._pct_cache = {}
        self._build_texts()

        # предрасчёты для быстрого кадра
        self._lk = None                       # ключ кэша слоя (фон + слайдеры + кнопка)
        self.layer = None
        self.spotc = (self.mask3 * C_SPOT * 0.10).astype(np.float32)
        iy, ix = np.nonzero(self.mask < 1.0)  # пиксели края/углов, которые смешиваются с прозрачным цветом
        self.edge_iy, self.edge_ix = iy, ix
        self.edge_m = self.mask[iy, ix][:, None]
        self.alpha8 = (self.mask * 255).astype(np.uint8)
        self.out_iy, self.out_ix = np.nonzero(self.mask < 0.5)      # пиксели вне формы (углы) — станут ключ-цветом
        self.KEYR = KEY + 0.5                 # +0.5 -> округление при astype(uint8)
        self.k = int(math.ceil(28 * S))       # ширина полосы у края, где считается свет границы

    def _roi(self, x0, y0, x1, y1):
        x0, y0 = max(int(x0), 0), max(int(y0), 0)
        x1, y1 = min(int(x1), self.W), min(int(y1), self.H)
        return slice(y0, y1), slice(x0, x1)

    # --- статичный фон: градиент, блик, бейдж, точки у строк ---
    def _build_bg(self):
        S = self.S
        yy = (self.Yg / self.H)[..., None]
        top = np.array((31, 31, 64), np.float32)
        bot = np.array((15, 15, 31), np.float32)
        bg = top * (1 - yy) + bot * yy
        bloom = np.exp(-(((self.Xg - 90 * S) ** 2 + (self.Yg - 5 * S) ** 2) / (2 * (270 * S) ** 2)))
        bg += np.array((60, 62, 160), np.float32) * (bloom * 0.20)[..., None]
        bg = bg.astype(np.float32)

        # цветные точки у подписей
        for cy, (_, _, col, _) in zip(self.row_y, ROWS):
            ys, xs = self._roi(0, cy - 30 * S, 80 * S, cy + 30 * S)
            X, Y = self.Xg[ys, xs], self.Yg[ys, xs]
            d = np.hypot(X - 33 * S, Y - cy) - 4.5 * S
            reg = bg[ys, xs]
            C = np.array(col, np.float32)
            reg += C * (0.55 * np.exp(-np.clip(d, 0, None) / (5 * S)))[..., None]
            blend(reg, C, cov(d))
        return bg

    # --- слайдер с мягким свечением: полоса (трек+заливка) и область ползунка ---
    def _slider(self, img, i):
        S = self.S
        _, _, col, lcol = ROWS[i]
        cy, x0, x1 = self.row_y[i], self.tx0, self.tx1
        hot = self.hot[i]
        xt = x0 + (x1 - x0) * self.disp[i] / 100.0
        C, CL = np.array(col, np.float32), np.array(lcol, np.float32)
        rt = 4 * S

        ys, xs = self._roi(x0 - 32 * S, cy - 24 * S, x1 + 32 * S, cy + 24 * S)
        reg = img[ys, xs]
        X, Y = self.Xg[ys, xs], self.Yg[ys, xs]
        dy = Y - cy
        sdf_t = np.hypot(np.maximum(np.maximum(x0 - X, X - x1), 0), dy) - rt
        blend(reg, TRACK, cov(sdf_t))
        sdf_f = np.hypot(np.maximum(np.maximum(x0 - X, X - xt), 0), dy) - rt
        reg += C * (np.exp(-np.clip(sdf_f, 0, None) / (7 * S)) * (0.10 + 0.14 * hot))[..., None]
        u = np.clip((X - x0) / max(xt - x0, 1.0), 0, 1)
        blend(reg, C * (0.70 + 0.30 * u)[..., None], cov(sdf_f))

        ys, xs = self._roi(xt - 44 * S, cy - 44 * S, xt + 44 * S, cy + 44 * S)
        reg = img[ys, xs]
        X, Y = self.Xg[ys, xs], self.Yg[ys, xs]
        dy = Y - cy
        r = (10 + 2 * hot) * S
        sdf_c = np.hypot(X - xt, dy) - r
        reg += C * (np.exp(-np.clip(sdf_c, 0, None) / (9 * S)) * (0.30 + 0.45 * hot))[..., None]
        sheen = np.exp(-(((X - xt + 3 * S) ** 2 + (dy + 3 * S) ** 2) / (2 * (4 * S) ** 2)))
        blend(reg, CL + 38 * sheen[..., None], cov(sdf_c))

    # --- кнопка apply ---
    def _button(self, img):
        S = self.S
        bx0, by0, bx1, by1 = self.btn
        pad = 22 * S
        ys, xs = self._roi(bx0 - pad, by0 - pad, bx1 + pad, by1 + pad)
        reg = img[ys, xs]
        X, Y = self.Xg[ys, xs], self.Yg[ys, xs]
        sdf = sdf_rrect(X, Y, (bx0 + bx1) / 2, (by0 + by1) / 2, (bx1 - bx0) / 2, (by1 - by0) / 2, 14 * S)
        h, p = self.bh, self.bp
        reg += np.array((90, 100, 255), np.float32) * (np.exp(-np.clip(sdf, 0, None) / (10 * S)) * (0.05 + 0.24 * h))[..., None]
        fill = (BTN_COL * (1 - h) + BTN_HOT * h) * (1 - 0.18 * p)
        blend(reg, fill, cov(sdf))
        ring = np.clip(0.5 + 0.5 * S - np.abs(sdf + 0.5 * S), 0, 1) * (0.22 + 0.45 * h)
        blend(reg, BTN_RING, ring)

    # --- тексты рисуются в кадре (а не виджетами Tk), чтобы наклон/масштаб окна их не ломал ---
    def _put(self, text, pt, col, anchor, x, y, symbol=False):
        rgb, a, dy = text_layer_m(text, pt * 96.0 / 72.0 * self.S, col, symbol)
        w = a.shape[1]
        x0 = int(round(x - 3)) if anchor == "l" else int(round(x + 3 - w)) if anchor == "r" else int(round(x - w / 2))
        return rgb, a, x0, int(round(y - dy))

    def _build_texts(self):
        S, W = self.S, self.W
        T = {"static": []}
        for cy, (_, title, _, _) in zip(self.row_y, ROWS):
            T["static"].append(self._put(title, 13, (207, 209, 238), "l", 52 * S, cy))
        T["static"].append(self._put(self.status, 11, (138, 138, 176), "r", W - 58 * S, 30 * S))
        bx0, by0, bx1, by1 = self.btn
        lab = self._put("apply", 13, (214, 217, 255), "c", 0, 0)
        arr = self._put("→", 13, (214, 217, 255), "c", 0, 0, True)
        wl, wa, gap = lab[1].shape[1], arr[1].shape[1], 8 * S
        cx, cy = (bx0 + bx1) / 2, (by0 + by1) / 2
        left = cx - (wl + gap + wa) / 2                # подпись + стрелка вместе по центру кнопки
        T["static"].append(self._put("apply", 13, (214, 217, 255), "c", left + wl / 2, cy))
        T["static"].append(self._put("→", 13, (214, 217, 255), "c", left + wl + gap + wa / 2, cy, True))
        T["x_norm"] = self._put("✕", 11, (108, 108, 148), "c", W - 32 * S, 30 * S, True)
        T["x_hot"] = self._put("✕", 11, (201, 203, 255), "c", W - 32 * S, 30 * S, True)
        self._T = T

    def _text_overlay(self, out):
        T = self._T
        for lay in T["static"]:
            comp_layer(out, *lay)
        comp_layer(out, *T["x_hot" if self.xh else "x_norm"])
        for i, cy in enumerate(self.row_y):
            key = (i, int(round(self.disp[i])))
            if key not in self._pct_cache:
                self._pct_cache[key] = self._put(f"{key[1]}%", 13, (236, 238, 251), "l", 500 * self.S, cy)
            comp_layer(out, *self._pct_cache[key])

    # --- карточка в шапке: рендер лицевой стороны, перспектива, вклейка ---
    def _header(self, out, lx, ly):
        c, hs = self.hdr, self.hs
        ccx, ccy = self.hdr_c
        face = c.render(c.Wc / 2 + lx - ccx, c.Hc / 2 + ly - ccy, hs["ax"], hs["ay"], hs["h"],
                        hs["u0"], hs["shine"], hs["flash"], rgba=True)
        xi, yi = int(round(ccx - c.WW / 2)), int(round(ccy - c.WH / 2))
        ql = [(x - xi, y - yi) for x, y in hs["quad"]]
        arr = warp_rgba(face, (c.WW, c.WH), ql, c.src_quad, BICUBIC).astype(np.float32)
        xa, ya, xb, yb = max(xi, 0), max(yi, 0), min(xi + c.WW, self.W), min(yi + c.WH, self.H)
        sub = arr[ya - yi:yb - yi, xa - xi:xb - xi]
        roi = out[ya:yb, xa:xb]
        roi += (sub[..., :3] - roi) * (sub[..., 3:4] / 255.0)

    # --- слой без света: перестраивается только когда меняются слайдеры/кнопка ---
    def _layer(self):
        key = (tuple(round(d, 2) for d in self.disp), tuple(round(h, 2) for h in self.hot),
               round(self.bh, 2), round(self.bp, 2))
        if key != self._lk:
            self._lk = key
            img = self.bg.copy()
            for i in range(3):
                self._slider(img, i)
            self._button(img)
            img += 0.5
            self.layer = img
        return self.layer

    # --- кадр целиком; (lx, ly) — источник света в координатах окна ---
    def render(self, lx, ly, boost=0.0):
        S, W, H, k = self.S, self.W, self.H, self.k
        out = self._layer().copy()
        self._header(out, lx, ly)

        s1 = 150 * S
        inv = 1.0 / (2 * s1 * s1)
        gx1 = np.exp(-((self.xs - lx) ** 2) * inv)
        gy1 = np.exp(-((self.ys - ly) ** 2) * inv)
        gx2 = np.exp(-((self.xs - (W - lx)) ** 2) * inv)      # отражённый источник -> блик на противоположной грани
        gy2 = np.exp(-((self.ys - (H - ly)) ** 2) * inv)

        # мягкая подсветка стекла под курсором
        s3 = 120 * S
        inv3 = 1.0 / (2 * s3 * s3)
        spot = np.outer(np.exp(-((self.ys - ly) ** 2) * inv3), np.exp(-((self.xs - lx) ** 2) * inv3))
        out += spot[..., None] * self.spotc

        # свет границы и свечение внутрь — только в полосе у края
        for y0, y1, x0, x1 in ((0, k, 0, W), (H - k, H, 0, W), (k, H - k, 0, k), (k, H - k, W - k, W)):
            L = np.outer(gy1[y0:y1], gx1[x0:x1]) + 0.35 * np.outer(gy2[y0:y1], gx2[x0:x1])
            e = self.ring[y0:y1, x0:x1] * ((0.22 + 0.95 * L) * (1 + 1.3 * boost) + 0.25 * boost)   # boost — окно «схвачено»
            g = self.iglow[y0:y1, x0:x1] * L * 0.34 * (1 + boost)
            out[y0:y1, x0:x1] += e[..., None] * C_EDGE + g[..., None] * C_GLOW

        self._text_overlay(out)
        np.minimum(out, 255.49, out=out)
        # RGBA: цвет валиден и за краем формы, форму задаёт альфа. Прозрачность окна (ключ-цвет) ставит оверлей:
        # так по краю нет тёмной каймы из полупрозрачных пикселей, смешанных с ключ-цветом
        return np.dstack([out.astype(np.uint8), self.alpha8])


# =====================================================================
#   Оверлей
# =====================================================================
class Spring:
    """Пружина с демпфированием: окно «желейно» догоняет цель и слегка перелетает."""

    def __init__(self, k, c):
        self.k, self.c, self.x, self.v = k, c, 0.0, 0.0

    def step(self, dt, target):
        dt = min(dt, 0.033)
        self.v += (self.k * (target - self.x) - self.c * self.v) * dt
        self.x += self.v * dt
        return self.x


def ease_out(x):
    x = min(max(x, 0.0), 1.0)
    return 1 - (1 - x) ** 3


def ease_back(x):
    x = min(max(x, 0.0), 1.0)
    c1 = 1.1
    return 1 + (c1 + 1) * (x - 1) ** 3 + c1 * (x - 1) ** 2


class Overlay:
    INTRO = 0.45
    OUTRO = 0.22
    CMAXX, CMAXY = math.radians(12), math.radians(10)     # наклон карточки в шапке

    def __init__(self, root, cfg, state, dry_run, on_close):
        from PIL import ImageTk
        self.root, self.cfg, self.dry_run, self.on_close = root, cfg, dry_run, on_close
        self.k = float(cfg["scale"])
        S = max(root.winfo_fpixels("1i") / 96.0, 1.0) * self.k
        self.S = S
        self.g = Glass(S, load_links().get("logo", ""), "preview" if dry_run else "now")
        W, H = self.g.W, self.g.H
        self.M = M = int(round(28 * S))                  # прозрачное поле вокруг карточки — место под наклон/масштаб
        self.WW, self.WH = WW, WH = W + 2 * M, H + 2 * M
        self.val = [float(state[k]) for k, *_ in ROWS]
        self.drag_i = None
        self.win_drag = None
        self.bdown = False
        self.pending = None
        self.closing = False
        self.dead = False
        self.settled = False
        self.sig = None
        self.cax = self.cay = self.ch = self.cp = self.cflash = self.cu0 = 0.0   # состояние карточки
        self.cpress = self.dragged = False
        self.card_press = None
        self.cursor = ""
        # хват и перемещение окна: пружины наклона, крена и «подъёма»
        self.grab = False
        self.vx = self.vy = 0.0
        self.sp_ax, self.sp_ay = Spring(170, 14), Spring(170, 14)
        self.sp_roll, self.sp_sc = Spring(150, 12), Spring(260, 17)
        self.gb = 0.0
        self.card = None
        self.tf = None
        self.canvas_arr = np.empty((WH, WW, 3), np.uint8)
        self.canvas_arr[:] = (1, 2, 3)
        self.coef = (1, 0, -M, 0, 1, -M, 0, 0)           # окно -> карточка (сначала просто сдвиг на поле)
        self.close_rect = self.g.close_rect
        self.dt_ms = max(1, int(1000 / max(cfg["fps"], 1)))

        l, t, r, b = work_area()
        m = cfg["margin"] * S
        pos = cfg["position"]
        if pos == "center":
            self.bx, self.by = (l + r - W) / 2, (t + b - H) / 2
        else:
            self.bx = r - W - m if "right" in pos else l + m
            self.by = b - H - m if "bottom" in pos else t + m
        self.bx -= M                                     # bx, by — левый верх окна, а не карточки
        self.by -= M
        self.pbx, self.pby = self.bx, self.by

        win = self.win = tk.Toplevel(root)
        win.withdraw()
        win.overrideredirect(True)                   # без рамки и без кнопки на панели задач
        win.attributes("-topmost", True)
        win.configure(bg=KEY_HEX)
        win.attributes("-transparentcolor", KEY_HEX)  # скруглённые углы
        win.attributes("-alpha", 0.0)
        win.geometry(f"{WW}x{WH}+{int(self.bx)}+{int(self.by + 28 * S)}")
        win.deiconify()

        self.cv = tk.Canvas(win, width=WW, height=WH, bg=KEY_HEX, highlightthickness=0, bd=0)
        self.cv.pack()
        self.photo = ImageTk.PhotoImage(Image.new("RGB", (WW, WH), (1, 2, 3)))
        self.cv.create_image(0, 0, image=self.photo, anchor="nw")
        self.cv.bind("<ButtonPress-1>", self._press)
        self.cv.bind("<B1-Motion>", self._motion)
        self.cv.bind("<ButtonRelease-1>", self._release)

        win.update_idletasks()
        px, py = win.winfo_pointerxy()
        self.light = list(self._to_card(px - win.winfo_rootx(), py - win.winfo_rooty()))
        self.t0 = self.last = time.perf_counter()
        self._tick()

    # --- мышь ---
    @staticmethod
    def _in(x, y, r):
        return r[0] <= x <= r[2] and r[1] <= y <= r[3]

    def _set(self, i, x):
        g = self.g
        v = (x - g.tx0) / (g.tx1 - g.tx0) * 100
        self.val[i] = float(round(min(max(v, 0), 100)))

    def _to_card(self, x, y):
        """Точка окна -> координаты карточки (с учётом текущего наклона/масштаба окна)."""
        a, b, c, d, e, f, g_, h = self.coef
        den = g_ * x + h * y + 1.0
        return (a * x + b * y + c) / den, (d * x + e * y + f) / den

    def _press(self, e):
        if self.closing:
            return
        S, g = self.S, self.g
        x, y = self._to_card(e.x, e.y)
        self.dragged = False
        if self._in(x, y, self.close_rect):
            self.pending = "close"
        elif self._in(x, y, g.btn):
            self.pending = "apply"
            self.bdown = True
        elif in_quad(x, y, g.hs["quad"]):
            self.card_press = (e.x_root, e.y_root)      # клик — ссылка, перетаскивание — двигает окно
            self.cpress = True
        else:
            for i in range(3):
                if abs(y - g.row_y[i]) < 20 * S and g.tx0 - 16 * S <= x <= g.tx1 + 16 * S:
                    self.drag_i = i
                    self._set(i, x)
                    return
            if y < 80 * S:
                self.win_drag = (e.x_root - self.bx, e.y_root - self.by)
                self.grab = True                          # окно «схвачено»

    def _motion(self, e):
        if self.drag_i is not None:
            self._set(self.drag_i, self._to_card(e.x, e.y)[0])
            return
        if self.card_press and not self.dragged and \
                math.hypot(e.x_root - self.card_press[0], e.y_root - self.card_press[1]) > 6 * self.S:
            self.dragged = True
            self.win_drag = (self.card_press[0] - self.bx, self.card_press[1] - self.by)
            self.grab = True
        if self.win_drag:
            self.bx, self.by = e.x_root - self.win_drag[0], e.y_root - self.win_drag[1]
            self.win.geometry(f"+{int(self.bx)}+{int(self.by)}")

    def _release(self, e):
        x, y = self._to_card(e.x, e.y)
        p, self.pending = self.pending, None
        card, self.card_press = self.card_press, None
        dragged, self.dragged = self.dragged, False
        self.bdown = self.cpress = self.grab = False
        self.drag_i = None
        self.win_drag = None
        if p == "close" and self._in(x, y, self.close_rect):
            self.close()
        elif p == "apply" and self._in(x, y, self.g.btn):
            self.apply()
        elif card and not dragged and in_quad(x, y, self.g.hs["quad"]):
            self.cflash = 1.0
            open_logo_link()

    # --- анимация и рендер ---
    def _tick(self):
        if self.dead:
            return
        now = time.perf_counter()
        dt = max(now - self.last, 1e-3)
        self.last = now
        t = now - self.t0
        S, g, win = self.S, self.g, self.win

        # появление / исчезновение: прозрачность + лёгкая "пружина" по вертикали
        if self.closing:
            kc = (now - self.tc) / self.OUTRO
            if kc >= 1:
                return self._finish()
            alpha, off = 1 - kc * kc, 14 * S * kc
            win.attributes("-alpha", alpha)
            win.geometry(f"+{int(self.bx)}+{int(self.by + off)}")
        elif not self.settled:
            kc = min(t / self.INTRO, 1.0)
            win.attributes("-alpha", ease_out(kc))
            win.geometry(f"+{int(self.bx)}+{int(self.by + (1 - ease_back(kc)) * 28 * S)}")
            if kc >= 1:
                self.settled = True
                win.geometry(f"+{int(self.bx)}+{int(self.by)}")

        # хват и перемещение: скорость окна -> пружины (наклон по ходу, крен, «подъём» при хвате)
        kv = 1 - math.exp(-dt * 18)
        self.vx += ((self.bx - self.pbx) / dt - self.vx) * kv
        self.vy += ((self.by - self.pby) / dt - self.vy) * kv
        self.pbx, self.pby = self.bx, self.by
        grab = self.grab and not self.closing
        cl = lambda v: max(-1.0, min(1.0, v))
        self.sp_ax.step(dt, cl(self.vx / 3200.0) * math.radians(9) if grab else 0.0)
        self.sp_ay.step(dt, cl(self.vy / 3200.0) * math.radians(7) if grab else 0.0)
        self.sp_roll.step(dt, cl(self.vx / 9000.0) * math.radians(3.5) if grab else 0.0)
        self.sp_sc.step(dt, 0.035 if grab else 0.0)
        self.gb += ((1.0 if grab else 0.0) - self.gb) * (1 - math.exp(-dt * 12))
        sps = (self.sp_ax, self.sp_ay, self.sp_roll, self.sp_sc)
        if not grab and all(abs(sp.x) < 2e-4 and abs(sp.v) < 2e-3 for sp in sps):
            for sp in sps:                                # всё улеглось — вернуться на быстрый путь без перспективы
                sp.x = sp.v = 0.0
        A, B, R, SC = self.sp_ax.x, -self.sp_ay.x, self.sp_roll.x, 1.0 + self.sp_sc.x
        quad = project_quad(g.W, g.H, self.WW / 2, self.WH / 2, 1100 * S, A, B, SC)
        if R:
            ox, oy, cr, sr = self.WW / 2, self.WH / 2, math.cos(R), math.sin(R)
            quad = [(ox + (x - ox) * cr - (y - oy) * sr, oy + (x - ox) * sr + (y - oy) * cr) for x, y in quad]
        self.coef = find_coeffs(quad, g.src_quad)
        self.quad_win = quad

        # курсор -> координаты карточки; источник света следует за ним с лёгким запаздыванием
        px, py = win.winfo_pointerxy()
        mx, my = self._to_card(px - win.winfo_rootx(), py - win.winfo_rooty())
        a = 1 - math.exp(-dt * 16)
        self.light[0] += (mx - self.light[0]) * a
        self.light[1] += (my - self.light[1]) * a

        # слайдеры: заполнение при появлении, дальше плавно догоняют значение
        for i in range(3):
            f = ease_out((t - 0.12 - 0.08 * i) / 0.65)
            goal = self.val[i] * f
            if f < 1:
                g.disp[i] = goal
            else:
                g.disp[i] += (goal - g.disp[i]) * (1 - math.exp(-dt * 22))
                if abs(goal - g.disp[i]) < 0.05:
                    g.disp[i] = goal
            xt = g.tx0 + (g.tx1 - g.tx0) * g.disp[i] / 100
            near = math.hypot(mx - xt, my - g.row_y[i]) < 16 * S
            target = 1.0 if (self.drag_i == i or (near and self.drag_i is None)) else 0.0
            g.hot[i] += (target - g.hot[i]) * (1 - math.exp(-dt * 14))

        inb = self._in(mx, my, g.btn)
        g.bh += ((1.0 if inb else 0.0) - g.bh) * (1 - math.exp(-dt * 14))
        g.bp += ((1.0 if (self.bdown and inb) else 0.0) - g.bp) * (1 - math.exp(-dt * 25))
        xh = self._in(mx, my, self.close_rect)
        g.xh = xh

        # карточка в шапке: hover -> поворот лицом к курсору, блик; клик (в _release) открывает ссылку
        c, hs = g.hdr, g.hs
        ccx, ccy = g.hdr_c
        inside_card = (not self.closing) and in_quad(mx, my, hs["quad"])
        if inside_card != (self.cursor == "hand2"):
            self.cursor = "hand2" if inside_card else ""
            self.cv.configure(cursor=self.cursor)
        self.ch += ((1.0 if (inside_card or self.cpress) else 0.0) - self.ch) * (1 - math.exp(-dt * 10))
        dxn = max(-1.0, min(1.0, (mx - ccx) / (c.Wc / 2)))
        dyn = max(-1.0, min(1.0, (my - ccy) / (c.Hc / 2)))
        fk = 1 - math.exp(-dt * 16)
        self.cax += (dxn * self.CMAXX * self.ch - self.cax) * fk
        self.cay += (dyn * self.CMAXY * self.ch - self.cay) * fk
        self.cp += ((1.0 if (self.cpress and not self.dragged) else 0.0) - self.cp) * (1 - math.exp(-dt * 25))
        self.cflash *= math.exp(-dt * 4.5)
        if t < 1.3:                                   # при появлении по логотипу пробегает блик
            ph = min(max((t - 0.5) / 0.8, 0.0), 1.0)
            u0, shine = -0.75 + 1.5 * ease_out(ph), math.sin(math.pi * ph) * 0.9
        else:
            self.cu0 += ((0.5 * dxn + 0.2 * dyn) * self.ch - self.cu0) * (1 - math.exp(-dt * 12))
            u0, shine = self.cu0, 0.55 * self.ch
        hs.update(ax=self.cax, ay=self.cay, h=self.ch, p=self.cp, flash=self.cflash, shine=shine, u0=u0)
        hs["quad"] = project_quad(c.Wc, c.Hc, ccx, ccy, 420 * S, self.cax, -self.cay,
                                  (1 + 0.05 * self.ch) * (1 - 0.035 * self.cp))

        sig = (round(self.light[0], 1), round(self.light[1], 1),
               tuple(round(d, 2) for d in g.disp), tuple(round(h, 2) for h in g.hot),
               round(g.bh, 2), round(g.bp, 2), xh,
               round(self.cax, 4), round(self.cay, 4), round(self.ch, 3), round(self.cp, 3),
               round(self.cflash, 3), round(u0, 3), round(shine, 3), round(self.gb, 3))
        changed = sig != self.sig
        if changed:                              # содержимое карточки перерисовываем только если оно изменилось
            self.sig = sig
            self.card = g.render(self.light[0], self.light[1], self.gb)
        tf = (round(A, 4), round(B, 4), round(R, 4), round(SC, 4))
        if changed or tf != self.tf:
            self.tf = tf
            self._present(tf == (0.0, 0.0, 0.0, 1.0))
        win.after(self.dt_ms, self._tick)

    def _present(self, identity):
        g, M = self.g, self.M
        if identity:                             # без наклона — положить карточку в центр окна
            roi = self.canvas_arr[M:M + g.H, M:M + g.W]
            roi[...] = self.card[..., :3]
            roi[g.out_iy, g.out_ix] = KEY_U8     # за границей формы (углы) — прозрачный цвет окна
            img = Image.fromarray(self.canvas_arr)
        else:                                    # хват/перемещение: наклон, крен и масштаб всего окна
            arr = warp_rgba(self.card, (self.WW, self.WH), self.quad_win, g.src_quad, BILINEAR)
            rgb = arr[..., :3].copy()
            rgb[arr[..., 3] < 128] = KEY_U8      # край по альфе: чёткий, без полупрозрачной «чёрной» каймы
            img = Image.fromarray(rgb)
        self.photo.paste(img)

    # --- действия ---
    def apply(self):
        if self.closing:
            return
        state = {k: int(round(v)) for (k, *_), v in zip(ROWS, self.val)}
        bio = make_bio(state, self.cfg["blocks"], self.cfg["bio_style"])
        print(f"[apply] {state}\n{bio}")
        if self.dry_run:
            print("[preview] в Telegram не отправлено")
        else:
            STATE_PATH.write_text(json.dumps(state), encoding="utf-8")
            if len(bio) > self.cfg["bio_limit"]:
                print(f"[apply] bio слишком длинное: {len(bio)}/{self.cfg['bio_limit']}")
            else:
                threading.Thread(target=push_bio, args=(self.cfg, bio), daemon=True).start()
        self.close()

    def close(self):
        if not self.closing:
            self.closing = True
            self.tc = time.perf_counter()

    def _finish(self):
        self.dead = True
        self.win.destroy()
        self.on_close()

    def lift(self):
        self.win.lift()


# =====================================================================
#   Карточка логотипа: наклоняется к курсору, клик открывает ссылку
# =====================================================================
DEFAULT_LINKS = {"logo": "https://github.com/"}
C_SHINE_TINT = np.array((130, 160, 255), np.float32)

_TR = getattr(Image, "Transform", Image)
_RS = getattr(Image, "Resampling", Image)
PERSPECTIVE, BICUBIC, BILINEAR, LANCZOS = _TR.PERSPECTIVE, _RS.BICUBIC, _RS.BILINEAR, _RS.LANCZOS


def load_links():
    """links.json: {"logo": "https://..."} — ссылка, которую открывает клик по логотипу."""
    if not LINKS_PATH.exists():
        LINKS_PATH.write_text(json.dumps(DEFAULT_LINKS, indent=2), encoding="utf-8")
        print(f"[links] создан {LINKS_PATH.name} — впиши свою ссылку в поле \"logo\"")
    try:
        return {**DEFAULT_LINKS, **json.loads(LINKS_PATH.read_text(encoding="utf-8"))}
    except Exception as e:
        print("[links] не удалось прочитать links.json:", e)
        return dict(DEFAULT_LINKS)


FONT = {"path": "", "fallback": [], "scale": 1.0, "fb_scale": 0.9, "gamma": 0.85}
SYSTEM_FONTS = ("segoeui.ttf", "arial.ttf", "DejaVuSans.ttf")
_PROBE, _GLYPH = {}, {}


def find_font(name):
    """Путь к файлу шрифта: как указано, рядом со скриптом / в fonts/, из ресурсов сборки, либо имя системного шрифта."""
    name = str(name or "").strip()
    if not name:
        return ""
    for cand in (Path(name), BASE / name, BASE / "fonts" / name, RES / name, RES / "fonts" / name):
        if cand.is_file():
            return str(cand)
    try:
        ImageFont.truetype(name, 12)
        return name
    except Exception:
        return ""


def apply_font_config(cfg):
    FONT["scale"] = float(cfg.get("font_scale") or 1.0)
    FONT["gamma"] = float(cfg.get("font_gamma") or 1.0)
    FONT["fb_scale"] = float(cfg.get("font_fallback_scale") or 1.0)
    want = str(cfg.get("font") or "").strip()
    FONT["path"] = find_font(want)
    if want and not FONT["path"]:
        print(f"[font] не нашёл «{want}» — кладу файл рядом со скриптом или в папку fonts/")
    fb = cfg.get("font_fallback") or []
    FONT["fallback"] = [p for p in (find_font(n) for n in ([fb] if isinstance(fb, str) else fb)) if p and p != FONT["path"]]
    if FONT["path"]:
        extra = f" (+ запасной: {', '.join(Path(p).name for p in FONT['fallback'])})" if FONT["fallback"] else ""
        print(f"[font] {Path(FONT['path']).name}{extra}")


def _probe(name):
    if name not in _PROBE:
        try:
            _PROBE[name] = ImageFont.truetype(name, 28)
        except Exception:
            _PROBE[name] = None
    return _PROBE[name]


def _gmask(font, ch):
    im = Image.new("L", (64, 64), 0)
    ImageDraw.Draw(im).text((8, 8), ch, font=font, fill=255)
    return im.tobytes()


def font_supports(name, text):
    """Есть ли в шрифте все символы строки (у отсутствующих рисуется одинаковый «квадратик» .notdef)."""
    f = _probe(name)
    if f is None:
        return False
    if (name, "\uffff") not in _GLYPH:
        _GLYPH[(name, "\uffff")] = _gmask(f, "\uffff")
    for ch in set(text):
        if ch.isspace():
            continue
        if (name, ch) not in _GLYPH:
            _GLYPH[(name, ch)] = _gmask(f, ch)
        if _GLYPH[(name, ch)] == _GLYPH[(name, "\uffff")]:
            return False
    return True


def pick_font(text, symbol=False):
    """-> (шрифт, множитель размера): основной, если умеет все символы строки; иначе запасной, иначе системный."""
    chain = [("seguisym.ttf", 1.0)] if symbol else ([(FONT["path"], 1.0)] if FONT["path"] else [])
    if not symbol:
        chain += [(p, FONT["fb_scale"]) for p in FONT["fallback"]]
    chain += [(n, 1.0) for n in SYSTEM_FONTS]
    if text:
        for name, mul in chain:
            if font_supports(name, text):
                return name, mul
    for name, mul in chain:
        if _probe(name) is not None:
            return name, mul
    return SYSTEM_FONTS[-1], 1.0


def open_font(name, size):
    try:
        return ImageFont.truetype(name, size)
    except Exception:
        try:
            return ImageFont.load_default(size=size)
        except TypeError:
            return ImageFont.load_default()


def load_font(size, symbol=False, text=None):
    return open_font(pick_font(text, symbol)[0], size)


def find_coeffs(dst, src):
    """Коэффициенты для Image.transform(PERSPECTIVE): точки dst (в кадре) -> точки src (в карточке)."""
    rows, rhs = [], []
    for (x, y), (u, v) in zip(dst, src):
        rows.append([x, y, 1, 0, 0, 0, -u * x, -u * y]); rhs.append(u)
        rows.append([0, 0, 0, x, y, 1, -v * x, -v * y]); rhs.append(v)
    return np.linalg.solve(np.array(rows, np.float64), np.array(rhs, np.float64))


def warp_rgba(face, size, quad, src_quad, resample, pad=3):
    """Перспективное преобразование RGBA-картинки без тёмной каймы по краю: источник дополняется копией
    крайних цветов (альфа за краем = 0), поэтому при интерполяции цвет не смешивается с чёрным."""
    rgb = np.pad(face[..., :3], ((pad, pad), (pad, pad), (0, 0)), mode="edge")
    a = np.pad(face[..., 3], pad, mode="constant")
    sq = [(x + pad, y + pad) for x, y in src_quad]
    im = Image.fromarray(np.dstack([rgb, a])).transform(size, PERSPECTIVE, find_coeffs(quad, sq), resample,
                                                         fillcolor=(0, 0, 0, 0))
    return np.asarray(im)


def project_quad(wc, hc, cx, cy, dist, a, b, s):
    """Углы карточки после поворота: a — вокруг вертикальной оси, b — вокруг горизонтальной, s — масштаб."""
    ca, sa, cb, sb = math.cos(a), math.sin(a), math.cos(b), math.sin(b)
    pts = []
    for x, y in ((-wc / 2, -hc / 2), (wc / 2, -hc / 2), (wc / 2, hc / 2), (-wc / 2, hc / 2)):
        x1 = x * ca
        y1 = y * cb + x * sa * sb
        z1 = y * sb - x * sa * cb          # z > 0 — ближе к зрителю
        f = dist / (dist - z1)
        pts.append((cx + s * x1 * f, cy + s * y1 * f))
    return pts


def in_quad(px, py, q):
    sg = []
    for i in range(4):
        x1, y1 = q[i]
        x2, y2 = q[(i + 1) % 4]
        sg.append((x2 - x1) * (py - y1) - (y2 - y1) * (px - x1))
    return all(v >= 0 for v in sg) or all(v <= 0 for v in sg)


def text_layer_m(text, px, color, symbol=False):
    """Текст для интерфейса: рисуется в 3x и сжимается (мягкое сглаживание без «лесенки»), высота слоя —
    по метрикам шрифта (базовая линия одинакова у всех строк).
    -> (rgb, альфа, смещение от верха слоя до вертикального центра строки)"""
    SS = 3
    name, mul = pick_font(text, symbol)
    px = px * (1.0 if symbol else FONT["scale"]) * mul
    font = open_font(name, max(8, int(round(px * SS))))
    try:
        asc, desc = font.getmetrics()
        tw = font.getlength(text)
    except Exception:                                  # совсем запасной растровый шрифт
        asc, desc = int(px * SS * 0.9), int(px * SS * 0.25)
        tw = ImageDraw.Draw(Image.new("L", (4, 4))).textlength(text, font=font)
    w, h = int(math.ceil(tw / SS)) + 6, int(math.ceil((asc + desc) / SS)) + 6
    big = Image.new("L", (w * SS, h * SS), 0)
    ImageDraw.Draw(big).text((3 * SS, 3 * SS), text, font=font, fill=255)
    a = np.asarray(big.resize((w, h), getattr(Image, "Resampling", Image).BOX), np.float32) / 255.0
    if not symbol:
        a = a ** FONT["gamma"]                         # плотность: светлый текст на тёмном выглядит тоньше, чем есть
    rgb = np.empty((h, w, 3), np.float32)
    rgb[:] = color
    return rgb, a, 3 + (asc + desc) / SS / 2


def comp_layer(out, rgb, a, x0, y0):
    """Вклеить RGB-слой с альфой в кадр out (float32) в позицию (x0, y0), с обрезкой по краям."""
    H, W = out.shape[:2]
    h, w = a.shape
    xa, ya, xb, yb = max(x0, 0), max(y0, 0), min(x0 + w, W), min(y0 + h, H)
    if xb <= xa or yb <= ya:
        return
    sl = (slice(ya - y0, yb - y0), slice(xa - x0, xb - x0))
    roi = out[ya:yb, xa:xb]
    roi += (rgb[sl] - roi) * a[sl][..., None]


def text_layer(text, px, color):
    """Текст -> (rgb, альфа) для слоя карточки."""
    font = load_font(max(8, int(round(px))), text=text)
    bb = ImageDraw.Draw(Image.new("RGBA", (4, 4))).textbbox((0, 0), text, font=font)
    tw, th = bb[2] - bb[0], bb[3] - bb[1]
    img = Image.new("RGBA", (tw + 6, th + 6), tuple(color) + (0,))
    ImageDraw.Draw(img).text((3 - bb[0], 3 - bb[1]), text, font=font, fill=tuple(color) + (255,))
    arr = np.asarray(img, np.float32)
    return arr[..., :3].copy(), arr[..., 3] / 255.0


def load_logo_rgba():
    for cand in (LOGO_PATH, RES / LOGO_PATH.name):                        # сначала рядом с exe, потом вшитый
        try:
            return Image.open(cand).convert("RGBA")
        except Exception:
            pass
    else:
        print(f"[logo] не найден {LOGO_PATH.name} — рисую обычным текстом")
        tmp = Image.new("RGBA", (1400, 300), (240, 240, 252, 0))
        ImageDraw.Draw(tmp).text((20, 20), "mentalizer", font=load_font(200), fill=(240, 240, 252, 255))
        return tmp.crop(tmp.getbbox() or (0, 0, 10, 10))


def open_logo_link():
    url = load_links().get("logo", "").strip()
    if not url:
        print("[logo] в links.json пустая ссылка (поле \"logo\")")
        return
    if url == DEFAULT_LINKS["logo"]:
        print("[logo] ссылка ещё не настроена — впиши свою в links.json (поле \"logo\")")
    print(f"[logo] открываю {url}")
    webbrowser.open(url)


class CardGlass:
    """Лицевая сторона карточки (плоская): стекло + слои логотипа с параллаксом + свет от курсора.
    mini=True — компактная карточка для шапки оверлея (выдаёт RGBA, вклеивается в оверлей)."""

    def __init__(self, S, url, mini=False):
        self.S, self.mini = S, mini
        cw, ch, mx, my, rad = (200, 60, 18, 10, 16) if mini else (640, 300, 70, 70, 30)
        self.Wc, self.Hc = int(round(cw * S)), int(round(ch * S))
        self.mx, self.my = int(round(mx * S)), int(round(my * S))
        self.WW, self.WH = self.Wc + 2 * self.mx, self.Hc + 2 * self.my      # область с запасом под наклон
        self.lk = 0.5 if mini else 1.0                                      # радиус света ~ размеру карточки
        W, H = self.Wc, self.Hc
        Yg, Xg = np.mgrid[0:H, 0:W]
        self.Xg = (Xg + 0.5).astype(np.float32)
        self.Yg = (Yg + 0.5).astype(np.float32)
        self.xs = (np.arange(W) + 0.5).astype(np.float32)
        self.ys = (np.arange(H) + 0.5).astype(np.float32)

        sdf = sdf_rrect(self.Xg, self.Yg, W / 2, H / 2, W / 2 - 1, H / 2 - 1, rad * S)
        self.mask = cov(sdf)
        self.ring = np.clip(0.5 + 0.75 * S - np.abs(sdf + 0.75 * S), 0, 1).astype(np.float32)
        self.iglow = (np.exp(-np.clip(-sdf, 0, None) / ((6 if mini else 9) * S)) * self.mask).astype(np.float32)
        self.spotc = (self.mask[..., None] * C_SPOT * 0.10).astype(np.float32)
        iy, ix = np.nonzero(self.mask < 1.0)
        self.edge_iy, self.edge_ix = iy, ix
        self.edge_m = self.mask[iy, ix][:, None]
        self.KEYR = KEY + 0.5
        self.k = int(math.ceil((18 if mini else 28) * S))
        self.u_full = (((self.Xg - W / 2) * 0.906 + (self.Yg - H / 2) * 0.423) / W).astype(np.float32)
        self.src_quad = [(0, 0), (W, 0), (W, H), (0, H)]
        self.bg = self._build_bg()
        self.layers = self._build_layers(url)

    def _build_bg(self):
        S, W, H, mini = self.S, self.Wc, self.Hc, self.mini
        yy = (self.Yg / H)[..., None]
        top, bot = ((50, 50, 106), (30, 30, 68)) if mini else ((31, 31, 64), (15, 15, 31))
        bg = np.array(top, np.float32) * (1 - yy) + np.array(bot, np.float32) * yy
        ks = 0.4 if mini else 1.0
        c2x, c2y = (0.50, 0.50) if mini else (0.64, 0.44)
        b1 = np.exp(-(((self.Xg - 0.12 * W) ** 2 + (self.Yg + 0.02 * H) ** 2) / (2 * (280 * S * ks) ** 2)))
        b2 = np.exp(-(((self.Xg - c2x * W) ** 2 + (self.Yg - c2y * H) ** 2) / (2 * (210 * S * ks) ** 2)))
        bg += np.array((60, 62, 160), np.float32) * (b1 * 0.20)[..., None]
        bg += np.array((70, 70, 200), np.float32) * (b2 * (0.20 if mini else 0.13))[..., None]
        return (bg + 0.5).astype(np.float32)

    def _build_layers(self, url):
        S, W, H = self.S, self.Wc, self.Hc
        logo = load_logo_rgba()
        if self.mini:
            lh = max(1, int(round(34 * S)))
            lw = max(1, int(round(lh * logo.width / logo.height)))
        else:
            lw = int(W * 0.80)
            lh = max(1, int(round(lw * logo.height / logo.width)))
        arr = np.asarray(logo.resize((lw, lh), LANCZOS), np.float32)
        lrgb, la = arr[..., :3].copy(), arr[..., 3] / 255.0
        # слой: (rgb, альфа, x, y, "высота" над стеклом в px для параллакса, тип, усиление без hover, прирост при hover)
        if self.mini:                       # в шапке — только логотип по центру карточки
            layers = [(lrgb, la, int((W - lw) / 2), int((H - lh) / 2), 22, "logo", 1.0, 0.0)]
        else:
            layers = [(lrgb, la, int((W - lw) / 2), int(H * 0.43 - lh / 2), 30, "logo", 1.0, 0.0)]
            text = url.split("://", 1)[-1].rstrip("/")
            text = (text[:42] + "…" if len(text) > 43 else text) + "  →"
            rgb, a = text_layer(text, 13 * S, (170, 176, 232))
            layers.append((rgb, a, int((W - a.shape[1]) / 2), int(H * 0.83 - a.shape[0] / 2), 12, "hint", 0.55, 0.45))
        return layers

    # lx, ly — источник света в координатах карточки; ax, ay — наклон (рад); h — hover 0..1;
    # u0 — положение блика-диагонали; shine — его сила; flash — вспышка при клике
    def render(self, lx, ly, ax, ay, h, u0, shine, flash, rgba=False):
        S, W, H, k = self.S, self.Wc, self.Hc, self.k
        out = self.bg.copy()
        band = np.exp(-((self.u_full - u0) ** 2) * (1.0 / (2 * 0.06 ** 2)))

        for rgb, a, bx, by, hgt, kind, g0, g1 in self.layers:
            x0 = bx + int(round(hgt * S * math.sin(ax)))
            y0 = by + int(round(hgt * S * math.sin(ay)))
            hh, ww = a.shape
            xa, ya, xb, yb = max(x0, 0), max(y0, 0), min(x0 + ww, W), min(y0 + hh, H)
            if xb <= xa or yb <= ya:
                continue
            sl = (slice(ya - y0, yb - y0), slice(xa - x0, xb - x0))
            rgb_s, a_s = rgb[sl], a[sl]
            roi = out[ya:yb, xa:xb]
            roi += (rgb_s - roi) * (a_s * (g0 + g1 * h))[..., None]
            if kind == "logo":
                roi += rgb_s * (a_s * (0.12 * h))[..., None]
                if shine > 0.001:
                    bs = band[ya:yb, xa:xb] * a_s * shine
                    roi += (C_SHINE_TINT - roi) * (bs * 0.55)[..., None]
                    roi += (bs * 60)[..., None]

        if shine > 0.001:
            out += (band * self.mask * (0.05 * shine))[..., None] * C_SPOT

        s3 = 120 * S * self.lk
        inv3 = 1.0 / (2 * s3 * s3)
        out += np.outer(np.exp(-((self.ys - ly) ** 2) * inv3), np.exp(-((self.xs - lx) ** 2) * inv3))[..., None] * self.spotc

        s1 = 150 * S * self.lk
        inv = 1.0 / (2 * s1 * s1)
        gx1, gy1 = np.exp(-((self.xs - lx) ** 2) * inv), np.exp(-((self.ys - ly) ** 2) * inv)
        gx2, gy2 = np.exp(-((self.xs - (W - lx)) ** 2) * inv), np.exp(-((self.ys - (H - ly)) ** 2) * inv)
        fb = 1.0 + 2.5 * flash
        for y0, y1, x0, x1 in ((0, k, 0, W), (H - k, H, 0, W), (k, H - k, 0, k), (k, H - k, W - k, W)):
            L = np.outer(gy1[y0:y1], gx1[x0:x1]) + 0.35 * np.outer(gy2[y0:y1], gx2[x0:x1])
            e = self.ring[y0:y1, x0:x1] * (0.22 + 0.95 * L) * fb
            g = self.iglow[y0:y1, x0:x1] * L * 0.34 * fb
            out[y0:y1, x0:x1] += e[..., None] * C_EDGE + g[..., None] * C_GLOW

        np.minimum(out, 255.49, out=out)
        if rgba:                                  # для вклейки в оверлей: углы — через альфу
            return np.dstack([out.astype(np.uint8), (self.mask * 255).astype(np.uint8)])
        iy, ix = self.edge_iy, self.edge_ix
        out[iy, ix] = out[iy, ix] * self.edge_m + self.KEYR * (1 - self.edge_m)   # скруглённые углы -> прозрачный цвет
        return out.astype(np.uint8)


class LogoOverlay:
    INTRO, OUTRO = 1.05, 0.25
    MAXX, MAXY = math.radians(15), math.radians(11)

    def __init__(self, root, cfg, on_close):
        from PIL import ImageTk
        self.root, self.cfg, self.on_close = root, cfg, on_close
        S = max(root.winfo_fpixels("1i") / 96.0, 1.0) * float(cfg["scale"])
        self.S = S
        self.g = g = CardGlass(S, load_links().get("logo", ""))
        self.cx, self.cy = g.WW / 2, g.WH / 2
        self.dist = 800 * S
        self.dt_ms = max(1, int(1000 / max(cfg["fps"], 1)))

        l, t, r, b = work_area()
        self.bx, self.by = (l + r - g.WW) / 2, (t + b - g.WH) / 2

        win = self.win = tk.Toplevel(root)
        win.withdraw()
        win.overrideredirect(True)
        win.attributes("-topmost", True)
        win.configure(bg=KEY_HEX)
        win.attributes("-transparentcolor", KEY_HEX)
        win.attributes("-alpha", 0.0)
        win.geometry(f"{g.WW}x{g.WH}+{int(self.bx)}+{int(self.by)}")
        win.deiconify()
        self.cv = tk.Canvas(win, width=g.WW, height=g.WH, bg=KEY_HEX, highlightthickness=0, bd=0)
        self.cv.pack()
        self.photo = ImageTk.PhotoImage(Image.new("RGB", (g.WW, g.WH), (1, 2, 3)))
        self.cv.create_image(0, 0, image=self.photo, anchor="nw")
        self.cv.bind("<ButtonPress-1>", self._press)
        self.cv.bind("<B1-Motion>", self._motion)
        self.cv.bind("<ButtonRelease-1>", self._release)
        self.cv.bind("<Button-3>", lambda e: self.close())

        self.ax = self.ay = 0.0          # текущий наклон
        self.h = self.p = self.flash = 0.0
        self.u0 = 0.0
        self.pressed = self.dragged = False
        self.closing = self.dead = False
        self.cursor = ""
        self.alpha = -1.0
        self.sig, self.sig_t, self.sharp = None, 0.0, True
        self.quad = project_quad(g.Wc, g.Hc, self.cx, self.cy, self.dist, 0, 0, 1)
        win.update_idletasks()
        px, py = win.winfo_pointerxy()
        self.light = [px - win.winfo_rootx(), py - win.winfo_rooty()]
        self.t0 = self.last = time.perf_counter()
        self._tick()

    # --- мышь: клик открывает ссылку, перетаскивание двигает окно, правый клик закрывает ---
    def _press(self, e):
        if self.closing:
            return
        self.pressed, self.dragged = True, False
        self.press_pos, self.press_base = (e.x_root, e.y_root), (self.bx, self.by)

    def _motion(self, e):
        if not self.pressed:
            return
        dx, dy = e.x_root - self.press_pos[0], e.y_root - self.press_pos[1]
        if not self.dragged and math.hypot(dx, dy) > 6 * self.S:
            self.dragged = True
        if self.dragged:
            self.bx, self.by = self.press_base[0] + dx, self.press_base[1] + dy
            self.win.geometry(f"+{int(self.bx)}+{int(self.by)}")

    def _release(self, e):
        was, self.pressed = self.pressed, False
        if was and not self.dragged and not self.closing and in_quad(e.x, e.y, self.quad):
            self._open()

    def _open(self):
        self.flash = 1.0
        open_logo_link()

    # --- анимация ---
    def _tick(self):
        if self.dead:
            return
        now = time.perf_counter()
        dt = max(now - self.last, 1e-3)
        self.last = now
        t = now - self.t0
        S, g, win = self.S, self.g, self.win

        if self.closing:
            kc = (now - self.tc) / self.OUTRO
            if kc >= 1:
                return self._finish()
            alpha, b_extra, s_extra = 1 - kc * kc, -0.35 * kc, 1 - 0.06 * kc
            ki = 1.0
        else:
            ki = min(t / self.INTRO, 1.0)
            alpha, b_extra, s_extra = ease_out(t / 0.45), 0.0, 1.0
        if abs(alpha - self.alpha) > 1e-3:
            self.alpha = alpha
            win.attributes("-alpha", min(max(alpha, 0.0), 1.0))

        px, py = win.winfo_pointerxy()
        mx, my = px - win.winfo_rootx(), py - win.winfo_rooty()
        inside = (not self.closing) and in_quad(mx, my, self.quad)
        if inside != (self.cursor == "hand2"):
            self.cursor = "hand2" if inside else ""
            self.cv.configure(cursor=self.cursor)

        # hover -> карточка поворачивается лицом к курсору и приподнимается
        self.h += ((1.0 if (inside or self.pressed) else 0.0) - self.h) * (1 - math.exp(-dt * 10))
        dxn = max(-1.0, min(1.0, (mx - self.cx) / (g.Wc / 2)))
        dyn = max(-1.0, min(1.0, (my - self.cy) / (g.Hc / 2)))
        f = 1 - math.exp(-dt * 16)
        self.ax += (dxn * self.MAXX * self.h - self.ax) * f
        self.ay += (dyn * self.MAXY * self.h - self.ay) * f
        self.p += ((1.0 if (self.pressed and not self.dragged) else 0.0) - self.p) * (1 - math.exp(-dt * 25))
        self.flash *= math.exp(-dt * 4.5)

        # свет следует за курсором с лёгким запаздыванием
        a = 1 - math.exp(-dt * 16)
        self.light[0] += (mx - self.light[0]) * a
        self.light[1] += (my - self.light[1]) * a
        lx, ly = g.Wc / 2 + self.light[0] - self.cx, g.Hc / 2 + self.light[1] - self.cy

        # появление: карточка "падает" на место с пружиной, по логотипу пробегает блик
        b_intro = (1 - ease_back(ki)) * 0.55
        s_intro = 0.86 + 0.14 * ease_back(ki)
        if t < 1.25:
            ph = min(max((t - 0.35) / 0.9, 0.0), 1.0)
            u0, shine = -0.75 + 1.5 * ease_out(ph), math.sin(math.pi * ph) * 0.9
        else:
            self.u0 += ((0.5 * dxn + 0.2 * dyn) * self.h - self.u0) * (1 - math.exp(-dt * 12))
            u0, shine = self.u0, 0.55 * self.h

        A, B = self.ax, -self.ay + b_intro + b_extra
        s = s_intro * s_extra * (1 + 0.05 * self.h) * (1 - 0.035 * self.p)
        self.quad = project_quad(g.Wc, g.Hc, self.cx, self.cy, self.dist, A, B, s)

        sig = (round(A, 4), round(B, 4), round(s, 4), round(lx), round(ly), round(self.h, 3),
               round(u0, 3), round(shine, 3), round(self.flash, 3))
        self.pose = (lx, ly, u0, shine)
        if sig != self.sig:                       # в движении — быстрый билинейный кадр
            self.sig, self.sig_t, self.sharp = sig, now, False
            self._draw(BILINEAR)
        elif not self.sharp and now - self.sig_t > 0.15:   # остановилась — один раз перерисовать чётко
            self.sharp = True
            self._draw(BICUBIC)
        win.after(self.dt_ms, self._tick)

    def _draw(self, resample):
        g = self.g
        lx, ly, u0, shine = self.pose
        card = g.render(lx, ly, self.ax, self.ay, self.h, u0, shine, self.flash, rgba=True)
        arr = warp_rgba(card, (g.WW, g.WH), self.quad, g.src_quad, resample)
        rgb = arr[..., :3].copy()
        rgb[arr[..., 3] < 128] = KEY_U8
        self.photo.paste(Image.fromarray(rgb))

    def close(self):
        if not self.closing:
            self.closing = True
            self.tc = time.perf_counter()

    def _finish(self):
        self.dead = True
        self.win.destroy()
        self.on_close()

    def lift(self):
        self.win.lift()


# =====================================================================
#   Трей, главное меню, автозапуск, лог
# =====================================================================
def make_icon(size=64):
    """Иконка приложения (светящаяся точка «i» на тёмном стекле) — для трея; тот же рисунок, что в mentalizer.ico."""
    N = 512
    yy, xx = np.mgrid[0:N, 0:N].astype(np.float32)
    t = (yy / N)[..., None]
    img = np.array((52, 52, 108), np.float32) * (1 - t) + np.array((16, 16, 36), np.float32) * t
    img += np.array((70, 74, 190), np.float32) * (np.exp(-(((xx - 90) ** 2 + (yy - 20) ** 2) / (2 * 230 ** 2))) * 0.30)[..., None]
    d = sdf_rrect(xx, yy, N / 2, N / 2, N / 2 - 6, N / 2 - 6, 110)
    ring = np.clip(2.2 - np.abs(d + 3.2), 0, 1) * (0.25 + 0.55 * np.exp(-((xx - 120) ** 2 + (yy - 40) ** 2) / (2 * 200 ** 2)))
    img += ring[..., None] * np.array((205, 214, 255), np.float32)
    cx, cy, r = 256, 196, 62
    dist = np.hypot(xx - cx, yy - cy)
    k = np.clip((yy - (cy - r)) / (2 * r), 0, 1)[..., None]
    dot = np.array((125, 205, 255), np.float32) * (1 - k) + np.array((80, 150, 255), np.float32) * k
    img += np.array((90, 150, 255), np.float32) * (np.exp(-np.clip(dist - r, 0, None) / 46) * 0.55)[..., None]
    a = np.clip(0.5 - (dist - r), 0, 1)[..., None]
    img = img * (1 - a) + dot * a
    sd = np.maximum(np.maximum(214 - xx, xx - 298), np.maximum(292 - yy, yy - 424))
    sa = np.clip(0.5 - sd, 0, 1)[..., None]
    kk = np.clip((yy - 292) / 132, 0, 1)[..., None]
    img = img * (1 - sa) + (np.array((170, 190, 255), np.float32) * (1 - kk) + np.array((110, 120, 255), np.float32) * kk) * sa
    im = Image.fromarray(np.dstack([np.clip(img, 0, 255), np.clip(0.5 - d, 0, 1) * 255]).astype(np.uint8))
    return im.resize((size, size), LANCZOS)


class Tee:
    """Пишет вывод и в консоль, и в файл — чтобы при скрытой консоли ничего не терялось."""

    def __init__(self, *streams):
        self.streams = [s for s in streams if s]

    def write(self, s):
        for st in self.streams:
            try:
                st.write(s)
                st.flush()
            except Exception:
                pass
        return len(s)

    def flush(self):
        pass

    def isatty(self):
        return False


def setup_logging(cfg):
    if not cfg.get("log_file"):
        return
    try:
        path = BASE / "mentalizer.log"
        if path.exists() and path.stat().st_size > 300_000:
            path.write_text("", encoding="utf-8")
        f = open(path, "a", encoding="utf-8", buffering=1)
        f.write(f"\n--- {time.strftime('%Y-%m-%d %H:%M:%S')} ---\n")
        sys.stdout = Tee(sys.__stdout__, f)
        sys.stderr = Tee(sys.__stderr__, f)
    except Exception:
        pass


def hide_console():
    try:
        hwnd = ctypes.windll.kernel32.GetConsoleWindow()
        if hwnd:
            ctypes.windll.user32.ShowWindow(hwnd, 0)
    except Exception:
        pass


RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"


def autostart_command():
    if FROZEN:
        return f'"{sys.executable}" --working'
    exe = Path(sys.executable)
    pyw = exe.with_name("pythonw.exe")
    return f'"{pyw if pyw.exists() else exe}" "{Path(__file__).resolve()}" --working'


def autostart_get():
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as k:
            winreg.QueryValueEx(k, "mentalizer")
            return True
    except Exception:
        return False


def autostart_set(on):
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE) as k:
            if on:
                winreg.SetValueEx(k, "mentalizer", 0, winreg.REG_SZ, autostart_command())
            else:
                winreg.DeleteValue(k, "mentalizer")
        return True
    except Exception as e:
        print("[autostart] не удалось изменить:", e)
        return False


def save_config_value(key, value):
    try:
        data = json.loads(CONFIG_PATH.read_text(encoding="utf-8")) if CONFIG_PATH.exists() else {}
        data[key] = value
        CONFIG_PATH.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
    except Exception as e:
        print("[config] не удалось сохранить:", e)


def apply_lights(g, out, lx, ly, boost=0.0):
    """Подсветка стекла под курсором и свет по обводке (тот же приём, что у оверлея)."""
    S, W, H, k = g.S, g.W, g.H, g.k
    s3 = 120 * S
    inv3 = 1.0 / (2 * s3 * s3)
    out += np.outer(np.exp(-((g.ys - ly) ** 2) * inv3), np.exp(-((g.xs - lx) ** 2) * inv3))[..., None] * g.spotc
    s1 = 150 * S
    inv = 1.0 / (2 * s1 * s1)
    gx1, gy1 = np.exp(-((g.xs - lx) ** 2) * inv), np.exp(-((g.ys - ly) ** 2) * inv)
    gx2, gy2 = np.exp(-((g.xs - (W - lx)) ** 2) * inv), np.exp(-((g.ys - (H - ly)) ** 2) * inv)
    for y0, y1, x0, x1 in ((0, k, 0, W), (H - k, H, 0, W), (k, H - k, 0, k), (k, H - k, W - k, W)):
        L = np.outer(gy1[y0:y1], gx1[x0:x1]) + 0.35 * np.outer(gy2[y0:y1], gx2[x0:x1])
        e = g.ring[y0:y1, x0:x1] * ((0.22 + 0.95 * L) * (1 + 1.3 * boost) + 0.25 * boost)
        gl = g.iglow[y0:y1, x0:x1] * L * 0.34 * (1 + boost)
        out[y0:y1, x0:x1] += e[..., None] * C_EDGE + gl[..., None] * C_GLOW


MENU_BTNS = [("check", "Проверить сейчас", 0, 0), ("preview", "Предпросмотр", 1, 0), ("pause", "Пауза на час", 2, 0),
             ("logo", "Логотип", 0, 1), ("config", "config.json", 1, 1), ("quit", "Выйти", 2, 1)]
BTN_QUIT_HOT = np.array((120, 64, 100), np.float32)


class MenuGlass:
    """Рендер главного меню: шапка-логотип, статус, кнопки, настройки."""
    W_D, H_D = 600, 440
    HDR = (20, 12, 220, 72)
    PANEL = (20, 88, 580, 202)
    _put = Glass._put
    _header = Glass._header
    _roi = Glass._roi

    def __init__(self, S, url):
        self.S = S
        self.W, self.H = int(round(self.W_D * S)), int(round(self.H_D * S))
        W, H = self.W, self.H
        Yg, Xg = np.mgrid[0:H, 0:W]
        self.Xg = (Xg + 0.5).astype(np.float32)
        self.Yg = (Yg + 0.5).astype(np.float32)
        self.xs = (np.arange(W) + 0.5).astype(np.float32)
        self.ys = (np.arange(H) + 0.5).astype(np.float32)
        sdf = sdf_rrect(self.Xg, self.Yg, W / 2, H / 2, W / 2 - 1, H / 2 - 1, 28 * S)
        self.mask = cov(sdf)
        self.ring = np.clip(0.5 + 0.75 * S - np.abs(sdf + 0.75 * S), 0, 1).astype(np.float32)
        self.iglow = (np.exp(-np.clip(-sdf, 0, None) / (9 * S)) * self.mask).astype(np.float32)
        self.spotc = (self.mask[..., None] * C_SPOT * 0.10).astype(np.float32)
        self.alpha8 = (self.mask * 255).astype(np.uint8)
        self.out_iy, self.out_ix = np.nonzero(self.mask < 0.5)
        self.k = int(math.ceil(28 * S))
        self.src_quad = [(0, 0), (W, 0), (W, H), (0, H)]

        self.hdr = CardGlass(S, url, mini=True)
        x0, y0, x1, y1 = (v * S for v in self.HDR)
        self.hdr_c = ((x0 + x1) / 2, (y0 + y1) / 2)
        self.hs = dict(ax=0.0, ay=0.0, h=0.0, p=0.0, flash=0.0, shine=0.0, u0=0.0,
                       quad=project_quad(self.hdr.Wc, self.hdr.Hc, *self.hdr_c, 420 * S, 0, 0, 1))

        self.rect = {}
        for bid, _, c, r in MENU_BTNS:
            self.rect[bid] = ((20 + 190 * c) * S, (216 + 60 * r) * S, (200 + 190 * c) * S, (266 + 60 * r) * S)
        self.rect["minus"] = (430 * S, 337 * S, 460 * S, 367 * S)
        self.rect["plus"] = (550 * S, 337 * S, 580 * S, 367 * S)
        self.rect["auto"] = (520 * S, 382 * S, 580 * S, 406 * S)
        self.rect["close"] = (W - 46 * S, 16 * S, W - 18 * S, 44 * S)
        self.hv = {k: 0.0 for k in self.rect}          # hover каждой кнопки (плавно 0..1)
        self.bi = {k: 0.0 for k in self.rect}          # появление при открытии
        self.press, self.pv, self.tog = None, 0.0, 0.0
        self.lines = ["", "", "", ""]
        self.pause_label, self.interval = "Пауза на час", 60
        self.bg = self._build_bg()
        self._tc = {}

    def _build_bg(self):
        S, W, H = self.S, self.W, self.H
        yy = (self.Yg / H)[..., None]
        bg = np.array((31, 31, 64), np.float32) * (1 - yy) + np.array((15, 15, 31), np.float32) * yy
        bloom = np.exp(-(((self.Xg - 0.12 * W) ** 2 + (self.Yg + 0.02 * H) ** 2) / (2 * (280 * S) ** 2)))
        bg += np.array((60, 62, 160), np.float32) * (bloom * 0.20)[..., None]
        px0, py0, px1, py1 = (v * S for v in self.PANEL)       # панель статуса — чуть темнее стекла
        ys, xs = self._roi(px0 - 2, py0 - 2, px1 + 2, py1 + 2)
        X, Y = self.Xg[ys, xs], self.Yg[ys, xs]
        sdf = sdf_rrect(X, Y, (px0 + px1) / 2, (py0 + py1) / 2, (px1 - px0) / 2, (py1 - py0) / 2, 16 * S)
        reg = bg[ys, xs]
        blend(reg, np.array((10, 10, 24), np.float32), cov(sdf) * 0.6)
        blend(reg, np.array((255, 255, 255), np.float32), np.clip(0.5 + 0.5 * S - np.abs(sdf + 0.5 * S), 0, 1) * 0.07)
        ly = int(330 * S)                                      # тонкая линия над настройками
        bg[ly:ly + max(1, int(S)), int(20 * S):int(580 * S)] += 9
        return (bg + 0.5).astype(np.float32)

    def _widgets(self, out):
        S = self.S
        for bid, rc in self.rect.items():
            if bid in ("close", "auto"):
                continue
            x0, y0, x1, y1 = rc
            big = bid not in ("minus", "plus")
            pad = 22 * S
            ys, xs = self._roi(x0 - pad, y0 - pad, x1 + pad, y1 + pad)
            reg = out[ys, xs]
            X, Y = self.Xg[ys, xs], self.Yg[ys, xs]
            sdf = sdf_rrect(X, Y, (x0 + x1) / 2, (y0 + y1) / 2, (x1 - x0) / 2, (y1 - y0) / 2, (14 if big else 9) * S)
            h, ia = self.hv[bid], self.bi[bid]
            p = self.pv if self.press == bid else 0.0
            hot = BTN_QUIT_HOT if bid == "quit" else BTN_HOT
            reg += np.array((90, 100, 255), np.float32) * (np.exp(-np.clip(sdf, 0, None) / (10 * S)) * (0.05 + 0.24 * h) * ia)[..., None]
            blend(reg, (BTN_COL * (1 - h) + hot * h) * (1 - 0.18 * p), cov(sdf) * ia)
            blend(reg, BTN_RING, np.clip(0.5 + 0.5 * S - np.abs(sdf + 0.5 * S), 0, 1) * (0.22 + 0.45 * h) * ia)
        # переключатель «автозапуск»
        x0, y0, x1, y1 = self.rect["auto"]
        ia, tg, h = self.bi["auto"], self.tog, self.hv["auto"]
        pad = 20 * S
        ys, xs = self._roi(x0 - pad, y0 - pad, x1 + pad, y1 + pad)
        reg = out[ys, xs]
        X, Y = self.Xg[ys, xs], self.Yg[ys, xs]
        sdf = sdf_rrect(X, Y, (x0 + x1) / 2, (y0 + y1) / 2, (x1 - x0) / 2, (y1 - y0) / 2, (y1 - y0) / 2)
        reg += np.array((90, 120, 255), np.float32) * (np.exp(-np.clip(sdf, 0, None) / (9 * S)) * (0.40 * tg + 0.08 * h) * ia)[..., None]
        track = np.array((52, 52, 84), np.float32) * (1 - tg) + np.array((84, 104, 250), np.float32) * tg
        blend(reg, track, cov(sdf) * ia)
        kr = (y1 - y0) / 2 - 3 * S
        kx = x0 + (y1 - y0) / 2 + tg * ((x1 - x0) - (y1 - y0))
        d = np.hypot(X - kx, Y - (y0 + y1) / 2) - kr
        blend(reg, np.array((236, 238, 252), np.float32), cov(d) * ia)

    def _txt(self, out, text, pt, col, anchor, x, y, symbol=False, gain=1.0):
        key = (text, pt, col, anchor, round(x), round(y), symbol)
        lay = self._tc.get(key)
        if lay is None:
            if len(self._tc) > 300:
                self._tc.clear()
            lay = self._tc[key] = self._put(text, pt, col, anchor, x, y, symbol)
        rgb, a, x0, y0 = lay
        comp_layer(out, rgb, a * gain if gain < 1.0 else a, x0, y0)

    def _texts(self, out):
        S, ink, mut = self.S, (228, 230, 250), (141, 144, 187)
        for i, line in enumerate(self.lines):
            self._txt(out, line, 15 if i == 0 else 12, (236, 238, 251) if i == 0 else mut, "l", 40 * S, (112, 140, 162, 184)[i] * S)
        for bid, label, _, _ in MENU_BTNS:
            x0, y0, x1, y1 = self.rect[bid]
            self._txt(out, self.pause_label if bid == "pause" else label, 13, (214, 217, 255), "c",
                      (x0 + x1) / 2, (y0 + y1) / 2, gain=self.bi[bid])
        self._txt(out, "Проверять раз в", 13, (207, 209, 238), "l", 20 * S, 352 * S, gain=self.bi["minus"])
        self._txt(out, "Запускать вместе с Windows", 13, (207, 209, 238), "l", 20 * S, 394 * S, gain=self.bi["auto"])
        self._txt(out, f"{self.interval} мин" if self.interval else "выкл", 13, ink, "c", 505 * S, 352 * S, gain=self.bi["minus"])
        for bid, ch in (("minus", "−"), ("plus", "+")):
            x0, y0, x1, y1 = self.rect[bid]
            self._txt(out, ch, 15, (214, 217, 255), "c", (x0 + x1) / 2, (y0 + y1) / 2, gain=self.bi[bid])
        x0, y0, x1, y1 = self.rect["close"]
        self._txt(out, "✕", 11, (201, 203, 255) if self.hv["close"] > 0.5 else (108, 108, 148), "c",
                  (x0 + x1) / 2, (y0 + y1) / 2, True)

    def render(self, lx, ly, boost=0.0):
        out = self.bg.copy()
        self._header(out, lx, ly)
        self._widgets(out)
        apply_lights(self, out, lx, ly, boost)
        self._texts(out)
        np.minimum(out, 255.49, out=out)
        return np.dstack([out.astype(np.uint8), self.alpha8])


class MenuWindow:
    """Окно главного меню: то же стекло, хват и пружины, что у оверлея. Закрытие прячет его в трей."""
    INTRO, OUTRO = 0.5, 0.22
    CMAXX, CMAXY = math.radians(12), math.radians(10)

    def __init__(self, root, app, cfg, on_close):
        from PIL import ImageTk
        self.root, self.app, self.cfg, self.on_close = root, app, cfg, on_close
        S = max(root.winfo_fpixels("1i") / 96.0, 1.0) * float(cfg["scale"])
        self.S = S
        self.g = g = MenuGlass(S, load_links().get("logo", ""))
        W, H = g.W, g.H
        self.M = M = int(round(28 * S))
        self.WW, self.WH = WW, WH = W + 2 * M, H + 2 * M
        l, t, r, b = work_area()
        self.bx, self.by = (l + r - W) / 2 - M, (t + b - H) / 2 - M
        self.pbx, self.pby = self.bx, self.by
        self.dt_ms = max(1, int(1000 / max(cfg["fps"], 1)))

        win = self.win = tk.Toplevel(root)
        win.withdraw()
        win.overrideredirect(True)
        win.attributes("-topmost", True)
        win.configure(bg=KEY_HEX)
        win.attributes("-transparentcolor", KEY_HEX)
        win.attributes("-alpha", 0.0)
        win.geometry(f"{WW}x{WH}+{int(self.bx)}+{int(self.by + 28 * S)}")
        win.deiconify()
        self.cv = tk.Canvas(win, width=WW, height=WH, bg=KEY_HEX, highlightthickness=0, bd=0)
        self.cv.pack()
        self.photo = ImageTk.PhotoImage(Image.new("RGB", (WW, WH), KEY_U8))
        self.cv.create_image(0, 0, image=self.photo, anchor="nw")
        self.cv.bind("<ButtonPress-1>", self._press)
        self.cv.bind("<B1-Motion>", self._motion)
        self.cv.bind("<ButtonRelease-1>", self._release)

        self.coef = (1, 0, -M, 0, 1, -M, 0, 0)
        self.canvas_arr = np.empty((WH, WW, 3), np.uint8)
        self.canvas_arr[:] = KEY_U8
        self.grab = self.dragged = self.closing = self.dead = self.settled = False
        self.win_drag = self.card_press = self.pending = None
        self.vx = self.vy = 0.0
        self.sp_ax, self.sp_ay = Spring(170, 14), Spring(170, 14)
        self.sp_roll, self.sp_sc = Spring(150, 12), Spring(260, 17)
        self.gb = 0.0
        self.cax = self.cay = self.ch = self.cp = self.cflash = self.cu0 = 0.0
        self.cpress = False
        self.cursor = ""
        self.card = self.tf = self.sig = None
        self.next_status = 0.0
        self.auto = False
        win.update_idletasks()
        px, py = win.winfo_pointerxy()
        self.light = list(self._to_card(px - win.winfo_rootx(), py - win.winfo_rooty()))
        self.t0 = self.last = time.perf_counter()
        self._tick()

    def _to_card(self, x, y):
        a, b, c, d, e, f, g_, h = self.coef
        den = g_ * x + h * y + 1.0
        return (a * x + b * y + c) / den, (d * x + e * y + f) / den

    def _hit(self, x, y):
        for bid, (x0, y0, x1, y1) in self.g.rect.items():
            if x0 <= x <= x1 and y0 <= y <= y1 and self.g.bi[bid] > 0.5:
                return bid
        return None

    def _press(self, e):
        if self.closing:
            return
        g = self.g
        x, y = self._to_card(e.x, e.y)
        self.dragged = False
        hid = self._hit(x, y)
        if hid:
            self.pending, g.press = hid, hid
        elif in_quad(x, y, g.hs["quad"]):
            self.card_press = (e.x_root, e.y_root)
            self.cpress = True
        else:
            self.win_drag = (e.x_root - self.bx, e.y_root - self.by)
            self.grab = True

    def _motion(self, e):
        if self.card_press and not self.dragged and \
                math.hypot(e.x_root - self.card_press[0], e.y_root - self.card_press[1]) > 6 * self.S:
            self.dragged = True
            self.win_drag = (self.card_press[0] - self.bx, self.card_press[1] - self.by)
            self.grab = True
        if self.win_drag:
            self.bx, self.by = e.x_root - self.win_drag[0], e.y_root - self.win_drag[1]
            self.win.geometry(f"+{int(self.bx)}+{int(self.by)}")

    def _release(self, e):
        x, y = self._to_card(e.x, e.y)
        pid, self.pending = self.pending, None
        card, self.card_press = self.card_press, None
        dragged, self.dragged = self.dragged, False
        self.g.press = None
        self.cpress = self.grab = False
        self.win_drag = None
        if pid and self._hit(x, y) == pid:
            if pid == "close":
                self.close()
            else:
                self.app.menu_action(pid)
        elif card and not dragged and in_quad(x, y, self.g.hs["quad"]):
            self.cflash = 1.0
            open_logo_link()

    def _tick(self):
        if self.dead:
            return
        now = time.perf_counter()
        dt = max(now - self.last, 1e-3)
        self.last = now
        t = now - self.t0
        S, g, win = self.S, self.g, self.win

        if self.closing:
            kc = (now - self.tc) / self.OUTRO
            if kc >= 1:
                return self._finish()
            win.attributes("-alpha", 1 - kc * kc)
            win.geometry(f"+{int(self.bx)}+{int(self.by + 14 * S * kc)}")
        elif not self.settled:
            kc = min(t / self.INTRO, 1.0)
            win.attributes("-alpha", ease_out(kc))
            win.geometry(f"+{int(self.bx)}+{int(self.by + (1 - ease_back(kc)) * 28 * S)}")
            if kc >= 1:
                self.settled = True
                win.geometry(f"+{int(self.bx)}+{int(self.by)}")

        # хват и перемещение окна — те же пружины, что у оверлея
        kv = 1 - math.exp(-dt * 18)
        self.vx += ((self.bx - self.pbx) / dt - self.vx) * kv
        self.vy += ((self.by - self.pby) / dt - self.vy) * kv
        self.pbx, self.pby = self.bx, self.by
        grab = self.grab and not self.closing
        cl = lambda v: max(-1.0, min(1.0, v))
        self.sp_ax.step(dt, cl(self.vx / 3200.0) * math.radians(9) if grab else 0.0)
        self.sp_ay.step(dt, cl(self.vy / 3200.0) * math.radians(7) if grab else 0.0)
        self.sp_roll.step(dt, cl(self.vx / 9000.0) * math.radians(3.5) if grab else 0.0)
        self.sp_sc.step(dt, 0.035 if grab else 0.0)
        self.gb += ((1.0 if grab else 0.0) - self.gb) * (1 - math.exp(-dt * 12))
        sps = (self.sp_ax, self.sp_ay, self.sp_roll, self.sp_sc)
        if not grab and all(abs(sp.x) < 2e-4 and abs(sp.v) < 2e-3 for sp in sps):
            for sp in sps:
                sp.x = sp.v = 0.0
        A, B, R, SC = self.sp_ax.x, -self.sp_ay.x, self.sp_roll.x, 1.0 + self.sp_sc.x
        quad = project_quad(g.W, g.H, self.WW / 2, self.WH / 2, 1100 * S, A, B, SC)
        if R:
            ox, oy, cr, sr = self.WW / 2, self.WH / 2, math.cos(R), math.sin(R)
            quad = [(ox + (x - ox) * cr - (y - oy) * sr, oy + (x - ox) * sr + (y - oy) * cr) for x, y in quad]
        self.coef = find_coeffs(quad, g.src_quad)
        self.quad_win = quad

        px, py = win.winfo_pointerxy()
        mx, my = self._to_card(px - win.winfo_rootx(), py - win.winfo_rooty())
        a = 1 - math.exp(-dt * 16)
        self.light[0] += (mx - self.light[0]) * a
        self.light[1] += (my - self.light[1]) * a

        # состояние приложения (раз в 0.5 с): строки статуса, подпись паузы, интервал, автозапуск
        if now >= self.next_status:
            self.next_status = now + 0.5
            st = self.app.menu_state()
            g.lines, g.pause_label, g.interval, self.auto = st["lines"], st["pause"], st["interval"], st["auto"]

        # кнопки: появление по очереди, hover, нажатие, переключатель
        ids = [b[0] for b in MENU_BTNS]
        for i, bid in enumerate(ids):
            g.bi[bid] = ease_out((t - 0.18 - 0.05 * i) / 0.4)
        for bid in ("minus", "plus", "auto", "close"):
            g.bi[bid] = ease_out((t - 0.5) / 0.4)
        hid = None if (self.closing or self.grab) else self._hit(mx, my)
        for bid in g.hv:
            g.hv[bid] += ((1.0 if bid == hid else 0.0) - g.hv[bid]) * (1 - math.exp(-dt * 14))
        g.pv += ((1.0 if g.press else 0.0) - g.pv) * (1 - math.exp(-dt * 25))
        g.tog += ((1.0 if self.auto else 0.0) - g.tog) * (1 - math.exp(-dt * 12))

        # шапка-карточка: взгляд на курсор, блик, клик открывает ссылку
        c, hs = g.hdr, g.hs
        ccx, ccy = g.hdr_c
        inside = (not self.closing) and in_quad(mx, my, hs["quad"])
        cur = "hand2" if (inside or hid) else ""
        if cur != self.cursor:
            self.cursor = cur
            self.cv.configure(cursor=cur)
        self.ch += ((1.0 if (inside or self.cpress) else 0.0) - self.ch) * (1 - math.exp(-dt * 10))
        dxn = max(-1.0, min(1.0, (mx - ccx) / (c.Wc / 2)))
        dyn = max(-1.0, min(1.0, (my - ccy) / (c.Hc / 2)))
        fk = 1 - math.exp(-dt * 16)
        self.cax += (dxn * self.CMAXX * self.ch - self.cax) * fk
        self.cay += (dyn * self.CMAXY * self.ch - self.cay) * fk
        self.cp += ((1.0 if (self.cpress and not self.dragged) else 0.0) - self.cp) * (1 - math.exp(-dt * 25))
        self.cflash *= math.exp(-dt * 4.5)
        if t < 1.3:
            ph = min(max((t - 0.5) / 0.8, 0.0), 1.0)
            u0, shine = -0.75 + 1.5 * ease_out(ph), math.sin(math.pi * ph) * 0.9
        else:
            self.cu0 += ((0.5 * dxn + 0.2 * dyn) * self.ch - self.cu0) * (1 - math.exp(-dt * 12))
            u0, shine = self.cu0, 0.55 * self.ch
        hs.update(ax=self.cax, ay=self.cay, h=self.ch, p=self.cp, flash=self.cflash, shine=shine, u0=u0)
        hs["quad"] = project_quad(c.Wc, c.Hc, ccx, ccy, 420 * S, self.cax, -self.cay,
                                  (1 + 0.05 * self.ch) * (1 - 0.035 * self.cp))

        sig = (round(self.light[0], 1), round(self.light[1], 1),
               tuple(round(v, 2) for v in g.hv.values()), tuple(round(v, 2) for v in g.bi.values()),
               round(g.pv, 2), g.press, round(g.tog, 2), tuple(g.lines), g.pause_label, g.interval,
               round(self.cax, 4), round(self.cay, 4), round(self.ch, 3), round(self.cp, 3),
               round(self.cflash, 3), round(u0, 3), round(shine, 3), round(self.gb, 3))
        changed = sig != self.sig
        if changed:
            self.sig = sig
            self.card = g.render(self.light[0], self.light[1], self.gb)
        tf = (round(A, 4), round(B, 4), round(R, 4), round(SC, 4))
        if changed or tf != self.tf:
            self.tf = tf
            self._present(tf == (0.0, 0.0, 0.0, 1.0))
        win.after(self.dt_ms, self._tick)

    def _present(self, identity):
        g, M = self.g, self.M
        if identity:
            roi = self.canvas_arr[M:M + g.H, M:M + g.W]
            roi[...] = self.card[..., :3]
            roi[g.out_iy, g.out_ix] = KEY_U8
            img = Image.fromarray(self.canvas_arr)
        else:
            arr = warp_rgba(self.card, (self.WW, self.WH), self.quad_win, g.src_quad, BILINEAR)
            rgb = arr[..., :3].copy()
            rgb[arr[..., 3] < 128] = KEY_U8
            img = Image.fromarray(rgb)
        self.photo.paste(img)

    def close(self):
        if not self.closing:
            self.closing = True
            self.tc = time.perf_counter()

    def _finish(self):
        self.dead = True
        self.win.destroy()
        self.on_close()

    def lift(self):
        self.win.lift()


class Tray:
    """Значок в области уведомлений (pystray крутится в своём потоке; команды уходят в очередь приложения)."""

    def __init__(self, app):
        import pystray
        q = lambda cmd: (lambda icon, item: app.cmds.put(cmd))
        menu = pystray.Menu(
            pystray.MenuItem("Открыть меню", q("menu"), default=True),
            pystray.MenuItem("Проверить сейчас", q("check_health")),
            pystray.MenuItem(lambda item: "Продолжить" if app.sched.is_paused(time.time()) else "Пауза на час", q("pause")),
            pystray.MenuItem("Логотип", q("logo")),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("Выйти", q("quit")))
        self.icon = pystray.Icon("mentalizer", make_icon(64), "mentalizer", menu)
        self.icon.run_detached()

    def title(self, text):
        try:
            if self.icon.title != text[:127]:
                self.icon.title = text[:127]
        except Exception:
            pass

    def notify(self, text):
        try:
            self.icon.notify(text, "mentalizer")
        except Exception:
            pass

    def stop(self):
        try:
            self.icon.stop()
        except Exception:
            pass


# =====================================================================
#   Приложение + консоль
# =====================================================================
HELP = """команды:
  check_health  показать оверлей прямо сейчас
  menu          главное меню
  pause         пауза на час / продолжить
  logo          показать карточку логотипа
  close         скрыть оверлеи
  status        режим, состояние окон и время следующей проверки
  next          когда следующая проверка
  game          видит ли скрипт запущенную игру прямо сейчас (и почему)
  steam         найденные библиотеки и игры Steam
  bio           как будет выглядеть bio с сохранёнными значениями
  idle          сколько секунд простоя
  quit          выйти"""


class App(tk.Tk):
    def __init__(self, cfg, mode):
        super().__init__()
        self.withdraw()
        self.cfg, self.mode = cfg, mode
        self.dry_run = mode in ("preview", "logo")
        self.popup = None
        self.logo = None
        self.menu = None
        self.tray = None
        self._last_game = (False, "")
        self.watch = GameWatch(cfg)
        self.sched = Scheduler(cfg, time.time())
        self.cmds = queue.Queue()

        threading.Thread(target=self._stdin_loop, daemon=True).start()
        self.after(200, self._poll_console)
        if mode == "logo":
            self.after(300, self.show_logo)
            print("[logo] наведи курсор на карточку; клик — ссылка из links.json; "
                  "перетащить — двигать; правый клик — закрыть. Напиши help для команд.")
        elif self.dry_run:
            self.after(300, lambda: self.check_health("preview start"))
        else:
            self.after(cfg["poll_seconds"] * 1000, self._poll_schedule)
            print(self.watch.describe())
            nd = self.sched.next_due
            if nd:
                print(f"[working] оверлей раз в {cfg['interval_minutes']} мин, первый в "
                      f"{time.strftime('%H:%M', time.localtime(nd))}. Если запущена игра — покажу после её закрытия.")
            elif cfg["idle_minutes"]:
                print(f"[working] жду простоя {cfg['idle_minutes']} мин.")
            else:
                print("[working] автопоказ выключен (interval_minutes и idle_minutes равны 0) — только check_health.")
            print("Напиши help для команд.")
            if cfg.get("tray"):
                self.after(300, self._start_tray)
        if cfg.get("menu_on_start"):                       # в любом режиме, независимо от трея
            self.after(900, self.show_menu)

    def _start_tray(self):
        try:
            self.tray = Tray(self)
        except Exception as e:
            self.tray = None
            print(f"[tray] значок не запущен ({e}). Установи: pip install pystray")
            return
        if FROZEN and self.cfg.get("hide_console"):
            hide_console()
        self.after(1500, lambda: self.tray and self.tray.notify("Работает в трее: клик по значку открывает меню"))

    def _stdin_loop(self):
        try:
            for line in sys.stdin:
                self.cmds.put(line.strip().lower())
        except Exception:
            pass

    def _poll_console(self):
        while not self.cmds.empty():
            self._run_cmd(self.cmds.get())
        self.after(200, self._poll_console)

    def _run_cmd(self, cmd):
        if cmd == "check_health":
            self.check_health("console")
        elif cmd == "logo":
            self.show_logo()
        elif cmd == "menu":
            self.show_menu()
        elif cmd == "pause":
            self.menu_action("pause")
        elif cmd == "close":
            for w in (self.popup, self.logo, self.menu):
                if w:
                    w.close()
        elif cmd == "status":
            print(f"mode={self.mode}  idle={idle_seconds():.0f}s  "
                  f"оверлей={'открыт' if self.popup else 'скрыт'}  логотип={'открыт' if self.logo else 'скрыт'}")
            self._print_next()
        elif cmd == "next":
            self._print_next()
        elif cmd == "steam":
            w = self.watch
            print(w.describe())
            for lib in w.libs:
                print("  библиотека:", lib)
            for name, _ in w.library[:40]:
                print("  ", name)
            if len(w.library) > 40:
                print(f"   … и ещё {len(w.library) - 40}")
        elif cmd == "game":
            busy, why = self.watch.check()
            print(f"игра/полноэкранный режим: {why}" if busy else "игра не обнаружена — показ разрешён")
        elif cmd == "bio":
            print(make_bio(load_state(), self.cfg["blocks"], self.cfg["bio_style"]))
        elif cmd == "idle":
            print(f"{idle_seconds():.0f}s")
        elif cmd in ("quit", "exit", "q"):
            self.quit_app()
        elif cmd in ("help", "?"):
            print(HELP)
        elif cmd:
            print(f"неизвестная команда: {cmd}  (help)")

    def check_health(self, reason):
        if self.popup:
            self.popup.lift()
            print("[check_health] оверлей уже открыт")
            return
        print(f"[check_health] {reason}")
        self.sched.reset(time.time())              # следующий отсчёт — от этого показа
        self.popup = Overlay(self, self.cfg, load_state(), self.dry_run, self._popup_closed)

    def _print_next(self):
        nd, pend = self.sched.next_due, self.sched.pending
        if pend:
            print(f"ждёт показа ({pend}) — игра закроется, и окно появится")
        elif nd:
            print(f"следующая проверка в {time.strftime('%H:%M', time.localtime(nd))} "
                  f"(через {max(nd - time.time(), 0) / 60:.0f} мин)")
        else:
            print("автопоказ по расписанию выключен")

    def _poll_schedule(self):
        try:
            busy, why = self.watch.check()
            self._last_game = (busy, why)
            reason, msgs = self.sched.step(time.time(), busy, why, idle_seconds(), self.popup is not None)
            for m in msgs:
                print(m)
            if reason:
                self.check_health(reason)
        except Exception as e:
            print("[schedule] ошибка:", e)
        if self.tray:
            self.tray.title(self.menu_state()["lines"][0])
        self.after(self.cfg["poll_seconds"] * 1000, self._poll_schedule)

    def _popup_closed(self):
        self.popup = None

    def show_logo(self):
        if self.logo:
            self.logo.lift()
            print("[logo] карточка уже открыта")
            return
        self.logo = LogoOverlay(self, self.cfg, self._logo_closed)

    # --- главное меню ---
    def show_menu(self):
        if self.menu:
            self.menu.lift()
            return
        try:
            self.menu = MenuWindow(self, self, self.cfg, self._menu_closed)
        except Exception as e:
            self.menu = None
            import traceback
            print("[menu] не удалось открыть меню:", e)
            traceback.print_exc()

    def _menu_closed(self):
        self.menu = None

    def menu_state(self):
        now, sc = time.time(), self.sched
        hm = lambda ts: time.strftime("%H:%M", time.localtime(ts))
        if sc.is_paused(now):
            l1, l2 = f"На паузе до {hm(sc.paused_until)}", "оверлей не появится, пока не нажмёшь «Продолжить»"
        elif sc.pending:
            l1, l2 = "Ждёт показа", f"{sc.pending}: игра закроется, и окно появится"
        elif sc.next_due:
            l1 = f"Следующая проверка в {hm(sc.next_due)}"
            l2 = f"через {max(sc.next_due - now, 0) / 60:.0f} мин, раз в {self.cfg['interval_minutes']} мин"
        else:
            l1, l2 = "Автопоказ выключен", "поставь интервал ниже, чтобы окно появлялось само"
        busy, why = self._last_game
        l3 = f"Игра: {why}" if busy else "Игра не обнаружена"
        if self.dry_run:
            l4 = "Режим предпросмотра: в Telegram ничего не отправляется"
        elif (BASE / (self.cfg["session"] + ".session")).exists():
            l4 = "Telegram: сессия найдена"
        else:
            l4 = "Telegram: нужен вход, запусти с --login"
        return {"lines": [l1, l2, l3, l4], "interval": int(self.cfg["interval_minutes"]),
                "pause": "Продолжить" if sc.is_paused(now) else "Пауза на час", "auto": autostart_get()}

    def menu_action(self, a):
        now = time.time()
        if a == "check":
            if self.menu:
                self.menu.close()
            self.check_health("из меню")
        elif a == "preview":
            if self.popup:
                self.popup.lift()
            else:
                self.popup = Overlay(self, self.cfg, load_state(), True, self._popup_closed)
        elif a == "pause":
            if self.sched.is_paused(now):
                self.sched.resume()
                print("[pause] продолжаю")
            else:
                self.sched.pause(now, 60)
                print("[pause] пауза на час")
        elif a == "logo":
            self.show_logo()
        elif a == "config":
            try:
                os.startfile(str(CONFIG_PATH))
            except Exception as e:
                print("[config] не удалось открыть:", e)
        elif a == "quit":
            self.quit_app()
        elif a in ("minus", "plus"):
            cur = int(self.cfg["interval_minutes"])
            new = max(5, cur - 5) if a == "minus" and cur else (cur + 5 if cur else 30) if a == "plus" else cur
            new = min(new, 240)
            self.cfg["interval_minutes"] = new
            self.sched.interval = new * 60
            self.sched.reset(now)
            save_config_value("interval_minutes", new)
        elif a == "auto":
            autostart_set(not autostart_get())

    def quit_app(self):
        if self.tray:
            self.tray.stop()
        self.destroy()

    def _logo_closed(self):
        self.logo = None
        if self.mode == "logo":
            print("[logo] закрыто. logo — открыть снова, quit — выйти")


def choose_mode():
    print("=== bio_checkin ===")
    print("  1) preview         — оверлей сразу, в Telegram ничего не отправляется")
    print("  2) see in working  — рабочий режим: оверлей раз в час (не во время игры), bio обновляется")
    print("  3) logo card       — превью карточки логотипа (наведи курсор, клик откроет ссылку из links.json)")
    while True:
        c = input("> ").strip().lower()
        if c in ("1", "p", "preview"):
            return "preview"
        if c in ("2", "w", "working", "see in working"):
            return "working"
        if c in ("3", "l", "logo", "logo card"):
            return "logo"


if __name__ == "__main__":
    cfg = load_config()
    if "--no-tray" in sys.argv:
        cfg["tray"] = False
    setup_logging(cfg)
    apply_font_config(cfg)

    if "--login" in sys.argv:
        tg_login(cfg)
        sys.exit(0)

    if "--preview" in sys.argv:
        mode = "preview"
    elif "--working" in sys.argv:
        mode = "working"
    elif "--logo" in sys.argv:
        mode = "logo"
    elif str(cfg.get("default_mode") or "").lower() in ("preview", "working", "logo"):
        mode = cfg["default_mode"].lower()
    elif sys.stdin is None:                      # exe собран без консоли (--noconsole): спрашивать некого
        mode = "working"
    else:
        mode = choose_mode()

    if mode == "working" and (not cfg["api_id"] or not cfg["api_hash"]):
        print(f"Впиши api_id и api_hash в {CONFIG_PATH.name} (или запусти preview).")
        sys.exit(1)

    App(cfg, mode).mainloop()
