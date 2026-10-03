"""控制端"""
from __future__ import annotations
import ctypes
import os
import sys
from io import BytesIO
import json
import queue
import socket
import struct
import threading
import time
import tkinter as tk
from tkinter import messagebox, scrolledtext, ttk

try:
    from PIL import Image, ImageTk
except ImportError as exc:
    raise SystemExit(
        "缺少 Pillow模块"
    ) from exc

try:
    DISPLAY_RESAMPLE = Image.Resampling.LANCZOS
except AttributeError:
    DISPLAY_RESAMPLE = Image.LANCZOS


TYPE_SCREEN = 1
TYPE_MOUSE = 2
TYPE_KEYBOARD = 3
TYPE_COMMAND = 4
TYPE_COMMAND_RESULT = 5
TYPE_ERROR = 6
TYPE_STATS = 7
TYPE_SPEED_TEST = 8
TYPE_SPEED_DATA = 9

DEFAULT_HOST = "120.26.37.94"
DEFAULT_PORT = 5000
MAX_PACKET_SIZE = 20 * 1024 * 1024
PACKET_HEAD_SIZE = 5

SPEED_TEST_SECONDS = 8.0
SPEED_TEST_CHUNK = 256 * 1024

TIME_FORMAT = "%Y-%m-%d %H:%M:%S"



IS_WINDOWS = sys.platform.startswith("win")

try:
    from ctypes import wintypes as _wintypes
except (ImportError, ValueError):
    _wintypes = None

try:
    _HOOKPROC_FACTORY = ctypes.WINFUNCTYPE
except AttributeError:
    _HOOKPROC_FACTORY = None

_USER32 = None
_KERNEL32 = None
_HOOKPROC_TYPE = None
_MSG_TYPE = None


def _init_winapi() -> bool:
    """准备 Win32 接口,非 Windows 或失败时返回 False"""
    global _USER32, _KERNEL32, _HOOKPROC_TYPE, _MSG_TYPE
    if not IS_WINDOWS or _HOOKPROC_FACTORY is None or _wintypes is None:
        return False

    try:
        user32 = ctypes.windll.user32
        kernel32 = ctypes.windll.kernel32
        hookproc = _HOOKPROC_FACTORY(
            ctypes.c_ssize_t,
            ctypes.c_int,
            ctypes.c_size_t,
            ctypes.c_ssize_t,
        )

        user32.GetForegroundWindow.restype = ctypes.c_void_p
        user32.GetWindowThreadProcessId.argtypes = [
            ctypes.c_void_p,
            ctypes.POINTER(ctypes.c_ulong),
        ]
        user32.GetWindowThreadProcessId.restype = ctypes.c_ulong
        user32.GetAsyncKeyState.argtypes = [ctypes.c_int]
        user32.GetAsyncKeyState.restype = ctypes.c_short
        user32.SetWindowsHookExW.argtypes = [
            ctypes.c_int,
            hookproc,
            ctypes.c_void_p,
            ctypes.c_ulong,
        ]
        user32.SetWindowsHookExW.restype = ctypes.c_void_p
        user32.UnhookWindowsHookEx.argtypes = [ctypes.c_void_p]
        user32.UnhookWindowsHookEx.restype = ctypes.c_int
        user32.CallNextHookEx.argtypes = [
            ctypes.c_void_p,
            ctypes.c_int,
            ctypes.c_size_t,
            ctypes.c_ssize_t,
        ]
        user32.CallNextHookEx.restype = ctypes.c_ssize_t
        user32.GetMessageW.argtypes = [
            ctypes.POINTER(_wintypes.MSG),
            ctypes.c_void_p,
            ctypes.c_uint,
            ctypes.c_uint,
        ]
        user32.GetMessageW.restype = ctypes.c_int
        user32.SetTimer.argtypes = [
            ctypes.c_void_p,
            ctypes.c_size_t,
            ctypes.c_uint,
            ctypes.c_void_p,
        ]
        user32.SetTimer.restype = ctypes.c_size_t
        user32.KillTimer.argtypes = [ctypes.c_void_p, ctypes.c_size_t]
        user32.KillTimer.restype = ctypes.c_int
        user32.PostThreadMessageW.argtypes = [
            ctypes.c_ulong,
            ctypes.c_uint,
            ctypes.c_size_t,
            ctypes.c_ssize_t,
        ]
        user32.PostThreadMessageW.restype = ctypes.c_int
        kernel32.GetCurrentThreadId.restype = ctypes.c_ulong

        _USER32 = user32
        _KERNEL32 = kernel32
        _HOOKPROC_TYPE = hookproc
        _MSG_TYPE = _wintypes.MSG
        return True
    except Exception:
        _USER32 = None
        _KERNEL32 = None
        _HOOKPROC_TYPE = None
        _MSG_TYPE = None
        return False


_init_winapi()


class KBDLLHOOKSTRUCT(ctypes.Structure):
    """键盘钩子的按键数据"""

    _fields_ = [
        ("vkCode", ctypes.c_ulong),
        ("scanCode", ctypes.c_ulong),
        ("flags", ctypes.c_ulong),
        ("time", ctypes.c_ulong),
        ("dwExtraInfo", ctypes.c_void_p),
    ]


_VK_KEYS: dict[int, str] = {
    0x08: "backspace",
    0x09: "tab",
    0x0D: "enter",
    0x10: "shift",
    0x11: "ctrl",
    0x12: "alt",
    0x13: "pause",
    0x14: "caps_lock",
    0x1B: "esc",
    0x20: "space",
    0x21: "page_up",
    0x22: "page_down",
    0x23: "end",
    0x24: "home",
    0x25: "left",
    0x26: "up",
    0x27: "right",
    0x28: "down",
    0x2C: "print_screen",
    0x2D: "insert",
    0x2E: "delete",
    0x5B: "cmd",
    0x5C: "cmd",
    0x5D: "menu",
    0x6A: "*",
    0x6B: "+",
    0x6C: ",",
    0x6D: "-",
    0x6E: ".",
    0x6F: "/",
    0x90: "num_lock",
    0x91: "scroll_lock",
    0xBA: ";",
    0xBB: "=",
    0xBC: ",",
    0xBD: "-",
    0xBE: ".",
    0xBF: "/",
    0xC0: "`",
    0xDB: "[",
    0xDC: "\\",
    0xDD: "]",
    0xDE: "'",
}
_VK_KEYS.update({0x30 + i: str(i) for i in range(10)})
_VK_KEYS.update({0x60 + i: str(i) for i in range(10)})
_VK_KEYS.update({0x41 + i: chr(ord("a") + i) for i in range(26)})
_VK_KEYS.update({0x70 + i: f"f{i + 1}" for i in range(12)})


def vk_to_remote_key(vk_code: int) -> str | None:
    """Windows 虚拟键码,被控端按键名"""
    return _VK_KEYS.get(int(vk_code))


def foreground_belongs_to_self() -> bool:
    """当前前台窗口是否属于本程序"""
    if _USER32 is None:
        return True
    try:
        window = _USER32.GetForegroundWindow()
        if not window:
            return False
        process_id = ctypes.c_ulong(0)
        _USER32.GetWindowThreadProcessId(
            ctypes.c_void_p(window),
            ctypes.byref(process_id),
        )
        return process_id.value == os.getpid()
    except Exception:
        return True


def enable_dpi_awareness() -> None:
    """让界面在高分屏上更清晰"""
    if not IS_WINDOWS:
        return
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(1)
        return
    except Exception:
        pass
    try:
        ctypes.windll.user32.SetProcessDPIAware()
    except Exception:
        pass


class KeyboardLock:
    """锁定本机键盘：按键只发送给被控端，本机不再响应。
    解锁方式：
      1. 点击窗口以外的任意位置解锁；
      2. 连接断开或程序关闭时自动解锁；
      3. 连续按下 Ctrl+Alt+Shift+Q 强制解锁。
    """

    WH_KEYBOARD_LL = 13
    WM_KEYDOWN = 0x0100
    WM_KEYUP = 0x0101
    WM_SYSKEYDOWN = 0x0104
    WM_SYSKEYUP = 0x0105
    WM_QUIT = 0x0012
    WM_TIMER = 0x0113

    VK_SHIFT = 0x10
    VK_CONTROL = 0x11
    VK_MENU = 0x12
    PANIC_VK = 0x51  # Q

    CHECK_INTERVAL_MS = 80
    FOCUS_LOST_LIMIT = 3

    def __init__(self, client: RemoteClient, events: queue.Queue):
        self.client = client
        self.events = events
        self._pressed: set[str] = set()
        self._pressed_lock = threading.Lock()
        self._stop = threading.Event()
        self._ready = threading.Event()
        self._thread: threading.Thread | None = None
        self._thread_id = 0
        self._hook = 0
        self._callback = None
        self._started = False
        self._lost_focus_checks = 0

    @staticmethod
    def supported() -> bool:
        return (
            _USER32 is not None
            and _KERNEL32 is not None
            and _MSG_TYPE is not None
        )

    @property
    def active(self) -> bool:
        thread = self._thread
        return self._started and thread is not None and thread.is_alive()

    def start(self) -> bool:
        """安装键盘钩子"""
        if self.active:
            return True
        if not self.supported():
            return False

        self._pressed.clear()
        self._lost_focus_checks = 0
        self._stop.clear()
        self._ready.clear()
        self._started = False
        thread = threading.Thread(
            target=self._run,
            name="keyboard-lock",
            daemon=True,
        )
        self._thread = thread
        thread.start()
        self._ready.wait(2.0)
        if not self._started:
            self._thread = None
        return self._started

    def stop(self) -> None:
        """卸载键盘钩子"""
        thread = self._thread
        self._stop.set()
        if thread is not None and thread is not threading.current_thread():
            if self._thread_id:
                try:
                    _USER32.PostThreadMessageW(
                        self._thread_id,
                        self.WM_QUIT,
                        0,
                        0,
                    )
                except Exception:
                    pass
            thread.join(timeout=1.5)
            if thread.is_alive() and self._hook:
                try:
                    _USER32.UnhookWindowsHookEx(
                        ctypes.c_void_p(self._hook)
                    )
                except Exception:
                    pass
                self._hook = 0
                self._started = False
        self._thread = None
        self._started = False
        self._release_all()

    def _run(self) -> None:
        user32 = _USER32
        kernel32 = _KERNEL32
        try:
            self._thread_id = int(kernel32.GetCurrentThreadId())
            self._callback = _HOOKPROC_TYPE(self._on_key_event)
            self._hook = int(
                user32.SetWindowsHookExW(
                    self.WH_KEYBOARD_LL,
                    self._callback,
                    None,
                    0,
                )
                or 0
            )
            if not self._hook:
                raise RuntimeError("安装键盘钩子失败")

            self._started = True
            self._ready.set()
            self._message_loop()
        except Exception as exc:
            self.events.put(
                ("keyboard_lock", False, f"键盘锁定失败：{exc}")
            )
        finally:
            self._started = False
            self._ready.set()
            if self._hook:
                try:
                    user32.UnhookWindowsHookEx(
                        ctypes.c_void_p(self._hook)
                    )
                except Exception:
                    pass
                self._hook = 0
            self._release_all()

    def _message_loop(self) -> None:
        """钩子线程的消息循环,同时定期检查窗口焦点
        用 GetMessage 取消息是低级键盘钩子的标准做法,
        系统会在该线程取消息时调用键盘钩子回调
        """
        user32 = _USER32
        message = _MSG_TYPE()
        timer_id = int(
            user32.SetTimer(None, 0, self.CHECK_INTERVAL_MS, None) or 0
        )
        if not timer_id:
            threading.Thread(
                target=self._tick_loop,
                args=(self._thread_id,),
                name="keyboard-lock-tick",
                daemon=True,
            ).start()

        try:
            while not self._stop.is_set():
                result = user32.GetMessageW(
                    ctypes.byref(message),
                    None,
                    0,
                    0,
                )
                if result <= 0:
                    break

                if message.message == self.WM_TIMER:
                    reason = self._check_state()
                    if reason is not None:
                        self.events.put(
                            ("keyboard_lock", False, reason)
                        )
                        break
                    continue

                user32.TranslateMessage(ctypes.byref(message))
                user32.DispatchMessageW(ctypes.byref(message))
        finally:
            if timer_id:
                try:
                    user32.KillTimer(None, timer_id)
                except Exception:
                    pass

    def _check_state(self) -> str | None:
        """返回 None 表示继续保持锁定,否则返回解锁原因"""
        if not self.client.connected:
            return "连接已断开"
        if foreground_belongs_to_self():
            self._lost_focus_checks = 0
            return None
        self._lost_focus_checks += 1
        if self._lost_focus_checks >= self.FOCUS_LOST_LIMIT:
            return "点击了窗口以外的区域"
        return None

    def _tick_loop(self, thread_id: int) -> None:
        """没有系统定时器时,定期给钩子线程发消息以触发焦点检查"""
        while not self._stop.wait(self.CHECK_INTERVAL_MS / 1000):
            try:
                _USER32.PostThreadMessageW(
                    thread_id,
                    self.WM_TIMER,
                    0,
                    0,
                )
            except Exception:
                return

    def _on_key_event(self, n_code, w_param, l_param):
        """键盘钩子:按键不再交给本机,而是发送给被控端"""
        user32 = _USER32
        if n_code < 0:
            return user32.CallNextHookEx(None, n_code, w_param, l_param)

        try:
            data = ctypes.cast(
                ctypes.c_void_p(l_param),
                ctypes.POINTER(KBDLLHOOKSTRUCT),
            ).contents
            vk_code = int(data.vkCode)
            is_press = int(w_param) in (
                self.WM_KEYDOWN,
                self.WM_SYSKEYDOWN,
            )

            if (
                is_press
                and vk_code == self.PANIC_VK
                and self._modifiers_down()
            ):
                self.events.put(
                    ("keyboard_lock", False, "按下了强制解锁组合键")
                )
                self._stop.set()
                return 1

            key = vk_to_remote_key(vk_code)
            if key is not None:
                self._send_key(key, is_press)
            return 1
        except Exception:
            # 出错时立刻解锁，避免本机键盘被卡住
            self.events.put(
                ("keyboard_lock", False, "键盘钩子异常")
            )
            self._stop.set()
            return user32.CallNextHookEx(None, n_code, w_param, l_param)

    def _modifiers_down(self) -> bool:
        user32 = _USER32
        return all(
            int(user32.GetAsyncKeyState(vk)) & 0x8000
            for vk in (self.VK_SHIFT, self.VK_CONTROL, self.VK_MENU)
        )

    def _send_key(self, key: str, is_press: bool) -> None:
        with self._pressed_lock:
            if is_press:
                self._pressed.add(key)
            else:
                self._pressed.discard(key)

        self.client.send_json(
            TYPE_KEYBOARD,
            {"action": "press" if is_press else "release", "key": key},
        )

    def _release_all(self) -> None:
        with self._pressed_lock:
            keys = tuple(self._pressed)
            self._pressed.clear()

        for key in keys:
            try:
                self.client.send_json(
                    TYPE_KEYBOARD,
                    {"action": "release", "key": key},
                )
            except Exception:
                pass


class RemoteClient:
    """负责连接、收发数据包，并把结果放入线程安全队列。"""

    STATS_INTERVAL = 1.0

    def __init__(self, events: queue.Queue, frames: queue.Queue):
        self.events = events
        self.frames = frames
        self._socket: socket.socket | None = None
        self._send_lock = threading.Lock()
        self._state_lock = threading.Lock()
        self._running = False
        self._stats_started = 0.0
        self._stats_frames = 0
        self._stats_bytes = 0
        self._last_frame_size = 0
        self._speed_started = 0.0
        self._speed_bytes = 0
        self._speed_peak = 0.0
        self._speed_last_emit = 0.0
        self._speed_window_start = 0.0
        self._speed_window_bytes = 0

    @property
    def connected(self) -> bool:
        with self._state_lock:
            return self._running and self._socket is not None

    def connect(self, host: str, port: int) -> None:
        self.disconnect(notify=False)
        try:
            sock = socket.create_connection((host, port), timeout=8)
            sock.setsockopt(
                socket.IPPROTO_TCP,
                socket.TCP_NODELAY,
                1,
            )
            sock.settimeout(None)
        except OSError as exc:
            self.events.put(("status", "连接失败", "red"))
            self.events.put(("error", f"无法连接 {host}:{port}\n{exc}"))
            return

        with self._state_lock:
            self._socket = sock
            self._running = True

        self._reset_stats()
        self._reset_speed()
        self.events.put(("status", "已连接", "green"))
        threading.Thread(
            target=self._receive_loop,
            args=(sock,),
            name="remote-recv",
            daemon=True,
        ).start()

    def disconnect(self, notify: bool = True) -> None:
        with self._state_lock:
            was_running = self._running
            self._running = False
            sock = self._socket
            self._socket = None

        if sock is not None:
            try:
                sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            try:
                sock.close()
            except OSError:
                pass

        if notify and was_running:
            self.events.put(("status", "已断开", "gray"))

    def send_packet(self, packet_type: int, data: bytes = b"") -> bool:
        with self._send_lock:
            with self._state_lock:
                sock = self._socket
                running = self._running
            if not running or sock is None:
                return False

            header = struct.pack("!BI", packet_type, len(data))
            try:
                sock.sendall(header + data)
                return True
            except OSError as exc:
                self.events.put(("error", f"发送数据失败：{exc}"))
                self.disconnect(notify=False)
                self.events.put(("status", "连接已断开", "red"))
                self.events.put(("connected", False))
                return False

    def send_json(self, packet_type: int, payload: dict) -> bool:
        data = json.dumps(
            payload,
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8")
        return self.send_packet(packet_type, data)

    def _receive_exact(self, sock: socket.socket, size: int) -> bytes:
        result = bytearray()
        while len(result) < size:
            chunk = sock.recv(size - len(result))
            if not chunk:
                raise ConnectionError("远程端已断开连接")
            result.extend(chunk)
        return bytes(result)

    def _receive_loop(self, sock: socket.socket) -> None:
        try:
            while True:
                with self._state_lock:
                    if not self._running or self._socket is not sock:
                        return
                header = self._receive_exact(sock, PACKET_HEAD_SIZE)
                packet_type, data_size = struct.unpack("!BI", header)
                if data_size > MAX_PACKET_SIZE:
                    raise ConnectionError(
                        f"数据包过大：{data_size} bytes"
                    )
                data = self._receive_exact(sock, data_size)

                if packet_type == TYPE_SCREEN:
                    self._count_frame(data)
                    self._put_latest(self.frames, data)
                elif packet_type == TYPE_COMMAND_RESULT:
                    text = data.decode("utf-8", errors="replace")
                    self.events.put(("command", text))
                elif packet_type == TYPE_ERROR:
                    self.events.put(("remote_message", data))
                elif packet_type == TYPE_STATS:
                    self.events.put(("remote_stats", data))
                elif packet_type == TYPE_SPEED_DATA:
                    self._count_speed_data(len(data))
                elif packet_type == TYPE_SPEED_TEST:
                    self.events.put(("speed_test", self._speed_message(data)))
        except (ConnectionError, OSError) as exc:
            with self._state_lock:
                was_running = (
                    self._running and self._socket is sock
                )
                if was_running:
                    self._running = False
                    self._socket = None
            try:
                sock.close()
            except OSError:
                pass
            if was_running:
                self.events.put(("error", f"连接中断：{exc}"))
                self.events.put(("status", "连接已断开", "red"))
                self.events.put(("connected", False))

    def _reset_stats(self) -> None:
        """重新开始统计带宽"""
        self._stats_started = 0.0
        self._stats_frames = 0
        self._stats_bytes = 0
        self._last_frame_size = 0

    def _count_frame(self, data: bytes) -> None:
        """
        统计控制端实际收到的画面数据,每秒把带宽和JPEG大小放进事件队列
        :param data:一帧画面的数据
        :return:None
        """
        now = time.monotonic()
        if (
            not self._stats_started
            or now - self._stats_started >= self.STATS_INTERVAL * 2
        ):
            # 第一次统计,或者画面中断过(例如刚做完压测),重新开始统计
            self._stats_started = now
            self._stats_frames = 0
            self._stats_bytes = 0
        self._stats_frames += 1
        self._stats_bytes += len(data) + PACKET_HEAD_SIZE
        self._last_frame_size = len(data)

        elapsed = now - self._stats_started
        if elapsed < self.STATS_INTERVAL:
            return
        frames = self._stats_frames
        self._emit("bandwidth", {
            "fps": frames / elapsed,
            "bitrate": self._stats_bytes * 8 / elapsed,
            "byte_rate": self._stats_bytes / elapsed,
            "frame_size": self._last_frame_size,
            "average_frame_size": self._stats_bytes / frames,
        })
        self._stats_started = now
        self._stats_frames = 0
        self._stats_bytes = 0

    def _reset_speed(self) -> None:
        """重新开始统计压测数据"""
        self._speed_started = 0.0
        self._speed_bytes = 0
        self._speed_peak = 0.0
        self._speed_last_emit = 0.0
        self._speed_window_start = 0.0
        self._speed_window_bytes = 0

    def _count_speed_data(self, size: int) -> None:
        """
        统计压测期间实际收到的数据量,定期把进度交给界面线程
        :param size:本次收到的数据大小
        :return:None
        """
        now = time.monotonic()
        if not self._speed_started:
            self._speed_started = now
            self._speed_last_emit = now
            self._speed_window_start = now

        received = size + PACKET_HEAD_SIZE
        self._speed_bytes += received
        self._speed_window_bytes += received
        if now - self._speed_last_emit < 0.2:
            return

        window_elapsed = max(now - self._speed_window_start, 0.001)
        self._speed_peak = max(
            self._speed_peak,
            self._speed_window_bytes * 8 / window_elapsed,
        )
        self._speed_window_start = now
        self._speed_window_bytes = 0
        self._speed_last_emit = now
        elapsed = max(now - self._speed_started, 0.001)
        self._emit("speed_progress", {
            "seconds": elapsed,
            "bytes": self._speed_bytes,
            "bitrate": self._speed_bytes * 8 / elapsed,
            "peak": self._speed_peak,
        })

    def _speed_message(self, data: bytes) -> dict:
        """
        解析被控端的压测消息,结束时附上控制端自己统计的结果
        :param data:被控端发来的消息
        :return:压测消息
        """
        message: dict = {}
        try:
            loaded = json.loads(data.decode("utf-8"))
            if isinstance(loaded, dict):
                message = loaded
        except (UnicodeDecodeError, ValueError):
            message = {}

        if message.get("action") != "finished":
            return message

        if str(message.get("direction") or "up") != "up":
            # 反向压测时控制端是发送方,收到的数字必须用被控端自己统计的
            self._reset_speed()
            return message

        elapsed = 0.0
        if self._speed_started:
            elapsed = max(time.monotonic() - self._speed_started, 0.001)
        message["received_bytes"] = self._speed_bytes
        message["receive_seconds"] = elapsed
        message["receive_bitrate"] = (
            self._speed_bytes * 8 / elapsed if elapsed else 0.0
        )
        message["receive_peak"] = self._speed_peak
        self._reset_speed()
        return message

    def _emit(self, name: str, payload: dict) -> None:
        """
        把统计结果交给界面线程显示,队列满了就丢掉这一次
        :param name:事件名
        :param payload:统计数据
        :return:None
        """
        try:
            self.events.put_nowait((name, payload))
        except queue.Full:
            pass

    @staticmethod
    def _put_latest(target: queue.Queue, item) -> None:
        try:
            target.put_nowait(item)
            return
        except queue.Full:
            pass

        try:
            target.get_nowait()
        except queue.Empty:
            return

        try:
            target.put_nowait(item)
        except queue.Full:
            pass


class RemoteDesktopApp:
    """远程桌面控制台主界面。"""

    BG = "SystemButtonFace"
    PANEL = "SystemButtonFace"
    PANEL_ALT = "SystemWindow"
    CANVAS = "black"
    TEXT = "SystemButtonText"
    MUTED = "SystemDisabledText"
    ACCENT = "SystemHighlight"
    BORDER = "SystemButtonShadow"
    LOCKED = "#c42b1c"

    FONT_UI = ("Microsoft YaHei UI", 9)
    FONT_UI_BOLD = ("Microsoft YaHei UI", 9, "bold")
    FONT_TITLE = ("Microsoft YaHei UI", 10, "bold")
    FONT_MONO = ("Consolas", 9)

    WINDOW_SIZE = (1200, 760)
    WINDOW_MIN_SIZE = (920, 580)
    SIDEBAR_WIDTH = 360
    SIDEBAR_HEIGHT = 560
    TEXT_LIMIT = 2000

    SPECIAL_KEYS = {
        "Return": "enter",
        "KP_Enter": "enter",
        "Escape": "esc",
        "BackSpace": "backspace",
        "Tab": "tab",
        "space": "space",
        "Shift_L": "shift",
        "Shift_R": "shift",
        "Control_L": "ctrl",
        "Control_R": "ctrl",
        "Alt_L": "alt",
        "Alt_R": "alt",
        "Super_L": "cmd",
        "Super_R": "cmd",
        "Caps_Lock": "caps_lock",
        "Num_Lock": "num_lock",
        "Scroll_Lock": "scroll_lock",
        "Print": "print_screen",
        "Pause": "pause",
        "Menu": "menu",
        "Up": "up",
        "Down": "down",
        "Left": "left",
        "Right": "right",
        "Home": "home",
        "End": "end",
        "Prior": "page_up",
        "Next": "page_down",
        "Delete": "delete",
        "Insert": "insert",
        "F1": "f1",
        "F2": "f2",
        "F3": "f3",
        "F4": "f4",
        "F5": "f5",
        "F6": "f6",
        "F7": "f7",
        "F8": "f8",
        "F9": "f9",
        "F10": "f10",
        "F11": "f11",
        "F12": "f12",
        "ISO_Left_Tab": "tab",
        "KP_0": "0",
        "KP_1": "1",
        "KP_2": "2",
        "KP_3": "3",
        "KP_4": "4",
        "KP_5": "5",
        "KP_6": "6",
        "KP_7": "7",
        "KP_8": "8",
        "KP_9": "9",
        "KP_Decimal": ".",
        "KP_Divide": "/",
        "KP_Multiply": "*",
        "KP_Subtract": "-",
        "KP_Add": "+",
    }

    PUNCTUATION_KEYS = {
        "minus": "-",
        "equal": "=",
        "bracketleft": "[",
        "bracketright": "]",
        "backslash": "\\",
        "semicolon": ";",
        "apostrophe": "'",
        "grave": "`",
        "comma": ",",
        "period": ".",
        "slash": "/",
    }

    def __init__(self, root: tk.Tk):
        self.root = root
        self.root.title("远程桌面控制台")
        self.root.configure(bg=self.BG)
        self._apply_window_size()

        self.events: queue.Queue = queue.Queue(maxsize=32)
        self.frames: queue.Queue = queue.Queue(maxsize=1)
        self.decoded_frames: queue.Queue = queue.Queue(maxsize=1)
        self.client = RemoteClient(self.events, self.frames)
        self.photo_image: ImageTk.PhotoImage | None = None
        self.display_box = (0, 0, 0, 0)
        self.frame_times: list[float] = []
        self.display_size = (1, 1)
        self.display_size_lock = threading.Lock()
        self.decode_stop = threading.Event()
        self.keyboard_enabled = False
        self.keyboard_lock = KeyboardLock(self.client, self.events)
        self.keyboard_lock_active = False
        self.pressed_remote_keys: set[str] = set()
        self.pressed_mouse_buttons: set[str] = set()
        self.last_mouse_send = 0.0
        self.pending_mouse: tuple[float, float] | None = None
        self.mouse_flush_job: str | None = None
        self.message_count = 0
        self.status_label: tk.Label | None = None
        self.message_output: scrolledtext.ScrolledText | None = None

        self.address_var = tk.StringVar(
            value=f"{DEFAULT_HOST}:{DEFAULT_PORT}"
        )
        self.status_var = tk.StringVar(value="● 未连接")
        self.status_color = self.MUTED
        self.keyboard_var = tk.StringVar(value="键盘：本机")
        self.fps_var = tk.StringVar(value="0 FPS")
        self.bandwidth_var = tk.StringVar(value="-")
        self.frame_size_var = tk.StringVar(value="-")
        self.remote_stats_var = tk.StringVar(value="-")
        self.speed_var = tk.StringVar(value="-")
        self.lock_keyboard_var = tk.BooleanVar(value=True)
        self.speed_testing = False
        self.speed_test_started = 0.0
        self.speed_test_progress = 0.0
        self.speed_phase = ""
        self.speed_results: dict[str, dict] = {}
        self.speed_samples: dict[str, list[float]] = {"up": [], "down": []}
        self.speed_down_sent = 0

        self._configure_style()
        self._build_ui()
        self._bind_events()

        threading.Thread(
            target=self._decode_loop,
            name="frame-decoder",
            daemon=True,
        ).start()

        self.root.protocol("WM_DELETE_WINDOW", self._on_close)
        self.root.after(50, self._poll_events)
        self.root.after(150, self._check_app_focus)
        self.root.after(300, self._connect)

    def _apply_window_size(self) -> None:
        """按系统缩放比例设置窗口大小,高分屏下界面不会被挤扁"""
        try:
            scale = self.root.winfo_fpixels("1i") / 96.0
        except Exception:
            scale = 1.0
        scale = min(max(scale, 1.0), 2.0)

        width, height = self.WINDOW_SIZE
        self.root.geometry(f"{int(width * scale)}x{int(height * scale)}")
        min_width, min_height = self.WINDOW_MIN_SIZE
        self.root.minsize(
            int(min_width * scale),
            int(min_height * scale),
        )

    def _configure_style(self) -> None:
        """沿用系统默认外观,只调整字体和控件间距"""
        style = ttk.Style(self.root)
        for theme_name in ("vista", "winnative", "aqua", "clam", "default"):
            if theme_name in style.theme_names():
                style.theme_use(theme_name)
                break

        style.configure(".", font=self.FONT_UI)
        style.configure("TButton", padding=(10, 4))
        style.configure("Toolbar.TButton", padding=(14, 5))
        style.configure("TEntry", padding=3)
        style.configure("TNotebook", tabmargins=(6, 5, 6, 0))
        style.configure("TNotebook.Tab", padding=(16, 6))

    def _build_ui(self) -> None:
        self.root.grid_rowconfigure(3, weight=1)
        self.root.grid_columnconfigure(0, weight=1)
        self._build_toolbar()
        self._build_stats_bar()
        self._build_body()
        self._build_status_bar()

    def _build_stats_bar(self) -> None:
        """画面统计条：实际带宽、JPEG大小、帧率和被控端的画质参数"""
        bar = tk.Frame(self.root, bg=self.BG)
        bar.grid(row=2, column=0, sticky="ew", padx=12, pady=(2, 6))

        column = self._add_stat(bar, 0, "带宽", self.bandwidth_var)
        column = self._add_stat(bar, column, "JPEG", self.frame_size_var)
        column = self._add_stat(bar, column, "帧率", self.fps_var)
        column = self._add_stat(
            bar,
            column,
            "被控端",
            self.remote_stats_var,
        )
        column = self._add_stat(bar, column, "压测", self.speed_var, last=True)
        bar.grid_columnconfigure(column, weight=1)

    def _add_stat(
        self,
        parent: tk.Widget,
        column: int,
        title: str,
        variable: tk.StringVar,
        last: bool = False,
    ) -> int:
        """
        统计条里加一项"标题 数值",返回下一个可用列
        :param parent:父容器
        :param column:起始列
        :param title:标题
        :param variable:数值
        :param last:是否是最后一项
        :return:下一个可用列
        """
        tk.Label(
            parent,
            text=title,
            bg=self.BG,
            fg=self.MUTED,
            font=self.FONT_UI,
        ).grid(row=0, column=column, padx=(0, 5), pady=2)
        tk.Label(
            parent,
            textvariable=variable,
            bg=self.BG,
            fg=self.TEXT,
            font=self.FONT_MONO,
        ).grid(row=0, column=column + 1, padx=(0, 12), pady=2)
        column += 2
        if last:
            return column
        ttk.Separator(parent, orient="vertical").grid(
            row=0,
            column=column,
            sticky="ns",
            padx=(0, 12),
            pady=2,
        )
        return column + 1

    def _build_toolbar(self) -> None:
        """顶部工具栏：标题、连接状态、远程地址和连接按钮"""
        top = tk.Frame(self.root, bg=self.BG)
        top.grid(row=0, column=0, sticky="ew")
        top.grid_columnconfigure(2, weight=1)

        tk.Label(
            top,
            text="远程桌面控制台",
            bg=self.BG,
            fg=self.TEXT,
            font=self.FONT_TITLE,
        ).grid(row=0, column=0, padx=(12, 14), pady=9)

        self.status_label = tk.Label(
            top,
            textvariable=self.status_var,
            bg=self.BG,
            fg=self.status_color,
            font=self.FONT_UI_BOLD,
        )
        self.status_label.grid(row=0, column=1, sticky="w")

        tk.Label(
            top,
            text="远程地址",
            bg=self.BG,
            fg=self.MUTED,
            font=self.FONT_UI,
        ).grid(row=0, column=3, padx=(12, 6))

        self.address_entry = ttk.Entry(
            top,
            textvariable=self.address_var,
            width=24,
            font=self.FONT_MONO,
        )
        self.address_entry.grid(row=0, column=4, pady=7)

        self.connect_button = ttk.Button(
            top,
            text="连接",
            style="Toolbar.TButton",
            command=self._toggle_connection,
        )
        self.connect_button.grid(row=0, column=5, padx=(8, 6), pady=7)

        self.speed_button = ttk.Button(
            top,
            text="带宽压测",
            style="Toolbar.TButton",
            command=self._toggle_speed_test,
        )
        self.speed_button.grid(row=0, column=6, padx=(0, 12), pady=7)

        ttk.Separator(self.root, orient="horizontal").grid(
            row=1,
            column=0,
            sticky="ew",
        )

    def _build_body(self) -> None:
        """主体区域：左边画面，右边可拖动的功能面板"""
        body = tk.Frame(self.root, bg=self.BG)
        body.grid(row=3, column=0, sticky="nsew")
        body.grid_rowconfigure(0, weight=1)
        body.grid_columnconfigure(0, weight=1)

        self.paned = ttk.PanedWindow(body, orient="horizontal")
        self.paned.grid(row=0, column=0, sticky="nsew", padx=8, pady=8)
        self.paned.add(self._build_screen_area(self.paned), weight=4)
        self.paned.add(self._build_sidebar(self.paned), weight=1)

    def _build_screen_area(self, parent: tk.Widget) -> tk.Widget:
        """远程画面区域,边框颜色跟随键盘状态变化"""
        screen_panel = tk.Frame(parent, bg=self.BG)
        screen_panel.grid_rowconfigure(0, weight=1)
        screen_panel.grid_columnconfigure(0, weight=1)

        self.screen_border = tk.Frame(
            screen_panel,
            bg=self.BORDER,
            padx=1,
            pady=1,
        )
        self.screen_border.grid(row=0, column=0, sticky="nsew")
        self.screen_border.grid_rowconfigure(0, weight=1)
        self.screen_border.grid_columnconfigure(0, weight=1)

        self.canvas = tk.Canvas(
            self.screen_border,
            bg=self.CANVAS,
            highlightthickness=0,
            takefocus=True,
            cursor="crosshair",
        )
        self.canvas.grid(row=0, column=0, sticky="nsew")
        self.placeholder_id = self.canvas.create_text(
            0,
            0,
            text="等待远程画面",
            fill="#a0a0a0",
            font=("Microsoft YaHei UI", 11),
        )
        return screen_panel

    def _build_sidebar(self, parent: tk.Widget) -> tk.Widget:
        """右侧功能面板：远程命令和被控端消息两个页签"""
        sidebar = tk.Frame(
            parent,
            bg=self.BG,
            width=self.SIDEBAR_WIDTH,
            height=self.SIDEBAR_HEIGHT,
        )
        sidebar.pack_propagate(False)

        self.notebook = ttk.Notebook(sidebar)
        self.notebook.pack(fill="both", expand=True)
        command_tab = self._build_command_tab()
        message_tab = self._build_message_tab()
        self.notebook.add(command_tab, text="远程命令")
        self.notebook.add(message_tab, text="被控端消息")
        self._message_tab_index = self.notebook.index(message_tab)
        self.notebook.bind(
            "<<NotebookTabChanged>>",
            self._on_notebook_tab_changed,
        )
        return sidebar

    def _build_command_tab(self) -> tk.Widget:
        """远程命令页：命令输出和命令输入"""
        tab = tk.Frame(self.notebook, bg=self.BG)
        tab.grid_rowconfigure(1, weight=1)
        tab.grid_columnconfigure(0, weight=1)

        header = tk.Frame(tab, bg=self.BG)
        header.grid(row=0, column=0, sticky="ew", padx=8, pady=(8, 4))
        header.grid_columnconfigure(0, weight=1)

        tk.Label(
            header,
            text="在被控端执行 CMD 命令",
            bg=self.BG,
            fg=self.MUTED,
            font=self.FONT_UI,
        ).grid(row=0, column=0, sticky="w")

        self.clear_button = ttk.Button(
            header,
            text="清空",
            command=self._clear_output,
        )
        self.clear_button.grid(row=0, column=1, sticky="e")

        self.output = scrolledtext.ScrolledText(
            tab,
            wrap="none",
            state="disabled",
            font=self.FONT_MONO,
            padx=6,
            pady=6,
            height=10,
        )
        self.output.grid(row=1, column=0, sticky="nsew", padx=8)

        output_scroll = ttk.Scrollbar(
            tab,
            orient="horizontal",
            command=self.output.xview,
        )
        output_scroll.grid(row=2, column=0, sticky="ew", padx=8)
        self.output.configure(xscrollcommand=output_scroll.set)

        command_row = tk.Frame(tab, bg=self.BG)
        command_row.grid(row=3, column=0, sticky="ew", padx=8, pady=8)
        command_row.grid_columnconfigure(0, weight=1)

        self.command_entry = ttk.Entry(
            command_row,
            font=self.FONT_MONO,
        )
        self.command_entry.grid(row=0, column=0, sticky="ew", padx=(0, 6))

        self.execute_button = ttk.Button(
            command_row,
            text="执行",
            command=self._send_command,
            state="disabled",
        )
        self.execute_button.grid(row=0, column=1)
        return tab

    def _build_message_tab(self) -> tk.Widget:
        """被控端消息页：被控端上报的错误会带着时间显示在这里"""
        tab = tk.Frame(self.notebook, bg=self.BG)
        tab.grid_rowconfigure(1, weight=1)
        tab.grid_columnconfigure(0, weight=1)

        header = tk.Frame(tab, bg=self.BG)
        header.grid(row=0, column=0, sticky="ew", padx=8, pady=(8, 4))
        header.grid_columnconfigure(0, weight=1)

        tk.Label(
            header,
            text="被控端出错时会在这里提示",
            bg=self.BG,
            fg=self.MUTED,
            font=self.FONT_UI,
        ).grid(row=0, column=0, sticky="w")

        self.message_clear_button = ttk.Button(
            header,
            text="清空",
            command=self._clear_messages,
        )
        self.message_clear_button.grid(row=0, column=1, sticky="e")

        self.message_output = scrolledtext.ScrolledText(
            tab,
            wrap="word",
            state="disabled",
            font=self.FONT_MONO,
            padx=6,
            pady=6,
            height=10,
        )
        self.message_output.grid(
            row=1,
            column=0,
            sticky="nsew",
            padx=8,
            pady=(0, 8),
        )
        return tab

    def _build_status_bar(self) -> None:
        """底部状态栏：键盘状态、键盘锁定开关和画面帧率"""
        ttk.Separator(self.root, orient="horizontal").grid(
            row=4,
            column=0,
            sticky="ew",
        )
        bar = tk.Frame(self.root, bg=self.BG)
        bar.grid(row=5, column=0, sticky="ew")
        bar.grid_columnconfigure(1, weight=1)

        self.keyboard_label = tk.Label(
            bar,
            textvariable=self.keyboard_var,
            bg=self.BG,
            fg=self.MUTED,
            anchor="w",
            font=self.FONT_UI,
        )
        self.keyboard_label.grid(
            row=0,
            column=0,
            sticky="w",
            padx=(12, 8),
            pady=5,
        )

        self.lock_check = tk.Checkbutton(
            bar,
            text="锁定本机键盘",
            variable=self.lock_keyboard_var,
            command=self._on_lock_toggle,
            bg=self.BG,
            fg=self.MUTED,
            activebackground=self.BG,
            activeforeground=self.TEXT,
            selectcolor=self.PANEL_ALT,
            highlightthickness=0,
            bd=0,
            takefocus=False,
            font=self.FONT_UI,
        )
        self.lock_check.grid(row=0, column=2, sticky="e", padx=(0, 12))

    def _bind_events(self) -> None:
        self.canvas.bind("<Configure>", self._on_canvas_resize)
        self.canvas.bind("<Motion>", self._on_mouse_move)
        self.canvas.bind("<Leave>", self._on_mouse_leave)
        self.canvas.bind("<ButtonPress>", self._on_mouse_press)
        self.canvas.bind("<ButtonRelease>", self._on_mouse_release)
        self.canvas.bind("<MouseWheel>", self._on_mouse_wheel)
        self.canvas.bind("<Button-4>", self._on_mouse_wheel_x11)
        self.canvas.bind("<Button-5>", self._on_mouse_wheel_x11)
        self.canvas.bind("<KeyPress>", self._on_key_press)
        self.canvas.bind("<KeyRelease>", self._on_key_release)
        self.command_entry.bind("<Return>", self._on_command_return)

        for widget in (
            self.address_entry,
            self.connect_button,
            self.clear_button,
            self.command_entry,
            self.execute_button,
        ):
            widget.bind("<FocusIn>", self._on_control_focus, add="+")

        self.root.bind("<FocusOut>", self._on_root_focus_out, add="+")

    def _toggle_connection(self) -> None:
        if self.client.connected:
            self.client.disconnect()
            self._set_disconnected_ui("已断开")
            return
        self._connect()

    def _connect(self) -> None:
        address = self.address_var.get().strip()
        try:
            host, port_text = address.rsplit(":", 1)
            port = int(port_text)
            if not host or not 1 <= port <= 65535:
                raise ValueError
        except ValueError:
            self._set_status("地址格式错误", "black")
            self._append_output(
                f"地址格式错误：{address or '(空)'}\n"
                "正确格式：frp-sea.com:24801\n"
            )
            return

        self._disable_keyboard_mode(send_release=False)
        self._set_status("连接中", "black")
        self.connect_button.configure(state="disabled")
        self.address_entry.configure(state="disabled")
        threading.Thread(
            target=self._connect_worker,
            args=(host, port),
            name="remote-connect",
            daemon=True,
        ).start()

    def _connect_worker(self, host: str, port: int) -> None:
        self.client.connect(host, port)
        self.events.put(("connected", self.client.connected))

    def _set_status(self, text: str, color: str) -> None:
        self.status_var.set(f"● {text}")
        self.status_color = color
        self.status_label.configure(fg=color)

    def _set_disconnected_ui(self, status: str | None = None) -> None:
        self._disable_keyboard_mode()
        self.connect_button.configure(state="normal", text="连接")
        self.address_entry.configure(state="normal")
        self.execute_button.configure(state="disabled")
        self._clear_queue(self.frames)
        self._clear_queue(self.decoded_frames)
        self._reset_stats_display()
        if status is not None:
            self._set_status(
                status,
                "red" if "断" in status else self.MUTED,
            )

    def _reset_stats_display(self) -> None:
        """断开连接后把带宽和画面统计归零"""
        self.bandwidth_var.set("-")
        self.frame_size_var.set("-")
        self.remote_stats_var.set("-")
        self.speed_var.set("-")
        self.fps_var.set("0 FPS")
        if self.speed_testing:
            self.speed_testing = False
            self.speed_button.configure(text="带宽压测")

    def _poll_events(self) -> None:
        try:
            while True:
                event = self.events.get_nowait()
                event_type = event[0]

                if event_type == "command":
                    self._append_output(event[1] + "\n")
                elif event_type == "keyboard_lock":
                    self._handle_keyboard_lock_event(event)
                elif event_type == "status":
                    self._set_status(event[1], event[2])
                elif event_type == "error":
                    self._append_output(f"[错误] {event[1]}\n")
                elif event_type == "remote_message":
                    self._show_remote_message(event[1])
                elif event_type == "bandwidth":
                    self._update_bandwidth(event[1])
                elif event_type == "remote_stats":
                    self._update_remote_stats(event[1])
                elif event_type == "speed_progress":
                    self._on_speed_progress(event[1])
                elif event_type == "speed_test":
                    self._on_speed_event(event[1])
                elif event_type == "connected":
                    connected = bool(event[1])
                    if connected:
                        self.connect_button.configure(
                            state="normal",
                            text="断开",
                        )
                        self.address_entry.configure(state="normal")
                        self.execute_button.configure(state="normal")
                        self._append_output(
                            f"[已连接] {self.address_var.get()}\n"
                        )
                    else:
                        self._set_disconnected_ui()
        except queue.Empty:
            pass

        self._display_latest_frame()
        self._check_speed_test_timeout()
        self.root.after(20, self._poll_events)

    def _handle_keyboard_lock_event(self, event: tuple) -> None:
        """键盘钩子线程请求解除锁定时由主线程收尾。"""
        if event[1] or not self.keyboard_lock_active:
            return

        reason = event[2] if len(event) > 2 else ""
        self.keyboard_lock_active = False
        self.keyboard_lock.stop()
        self._disable_keyboard_mode()
        self._append_output(
            f"[键盘] 本机键盘已恢复（{reason}）。\n"
        )

    def _decode_loop(self) -> None:
        while not self.decode_stop.is_set():
            try:
                data = self.frames.get(timeout=0.1)
            except queue.Empty:
                continue

            try:
                image = Image.open(BytesIO(data))
                image.load()
                if image.mode != "RGB":
                    image = image.convert("RGB")

                with self.display_size_lock:
                    canvas_width, canvas_height = self.display_size
                canvas_width = max(canvas_width, 1)
                canvas_height = max(canvas_height, 1)
                if canvas_width > 1 and canvas_height > 1:
                    scale = min(
                        canvas_width / max(image.width, 1),
                        canvas_height / max(image.height, 1),
                    )
                    display_size = (
                        max(1, int(image.width * scale)),
                        max(1, int(image.height * scale)),
                    )
                else:
                    display_size = image.size
                if image.size != display_size:
                    image = image.resize(display_size, DISPLAY_RESAMPLE)
                self._put_latest(self.decoded_frames, image)
            except Exception as exc:
                try:
                    self.events.put_nowait(
                        ("error", f"画面解码失败：{exc}")
                    )
                except queue.Full:
                    pass

    def _display_latest_frame(self) -> None:
        try:
            display_image = None
            while True:
                display_image = self.decoded_frames.get_nowait()
        except queue.Empty:
            if display_image is None:
                return

        canvas_width = max(self.canvas.winfo_width(), 1)
        canvas_height = max(self.canvas.winfo_height(), 1)
        image_width, image_height = display_image.size
        scale = min(
            canvas_width / image_width,
            canvas_height / image_height,
        )
        target_size = (
            max(1, int(image_width * scale)),
            max(1, int(image_height * scale)),
        )
        if display_image.size != target_size:
            display_image = display_image.resize(
                target_size,
                DISPLAY_RESAMPLE,
            )

        display_width, display_height = display_image.size
        x = (canvas_width - display_width) // 2
        y = (canvas_height - display_height) // 2

        self.photo_image = ImageTk.PhotoImage(display_image)
        self.canvas.delete("remote_frame")
        self.canvas.create_image(
            x,
            y,
            image=self.photo_image,
            anchor="nw",
            tags="remote_frame",
        )
        self.canvas.tag_lower("remote_frame")
        self.canvas.itemconfigure(self.placeholder_id, state="hidden")
        self.display_box = (x, y, display_width, display_height)

        now = time.monotonic()
        self.frame_times.append(now)
        while self.frame_times and now - self.frame_times[0] > 1.5:
            self.frame_times.pop(0)
        if len(self.frame_times) >= 2:
            duration = self.frame_times[-1] - self.frame_times[0]
            fps = (len(self.frame_times) - 1) / max(duration, 0.001)
            self.fps_var.set(f"{fps:.1f} FPS")

    @staticmethod
    def _put_latest(target: queue.Queue, item) -> None:
        try:
            target.put_nowait(item)
            return
        except queue.Full:
            pass

        try:
            target.get_nowait()
        except queue.Empty:
            return

        try:
            target.put_nowait(item)
        except queue.Full:
            pass

    @staticmethod
    def _clear_queue(target: queue.Queue) -> None:
        try:
            while True:
                target.get_nowait()
        except queue.Empty:
            pass

    def _on_canvas_resize(self, _event: tk.Event) -> None:
        width = max(self.canvas.winfo_width(), 1)
        height = max(self.canvas.winfo_height(), 1)
        with self.display_size_lock:
            self.display_size = (width, height)
        self.canvas.coords(self.placeholder_id, width // 2, height // 2)

    def _canvas_to_ratio(self, x: int, y: int) -> tuple[float, float] | None:
        left, top, width, height = self.display_box
        if width <= 0 or height <= 0:
            return None
        if not (
            left <= x < left + width
            and top <= y < top + height
        ):
            return None

        x_ratio = (x - left) / max(width - 1, 1)
        y_ratio = (y - top) / max(height - 1, 1)
        return x_ratio, y_ratio

    def _flush_pending_mouse(self) -> None:
        self.mouse_flush_job = None
        if self.pending_mouse is None or not self.client.connected:
            return

        x_ratio, y_ratio = self.pending_mouse
        self.pending_mouse = None
        self.client.send_json(
            TYPE_MOUSE,
            {
                "action": "move",
                "x": x_ratio,
                "y": y_ratio,
            },
        )
        self.last_mouse_send = time.monotonic()

    def _on_mouse_move(self, event: tk.Event) -> str:
        if not self.client.connected:
            return "break"

        ratio = self._canvas_to_ratio(event.x, event.y)
        if ratio is None:
            return "break"

        self.pending_mouse = ratio
        elapsed = time.monotonic() - self.last_mouse_send
        if elapsed >= 0.012:
            self._flush_pending_mouse()
        elif self.mouse_flush_job is None:
            delay = max(1, int((0.012 - elapsed) * 1000))
            self.mouse_flush_job = self.root.after(
                delay,
                self._flush_pending_mouse,
            )
        return "break"

    def _on_mouse_leave(self, _event: tk.Event) -> str:
        self.pending_mouse = None
        if self.mouse_flush_job is not None:
            self.root.after_cancel(self.mouse_flush_job)
            self.mouse_flush_job = None
        self._release_all_mouse_buttons()
        return "break"

    def _button_name(self, event: tk.Event) -> str | None:
        return {
            1: "left",
            2: "middle",
            3: "right",
        }.get(event.num)

    def _on_mouse_press(self, event: tk.Event) -> str:
        self.canvas.focus_set()
        if self.display_box[2] > 0:
            self._enable_keyboard_mode()

        button = self._button_name(event)
        if button is None or not self.client.connected:
            return "break"

        self.pressed_mouse_buttons.add(button)
        self.client.send_json(
            TYPE_MOUSE,
            {"action": "press", "button": button},
        )
        return "break"

    def _on_mouse_release(self, event: tk.Event) -> str:
        button = self._button_name(event)
        if button is None:
            return "break"

        self.pressed_mouse_buttons.discard(button)
        if self.client.connected:
            self.client.send_json(
                TYPE_MOUSE,
                {"action": "release", "button": button},
            )
        return "break"

    def _release_all_mouse_buttons(self) -> None:
        if self.client.connected:
            for button in tuple(self.pressed_mouse_buttons):
                self.client.send_json(
                    TYPE_MOUSE,
                    {"action": "release", "button": button},
                )
        self.pressed_mouse_buttons.clear()

    def _on_mouse_wheel(self, event: tk.Event) -> str:
        if not self.client.connected:
            return "break"
        delta = event.delta
        if delta == 0:
            return "break"
        steps = int(delta / 120) if abs(delta) >= 120 else (1 if delta > 0 else -1)
        self.client.send_json(
            TYPE_MOUSE,
            {"action": "scroll", "dx": 0, "dy": steps},
        )
        return "break"

    def _on_mouse_wheel_x11(self, event: tk.Event) -> str:
        if not self.client.connected:
            return "break"
        steps = 1 if event.num == 4 else -1
        self.client.send_json(
            TYPE_MOUSE,
            {"action": "scroll", "dx": 0, "dy": steps},
        )
        return "break"

    def _event_to_remote_key(self, event: tk.Event) -> str | None:
        keysym = event.keysym
        if keysym in self.SPECIAL_KEYS:
            return self.SPECIAL_KEYS[keysym]
        if keysym in self.PUNCTUATION_KEYS:
            return self.PUNCTUATION_KEYS[keysym]
        if len(keysym) == 1 and keysym.isascii():
            return keysym.lower()
        return None

    def _on_key_press(self, event: tk.Event) -> str:
        if not self.keyboard_enabled:
            return "break"
        if self.keyboard_lock_active:
            # 按键已经由全局键盘钩子发送，这里不再重复发送
            return "break"

        key = self._event_to_remote_key(event)
        if key is None:
            return "break"

        self.pressed_remote_keys.add(key)
        self.client.send_json(
            TYPE_KEYBOARD,
            {"action": "press", "key": key},
        )
        return "break"

    def _on_key_release(self, event: tk.Event) -> str:
        if not self.keyboard_enabled:
            return "break"
        if self.keyboard_lock_active:
            return "break"

        key = self._event_to_remote_key(event)
        if key is None:
            return "break"

        self.pressed_remote_keys.discard(key)
        self.client.send_json(
            TYPE_KEYBOARD,
            {"action": "release", "key": key},
        )
        return "break"

    def _enable_keyboard_mode(self) -> None:
        if not self.client.connected:
            return
        self.keyboard_enabled = True
        if self.lock_keyboard_var.get():
            self._start_keyboard_lock()
        self._refresh_keyboard_indicator()

    def _disable_keyboard_mode(self, send_release: bool = True) -> None:
        self._stop_keyboard_lock()
        if send_release and self.client.connected:
            for key in tuple(self.pressed_remote_keys):
                self.client.send_json(
                    TYPE_KEYBOARD,
                    {"action": "release", "key": key},
                )
            self._release_all_mouse_buttons()

        self.pressed_remote_keys.clear()
        self.keyboard_enabled = False
        self._refresh_keyboard_indicator()

    def _refresh_keyboard_indicator(self) -> None:
        """刷新键盘状态提示和边框颜色。"""
        if not self.keyboard_enabled:
            self.keyboard_var.set("键盘：本机")
            self.keyboard_label.configure(fg=self.MUTED)
            self.lock_check.configure(fg=self.MUTED)
            self.screen_border.configure(bg=self.BORDER)
        elif self.keyboard_lock_active:
            self.keyboard_var.set("键盘：远程（本机已锁定）")
            self.keyboard_label.configure(fg=self.LOCKED)
            self.lock_check.configure(fg=self.LOCKED)
            self.screen_border.configure(bg=self.LOCKED)
        else:
            self.keyboard_var.set("键盘：远程")
            self.keyboard_label.configure(fg=self.ACCENT)
            self.lock_check.configure(fg=self.MUTED)
            self.screen_border.configure(bg=self.ACCENT)

    def _start_keyboard_lock(self) -> None:
        """锁定本机键盘，让按键只发送到被控端。"""
        if self.keyboard_lock_active or not self.client.connected:
            return
        if not KeyboardLock.supported():
            self._append_output(
                "[提示] 当前系统不支持全局键盘锁定，"
                "仅在窗口内转发键盘输入。\n"
            )
            return

        if self.keyboard_lock.start():
            self.keyboard_lock_active = True
            self._append_output(
                "[键盘] 本机键盘已锁定：输入只发送到被控端，"
                "点击窗口外的任意位置即可恢复本机键盘"
                "（Ctrl+Alt+Shift+Q 可强制解锁）。\n"
            )
        else:
            self._append_output(
                "[提示] 本机键盘锁定未生效，仅在窗口内转发键盘输入。\n"
            )

    def _stop_keyboard_lock(self) -> None:
        if not self.keyboard_lock_active:
            return
        self.keyboard_lock_active = False
        self.keyboard_lock.stop()

    def _on_lock_toggle(self) -> None:
        if not self.lock_keyboard_var.get():
            self._stop_keyboard_lock()
            self._refresh_keyboard_indicator()
            self._append_output(
                "[键盘] 已关闭本机键盘锁定，仅在窗口内转发键盘输入。\n"
            )
            return

        if self.keyboard_enabled:
            self._start_keyboard_lock()
        self._refresh_keyboard_indicator()

    def _on_control_focus(self, _event: tk.Event) -> None:
        self._disable_keyboard_mode()

    def _on_root_focus_out(self, _event: tk.Event) -> None:
        self.root.after(40, self._check_app_focus)

    def _app_still_focused(self) -> bool:
        """本程序窗口当前是否仍处于前台。"""
        if _USER32 is not None:
            return foreground_belongs_to_self()
        return self.root.focus_displayof() is not None

    def _check_app_focus(self) -> None:
        if self.keyboard_enabled and not self._app_still_focused():
            self._disable_keyboard_mode()
        self.root.after(150, self._check_app_focus)

    def _on_command_return(self, _event: tk.Event) -> str:
        self._send_command()
        return "break"

    def _send_command(self) -> None:
        command = self.command_entry.get().strip()
        if not command:
            return
        if not self.client.connected:
            self._append_output("[错误] 尚未连接远程端\n")
            return

        if self.client.send_packet(
            TYPE_COMMAND,
            command.encode("utf-8"),
        ):
            self._append_output(f"> {command}\n")
            self.command_entry.delete(0, "end")

    def _append_output(self, text: str) -> None:
        self._append_text(self.output, text)

    def _clear_output(self) -> None:
        self._clear_text(self.output)

    def _append_text(self, widget: scrolledtext.ScrolledText, text: str) -> None:
        """向只读文本框追加内容,并限制最大行数"""
        widget.configure(state="normal")
        widget.insert("end", text)
        lines = int(widget.index("end-1c").split(".")[0])
        if lines > self.TEXT_LIMIT:
            widget.delete("1.0", f"{lines - self.TEXT_LIMIT}.0")
        widget.see("end")
        widget.configure(state="disabled")

    @staticmethod
    def _clear_text(widget: scrolledtext.ScrolledText) -> None:
        """清空只读文本框"""
        widget.configure(state="normal")
        widget.delete("1.0", "end")
        widget.configure(state="disabled")

    def _now_text(self) -> str:
        """本机当前时间文本"""
        return time.strftime(TIME_FORMAT)

    def _show_remote_message(self, data: bytes) -> None:
        """显示被控端上报的错误信息,时间由被控端提供"""
        payload: dict = {}
        try:
            loaded = json.loads(data.decode("utf-8"))
            if isinstance(loaded, dict):
                payload = loaded
        except (UnicodeDecodeError, ValueError):
            payload = {}

        timestamp = str(payload.get("time") or self._now_text())
        source = str(payload.get("source") or "被控端")
        message = str(
            payload.get("message")
            or data.decode("utf-8", errors="replace")
        )
        self._append_text(
            self.message_output,
            f"[{timestamp}] [{source}] {message}\n",
        )
        self._notify_message()

    def _notify_message(self) -> None:
        """没在看消息页时,在页签上标出未读数量"""
        if self.notebook.index(self.notebook.select()) == self._message_tab_index:
            return
        self.message_count += 1
        self.notebook.tab(
            self._message_tab_index,
            text=f"被控端消息 ({self.message_count})",
        )

    def _update_bandwidth(self, payload: dict) -> None:
        """
        显示控制端实测的带宽和JPEG大小
        :param payload:统计数据
        :return:None
        """
        bitrate = float(payload.get("bitrate") or 0.0)
        frame_size = int(payload.get("frame_size") or 0)
        self.bandwidth_var.set(
            f"{bitrate / 1_000_000:.2f} Mbps"
            f"（{bitrate / 8 / 1_000_000:.2f} MB/s）"
        )
        self.frame_size_var.set(f"{frame_size / 1024:.1f} KB")

    def _update_remote_stats(self, data: bytes) -> None:
        """
        显示被控端上报的画质、画面宽度和它自己实测的帧率
        :param data:被控端上报的统计数据
        :return:None
        """
        try:
            stats = json.loads(data.decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            return
        if not isinstance(stats, dict):
            return

        parts = []
        if stats.get("quality") is not None:
            parts.append(f"画质{stats['quality']}")
        if stats.get("width") is not None:
            parts.append(f"{stats['width']}px")
        if stats.get("fps") is not None:
            parts.append(f"{float(stats['fps']):.1f}帧")
        self.remote_stats_var.set(" · ".join(parts) if parts else "-")

    def _toggle_speed_test(self) -> None:
        """点按钮开始或停止隧道带宽压测"""
        if self.speed_testing:
            self._stop_speed_test()
            return
        if not self.client.connected:
            self._append_output("[提示] 尚未连接被控端，无法压测。\n")
            return

        self.speed_testing = True
        self.speed_phase = "up"
        self.speed_results = {}
        self.speed_samples = {"up": [], "down": []}
        self.speed_down_sent = 0
        self.speed_test_started = time.monotonic()
        self.speed_test_progress = self.speed_test_started
        self.speed_button.configure(text="停止压测")
        self.speed_var.set("上行压测中…")
        self._append_output(
            f"[压测] 上行(被控端→本机) {SPEED_TEST_SECONDS:.0f} 秒（暂停画面）\n"
        )
        self.client.send_json(
            TYPE_SPEED_TEST,
            {
                "action": "start",
                "direction": "up",
                "seconds": SPEED_TEST_SECONDS,
                "chunk_size": SPEED_TEST_CHUNK,
            },
        )

    def _stop_speed_test(self) -> None:
        """请求被控端停止压测"""
        if not self.speed_testing:
            return
        self.speed_testing = False
        self.speed_phase = ""
        self.speed_button.configure(text="带宽压测")
        self.speed_var.set("-")
        if self.client.connected:
            self.client.send_json(
                TYPE_SPEED_TEST,
                {"action": "stop", "direction": "up"},
            )
            self.client.send_json(
                TYPE_SPEED_TEST,
                {"action": "stop", "direction": "down"},
            )
        self._append_output("[压测] 已请求停止。\n")

    def _on_speed_progress(self, payload: dict) -> None:
        """
        压测进行中,刷新统计条上的进度
        :param payload:压测进度
        :return:None
        """
        if not self.speed_testing:
            return
        direction = str(payload.get("direction") or "up")
        seconds = float(payload.get("seconds") or 0.0)
        bitrate = float(payload.get("bitrate") or 0.0)
        self._sample_speed(
            direction,
            seconds,
            float(payload.get("bytes") or 0.0),
        )
        self.speed_test_progress = time.monotonic()
        label = "上行" if direction == "up" else "下行"
        self.speed_var.set(
            f"{label} {seconds:>4.1f}s {bitrate / 1e6:>6.1f} Mbps"
        )

    def _sample_speed(
        self,
        direction: str,
        seconds: float,
        total_bytes: float,
    ) -> None:
        """
        每秒记一次累计字节数,用来画速率曲线
        :param direction:压测方向
        :param seconds:已经过去的秒数
        :param total_bytes:累计字节数
        :return:None
        """
        samples = self.speed_samples.setdefault(direction, [])
        second = int(seconds)
        if second < len(samples):
            return
        while len(samples) < second:
            samples.append(samples[-1] if samples else 0.0)
        samples.append(total_bytes)

    def _speed_curve(self, direction: str) -> tuple[str, float]:
        """
        按每秒窗口算出速率曲线和峰值
        :param direction:压测方向
        :return:(曲线文本, 峰值Mbps)
        """
        samples = self.speed_samples.get(direction) or []
        rates = [
            (samples[index] - samples[index - 1]) * 8 / 1e6
            for index in range(1, len(samples))
        ]
        if not rates:
            return "", 0.0
        text = " ".join(f"{rate:.0f}" for rate in rates[:14])
        return text, max(rates)

    def _on_speed_event(self, payload: dict) -> None:
        """
        处理被控端的压测开始和结束消息
        :param payload:被控端消息
        :return:None
        """
        action = payload.get("action")
        if action == "started":
            if self.speed_testing:
                self.speed_test_progress = time.monotonic()
                direction = str(payload.get("direction") or "up")
                self.speed_var.set(
                    "上行压测中…" if direction == "up" else "下行压测中…"
                )
            return
        if action != "finished":
            return

        direction = str(payload.get("direction") or "up")
        self.speed_results[direction] = payload
        if direction == "up":
            # 上行测完接着测下行(本机 -> 被控端)
            self._start_speed_down()
            return
        self._finish_speed_test()

    def _start_speed_down(self) -> None:
        """开始反向压测:控制端往被控端灌数据"""
        if not self.speed_testing or not self.client.connected:
            self._finish_speed_test()
            return
        self.speed_phase = "down"
        self.speed_test_started = time.monotonic()
        self.speed_test_progress = self.speed_test_started
        self.speed_var.set("下行压测中…")
        self._append_output(
            f"[压测] 下行(本机→被控端) {SPEED_TEST_SECONDS:.0f} 秒\n"
        )
        self.client.send_json(
            TYPE_SPEED_TEST,
            {
                "action": "start",
                "direction": "down",
                "seconds": SPEED_TEST_SECONDS,
                "chunk_size": SPEED_TEST_CHUNK,
            },
        )
        threading.Thread(
            target=self._send_speed_data,
            name="speed-test-down",
            daemon=True,
        ).start()

    def _send_speed_data(self) -> None:
        """反向压测:控制端不停往被控端发数据,发完让被控端统计结果"""
        block = os.urandom(SPEED_TEST_CHUNK)
        sent = 0
        started = time.monotonic()
        last_emit = started
        while time.monotonic() - started < SPEED_TEST_SECONDS:
            if not self.client.connected or not self.speed_testing:
                break
            if not self.client.send_packet(TYPE_SPEED_DATA, block):
                break
            sent += len(block) + PACKET_HEAD_SIZE
            now = time.monotonic()
            if now - last_emit < 0.2:
                continue
            last_emit = now
            elapsed = max(now - started, 0.001)
            self._put_event("speed_progress", {
                "direction": "down",
                "seconds": elapsed,
                "bytes": sent,
                "bitrate": sent * 8 / elapsed,
            })
        self.speed_down_sent = sent
        if self.client.connected:
            self.client.send_json(
                TYPE_SPEED_TEST,
                {"action": "stop", "direction": "down"},
            )

    def _put_event(self, name: str, payload: dict) -> None:
        """
        从工作线程往界面线程投递事件
        :param name:事件名
        :param payload:事件内容
        :return:None
        """
        try:
            self.events.put_nowait((name, payload))
        except queue.Full:
            pass

    def _finish_speed_test(self) -> None:
        """两段压测都结束,汇总显示结果"""
        self.speed_testing = False
        self.speed_phase = ""
        self.speed_button.configure(text="带宽压测")

        up = self.speed_results.get("up") or {}
        down = self.speed_results.get("down") or {}
        up_curve, up_peak = self._speed_curve("up")
        down_curve, down_peak = self._speed_curve("down")

        up_bitrate = float(up.get("receive_bitrate") or 0.0)
        # 峰值取所有统计窗口里最高的那个(统一换成 bps),不会低于平均值
        up_peak = max(
            up_peak * 1e6,
            float(up.get("receive_peak") or 0.0),
            up_bitrate,
        )
        up_seconds = float(up.get("receive_seconds") or 0.0)
        up_received = int(up.get("received_bytes") or 0)
        up_sent = int(up.get("sent_bytes") or 0)
        up_remote = float(up.get("bitrate") or 0.0)

        down_sent = int(self.speed_down_sent)
        down_seconds = float(down.get("seconds") or 0.0)
        down_received = int(down.get("received_bytes") or 0)
        down_bitrate = (
            down_sent * 8 / down_seconds if down_seconds else 0.0
        )
        down_peak = max(down_peak * 1e6, down_bitrate)

        self.speed_var.set(
            f"上 {up_bitrate / 1e6:.1f} / 下 {down_bitrate / 1e6:.1f} Mbps"
        )
        self._append_output(
            f"[压测] 上行 {up_bitrate / 1e6:.1f} Mbps"
            f"（峰值 {up_peak / 1e6:.1f}）\n"
            f"[压测] 上行曲线 {up_curve or '-'}\n"
            f"[压测] 下行 {down_bitrate / 1e6:.1f} Mbps"
            f"（峰值 {down_peak / 1e6:.1f}）\n"
            f"[压测] 下行曲线 {down_curve or '-'}\n"
        )
        messagebox.showinfo(
            "隧道带宽压测结果",
            f"上行（被控端 → 本机）\n"
            f"  平均 {up_bitrate / 1e6:.2f} Mbps"
            f"（{up_bitrate / 8 / 1e6:.2f} MB/s），"
            f"峰值 {up_peak / 1e6:.1f} Mbps\n"
            f"  用时 {up_seconds:.1f} 秒，收到 {up_received / 1048576:.1f} MB，"
            f"被控端发出 {up_sent / 1048576:.1f} MB（{up_remote / 1e6:.1f} Mbps）\n"
            f"  每秒速率(Mbps)：{up_curve or '-'}\n\n"
            f"下行（本机 → 被控端）\n"
            f"  平均 {down_bitrate / 1e6:.2f} Mbps"
            f"（{down_bitrate / 8 / 1e6:.2f} MB/s），"
            f"峰值 {down_peak / 1e6:.1f} Mbps\n"
            f"  用时 {down_seconds:.1f} 秒，本机发出 {down_sent / 1048576:.1f} MB，"
            f"被控端收到 {down_received / 1048576:.1f} MB\n"
            f"  每秒速率(Mbps)：{down_curve or '-'}",
            parent=self.root,
        )

    def _check_speed_test_timeout(self) -> None:
        """压测迟迟没有结果时给个提示(例如被控端还是旧版本)"""
        if not self.speed_testing:
            return
        if time.monotonic() - self.speed_test_progress < SPEED_TEST_SECONDS + 4:
            return
        self.speed_testing = False
        self.speed_phase = ""
        self.speed_button.configure(text="带宽压测")
        self.speed_var.set("无响应")
        self._append_output(
            "[压测] 被控端没有返回结果，请确认被控端也已经更新到最新版本。\n"
        )

    def _on_notebook_tab_changed(self, _event: tk.Event) -> None:
        """切到被控端消息页时清掉未读数量"""
        if self.notebook.index(self.notebook.select()) != self._message_tab_index:
            return
        self.message_count = 0
        self.notebook.tab(self._message_tab_index, text="被控端消息")

    def _clear_messages(self) -> None:
        self._clear_text(self.message_output)
        self.message_count = 0
        self.notebook.tab(self._message_tab_index, text="被控端消息")

    def _on_close(self) -> None:
        self._disable_keyboard_mode()
        self.decode_stop.set()
        self.client.disconnect(notify=False)
        self.root.destroy()


def main() -> None:
    enable_dpi_awareness()
    root = tk.Tk()
    RemoteDesktopApp(root)
    root.mainloop()


if __name__ == "__main__":
    main()
