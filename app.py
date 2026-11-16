import tkinter as tk
from tkinter import ttk, messagebox
import threading
import time
import re
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
        self.hotkey_listener = keyboard.GlobalHotKeys({
            "<f6>": self.toggle_forward,
            "<f7>": self.toggle_mine,
            "<f8>": self.stop_all,
        })
        self.hotkey_listener.start()
        self.build()
        self.root.protocol("WM_DELETE_WINDOW", self.close)
        self.refresh_game_window()
        self.root.after(3000, self.poll_game_window)

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
            text="F6 — идти вперёд (W), F7 — удерживать ЛКМ, F8 — отпустить W и ЛКМ. Перед использованием активируй окно Minecraft.",
            wraplength=500
        ).pack(anchor="w")
        self.move_status = tk.StringVar(value="Движение выключено")
        ttk.Label(movement, textvariable=self.move_status).pack(anchor="w", pady=(4, 0))
        self.mine_status = tk.StringVar(value="Копание выключено")
        ttk.Label(movement, textvariable=self.mine_status).pack(anchor="w", pady=(2, 0))
        self.game_window_status = tk.StringVar(value="Проверяю окно Minecraft…")
        ttk.Label(movement, textvariable=self.game_window_status).pack(anchor="w", pady=(4, 0))
        ttk.Button(movement, text="Найти окно Minecraft", command=self.refresh_game_window).pack(anchor="w", pady=(5, 0))

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
        """Добавляет явные сочетания клавиш и контекстное меню для вставки текста."""
        menu = tk.Menu(widget, tearoff=0)
        menu.add_command(label="Вырезать", command=lambda: widget.event_generate("<<Cut>>"))
        menu.add_command(label="Копировать", command=lambda: widget.event_generate("<<Copy>>"))
        menu.add_command(label="Вставить", command=lambda: widget.event_generate("<<Paste>>"))
        menu.add_separator()
        menu.add_command(label="Выделить всё", command=lambda: (
            widget.select_range(0, "end"),
            widget.icursor("end")
        ))

        def show_menu(event):
            try:
                menu.tk_popup(event.x_root, event.y_root)
            finally:
                menu.grab_release()

        widget.bind("<Button-3>", show_menu)
        widget.bind("<Control-v>", lambda event: (widget.event_generate("<<Paste>>"), "break")[1])
        widget.bind("<Control-V>", lambda event: (widget.event_generate("<<Paste>>"), "break")[1])
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

    def stop_all(self):
        self.stop_forward()
        self.stop_mine()

    def close(self):
        self.stop_all()
        self.running = False
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
        self.status.set("Проверяю подключение к Telegram…")
        threading.Thread(target=self._test_telegram_worker, args=(token, chat), daemon=True).start()

    def _test_telegram_worker(self, token, chat):
        try:
            response = requests.post(
                f"https://api.telegram.org/bot{token}/sendMessage",
                data={"chat_id": chat, "text": "FunTime ESP Observer: тестовое сообщение. Telegram подключён."},
                timeout=15
            )
            response.raise_for_status()
            result = response.json()
            if not result.get("ok"):
                raise RuntimeError(result.get("description", "Telegram API вернул ошибку"))
            self.set_status("Успешно! Тестовое сообщение отправлено в Telegram.")
        except Exception as exc:
            self.set_status("Ошибка Telegram: " + str(exc)[:220])
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
        try:
            h, w = frame.shape[:2]
            crop = frame[:int(h * .28), :int(w * .42)]
            gray = cv2.cvtColor(crop, cv2.COLOR_BGR2GRAY)
            gray = cv2.resize(gray, None, fx=2, fy=2, interpolation=cv2.INTER_CUBIC)
            txt = pytesseract.image_to_string(gray, config="--psm 6")
            match = re.search(
                r"(-?\d{1,7})\s*[, ]+\s*(-?\d{1,7})\s*[, ]+\s*(-?\d{1,7})",
                txt
            )
            return (
                f"X={match.group(1)}, Y={match.group(2)}, Z={match.group(3)}"
                if match else "Координаты не распознаны"
            )
        except Exception:
            return "Координаты недоступны (проверь установку Tesseract OCR)"

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
                            + self.coords(frame)
                            + f"\n{datetime.now():%Y-%m-%d %H:%M:%S}\nПикселей: {count}"
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
