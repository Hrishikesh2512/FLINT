"""F.L.I.N.T desktop shell — "QUANTUM CONSOLE" edition.

A PyQt6 HUD styled like a premium gaming console: neon-glow panel borders
with travelling energy sweeps, an animated reactor HUD, loading-state
animations, a live system terminal (fed by core.log_interceptor), and a
remote-link indicator for WebSocket clients.

Public surface (kept stable for main.py and every actions/ module):

    ui = FlintUI("face.png")
    ui.on_text_command = callback            # text commands from the user
    ui.set_state("LISTENING" | "THINKING" | "SPEAKING" | "PROCESSING")
    ui.write_log("Flint: hello")
    ui.muted / ui.current_file
    ui.wait_for_api_key()
    ui.start_speaking() / ui.stop_speaking()
    ui.root.mainloop()

New surface:
    ui.set_link_clients(n)                   # remote phone clients online
    ui.attach_pipeline(pipeline)             # show live worker activity
"""

from __future__ import annotations

import json
import math
import os
import platform
import random
import subprocess
import sys
import threading
import time
from pathlib import Path

import psutil

from PyQt6.QtCore import (
    QEasingCurve, QPointF, QPropertyAnimation, QRectF, Qt, QTimer, pyqtSignal,
)
from PyQt6.QtGui import (
    QBrush, QColor, QDragEnterEvent, QDropEvent, QFont, QKeySequence,
    QLinearGradient, QPainter, QPainterPath, QPen, QPixmap, QRadialGradient,
    QShortcut, QTextCursor, QTextCharFormat,
)
from PyQt6.QtWidgets import (
    QApplication, QFileDialog, QFrame, QGraphicsOpacityEffect, QHBoxLayout,
    QLabel, QLineEdit, QMainWindow, QPlainTextEdit, QPushButton, QSizePolicy,
    QStackedLayout, QTextEdit, QVBoxLayout, QWidget,
)


def _base_dir() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).parent
    return Path(__file__).resolve().parent


BASE_DIR   = _base_dir()
CONFIG_DIR = BASE_DIR / "config"
API_FILE   = CONFIG_DIR / "api_keys.json"

_DEFAULT_W, _DEFAULT_H = 1180, 760
_MIN_W,     _MIN_H     = 900, 620

_OS = platform.system()

_MONO = "Cascadia Mono" if _OS == "Windows" else "Menlo"
_MONO_FALLBACK = "Consolas"


def F(size: int, bold: bool = False) -> QFont:
    f = QFont(_MONO, size, QFont.Weight.Bold if bold else QFont.Weight.Normal)
    if not f.exactMatch():
        f = QFont(_MONO_FALLBACK, size,
                  QFont.Weight.Bold if bold else QFont.Weight.Normal)
    f.setStyleHint(QFont.StyleHint.Monospace)
    return f


# ── Theme: Quantum Console ────────────────────────────────────────────────────
class T:
    BG        = "#03050a"        # void black
    PANEL     = "#060a12"
    PANEL2    = "#0a101c"
    INSET     = "#04070d"
    BORDER    = "#11203a"
    BORDER_HI = "#1d3a66"

    CYAN      = "#00e5ff"        # primary neon
    CYAN_DIM  = "#0e6a82"
    CYAN_GHO  = "#06222e"
    MAGENTA   = "#ff2d78"        # alert / muted
    VIOLET    = "#7c4dff"
    AMBER     = "#ffb300"
    EMERALD   = "#00e676"
    EMERALD_D = "#0a5c38"
    RED       = "#ff1744"

    TEXT      = "#9adef0"
    TEXT_DIM  = "#3a5a78"
    TEXT_MED  = "#5e8cb0"
    WHITE     = "#eafdff"


def qcol(h: str, a: int = 255) -> QColor:
    c = QColor(h)
    c.setAlpha(a)
    return c


_STATE_COLORS = {
    "LISTENING":    T.EMERALD,
    "SPEAKING":     T.AMBER,
    "THINKING":     T.CYAN,
    "PROCESSING":   T.VIOLET,
    "MUTED":        T.MAGENTA,
    "INITIALISING": T.CYAN,
}


# ── System metrics (background sampler) ──────────────────────────────────────
class _SysMetrics:
    def __init__(self):
        self.cpu = self.mem = self.net = 0.0
        self.gpu = self.tmp = -1.0
        self._lock = threading.Lock()
        self._last_net = psutil.net_io_counters()
        self._last_net_t = time.time()
        threading.Thread(target=self._loop, daemon=True,
                         name="FlintMetrics").start()

    def _loop(self):
        while True:
            try:
                self._update()
            except Exception:
                pass
            time.sleep(1.5)

    def _update(self):
        cpu = psutil.cpu_percent(interval=None)
        mem = psutil.virtual_memory().percent
        nc, now = psutil.net_io_counters(), time.time()
        dt = now - self._last_net_t
        net = (((nc.bytes_sent - self._last_net.bytes_sent)
                + (nc.bytes_recv - self._last_net.bytes_recv)) / dt
               / (1024 * 1024)) if dt > 0 else 0.0
        self._last_net, self._last_net_t = nc, now
        gpu, tmp = self._gpu(), self._temp()
        with self._lock:
            self.cpu, self.mem, self.net, self.gpu, self.tmp = cpu, mem, net, gpu, tmp

    @staticmethod
    def _gpu() -> float:
        try:
            r = subprocess.run(
                ["nvidia-smi", "--query-gpu=utilization.gpu",
                 "--format=csv,noheader,nounits"],
                capture_output=True, text=True, timeout=2,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            if r.returncode == 0:
                vals = [float(v) for v in r.stdout.split() if v.strip()]
                if vals:
                    return sum(vals) / len(vals)
        except Exception:
            pass
        return -1.0

    @staticmethod
    def _temp() -> float:
        try:
            temps = psutil.sensors_temperatures()
            for name in ("coretemp", "k10temp", "cpu_thermal", "acpitz",
                         "zenpower", "it8688"):
                if temps.get(name):
                    return temps[name][0].current
            for entries in temps.values():
                if entries:
                    return entries[0].current
        except Exception:
            pass
        return -1.0

    def snapshot(self) -> dict:
        with self._lock:
            return {"cpu": self.cpu, "mem": self.mem, "net": self.net,
                    "gpu": self.gpu, "tmp": self.tmp}


_metrics = _SysMetrics()


# ── NeonPanel: glowing border container with travelling energy sweep ─────────
class NeonPanel(QFrame):
    _shared_phase = 0.0
    _phase_timer: QTimer | None = None
    _instances: list["NeonPanel"] = []

    def __init__(self, glow: str = T.CYAN, radius: int = 10,
                 sweep: bool = True, parent=None):
        super().__init__(parent)
        self._glow = glow
        self._radius = radius
        self._sweep = sweep
        self._intensity = 1.0
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        NeonPanel._instances.append(self)
        if NeonPanel._phase_timer is None:
            NeonPanel._phase_timer = QTimer()
            NeonPanel._phase_timer.timeout.connect(NeonPanel._advance)
            NeonPanel._phase_timer.start(33)

    @classmethod
    def _advance(cls):
        cls._shared_phase = (cls._shared_phase + 0.006) % 1.0
        for w in cls._instances:
            if w.isVisible():
                w.update()

    def set_glow(self, color: str, intensity: float = 1.0):
        self._glow = color
        self._intensity = intensity
        self.update()

    def paintEvent(self, _):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        W, H = self.width(), self.height()
        r = QRectF(1.5, 1.5, W - 3, H - 3)

        path = QPainterPath()
        path.addRoundedRect(r, self._radius, self._radius)
        p.fillPath(path, QBrush(qcol(T.PANEL, 235)))

        # layered outer glow
        for i, (wd, al) in enumerate([(3.5, 18), (2.2, 42), (1.1, 110)]):
            p.setPen(QPen(qcol(self._glow, int(al * self._intensity)), wd))
            p.setBrush(Qt.BrushStyle.NoBrush)
            p.drawPath(path)

        # travelling energy sweep along the border
        if self._sweep:
            per = 2 * (W + H)
            pos = (NeonPanel._shared_phase * per) % per
            grad = QLinearGradient(0, 0, W, 0)
            seg = max(0.06, 90.0 / max(per, 1))
            t0 = pos / per
            g = QColor(self._glow)
            for k in range(-2, 3):
                tt = t0 + k * seg * 0.5
                if 0.0 <= tt <= 1.0:
                    g.setAlpha(max(0, 160 - abs(k) * 60))
                    grad.setColorAt(tt, g)
            pen = QPen(QBrush(grad), 1.6)
            p.setPen(pen)
            p.drawPath(path)


# ── Spinner: loading-state arc animation ─────────────────────────────────────
class Spinner(QWidget):
    def __init__(self, color: str = T.CYAN, size: int = 18, parent=None):
        super().__init__(parent)
        self._color = color
        self._angle = 0
        self.setFixedSize(size, size)
        self._tmr = QTimer(self)
        self._tmr.timeout.connect(self._spin)

    def start(self):
        self._tmr.start(16)
        self.show()

    def stop(self):
        self._tmr.stop()
        self.hide()

    def set_color(self, color: str):
        self._color = color

    def _spin(self):
        self._angle = (self._angle + 7) % 360
        self.update()

    def paintEvent(self, _):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        rect = QRectF(2, 2, self.width() - 4, self.height() - 4)
        p.setPen(QPen(qcol(self._color, 50), 2))
        p.drawEllipse(rect)
        p.setPen(QPen(qcol(self._color), 2.4))
        p.drawArc(rect, int(-self._angle * 16), 100 * 16)
        p.setPen(QPen(qcol(self._color, 120), 1.4))
        p.drawArc(rect, int((-self._angle - 140) * 16), 50 * 16)


# ── HUD reactor canvas ───────────────────────────────────────────────────────
class HudCanvas(QWidget):
    def __init__(self, face_path: str, parent=None):
        super().__init__(parent)
        self.setAttribute(Qt.WidgetAttribute.WA_OpaquePaintEvent)
        self.setMinimumSize(320, 320)
        self.setSizePolicy(QSizePolicy.Policy.Expanding,
                           QSizePolicy.Policy.Expanding)

        self.muted = False
        self.speaking = False
        self.state = "INITIALISING"

        self._tick = 0
        self._scale, self._tgt_scale = 1.0, 1.0
        self._halo, self._tgt_halo = 55.0, 55.0
        self._last_t = time.time()
        self._scan, self._scan2 = 0.0, 180.0
        self._rings = [0.0, 120.0, 240.0]
        self._pulses: list[float] = [0.0, 60.0]
        self._loader_angle = 0.0
        self._blink = True
        self._blink_tick = 0
        self._particles: list[list[float]] = []
        self._face_px: QPixmap | None = None
        self._load_face(face_path)

        self._tmr = QTimer(self)
        self._tmr.timeout.connect(self._step)
        self._tmr.start(16)

    # state colour for the whole reactor
    def _col(self) -> str:
        if self.muted:
            return T.MAGENTA
        if self.speaking:
            return T.AMBER
        return _STATE_COLORS.get(self.state, T.CYAN)

    def _busy(self) -> bool:
        return self.state in ("THINKING", "PROCESSING") and not self.speaking

    def _load_face(self, path: str):
        try:
            from PIL import Image, ImageDraw
            import io
            img = Image.open(path).convert("RGBA")
            sz = min(img.size)
            img = img.resize((sz, sz), Image.LANCZOS)
            mk = Image.new("L", (sz, sz), 0)
            ImageDraw.Draw(mk).ellipse((2, 2, sz - 2, sz - 2), fill=255)
            img.putalpha(mk)
            buf = io.BytesIO()
            img.save(buf, format="PNG")
            px = QPixmap()
            px.loadFromData(buf.getvalue())
            self._face_px = px
        except Exception:
            self._face_px = None

    def _step(self):
        self._tick += 1
        now = time.time()
        if now - self._last_t > (0.10 if self.speaking else 0.45):
            if self.speaking:
                self._tgt_scale = random.uniform(1.05, 1.13)
                self._tgt_halo = random.uniform(130, 180)
            elif self.muted:
                self._tgt_scale = random.uniform(0.998, 1.002)
                self._tgt_halo = random.uniform(12, 24)
            else:
                self._tgt_scale = random.uniform(1.001, 1.007)
                self._tgt_halo = random.uniform(44, 62)
            self._last_t = now

        sp = 0.36 if self.speaking else 0.14
        self._scale += (self._tgt_scale - self._scale) * sp
        self._halo += (self._tgt_halo - self._halo) * sp

        speeds = [1.1, -0.75, 1.8] if self.speaking else [0.45, -0.28, 0.75]
        for i, spd in enumerate(speeds):
            self._rings[i] = (self._rings[i] + spd) % 360

        self._scan = (self._scan + (2.8 if self.speaking else 1.1)) % 360
        self._scan2 = (self._scan2 + (-1.8 if self.speaking else -0.65)) % 360
        self._loader_angle = (self._loader_angle + 5.5) % 360

        fw = min(self.width(), self.height())
        lim = fw * 0.74
        spd = 3.8 if self.speaking else 1.8
        self._pulses = [r + spd for r in self._pulses if r + spd < lim]
        if len(self._pulses) < 3 and random.random() < (0.065 if self.speaking else 0.022):
            self._pulses.append(0.0)

        if self.speaking and random.random() < 0.24:
            cx, cy = self.width() / 2, self.height() / 2
            ang = random.uniform(0, 2 * math.pi)
            r_s = fw * 0.27
            self._particles.append([
                cx + math.cos(ang) * r_s, cy + math.sin(ang) * r_s,
                math.cos(ang) * random.uniform(0.8, 2.2),
                math.sin(ang) * random.uniform(0.8, 2.2) - 0.35, 1.0])
        self._particles = [
            [pt[0] + pt[2], pt[1] + pt[3], pt[2] * 0.97, pt[3] * 0.97, pt[4] - 0.026]
            for pt in self._particles if pt[4] > 0]

        self._blink_tick += 1
        if self._blink_tick >= 34:
            self._blink = not self._blink
            self._blink_tick = 0
        self.update()

    def paintEvent(self, _):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)

        W, H = self.width(), self.height()
        cx, cy = W / 2, H / 2
        fw = min(W, H)
        col = self._col()

        # vignette background
        bg = QRadialGradient(cx, cy, max(W, H) * 0.75)
        bg.setColorAt(0.0, qcol("#071019"))
        bg.setColorAt(1.0, qcol(T.BG))
        p.fillRect(self.rect(), QBrush(bg))

        # perspective grid floor — console vibe
        p.setPen(QPen(qcol(T.BORDER, 70), 0.7))
        horizon = cy + fw * 0.30
        for i in range(1, 9):
            y = horizon + (i ** 1.7) * 9
            if y < H:
                p.drawLine(QPointF(0, y), QPointF(W, y))
        for i in range(-12, 13):
            p.drawLine(QPointF(cx + i * 26, horizon),
                       QPointF(cx + i * 110, H))

        r_face = fw * 0.30

        # halo
        for i in range(8):
            r = r_face * (1.9 - i * 0.085)
            a = max(0, min(255, int(self._halo * 0.07 * (1.0 - i / 8))))
            p.setPen(QPen(qcol(col, a), 1.2))
            p.setBrush(Qt.BrushStyle.NoBrush)
            p.drawEllipse(QRectF(cx - r, cy - r, r * 2, r * 2))

        # expanding pulses
        for pr in self._pulses:
            a = max(0, int(200 * (1.0 - pr / (fw * 0.74))))
            p.setPen(QPen(qcol(col, a), 1.2))
            p.drawEllipse(QRectF(cx - pr, cy - pr, pr * 2, pr * 2))

        # orbital arc rings
        for idx, (r_frac, w_r, arc_l, gap) in enumerate(
                [(0.485, 2.5, 110, 80), (0.405, 1.8, 72, 58), (0.33, 1.2, 50, 44)]):
            ring_r = fw * r_frac
            a_val = max(0, min(255, int(self._halo * (1.0 - idx * 0.18))))
            p.setPen(QPen(qcol(col, a_val), w_r))
            rect = QRectF(cx - ring_r, cy - ring_r, ring_r * 2, ring_r * 2)
            ang = self._rings[idx]
            while ang < self._rings[idx] + 360:
                p.drawArc(rect, int(ang * 16), int(arc_l * 16))
                ang += arc_l + gap

        # scanners
        sr = fw * 0.50
        sa = min(255, int(self._halo * 1.4))
        ex = 70 if self.speaking else 40
        srect = QRectF(cx - sr, cy - sr, sr * 2, sr * 2)
        p.setPen(QPen(qcol(T.WHITE, sa), 2.2))
        p.drawArc(srect, int(self._scan * 16), int(ex * 16))
        p.setPen(QPen(qcol(T.VIOLET, sa // 3), 1.2))
        p.drawArc(srect, int(self._scan2 * 16), int(ex * 16))

        # busy loader ring — the visual "loading state"
        if self._busy():
            lr = fw * 0.55
            lrect = QRectF(cx - lr, cy - lr, lr * 2, lr * 2)
            p.setPen(QPen(qcol(col, 60), 3))
            p.drawEllipse(lrect)
            p.setPen(QPen(qcol(col, 230), 3))
            p.drawArc(lrect, int(-self._loader_angle * 16), 80 * 16)
            p.setPen(QPen(qcol(T.WHITE, 160), 1.6))
            p.drawArc(lrect, int((-self._loader_angle - 130) * 16), 30 * 16)

        # tick marks
        t_out, t_in = fw * 0.497, fw * 0.472
        for deg in range(0, 360, 6):
            rad = math.radians(deg)
            is_m = deg % 30 == 0
            inn = t_in if is_m else t_in + (8 if deg % 15 == 0 else 12)
            p.setPen(QPen(qcol(T.WHITE if is_m else col, 180 if is_m else 70),
                          1.0 if is_m else 0.5))
            p.drawLine(
                QPointF(cx + t_out * math.cos(rad), cy - t_out * math.sin(rad)),
                QPointF(cx + inn * math.cos(rad), cy - inn * math.sin(rad)))

        # face / orb
        if self._face_px:
            fsz = int(fw * 0.60 * self._scale)
            scaled = self._face_px.scaled(
                fsz, fsz, Qt.AspectRatioMode.KeepAspectRatio,
                Qt.TransformationMode.SmoothTransformation)
            p.drawPixmap(int(cx - fsz / 2), int(cy - fsz / 2), scaled)
        else:
            orb_r = int(fw * 0.26 * self._scale)
            base = QColor(col)
            for i in range(8, 0, -1):
                frc = i / 8
                a = max(0, min(255, int(self._halo * frc)))
                oc = QColor(int(base.red() * frc * 0.4),
                            int(base.green() * frc * 0.4),
                            int(base.blue() * frc * 0.4), a)
                p.setBrush(QBrush(oc))
                p.setPen(Qt.PenStyle.NoPen)
                r2 = int(orb_r * frc)
                p.drawEllipse(QRectF(cx - r2, cy - r2, r2 * 2, r2 * 2))
            p.setPen(QPen(qcol(T.WHITE, min(255, int(self._halo * 2))), 1))
            p.setFont(F(13, True))
            p.drawText(QRectF(cx - 80, cy - 14, 160, 28),
                       Qt.AlignmentFlag.AlignCenter, "F.L.I.N.T")

        # particles
        for pt in self._particles:
            a = max(0, min(255, int(pt[4] * 255)))
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(QBrush(qcol(T.WHITE, a)))
            p.drawEllipse(QPointF(pt[0], pt[1]), 2.2, 2.2)

        # status badge
        sy = cy + fw * 0.415
        if self.muted:
            txt = "⊘  MUTED"
        elif self.speaking:
            txt = "◉  SPEAKING"
        elif self._busy():
            txt = f"{'◈' if self._blink else '◇'}  {self.state}"
        else:
            txt = f"{'●' if self._blink else '○'}  {self.state}"
        bcol = qcol(col)
        fm_w = len(txt) * 8 + 28
        pill = QRectF(cx - fm_w / 2, sy - 2, fm_w, 22)
        p.setBrush(QBrush(qcol(T.PANEL, 210)))
        p.setPen(QPen(bcol, 1.0))
        p.drawRoundedRect(pill, 11, 11)
        p.setPen(QPen(bcol, 1))
        p.setFont(F(9, True))
        p.drawText(pill, Qt.AlignmentFlag.AlignCenter, txt)

        # waveform
        wy = sy + 28
        N, bw = 42, 7
        wx0 = (W - N * bw) / 2
        for i in range(N):
            if self.muted:
                hgt, cl = 2, qcol(T.MAGENTA, 120)
            elif self.speaking:
                hgt = random.randint(2, 18)
                cl = qcol(T.WHITE if hgt > 10 else T.AMBER, 200)
            elif self._busy():
                hgt = int(3 + 6 * abs(math.sin(self._tick * 0.11 + i * 0.4)))
                cl = qcol(col, 150)
            else:
                hgt = int(2 + 2 * math.sin(self._tick * 0.08 + i * 0.55))
                cl = qcol(T.BORDER_HI, 160)
            p.setBrush(QBrush(cl))
            p.setPen(Qt.PenStyle.NoPen)
            p.drawRoundedRect(QRectF(wx0 + i * bw, wy + 18 - hgt, bw - 1, hgt), 1, 1)


# ── Metric bar ───────────────────────────────────────────────────────────────
class MetricBar(QWidget):
    def __init__(self, label: str, color: str = T.CYAN, parent=None):
        super().__init__(parent)
        self._label, self._color = label, color
        self._value, self._shown = 0.0, 0.0
        self._text = "--"
        self.setFixedHeight(40)
        self.setMinimumWidth(80)
        self._tmr = QTimer(self)
        self._tmr.timeout.connect(self._ease)
        self._tmr.start(33)

    def set_value(self, pct: float, text: str):
        self._value = max(0.0, min(100.0, pct))
        self._text = text

    def _ease(self):
        if abs(self._shown - self._value) > 0.2:
            self._shown += (self._value - self._shown) * 0.18
            self.update()

    def paintEvent(self, _):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        W, H = self.width(), self.height()

        path = QPainterPath()
        path.addRoundedRect(QRectF(0, 0, W, H), 6, 6)
        p.fillPath(path, QBrush(qcol(T.PANEL2)))
        p.setPen(QPen(qcol(T.BORDER), 0.8))
        p.drawPath(path)

        bar_h, bar_x = 3, 7
        bar_y = H - bar_h - 6
        bar_w = W - 14
        fill_w = int(bar_w * self._shown / 100)

        p.setBrush(QBrush(qcol(T.INSET)))
        p.setPen(Qt.PenStyle.NoPen)
        p.drawRoundedRect(QRectF(bar_x, bar_y, bar_w, bar_h), 1.5, 1.5)

        bar_col = (qcol(T.RED) if self._shown > 85
                   else qcol(T.AMBER) if self._shown > 65
                   else qcol(self._color))
        if fill_w > 0:
            p.setBrush(QBrush(bar_col))
            p.drawRoundedRect(QRectF(bar_x, bar_y, fill_w, bar_h), 1.5, 1.5)
            glow = QRadialGradient(bar_x + fill_w, bar_y + bar_h / 2, 6)
            glow.setColorAt(0, qcol(self._color, 220))
            glow.setColorAt(1, qcol(self._color, 0))
            p.setBrush(QBrush(glow))
            p.drawEllipse(QPointF(bar_x + fill_w, bar_y + bar_h / 2), 6, 6)

        p.setFont(F(7, True))
        p.setPen(QPen(qcol(T.TEXT_DIM), 1))
        p.drawText(QRectF(8, 7, 40, 13),
                   Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
                   self._label)
        p.setFont(F(9, True))
        p.setPen(QPen(bar_col if self._text != "--" else qcol(T.TEXT_DIM), 1))
        p.drawText(QRectF(0, 6, W - 7, 15),
                   Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter,
                   self._text)


# ── Activity log (typewriter) ────────────────────────────────────────────────
class LogWidget(QTextEdit):
    _sig = pyqtSignal(str)

    _COLORS = {
        "you": T.WHITE, "ai": T.CYAN, "err": T.RED,
        "file": T.EMERALD, "sys": T.AMBER,
    }

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setReadOnly(True)
        self.setFont(F(9))
        self.setFrameStyle(QFrame.Shape.NoFrame.value)
        self.setStyleSheet(f"""
            QTextEdit {{
                background: transparent; color: {T.TEXT};
                border: none; padding: 6px;
                selection-background-color: {T.CYAN_GHO};
            }}
            QScrollBar:vertical {{
                background: transparent; width: 6px; border: none;
            }}
            QScrollBar::handle:vertical {{
                background: {T.BORDER_HI}; border-radius: 3px; min-height: 16px;
            }}
            QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {{ height: 0; }}
        """)
        self._queue: list[str] = []
        self._typing = False
        self._text, self._pos, self._tag = "", 0, "sys"
        self._tmr = QTimer(self)
        self._tmr.timeout.connect(self._step)
        self._sig.connect(self._enqueue)

    def append_log(self, text: str):
        self._sig.emit(text)

    def _enqueue(self, text: str):
        self._queue.append(text)
        if not self._typing:
            self._next()

    def _next(self):
        if not self._queue:
            self._typing = False
            return
        self._typing = True
        self._text = self._queue.pop(0)
        self._pos = 0
        tl = self._text.lower()
        if tl.startswith("you:"):
            self._tag = "you"
        elif tl.startswith("flint:"):
            self._tag = "ai"
        elif tl.startswith("file:"):
            self._tag = "file"
        elif "err" in tl:
            self._tag = "err"
        else:
            self._tag = "sys"
        self._tmr.start(4)

    def _step(self):
        if self._pos < len(self._text):
            cur = self.textCursor()
            fmt = cur.charFormat()
            fmt.setForeground(QBrush(qcol(self._COLORS.get(self._tag, T.TEXT))))
            cur.movePosition(QTextCursor.MoveOperation.End)
            cur.insertText(self._text[self._pos], fmt)
            self.setTextCursor(cur)
            self.ensureCursorVisible()
            self._pos += 1
        else:
            self._tmr.stop()
            cur = self.textCursor()
            cur.movePosition(QTextCursor.MoveOperation.End)
            cur.insertText("\n")
            self.setTextCursor(cur)
            self.ensureCursorVisible()
            QTimer.singleShot(14, self._next)


# ── Live terminal (stdout/stderr feed) ───────────────────────────────────────
class TerminalWidget(QPlainTextEdit):
    """Real-time mirror of everything the process prints, colour-coded."""
    _sig = pyqtSignal(str, str)          # line, level

    _LEVEL_COLORS = {
        "info": T.TEXT_MED, "ok": T.EMERALD,
        "warn": T.AMBER, "error": T.RED,
    }

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setReadOnly(True)
        self.setFont(F(8))
        self.setMaximumBlockCount(1200)
        self.setFrameStyle(QFrame.Shape.NoFrame.value)
        self.setStyleSheet(f"""
            QPlainTextEdit {{
                background: {T.INSET}; color: {T.TEXT_MED};
                border: none; padding: 6px;
                selection-background-color: {T.CYAN_GHO};
            }}
            QScrollBar:vertical {{
                background: transparent; width: 6px; border: none;
            }}
            QScrollBar::handle:vertical {{
                background: {T.BORDER_HI}; border-radius: 3px; min-height: 16px;
            }}
            QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {{ height: 0; }}
        """)
        self._sig.connect(self._append)
        self._attach_hub()

    def _attach_hub(self):
        try:
            from core.log_interceptor import get_hub
            hub = get_hub()
            for _, line, level, _stream in hub.snapshot()[-200:]:
                self._append(line, level)
            hub.subscribe(self._on_line)
        except Exception:
            self._append("log interceptor unavailable", "warn")

    # called from arbitrary threads — only emit the signal here
    def _on_line(self, line: str, level: str, _stream: str):
        self._sig.emit(line, level)

    def _append(self, line: str, level: str):
        cur = self.textCursor()
        cur.movePosition(QTextCursor.MoveOperation.End)
        ts_fmt = QTextCharFormat()
        ts_fmt.setForeground(QBrush(qcol(T.TEXT_DIM)))
        cur.insertText(time.strftime("%H:%M:%S "), ts_fmt)
        ln_fmt = QTextCharFormat()
        ln_fmt.setForeground(QBrush(qcol(self._LEVEL_COLORS.get(level, T.TEXT_MED))))
        cur.insertText(line + "\n", ln_fmt)
        sb = self.verticalScrollBar()
        sb.setValue(sb.maximum())


# ── File handling ────────────────────────────────────────────────────────────
_FILE_ICONS = {
    "image": ("🖼", T.CYAN), "video": ("🎬", T.AMBER), "audio": ("🎵", T.VIOLET),
    "pdf": ("📄", T.RED), "word": ("📝", T.VIOLET), "excel": ("📊", T.EMERALD),
    "code": ("💻", T.AMBER), "archive": ("📦", "#fb923c"), "pptx": ("📊", T.MAGENTA),
    "text": ("📃", T.TEXT_MED), "data": ("🔧", T.CYAN), "unknown": ("📎", T.TEXT_DIM),
}
_EXT_TO_CAT = {
    **dict.fromkeys(["jpg", "jpeg", "png", "gif", "webp", "bmp", "tiff", "svg", "ico"], "image"),
    **dict.fromkeys(["mp4", "avi", "mov", "mkv", "wmv", "flv", "webm", "m4v"], "video"),
    **dict.fromkeys(["mp3", "wav", "ogg", "m4a", "aac", "flac", "wma", "opus"], "audio"),
    **dict.fromkeys(["pdf"], "pdf"),
    **dict.fromkeys(["doc", "docx"], "word"),
    **dict.fromkeys(["xls", "xlsx", "ods"], "excel"),
    **dict.fromkeys(["ppt", "pptx"], "pptx"),
    **dict.fromkeys(["py", "js", "ts", "jsx", "tsx", "html", "css", "java", "c",
                     "cpp", "cs", "go", "rs", "rb", "php", "swift", "kt", "sh",
                     "sql", "lua"], "code"),
    **dict.fromkeys(["zip", "rar", "tar", "gz", "7z", "bz2", "xz"], "archive"),
    **dict.fromkeys(["txt", "md", "rst", "log"], "text"),
    **dict.fromkeys(["csv", "tsv", "json", "xml"], "data"),
}


def _file_category(path: Path) -> str:
    return _EXT_TO_CAT.get(path.suffix.lower().lstrip("."), "unknown")


def _fmt_size(size: int) -> str:
    if size < 1024:
        return f"{size} B"
    if size < 1024 ** 2:
        return f"{size / 1024:.1f} KB"
    if size < 1024 ** 3:
        return f"{size / 1024 ** 2:.1f} MB"
    return f"{size / 1024 ** 3:.1f} GB"


class FileDropZone(QWidget):
    file_selected = pyqtSignal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setAcceptDrops(True)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setFixedHeight(86)
        self._current_file: str | None = None
        self._hovering = self._drag_over = False
        self._dash_offset = 0.0
        self._anim = QTimer(self)
        self._anim.timeout.connect(self._animate)
        self._anim.start(35)

    def _animate(self):
        self._dash_offset = (self._dash_offset + 0.7) % 20
        self.update()

    def dragEnterEvent(self, e: QDragEnterEvent):
        if e.mimeData().hasUrls():
            e.acceptProposedAction()
            self._drag_over = True
            self.update()

    def dragLeaveEvent(self, _):
        self._drag_over = False
        self.update()

    def dropEvent(self, e: QDropEvent):
        self._drag_over = False
        urls = e.mimeData().urls()
        if urls:
            path = urls[0].toLocalFile()
            if Path(path).is_file():
                self._set_file(path)
        self.update()

    def enterEvent(self, _):
        self._hovering = True
        self.update()

    def leaveEvent(self, _):
        self._hovering = False
        self.update()

    def mousePressEvent(self, e):
        if e.button() != Qt.MouseButton.LeftButton:
            return
        if self._current_file and e.pos().x() > self.width() - 32:
            self.clear_file()
        else:
            self._browse()

    def current_file(self) -> str | None:
        return self._current_file

    def clear_file(self):
        self._current_file = None
        self.update()

    def _browse(self):
        path, _ = QFileDialog.getOpenFileName(
            self, "Select a file for FLINT", str(Path.home()),
            "All Files (*.*);;Images (*.jpg *.jpeg *.png *.gif *.webp);;"
            "Documents (*.pdf *.docx *.txt *.md *.pptx);;"
            "Data (*.csv *.xlsx *.json *.xml);;"
            "Audio (*.mp3 *.wav *.m4a *.flac);;Video (*.mp4 *.mov *.mkv);;"
            "Archives (*.zip *.rar *.7z)")
        if path:
            self._set_file(path)

    def _set_file(self, path: str):
        self._current_file = path
        self.update()
        self.file_selected.emit(path)

    def paintEvent(self, _):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        W, H = self.width(), self.height()
        rect = QRectF(4, 4, W - 8, H - 8)

        bg = (qcol("#0a1a2e") if self._drag_over
              else qcol("#081120") if self._hovering else qcol(T.INSET))
        path = QPainterPath()
        path.addRoundedRect(rect, 8, 8)
        p.fillPath(path, QBrush(bg))

        border = (qcol(T.EMERALD, 200) if self._current_file
                  else qcol(T.WHITE, 230) if self._drag_over
                  else qcol(T.CYAN, 180) if self._hovering
                  else qcol(T.BORDER_HI, 150))
        pen = QPen(border, 1.2, Qt.PenStyle.DashLine)
        pen.setDashOffset(self._dash_offset)
        p.setPen(pen)
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.drawRoundedRect(rect, 8, 8)

        if self._current_file:
            self._paint_file(p, W, H)
        elif self._drag_over:
            p.setFont(F(16))
            p.setPen(QPen(qcol(T.WHITE), 1))
            p.drawText(QRectF(0, H / 2 - 22, W, 30),
                       Qt.AlignmentFlag.AlignCenter, "⬇")
            p.setFont(F(8, True))
            p.setPen(QPen(qcol(T.CYAN), 1))
            p.drawText(QRectF(0, H / 2 + 8, W, 15),
                       Qt.AlignmentFlag.AlignCenter, "Release to load")
        else:
            cx, cy = W / 2, H / 2
            col = qcol(T.CYAN if self._hovering else T.CYAN_DIM)
            p.setPen(QPen(col, 1.8))
            p.drawLine(QPointF(cx, cy - 14), QPointF(cx, cy + 2))
            p.drawLine(QPointF(cx - 7, cy - 6), QPointF(cx, cy - 14))
            p.drawLine(QPointF(cx + 7, cy - 6), QPointF(cx, cy - 14))
            p.drawLine(QPointF(cx - 12, cy + 4), QPointF(cx + 12, cy + 4))
            p.setFont(F(8))
            p.setPen(QPen(qcol(T.TEXT if self._hovering else T.TEXT_DIM), 1))
            p.drawText(QRectF(0, cy + 8, W, 15), Qt.AlignmentFlag.AlignCenter,
                       "Drop file  ·  Click to browse")

    def _paint_file(self, p: QPainter, W: int, H: int):
        path = Path(self._current_file)
        icon, icon_col = _FILE_ICONS.get(_file_category(path), _FILE_ICONS["unknown"])
        try:
            size_str = _fmt_size(path.stat().st_size)
        except OSError:
            size_str = "?"
        p.setFont(QFont("Segoe UI Emoji", 18) if _OS == "Windows" else QFont("Arial", 18))
        p.setPen(QPen(qcol(icon_col), 1))
        p.drawText(QRectF(10, 0, 50, H), Qt.AlignmentFlag.AlignCenter, icon)

        tx, tw = 64, W - 64 - 36
        p.setFont(F(8, True))
        p.setPen(QPen(qcol(T.WHITE), 1))
        name = path.name if len(path.name) <= 32 else path.name[:29] + "…"
        p.drawText(QRectF(tx, H * 0.22, tw, 15),
                   Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter, name)
        p.setFont(F(7))
        p.setPen(QPen(qcol(T.TEXT_MED), 1))
        p.drawText(QRectF(tx, H * 0.22 + 17, tw, 13),
                   Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
                   f"{path.suffix.upper().lstrip('.') or 'FILE'}  ·  {size_str}")
        p.setFont(F(10, True))
        p.setPen(QPen(qcol(T.RED, 180), 1))
        p.drawText(QRectF(W - 32, 0, 26, H), Qt.AlignmentFlag.AlignCenter, "✕")


# ── Boot overlay: animated loading sequence ──────────────────────────────────
class BootOverlay(QWidget):
    _BOOT_LINES = [
        "QUANTUM CORE ............ ONLINE",
        "NEURAL LINK ............. SYNCED",
        "CAPTURE ENGINE .......... ARMED",
        "ASYNC PIPELINE .......... 4 WORKERS",
        "REMOTE LISTENER ......... STANDBY",
        "AUDIO SUBSYSTEM ......... CALIBRATED",
    ]

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        self._progress = 0.0
        self._line_idx = 0
        self._done = False
        self._tmr = QTimer(self)
        self._tmr.timeout.connect(self._step)
        self._tmr.start(28)

    def _step(self):
        if self._progress < 100:
            self._progress = min(100.0, self._progress + random.uniform(0.6, 2.4))
            self._line_idx = int(self._progress / 100 * len(self._BOOT_LINES))
            self.update()

    def finish(self):
        """Fade out once the backend reports ready."""
        if self._done:
            return
        self._done = True
        self._progress = 100.0
        self._tmr.stop()
        eff = QGraphicsOpacityEffect(self)
        self.setGraphicsEffect(eff)
        anim = QPropertyAnimation(eff, b"opacity", self)
        anim.setDuration(650)
        anim.setStartValue(1.0)
        anim.setEndValue(0.0)
        anim.setEasingCurve(QEasingCurve.Type.OutCubic)
        anim.finished.connect(self.hide)
        anim.start()
        self._fade_anim = anim          # keep a reference alive

    def paintEvent(self, _):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        W, H = self.width(), self.height()
        p.fillRect(self.rect(), qcol(T.BG, 235))
        cx, cy = W / 2, H / 2

        p.setFont(F(22, True))
        p.setPen(QPen(qcol(T.CYAN), 1))
        p.drawText(QRectF(0, cy - 110, W, 40),
                   Qt.AlignmentFlag.AlignCenter, "F . L . I . N . T")
        p.setFont(F(8))
        p.setPen(QPen(qcol(T.TEXT_DIM), 1))
        p.drawText(QRectF(0, cy - 68, W, 16), Qt.AlignmentFlag.AlignCenter,
                   "QUANTUM CONSOLE  ·  BOOT SEQUENCE")

        # progress bar
        bw, bh = min(420, W * 0.5), 6
        bx, by = cx - bw / 2, cy - 20
        p.setPen(QPen(qcol(T.BORDER_HI), 1))
        p.setBrush(QBrush(qcol(T.INSET)))
        p.drawRoundedRect(QRectF(bx, by, bw, bh), 3, 3)
        fw = bw * self._progress / 100
        if fw > 2:
            grad = QLinearGradient(bx, 0, bx + bw, 0)
            grad.setColorAt(0, qcol(T.CYAN))
            grad.setColorAt(1, qcol(T.VIOLET))
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(QBrush(grad))
            p.drawRoundedRect(QRectF(bx, by, fw, bh), 3, 3)
            glow = QRadialGradient(bx + fw, by + bh / 2, 10)
            glow.setColorAt(0, qcol(T.CYAN, 200))
            glow.setColorAt(1, qcol(T.CYAN, 0))
            p.setBrush(QBrush(glow))
            p.drawEllipse(QPointF(bx + fw, by + bh / 2), 10, 10)

        p.setFont(F(9, True))
        p.setPen(QPen(qcol(T.CYAN), 1))
        p.drawText(QRectF(0, by + 14, W, 18), Qt.AlignmentFlag.AlignCenter,
                   f"{self._progress:3.0f}%")

        # boot lines
        p.setFont(F(8))
        for i, line in enumerate(self._BOOT_LINES[:self._line_idx]):
            p.setPen(QPen(qcol(T.EMERALD if i < self._line_idx - 1 else T.TEXT), 1))
            p.drawText(QRectF(cx - 160, by + 46 + i * 17, 360, 15),
                       Qt.AlignmentFlag.AlignLeft, line)


# ── Setup overlay (API keys) ─────────────────────────────────────────────────
class SetupOverlay(QWidget):
    done = pyqtSignal(str, str, str, str)

    def __init__(self, parent=None):
        super().__init__(parent)
        # Prefill from any existing config so an upgrade (e.g. a new required
        # field like user_name) doesn't force re-entering the API keys.
        existing = {}
        try:
            existing = json.loads(API_FILE.read_text(encoding="utf-8"))
        except Exception:
            pass
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.setStyleSheet(f"""
            SetupOverlay {{
                background: rgba(3, 5, 10, 250);
                border: 1px solid {T.BORDER_HI};
                border-radius: 12px;
            }}
        """)
        detected = {"darwin": "mac", "windows": "windows"}.get(_OS.lower(), "linux")
        self._sel_os = detected

        lay = QVBoxLayout(self)
        lay.setContentsMargins(32, 24, 32, 24)
        lay.setSpacing(9)

        def _lbl(txt, size=9, bold=False, color=T.CYAN,
                 align=Qt.AlignmentFlag.AlignCenter):
            w = QLabel(txt)
            w.setAlignment(align)
            w.setFont(F(size, bold))
            w.setStyleSheet(f"color: {color}; background: transparent; border: none;")
            return w

        lay.addWidget(_lbl("INITIALISATION REQUIRED", 13, True,
                           align=Qt.AlignmentFlag.AlignLeft))
        lay.addWidget(_lbl("Configure F.L.I.N.T. before first boot.", 8,
                           color=T.TEXT_DIM, align=Qt.AlignmentFlag.AlignLeft))

        def _field(placeholder, focus=T.CYAN, secret=True):
            e = QLineEdit()
            if secret:
                e.setEchoMode(QLineEdit.EchoMode.Password)
            e.setPlaceholderText(placeholder)
            e.setFont(F(10))
            e.setFixedHeight(34)
            e.setStyleSheet(f"""
                QLineEdit {{
                    background: {T.INSET}; color: {T.TEXT};
                    border: 1px solid {T.BORDER_HI}; border-radius: 6px;
                    padding: 5px 10px;
                }}
                QLineEdit:focus {{ border: 1px solid {focus}; }}
            """)
            return e

        lay.addWidget(_lbl("YOUR NAME", 7, color=T.TEXT_DIM,
                           align=Qt.AlignmentFlag.AlignLeft))
        self._name_input = _field("How should FLINT address you?", T.VIOLET,
                                  secret=False)
        self._name_input.setText(str(existing.get("user_name", "")).strip())
        lay.addWidget(self._name_input)

        lay.addWidget(_lbl("GEMINI API KEY", 7, color=T.TEXT_DIM,
                           align=Qt.AlignmentFlag.AlignLeft))
        self._key_input = _field("AIza…", T.CYAN)
        self._key_input.setText(str(existing.get("gemini_api_key", "")).strip())
        lay.addWidget(self._key_input)

        lay.addWidget(_lbl("OPENROUTER API KEY", 7, color=T.TEXT_DIM,
                           align=Qt.AlignmentFlag.AlignLeft))
        self._or_input = _field("sk-or-…", T.EMERALD)
        self._or_input.setText(str(existing.get("openrouter_api_key", "")).strip())
        lay.addWidget(self._or_input)

        lay.addSpacing(4)
        lay.addWidget(_lbl("OPERATING SYSTEM", 7, color=T.TEXT_DIM,
                           align=Qt.AlignmentFlag.AlignLeft))
        os_row = QHBoxLayout()
        os_row.setSpacing(7)
        self._os_btns: dict[str, QPushButton] = {}
        for key, label in [("windows", "⊞  Windows"), ("mac", "  macOS"),
                           ("linux", "🐧  Linux")]:
            btn = QPushButton(label)
            btn.setFont(F(9, True))
            btn.setFixedHeight(32)
            btn.setCursor(Qt.CursorShape.PointingHandCursor)
            btn.clicked.connect(lambda _, k=key: self._sel(k))
            os_row.addWidget(btn)
            self._os_btns[key] = btn
        lay.addLayout(os_row)
        self._sel(detected)

        lay.addSpacing(6)
        init_btn = QPushButton("▸  INITIALISE SYSTEMS")
        init_btn.setFont(F(10, True))
        init_btn.setFixedHeight(38)
        init_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        init_btn.setStyleSheet(f"""
            QPushButton {{
                background: {T.CYAN_GHO}; color: {T.CYAN};
                border: 1px solid {T.CYAN_DIM}; border-radius: 6px;
                letter-spacing: 1px;
            }}
            QPushButton:hover {{
                background: #0a3340; border: 1px solid {T.CYAN};
                color: {T.WHITE};
            }}
        """)
        init_btn.clicked.connect(self._submit)
        lay.addWidget(init_btn)

    def _sel(self, key: str):
        self._sel_os = key
        pal = {"windows": (T.CYAN, "#06222e"),
               "mac": (T.EMERALD, "#03241a"),
               "linux": (T.AMBER, "#241a03")}
        for k, btn in self._os_btns.items():
            if k == key:
                fg, bg = pal[k]
                btn.setStyleSheet(
                    f"QPushButton {{ background: {bg}; color: {fg};"
                    f" border: 1px solid {fg}; border-radius: 6px; }}")
            else:
                btn.setStyleSheet(f"""
                    QPushButton {{
                        background: {T.PANEL}; color: {T.TEXT_DIM};
                        border: 1px solid {T.BORDER}; border-radius: 6px;
                    }}
                    QPushButton:hover {{
                        color: {T.TEXT}; border: 1px solid {T.BORDER_HI};
                    }}
                """)

    def _submit(self):
        name = self._name_input.text().strip()
        key = self._key_input.text().strip()
        or_key = self._or_input.text().strip()
        for val, field in [(name, self._name_input), (key, self._key_input),
                           (or_key, self._or_input)]:
            if not val:
                field.setStyleSheet(field.styleSheet()
                                    + f" QLineEdit {{ border: 1px solid {T.RED}; }}")
                return
        self.done.emit(key, or_key, name, self._sel_os)


# ── small helpers ────────────────────────────────────────────────────────────
def _section_label(text: str) -> QLabel:
    lbl = QLabel(text)
    lbl.setFont(F(7, True))
    lbl.setStyleSheet(f"""
        color: {T.TEXT_DIM}; background: transparent;
        border: none; border-bottom: 1px solid {T.BORDER};
        padding-bottom: 4px; letter-spacing: 2px;
    """)
    return lbl


class _TabButton(QPushButton):
    def __init__(self, text: str):
        super().__init__(text)
        self.setCheckable(True)
        self.setFont(F(7, True))
        self.setFixedHeight(24)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setStyleSheet(f"""
            QPushButton {{
                background: transparent; color: {T.TEXT_DIM};
                border: 1px solid {T.BORDER}; border-radius: 5px;
                letter-spacing: 1px;
            }}
            QPushButton:checked {{
                background: {T.CYAN_GHO}; color: {T.CYAN};
                border: 1px solid {T.CYAN_DIM};
            }}
            QPushButton:hover {{ color: {T.TEXT}; }}
        """)


# ── Main window ──────────────────────────────────────────────────────────────
class MainWindow(QMainWindow):
    _log_sig   = pyqtSignal(str)
    _state_sig = pyqtSignal(str)
    _link_sig  = pyqtSignal(int)
    _jobs_sig  = pyqtSignal(int)

    def __init__(self, face_path: str):
        super().__init__()
        self.setWindowTitle("F.L.I.N.T — QUANTUM CONSOLE")
        self.setMinimumSize(_MIN_W, _MIN_H)
        self.resize(_DEFAULT_W, _DEFAULT_H)
        screen = QApplication.primaryScreen().availableGeometry()
        self.move((screen.width() - _DEFAULT_W) // 2,
                  (screen.height() - _DEFAULT_H) // 2)

        self.on_text_command = None
        self._muted = False
        self._state = "INITIALISING"

        central = QWidget()
        central.setStyleSheet(f"background: {T.BG};")
        self.setCentralWidget(central)

        root = QVBoxLayout(central)
        root.setContentsMargins(8, 8, 8, 8)
        root.setSpacing(8)
        root.addWidget(self._build_header())

        body = QHBoxLayout()
        body.setSpacing(8)
        self._left_panel = self._build_left_panel()
        body.addWidget(self._left_panel, stretch=0)
        self.hud = HudCanvas(face_path)
        body.addWidget(self.hud, stretch=5)
        self._right_panel = self._build_right_panel()
        body.addWidget(self._right_panel, stretch=0)
        root.addLayout(body, stretch=1)
        root.addWidget(self._build_footer())

        # timers
        self._clock_tmr = QTimer(self)
        self._clock_tmr.timeout.connect(self._tick_clock)
        self._clock_tmr.start(1000)
        self._tick_clock()
        self._metric_tmr = QTimer(self)
        self._metric_tmr.timeout.connect(self._update_metrics)
        self._metric_tmr.start(2000)
        self._update_metrics()

        # cross-thread signals
        self._log_sig.connect(self._log.append_log)
        self._state_sig.connect(self._apply_state)
        self._link_sig.connect(self._apply_link)
        self._jobs_sig.connect(self._apply_jobs)

        # overlays
        self._boot = BootOverlay(central)
        self._boot.setGeometry(0, 0, self.width(), self.height())
        self._boot.show()
        self._boot.raise_()

        self._overlay: SetupOverlay | None = None
        self._ready = self._check_config()
        if not self._ready:
            self._show_setup()

        QShortcut(QKeySequence("F4"), self).activated.connect(self._toggle_mute)
        QShortcut(QKeySequence("F11"), self).activated.connect(self._toggle_fullscreen)

    # ── layout scaling ────────────────────────────────────────────────────────
    def resizeEvent(self, event):
        super().resizeEvent(event)
        w = self.width()
        # side panels scale with the window, clamped to sane bounds
        self._left_panel.setFixedWidth(max(150, min(220, int(w * 0.14))))
        self._right_panel.setFixedWidth(max(330, min(460, int(w * 0.31))))
        cw = self.centralWidget()
        if self._boot and self._boot.isVisible():
            self._boot.setGeometry(0, 0, cw.width(), cw.height())
        if self._overlay and self._overlay.isVisible():
            ow, oh = 480, 420
            self._overlay.setGeometry((cw.width() - ow) // 2,
                                      (cw.height() - oh) // 2, ow, oh)

    def _toggle_fullscreen(self):
        self.showNormal() if self.isFullScreen() else self.showFullScreen()

    # ── header ────────────────────────────────────────────────────────────────
    def _build_header(self) -> QWidget:
        panel = NeonPanel(glow=T.CYAN, radius=10)
        panel.setFixedHeight(60)
        lay = QHBoxLayout(panel)
        lay.setContentsMargins(18, 6, 18, 6)

        def _badge(txt, color=T.TEXT_DIM, bold=False):
            l = QLabel(txt)
            l.setFont(F(7, bold))
            l.setStyleSheet(f"""
                color: {color}; background: {T.PANEL2};
                border: 1px solid {T.BORDER}; border-radius: 4px;
                padding: 2px 8px;
            """)
            return l

        lay.addWidget(_badge("QUANTUM", T.CYAN_DIM))
        lay.addSpacing(6)
        self._link_badge = _badge("LINK  OFFLINE", T.TEXT_DIM, bold=True)
        lay.addWidget(self._link_badge)
        lay.addSpacing(6)
        self._spinner = Spinner(T.CYAN, 18)
        self._spinner.hide()
        lay.addWidget(self._spinner)
        self._jobs_lbl = QLabel("")
        self._jobs_lbl.setFont(F(7, True))
        self._jobs_lbl.setStyleSheet(
            f"color: {T.VIOLET}; background: transparent; border: none;")
        lay.addWidget(self._jobs_lbl)
        lay.addStretch()

        mid = QVBoxLayout()
        mid.setSpacing(2)
        title = QLabel("F.L.I.N.T")
        title.setAlignment(Qt.AlignmentFlag.AlignCenter)
        title.setFont(F(18, True))
        title.setStyleSheet(
            f"color: {T.WHITE}; background: transparent; border: none;"
            f" letter-spacing: 8px;")
        mid.addWidget(title)
        sub = QLabel("For Less Intelligent Networked Things")
        sub.setAlignment(Qt.AlignmentFlag.AlignCenter)
        sub.setFont(F(7))
        sub.setStyleSheet(
            f"color: {T.TEXT_DIM}; background: transparent; border: none;"
            f" letter-spacing: 2px;")
        mid.addWidget(sub)
        lay.addLayout(mid)
        lay.addStretch()

        right = QVBoxLayout()
        right.setSpacing(2)
        self._clock_lbl = QLabel("00:00:00")
        self._clock_lbl.setFont(F(15, True))
        self._clock_lbl.setStyleSheet(
            f"color: {T.CYAN}; background: transparent; border: none;")
        self._clock_lbl.setAlignment(Qt.AlignmentFlag.AlignRight)
        right.addWidget(self._clock_lbl)
        self._date_lbl = QLabel("")
        self._date_lbl.setFont(F(7))
        self._date_lbl.setStyleSheet(
            f"color: {T.TEXT_DIM}; background: transparent; border: none;")
        self._date_lbl.setAlignment(Qt.AlignmentFlag.AlignRight)
        right.addWidget(self._date_lbl)
        lay.addLayout(right)
        return panel

    def _tick_clock(self):
        self._clock_lbl.setText(time.strftime("%H:%M:%S"))
        self._date_lbl.setText(time.strftime("%a  %d  %b  %Y"))

    # ── left rail ─────────────────────────────────────────────────────────────
    def _build_left_panel(self) -> QWidget:
        panel = NeonPanel(glow=T.VIOLET, radius=10, sweep=False)
        panel.setFixedWidth(170)
        lay = QVBoxLayout(panel)
        lay.setContentsMargins(10, 12, 10, 12)
        lay.setSpacing(5)

        lay.addWidget(_section_label("SYS  MONITOR"))
        lay.addSpacing(3)
        self._bar_cpu = MetricBar("CPU", T.CYAN)
        self._bar_mem = MetricBar("MEM", T.EMERALD)
        self._bar_net = MetricBar("NET", T.VIOLET)
        self._bar_gpu = MetricBar("GPU", T.AMBER)
        self._bar_tmp = MetricBar("TMP", T.MAGENTA)
        for bar in (self._bar_cpu, self._bar_mem, self._bar_net,
                    self._bar_gpu, self._bar_tmp):
            lay.addWidget(bar)

        lay.addSpacing(6)
        info = QWidget()
        info.setStyleSheet(f"""
            background: {T.PANEL2}; border: 1px solid {T.BORDER};
            border-radius: 6px;
        """)
        il = QVBoxLayout(info)
        il.setContentsMargins(8, 6, 8, 6)
        il.setSpacing(4)
        self._uptime_lbl = QLabel("UPTIME  --:--")
        self._uptime_lbl.setFont(F(7, True))
        self._uptime_lbl.setStyleSheet(
            f"color: {T.EMERALD}; background: transparent; border: none;")
        il.addWidget(self._uptime_lbl)
        self._proc_lbl = QLabel("PROCS  --")
        self._proc_lbl.setFont(F(7))
        self._proc_lbl.setStyleSheet(
            f"color: {T.TEXT_MED}; background: transparent; border: none;")
        il.addWidget(self._proc_lbl)
        self._cache_lbl = QLabel("VISION CACHE  --")
        self._cache_lbl.setFont(F(7))
        self._cache_lbl.setStyleSheet(
            f"color: {T.AMBER}; background: transparent; border: none;")
        il.addWidget(self._cache_lbl)
        lay.addWidget(info)
        lay.addStretch()

        for top, bot, col in [("AI CORE", "ACTIVE", T.EMERALD),
                              ("PIPELINE", "ARMED", T.CYAN),
                              ("PROTOCOL", "ODIN", T.TEXT_DIM)]:
            badge = QWidget()
            badge.setStyleSheet(f"""
                background: {T.PANEL2}; border: 1px solid {T.BORDER};
                border-radius: 5px;
            """)
            bl = QVBoxLayout(badge)
            bl.setContentsMargins(6, 4, 6, 4)
            bl.setSpacing(1)
            tl = QLabel(top)
            tl.setFont(F(6))
            tl.setAlignment(Qt.AlignmentFlag.AlignCenter)
            tl.setStyleSheet(
                f"color: {T.TEXT_DIM}; background: transparent; border: none;")
            vl = QLabel(bot)
            vl.setFont(F(8, True))
            vl.setAlignment(Qt.AlignmentFlag.AlignCenter)
            vl.setStyleSheet(
                f"color: {col}; background: transparent; border: none;")
            bl.addWidget(tl)
            bl.addWidget(vl)
            lay.addWidget(badge)
        return panel

    # ── right console ─────────────────────────────────────────────────────────
    def _build_right_panel(self) -> QWidget:
        panel = NeonPanel(glow=T.CYAN, radius=10, sweep=False)
        panel.setFixedWidth(370)
        lay = QVBoxLayout(panel)
        lay.setContentsMargins(10, 10, 10, 10)
        lay.setSpacing(8)

        # tab switch: ACTIVITY | TERMINAL
        tabs = QHBoxLayout()
        tabs.setSpacing(6)
        self._tab_act = _TabButton("ACTIVITY  LOG")
        self._tab_term = _TabButton("SYSTEM  TERMINAL")
        self._tab_act.setChecked(True)
        tabs.addWidget(self._tab_act)
        tabs.addWidget(self._tab_term)
        lay.addLayout(tabs)

        self._stack = QStackedLayout()
        self._log = LogWidget()
        self._terminal = TerminalWidget()
        page1, page2 = QWidget(), QWidget()
        for page, widget in ((page1, self._log), (page2, self._terminal)):
            pl = QVBoxLayout(page)
            pl.setContentsMargins(0, 0, 0, 0)
            page.setStyleSheet(f"""
                background: {T.INSET}; border: 1px solid {T.BORDER};
                border-radius: 6px;
            """)
            pl.addWidget(widget)
        self._stack.addWidget(page1)
        self._stack.addWidget(page2)
        stack_host = QWidget()
        stack_host.setLayout(self._stack)
        stack_host.setStyleSheet("background: transparent;")
        lay.addWidget(stack_host, stretch=1)

        self._tab_act.clicked.connect(lambda: self._switch_tab(0))
        self._tab_term.clicked.connect(lambda: self._switch_tab(1))

        lay.addWidget(_section_label("FILE  UPLOAD"))
        self._drop_zone = FileDropZone()
        self._drop_zone.file_selected.connect(self._on_file_selected)
        lay.addWidget(self._drop_zone)
        self._file_hint = QLabel("No file loaded")
        self._file_hint.setFont(F(7))
        self._file_hint.setStyleSheet(
            f"color: {T.TEXT_DIM}; background: transparent; border: none;")
        self._file_hint.setWordWrap(True)
        lay.addWidget(self._file_hint)

        lay.addWidget(_section_label("COMMAND  INPUT"))
        lay.addLayout(self._build_input_row())

        self._mute_btn = QPushButton()
        self._mute_btn.setFixedHeight(32)
        self._mute_btn.setFont(F(8, True))
        self._mute_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._mute_btn.clicked.connect(self._toggle_mute)
        self._style_mute_btn()
        lay.addWidget(self._mute_btn)
        return panel

    def _switch_tab(self, idx: int):
        self._stack.setCurrentIndex(idx)
        self._tab_act.setChecked(idx == 0)
        self._tab_term.setChecked(idx == 1)

    def _build_input_row(self) -> QHBoxLayout:
        row = QHBoxLayout()
        row.setSpacing(6)
        self._input = QLineEdit()
        self._input.setPlaceholderText("Type a command or question…")
        self._input.setFont(F(9))
        self._input.setFixedHeight(32)
        self._input.setStyleSheet(f"""
            QLineEdit {{
                background: {T.INSET}; color: {T.WHITE};
                border: 1px solid {T.BORDER_HI}; border-radius: 6px;
                padding: 3px 8px;
            }}
            QLineEdit:focus {{
                border: 1px solid {T.CYAN}; background: {T.CYAN_GHO};
            }}
        """)
        self._input.returnPressed.connect(self._send)
        row.addWidget(self._input)

        send = QPushButton("▸")
        send.setFixedSize(32, 32)
        send.setFont(F(12, True))
        send.setCursor(Qt.CursorShape.PointingHandCursor)
        send.setStyleSheet(f"""
            QPushButton {{
                background: {T.CYAN_GHO}; color: {T.CYAN};
                border: 1px solid {T.CYAN_DIM}; border-radius: 6px;
            }}
            QPushButton:hover {{
                background: #0a3340; border: 1px solid {T.CYAN};
                color: {T.WHITE};
            }}
        """)
        send.clicked.connect(self._send)
        row.addWidget(send)
        return row

    # ── footer ────────────────────────────────────────────────────────────────
    def _build_footer(self) -> QWidget:
        w = QWidget()
        w.setFixedHeight(22)
        w.setStyleSheet("background: transparent;")
        lay = QHBoxLayout(w)
        lay.setContentsMargins(10, 0, 10, 0)

        def _fl(txt, color=T.TEXT_DIM):
            l = QLabel(txt)
            l.setFont(F(7))
            l.setStyleSheet(f"color: {color}; background: transparent; border: none;")
            return l

        lay.addWidget(_fl("[F4] Mute  ·  [F11] Fullscreen"))
        lay.addStretch()
        self._footer_link = _fl("REMOTE LINK  ·  ws://0.0.0.0:8765", T.BORDER_HI)
        lay.addWidget(self._footer_link)
        lay.addStretch()
        lay.addWidget(_fl("QUANTUM CONSOLE  ·  v2.0", T.CYAN_DIM))
        return w

    # ── periodic updates ─────────────────────────────────────────────────────
    def _update_metrics(self):
        snap = _metrics.snapshot()
        self._bar_cpu.set_value(snap["cpu"], f"{snap['cpu']:.0f}%")
        self._bar_mem.set_value(snap["mem"], f"{snap['mem']:.0f}%")
        net = snap["net"]
        self._bar_net.set_value(min(100, net * 10),
                                f"{net*1024:.0f}KB/s" if net < 1.0 else f"{net:.1f}MB/s")
        gpu = snap["gpu"]
        self._bar_gpu.set_value(max(gpu, 0), f"{gpu:.0f}%" if gpu >= 0 else "N/A")
        tmp = snap["tmp"]
        self._bar_tmp.set_value(min(100, tmp) if tmp >= 0 else 0,
                                f"{tmp:.0f}°C" if tmp >= 0 else "N/A")
        try:
            elapsed = time.time() - psutil.boot_time()
            h, m = int(elapsed // 3600), int((elapsed % 3600) // 60)
            self._uptime_lbl.setText(f"UPTIME  {h:02d}:{m:02d}")
            self._proc_lbl.setText(f"PROCS  {len(psutil.pids())}")
        except Exception:
            pass
        try:
            from core.capture_engine import get_engine
            s = get_engine().snapshot_stats()
            if s["captures"]:
                self._cache_lbl.setText(f"VISION CACHE  {s['hit_rate']:.0f}%")
        except Exception:
            pass

    # ── slots ─────────────────────────────────────────────────────────────────
    def _on_file_selected(self, path: str):
        p = Path(path)
        icon, _ = _FILE_ICONS.get(_file_category(p), _FILE_ICONS["unknown"])
        size = _fmt_size(p.stat().st_size)
        self._file_hint.setText(
            f"{icon}  {p.name}  ·  {size}  ·  Tell FLINT what to do with it")
        self._log.append_log(f"FILE: {p.name} ({size}) loaded")
        if self.on_text_command:
            msg = (f"[FILE_UPLOADED] path={path} | name={p.name} | "
                   f"type={p.suffix.lstrip('.')} | size={size} | "
                   f"Briefly tell the user you can see the file '{p.name}' "
                   f"({size}) has been uploaded and ask what they'd like "
                   f"to do with it.")
            threading.Thread(target=self.on_text_command, args=(msg,),
                             daemon=True).start()

    def _toggle_mute(self):
        self._muted = not self._muted
        self.hud.muted = self._muted
        self._style_mute_btn()
        self._apply_state("MUTED" if self._muted else "LISTENING")
        self._log.append_log("SYS: Microphone muted." if self._muted
                             else "SYS: Microphone active.")

    def _style_mute_btn(self):
        if self._muted:
            self._mute_btn.setText("⊘   MICROPHONE  MUTED")
            self._mute_btn.setStyleSheet(f"""
                QPushButton {{
                    background: #260a18; color: {T.MAGENTA};
                    border: 1px solid {T.MAGENTA}; border-radius: 6px;
                }}
                QPushButton:hover {{ background: #330d20; }}
            """)
        else:
            self._mute_btn.setText("◉   MICROPHONE  ACTIVE")
            self._mute_btn.setStyleSheet(f"""
                QPushButton {{
                    background: #03241a; color: {T.EMERALD};
                    border: 1px solid {T.EMERALD_D}; border-radius: 6px;
                }}
                QPushButton:hover {{
                    background: #053424; border: 1px solid {T.EMERALD};
                }}
            """)

    def _send(self):
        txt = self._input.text().strip()
        if not txt:
            return
        self._input.clear()
        self._log.append_log(f"You: {txt}")
        if self.on_text_command:
            threading.Thread(target=self.on_text_command, args=(txt,),
                             daemon=True).start()

    def _apply_state(self, state: str):
        self._state = state
        self.hud.state = state
        self.hud.speaking = (state == "SPEAKING")
        self.hud.muted = self._muted
        col = _STATE_COLORS.get(state, T.CYAN)
        # busy states show the header spinner + brighter glow
        if state in ("THINKING", "PROCESSING"):
            self._spinner.set_color(col)
            self._spinner.start()
        else:
            self._spinner.stop()
        # first real state ends the boot sequence
        if state in ("LISTENING", "SPEAKING") and self._boot.isVisible():
            self._boot.finish()

    def _apply_link(self, clients: int):
        if clients > 0:
            self._link_badge.setText(f"LINK  {clients} CLIENT{'S' if clients != 1 else ''}")
            self._link_badge.setStyleSheet(f"""
                color: {T.EMERALD}; background: #03241a;
                border: 1px solid {T.EMERALD_D}; border-radius: 4px;
                padding: 2px 8px;
            """)
        else:
            self._link_badge.setText("LINK  OFFLINE")
            self._link_badge.setStyleSheet(f"""
                color: {T.TEXT_DIM}; background: {T.PANEL2};
                border: 1px solid {T.BORDER}; border-radius: 4px;
                padding: 2px 8px;
            """)

    def _apply_jobs(self, active: int):
        self._jobs_lbl.setText(f"JOBS {active}" if active > 0 else "")

    # ── config gate ───────────────────────────────────────────────────────────
    def _check_config(self) -> bool:
        if not API_FILE.exists():
            return False
        try:
            d = json.loads(API_FILE.read_text(encoding="utf-8"))
            return (bool(d.get("gemini_api_key"))
                    and bool(d.get("openrouter_api_key"))
                    and bool(d.get("os_system"))
                    and bool(str(d.get("user_name", "")).strip()))
        except Exception:
            return False

    def _show_setup(self):
        ov = SetupOverlay(self.centralWidget())
        cw = self.centralWidget()
        ow, oh = 480, 420
        ov.setGeometry((cw.width() - ow) // 2, (cw.height() - oh) // 2, ow, oh)
        ov.done.connect(self._on_setup_done)
        ov.show()
        ov.raise_()
        self._overlay = ov

    def _on_setup_done(self, key: str, or_key: str, name: str, os_name: str):
        os.makedirs(CONFIG_DIR, exist_ok=True)
        existing = {}
        try:
            existing = json.loads(API_FILE.read_text(encoding="utf-8"))
        except Exception:
            pass
        existing.update({
            "gemini_api_key": key,
            "openrouter_api_key": or_key,
            "user_name": name,
            "os_system": os_name,
        })
        API_FILE.write_text(json.dumps(existing, indent=4), encoding="utf-8")
        self._ready = True
        if self._overlay:
            self._overlay.hide()
            self._overlay = None
        self._apply_state("LISTENING")
        self._log.append_log(f"SYS: Initialised. OS={os_name.upper()}. "
                             f"FLINT online for {name}.")


# ── tkinter-style mainloop shim (main.py compatibility) ──────────────────────
class _RootShim:
    def __init__(self, app: QApplication):
        self._app = app

    def mainloop(self):
        self._app.exec()

    def protocol(self, *_):
        pass


# ── Public API ────────────────────────────────────────────────────────────────
class FlintUI:
    def __init__(self, face_path: str, size=None):
        self._app = QApplication.instance() or QApplication(sys.argv)
        self._app.setStyle("Fusion")
        self._win = MainWindow(face_path)
        self._win.show()
        self.root = _RootShim(self._app)

    @property
    def muted(self) -> bool:
        return self._win._muted

    @muted.setter
    def muted(self, v: bool):
        if v != self._win._muted:
            self._win._toggle_mute()

    @property
    def current_file(self) -> str | None:
        return self._win._drop_zone.current_file()

    @property
    def on_text_command(self):
        return self._win.on_text_command

    @on_text_command.setter
    def on_text_command(self, cb):
        self._win.on_text_command = cb

    def set_state(self, state: str):
        self._win._state_sig.emit(state)

    def write_log(self, text: str):
        self._win._log_sig.emit(text)

    def set_link_clients(self, n: int):
        self._win._link_sig.emit(n)

    def attach_pipeline(self, pipeline):
        """Mirror pipeline activity in the header (thread-safe)."""
        def _refresh(_payload):
            try:
                self._win._jobs_sig.emit(len(pipeline.active_jobs())
                                         + pipeline.queued_count())
            except Exception:
                pass
        for ev in ("job_submitted", "job_started", "job_finished", "job_failed"):
            pipeline.bus.on(ev, _refresh)

    def wait_for_api_key(self):
        while not self._win._ready:
            time.sleep(0.1)

    def start_speaking(self):
        self.set_state("SPEAKING")

    def stop_speaking(self):
        if not self.muted:
            self.set_state("LISTENING")
