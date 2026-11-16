import tkinter as tk
from tkinter import ttk, messagebox
import threading, time, re
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
        self.root.geometry("540x430")
        self.running = False
        self.last_alert = 0
        self.token = tk.StringVar()
        self.chat = tk.StringVar()
        self.threshold = tk.IntVar(value=180)
        self.cooldown = tk.IntVar(value=45)
        self.attach = tk.BooleanVar(value=True)
        self.build()

    def build(self):
        f = ttk.Frame(self.root, padding=14)
        f.pack(fill="both", expand=True)
        ttk.Label(f, text="FunTime ESP Observer", font=("Segoe UI", 16, "bold")).pack(anchor="w")
        ttk.Label(f, text="Экранный мониторинг ESP и уведомления Telegram. Не управляет персонажем.").pack(anchor="w", pady=(4,12))
        ttk.Label(f, text="Telegram Bot Token").pack(anchor="w")
        ttk.Entry(f, textvariable=self.token, show="*").pack(fill="x", pady=(2,8))
        ttk.Label(f, text="Telegram Chat ID").pack(anchor="w")
        ttk.Entry(f, textvariable=self.chat).pack(fill="x", pady=(2,8))
        line = ttk.Frame(f); line.pack(fill="x", pady=6)
        ttk.Label(line, text="Порог ESP-пикселей").pack(side="left")
        ttk.Spinbox(line, from_=20, to=5000, textvariable=self.threshold, width=8).pack(side="left", padx=6)
        ttk.Label(line, text="Пауза (сек.)").pack(side="left", padx=(12,0))
        ttk.Spinbox(line, from_=5, to=3600, textvariable=self.cooldown, width=8).pack(side="left", padx=6)
        ttk.Checkbutton(f, text="Прикреплять скриншот", variable=self.attach).pack(anchor="w", pady=6)
        buttons = ttk.Frame(f); buttons.pack(anchor="w", pady=10)
        self.start_btn = ttk.Button(buttons, text="Начать мониторинг", command=self.start); self.start_btn.pack(side="left")
        self.stop_btn = ttk.Button(buttons, text="Остановить", command=self.stop, state="disabled"); self.stop_btn.pack(side="left", padx=8)
        self.status = tk.StringVar(value="Ожидание запуска")
        ttk.Label(f, textvariable=self.status, wraplength=500).pack(anchor="w", pady=8)
        ttk.Label(f, text="Детектор экспериментальный: возможны ложные срабатывания. Цветовые маски и область OCR могут потребовать настройки.").pack(anchor="w", pady=8)

    def start(self):
        if self.running: return
        self.running = True
        self.start_btn.config(state="disabled"); self.stop_btn.config(state="normal")
        threading.Thread(target=self.loop, daemon=True).start()

    def stop(self):
        self.running = False
        self.status.set("Остановка…")

    def coords(self, frame):
        h,w = frame.shape[:2]
        crop = frame[:int(h*.28), :int(w*.42)]
        gray = cv2.cvtColor(crop, cv2.COLOR_BGR2GRAY)
        gray = cv2.resize(gray, None, fx=2, fy=2, interpolation=cv2.INTER_CUBIC)
        txt = pytesseract.image_to_string(gray, config="--psm 6")
        m = re.search(r"(-?\d{1,7})\s*[, ]+\s*(-?\d{1,7})\s*[, ]+\s*(-?\d{1,7})", txt)
        return f"X={m.group(1)}, Y={m.group(2)}, Z={m.group(3)}" if m else "Координаты не распознаны"

    def send(self, msg, photo):
        token, chat = self.token.get().strip(), self.chat.get().strip()
        if not token or not chat:
            self.root.after(0, lambda: self.status.set("Мониторинг активен; Telegram Token/Chat ID не заполнены."))
            return
        try:
            requests.post(f"https://api.telegram.org/bot{token}/sendMessage", data={"chat_id":chat,"text":msg}, timeout=15).raise_for_status()
            if self.attach.get():
                with open(photo,"rb") as fp:
                    requests.post(f"https://api.telegram.org/bot{token}/sendPhoto", data={"chat_id":chat}, files={"photo":fp}, timeout=30).raise_for_status()
        except Exception as e:
            err = str(e)
            self.root.after(0, lambda: self.status.set("Ошибка Telegram: "+err[:140]))

    def loop(self):
        try:
            with mss.mss() as sct:
                mon = sct.monitors[1]
                while self.running:
                    raw = np.array(sct.grab(mon))
                    frame = cv2.cvtColor(raw, cv2.COLOR_BGRA2BGR)
                    hsv = cv2.cvtColor(frame, cv2.COLOR_BGR2HSV)
                    purple = cv2.inRange(hsv, np.array([125,55,70]), np.array([170,255,255]))
                    orange = cv2.inRange(hsv, np.array([5,70,90]), np.array([28,255,255]))
                    count = cv2.countNonZero(purple) + cv2.countNonZero(orange)
                    self.root.after(0, lambda c=count: self.status.set(f"Мониторинг активен. ESP-пикселей: {c}"))
                    now = time.time()
                    if count >= max(1,int(self.threshold.get())) and now-self.last_alert >= max(5,int(self.cooldown.get())):
                        self.last_alert = now
                        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
                        path = CAPTURES/f"esp_{stamp}.jpg"
                        cv2.imwrite(str(path), frame, [int(cv2.IMWRITE_JPEG_QUALITY),85])
                        msg = f"Обнаружена ESP-подсветка\n{self.coords(frame)}\n{datetime.now():%Y-%m-%d %H:%M:%S}\nПикселей: {count}"
                        threading.Thread(target=self.send, args=(msg,path), daemon=True).start()
                    time.sleep(1)
        except Exception as e:
            err = str(e)
            self.root.after(0, lambda: messagebox.showerror("Ошибка", err))
        finally:
            self.running = False
            self.root.after(0, lambda: self.start_btn.config(state="normal"))
            self.root.after(0, lambda: self.stop_btn.config(state="disabled"))
            self.root.after(0, lambda: self.status.set("Мониторинг остановлен"))

if __name__ == "__main__":
    root = tk.Tk()
    Observer(root)
    root.mainloop()
