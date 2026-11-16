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

CAPTURES = Path("captures")
CAPTURES.mkdir(exist_ok=True)


class Observer:
    def __init__(self, root):
        self.root = root
        self.root.title("FunTime ESP Observer")
        self.root.geometry("560x470")
        self.root.minsize(520, 430)
        self.running = False
        self.last_alert = 0
        self.token = tk.StringVar()
        self.chat = tk.StringVar()
        self.threshold = tk.IntVar(value=180)
        self.cooldown = tk.IntVar(value=45)
        self.attach = tk.BooleanVar(value=True)
        self.config = {}
        self.build()

    def build(self):
        f = ttk.Frame(self.root, padding=14)
        f.pack(fill="both", expand=True)
        ttk.Label(f, text="FunTime ESP Observer", font=("Segoe UI", 16, "bold")).pack(anchor="w")
        ttk.Label(
            f,
            text="Экранный мониторинг ESP и уведомления Telegram. Персонажем не управляет."
        ).pack(anchor="w", pady=(4, 12))

        ttk.Label(f, text="Telegram Bot Token").pack(anchor="w")
        ttk.Entry(f, textvariable=self.token, show="*").pack(fill="x", pady=(2, 8))
        ttk.Label(f, text="Telegram Chat ID").pack(anchor="w")
        ttk.Entry(f, textvariable=self.chat).pack(fill="x", pady=(2, 8))

        line = ttk.Frame(f)
        line.pack(fill="x", pady=6)
        ttk.Label(line, text="Порог ESP-пикселей").pack(side="left")
        ttk.Spinbox(line, from_=20, to=5000, textvariable=self.threshold, width=8).pack(side="left", padx=6)
        ttk.Label(line, text="Пауза (сек.)").pack(side="left", padx=(12, 0))
        ttk.Spinbox(line, from_=5, to=3600, textvariable=self.cooldown, width=8).pack(side="left", padx=6)

        ttk.Checkbutton(f, text="Прикреплять скриншот", variable=self.attach).pack(anchor="w", pady=6)

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
