import tkinter as tk
from tkinter import ttk, messagebox
import threading
import time
import re
import shutil
import os
from pathlib import Path
from datetime import datetime

import cv2
import mss
import numpy as np
import pytesseract
import requests
from pynput import keyboard
import ctypes
from ctypes import wintypes

CAPTURES = Path("captures")

def configure_tesseract():
    """Find the Windows Tesseract executable automatically."""
    candidates = [
        shutil.which("tesseract"),
        r"C:\Program Files\Tesseract-OCR\tesseract.exe",
        r"C:\Program Files (x86)\Tesseract-OCR\tesseract.exe",
        str(Path(os.environ.get("LOCALAPPDATA", "")) / "Programs" / "Tesseract-OCR" / "tesseract.exe"),
    ]
    for candidate in candidates:
        if candidate and Path(candidate).is_file():
            pytesseract.pytesseract.tesseract_cmd = str(candidate)
            return str(candidate)
    return None


TESSERACT_PATH = configure_tesseract()


# Windows keyboard input fallback: some games do not accept pynput events.
ULONG_PTR = ctypes.c_ulonglong if ctypes.sizeof(ctypes.c_void_p) == 8 else ctypes.c_ulong


class KEYBDINPUT(ctypes.Structure):
    _fields_ = [
        ("wVk", wintypes.WORD),
        ("wScan", wintypes.WORD),
        ("dwFlags", wintypes.DWORD),
        ("time", wintypes.DWORD),
        ("dwExtraInfo", ULONG_PTR),
    ]


class MOUSEINPUT(ctypes.Structure):
    _fields_ = [
        ("dx", wintypes.LONG),
        ("dy", wintypes.LONG),
        ("mouseData", wintypes.DWORD),
        ("dwFlags", wintypes.DWORD),
        ("time", wintypes.DWORD),
        ("dwExtraInfo", ULONG_PTR),
    ]


class HARDWAREINPUT(ctypes.Structure):
    _fields_ = [
        ("uMsg", wintypes.DWORD),
        ("wParamL", wintypes.WORD),
        ("wParamH", wintypes.WORD),
    ]


class INPUT_UNION(ctypes.Union):
    _fields_ = [
        ("mi", MOUSEINPUT),
        ("ki", KEYBDINPUT),
        ("hi", HARDWAREINPUT),
    ]


class INPUT(ctypes.Structure):
    _anonymous_ = ("u",)
    _fields_ = [("type", wintypes.DWORD), ("u", INPUT_UNION)]


def send_input(event_type, union_value):
    event = INPUT()
    event.type = event_type
    if event_type == 1:
        event.ki = union_value
    else:
        event.mi = union_value
    user32 = ctypes.WinDLL("user32", use_last_error=True)
    user32.SendInput.argtypes = (wintypes.UINT, ctypes.POINTER(INPUT), ctypes.c_int)
    user32.SendInput.restype = wintypes.UINT
    sent = user32.SendInput(1, ctypes.byref(event), ctypes.sizeof(INPUT))
    if sent != 1:
        error = ctypes.get_last_error()
        raise OSError(error, ctypes.FormatError(error) if error else "SendInput не принял событие")


def send_w_key(key_up=False):
    flags = 0x0002 if key_up else 0
    send_input(1, KEYBDINPUT(
        wVk=0x57, wScan=0, dwFlags=flags, time=0, dwExtraInfo=0
    ))


def send_left_mouse(key_up=False):
    flags = 0x0004 if key_up else 0x0002
    send_input(0, MOUSEINPUT(
        dx=0, dy=0, mouseData=0, dwFlags=flags, time=0, dwExtraInfo=0
    ))

def send_f3_key(key_up=False):
    """Send F3 key press/release to the foreground game window."""
    flags = 0x0002 if key_up else 0
    send_input(1, KEYBDINPUT(
        wVk=0x72, wScan=0, dwFlags=flags, time=0, dwExtraInfo=0
    ))
CAPTURES.mkdir(exist_ok=True)


class Observer:
    def __init__(self, root):
        self.root = root
        self.root.title("FunTime ESP Observer")
        self.root.geometry("560x500")
        self.root.minsize(520, 430)
        self.running = False
        self.last_alert = 0
        self.token = tk.StringVar()
        self.chat = tk.StringVar()
        self.threshold = tk.IntVar(value=180)
        self.cooldown = tk.IntVar(value=45)
        self.attach = tk.BooleanVar(value=True)
        self.config = {}
        self.forward_active = False
        self.mine_active = False
        self.repair_watch_enabled = True
        self.repair_watch_running = True
        self.last_durability_notice = 0
        self.hotkey_listener = keyboard.GlobalHotKeys({
            "<f6>": self.toggle_forward,
            "<f7>": self.toggle_mine,
            "<f8>": self.stop_all,
            "<f9>": self.toggle_repair_watch,
        })
        self.hotkey_listener.start()
        self.build()
        self.root.protocol("WM_DELETE_WINDOW", self.close)
        self.refresh_game_window()
        self.root.after(3000, self.poll_game_window)
        threading.Thread(target=self.repair_watch_loop, daemon=True).start()

    def build(self):
        f = ttk.Frame(self.root, padding=14)
        f.pack(fill="both", expand=True)
        ttk.Label(f, text="FunTime ESP Observer", font=("Segoe UI", 16, "bold")).pack(anchor="w")
        ttk.Label(
            f,
            text="ESP-мониторинг, Telegram и простой режим движения вперёд."
        ).pack(anchor="w", pady=(4, 12))

        ttk.Label(f, text="Telegram Bot Token").pack(anchor="w")
        token_entry = ttk.Entry(f, textvariable=self.token, show="*")
        token_entry.pack(fill="x", pady=(2, 8))
        self.enable_paste(token_entry)
        ttk.Label(f, text="Telegram Chat ID").pack(anchor="w")
        chat_entry = ttk.Entry(f, textvariable=self.chat)
        chat_entry.pack(fill="x", pady=(2, 8))
        self.enable_paste(chat_entry)

        line = ttk.Frame(f)
        line.pack(fill="x", pady=6)
        ttk.Label(line, text="Порог ESP-пикселей").pack(side="left")
        ttk.Spinbox(line, from_=20, to=5000, textvariable=self.threshold, width=8).pack(side="left", padx=6)
        ttk.Label(line, text="Пауза (сек.)").pack(side="left", padx=(12, 0))
        ttk.Spinbox(line, from_=5, to=3600, textvariable=self.cooldown, width=8).pack(side="left", padx=6)

        ttk.Checkbutton(f, text="Прикреплять скриншот", variable=self.attach).pack(anchor="w", pady=6)

        movement = ttk.LabelFrame(f, text="Управление движением", padding=8)
        movement.pack(fill="x", pady=6)
        ttk.Label(
            movement,
            text="F6 — идти вперёд (W), F7 — удерживать ЛКМ, F8 — отпустить W и ЛКМ, F9 — контроль прочности. Кирка должна быть во втором слоте хотбара.",
            wraplength=500
        ).pack(anchor="w")
        self.move_status = tk.StringVar(value="Движение выключено")
        ttk.Label(movement, textvariable=self.move_status).pack(anchor="w", pady=(4, 0))
        self.mine_status = tk.StringVar(value="Копание выключено")
        ttk.Label(movement, textvariable=self.mine_status).pack(anchor="w", pady=(2, 0))
        self.game_window_status = tk.StringVar(value="Проверяю окно Minecraft…")
        ttk.Label(movement, textvariable=self.game_window_status).pack(anchor="w", pady=(4, 0))
        self.repair_status = tk.StringVar(value="Контроль прочности включён: порог остановки 20%")
        ttk.Label(movement, textvariable=self.repair_status, wraplength=500).pack(anchor="w", pady=(4, 0))
        ttk.Button(movement, text="Найти окно Minecraft", command=self.refresh_game_window).pack(anchor="w", pady=(5, 0))
        ttk.Button(movement, text="Включить F3 и проверить XYZ", command=self.check_coordinates).pack(anchor="w", pady=(5, 0))

        buttons = ttk.Frame(f)
        buttons.pack(anchor="w", pady=10)
        self.test_btn = ttk.Button(buttons, text="Проверить Telegram", command=self.test_telegram)
        self.test_btn.pack(side="left")
        self.start_btn = ttk.Button(buttons, text="Начать мониторинг", command=self.start)
        self.start_btn.pack(side="left", padx=8)
        self.stop_btn = ttk.Button(buttons, text="Остановить", command=self.stop, state="disabled")
        self.stop_btn.pack(side="left")

        self.status = tk.StringVar(value="Ожидание запуска")
        ttk.Label(f, textvariable=self.status, wraplength=520).pack(anchor="w", pady=8)
        ttk.Label(
            f,
            text="Важно: для распознавания координат нужен Tesseract OCR. "
                 "Если он не установлен, ESP-мониторинг продолжит работать, но координаты будут недоступны.",
            wraplength=520
        ).pack(anchor="w", pady=8)
        ttk.Label(
            f,
            text="Детектор экспериментальный: возможны ложные срабатывания. "
                 "Цветовые маски и область OCR могут потребовать настройки.",
            wraplength=520
        ).pack(anchor="w", pady=4)

    def enable_paste(self, widget):
        """Надёжная вставка текста из буфера обмена в поля Tkinter."""
        menu = tk.Menu(widget, tearoff=0)
        menu.add_command(label="Вырезать", command=lambda: widget.event_generate("<<Cut>>"))
        menu.add_command(label="Копировать", command=lambda: widget.event_generate("<<Copy>>"))
        menu.add_command(label="Вставить", command=lambda: paste_text())
        menu.add_separator()
        menu.add_command(label="Выделить всё", command=lambda: (
            widget.select_range(0, "end"),
            widget.icursor("end")
        ))

        def paste_text(event=None):
            try:
                clipboard_text = self.root.clipboard_get()
                try:
                    first = widget.index("sel.first")
                    last = widget.index("sel.last")
                    widget.delete(first, last)
                except tk.TclError:
                    pass
                widget.insert("insert", clipboard_text)
                return "break"
            except tk.TclError:
                return "break"

        def show_menu(event):
            try:
                widget.focus_set()
                menu.tk_popup(event.x_root, event.y_root)
            finally:
                menu.grab_release()

        widget.bind("<Button-3>", show_menu)
        widget.bind("<Control-v>", paste_text)
        widget.bind("<Control-V>", paste_text)
        widget.bind("<Control-KeyPress-v>", paste_text)
        widget.bind("<Control-KeyPress-V>", paste_text)
        widget.bind("<Shift-Insert>", paste_text)
        widget.bind("<Control-c>", lambda event: (widget.event_generate("<<Copy>>"), "break")[1])
        widget.bind("<Control-C>", lambda event: (widget.event_generate("<<Copy>>"), "break")[1])
        widget.bind("<Control-x>", lambda event: (widget.event_generate("<<Cut>>"), "break")[1])
        widget.bind("<Control-X>", lambda event: (widget.event_generate("<<Cut>>"), "break")[1])
        widget.bind("<Control-a>", lambda event: (
            widget.select_range(0, "end"), widget.icursor("end"), "break"
        )[2])
        widget.bind("<Control-A>", lambda event: (
            widget.select_range(0, "end"), widget.icursor("end"), "break"
        )[2])

    def find_game_window(self):
        try:
            user32 = ctypes.windll.user32
            titles = []

            @ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p)
            def enum_callback(hwnd, _):
                if user32.IsWindowVisible(hwnd):
                    length = user32.GetWindowTextLengthW(hwnd)
                    if length:
                        buffer = ctypes.create_unicode_buffer(length + 1)
                        user32.GetWindowTextW(hwnd, buffer, length + 1)
                        title = buffer.value.strip()
                        if title:
                            titles.append(title)
                return True

            user32.EnumWindows(enum_callback, 0)
            matches = [title for title in titles if "minecraft" in title.casefold()]
            if matches:
                return matches[0]
            java_matches = [title for title in titles if "java" in title.casefold()]
            if java_matches:
                return "Возможно Minecraft: " + java_matches[0]
            return None
        except Exception as exc:
            return "ERROR:" + str(exc)

    def refresh_game_window(self):
        title = self.find_game_window()
        if title is None:
            self.game_window_status.set("Minecraft: окно не найдено. Проверь, что игра запущена.")
        elif title.startswith("ERROR:"):
            self.game_window_status.set("Ошибка поиска окна: " + title[6:120])
        else:
            self.game_window_status.set("Найдено окно: " + title[:100])

    def poll_game_window(self):
        if not self.root.winfo_exists():
            return
        self.refresh_game_window()
        self.root.after(3000, self.poll_game_window)

    def _find_game_hwnd(self):
        user32 = ctypes.windll.user32
        found = []
        @ctypes.WINFUNCTYPE(ctypes.c_bool, wintypes.HWND, wintypes.LPARAM)
        def enum_callback(hwnd, _):
            if user32.IsWindowVisible(hwnd):
                length = user32.GetWindowTextLengthW(hwnd)
                if length:
                    title = ctypes.create_unicode_buffer(length + 1)
                    user32.GetWindowTextW(hwnd, title, length + 1)
                    if "minecraft" in title.value.casefold():
                        found.append(hwnd)
            return True
        user32.EnumWindows(enum_callback, 0)
        return found[0] if found else None

    def check_coordinates(self):
        if not TESSERACT_PATH:
            messagebox.showerror("OCR не найден", "Установи Tesseract OCR, затем перезапусти программу.")
            return
        self.status.set("Проверяю экран Minecraft и координаты…")
        threading.Thread(target=self._check_coordinates_worker, daemon=True).start()

    def _check_coordinates_worker(self):
        try:
            hwnd = self._find_game_hwnd()
            if not hwnd:
                raise RuntimeError("Окно Minecraft не найдено. Запусти игру в оконном или полноэкранном режиме.")
            user32 = ctypes.windll.user32
            user32.ShowWindow(hwnd, 5)
            user32.SetForegroundWindow(hwnd)
            time.sleep(0.8)
            with mss.mss() as sct:
                monitor = sct.monitors[1]
                frame = cv2.cvtColor(np.array(sct.grab(monitor)), cv2.COLOR_BGRA2BGR)
                result = self.coords(frame)
                pressed_f3 = False
                # Проверяем наличие самого F3-оверлея отдельно от успешного чтения XYZ.
                top_left = frame[:int(frame.shape[0] * .72), :int(frame.shape[1] * .62)]
                overlay_text = pytesseract.image_to_string(
                    cv2.cvtColor(top_left, cv2.COLOR_BGR2GRAY),
                    config="--psm 6"
                )
                f3_visible = bool(re.search(r"\b(XYZ|Facing|Block|Chunk)\b", overlay_text, re.IGNORECASE))
                # Не нажимаем F3 повторно, если отладочный экран уже открыт.
                if not result.startswith("X=") and not f3_visible:
                    send_f3_key(False)
                    time.sleep(0.12)
                    send_f3_key(True)
                    pressed_f3 = True
                    time.sleep(1.2)
                    frame = cv2.cvtColor(np.array(sct.grab(monitor)), cv2.COLOR_BGRA2BGR)
                    result = self.coords(frame)
                stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
                path = CAPTURES / f"coords_check_{stamp}.jpg"
                cv2.imwrite(str(path), frame, [int(cv2.IMWRITE_JPEG_QUALITY), 92])
            if result.startswith("X="):
                message = "Координаты обнаружены: " + result
                self.set_status(message)
            else:
                message = ("XYZ не удалось прочитать. " +
                           ("F3 был нажат автоматически. " if pressed_f3 else "") +
                           "Проверь, что Minecraft активен и строка XYZ видна слева сверху. " + result)
                self.set_status(message)
            token, chat = self.token.get().strip(), self.chat.get().strip()
            if token and chat:
                threading.Thread(target=self.send, args=(
                    {"token": token, "chat": chat, "attach": True},
                    "Проверка координат Minecraft:\n" + message, path
                ), daemon=True).start()
            self.root.after(0, lambda m=message: messagebox.showinfo("Проверка XYZ", m))
        except Exception as exc:
            error = str(exc)[:250]
            self.set_status("Проверка координат: " + error)
            self.root.after(0, lambda e=error: messagebox.showerror("Проверка XYZ", e))

    def toggle_forward(self):
        if self.forward_active:
            self.stop_forward()
            return
        try:
            send_w_key(key_up=False)
            self.forward_active = True
            self.root.after(0, lambda: self.move_status.set("Движение включено: удерживается W"))
        except Exception as exc:
            message = "Ошибка F6: " + str(exc)[:140]
            self.root.after(0, lambda value=message: self.move_status.set(value))
            self.set_status(message)

    def toggle_mine(self):
        if self.mine_active:
            self.stop_mine()
            return
        try:
            send_left_mouse(key_up=False)
            self.mine_active = True
            self.root.after(0, lambda: self.mine_status.set("ЛКМ удерживается: копание включено"))
        except Exception as exc:
            message = "Ошибка F7: " + str(exc)[:140]
            self.root.after(0, lambda value=message: self.mine_status.set(value))
            self.set_status(message)

    def stop_mine(self):
        try:
            send_left_mouse(key_up=True)
        except Exception:
            pass
        self.mine_active = False
        if hasattr(self, "mine_status"):
            self.root.after(0, lambda: self.mine_status.set("Копание выключено"))

    def stop_forward(self):
        try:
            send_w_key(key_up=True)
        except Exception:
            pass
        self.forward_active = False
        if hasattr(self, "move_status"):
            self.root.after(0, lambda: self.move_status.set("Движение выключено"))


    def toggle_repair_watch(self):
        self.repair_watch_enabled = not self.repair_watch_enabled
        value = ("Контроль прочности включён: порог остановки 20%"
                 if self.repair_watch_enabled else "Контроль прочности выключен")
        self.root.after(0, lambda: self.repair_status.set(value))

    def read_pickaxe_durability(self, sct):
        """Экспериментальная оценка полоски второго слота при 1920x1080, GUI Scale 2."""
        region = {"left": 812, "top": 1055, "width": 56, "height": 16}
        shot = np.array(sct.grab(region))
        hsv = cv2.cvtColor(shot[:, :, :3], cv2.COLOR_BGR2HSV)
        mask = ((hsv[:, :, 1] > 90) & (hsv[:, :, 2] > 70) &
                ((hsv[:, :, 0] <= 12) | ((hsv[:, :, 0] >= 18) & (hsv[:, :, 0] <= 100))))
        best = 0
        for row in mask:
            xs = np.where(row)[0]
            if len(xs) >= 2:
                best = max(best, int(xs.max() - xs.min() + 1))
        if best < 3:
            return None
        return max(0, min(100, round(best / 26 * 100)))

    def repair_watch_loop(self):
        try:
            with mss.mss() as sct:
                while self.repair_watch_running:
                    if self.repair_watch_enabled and self.mine_active:
                        try:
                            durability = self.read_pickaxe_durability(sct)
                            if durability is not None:
                                self.root.after(0, lambda d=durability: self.repair_status.set(
                                    f"Прочность кирки (оценка): {d}%"))
                                if durability <= 20:
                                    self.stop_mine()
                                    now = time.time()
                                    if now - self.last_durability_notice > 10:
                                        self.last_durability_notice = now
                                        self.root.after(0, lambda: messagebox.showwarning(
                                            "Кирка почти сломалась",
                                            "Копание остановлено: прочность примерно 20% или ниже. "
                                            "Автоматическое использование пузырьков опыта пока не включено."
                                        ))
                            else:
                                self.root.after(0, lambda: self.repair_status.set(
                                    "Полоска прочности не распознана; контроль ожидает изображения"))
                        except Exception:
                            pass
                    time.sleep(0.5)
        except Exception:
            return

    def stop_all(self):
        self.stop_forward()
        self.stop_mine()

    def close(self):
        self.stop_all()
        self.running = False
        self.repair_watch_running = False
        try:
            self.hotkey_listener.stop()
        except Exception:
            pass
        self.root.destroy()

    def set_status(self, text):
        self.root.after(0, lambda value=text: self.status.set(value))

    def test_telegram(self):
        token = self.token.get().strip()
        chat = self.chat.get().strip()
        if not token or not chat:
            messagebox.showwarning("Не заполнено", "Введи Telegram Bot Token и Chat ID.")
            return
        self.test_btn.config(state="disabled")
        self.status.set("Проверяю Telegram… подожди до 15 секунд.")
        threading.Thread(
            target=self._test_telegram_worker,
            args=(token, chat),
            daemon=True
        ).start()

    def _test_telegram_worker(self, token, chat):
        try:
            response = requests.post(
                f"https://api.telegram.org/bot{token}/sendMessage",
                data={
                    "chat_id": chat,
                    "text": "FunTime ESP Observer: тестовое сообщение. Telegram подключён."
                },
                timeout=15
            )
            try:
                result = response.json()
            except ValueError:
                result = {}
            if response.status_code != 200 or not result.get("ok"):
                description = result.get("description") or response.text[:300] or f"HTTP {response.status_code}"
                raise RuntimeError(description)
            self.root.after(0, lambda: messagebox.showinfo(
                "Telegram подключён",
                "Тестовое сообщение успешно отправлено. Проверь Telegram."
            ))
            self.set_status("Успешно! Тестовое сообщение отправлено в Telegram.")
        except Exception as exc:
            error_text = str(exc)[:350]
            self.set_status("Ошибка Telegram: " + error_text)
            self.root.after(0, lambda value=error_text: messagebox.showerror(
                "Не удалось отправить сообщение",
                "Telegram вернул ошибку или не ответил. Подробности:\\n" + value
            ))
        finally:
            self.root.after(0, lambda: self.test_btn.config(state="normal"))

    def start(self):
        if self.running:
            return
        config = {
            "threshold": max(1, int(self.threshold.get())),
            "cooldown": max(5, int(self.cooldown.get())),
            "attach": bool(self.attach.get()),
            "token": self.token.get().strip(),
            "chat": self.chat.get().strip(),
        }
        self.running = True
        self.start_btn.config(state="disabled")
        self.stop_btn.config(state="normal")
        threading.Thread(target=self.loop, args=(config,), daemon=True).start()

    def stop(self):
        self.running = False
        self.status.set("Остановка…")

    def coords(self, frame):
        """Read only the labelled XYZ line from Minecraft's F3 overlay."""
        if not TESSERACT_PATH:
            return "Tesseract OCR не найден. Установи его или добавь в PATH."
        try:
            h, w = frame.shape[:2]
            # В Minecraft F3 строка XYZ находится в левом верхнем блоке отладки.
            crop = frame[:int(h * .48), :int(w * .48)]
            gray = cv2.cvtColor(crop, cv2.COLOR_BGR2GRAY)
            gray = cv2.resize(gray, None, fx=3, fy=3, interpolation=cv2.INTER_CUBIC)
            variants = [
                cv2.convertScaleAbs(gray, alpha=1.8, beta=25),
                cv2.bitwise_not(cv2.convertScaleAbs(gray, alpha=1.8, beta=25)),
            ]
            number = r"[-+]?\d{1,7}(?:[.,]\d+)?"
            # Обязательно требуем метку XYZ: случайные три числа не считаются координатами.
            patterns = [
                re.compile(rf"\bXYZ\b\s*:?\s*({number})\s*[/,; ]+\s*({number})\s*[/,; ]+\s*({number})", re.IGNORECASE),
                re.compile(rf"\bXYZ\b\s*:?\s*({number})\s+({number})\s+({number})", re.IGNORECASE),
            ]
            ocr_text = []
            for image in variants:
                for psm in (6, 11):
                    txt = pytesseract.image_to_string(
                        image,
                        config=f"--psm {psm} -c tessedit_char_whitelist=XYZxyz0123456789-+.,/:; "
                    )
                    ocr_text.append(txt)
                    for pattern in patterns:
                        match = pattern.search(txt)
                        if match:
                            values = [part.replace(",", ".") for part in match.groups()]
                            return f"X={values[0]}, Y={values[1]}, Z={values[2]}"
            return "XYZ-строка не найдена. OCR: " + " | ".join(
                " ".join(t.split())[:180] for t in ocr_text[:2]
            )
        except pytesseract.pytesseract.TesseractNotFoundError:
            return "Tesseract OCR не найден по пути: " + str(TESSERACT_PATH)
        except Exception as exc:
            return "Ошибка распознавания координат: " + str(exc)[:120]

    def target_direction(self, frame):
        """Оценивает только направление на крупнейшую ESP-область; дальность по одному кадру неизвестна."""
        h, w = frame.shape[:2]
        hsv = cv2.cvtColor(frame, cv2.COLOR_BGR2HSV)
        purple = cv2.inRange(hsv, np.array([125, 55, 70]), np.array([170, 255, 255]))
        orange = cv2.inRange(hsv, np.array([5, 70, 90]), np.array([28, 255, 255]))
        mask = cv2.bitwise_or(purple, orange)

        # Убираем одиночные шумовые пиксели и ищем отдельные группы подсветки.
        kernel = np.ones((3, 3), np.uint8)
        mask = cv2.morphologyEx(mask, cv2.MORPH_OPEN, kernel)
        count, labels, stats, centers = cv2.connectedComponentsWithStats(mask, 8)
        candidates = []
        for i in range(1, count):
            area = int(stats[i, cv2.CC_STAT_AREA])
            if area >= 8:
                candidates.append((area, centers[i]))
        if not candidates:
            return "Направление на ESP не определено (подсветка слишком мала или размыта)."

        # Самая крупная связная область — лишь эвристика, не гарантированно ближайшая цель.
        area, center = max(candidates, key=lambda item: item[0])
        dx = float(center[0]) - (w / 2)
        relative_angle = dx / max(1, w / 2) * 35.0  # приблизительно, FOV по горизонтали принят за 70°.
        if abs(relative_angle) < 3:
            side = "почти прямо по центру"
        elif relative_angle > 0:
            side = "правее центра"
        else:
            side = "левее центра"
        return (f"Направление на ESP: {side}, примерно {relative_angle:+.1f}° "
                f"от направления взгляда (оценка при FOV 70°). "
                "Расстояние неизвестно — точные координаты объекта вычислить нельзя.")

    def send(self, config, msg, photo):
        token, chat = config["token"], config["chat"]
        if not token or not chat:
            self.set_status("Мониторинг активен; для Telegram заполни Token и Chat ID.")
            return
        try:
            response = requests.post(
                f"https://api.telegram.org/bot{token}/sendMessage",
                data={"chat_id": chat, "text": msg},
                timeout=15
            )
            response.raise_for_status()
            if config["attach"]:
                with open(photo, "rb") as fp:
                    response = requests.post(
                        f"https://api.telegram.org/bot{token}/sendPhoto",
                        data={"chat_id": chat},
                        files={"photo": fp},
                        timeout=30
                    )
                    response.raise_for_status()
        except Exception as exc:
            self.set_status("Ошибка отправки Telegram: " + str(exc)[:180])

    def loop(self, config):
        try:
            with mss.mss() as sct:
                mon = sct.monitors[1]
                while self.running:
                    raw = np.array(sct.grab(mon))
                    frame = cv2.cvtColor(raw, cv2.COLOR_BGRA2BGR)
                    hsv = cv2.cvtColor(frame, cv2.COLOR_BGR2HSV)
                    purple = cv2.inRange(hsv, np.array([125, 55, 70]), np.array([170, 255, 255]))
                    orange = cv2.inRange(hsv, np.array([5, 70, 90]), np.array([28, 255, 255]))
                    count = cv2.countNonZero(purple) + cv2.countNonZero(orange)
                    self.set_status(f"Мониторинг активен. ESP-пикселей: {count}")

                    now = time.time()
                    if count >= config["threshold"] and now - self.last_alert >= config["cooldown"]:
                        self.last_alert = now
                        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
                        path = CAPTURES / f"esp_{stamp}.jpg"
                        cv2.imwrite(str(path), frame, [int(cv2.IMWRITE_JPEG_QUALITY), 85])
                        message = (
                            "Обнаружена ESP-подсветка\n"
                            + "Координаты игрока: " + self.coords(frame)
                            + "\n" + self.target_direction(frame)
                            + f"\n{datetime.now():%Y-%m-%d %H:%M:%S}\nESP-пикселей: {count}"
                        )
                        threading.Thread(
                            target=self.send,
                            args=(config, message, path),
                            daemon=True
                        ).start()
                    time.sleep(1)
        except Exception as exc:
            error = str(exc)
            self.root.after(0, lambda value=error: messagebox.showerror("Ошибка мониторинга", value))
        finally:
            self.running = False
            self.root.after(0, lambda: self.start_btn.config(state="normal"))
            self.root.after(0, lambda: self.stop_btn.config(state="disabled"))
            self.set_status("Мониторинг остановлен")


if __name__ == "__main__":
    root = tk.Tk()
    Observer(root)
    root.mainloop()
