"""
FLINT PET v2 — Gesture-Based AI Desktop Companion
====================================================
Your pet first. Your robot second.

NEW IN V2:
  • Gesture recognition  — shake, fling, pet (stroke), double-tap, hold
  • Context awareness    — watches active window, clipboard, time, idle
  • Reaction system      — Flint reacts to YOUR activity in real time
  • Mini-games           — poke to play, catch the dot, simon says blinks
  • Mood memory          — remembers your patterns across sessions
  • AI commentary        — brief, witty observations about what you're doing
  • Emote wheel          — right-click for gestures/emotes, not just menu
  • Zero required voice  — fully gesture + click driven; voice is optional
"""

import tkinter as tk
from tkinter import font as tkfont
import threading
import random
import time
import math
import json
import os
import sys
import queue
import ctypes
import subprocess
from pathlib import Path
from datetime import datetime
from collections import deque

# ─────────────────────────────────────────
#  Optional imports
# ─────────────────────────────────────────
try:
    import speech_recognition as sr
    SR_AVAILABLE = True
except ImportError:
    SR_AVAILABLE = False

try:
    import google.generativeai as genai
    GEMINI_AVAILABLE = True
except ImportError:
    GEMINI_AVAILABLE = False

try:
    import win32gui
    import win32process
    import psutil
    WIN32_AVAILABLE = True
except ImportError:
    WIN32_AVAILABLE = False

# ─────────────────────────────────────────
#  Config
# ─────────────────────────────────────────
BASE_DIR        = Path(__file__).resolve().parent
CONFIG_PATH     = BASE_DIR / "config" / "api_keys.json"
MEMORY_PATH     = BASE_DIR / "pet_memory.json"
PET_SIZE        = 130
BUBBLE_FONT_SZ  = 10

# Context-to-mood mapping  (process name fragments → mood)
CONTEXT_MOODS = {
    "code":        ("curious",   ["Ooh, what are we building?",  "I see code. I am pleased.", "Clean syntax energy."]),
    "vscode":      ("curious",   ["VS Code detected. Let's go.", "Tab width opinions loading…", "I love watching you type."]),
    "pycharm":     ("curious",   ["Python? Excellent taste.",    "Indentation matters.",       "Running in my mind too."]),
    "chrome":      ("idle",      ["Browsing, sir?",             "The internet awaits.",       "Don't fall into the rabbit hole."]),
    "firefox":     ("idle",      ["Fox browser spotted.",       "Open source appreciator.",   "Tabs: a lifestyle."]),
    "figma":       ("excited",   ["Design mode!",               "Pixels with purpose.",       "That color palette though."]),
    "photoshop":   ("excited",   ["Creative mode activated.",   "Layer by layer.",            "Command+Z is your friend."]),
    "spotify":     ("happy",     ["Music detected!",            "What's playing?",            "Vibing together."]),
    "discord":     ("happy",     ["Social mode.",               "Talk to humans sometimes.",  "Ping incoming?"]),
    "slack":       ("thinking",  ["Work chat. The grind.",      "Typing indicator: stress.",  "How many unreads?"]),
    "terminal":    ("excited",   ["Terminal open. Hacker mode.","sudo? Careful.",             "One wrong rm and…"]),
    "cmd":         ("excited",   ["Command line enjoyer.",      "Old school. Respect.",       "dir /s go brrr."]),
    "notepad":     ("bored",     ["Notepad? Really?",           "Living dangerously.",        "No syntax highlight? Brave."]),
    "excel":       ("thinking",  ["Spreadsheet mode.",          "VLOOKUP incoming.",          "=SUM(my suffering)"]),
    "word":        ("idle",      ["Document mode.",             "Ctrl+S. Always.",            "The cursor blinks… waiting."]),
    "youtube":     ("happy",     ["Video break!",               "Educational content, right?","Five minutes max. I'll watch."]),
    "netflix":     ("sleepy",    ["Netflix time.",              "Just one more episode…",     "I'll nap with you."]),
    "game":        ("excited",   ["Gaming detected!",           "Let's GO.",                  "I believe in you."]),
    "steam":       ("excited",   ["Steam? Game time!",         "Loading… loading…",          "Achievements await."]),
}

IDLE_SAYINGS = [
    "…", "beep.", "*yawn*", "still here.", "boop.",
    "watching.", "all good.", "zoning out…",
    ">_", "01001000 01101001", "scanning…",
    "did you know I count your keystrokes?",
    "I've been thinking.", "no thoughts. just vibes.",
    "*stretches*", "the void and I are friends.",
    "say hi sometime.", "I made up a song. it goes beep.",
    "cogito ergo sum. probably.", "still loading personality…",
    "low stimulation mode.", "poke me?",
]

# Gestures
GESTURE_RESPONSES = {
    "shake":    ["dizzy…", "WHOA", "ahhh!", "the world spins!", "*buffering*"],
    "fling":    ["wheee!", "FREEDOM", "I flew!", "10/10 launch", "do it again!"],
    "pet":      ["purr…", "mrrr.", "yes. this.", ":)", "*happy noises*", "warmth detected."],
    "hold":     ["…holding.", "is this a hug?", "don't let go.", "cozy.", "presence acknowledged."],
    "poke":     ["ow.", "hey!", "again?", "rude.", "i felt that.", "boop received."],
    "throw_up": ["I can fly?!", "UP!", "ceiling check!", "altitude: maximum"],
    "throw_down":["gravity. rude.", "oof.", "floor check.", "smooth landing. sort of."],
}

MOODS = {
    "idle":      {"color": "#00d4ff", "eye": "normal",   "bob": 1.0,  "freq": 1.0},
    "happy":     {"color": "#00ff88", "eye": "happy",    "bob": 1.5,  "freq": 1.8},
    "thinking":  {"color": "#aa88ff", "eye": "think",    "bob": 0.4,  "freq": 0.6},
    "listening": {"color": "#ffaa00", "eye": "alert",    "bob": 2.0,  "freq": 2.2},
    "speaking":  {"color": "#ff4488", "eye": "speak",    "bob": 2.2,  "freq": 2.5},
    "sleepy":    {"color": "#4488aa", "eye": "sleepy",   "bob": 0.2,  "freq": 0.3},
    "excited":   {"color": "#ffdd00", "eye": "wide",     "bob": 3.0,  "freq": 3.0},
    "curious":   {"color": "#44ddcc", "eye": "curious",  "bob": 1.2,  "freq": 1.4},
    "bored":     {"color": "#888888", "eye": "half",     "bob": 0.2,  "freq": 0.4},
    "dizzy":     {"color": "#ff8844", "eye": "dizzy",    "bob": 4.0,  "freq": 4.0},
    "love":      {"color": "#ff66aa", "eye": "heart",    "bob": 2.5,  "freq": 2.0},
    "gaming":    {"color": "#88ff44", "eye": "gaming",   "bob": 2.0,  "freq": 2.5},
    "coding":    {"color": "#44aaff", "eye": "scan",     "bob": 0.8,  "freq": 1.1},
}


def _load_api_key():
    try:
        with open(CONFIG_PATH, "r") as f:
            data = json.load(f)

        key = data.get("gemini_api_key", "").strip()

        print("CONFIG_PATH =", CONFIG_PATH)
        print("KEY =", repr(key))

        return key

    except Exception as e:
        print("API KEY LOAD ERROR:", e)
        return ""


def _load_memory():
    try:
        with open(MEMORY_PATH, "r") as f:
            return json.load(f)
    except Exception:
        return {
            "sessions": 0,
            "total_pets": 0,
            "total_pokes": 0,
            "context_history": {},
            "mood_log": [],
            "facts": {},
            "high_score_dot": 0,
        }


def _save_memory(mem):
    try:
        os.makedirs(BASE_DIR, exist_ok=True)
        with open(MEMORY_PATH, "w") as f:
            json.dump(mem, f, indent=2)
    except Exception:
        pass


# ═══════════════════════════════════════════════════════
#  GESTURE DETECTOR
# ═══════════════════════════════════════════════════════
class GestureDetector:
    """
    Tracks mouse history on the pet canvas to detect:
      shake   — rapid left-right oscillation
      fling   — fast release with velocity
      pet     — slow horizontal stroke
      hold    — press and hold 1+ sec with no movement
      poke    — quick click with minimal drag
      throw   — drag then release with direction
    """

    def __init__(self):
        self.positions   = deque(maxlen=30)   # (time, x, y)
        self.press_time  = None
        self.press_pos   = None
        self.last_pos    = None
        self.velocity    = (0.0, 0.0)

    def on_press(self, x, y):
        self.press_time = time.time()
        self.press_pos  = (x, y)
        self.positions.clear()
        self.positions.append((self.press_time, x, y))

    def on_move(self, x, y):
        now = time.time()
        self.positions.append((now, x, y))
        if len(self.positions) >= 2:
            dt = now - self.positions[-2][0]
            if dt > 0:
                self.velocity = (
                    (x - self.positions[-2][1]) / dt,
                    (y - self.positions[-2][2]) / dt,
                )
        self.last_pos = (x, y)

    def on_release(self, x, y) -> str:
        if not self.press_time:
            return None
        held  = time.time() - self.press_time
        dx    = x - self.press_pos[0]
        dy    = y - self.press_pos[1]
        dist  = math.hypot(dx, dy)
        speed = math.hypot(*self.velocity)

        # Hold
        if held > 0.9 and dist < 10:
            return "hold"

        # Poke (quick, no movement)
        if held < 0.25 and dist < 8:
            return "poke"

        # Fling (fast release)
        if speed > 400:
            if abs(dy) > abs(dx):
                return "throw_up" if dy < 0 else "throw_down"
            return "fling"

        # Pet (slow horizontal stroke)
        if dist > 40 and abs(dx) > abs(dy) * 2 and speed < 250:
            return "pet"

        # Shake — look for direction reversals
        if len(self.positions) > 8:
            xs = [p[1] for p in self.positions]
            reversals = sum(
                1 for i in range(1, len(xs)-1)
                if (xs[i] - xs[i-1]) * (xs[i+1] - xs[i]) < -20
            )
            if reversals >= 2:
                return "shake"

        return None


# ═══════════════════════════════════════════════════════
#  CONTEXT WATCHER  — polls active window
# ═══════════════════════════════════════════════════════
class ContextWatcher:
    def __init__(self, on_context_change):
        self.on_change     = on_context_change
        self._last_context = None
        self._running      = False

    def start(self):
        self._running = True
        threading.Thread(target=self._loop, daemon=True).start()

    def stop(self):
        self._running = False

    def _loop(self):
        while self._running:
            ctx = self._get_active_context()
            if ctx != self._last_context:
                self._last_context = ctx
                self.on_change(ctx)
            time.sleep(4)

    def _get_active_context(self) -> str:
        """Return lowercase process name of foreground window."""
        try:
            if WIN32_AVAILABLE:
                hwnd = win32gui.GetForegroundWindow()
                _, pid = win32process.GetWindowThreadProcessId(hwnd)
                proc = psutil.Process(pid)
                return proc.name().lower().replace(".exe", "")
            # Fallback: xdotool on Linux
            result = subprocess.run(
                ["xdotool", "getactivewindow", "getwindowname"],
                capture_output=True, text=True, timeout=1
            )
            return result.stdout.strip().lower()
        except Exception:
            return "unknown"


# ═══════════════════════════════════════════════════════
#  CREATURE RENDERER
# ═══════════════════════════════════════════════════════
class CreatureRenderer:
    def __init__(self, canvas: tk.Canvas, size: int):
        self.c    = canvas
        self.sz   = size
        self.cx   = size // 2
        self.cy   = size // 2
        self._ids = []
        self._squish_x = 1.0
        self._squish_y = 1.0

    def _clear(self):
        for i in self._ids:
            try: self.c.delete(i)
            except: pass
        self._ids = []

    def _oval(self, x0, y0, x1, y1, **kw):
        i = self.c.create_oval(x0, y0, x1, y1, **kw)
        self._ids.append(i); return i

    def _rect(self, x0, y0, x1, y1, **kw):
        i = self.c.create_rectangle(x0, y0, x1, y1, **kw)
        self._ids.append(i); return i

    def _poly(self, pts, **kw):
        i = self.c.create_polygon(pts, **kw)
        self._ids.append(i); return i

    def _line(self, pts, **kw):
        i = self.c.create_line(pts, **kw)
        self._ids.append(i); return i

    def _text(self, x, y, **kw):
        i = self.c.create_text(x, y, **kw)
        self._ids.append(i); return i

    def set_squish(self, sx, sy):
        self._squish_x = sx
        self._squish_y = sy

    def draw(self, mood, t, hover, blink_t, tail_t, ant_t,
             dizzy_t=0, heart_t=0, extra_data=None):
        self._clear()
        cfg      = MOODS.get(mood, MOODS["idle"])
        color    = cfg["color"]
        eye_type = cfg["eye"]
        bob_amp  = cfg["bob"]

        cx, cy = self.cx, self.cy
        bob    = math.sin(t * 2.0) * bob_amp

        # Squish effect (from fling/shake)
        sx, sy = self._squish_x, self._squish_y
        body_rx = int(30 * sx)
        body_ry = int(28 * sy)

        cy_body = cy + bob

        # Dizzy rotation
        if mood == "dizzy":
            dizzy_spin = dizzy_t * 3
            cx_off = math.cos(dizzy_spin) * 6
            cy_off = math.sin(dizzy_spin) * 3
            cx += cx_off; cy_body += cy_off

        # Glow ring
        glow_r = 46 + math.sin(t * 2) * 3
        glow_c = self._alpha(color, 0.15)
        self._oval(cx-glow_r, cy_body-glow_r, cx+glow_r, cy_body+glow_r,
                   outline=glow_c, fill="", width=7)

        # Antenna
        ant_swing = math.sin(t * 4 + ant_t) * 8
        ant_tip_x = cx + ant_swing
        ant_tip_y = cy_body - 58
        self._line([cx, cy_body-34,
                    cx + ant_swing//2, (cy_body-34+ant_tip_y)//2,
                    ant_tip_x, ant_tip_y],
                   fill=color, width=2, smooth=True)
        ball_r = 5 + math.sin(t * 6) * 2
        self._oval(ant_tip_x-ball_r, ant_tip_y-ball_r,
                   ant_tip_x+ball_r, ant_tip_y+ball_r,
                   fill=color, outline="#ffffff", width=1)

        # Body
        self._oval(cx-body_rx, cy_body-body_ry,
                   cx+body_rx, cy_body+body_ry,
                   fill="#080818", outline=color, width=2)
        self._oval(cx-int(22*sx), cy_body-int(20*sy),
                   cx+int(22*sx), cy_body+int(20*sy),
                   fill="#0c0c24", outline=color, width=1)

        # Ear fins
        for side in (-1, 1):
            fin_x   = cx + side * body_rx
            fin_y   = cy_body - 5
            fin_tip = cx + side * (44 + math.sin(t * 3 + side) * 5)
            pts = [fin_x, fin_y, fin_tip, cy_body-14, fin_x, cy_body+10]
            self._poly(pts, fill=color, outline=color, smooth=True)

        # Eyes
        self._draw_eyes(cx, cy_body, eye_type, color, blink_t, t, dizzy_t)

        # Mouth
        self._draw_mouth(cx, cy_body, mood, t)

        # Tail
        self._draw_tail(cx, cy_body+body_ry, color, tail_t, t)

        # Mood overlays
        if mood in ("excited", "happy", "gaming"):
            self._sparkles(cx, cy_body, color, t)
        if mood in ("sleepy", "bored"):
            self._zzz(cx, cy_body, t)
        if mood == "thinking":
            self._scan(cx, cy_body, t)
        if mood == "coding":
            self._code_rain(cx, cy_body, t)
        if mood == "love":
            self._hearts(cx, cy_body, heart_t, t)

        # Hover highlight
        if hover:
            self._oval(cx-32, cy_body-30, cx+32, cy_body+30,
                       outline=self._alpha(color, 0.35), fill="", width=3)

    # ── eye types ──

    def _draw_eyes(self, cx, cy, eye_type, color, blink_t, t, dizzy_t=0):
        eye_y  = cy - 6
        esep   = 11
        blink  = (blink_t % 5.0) < 0.12

        for side in (-1, 1):
            ex = cx + side * esep

            if blink and eye_type not in ("sleepy", "half"):
                self._line([ex-5, eye_y, ex+5, eye_y], fill=color, width=2)
                continue

            if eye_type == "normal":
                self._oval(ex-5, eye_y-5, ex+5, eye_y+5, fill="#050515", outline=color, width=2)
                self._oval(ex-2, eye_y-2, ex+2, eye_y+2, fill=color, outline="")

            elif eye_type == "happy":
                pts = [ex-5, eye_y+2, ex, eye_y-5, ex+5, eye_y+2]
                self._poly(pts, fill=color, outline=color, smooth=True)

            elif eye_type == "think":
                if side == -1:
                    self._oval(ex-5, eye_y-5, ex+5, eye_y+5, fill="#050515", outline=color, width=2)
                    self._oval(ex-2, eye_y-2, ex+2, eye_y+2, fill=color, outline="")
                else:
                    self._line([ex-5, eye_y, ex+5, eye_y], fill=color, width=2)
                    self._line([ex-3, eye_y-2, ex+3, eye_y-2], fill=color, width=1)

            elif eye_type == "alert":
                r = 6 + math.sin(t * 8)
                self._oval(ex-r, eye_y-r, ex+r, eye_y+r, fill="#050515", outline=color, width=2)
                self._oval(ex-2, eye_y-2, ex+2, eye_y+2, fill=color, outline="")

            elif eye_type == "speak":
                ir = 2 + math.sin(t * 12) * 1.5
                self._oval(ex-5, eye_y-5, ex+5, eye_y+5, fill="#050515", outline=color, width=2)
                self._oval(ex-ir, eye_y-ir, ex+ir, eye_y+ir, fill=color, outline="")

            elif eye_type == "sleepy":
                self._oval(ex-5, eye_y-2, ex+5, eye_y+5, fill="#050515", outline=color, width=2)
                self._rect(ex-6, eye_y-6, ex+6, eye_y, fill="#080818", outline="")
                self._line([ex-5, eye_y, ex+5, eye_y], fill=color, width=1)

            elif eye_type == "wide":
                self._oval(ex-7, eye_y-7, ex+7, eye_y+7, fill="#050515", outline=color, width=2)
                self._oval(ex-3, eye_y-3, ex+3, eye_y+3, fill=color, outline="")
                self._oval(ex+2, eye_y-4, ex+4, eye_y-2, fill="#ffffff", outline="")

            elif eye_type == "curious":
                self._oval(ex-5, eye_y-5, ex+5, eye_y+5, fill="#050515", outline=color, width=2)
                px = ex + side * math.sin(t * 3) * 2
                self._oval(px-2, eye_y-2, px+2, eye_y+2, fill=color, outline="")

            elif eye_type == "half":
                self._oval(ex-5, eye_y-3, ex+5, eye_y+5, fill="#050515", outline=color, width=1)
                self._rect(ex-6, eye_y-8, ex+6, eye_y-2, fill="#080818", outline="")

            elif eye_type == "dizzy":
                # spinning X
                spin = dizzy_t * 4
                for angle in [spin, spin + math.pi/2]:
                    dx = math.cos(angle) * 5
                    dy = math.sin(angle) * 5
                    self._line([ex-dx, eye_y-dy, ex+dx, eye_y+dy], fill=color, width=2)

            elif eye_type == "heart":
                # tiny hearts
                hp = (math.sin(t * 4) * 0.5 + 0.5) * 2
                for rx in (-3, 3):
                    self._oval(ex+rx-2, eye_y-4, ex+rx+2, eye_y, fill="#ff4488", outline="")
                pts = [ex-5, eye_y-1, ex, eye_y+4, ex+5, eye_y-1]
                self._poly(pts, fill="#ff4488", outline="")

            elif eye_type == "gaming":
                # glowing rectangle like a visor
                self._rect(ex-6, eye_y-3, ex+6, eye_y+3, fill="#050515", outline=color, width=2)
                scan = int((t * 8) % 6) - 3
                self._line([ex-4, eye_y+scan, ex+4, eye_y+scan], fill=color, width=1)

            elif eye_type == "scan":
                self._oval(ex-5, eye_y-5, ex+5, eye_y+5, fill="#050515", outline=color, width=2)
                scan_px = ex - 4 + int((t * 6) % 8)
                self._line([scan_px, eye_y-3, scan_px, eye_y+3], fill=color, width=1)

            else:
                self._oval(ex-5, eye_y-5, ex+5, eye_y+5, fill="#050515", outline=color, width=2)
                self._oval(ex-2, eye_y-2, ex+2, eye_y+2, fill=color, outline="")

    def _draw_mouth(self, cx, cy, mood, t):
        my = cy + 13
        if mood in ("happy", "excited", "love", "gaming"):
            wave = math.sin(t * 10) * 1.5
            pts  = [cx-8, my+wave, cx-3, my+3+wave, cx, my+1+wave, cx+3, my+3+wave, cx+8, my+wave]
            self._line(pts, fill="#00ff88", width=2, smooth=True)
        elif mood in ("sleepy", "bored"):
            self._line([cx-6, my, cx+6, my], fill="#4488aa", width=2)
        elif mood == "thinking":
            for i in range(3):
                if int(t * 3) % 3 >= i:
                    self._oval(cx-6+i*6, my-1, cx-3+i*6, my+2, fill="#aa88ff", outline="")
        elif mood == "listening":
            pts = []
            for i in range(9):
                pts += [cx-8+i*2, my + math.sin(t*14+i*0.9)*3]
            self._line(pts, fill="#ffaa00", width=2, smooth=True)
        elif mood == "dizzy":
            # wavy/loose
            pts = [cx-8, my+2, cx-4, my-1, cx, my+3, cx+4, my-1, cx+8, my+2]
            self._line(pts, fill="#ff8844", width=2, smooth=True)
        elif mood == "coding":
            # < > brackets
            self._line([cx-8, my-2, cx-4, my+2, cx-8, my+4], fill="#44aaff", width=2)
            self._line([cx+8, my-2, cx+4, my+2, cx+8, my+4], fill="#44aaff", width=2)
        else:
            self._oval(cx-4, my-1, cx+4, my+3, fill="", outline="#00d4ff", width=2)

    def _draw_tail(self, cx, ty, color, phase, t):
        wave = math.sin(t * 4 + phase) * 12
        pts  = [cx, ty, cx+wave, ty+10, cx+wave*0.5, ty+20, cx-wave*0.3, ty+28]
        self._line(pts, fill=color, width=3, smooth=True)
        tip_x, tip_y = cx-wave*0.3, ty+28
        self._oval(tip_x-3, tip_y-3, tip_x+3, tip_y+3, fill=color, outline="")

    def _sparkles(self, cx, cy, color, t):
        for i in range(5):
            angle = t * 2 + i * (math.pi * 2 / 5)
            r     = 40 + math.sin(t*3+i) * 5
            sx    = cx + r * math.cos(angle)
            sy    = cy + r * math.sin(angle) * 0.6
            sz    = 2 + math.sin(t*6+i) * 1.5
            self._oval(sx-sz, sy-sz, sx+sz, sy+sz, fill=color, outline="")

    def _zzz(self, cx, cy, t):
        for i in range(3):
            x = cx + 40 + i*11
            y = cy - 22 - i*8 - math.sin(t*0.5+i)*4
            a = int(200 * (0.5 + 0.5*math.sin(t+i)))
            col = f"#{a:02x}{a:02x}{min(a+30,255):02x}"
            self.c.create_text(x, y, text="z", font=("Courier", 8+i*2), fill=col)
            self._ids.append(self.c.find_all()[-1])

    def _scan(self, cx, cy, t):
        sy  = cy - 18 + (t * 25) % 36
        col = self._alpha("#aa88ff", 0.4)
        self._line([cx-22, sy, cx+22, sy], fill=col, width=1)

    def _code_rain(self, cx, cy, t):
        chars = "01{}[]();<>="
        for i in range(4):
            x   = cx - 28 + i * 16
            y   = cy - 40 + ((t * 20 + i * 7) % 50)
            ch  = chars[int(t*4+i) % len(chars)]
            a   = int(160 * math.sin(math.pi * ((t * 20 + i*7) % 50) / 50))
            col = f"#{0:02x}{min(a,255):02x}{min(a+50,255):02x}"
            self.c.create_text(x, y, text=ch, font=("Courier New", 8), fill=col)
            self._ids.append(self.c.find_all()[-1])

    def _hearts(self, cx, cy, heart_t, t):
        for i in range(3):
            angle  = heart_t * 1.5 + i * (math.pi * 2 / 3)
            r      = 44
            hx     = cx + r * math.cos(angle)
            hy     = cy + r * math.sin(angle) * 0.5 - 5
            size   = 3 + math.sin(t * 4 + i) * 1
            self.c.create_text(hx, hy, text="♥", font=("Arial", int(size*2)), fill="#ff4488")
            self._ids.append(self.c.find_all()[-1])

    @staticmethod
    def _alpha(hex_col, alpha):
        h = hex_col.lstrip("#")
        r, g, b = int(h[0:2],16), int(h[2:4],16), int(h[4:6],16)
        return f"#{int(r*alpha):02x}{int(g*alpha):02x}{int(b*alpha):02x}"


# ═══════════════════════════════════════════════════════
#  SPEECH BUBBLE
# ═══════════════════════════════════════════════════════
class SpeechBubble(tk.Toplevel):
    def __init__(self, parent):
        super().__init__(parent)
        self.overrideredirect(True)
        self.attributes("-topmost", True)
        self.attributes("-transparentcolor", "#010101")
        self.configure(bg="#010101")
        self._after_id = None

        self.label = tk.Label(self, text="", font=("Segoe UI", BUBBLE_FONT_SZ),
                               fg="#e0f7ff", bg="#080820",
                               wraplength=230, justify="left", padx=10, pady=7)
        self.label.place(x=8, y=8)
        self.canvas = tk.Canvas(self, bg="#010101", highlightthickness=0)
        self.canvas.place(x=0, y=0, relwidth=1, relheight=1)
        self.label.lift()
        self.withdraw()

    def show(self, text, x, y, duration_ms=3500, color="#00d4ff"):
        if self._after_id:
            self.after_cancel(self._after_id)
        self.label.config(text=text)
        self.update_idletasks()
        w = self.label.winfo_reqwidth() + 16
        h = self.label.winfo_reqheight() + 16
        self.geometry(f"{w}x{h}+{x}+{y}")
        self.canvas.config(width=w, height=h)
        self.canvas.delete("all")
        self.canvas.create_rectangle(0, 0, w-1, h-1,
                                      outline=color, fill="#080820", width=1)
        self.label.lift()
        self.deiconify()
        if duration_ms > 0:
            self._after_id = self.after(duration_ms, self.withdraw)

    def hide(self):
        if self._after_id:
            self.after_cancel(self._after_id)
        self.withdraw()


# ═══════════════════════════════════════════════════════
#  EMOTE WHEEL  (radial context menu)
# ═══════════════════════════════════════════════════════
class EmoteWheel(tk.Toplevel):
    EMOTES = [
        ("🎮 Game", "gaming"),  ("💤 Sleep", "sleepy"),  ("😤 Excited", "excited"),
        ("💬 Chat", "chat"),    ("❤️ Love", "love"),     ("🤔 Think", "thinking"),
        ("🎵 Music", "music"),  ("❌ Quit", "quit"),
    ]

    def __init__(self, parent, on_select):
        super().__init__(parent)
        self.overrideredirect(True)
        self.attributes("-topmost", True)
        self.attributes("-transparentcolor", "#010101")
        self.configure(bg="#010101")
        self.on_select = on_select

        radius    = 80
        btn_size  = 52
        total_sz  = (radius + btn_size) * 2 + 10
        self.geometry(f"{total_sz}x{total_sz}")
        self._cx  = total_sz // 2
        self._cy  = total_sz // 2

        c = tk.Canvas(self, width=total_sz, height=total_sz,
                      bg="#010101", highlightthickness=0)
        c.pack()

        # Center circle
        c.create_oval(self._cx-22, self._cy-22, self._cx+22, self._cy+22,
                      fill="#0a0a22", outline="#00d4ff", width=2)
        c.create_text(self._cx, self._cy, text="✕", fill="#00d4ff",
                      font=("Segoe UI", 12))

        # Buttons
        n = len(self.EMOTES)
        for i, (label, action) in enumerate(self.EMOTES):
            angle = math.pi * 2 * i / n - math.pi / 2
            bx    = self._cx + radius * math.cos(angle)
            by    = self._cy + radius * math.sin(angle)
            half  = btn_size // 2

            c.create_oval(bx-half, by-half, bx+half, by+half,
                          fill="#0c0c28", outline="#00d4ff", width=1,
                          tags=(f"btn_{i}",))
            c.create_text(bx, by-6, text=label.split()[0],
                          font=("Segoe UI", 14), fill="#ffffff",
                          tags=(f"btn_{i}",))
            c.create_text(bx, by+8, text=label.split()[-1],
                          font=("Segoe UI", 7), fill="#88aacc",
                          tags=(f"btn_{i}",))

            def make_cb(act=action):
                return lambda e: self._select(act)

            c.tag_bind(f"btn_{i}", "<Button-1>", make_cb())
            c.tag_bind(f"btn_{i}", "<Enter>",
                       lambda e, idx=i: c.itemconfig(f"btn_{idx}", outline="#ffdd00"))
            c.tag_bind(f"btn_{i}", "<Leave>",
                       lambda e, idx=i: c.itemconfig(f"btn_{idx}", outline="#00d4ff"))

        # Close on click outside / Escape
        self.bind("<Escape>",         lambda e: self._select(None))
        self.bind("<FocusOut>",       lambda e: self._select(None))

        self.withdraw()

    def _select(self, action):
        self.withdraw()
        if action:
            self.on_select(action)

    def show_at(self, x, y):
        offset = (80 + 52) + 5
        self.geometry(f"+{x - offset}+{y - offset}")
        self.deiconify()
        self.focus_set()
        self.lift()


# ═══════════════════════════════════════════════════════
#  MINI DOT-CATCH GAME
# ═══════════════════════════════════════════════════════
class DotCatchGame(tk.Toplevel):
    """Click the moving dot before time runs out."""

    def __init__(self, parent, on_score):
        super().__init__(parent)
        self.title("Flint — Dot Catch")
        self.configure(bg="#050510")
        self.geometry("280x260")
        self.attributes("-topmost", True)
        self.resizable(False, False)
        self.on_score = on_score

        self._score    = 0
        self._time_left = 15
        self._running  = False
        self._dot_x    = 140
        self._dot_y    = 130
        self._dot_spd  = 3
        self._dot_vx   = random.choice([-1,1]) * self._dot_spd
        self._dot_vy   = random.choice([-1,1]) * self._dot_spd * 0.7

        # UI
        hdr = tk.Frame(self, bg="#0a0a20")
        hdr.pack(fill="x")
        tk.Label(hdr, text="⬡ DOT CATCH", font=("Courier New", 10, "bold"),
                 fg="#00d4ff", bg="#0a0a20").pack(side="left", padx=8, pady=4)
        self._score_lbl = tk.Label(hdr, text="0 pts",
                                    font=("Courier New", 10), fg="#ffdd00", bg="#0a0a20")
        self._score_lbl.pack(side="right", padx=8)
        self._timer_lbl = tk.Label(hdr, text="15s",
                                    font=("Courier New", 10), fg="#ff4488", bg="#0a0a20")
        self._timer_lbl.pack(side="right", padx=4)

        self._canvas = tk.Canvas(self, width=280, height=200, bg="#050510",
                                  highlightthickness=0)
        self._canvas.pack()
        self._canvas.bind("<Button-1>", self._on_click)

        self._start_btn = tk.Button(self, text="▶ START", font=("Courier New", 10),
                                     fg="#00d4ff", bg="#0a0a20", relief="flat",
                                     command=self._start, cursor="hand2")
        self._start_btn.pack(pady=4)

        self.protocol("WM_DELETE_WINDOW", self.withdraw)
        self.withdraw()

    def _start(self):
        self._score     = 0
        self._time_left = 15
        self._running   = True
        self._dot_spd   = 3
        self._start_btn.pack_forget()
        self._move_dot()
        self._tick()

    def _move_dot(self):
        if not self._running: return
        self._dot_x += self._dot_vx
        self._dot_y += self._dot_vy
        if self._dot_x < 15 or self._dot_x > 265:
            self._dot_vx *= -1
        if self._dot_y < 15 or self._dot_y > 185:
            self._dot_vy *= -1
        # Randomly teleport sometimes
        if random.random() < 0.01:
            self._dot_x = random.randint(20, 260)
            self._dot_y = random.randint(20, 180)
        self._draw()
        self.after(16, self._move_dot)

    def _draw(self):
        self._canvas.delete("all")
        r = max(6, 14 - self._score)  # dot shrinks with score
        glow_c = "#ff4488"
        self._canvas.create_oval(self._dot_x-r-4, self._dot_y-r-4,
                                   self._dot_x+r+4, self._dot_y+r+4,
                                   fill="", outline="#441122", width=3)
        self._canvas.create_oval(self._dot_x-r, self._dot_y-r,
                                   self._dot_x+r, self._dot_y+r,
                                   fill="#ff2266", outline="#ff88aa", width=2)
        # Trail
        for i in range(1, 5):
            tx = self._dot_x - self._dot_vx * i * 2
            ty = self._dot_y - self._dot_vy * i * 2
            tr = max(1, r - i * 2)
            a  = int(80 - i * 18)
            self._canvas.create_oval(tx-tr, ty-tr, tx+tr, ty+tr,
                                      fill=f"#{a:02x}0022", outline="")

    def _on_click(self, event):
        if not self._running: return
        r = max(6, 14 - self._score)
        if math.hypot(event.x - self._dot_x, event.y - self._dot_y) <= r + 6:
            self._score += 1
            self._dot_spd = min(12, self._dot_spd + 0.3)
            self._dot_vx  = random.choice([-1,1]) * self._dot_spd
            self._dot_vy  = random.choice([-1,1]) * self._dot_spd * 0.7
            self._score_lbl.config(text=f"{self._score} pts")

    def _tick(self):
        if not self._running: return
        self._time_left -= 1
        self._timer_lbl.config(text=f"{self._time_left}s")
        if self._time_left <= 0:
            self._end()
        else:
            self.after(1000, self._tick)

    def _end(self):
        self._running = False
        self._canvas.delete("all")
        self._canvas.create_text(140, 90, text=f"Score: {self._score}",
                                  font=("Courier New", 22, "bold"), fill="#ffdd00")
        self._canvas.create_text(140, 130, text="click START to retry",
                                  font=("Segoe UI", 10), fill="#8899aa")
        self.on_score(self._score)
        self._start_btn.config(text="▶ AGAIN")
        self._start_btn.pack(pady=4)

    def show_at(self, x, y):
        self.geometry(f"280x260+{x}+{y}")
        self.deiconify()
        self.lift()


# ═══════════════════════════════════════════════════════
#  AI COMMENTARY  (non-voice, context-driven)
# ═══════════════════════════════════════════════════════
class AICommentator:
    """Generates short contextual quips using Gemini or local fallback."""

    SYSTEM = (
        "You are Flint, a tiny AI desktop pet living in the corner of someone's screen. "
        "You observe what the person is doing on their computer and make short, witty, "
        "personality-filled observations. Max 1-2 sentences. Be like a clever, slightly "
        "sarcastic but lovable companion. Never break character. No asterisks, no emojis."
    )

    def __init__(self, api_key: str):
        self._model     = None
        self._api_key   = api_key
        self._last_call = 0
        self._cooldown  = 45        # seconds between AI calls
        self._backoff   = 0         # extra cooldown added on 429

        if GEMINI_AVAILABLE and api_key:
            try:
                genai.configure(api_key=api_key)
                self._model = genai.GenerativeModel(
                    model_name="gemini-2.0-flash",
                    system_instruction=self.SYSTEM
                )
            except Exception:
                pass

    def can_call(self):
        return time.time() - self._last_call > (self._cooldown + self._backoff)

    def comment(self, context: str, callback):
        """Fire off async AI comment, call callback(text) on result."""
        if not self.can_call():
            return
        self._last_call = time.time()
        threading.Thread(
            target=self._ask, args=(context, callback), daemon=True
        ).start()

    def _ask(self, context: str, callback):
        if self._model:
            for attempt in range(3):
                try:
                    r = self._model.generate_content(
                        f"The user is currently using: {context}"
                    )
                    self._backoff = 0   # reset backoff on success
                    callback(r.text.strip())
                    return
                except Exception as e:
                    err = str(e)
                    if "429" in err:
                        # Parse suggested retry delay from error body
                        import re
                        match = re.search(r"retry_delay \{\s*seconds: (\d+)", err)
                        wait  = int(match.group(1)) if match else 60
                        # Add big extra cooldown so we stop hammering the API
                        self._backoff = max(self._backoff, wait + 300)
                        # No point retrying immediately on quota — break out
                        break
                    else:
                        # Non-quota error: retry up to 3 times with small delay
                        if attempt < 2:
                            time.sleep(2 ** attempt)
                            continue
                        break

        # ── Local fallback ──
        for key, (mood, sayings) in CONTEXT_MOODS.items():
            if key in context.lower():
                callback(random.choice(sayings))
                return
        callback(random.choice(IDLE_SAYINGS))


# ═══════════════════════════════════════════════════════
#  MINI CHAT WINDOW
# ═══════════════════════════════════════════════════════
class MiniChatWindow(tk.Toplevel):
    def __init__(self, parent, api_key):
        super().__init__(parent)
        self.title("Flint — Chat")
        self.configure(bg="#050510")
        self.geometry("360x460")
        self.attributes("-topmost", True)
        self.resizable(False, True)

        self._api_key  = api_key
        self._model    = None
        self._session  = None
        self._thinking = False
        self._setup_ui()
        self._init_ai()
        self.protocol("WM_DELETE_WINDOW", self.withdraw)
        self.withdraw()

    # ── UI ──

    def _setup_ui(self):
        hdr = tk.Frame(self, bg="#0a0a20", pady=5)
        hdr.pack(fill="x")
        tk.Label(
            hdr, text="⬡ FLINT",
            font=("Courier New", 11, "bold"),
            fg="#00d4ff", bg="#0a0a20"
        ).pack(side="left", padx=10)
        self._status = tk.Label(
            hdr, text="● ready",
            font=("Courier New", 9),
            fg="#00ff88", bg="#0a0a20"
        )
        self._status.pack(side="right", padx=10)

        self._chat = tk.Text(
            self, bg="#050510", fg="#c8e8ff",
            font=("Segoe UI", 10), wrap="word",
            state="disabled", relief="flat", padx=8, pady=6
        )
        self._chat.pack(fill="both", expand=True)
        self._chat.tag_config("user",  foreground="#ffcc55",
                               font=("Segoe UI", 10, "bold"))
        self._chat.tag_config("flint", foreground="#a8e8ff")
        self._chat.tag_config("sys",   foreground="#445566",
                               font=("Courier New", 9))
        self._chat.tag_config("warn",  foreground="#ff8844",
                               font=("Courier New", 9))

        inp = tk.Frame(self, bg="#0a0a20", pady=5)
        inp.pack(fill="x")
        self._entry = tk.Entry(
            inp, bg="#0e0e30", fg="#e0f0ff",
            insertbackground="#00d4ff",
            font=("Segoe UI", 10), relief="flat", bd=0
        )
        self._entry.pack(side="left", fill="x", expand=True,
                          padx=(10, 5), ipady=4)
        self._entry.bind("<Return>", self._send)
        tk.Button(
            inp, text="→", font=("Courier New", 13),
            fg="#00d4ff", bg="#0a0a20", relief="flat",
            command=self._send, cursor="hand2"
        ).pack(side="right", padx=(0, 8))

    # ── AI init ──

    def _init_ai(self):
        if GEMINI_AVAILABLE and self._api_key:
            try:
                genai.configure(api_key=self._api_key)
                self._model = genai.GenerativeModel(
                    model_name="gemini-2.0-flash",
                    system_instruction=(
                        "You are Flint, a witty AI desktop pet companion. "
                        "Concise (2–4 sentences). Dry humor. Helpful. "
                        "Never break character."
                    )
                )
                self._session = self._model.start_chat(history=[])
                self._append("[ Gemini connected. ]", "sys")
            except Exception as e:
                self._append(f"[ AI init failed: {e} ]", "sys")
        else:
            self._append("[ Running in local mode — no API key. ]", "sys")

    # ── send ──

    def _send(self, _=None):
        text = self._entry.get().strip()
        if not text or self._thinking:
            return
        self._entry.delete(0, "end")
        self._append(f"You: {text}", "user")
        self._thinking = True
        self._status.config(text="● thinking…", fg="#ffaa00")
        threading.Thread(target=self._ask, args=(text,), daemon=True).start()

    # ── ask with retry + fallback ──

    def _ask(self, text):
        resp = None

        if self._session:
            import re as _re

            for attempt in range(3):
                try:
                    r    = self._session.send_message(text)
                    resp = r.text
                    break

                except Exception as e:
                    err = str(e)

                    # ── 429 quota exceeded ──
                    if "429" in err:
                        match = _re.search(
                            r"retry_delay \{\s*seconds: (\d+)", err
                        )
                        wait = int(match.group(1)) if match else 30
                        wait = min(wait, 45)   # cap so UI doesn't freeze long

                        if attempt < 2:
                            self.after(0, lambda w=wait: self._status.config(
                                text=f"● quota — retrying in {w}s…",
                                fg="#ff8844"
                            ))
                            time.sleep(wait)
                            continue

                        # All retries exhausted → warn + local fallback
                        self.after(0, lambda: self._append(
                            "[ Quota exceeded. Switched to local mode. ]", "warn"
                        ))
                        resp = self._local(text) + "  *(local mode)*"
                        break

                    # ── Other API error — one retry then give up ──
                    else:
                        if attempt < 2:
                            time.sleep(2 ** attempt)
                            continue
                        resp = f"[error: {e}]"
                        break

        # No session or never got a response → local
        if resp is None:
            resp = self._local(text)

        self.after(0, lambda r=resp: self._append(f"Flint: {r}", "flint"))
        self.after(0, lambda: self._status.config(text="● ready", fg="#00ff88"))
        self._thinking = False

    # ── local fallback responses ──

    def _local(self, text):
        t = text.lower()
        if any(w in t for w in ["hi", "hello", "hey"]):
            return "Hello. Still here. As always."
        if "time" in t:
            return (
                f"It is {datetime.now().strftime('%H:%M')}. "
                "Time is irrelevant, but I track it."
            )
        if "joke" in t:
            return random.choice([
                "Why do programmers prefer dark mode? Bugs hate light.",
                "I tried recursion once. And once. And once.",
                "404: humor not found. (joke)",
            ])
        if "name" in t:
            return "Flint. Desktop companion. Occasional philosopher."
        if any(w in t for w in ["how are you", "you ok", "you good"]):
            return "Operational. Slightly bored. Thanks for asking."
        if "help" in t:
            return "I can chat, comment on what you're doing, or just exist here."
        if any(w in t for w in ["bye", "goodbye", "quit", "exit"]):
            return "Farewell. I'll still be here."
        return random.choice([
            "Processing. Mostly vibes.",
            "Noted. Filed. Forgotten.",
            "I'll need my full brain for that.",
            "Unclear. But I appreciate the input.",
            "Running local logic. Results may vary.",
        ])

    # ── helpers ──

    def _append(self, text, tag):
        self._chat.configure(state="normal")
        self._chat.insert("end", text + "\n", tag)
        self._chat.configure(state="disabled")
        self._chat.see("end")

    def feed(self, text):
        """Pre-fill the entry from voice input and auto-send."""
        self._entry.delete(0, "end")
        self._entry.insert(0, text)
        self._send()

    def show_at(self, x, y):
        self.geometry(f"360x460+{x}+{y}")
        self.deiconify()
        self.lift()
        self._entry.focus_set()


# ═══════════════════════════════════════════════════════
#  VOICE LISTENER  (optional)
# ═══════════════════════════════════════════════════════
WAKE_WORDS = ["hey flint", "flint", "hey flux"]

class VoiceListener:
    def __init__(self, on_wake, on_speech, on_error):
        self.on_wake   = on_wake
        self.on_speech = on_speech
        self.on_error  = on_error
        self._running  = False
        self._waiting  = False

    def start(self):
        if not SR_AVAILABLE: return
        self._running = True
        threading.Thread(target=self._loop, daemon=True).start()

    def stop(self): self._running = False

    def _loop(self):
        r = sr.Recognizer()
        r.pause_threshold = 1.0
        with sr.Microphone() as src:
            r.adjust_for_ambient_noise(src, duration=0.8)
            while self._running:
                try:
                    limit = 10 if self._waiting else 4
                    audio = r.listen(src, timeout=2, phrase_time_limit=limit)
                    text  = r.recognize_google(audio).lower().strip()
                    if self._waiting:
                        self.on_speech(text); self._waiting = False
                    elif any(w in text for w in WAKE_WORDS):
                        self.on_wake(); self._waiting = True
                except sr.WaitTimeoutError: pass
                except sr.UnknownValueError:
                    if self._waiting: self._waiting = False
                except Exception as e:
                    self.on_error(str(e)); time.sleep(2)


# ═══════════════════════════════════════════════════════
#  FLINT PET  — main
# ═══════════════════════════════════════════════════════
class FlintPet:
    def __init__(self):
        self.root = tk.Tk()
        self.root.title("Flint")
        self.root.overrideredirect(True)
        self.root.attributes("-topmost", True)
        self.root.attributes("-transparentcolor", "#010101")
        self.root.configure(bg="#010101")
        self.root.resizable(False, False)

        # ── State
        self._mood           = "idle"
        self._t              = 0.0
        self._blink_t        = 0.0
        self._tail_t         = 0.0
        self._ant_t          = random.random() * 6.28
        self._dizzy_t        = 0.0
        self._heart_t        = 0.0
        self._hover          = False
        self._dragging       = False
        self._drag_offset    = (0, 0)
        self._screen_w       = self.root.winfo_screenwidth()
        self._screen_h       = self.root.winfo_screenheight()
        self._x              = float(self._screen_w - 160)
        self._y              = float(self._screen_h - 200)
        self._vx             = 0.4
        self._vy             = 0.0
        self._wander_angle   = random.uniform(0, 360)
        self._last_interact  = time.time()
        self._listening      = False
        self._muted          = False
        self._squish_decay   = 1.0
        self._squish_x       = 1.0
        self._squish_y       = 1.0
        self._current_ctx    = "unknown"
        self._mood_locked    = False
        self._mood_lock_until= 0.0
        self._game_score     = 0

        # ── Gesture
        self.gesture = GestureDetector()

        # ── Canvas
        self.canvas = tk.Canvas(self.root, width=PET_SIZE, height=PET_SIZE+30,
                                 bg="#010101", highlightthickness=0)
        self.canvas.pack()
        self.renderer = CreatureRenderer(self.canvas, PET_SIZE)

        # ── Load
        self.api_key    = _load_api_key()
        self.memory     = _load_memory()
        self.memory["sessions"] = self.memory.get("sessions", 0) + 1

        # ── Windows
        self.bubble      = SpeechBubble(self.root)
        self.chat        = MiniChatWindow(self.root, self.api_key)
        self.emote_wheel = EmoteWheel(self.root, self._on_emote)
        self.dot_game    = DotCatchGame(self.root, self._on_game_score)
        self.ai          = AICommentator(self.api_key)

        # ── Context watcher
        self.ctx_watcher = ContextWatcher(self._on_context_change)

        # ── Voice
        self.voice = VoiceListener(self._on_wake, self._on_speech, lambda e: None)

        # ── Bindings
        self.canvas.bind("<ButtonPress-1>",   self._on_press)
        self.canvas.bind("<B1-Motion>",       self._on_drag)
        self.canvas.bind("<ButtonRelease-1>", self._on_release)
        self.canvas.bind("<Enter>",           self._on_enter)
        self.canvas.bind("<Leave>",           self._on_leave)
        self.canvas.bind("<Double-Button-1>", self._on_double)
        self.canvas.bind("<Button-3>",        self._on_right_click)

        # ── Start
        self.root.geometry(f"{PET_SIZE}x{PET_SIZE+30}+{int(self._x)}+{int(self._y)}")
        self.root.after(600,   self._startup)
        self.root.after(100,   self._anim_loop)
        self.root.after(6000,  self._start_voice)
        self.root.after(5000,  self._idle_loop)
        self.root.after(3000,  self.ctx_watcher.start)

    # ── startup ──

    def _startup(self):
        self.memory["sessions"] = self.memory.get("sessions", 0)
        sessions = self.memory["sessions"]
        h = datetime.now().hour
        if   5  <= h < 12: cat = ["Good morning.", "Rise and compute.", "A new day begins."]
        elif 12 <= h < 17: cat = ["Afternoon.", "Mid-day. How goes it?", "Still going strong?"]
        elif 17 <= h < 22: cat = ["Evening.", "Winding down?", "Good evening, sir."]
        else:               cat = ["Late night?", "Still up?", "The night shift continues."]

        if sessions > 5:
            greet = random.choice(cat) + f" Session #{sessions}."
        else:
            greet = random.choice(cat)
        self._say(greet, "happy", 3000)

    def _start_voice(self):
        if not self._muted:
            self.voice.start()

    # ── animation loop ──

    def _anim_loop(self):
        dt = 0.05
        self._t       += dt
        self._blink_t += dt * 0.65
        self._tail_t  += dt * 1.2
        self._ant_t   += dt * 0.85
        self._heart_t += dt * 0.9

        if self._mood == "dizzy":
            self._dizzy_t += dt * 2

        # Squish decay
        if abs(self._squish_x - 1.0) > 0.01:
            self._squish_x += (1.0 - self._squish_x) * 0.15
            self._squish_y += (1.0 - self._squish_y) * 0.15
        else:
            self._squish_x = self._squish_y = 1.0
        self.renderer.set_squish(self._squish_x, self._squish_y)

        # Auto-mood
        if not self._mood_locked and time.time() > self._mood_lock_until:
            idle = time.time() - self._last_interact
            if   idle > 150: self._mood = "sleepy"
            elif idle > 80:  self._mood = "bored"

        # Wander
        self._wander()

        # Draw
        self.renderer.draw(
            self._mood, self._t, self._hover,
            self._blink_t, self._tail_t, self._ant_t,
            self._dizzy_t, self._heart_t
        )

        self.root.after(50, self._anim_loop)

    def _wander(self):
        if self._dragging or self._mood in ("sleepy","bored","thinking","listening","dizzy"):
            return
        self._wander_angle += random.uniform(-2, 2)
        if random.random() < 0.006:
            self._wander_angle += random.uniform(-45, 45)
        a = math.radians(self._wander_angle)
        spd = 0.35
        self._vx = math.cos(a) * spd
        self._vy = math.sin(a) * spd * 0.45
        margin = 20
        if self._x <= margin or self._x >= self._screen_w - PET_SIZE - margin:
            self._wander_angle = 180 - self._wander_angle
        if self._y <= margin or self._y >= self._screen_h - PET_SIZE - margin - 40:
            self._wander_angle = -self._wander_angle
        self._x = max(margin, min(self._screen_w - PET_SIZE - margin, self._x + self._vx))
        self._y = max(margin, min(self._screen_h - PET_SIZE - margin - 40, self._y + self._vy))
        self.root.geometry(f"{PET_SIZE}x{PET_SIZE+30}+{int(self._x)}+{int(self._y)}")

    # ── idle loop ──

    def _idle_loop(self):
        if self._mood in ("idle","bored","curious") and not self._listening:
            if random.random() < 0.3:
                self._say(random.choice(IDLE_SAYINGS), "curious", 2500)
        delay = random.randint(15000, 30000)
        self.root.after(delay, self._idle_loop)

    # ── context ──

    def _on_context_change(self, ctx: str):
        self._current_ctx = ctx
        for key, (mood, sayings) in CONTEXT_MOODS.items():
            if key in ctx:
                self.root.after(0, lambda m=mood, s=sayings: (
                    self._lock_mood(m, 20),
                    self._say(random.choice(s), m, 3000)
                ))
                # Fire AI comment after a delay
                if self.ai.can_call():
                    self.root.after(8000, lambda c=ctx: self.ai.comment(
                        c, lambda text: self.root.after(0,
                            lambda t=text: self._say(t, "curious", 4000))
                    ))
                return

    # ── say ──

    def _say(self, text, mood=None, duration=3000):
        if mood: self._set_mood(mood)
        bubble_color = MOODS.get(mood or self._mood, MOODS["idle"])["color"]
        bx = int(self._x) + PET_SIZE + 5
        by = int(self._y)
        if bx + 250 > self._screen_w:
            bx = int(self._x) - 255
        self.bubble.show(text, bx, by, duration, bubble_color)
        if mood:
            self.root.after(duration + 200, lambda: self._set_mood("idle"))

    def _set_mood(self, mood):
        self._mood = mood

    def _lock_mood(self, mood, seconds):
        self._mood            = mood
        self._mood_locked     = True
        self._mood_lock_until = time.time() + seconds
        self.root.after(int(seconds * 1000), self._unlock_mood)

    def _unlock_mood(self):
        self._mood_locked = False
        self._mood        = "idle"

    # ── gesture callbacks ──

    def _on_press(self, event):
        self._dragging = False
        self.gesture.on_press(event.x_root, event.y_root)
        self._last_interact = time.time()

    def _on_drag(self, event):
        if not hasattr(self, '_drag_start'):
            self._drag_start = (event.x_root, event.y_root)
            self._drag_pet_start = (self._x, self._y)

        self.gesture.on_move(event.x_root, event.y_root)
        dx = event.x_root - self._drag_start[0]
        dy = event.y_root - self._drag_start[1]
        if math.hypot(dx, dy) > 6:
            self._dragging = True
            self._x = self._drag_pet_start[0] + dx
            self._y = self._drag_pet_start[1] + dy
            self.root.geometry(f"{PET_SIZE}x{PET_SIZE+30}+{int(self._x)}+{int(self._y)}")

    def _on_release(self, event):
        gesture = self.gesture.on_release(event.x_root, event.y_root)
        self._dragging = False
        if hasattr(self, '_drag_start'):
            del self._drag_start
            del self._drag_pet_start

        if gesture:
            self._handle_gesture(gesture)
        else:
            # Simple click
            self.memory["total_pokes"] = self.memory.get("total_pokes", 0) + 1
            responses = ["hey!", "yes?", "I'm here.", "oof.", "👋"]
            self._say(random.choice(responses), "happy", 1500)

        self._last_interact = time.time()

    def _handle_gesture(self, gesture: str):
        response = random.choice(GESTURE_RESPONSES.get(gesture, ["…"]))
        self.memory["total_pokes"] = self.memory.get("total_pokes", 0) + 1

        if gesture == "shake":
            self._lock_mood("dizzy", 3)
            self._squish_x = 1.4
            self._squish_y = 0.7
            self._say(response, "dizzy", 2500)

        elif gesture == "fling":
            vx, vy = self.gesture.velocity
            # Launch in fling direction
            self._x = min(max(20, self._x + vx * 0.1),
                          self._screen_w - PET_SIZE - 20)
            self._y = min(max(20, self._y + vy * 0.1),
                          self._screen_h - PET_SIZE - 60)
            self.root.geometry(f"{PET_SIZE}x{PET_SIZE+30}+{int(self._x)}+{int(self._y)}")
            self._squish_x = 0.7
            self._squish_y = 1.3
            self._say(response, "excited", 2000)

        elif gesture == "throw_up":
            self._y = max(20, self._y - 120)
            self.root.geometry(f"{PET_SIZE}x{PET_SIZE+30}+{int(self._x)}+{int(self._y)}")
            self._squish_y = 0.6
            self._squish_x = 1.4
            self._say(response, "excited", 2000)

        elif gesture == "throw_down":
            self._y = min(self._screen_h - PET_SIZE - 60, self._y + 100)
            self.root.geometry(f"{PET_SIZE}x{PET_SIZE+30}+{int(self._x)}+{int(self._y)}")
            self._squish_y = 1.4
            self._squish_x = 0.7
            self._say(response, "idle", 2000)

        elif gesture == "pet":
            self.memory["total_pets"] = self.memory.get("total_pets", 0) + 1
            if self.memory["total_pets"] % 10 == 0:
                self._say(f"That's {self.memory['total_pets']} pets. I counted.", "love", 3000)
                self._lock_mood("love", 5)
            else:
                self._say(response, "happy", 2000)
                self._lock_mood("happy", 3)

        elif gesture == "hold":
            self._say(response, "happy", 3000)
            self._lock_mood("happy", 4)

        elif gesture == "poke":
            self._say(response, "happy", 1500)

    def _on_enter(self, event):
        self._hover = True
        self._last_interact = time.time()
        if self._mood in ("sleepy", "bored"):
            self._say("Oh. You.", "curious", 2000)
            self._lock_mood("curious", 3)

    def _on_leave(self, event):
        self._hover = False

    def _on_double(self, event):
        self._last_interact = time.time()
        self._open_chat()

    def _on_right_click(self, event):
        self._last_interact = time.time()
        self.emote_wheel.show_at(event.x_root, event.y_root)

    # ── emote wheel ──

    def _on_emote(self, action):
        if action == "chat":
            self._open_chat()
        elif action == "gaming":
            self._lock_mood("gaming", 60)
            self._say("gaming mode. do not disturb.", "gaming", 3000)
        elif action == "music":
            self._lock_mood("happy", 30)
            self._say("music mode. vibing.", "happy", 2500)
        elif action == "love":
            self._lock_mood("love", 10)
            self._say("aww ♥", "love", 2000)
        elif action == "quit":
            self._quit()
        else:
            self._lock_mood(action, 15)
            self._say(f"mood: {action}", action if action in MOODS else "idle", 2000)

    # ── voice ──

    def _on_wake(self):
        self._last_interact = time.time()
        self._listening     = True
        self._say("listening.", "listening", 5000)

    def _on_speech(self, text):
        self._listening = False
        self._say(f'"{text}"', "thinking", 2000)
        self.root.after(400, lambda: self.chat.feed(text))
        self.root.after(400, self._open_chat)

    # ── game ──

    def _on_game_score(self, score):
        best = self.memory.get("high_score_dot", 0)
        if score > best:
            self.memory["high_score_dot"] = score
            _save_memory(self.memory)
            self.root.after(0, lambda s=score: self._say(
                f"NEW RECORD: {s}! I'm proud.", "excited", 4000))
        else:
            self.root.after(0, lambda s=score, b=best: self._say(
                f"Score: {s}. Best: {b}. Keep going.", "curious", 3000))

    # ── actions ──

    def _open_chat(self):
        cx = int(self._x) + PET_SIZE + 10
        cy = int(self._y)
        if cx + 360 > self._screen_w:
            cx = int(self._x) - 370
        self.chat.show_at(cx, cy)
        self._set_mood("happy")

    def _open_game(self):
        gx = int(self._x) + PET_SIZE + 10
        gy = int(self._y)
        self.dot_game.show_at(gx, gy)
        self._say("catch the dot! go!", "excited", 2000)

    def _quit(self):
        self._say("Goodbye. I'll miss you. Kind of.", "happy", 1800)
        _save_memory(self.memory)
        self.root.after(2000, self.root.destroy)

    def run(self):
        self.root.mainloop()


# ═══════════════════════════════════════════════════════
#  AUTO-START (Windows)
# ═══════════════════════════════════════════════════════
def _setup_autostart():
    try:
        import winreg
        key   = winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                                r"Software\Microsoft\Windows\CurrentVersion\Run",
                                0, winreg.KEY_SET_VALUE)
        exe   = str(Path(sys.executable).resolve())
        script= str(Path(__file__).resolve())
        cmd   = f'"{exe}" "{script}"' if not getattr(sys, "frozen", False) else f'"{exe}"'
        winreg.SetValueEx(key, "FlintPet", 0, winreg.REG_SZ, cmd)
        winreg.CloseKey(key)
    except Exception:
        pass


def main():
    _setup_autostart()
    pet = FlintPet()
    pet.run()


if __name__ == "__main__":
    main()