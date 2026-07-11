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
    QEasingCurve, QMimeData, QObject, QPointF, QRectF, QSize, Qt,
    QTimer, QUrl, pyqtSignal,
)
from PyQt6.QtGui import (
    QBrush, QColor, QDragEnterEvent, QDropEvent, QFont, QFontDatabase,
    QKeySequence, QLinearGradient, QPainter, QPainterPath, QPen, QPixmap,
    QRadialGradient, QShortcut,
)
from PyQt6.QtWidgets import (
    QApplication, QFileDialog, QFrame, QHBoxLayout, QLabel, QLineEdit,
    QMainWindow, QPushButton, QScrollArea, QSizePolicy, QTextEdit,
    QVBoxLayout, QWidget, QProgressBar,
)


def _base_dir() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).parent
    return Path(__file__).resolve().parent


BASE_DIR   = _base_dir()
CONFIG_DIR = BASE_DIR / "config"
API_FILE   = CONFIG_DIR / "api_keys.json"

_DEFAULT_W, _DEFAULT_H = 1060, 720
_MIN_W,     _MIN_H     = 860, 600
_LEFT_W  = 160
_RIGHT_W = 360

_OS = platform.system()


# ── Colour palette: Neural Obsidian ─────────────────────────────────────────
class C:
    BG        = "#050507"
    PANEL     = "#08080d"
    PANEL2    = "#0b0b12"
    BORDER    = "#1a1a2e"
    BORDER_B  = "#2d2d55"
    BORDER_A  = "#22223f"
    PRI       = "#a78bfa"        # violet
    PRI_DIM   = "#4c3d8f"
    PRI_GHO   = "#0d0a1f"
    PRI_BRIGHT= "#c4b5fd"
    ACC       = "#f59e0b"        # amber
    ACC2      = "#34d399"        # emerald
    GREEN     = "#10b981"
    GREEN_D   = "#065f46"
    RED       = "#f43f5e"
    MUTED_C   = "#f43f5e"
    TEXT      = "#c4b5fd"
    TEXT_DIM  = "#4b4580"
    TEXT_MED  = "#7c6fcd"
    WHITE     = "#f0eeff"
    DARK      = "#030305"
    BAR_BG    = "#0f0f1e"
    INDIGO    = "#6366f1"
    ROSE      = "#fb7185"


def qcol(h: str, a: int = 255) -> QColor:
    c = QColor(h)
    c.setAlpha(a)
    return c


# In a windowed (no-console) build, every subprocess spawn flashes a console
# window — the GPU probe alone did it every 1.5s. Spawn all probes hidden.
_NO_WINDOW = subprocess.CREATE_NO_WINDOW if _OS == "Windows" else 0


def _probe(args: list[str], timeout: float):
    return subprocess.run(args, capture_output=True, text=True,
                          timeout=timeout, creationflags=_NO_WINDOW)


# ── System metrics ───────────────────────────────────────────────────────────
class _SysMetrics:
    def __init__(self):
        self.cpu  = 0.0
        self.mem  = 0.0
        self.net  = 0.0
        self.gpu  = -1.0
        self.tmp  = -1.0
        self._lock = threading.Lock()
        self._last_net = psutil.net_io_counters()
        self._last_net_t = time.time()
        # Give up on probes that keep failing: each GPU/temp attempt spawns
        # a subprocess (powershell on Windows!) — retrying a missing tool
        # every 1.5s forever burns CPU for a permanent "N/A".
        self._gpu_fails = 0
        self._tmp_fails = 0
        self._running = True
        threading.Thread(target=self._loop, daemon=True).start()

    def _loop(self):
        while self._running:
            try:
                self._update()
            except Exception:
                pass
            time.sleep(1.5)

    def _update(self):
        cpu = psutil.cpu_percent(interval=None)
        mem = psutil.virtual_memory().percent
        nc  = psutil.net_io_counters()
        now = time.time()
        dt  = now - self._last_net_t
        if dt > 0:
            sent = (nc.bytes_sent - self._last_net.bytes_sent) / dt
            recv = (nc.bytes_recv - self._last_net.bytes_recv) / dt
            net  = (sent + recv) / (1024 * 1024)
        else:
            net = 0.0
        self._last_net   = nc
        self._last_net_t = now
        if self._gpu_fails < 3:
            gpu = self._get_gpu()
            self._gpu_fails = 0 if gpu >= 0 else self._gpu_fails + 1
        else:
            gpu = -1.0
        if self._tmp_fails < 3:
            tmp = self._get_temp()
            self._tmp_fails = 0 if tmp >= 0 else self._tmp_fails + 1
        else:
            tmp = -1.0
        with self._lock:
            self.cpu = cpu
            self.mem = mem
            self.net = net
            self.gpu = gpu
            self.tmp = tmp

    def _get_gpu(self) -> float:
        try:
            r = _probe(
                ["nvidia-smi", "--query-gpu=utilization.gpu",
                 "--format=csv,noheader,nounits"], timeout=2)
            if r.returncode == 0:
                vals = [float(v.strip()) for v in r.stdout.strip().split("\n") if v.strip()]
                if vals:
                    return sum(vals) / len(vals)
        except Exception:
            pass
        if _OS == "Linux":
            try:
                r = _probe(["rocm-smi", "--showuse", "--csv"], timeout=2)
                if r.returncode == 0:
                    for line in r.stdout.strip().split("\n"):
                        parts = line.split(",")
                        if len(parts) >= 2:
                            try:
                                return float(parts[1].strip().replace("%", ""))
                            except ValueError:
                                pass
            except Exception:
                pass
            try:
                r = _probe(["intel_gpu_top", "-J", "-s", "500"], timeout=1)
                if r.returncode == 0 and "Render/3D" in r.stdout:
                    import re
                    m = re.search(r'"busy":\s*([\d.]+)', r.stdout)
                    if m:
                        return float(m.group(1))
            except Exception:
                pass
        if _OS == "Darwin":
            try:
                r = _probe(
                    ["sudo", "-n", "powermetrics", "-n", "1", "-i", "500",
                     "--samplers", "gpu_power"], timeout=2)
                if r.returncode == 0 and "GPU" in r.stdout:
                    import re
                    m = re.search(r'GPU\s+Active:\s+([\d.]+)%', r.stdout)
                    if m:
                        return float(m.group(1))
            except Exception:
                pass
        return -1.0

    def _get_temp(self) -> float:
        try:
            temps = psutil.sensors_temperatures()
            candidates = ["coretemp", "k10temp", "cpu_thermal", "acpitz",
                          "cpu-thermal", "zenpower", "it8688"]
            for name in candidates:
                if name in temps:
                    entries = temps[name]
                    if entries:
                        return entries[0].current
            for entries in temps.values():
                if entries:
                    return entries[0].current
        except Exception:
            pass
        if _OS == "Darwin":
            try:
                r = _probe(["osx-cpu-temp"], timeout=2)
                if r.returncode == 0:
                    import re
                    m = re.search(r"([\d.]+)", r.stdout)
                    if m:
                        return float(m.group(1))
            except Exception:
                pass
        if _OS == "Windows":
            try:
                r = _probe(
                    ["powershell", "-NoProfile", "-Command",
                     "(Get-WmiObject MSAcpi_ThermalZoneTemperature -Namespace root/wmi).CurrentTemperature"],
                    timeout=3)
                if r.returncode == 0 and r.stdout.strip():
                    raw = float(r.stdout.strip().split("\n")[0])
                    return (raw / 10.0) - 273.15
            except Exception:
                pass
        return -1.0

    def snapshot(self) -> dict:
        with self._lock:
            return {"cpu": self.cpu, "mem": self.mem,
                    "net": self.net, "gpu": self.gpu, "tmp": self.tmp}


_metrics = _SysMetrics()


# ── HUD Canvas ───────────────────────────────────────────────────────────────
class HudCanvas(QWidget):
    def __init__(self, face_path: str, parent=None):
        super().__init__(parent)
        self.setAttribute(Qt.WidgetAttribute.WA_OpaquePaintEvent)
        self.setMinimumSize(300, 300)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)

        self.muted    = False
        self.speaking = False
        self.state    = "INITIALISING"

        self._tick       = 0
        self._scale      = 1.0
        self._tgt_scale  = 1.0
        self._halo       = 55.0
        self._tgt_halo   = 55.0
        self._last_t     = time.time()
        self._scan       = 0.0
        self._scan2      = 180.0
        self._rings      = [0.0, 120.0, 240.0]
        self._pulses: list[float] = [0.0, 50.0, 100.0]
        self._blink      = True
        self._blink_tick = 0
        self._particles: list[list[float]] = []
        self._face_px: QPixmap | None = None
        self._load_face(face_path)
        self._hex_angle  = 0.0
        self._data_lines = [random.uniform(0, 1) for _ in range(12)]
        self._data_tick  = 0
        self._grid_px: QPixmap | None = None  # cached hex-grid background

        self._tmr = QTimer(self)
        self._tmr.timeout.connect(self._step)
        self._tmr.start(16)

    def _load_face(self, path: str):
        try:
            from PIL import Image, ImageDraw
            import io
            img = Image.open(path).convert("RGBA")
            sz  = min(img.size)
            img = img.resize((sz, sz), Image.LANCZOS)
            mk  = Image.new("L", (sz, sz), 0)
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
                self._tgt_halo  = random.uniform(130, 180)
            elif self.muted:
                self._tgt_scale = random.uniform(0.998, 1.002)
                self._tgt_halo  = random.uniform(12, 24)
            else:
                self._tgt_scale = random.uniform(1.001, 1.007)
                self._tgt_halo  = random.uniform(44, 62)
            self._last_t = now

        sp = 0.36 if self.speaking else 0.14
        self._scale += (self._tgt_scale - self._scale) * sp
        self._halo  += (self._tgt_halo  - self._halo)  * sp

        speeds = [1.1, -0.75, 1.8] if self.speaking else [0.45, -0.28, 0.75]
        for i, spd in enumerate(speeds):
            self._rings[i] = (self._rings[i] + spd) % 360

        self._scan  = (self._scan  + (2.8 if self.speaking else 1.1)) % 360
        self._scan2 = (self._scan2 + (-1.8 if self.speaking else -0.65)) % 360
        self._hex_angle = (self._hex_angle + (0.4 if self.speaking else 0.15)) % 360

        fw  = min(self.width(), self.height())
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
                math.sin(ang) * random.uniform(0.8, 2.2) - 0.35, 1.0,
            ])
        self._particles = [
            [p[0]+p[2], p[1]+p[3], p[2]*0.97, p[3]*0.97, p[4]-0.026]
            for p in self._particles if p[4] > 0
        ]

        self._data_tick += 1
        if self._data_tick % 8 == 0:
            idx = random.randint(0, len(self._data_lines) - 1)
            self._data_lines[idx] = random.uniform(0.1, 1.0)

        self._blink_tick += 1
        if self._blink_tick >= 34:
            self._blink = not self._blink
            self._blink_tick = 0
        self.update()

    def _draw_hexagon(self, p: QPainter, cx: float, cy: float, r: float,
                      angle_offset: float, color: QColor, width: float = 1.0):
        pts = []
        for i in range(6):
            ang = math.radians(angle_offset + i * 60)
            pts.append(QPointF(cx + r * math.cos(ang), cy + r * math.sin(ang)))
        path = QPainterPath()
        path.moveTo(pts[0])
        for pt in pts[1:]:
            path.lineTo(pt)
        path.closeSubpath()
        p.setPen(QPen(color, width))
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.drawPath(path)

    def paintEvent(self, _):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.fillRect(self.rect(), qcol(C.BG))

        W, H = self.width(), self.height()
        cx, cy = W / 2, H / 2
        fw = min(W, H)

        # subtle hex grid background — static, so render it ONCE per size:
        # painting ~800 QPainterPaths every 16ms frame pegged a CPU core.
        if (self._grid_px is None or self._grid_px.width() != W
                or self._grid_px.height() != H):
            self._grid_px = QPixmap(W, H)
            self._grid_px.fill(qcol(C.BG))
            gp = QPainter(self._grid_px)
            gp.setRenderHint(QPainter.RenderHint.Antialiasing)
            hex_size = 32
            for row in range(-2, H // hex_size + 4):
                for col in range(-2, W // (hex_size) + 4):
                    hx = col * hex_size * 1.732
                    hy = row * hex_size * 2
                    if col % 2 == 1:
                        hy += hex_size
                    self._draw_hexagon(gp, hx, hy, hex_size * 0.92,
                                       0, qcol(C.BORDER, 28), 0.4)
            gp.end()
        p.drawPixmap(0, 0, self._grid_px)

        r_face = fw * 0.30

        # outer glow rings (violet)
        for i in range(8):
            r   = r_face * (1.9 - i * 0.085)
            frc = 1.0 - i / 8
            a   = max(0, min(255, int(self._halo * 0.07 * frc)))
            col = qcol(C.MUTED_C if self.muted else C.PRI, a)
            p.setPen(QPen(col, 1.2))
            p.setBrush(Qt.BrushStyle.NoBrush)
            p.drawEllipse(QRectF(cx - r, cy - r, r * 2, r * 2))

        # pulse rings
        for pr in self._pulses:
            a   = max(0, int(200 * (1.0 - pr / (fw * 0.74))))
            col = qcol(C.MUTED_C if self.muted else C.PRI, a)
            p.setPen(QPen(col, 1.2))
            p.setBrush(Qt.BrushStyle.NoBrush)
            p.drawEllipse(QRectF(cx - pr, cy - pr, pr * 2, pr * 2))

        # rotating hexagon frames
        for idx, (r_frac, alpha_mul) in enumerate(
            [(0.52, 1.0), (0.44, 0.7), (0.36, 0.5)]
        ):
            hex_r = fw * r_frac
            angle = self._hex_angle * (1 if idx % 2 == 0 else -1) + idx * 30
            a_val = max(0, min(255, int(self._halo * 0.6 * alpha_mul)))
            col   = qcol(C.MUTED_C if self.muted else C.INDIGO, a_val)
            w_r   = 1.5 - idx * 0.3
            self._draw_hexagon(p, cx, cy, hex_r, angle, col, w_r)

        # spinning arc rings
        for idx, (r_frac, w_r, arc_l, gap) in enumerate(
            [(0.485, 2.5, 110, 80), (0.405, 1.8, 72, 58), (0.33, 1.2, 50, 44)]
        ):
            ring_r = fw * r_frac
            base   = self._rings[idx]
            a_val  = max(0, min(255, int(self._halo * (1.0 - idx * 0.18))))
            col    = qcol(C.MUTED_C if self.muted else C.PRI, a_val)
            p.setPen(QPen(col, w_r))
            p.setBrush(Qt.BrushStyle.NoBrush)
            angle = base
            rect  = QRectF(cx - ring_r, cy - ring_r, ring_r * 2, ring_r * 2)
            while angle < base + 360:
                p.drawArc(rect, int(angle * 16), int(arc_l * 16))
                angle += arc_l + gap

        # scanners
        sr  = fw * 0.50
        sa  = min(255, int(self._halo * 1.4))
        ex  = 70 if self.speaking else 40
        p.setPen(QPen(qcol(C.MUTED_C if self.muted else C.PRI_BRIGHT, sa), 2.2))
        p.setBrush(Qt.BrushStyle.NoBrush)
        srect = QRectF(cx - sr, cy - sr, sr * 2, sr * 2)
        p.drawArc(srect, int(self._scan * 16), int(ex * 16))
        p.setPen(QPen(qcol(C.ACC, sa // 3), 1.2))
        p.drawArc(srect, int(self._scan2 * 16), int(ex * 16))

        # tick marks
        t_out, t_in = fw * 0.497, fw * 0.472
        for deg in range(0, 360, 6):
            rad  = math.radians(deg)
            is_m = deg % 30 == 0
            inn  = t_in if is_m else t_in + (8 if deg % 15 == 0 else 12)
            a    = 180 if is_m else 70
            p.setPen(QPen(qcol(C.PRI_BRIGHT if is_m else C.PRI, a), 1.0 if is_m else 0.5))
            p.drawLine(
                QPointF(cx + t_out * math.cos(rad), cy - t_out * math.sin(rad)),
                QPointF(cx + inn  * math.cos(rad), cy - inn  * math.sin(rad)),
            )

        # diamond crosshair
        ch_r, gap_h = fw * 0.52, fw * 0.15
        a_ch = int(self._halo * 0.45)
        p.setPen(QPen(qcol(C.PRI, a_ch), 1))
        p.drawLine(QPointF(cx - ch_r, cy), QPointF(cx - gap_h, cy))
        p.drawLine(QPointF(cx + gap_h, cy), QPointF(cx + ch_r, cy))
        p.drawLine(QPointF(cx, cy - ch_r), QPointF(cx, cy - gap_h))
        p.drawLine(QPointF(cx, cy + gap_h), QPointF(cx, cy + ch_r))
        # diagonal lines
        d45 = gap_h * 0.707
        p.setPen(QPen(qcol(C.INDIGO, a_ch // 2), 0.7))
        for dx, dy in [(1, 1), (-1, 1), (1, -1), (-1, -1)]:
            p.drawLine(
                QPointF(cx + dx * gap_h * 1.1, cy + dy * gap_h * 1.1),
                QPointF(cx + dx * ch_r * 0.65, cy + dy * ch_r * 0.65),
            )

        # corner ornaments — art deco style
        hl = cx - fw * 0.50 + 2
        hr = cx + fw * 0.50 - 2
        ht = cy - fw * 0.50 + 2
        hb = cy + fw * 0.50 - 2
        seg = 28
        seg2 = 14
        p.setPen(QPen(qcol(C.PRI_BRIGHT, 220), 1.5))
        for bx, by, dx, dy in [(hl,ht,1,1),(hr,ht,-1,1),(hl,hb,1,-1),(hr,hb,-1,-1)]:
            p.drawLine(QPointF(bx, by), QPointF(bx + dx * seg, by))
            p.drawLine(QPointF(bx, by), QPointF(bx, by + dy * seg))
        p.setPen(QPen(qcol(C.INDIGO, 140), 1.0))
        for bx, by, dx, dy in [(hl,ht,1,1),(hr,ht,-1,1),(hl,hb,1,-1),(hr,hb,-1,-1)]:
            p.drawLine(QPointF(bx + dx * seg + dx * 4, by),
                       QPointF(bx + dx * seg + dx * 4 + dx * seg2, by))
            p.drawLine(QPointF(bx, by + dy * seg + dy * 4),
                       QPointF(bx, by + dy * seg + dy * 4 + dy * seg2))

        # face / orb
        if self._face_px:
            fsz    = int(fw * 0.60 * self._scale)
            scaled = self._face_px.scaled(
                fsz, fsz,
                Qt.AspectRatioMode.KeepAspectRatio,
                Qt.TransformationMode.SmoothTransformation,
            )
            p.drawPixmap(int(cx - fsz / 2), int(cy - fsz / 2), scaled)
        else:
            orb_r = int(fw * 0.26 * self._scale)
            oc    = (180, 0, 80) if self.muted else (40, 20, 130)
            for i in range(8, 0, -1):
                r2  = int(orb_r * i / 8)
                frc = i / 8
                a   = max(0, min(255, int(self._halo * 1.0 * frc)))
                p.setBrush(QBrush(QColor(int(oc[0]*frc), int(oc[1]*frc), int(oc[2]*frc), a)))
                p.setPen(Qt.PenStyle.NoPen)
                p.drawEllipse(QRectF(cx - r2, cy - r2, r2 * 2, r2 * 2))
            p.setPen(QPen(qcol(C.PRI_BRIGHT, min(255, int(self._halo * 2))), 1))
            p.setFont(QFont("Courier New", 13, QFont.Weight.Bold))
            p.drawText(QRectF(cx - 80, cy - 14, 160, 28),
                       Qt.AlignmentFlag.AlignCenter, "F.L.I.N.T")

        # particles
        for pt in self._particles:
            a = max(0, min(255, int(pt[4] * 255)))
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(QBrush(qcol(C.PRI_BRIGHT, a)))
            p.drawEllipse(QPointF(pt[0], pt[1]), 2.2, 2.2)

        # status badge
        sy = cy + fw * 0.415
        if self.muted:
            txt, col = "⊘  MUTED",      qcol(C.MUTED_C)
        elif self.speaking:
            txt, col = "◉  SPEAKING",   qcol(C.ACC)
        elif self.state == "THINKING":
            sym = "◈" if self._blink else "◇"
            txt, col = f"{sym}  THINKING",   qcol(C.ACC2)
        elif self.state == "PROCESSING":
            sym = "▷" if self._blink else "▶"
            txt, col = f"{sym}  PROCESSING", qcol(C.ACC2)
        elif self.state == "LISTENING":
            sym = "●" if self._blink else "○"
            txt, col = f"{sym}  LISTENING",  qcol(C.GREEN)
        else:
            sym = "●" if self._blink else "○"
            txt, col = f"{sym}  {self.state}", qcol(C.PRI)

        # badge pill background
        fm_w = len(txt) * 7 + 24
        pill = QRectF(cx - fm_w / 2, sy - 2, fm_w, 20)
        p.setBrush(QBrush(qcol(C.PANEL, 200)))
        p.setPen(QPen(col, 0.8))
        p.drawRoundedRect(pill, 10, 10)

        p.setPen(QPen(col, 1))
        p.setFont(QFont("Courier New", 9, QFont.Weight.Bold))
        p.drawText(pill, Qt.AlignmentFlag.AlignCenter, txt)

        # waveform bar
        wy  = sy + 26
        N, bw = 40, 7
        wx0 = (W - N * bw) / 2
        for i in range(N):
            if self.muted:
                hgt, cl = 2, qcol(C.MUTED_C, 120)
            elif self.speaking:
                hgt = random.randint(2, 18)
                cl  = qcol(C.PRI_BRIGHT if hgt > 10 else C.PRI, 200)
            else:
                hgt = int(2 + 2 * math.sin(self._tick * 0.08 + i * 0.55))
                cl  = qcol(C.BORDER_B, 160)
            bar_rect = QRectF(wx0 + i * bw, wy + 18 - hgt, bw - 1, hgt)
            p.setBrush(QBrush(cl))
            p.setPen(Qt.PenStyle.NoPen)
            p.drawRoundedRect(bar_rect, 1, 1)

        # data readout — right side of HUD
        dx0 = cx + fw * 0.52 + 6
        if dx0 + 80 < W:
            p.setFont(QFont("Courier New", 7))
            metrics_snap = _metrics.snapshot()
            items = [
                ("CPU", f"{metrics_snap['cpu']:.0f}%", C.PRI),
                ("MEM", f"{metrics_snap['mem']:.0f}%", C.ACC2),
                ("NET", f"{metrics_snap['net']:.1f}M", C.INDIGO),
            ]
            for k, (lbl, val, col) in enumerate(items):
                yy = cy - 28 + k * 20
                p.setPen(QPen(qcol(C.TEXT_DIM), 1))
                p.drawText(QRectF(dx0, yy, 30, 14),
                           Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter, lbl)
                p.setPen(QPen(qcol(col, 200), 1))
                p.drawText(QRectF(dx0 + 28, yy, 50, 14),
                           Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter, val)


# ── Metric bar ───────────────────────────────────────────────────────────────
class MetricBar(QWidget):
    def __init__(self, label: str, color: str = C.PRI, parent=None):
        super().__init__(parent)
        self._label = label
        self._color = color
        self._value = 0.0
        self._text  = "--"
        self.setFixedHeight(40)
        self.setMinimumWidth(80)

    def set_value(self, pct: float, text: str):
        self._value = max(0.0, min(100.0, pct))
        self._text  = text
        self.update()

    def paintEvent(self, _):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        W, H = self.width(), self.height()

        # card bg
        path = QPainterPath()
        path.addRoundedRect(QRectF(0, 0, W, H), 6, 6)
        p.fillPath(path, QBrush(qcol(C.PANEL2)))
        p.setPen(QPen(qcol(C.BORDER_A), 0.8))
        p.drawPath(path)

        bar_h   = 3
        bar_y   = H - bar_h - 6
        bar_w   = W - 14
        bar_x   = 7
        fill_w  = int(bar_w * self._value / 100)

        # bar track
        p.setBrush(QBrush(qcol(C.BAR_BG)))
        p.setPen(Qt.PenStyle.NoPen)
        p.drawRoundedRect(QRectF(bar_x, bar_y, bar_w, bar_h), 1.5, 1.5)

        bar_col = (qcol(C.RED) if self._value > 85
                   else qcol(C.ACC) if self._value > 65
                   else qcol(self._color))

        if fill_w > 0:
            p.setBrush(QBrush(bar_col))
            p.drawRoundedRect(QRectF(bar_x, bar_y, fill_w, bar_h), 1.5, 1.5)
            # glow dot at end
            if fill_w > 4:
                glow = QRadialGradient(bar_x + fill_w, bar_y + bar_h/2, 5)
                glow.setColorAt(0, qcol(self._color, 200))
                glow.setColorAt(1, qcol(self._color, 0))
                p.setBrush(QBrush(glow))
                p.drawEllipse(QPointF(bar_x + fill_w, bar_y + bar_h/2), 5, 5)

        p.setFont(QFont("Courier New", 7, QFont.Weight.Bold))
        p.setPen(QPen(qcol(C.TEXT_DIM), 1))
        p.drawText(QRectF(8, 7, 40, 13),
                   Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
                   self._label)

        p.setFont(QFont("Courier New", 9, QFont.Weight.Bold))
        val_col = bar_col if self._text != "--" else qcol(C.TEXT_DIM)
        p.setPen(QPen(val_col, 1))
        p.drawText(QRectF(0, 6, W - 7, 15),
                   Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter,
                   self._text)


# ── Log widget ───────────────────────────────────────────────────────────────
class LogWidget(QTextEdit):
    _sig = pyqtSignal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setReadOnly(True)
        self.setFont(QFont("Courier New", 9))
        self.setStyleSheet(f"""
            QTextEdit {{
                background: {C.PANEL};
                color: {C.TEXT};
                border: 1px solid {C.BORDER};
                border-radius: 6px;
                padding: 8px;
                selection-background-color: {C.PRI_GHO};
            }}
            QScrollBar:vertical {{
                background: {C.BG};
                width: 6px;
                border: none;
                border-radius: 3px;
            }}
            QScrollBar::handle:vertical {{
                background: {C.BORDER_B};
                border-radius: 3px;
                min-height: 16px;
            }}
            QScrollBar::add-line:vertical,
            QScrollBar::sub-line:vertical {{
                height: 0;
            }}
        """)
        self._queue: list[str] = []
        self._typing  = False
        self._text    = ""
        self._pos     = 0
        self._tag     = "sys"
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
        self._text   = self._queue.pop(0)
        self._pos    = 0
        tl = self._text.lower()
        if   tl.startswith("you:"):    self._tag = "you"
        elif tl.startswith("flint:"): self._tag = "ai"
        elif tl.startswith("file:"):   self._tag = "file"
        elif "err" in tl:              self._tag = "err"
        else:                          self._tag = "sys"
        # Typewriter is a garnish, not a bottleneck: long transcripts or a
        # backlog (FLINT logs fast mid-task) print instantly, otherwise the
        # 5ms/char animation falls minutes behind the conversation.
        if len(self._text) > 120 or len(self._queue) > 3:
            self._insert_whole()
            QTimer.singleShot(0, self._next)
            return
        self._tmr.start(5)

    def _insert_whole(self):
        cur = self.textCursor()
        fmt = cur.charFormat()
        fmt.setForeground(QBrush({
            "you":  qcol(C.WHITE),
            "ai":   qcol(C.PRI_BRIGHT),
            "err":  qcol(C.RED),
            "file": qcol(C.ACC2),
            "sys":  qcol(C.ACC),
        }.get(self._tag, qcol(C.TEXT))))
        cur.movePosition(cur.MoveOperation.End)
        cur.insertText(self._text + "\n", fmt)
        self.setTextCursor(cur)
        self.ensureCursorVisible()

    def _step(self):
        if self._pos < len(self._text):
            ch  = self._text[self._pos]
            cur = self.textCursor()
            fmt = cur.charFormat()
            col = {
                "you":  qcol(C.WHITE),
                "ai":   qcol(C.PRI_BRIGHT),
                "err":  qcol(C.RED),
                "file": qcol(C.ACC2),
                "sys":  qcol(C.ACC),
            }.get(self._tag, qcol(C.TEXT))
            fmt.setForeground(QBrush(col))
            cur.movePosition(cur.MoveOperation.End)
            cur.insertText(ch, fmt)
            self.setTextCursor(cur)
            self.ensureCursorVisible()
            self._pos += 1
        else:
            self._tmr.stop()
            cur = self.textCursor()
            cur.movePosition(cur.MoveOperation.End)
            cur.insertText("\n")
            self.setTextCursor(cur)
            self.ensureCursorVisible()
            QTimer.singleShot(18, self._next)


# ── File handling ─────────────────────────────────────────────────────────────
_FILE_ICONS = {
    "image":   ("🖼", C.PRI),
    "video":   ("🎬", C.ACC),
    "audio":   ("🎵", C.INDIGO),
    "pdf":     ("📄", C.RED),
    "word":    ("📝", C.INDIGO),
    "excel":   ("📊", C.ACC2),
    "code":    ("💻", C.ACC),
    "archive": ("📦", "#fb923c"),
    "pptx":    ("📊", C.ROSE),
    "text":    ("📃", C.TEXT_MED),
    "data":    ("🔧", C.PRI),
    "unknown": ("📎", C.TEXT_DIM),
}
_EXT_TO_CAT = {
    **dict.fromkeys(["jpg","jpeg","png","gif","webp","bmp","tiff","svg","ico"], "image"),
    **dict.fromkeys(["mp4","avi","mov","mkv","wmv","flv","webm","m4v"],         "video"),
    **dict.fromkeys(["mp3","wav","ogg","m4a","aac","flac","wma","opus"],        "audio"),
    **dict.fromkeys(["pdf"],                                                     "pdf"),
    **dict.fromkeys(["doc","docx"],                                              "word"),
    **dict.fromkeys(["xls","xlsx","ods"],                                        "excel"),
    **dict.fromkeys(["ppt","pptx"],                                              "pptx"),
    **dict.fromkeys(["py","js","ts","jsx","tsx","html","css","java","c","cpp",
                     "cs","go","rs","rb","php","swift","kt","sh","sql","lua"],   "code"),
    **dict.fromkeys(["zip","rar","tar","gz","7z","bz2","xz"],                   "archive"),
    **dict.fromkeys(["txt","md","rst","log"],                                    "text"),
    **dict.fromkeys(["csv","tsv","json","xml"],                                  "data"),
}


def _file_category(path: Path) -> str:
    return _EXT_TO_CAT.get(path.suffix.lower().lstrip("."), "unknown")


def _fmt_size(size: int) -> str:
    if   size < 1024:    return f"{size} B"
    elif size < 1024**2: return f"{size/1024:.1f} KB"
    elif size < 1024**3: return f"{size/1024**2:.1f} MB"
    else:                return f"{size/1024**3:.1f} GB"


# ── File drop zone ─────────────────────────────────────────────────────────
class FileDropZone(QWidget):
    file_selected = pyqtSignal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setAcceptDrops(True)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setFixedHeight(90)
        self._current_file: str | None = None
        self._hovering   = False
        self._drag_over  = False
        self._dash_offset = 0.0
        self._anim_tmr = QTimer(self)
        self._anim_tmr.timeout.connect(self._animate)
        self._anim_tmr.start(35)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        self._canvas = _DropCanvas(self)
        layout.addWidget(self._canvas)

    def _animate(self):
        self._dash_offset = (self._dash_offset + 0.7) % 20
        self._canvas.update()

    def dragEnterEvent(self, e: QDragEnterEvent):
        if e.mimeData().hasUrls():
            e.acceptProposedAction()
            self._drag_over = True
            self._canvas.update()

    def dragLeaveEvent(self, e):
        self._drag_over = False
        self._canvas.update()

    def dropEvent(self, e: QDropEvent):
        self._drag_over = False
        urls = e.mimeData().urls()
        if urls:
            path = urls[0].toLocalFile()
            if Path(path).is_file():
                self._set_file(path)
        self._canvas.update()

    def mousePressEvent(self, e):
        if e.button() == Qt.MouseButton.LeftButton:
            self._browse()

    def enterEvent(self, e):
        self._hovering = True
        self._canvas.update()

    def leaveEvent(self, e):
        self._hovering = False
        self._canvas.update()

    def current_file(self) -> str | None:
        return self._current_file

    def clear_file(self):
        self._current_file = None
        self._canvas.update()

    def _browse(self):
        path, _ = QFileDialog.getOpenFileName(
            self, "Select a file for FLINT", str(Path.home()),
            "All Files (*.*);;"
            "Images (*.jpg *.jpeg *.png *.gif *.webp *.bmp *.svg);;"
            "Documents (*.pdf *.docx *.txt *.md *.pptx);;"
            "Data (*.csv *.xlsx *.json *.xml);;"
            "Code (*.py *.js *.ts *.html *.css *.java *.cpp *.go);;"
            "Audio (*.mp3 *.wav *.ogg *.m4a *.aac *.flac);;"
            "Video (*.mp4 *.avi *.mov *.mkv *.wmv *.webm);;"
            "Archives (*.zip *.rar *.tar *.gz *.7z)",
        )
        if path:
            self._set_file(path)

    def _set_file(self, path: str):
        self._current_file = path
        self._canvas.update()
        self.file_selected.emit(path)


class _DropCanvas(QWidget):
    def __init__(self, zone: FileDropZone):
        super().__init__(zone)
        self._z = zone

    def paintEvent(self, _):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        z    = self._z
        W, H = self.width(), self.height()
        pad  = 5
        rect = QRectF(pad, pad, W - pad * 2, H - pad * 2)

        if z._drag_over:
            bg_col = qcol("#100a2a")
        elif z._hovering:
            bg_col = qcol("#0c0918")
        else:
            bg_col = qcol(C.PANEL)

        path = QPainterPath()
        path.addRoundedRect(rect, 8, 8)
        p.fillPath(path, QBrush(bg_col))

        if z._current_file:
            border_col = qcol(C.ACC2, 200)
        elif z._drag_over:
            border_col = qcol(C.PRI_BRIGHT, 230)
        elif z._hovering:
            border_col = qcol(C.PRI, 180)
        else:
            border_col = qcol(C.BORDER_B, 140)

        pen = QPen(border_col, 1.2, Qt.PenStyle.DashLine)
        pen.setDashOffset(z._dash_offset)
        p.setPen(pen)
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.drawRoundedRect(rect, 8, 8)

        if z._current_file:
            self._paint_file(p, W, H)
        elif z._drag_over:
            self._paint_drag_over(p, W, H)
        else:
            self._paint_idle(p, W, H, z._hovering)

    def _paint_idle(self, p, W, H, hover):
        cx, cy = W / 2, H / 2
        col = qcol(C.PRI if hover else C.PRI_DIM)
        # upload icon
        p.setPen(QPen(col, 1.8))
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.drawLine(QPointF(cx, cy - 16), QPointF(cx, cy))
        p.drawLine(QPointF(cx - 7, cy - 8), QPointF(cx, cy - 16))
        p.drawLine(QPointF(cx + 7, cy - 8), QPointF(cx, cy - 16))
        p.drawLine(QPointF(cx - 12, cy + 2), QPointF(cx + 12, cy + 2))
        p.setFont(QFont("Courier New", 8))
        p.setPen(QPen(qcol(C.TEXT if hover else C.TEXT_DIM), 1))
        p.drawText(QRectF(0, cy + 6, W, 15),
                   Qt.AlignmentFlag.AlignCenter,
                   "Drop file  ·  Click to browse")
        p.setFont(QFont("Courier New", 7))
        p.setPen(QPen(qcol(C.TEXT_DIM, 140), 1))
        p.drawText(QRectF(0, cy + 20, W, 13),
                   Qt.AlignmentFlag.AlignCenter,
                   "Images · Video · Audio · PDF · Docs · Code · Data")

    def _paint_drag_over(self, p, W, H):
        cx, cy = W / 2, H / 2
        p.setFont(QFont("Courier New", 18))
        p.setPen(QPen(qcol(C.PRI_BRIGHT), 1))
        p.drawText(QRectF(0, cy - 22, W, 30), Qt.AlignmentFlag.AlignCenter, "⬇")
        p.setFont(QFont("Courier New", 8, QFont.Weight.Bold))
        p.setPen(QPen(qcol(C.PRI), 1))
        p.drawText(QRectF(0, cy + 10, W, 15), Qt.AlignmentFlag.AlignCenter, "Release to load")

    def _paint_file(self, p, W, H):
        path = Path(self._z._current_file)
        cat  = _file_category(path)
        icon, icon_col = _FILE_ICONS.get(cat, _FILE_ICONS["unknown"])
        size_str = _fmt_size(path.stat().st_size)
        ext_str  = path.suffix.upper().lstrip(".") or "FILE"

        bx, bw = 10, 56
        p.setFont(QFont("Segoe UI Emoji", 20) if _OS == "Windows" else QFont("Arial", 20))
        p.setPen(QPen(qcol(icon_col), 1))
        p.drawText(QRectF(bx, 0, bw, H), Qt.AlignmentFlag.AlignCenter, icon)

        tx = bx + bw + 6
        tw = W - tx - 36

        p.setFont(QFont("Courier New", 8, QFont.Weight.Bold))
        p.setPen(QPen(qcol(C.WHITE), 1))
        name = path.name if len(path.name) <= 34 else path.name[:31] + "..."
        p.drawText(QRectF(tx, H * 0.16, tw, 15),
                   Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter, name)

        p.setFont(QFont("Courier New", 7))
        p.setPen(QPen(qcol(C.TEXT_MED), 1))
        p.drawText(QRectF(tx, H * 0.16 + 16, tw, 13),
                   Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
                   f"{ext_str}  ·  {size_str}")

        p.setFont(QFont("Courier New", 6))
        p.setPen(QPen(qcol(C.TEXT_DIM, 160), 1))
        par = str(path.parent)
        if len(par) > 42:
            par = "…" + par[-41:]
        p.drawText(QRectF(tx, H * 0.16 + 30, tw, 11),
                   Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter, par)

        # × button
        p.setFont(QFont("Courier New", 10, QFont.Weight.Bold))
        p.setPen(QPen(qcol(C.RED, 180), 1))
        p.drawText(QRectF(W - 32, 0, 26, H), Qt.AlignmentFlag.AlignCenter, "✕")

    def mousePressEvent(self, e):
        z = self._z
        if z._current_file and e.pos().x() > self.width() - 32:
            z.clear_file()
        else:
            z.mousePressEvent(e)


# ── Setup overlay ─────────────────────────────────────────────────────────────
class SetupOverlay(QWidget):
    done = pyqtSignal(str, str, str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.setStyleSheet(f"""
            SetupOverlay {{
                background: rgba(5, 5, 7, 250);
                border: 1px solid {C.BORDER_B};
                border-radius: 10px;
            }}
        """)

        detected = {"darwin": "mac", "windows": "windows"}.get(_OS.lower(), "linux")
        self._sel_os = detected

        layout = QVBoxLayout(self)
        layout.setContentsMargins(32, 24, 32, 24)
        layout.setSpacing(9)

        def _lbl(txt, font_size=9, bold=False, color=C.PRI,
                 align=Qt.AlignmentFlag.AlignCenter):
            w = QLabel(txt)
            w.setAlignment(align)
            w.setFont(QFont("Courier New", font_size,
                            QFont.Weight.Bold if bold else QFont.Weight.Normal))
            w.setStyleSheet(f"color: {color}; background: transparent;")
            return w

        # header
        header_row = QHBoxLayout()
        dot = QLabel("◈")
        dot.setFont(QFont("Courier New", 16))
        dot.setStyleSheet(f"color: {C.PRI}; background: transparent;")
        header_row.addWidget(dot)
        title_col = QVBoxLayout()
        title_col.addWidget(_lbl("INITIALISATION REQUIRED", 13, True,
                                  align=Qt.AlignmentFlag.AlignLeft))
        title_col.addWidget(_lbl("Configure F.L.I.N.T. before first boot.", 8,
                                  color=C.TEXT_DIM,
                                  align=Qt.AlignmentFlag.AlignLeft))
        header_row.addLayout(title_col)
        header_row.addStretch()
        layout.addLayout(header_row)

        sep = QFrame()
        sep.setFrameShape(QFrame.Shape.HLine)
        sep.setStyleSheet(f"color: {C.BORDER_B};")
        layout.addWidget(sep)

        def _field_lbl(txt):
            return _lbl(txt, 7, color=C.TEXT_DIM,
                        align=Qt.AlignmentFlag.AlignLeft)

        def _field_style(focus_col=C.PRI):
            return f"""
                QLineEdit {{
                    background: #0a0a12; color: {C.TEXT};
                    border: 1px solid {C.BORDER_B}; border-radius: 5px;
                    padding: 5px 10px;
                }}
                QLineEdit:focus {{ border: 1px solid {focus_col}; }}
                QLineEdit::placeholder {{ color: {C.TEXT_DIM}; }}
            """

        layout.addWidget(_field_lbl("GEMINI API KEY"))
        self._key_input = QLineEdit()
        self._key_input.setEchoMode(QLineEdit.EchoMode.Password)
        self._key_input.setPlaceholderText("AIza…")
        self._key_input.setFont(QFont("Courier New", 10))
        self._key_input.setFixedHeight(34)
        self._key_input.setStyleSheet(_field_style(C.PRI))
        layout.addWidget(self._key_input)

        layout.addWidget(_field_lbl("OPENROUTER API KEY"))
        self._or_input = QLineEdit()
        self._or_input.setEchoMode(QLineEdit.EchoMode.Password)
        self._or_input.setPlaceholderText("sk-or-…")
        self._or_input.setFont(QFont("Courier New", 10))
        self._or_input.setFixedHeight(34)
        self._or_input.setStyleSheet(_field_style(C.ACC2))
        layout.addWidget(self._or_input)

        layout.addSpacing(4)
        sep2 = QFrame()
        sep2.setFrameShape(QFrame.Shape.HLine)
        sep2.setStyleSheet(f"color: {C.BORDER};")
        layout.addWidget(sep2)

        layout.addWidget(_field_lbl("OPERATING SYSTEM"))
        det_name = {"windows": "Windows", "mac": "macOS", "linux": "Linux"}[detected]
        layout.addWidget(_lbl(f"Auto-detected: {det_name}", 7, color=C.ACC2,
                               align=Qt.AlignmentFlag.AlignLeft))

        os_row = QHBoxLayout()
        os_row.setSpacing(7)
        self._os_btns: dict[str, QPushButton] = {}
        for key, label in [("windows", "⊞  Windows"), ("mac", "  macOS"), ("linux", "🐧  Linux")]:
            btn = QPushButton(label)
            btn.setFont(QFont("Courier New", 9, QFont.Weight.Bold))
            btn.setFixedHeight(32)
            btn.setCursor(Qt.CursorShape.PointingHandCursor)
            btn.clicked.connect(lambda _, k=key: self._sel(k))
            os_row.addWidget(btn)
            self._os_btns[key] = btn
        layout.addLayout(os_row)
        self._sel(detected)

        layout.addSpacing(6)
        init_btn = QPushButton("▸  INITIALISE SYSTEMS")
        init_btn.setFont(QFont("Courier New", 10, QFont.Weight.Bold))
        init_btn.setFixedHeight(38)
        init_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        init_btn.setStyleSheet(f"""
            QPushButton {{
                background: {C.PRI_GHO}; color: {C.PRI_BRIGHT};
                border: 1px solid {C.PRI_DIM}; border-radius: 5px;
                letter-spacing: 1px;
            }}
            QPushButton:hover {{
                background: #1a0f40; border: 1px solid {C.PRI};
                color: {C.WHITE};
            }}
            QPushButton:pressed {{
                background: {C.PRI_GHO};
            }}
        """)
        init_btn.clicked.connect(self._submit)
        layout.addWidget(init_btn)

    def _sel(self, key: str):
        self._sel_os = key
        pal = {
            "windows": (C.PRI_BRIGHT, "#0d0820"),
            "mac":     (C.ACC2,       "#031a10"),
            "linux":   (C.ACC,        "#1a0e00"),
        }
        for k, btn in self._os_btns.items():
            if k == key:
                fg, bg = pal[k]
                btn.setStyleSheet(f"""
                    QPushButton {{
                        background: {bg}; color: {fg};
                        border: 1px solid {fg}; border-radius: 5px;
                    }}
                """)
            else:
                btn.setStyleSheet(f"""
                    QPushButton {{
                        background: {C.PANEL}; color: {C.TEXT_DIM};
                        border: 1px solid {C.BORDER}; border-radius: 5px;
                    }}
                    QPushButton:hover {{
                        color: {C.TEXT}; border: 1px solid {C.BORDER_B};
                    }}
                """)

    def _submit(self):
        key    = self._key_input.text().strip()
        or_key = self._or_input.text().strip()
        if not key:
            self._key_input.setStyleSheet(
                self._key_input.styleSheet() +
                f" QLineEdit {{ border: 1px solid {C.RED}; }}"
            )
            return
        if not or_key:
            self._or_input.setStyleSheet(
                self._or_input.styleSheet() +
                f" QLineEdit {{ border: 1px solid {C.RED}; }}"
            )
            return
        self.done.emit(key, or_key, self._sel_os)


# ── Section header helper ─────────────────────────────────────────────────────
def _section_label(text: str) -> QLabel:
    lbl = QLabel(text)
    lbl.setFont(QFont("Courier New", 7, QFont.Weight.Bold))
    lbl.setStyleSheet(f"""
        color: {C.TEXT_DIM};
        background: transparent;
        border-bottom: 1px solid {C.BORDER};
        padding-bottom: 4px;
        letter-spacing: 2px;
    """)
    return lbl


# ── Main Window ────────────────────────────────────────────────────────────────
class MainWindow(QMainWindow):
    _log_sig   = pyqtSignal(str)
    _state_sig = pyqtSignal(str)
    _link_sig  = pyqtSignal(int)   # remote clients (phone app / Venom)
    _jobs_sig  = pyqtSignal(int)   # pipeline jobs (active + queued)

    def __init__(self, face_path: str):
        super().__init__()
        self.setWindowTitle("F.L.I.N.T — AXIOM")
        self.setMinimumSize(_MIN_W, _MIN_H)
        self.resize(_DEFAULT_W, _DEFAULT_H)

        screen = QApplication.primaryScreen().availableGeometry()
        self.move(
            (screen.width()  - _DEFAULT_W) // 2,
            (screen.height() - _DEFAULT_H) // 2,
        )

        self.on_text_command   = None
        self._muted            = False
        self._current_file: str | None = None

        central = QWidget()
        central.setStyleSheet(f"background: {C.BG};")
        self.setCentralWidget(central)

        root = QVBoxLayout(central)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)
        root.addWidget(self._build_header())

        body = QHBoxLayout()
        body.setContentsMargins(0, 0, 0, 0)
        body.setSpacing(0)

        self._left_panel = self._build_left_panel()
        body.addWidget(self._left_panel, stretch=0)

        self.hud = HudCanvas(face_path)
        self.hud.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        body.addWidget(self.hud, stretch=5)

        self._right_panel = self._build_right_panel()
        body.addWidget(self._right_panel, stretch=0)

        root.addLayout(body, stretch=1)
        root.addWidget(self._build_footer())

        self._clock_tmr = QTimer(self)
        self._clock_tmr.timeout.connect(self._tick_clock)
        self._clock_tmr.start(1000)
        self._tick_clock()

        self._metric_tmr = QTimer(self)
        self._metric_tmr.timeout.connect(self._update_metrics)
        self._metric_tmr.start(2000)
        self._update_metrics()

        self._log_sig.connect(self._log.append_log)
        self._state_sig.connect(self._apply_state)
        self._link_sig.connect(self._apply_link)
        self._jobs_sig.connect(self._apply_jobs)

        self._overlay: SetupOverlay | None = None
        self._ready = self._check_config()
        if not self._ready:
            self._show_setup()

        sc_mute = QShortcut(QKeySequence("F4"), self)
        sc_mute.activated.connect(self._toggle_mute)
        sc_full = QShortcut(QKeySequence("F11"), self)
        sc_full.activated.connect(self._toggle_fullscreen)

    def _toggle_fullscreen(self):
        if self.isFullScreen():
            self.showNormal()
        else:
            self.showFullScreen()

    def resizeEvent(self, event):
        super().resizeEvent(event)
        if self._overlay and self._overlay.isVisible():
            ow, oh = 480, 440
            cw = self.centralWidget()
            self._overlay.setGeometry(
                (cw.width()  - ow) // 2,
                (cw.height() - oh) // 2,
                ow, oh,
            )

    def _update_metrics(self):
        snap = _metrics.snapshot()

        self._bar_cpu.set_value(snap["cpu"], f"{snap['cpu']:.0f}%")
        self._bar_mem.set_value(snap["mem"], f"{snap['mem']:.0f}%")

        net = snap["net"]
        net_str = f"{net*1024:.0f}KB/s" if net < 1.0 else f"{net:.1f}MB/s"
        self._bar_net.set_value(min(100, net * 10), net_str)

        gpu = snap["gpu"]
        self._bar_gpu.set_value(gpu if gpu >= 0 else 0,
                                 f"{gpu:.0f}%" if gpu >= 0 else "N/A")

        tmp = snap["tmp"]
        self._bar_tmp.set_value(min(100, (tmp / 100) * 100) if tmp >= 0 else 0,
                                 f"{tmp:.0f}°C" if tmp >= 0 else "N/A")

        try:
            elapsed = time.time() - psutil.boot_time()
            h, m = int(elapsed // 3600), int((elapsed % 3600) // 60)
            self._uptime_lbl.setText(f"UPTIME  {h:02d}:{m:02d}")
        except Exception:
            self._uptime_lbl.setText("UPTIME  --:--")

        try:
            self._proc_lbl.setText(f"PROCS  {len(psutil.pids())}")
        except Exception:
            self._proc_lbl.setText("PROCS  --")

    # ── Header ────────────────────────────────────────────────────────────────
    def _build_header(self) -> QWidget:
        w = QWidget()
        w.setFixedHeight(58)
        w.setStyleSheet(f"""
            background: {C.DARK};
            border-bottom: 1px solid {C.BORDER_B};
        """)
        lay = QHBoxLayout(w)
        lay.setContentsMargins(18, 0, 18, 0)

        def _badge(txt, color=C.TEXT_DIM, bold=False):
            l = QLabel(txt)
            l.setFont(QFont("Courier New", 7,
                            QFont.Weight.Bold if bold else QFont.Weight.Normal))
            l.setStyleSheet(f"""
                color: {color};
                background: {C.PANEL2};
                border: 1px solid {C.BORDER_A};
                border-radius: 4px;
                padding: 2px 8px;
            """)
            return l

        lay.addWidget(_badge("AXIOM", C.PRI_DIM))
        lay.addSpacing(8)
        lay.addWidget(_badge("CLASSIFIED", C.RED, bold=True))
        lay.addSpacing(8)
        self._link_lbl = _badge("LINK  --", C.TEXT_DIM)
        lay.addWidget(self._link_lbl)
        lay.addSpacing(8)
        self._jobs_lbl = _badge("JOBS  0", C.TEXT_DIM)
        lay.addWidget(self._jobs_lbl)
        lay.addStretch()

        # centre title
        mid = QVBoxLayout()
        mid.setSpacing(2)
        title = QLabel("F.L.I.N.T")
        title.setAlignment(Qt.AlignmentFlag.AlignCenter)
        title.setFont(QFont("Courier New", 18, QFont.Weight.Bold))
        title.setStyleSheet(f"color: {C.PRI_BRIGHT}; background: transparent; letter-spacing: 6px;")
        mid.addWidget(title)
        sub = QLabel("For Less Intelligent Networked Things")
        sub.setAlignment(Qt.AlignmentFlag.AlignCenter)
        sub.setFont(QFont("Courier New", 7))
        sub.setStyleSheet(f"color: {C.TEXT_DIM}; background: transparent; letter-spacing: 2px;")
        mid.addWidget(sub)
        lay.addLayout(mid)
        lay.addStretch()

        right_col = QVBoxLayout()
        right_col.setSpacing(2)
        self._clock_lbl = QLabel("00:00:00")
        self._clock_lbl.setFont(QFont("Courier New", 15, QFont.Weight.Bold))
        self._clock_lbl.setStyleSheet(f"color: {C.PRI}; background: transparent;")
        self._clock_lbl.setAlignment(Qt.AlignmentFlag.AlignRight)
        right_col.addWidget(self._clock_lbl)
        self._date_lbl = QLabel("")
        self._date_lbl.setFont(QFont("Courier New", 7))
        self._date_lbl.setStyleSheet(f"color: {C.TEXT_DIM}; background: transparent;")
        self._date_lbl.setAlignment(Qt.AlignmentFlag.AlignRight)
        right_col.addWidget(self._date_lbl)
        lay.addLayout(right_col)
        return w

    def _tick_clock(self):
        self._clock_lbl.setText(time.strftime("%H:%M:%S"))
        self._date_lbl.setText(time.strftime("%a  %d  %b  %Y"))

    # ── Left panel ────────────────────────────────────────────────────────────
    def _build_left_panel(self) -> QWidget:
        w = QWidget()
        w.setFixedWidth(_LEFT_W)
        w.setStyleSheet(f"""
            background: {C.DARK};
            border-right: 1px solid {C.BORDER};
        """)
        lay = QVBoxLayout(w)
        lay.setContentsMargins(10, 12, 10, 12)
        lay.setSpacing(5)

        lay.addWidget(_section_label("SYS  MONITOR"))
        lay.addSpacing(3)

        self._bar_cpu = MetricBar("CPU", C.PRI)
        self._bar_mem = MetricBar("MEM", C.ACC2)
        self._bar_net = MetricBar("NET", C.INDIGO)
        self._bar_gpu = MetricBar("GPU", C.ACC)
        self._bar_tmp = MetricBar("TMP", C.ROSE)

        for bar in [self._bar_cpu, self._bar_mem, self._bar_net,
                    self._bar_gpu, self._bar_tmp]:
            lay.addWidget(bar)

        lay.addSpacing(6)

        # info panel
        info = QWidget()
        info.setStyleSheet(f"""
            background: {C.PANEL2};
            border: 1px solid {C.BORDER_A};
            border-radius: 6px;
        """)
        ip_lay = QVBoxLayout(info)
        ip_lay.setContentsMargins(8, 6, 8, 6)
        ip_lay.setSpacing(4)

        self._uptime_lbl = QLabel("UPTIME  --:--")
        self._uptime_lbl.setFont(QFont("Courier New", 7, QFont.Weight.Bold))
        self._uptime_lbl.setStyleSheet(
            f"color: {C.ACC2}; background: transparent; border: none;")
        ip_lay.addWidget(self._uptime_lbl)

        self._proc_lbl = QLabel("PROCS  --")
        self._proc_lbl.setFont(QFont("Courier New", 7))
        self._proc_lbl.setStyleSheet(
            f"color: {C.TEXT_MED}; background: transparent; border: none;")
        ip_lay.addWidget(self._proc_lbl)

        os_name = {"Windows": "WIN", "Darwin": "macOS", "Linux": "LINUX"}.get(_OS, _OS.upper())
        os_lbl = QLabel(f"OS  {os_name}")
        os_lbl.setFont(QFont("Courier New", 7))
        os_lbl.setStyleSheet(f"color: {C.ACC}; background: transparent; border: none;")
        ip_lay.addWidget(os_lbl)

        lay.addWidget(info)
        lay.addStretch()

        # status badges
        badges = [
            ("AI CORE",   "ACTIVE",   C.ACC2),
            ("SECURITY",  "CLEARED",  C.PRI_BRIGHT),
            ("PROTOCOL",  "ODIN",  C.TEXT_DIM),
        ]
        for top, bot, col in badges:
            badge = QWidget()
            badge.setStyleSheet(f"""
                background: {C.PANEL2};
                border: 1px solid {C.BORDER_A};
                border-radius: 5px;
            """)
            bl = QVBoxLayout(badge)
            bl.setContentsMargins(6, 4, 6, 4)
            bl.setSpacing(1)
            tl = QLabel(top)
            tl.setFont(QFont("Courier New", 6))
            tl.setAlignment(Qt.AlignmentFlag.AlignCenter)
            tl.setStyleSheet(f"color: {C.TEXT_DIM}; background: transparent; border: none;")
            vl = QLabel(bot)
            vl.setFont(QFont("Courier New", 8, QFont.Weight.Bold))
            vl.setAlignment(Qt.AlignmentFlag.AlignCenter)
            vl.setStyleSheet(f"color: {col}; background: transparent; border: none;")
            bl.addWidget(tl)
            bl.addWidget(vl)
            lay.addWidget(badge)

        return w

    # ── Right panel ───────────────────────────────────────────────────────────
    def _build_right_panel(self) -> QWidget:
        w = QWidget()
        w.setFixedWidth(_RIGHT_W)
        w.setStyleSheet(f"""
            background: {C.DARK};
            border-left: 1px solid {C.BORDER};
        """)
        lay = QVBoxLayout(w)
        lay.setContentsMargins(10, 10, 10, 10)
        lay.setSpacing(8)

        lay.addWidget(_section_label("ACTIVITY  LOG"))

        self._log = LogWidget()
        lay.addWidget(self._log, stretch=1)

        self._add_divider(lay)

        lay.addWidget(_section_label("FILE  UPLOAD"))
        self._drop_zone = FileDropZone()
        self._drop_zone.file_selected.connect(self._on_file_selected)
        lay.addWidget(self._drop_zone)

        self._file_hint = QLabel("No file loaded")
        self._file_hint.setFont(QFont("Courier New", 7))
        self._file_hint.setStyleSheet(f"color: {C.TEXT_DIM}; background: transparent;")
        self._file_hint.setWordWrap(True)
        lay.addWidget(self._file_hint)

        self._add_divider(lay)

        lay.addWidget(_section_label("COMMAND  INPUT"))
        lay.addLayout(self._build_input_row())

        self._mute_btn = QPushButton()
        self._mute_btn.setFixedHeight(32)
        self._mute_btn.setFont(QFont("Courier New", 8, QFont.Weight.Bold))
        self._mute_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._mute_btn.clicked.connect(self._toggle_mute)
        self._style_mute_btn()
        lay.addWidget(self._mute_btn)

        fs_btn = QPushButton("⛶   FULLSCREEN   [F11]")
        fs_btn.setFixedHeight(26)
        fs_btn.setFont(QFont("Courier New", 7))
        fs_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        fs_btn.setStyleSheet(f"""
            QPushButton {{
                background: transparent; color: {C.TEXT_DIM};
                border: 1px solid {C.BORDER}; border-radius: 5px;
            }}
            QPushButton:hover {{
                color: {C.PRI}; border: 1px solid {C.BORDER_B};
                background: {C.PRI_GHO};
            }}
        """)
        fs_btn.clicked.connect(self._toggle_fullscreen)
        lay.addWidget(fs_btn)

        return w

    def _add_divider(self, lay: QVBoxLayout):
        sep = QFrame()
        sep.setFrameShape(QFrame.Shape.HLine)
        sep.setStyleSheet(f"color: {C.BORDER}; margin: 1px 0;")
        lay.addWidget(sep)

    def _build_input_row(self) -> QHBoxLayout:
        row = QHBoxLayout()
        row.setSpacing(6)

        self._input = QLineEdit()
        self._input.setPlaceholderText("Type a command or question…")
        self._input.setFont(QFont("Courier New", 9))
        self._input.setFixedHeight(32)
        self._input.setStyleSheet(f"""
            QLineEdit {{
                background: {C.PANEL};
                color: {C.WHITE};
                border: 1px solid {C.BORDER_B};
                border-radius: 5px;
                padding: 3px 8px;
            }}
            QLineEdit:focus {{
                border: 1px solid {C.PRI};
                background: {C.PRI_GHO};
            }}
        """)
        self._input.returnPressed.connect(self._send)
        row.addWidget(self._input)

        send = QPushButton("▸")
        send.setFixedSize(32, 32)
        send.setFont(QFont("Courier New", 12, QFont.Weight.Bold))
        send.setCursor(Qt.CursorShape.PointingHandCursor)
        send.setStyleSheet(f"""
            QPushButton {{
                background: {C.PRI_GHO}; color: {C.PRI_BRIGHT};
                border: 1px solid {C.PRI_DIM}; border-radius: 5px;
            }}
            QPushButton:hover {{
                background: #1a0f40; border: 1px solid {C.PRI};
                color: {C.WHITE};
            }}
            QPushButton:pressed {{
                background: {C.BG};
            }}
        """)
        send.clicked.connect(self._send)
        row.addWidget(send)
        return row

    # ── Footer ────────────────────────────────────────────────────────────────
    def _build_footer(self) -> QWidget:
        w = QWidget()
        w.setFixedHeight(24)
        w.setStyleSheet(f"""
            background: {C.DARK};
            border-top: 1px solid {C.BORDER_B};
        """)
        lay = QHBoxLayout(w)
        lay.setContentsMargins(16, 0, 16, 0)

        def _fl(txt, color=C.TEXT_DIM):
            l = QLabel(txt)
            l.setFont(QFont("Courier New", 7))
            l.setStyleSheet(f"color: {color}; background: transparent;")
            return l

        lay.addWidget(_fl("[F4] Mute  ·  [F11] Fullscreen"))
        lay.addStretch()
        lay.addWidget(_fl("Tushar Industries  ·  AXIOM  ·  CLASSIFIED",
                          C.BORDER_B))
        lay.addStretch()
        lay.addWidget(_fl("© STARK INDUSTRIES", C.PRI_DIM))
        return w

    # ── Slots ─────────────────────────────────────────────────────────────────
    def _on_file_selected(self, path: str):
        self._current_file = path
        p    = Path(path)
        cat  = _file_category(p)
        icon, _ = _FILE_ICONS.get(cat, _FILE_ICONS["unknown"])
        size = _fmt_size(p.stat().st_size)
        self._file_hint.setText(
            f"{icon}  {p.name}  ·  {size}  ·  Tell FLINT what to do with it")
        self._log.append_log(f"FILE: {p.name} ({size}) loaded")
        if self.on_text_command:
            msg = (
                f"[FILE_UPLOADED] path={path} | name={p.name} | "
                f"type={p.suffix.lstrip('.')} | size={size} | "
                f"Briefly tell the user you can see the file '{p.name}' "
                f"({size}) has been uploaded and ask what they'd like to do with it."
            )
            threading.Thread(target=self.on_text_command, args=(msg,), daemon=True).start()

    def _toggle_mute(self):
        self._muted = not self._muted
        self.hud.muted = self._muted
        self._style_mute_btn()
        if self._muted:
            self._apply_state("MUTED")
            self._log.append_log("SYS: Microphone muted.")
        else:
            self._apply_state("LISTENING")
            self._log.append_log("SYS: Microphone active.")

    def _style_mute_btn(self):
        if self._muted:
            self._mute_btn.setText("⊘   MICROPHONE  MUTED")
            self._mute_btn.setStyleSheet(f"""
                QPushButton {{
                    background: #1a0010; color: {C.MUTED_C};
                    border: 1px solid {C.MUTED_C}; border-radius: 5px;
                }}
                QPushButton:hover {{
                    background: #260018;
                }}
            """)
        else:
            self._mute_btn.setText("◉   MICROPHONE  ACTIVE")
            self._mute_btn.setStyleSheet(f"""
                QPushButton {{
                    background: #001a10; color: {C.ACC2};
                    border: 1px solid {C.GREEN_D}; border-radius: 5px;
                }}
                QPushButton:hover {{
                    background: #002518; border: 1px solid {C.ACC2};
                }}
            """)

    def _send(self):
        txt = self._input.text().strip()
        if not txt:
            return
        self._input.clear()
        self._log.append_log(f"You: {txt}")
        if self.on_text_command:
            threading.Thread(target=self.on_text_command, args=(txt,), daemon=True).start()

    def _apply_state(self, state: str):
        self.hud.state    = state
        self.hud.speaking = (state == "SPEAKING")
        self.hud.muted    = self._muted

    def _apply_link(self, n: int):
        col = C.ACC2 if n > 0 else C.TEXT_DIM
        self._link_lbl.setText(f"LINK  {n}" if n > 0 else "LINK  --")
        self._link_lbl.setStyleSheet(f"""
            color: {col};
            background: {C.PANEL2};
            border: 1px solid {C.BORDER_A};
            border-radius: 4px;
            padding: 2px 8px;
        """)

    def _apply_jobs(self, n: int):
        col = C.ACC if n > 0 else C.TEXT_DIM
        self._jobs_lbl.setText(f"JOBS  {n}")
        self._jobs_lbl.setStyleSheet(f"""
            color: {col};
            background: {C.PANEL2};
            border: 1px solid {C.BORDER_A};
            border-radius: 4px;
            padding: 2px 8px;
        """)

    def _check_config(self) -> bool:
        if not API_FILE.exists():
            return False
        try:
            d = json.loads(API_FILE.read_text(encoding="utf-8"))
            return (bool(d.get("gemini_api_key")) and
                    bool(d.get("openrouter_api_key")) and
                    bool(d.get("os_system")))
        except Exception:
            return False

    def _show_setup(self):
        ov = SetupOverlay(self.centralWidget())
        cw = self.centralWidget()
        ow, oh = 480, 440
        ov.setGeometry(
            (cw.width()  - ow) // 2,
            (cw.height() - oh) // 2,
            ow, oh,
        )
        ov.done.connect(self._on_setup_done)
        ov.show()
        self._overlay = ov

    def _on_setup_done(self, key: str, or_key: str, os_name: str):
        os.makedirs(CONFIG_DIR, exist_ok=True)
        # MERGE into whatever already exists — a wholesale rewrite here
        # silently destroyed every other field (remote_token, Supabase
        # account, user profile) the first time the overlay ever fired.
        data: dict = {}
        try:
            data = json.loads(API_FILE.read_text(encoding="utf-8"))
        except Exception:
            pass
        data.update({
            "gemini_api_key":     key,
            "openrouter_api_key": or_key,
            "os_system":          os_name,
        })
        API_FILE.write_text(json.dumps(data, indent=4), encoding="utf-8")
        self._ready = True
        if self._overlay:
            self._overlay.hide()
            self._overlay = None
        self._apply_state("LISTENING")
        self._log.append_log(f"SYS: Initialised. OS={os_name.upper()}. FLINT online.")


# ── Shim for tkinter-style mainloop ──────────────────────────────────────────
class _RootShim:
    def __init__(self, app: QApplication):
        self._app = app

    def mainloop(self):
        self._app.exec()

    def protocol(self, *_):
        pass


# ── Public API ─────────────────────────────────────────────────────────────────
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