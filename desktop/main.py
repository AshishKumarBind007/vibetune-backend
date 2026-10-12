"""
VibeTune - floating glassmorphism desktop widget.

PHASE 14: real-time audio-reactive visualizer + Apple-Music-style player.

Built on top of the working Audius implementation (backup:
main_audius_working_backup.py).  Recommendations, backend, Audius search /
stream resolution, floating / drag behaviour and the glass design are
unchanged.

New in this phase:
  * AudioAnalyzer      - real PCM analysis through QAudioBufferOutput
                         (Qt 6.8+); automatic, safe fallback otherwise.
  * VisualizerState    - smoothed energy / bass / mid / treble / punch values.
  * Reactive ring      - premium audio-reactive boundary around the logo
                         (collapsed + expanded header).
  * NowPlayingBar      - title / artist / real QMediaPlayer timeline with
                         seeking and a play/pause button.
  * ResultRow          - animated selection (highlight sweep, glow, press
                         feedback, badge pop) and an audio-driven 3-bar
                         indicator with playing / paused / loading states.

Run:
    cd C:\\Users\\ashis\\VibeTune
    .venv\\Scripts\\activate
    python main.py
"""

import math
import os
import re
import sys
import time
from array import array
from collections import deque

import requests

from PySide6.QtCore import (
    QAbstractAnimation,
    QElapsedTimer,
    QEasingCurve,
    QObject,
    QPoint,
    QPointF,
    QPropertyAnimation,
    QRect,
    QRectF,
    Qt,
    QThread,
    QTimer,
    QVariantAnimation,
    Property,
    Signal,
    Slot,
    QUrl,
)
from PySide6.QtMultimedia import QAudioFormat, QAudioOutput, QMediaPlayer

try:  # Qt 6.8+ only.  Without it the visualizer uses the safe approximation.
    from PySide6.QtMultimedia import QAudioBufferOutput
except ImportError:  # pragma: no cover
    QAudioBufferOutput = None

from PySide6.QtGui import (
    QBrush,
    QColor,
    QConicalGradient,
    QFont,
    QFontMetricsF,
    QGuiApplication,
    QLinearGradient,
    QPainter,
    QPainterPath,
    QPen,
    QPixmap,
    QPolygonF,
    QRadialGradient,
    QTransform,
)
from PySide6.QtWidgets import (
    QApplication,
    QComboBox,
    QGraphicsOpacityEffect,
    QHBoxLayout,
    QLabel,
    QMenu,
    QPushButton,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)

# --------------------------------------------------------------------------
# API configuration / constants
# --------------------------------------------------------------------------

# Backend (already deployed on Render)
API_BASE_URL = "https://vibetune-api.onrender.com"
RECOMMEND_ENDPOINT = f"{API_BASE_URL}/recommend"
REQUEST_TIMEOUT = 30          # seconds (a sleeping free Render instance can be slow)
RESULT_LIMIT = 10

# Audius playback (recommendations still come exclusively from VibeTune V3)
AUDIUS_API_BASE = "https://api.audius.co/v1"
AUDIUS_SEARCH_ENDPOINT = f"{AUDIUS_API_BASE}/tracks/search"
AUDIUS_STREAM_ENDPOINT = f"{AUDIUS_API_BASE}/tracks/{{track_id}}/stream"
AUDIUS_TIMEOUT = 15

MSG_PLAYBACK_UNAVAILABLE = "Playback unavailable on Audius"

MSG_ERROR = "Unable to get recommendations.\nPlease try again."
MSG_TIMEOUT = "The server is taking too long\nto respond. Please try again."
MSG_OFFLINE = "Can't reach the server.\nCheck your connection and try again."
MSG_EMPTY = "No matches found.\nTry a different mood, genre or energy."

MOODS = ["Happy", "Sad", "Angry", "Romantic", "Fearful", "Surprised", "Calm"]
GENRES = ["Pop", "Rock", "Hip Hop", "Electronic", "Jazz", "Country", "Folk"]
ENERGIES = ["Low", "Medium", "High"]

# Geometry (logical pixels). MARGIN is transparent room around the card for glow/shadow.
MARGIN = 18
COLL_W, COLL_H = 232, 96      # collapsed card
EXP_W, EXP_H = 316, 400       # expanded card
PAD = 22                      # inner padding of the expanded card
PANEL_TOP = 94                # y (inside the card) where the controls start
PANEL_W = EXP_W - 2 * PAD
PANEL_H = EXP_H - PANEL_TOP - 16
HEADER_LINE = 88              # y of the header/controls divider (inside the card)

# Interaction
DRAG_THRESHOLD = 6            # px of movement before a press becomes a drag

# Floating (vertical only)
FLOAT_AMPLITUDE = 4.0         # px (+/-)
FLOAT_PERIOD = 5.0            # seconds per full up/down cycle
FLOAT_INTERVAL_MS = 16        # ~60 FPS tick; window only moves when the pixel changes

# Flowing-music animation (also drives the visualizer / timeline smoothing)
WAVE_INTERVAL_MS = 33         # ~30 FPS; only a small region is repainted

# Now-playing bar
NP_H = 86                     # height of the compact "Now Playing" card

# Audio analysis.  Set to False to force the lightweight approximation.
ENABLE_PCM_ANALYSIS = True
PCM_MAX_FRAMES = 2048         # frames analysed per decoded buffer (keeps CPU tiny)
PCM_QUEUE_LEN = 256

GREEN = (29, 185, 84)         # Spotify green #1DB954
MINT = QColor(140, 255, 185)

# (amplitude multiplier, wave cycles across width, speed, fill alpha, line alpha, rgb)
WAVE_LAYERS = [
    (1.00, 1.6, 1.7, 70, 175, (29, 185, 84)),
    (0.75, 2.4, -2.3, 46, 120, (120, 240, 170)),
    (0.55, 3.3, 3.1, 30, 80, (255, 255, 255)),
]


def lerp(a, b, t):
    return a + (b - a) * t


def clamp01(v):
    return max(0.0, min(1.0, v))


def ease_to(cur, target, rate):
    """Exponential smoothing step. `rate` already includes dt (rate = dt * speed)."""
    return cur + (target - cur) * (1.0 - math.exp(-rate))


def green(alpha=255):
    return QColor(GREEN[0], GREEN[1], GREEN[2], alpha)


def fmt_time(ms):
    s = max(0, int(ms // 1000))
    h, rem = divmod(s, 3600)
    m, sec = divmod(rem, 60)
    if h:
        return f"{h}:{m:02d}:{sec:02d}"
    return f"{m:02d}:{sec:02d}"


def draw_text(p, text, font, color, x, y_center):
    """Draw text with its optical vertical centre on y_center (left aligned at x)."""
    p.setFont(font)
    p.setPen(color)
    fm = QFontMetricsF(font)
    p.drawText(QPointF(x, y_center + (fm.ascent() - fm.descent()) / 2), text)


def make_font(size, weight=QFont.Weight.Normal, spacing=0.0):
    f = QFont("Segoe UI")
    f.setPointSizeF(size)
    f.setWeight(weight)
    if spacing:
        f.setLetterSpacing(QFont.SpacingType.AbsoluteSpacing, spacing)
    return f


# --------------------------------------------------------------------------
# Shared glass / music drawing helpers
# --------------------------------------------------------------------------


def _build_note_path():
    """A beamed pair of eighth notes in a [-1, 1] unit box."""
    path = QPainterPath()
    path.setFillRule(Qt.FillRule.WindingFill)
    for cx, cy in ((-0.42, 0.52), (0.42, 0.34)):
        head = QPainterPath()
        head.addEllipse(QPointF(0, 0), 0.30, 0.22)
        tr = QTransform()
        tr.translate(cx, cy)
        tr.rotate(-22)
        path.addPath(tr.map(head))
    path.addRect(QRectF(-0.19, -0.55, 0.085, 1.07))   # left stem
    path.addRect(QRectF(0.61, -0.72, 0.085, 1.04))    # right stem
    path.addPolygon(                                    # beam
        QPolygonF(
            [
                QPointF(-0.19, -0.55),
                QPointF(0.695, -0.72),
                QPointF(0.695, -0.46),
                QPointF(-0.19, -0.29),
            ]
        )
    )
    return path


NOTE_PATH = _build_note_path()


def paint_note(p, c, s, color):
    p.save()
    p.translate(c)
    p.scale(s, s)
    p.setPen(Qt.PenStyle.NoPen)
    p.setBrush(color)
    p.drawPath(NOTE_PATH)
    p.restore()


def paint_logo(p, c, r):
    """
    VibeTune music logo: a glossy green rounded-square badge with a white
    double-note glyph (app-icon style) and a soft halo.
    The audio-reactive ring is drawn around (c, r) by draw_reactive_ring().
    """
    side = 2 * r
    rect = QRectF(c.x() - r, c.y() - r, side, side)
    rad = side * 0.30

    p.setPen(Qt.PenStyle.NoPen)
    halo = QRadialGradient(c, r * 2.3)
    halo.setColorAt(0.0, green(80))
    halo.setColorAt(1.0, green(0))
    p.setBrush(QBrush(halo))
    p.drawEllipse(c, r * 2.3, r * 2.3)

    body = QLinearGradient(rect.topLeft(), rect.bottomRight())
    body.setColorAt(0.0, QColor(72, 232, 132))
    body.setColorAt(0.55, QColor(29, 185, 84))
    body.setColorAt(1.0, QColor(10, 112, 48))
    p.setBrush(QBrush(body))
    p.drawRoundedRect(rect, rad, rad)

    # glossy upper half
    p.save()
    clip = QPainterPath()
    clip.addRoundedRect(rect, rad, rad)
    p.setClipPath(clip)
    gloss = QLinearGradient(rect.topLeft(), QPointF(rect.left(), rect.center().y()))
    gloss.setColorAt(0.0, QColor(255, 255, 255, 90))
    gloss.setColorAt(1.0, QColor(255, 255, 255, 0))
    p.setBrush(QBrush(gloss))
    p.drawRect(QRectF(rect.left(), rect.top(), side, side * 0.55))
    p.restore()

    p.setBrush(Qt.BrushStyle.NoBrush)
    p.setPen(QPen(QColor(255, 255, 255, 95), 1.0))
    p.drawRoundedRect(rect.adjusted(0.5, 0.5, -0.5, -0.5), rad, rad)

    paint_note(p, QPointF(c.x(), c.y() + 0.8), r * 0.60, QColor(0, 40, 15, 70))
    paint_note(p, c, r * 0.60, QColor(255, 255, 255, 245))


def draw_glass(p, card, radius, hover=False, amb_center=None, amb_radius=110.0, amb_alpha=40):
    """
    Apple-style frosted glass body: soft shadow, faint green glow, dark
    translucent gradient, diagonal white tint, green/teal colour bleed and a
    top sheen. (Real behind-window blur is not available in plain Qt, so the
    frosted feel is built from layered gradients.)
    """
    p.setPen(Qt.PenStyle.NoPen)

    # 1. soft drop shadow
    p.setBrush(QColor(0, 0, 0, 5))
    for i in range(8, 0, -1):
        s = i * 1.5
        p.drawRoundedRect(card.adjusted(-s, -s, s, s).translated(0, 5), radius + s, radius + s)

    # 2. faint green glow
    p.setBrush(green(4 if hover else 3))
    for i in range(10, 0, -1):
        s = i * 1.4
        p.drawRoundedRect(card.adjusted(-s, -s, s, s), radius + s, radius + s)

    # 3. glass body
    path = QPainterPath()
    path.addRoundedRect(card, radius, radius)
    base = QLinearGradient(card.topLeft(), card.bottomLeft())
    base.setColorAt(0.0, QColor(34, 40, 38, 224))
    base.setColorAt(1.0, QColor(11, 14, 13, 220))
    p.fillPath(path, QBrush(base))

    p.save()
    p.setClipPath(path)

    tint = QLinearGradient(card.topLeft(), card.bottomRight())
    tint.setColorAt(0.0, QColor(255, 255, 255, 24))
    tint.setColorAt(1.0, QColor(255, 255, 255, 2))
    p.fillRect(card, QBrush(tint))

    if amb_center is not None:
        amb = QRadialGradient(amb_center, amb_radius)
        amb.setColorAt(0.0, green(amb_alpha))
        amb.setColorAt(1.0, green(0))
        p.fillRect(card, QBrush(amb))

    cool = QRadialGradient(card.bottomRight() - QPointF(40, 16), 130)
    cool.setColorAt(0.0, QColor(60, 210, 170, 26))
    cool.setColorAt(1.0, QColor(60, 210, 170, 0))
    p.fillRect(card, QBrush(cool))

    sheen = QLinearGradient(card.topLeft(), QPointF(card.left(), card.top() + card.height() * 0.5))
    sheen.setColorAt(0.0, QColor(255, 255, 255, 30))
    sheen.setColorAt(1.0, QColor(255, 255, 255, 0))
    p.fillRect(card, QBrush(sheen))
    p.restore()
    return path


def draw_rim(p, card, radius, hover=False):
    """Specular glass rim: bright top-left and bottom-right, plus a faint inner line."""
    h = 1 if hover else 0
    grad = QLinearGradient(card.topLeft(), card.bottomRight())
    grad.setColorAt(0.0, QColor(255, 255, 255, 120 + 20 * h))
    grad.setColorAt(0.3, QColor(255, 255, 255, 34 + 8 * h))
    grad.setColorAt(0.7, QColor(255, 255, 255, 20 + 8 * h))
    grad.setColorAt(1.0, QColor(255, 255, 255, 80 + 14 * h))
    p.setBrush(Qt.BrushStyle.NoBrush)
    p.setPen(QPen(QBrush(grad), 1.0))
    p.drawRoundedRect(card.adjusted(0.5, 0.5, -0.5, -0.5), radius - 0.5, radius - 0.5)

    inner = QLinearGradient(card.topLeft(), card.bottomLeft())
    inner.setColorAt(0.0, QColor(255, 255, 255, 26))
    inner.setColorAt(0.5, QColor(255, 255, 255, 0))
    p.setPen(QPen(QBrush(inner), 1.0))
    p.drawRoundedRect(card.adjusted(1.5, 1.5, -1.5, -1.5), radius - 1.5, radius - 1.5)


def draw_wave_ribbon(p, x0, x1, base_y, amp, bottom_y, t):
    """Layered flowing sine ribbons (filled + stroked), drawn between base_y and bottom_y."""
    n = max(8, int((x1 - x0) / 4))
    for i, (amp_mul, cycles, speed, fill_a, line_a, rgb) in enumerate(WAVE_LAYERS):
        curve = QPainterPath()
        for j in range(n + 1):
            u = j / n
            x = x0 + u * (x1 - x0)
            # slow "breathing" of the amplitude + taper at both ends => musical, organic feel
            env = 0.55 + 0.45 * math.sin(t * 0.9 + i * 1.7 + u * 2.5)
            edge = math.sin(math.pi * u) ** 0.7
            y = base_y + amp * amp_mul * env * edge * math.sin(2 * math.pi * cycles * u + t * speed + i * 2.1)
            if j == 0:
                curve.moveTo(x, y)
            else:
                curve.lineTo(x, y)

        fill = QPainterPath(curve)
        fill.lineTo(x1, bottom_y)
        fill.lineTo(x0, bottom_y)
        fill.closeSubpath()

        grad = QLinearGradient(0, base_y - amp, 0, bottom_y)
        grad.setColorAt(0.0, QColor(rgb[0], rgb[1], rgb[2], fill_a))
        grad.setColorAt(1.0, QColor(rgb[0], rgb[1], rgb[2], 0))
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QBrush(grad))
        p.drawPath(fill)

        p.setBrush(Qt.BrushStyle.NoBrush)
        p.setPen(QPen(QColor(rgb[0], rgb[1], rgb[2], line_a), 1.2))
        p.drawPath(curve)


def draw_eq_bars(p, x, y_center, t, n=4, bar_w=3.0, gap=3.0, max_h=22.0):
    """Tiny bouncing equalizer."""
    grad = QLinearGradient(0, y_center - max_h / 2, 0, y_center + max_h / 2)
    grad.setColorAt(0.0, QColor(150, 255, 195, 235))
    grad.setColorAt(1.0, QColor(29, 185, 84, 235))
    p.setPen(Qt.PenStyle.NoPen)
    p.setBrush(QBrush(grad))
    for k in range(n):
        level = 0.5 + 0.5 * math.sin(t * (2.6 + 0.7 * k) + k * 1.9)
        level = 0.18 + 0.82 * (0.6 * level + 0.4 * (0.5 + 0.5 * math.sin(t * 5.3 + k * 0.8)))
        h = max_h * level
        p.drawRoundedRect(QRectF(x + k * (bar_w + gap), y_center - h / 2, bar_w, h), bar_w / 2, bar_w / 2)


# --------------------------------------------------------------------------
# API worker / Recommendation worker (runs on a background QThread - never touches widgets)
# --------------------------------------------------------------------------


def parse_recommendations(data):
    """Validate the backend JSON and return a clean list of recommendation dicts."""
    if not isinstance(data, dict):
        raise ValueError("response is not a JSON object")
    recs = data.get("recommendations")
    if not isinstance(recs, list):
        raise ValueError('response has no "recommendations" list')
    return [r for r in recs if isinstance(r, dict)]


class RecommendWorker(QObject):
    """Sends ONE POST request to the recommendation API and reports back via signals."""

    succeeded = Signal(list)   # list of recommendation dicts
    failed = Signal(str)       # short, user-facing message
    finished = Signal()        # always emitted last (used to stop the thread)

    def __init__(self, payload):
        super().__init__()
        self._payload = payload

    @Slot()
    def run(self):
        try:
            resp = requests.post(RECOMMEND_ENDPOINT, json=self._payload, timeout=REQUEST_TIMEOUT)
            resp.raise_for_status()
            recs = parse_recommendations(resp.json())
            if not recs:
                print("[VibeTune] API returned no recommendations for:", self._payload)
                self.failed.emit(MSG_EMPTY)
            else:
                self.succeeded.emit(recs)
        except requests.Timeout as e:
            print("[VibeTune] Request timed out:", e)
            self.failed.emit(MSG_TIMEOUT)
        except requests.ConnectionError as e:
            print("[VibeTune] Connection error:", e)
            self.failed.emit(MSG_OFFLINE)
        except requests.HTTPError as e:
            print("[VibeTune] HTTP error:", e)
            self.failed.emit(MSG_ERROR)
        except ValueError as e:  # invalid JSON / unexpected response shape
            print("[VibeTune] Bad API response:", e)
            self.failed.emit(MSG_ERROR)
        except requests.RequestException as e:
            print("[VibeTune] Request failed:", e)
            self.failed.emit(MSG_ERROR)
        except Exception as e:  # never let a worker exception escape
            print("[VibeTune] Unexpected error:", repr(e))
            self.failed.emit(MSG_ERROR)
        finally:
            self.finished.emit()


# --------------------------------------------------------------------------
# Glass controls
# --------------------------------------------------------------------------


class GlassPopup(QWidget):
    """Custom frosted-glass dropdown list (replaces the stock QComboBox popup)."""

    ROW_H = 32
    PAD_V = 8
    chosen = Signal(int)
    closed = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowFlags(
            Qt.WindowType.Popup
            | Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.NoDropShadowWindowHint
            | Qt.WindowType.WindowStaysOnTopHint
        )
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setMouseTracking(True)

        self._items = []
        self._current = -1
        self._hover = -1
        self._pressed = -1
        self._font = make_font(10.0)
        self._font_sel = make_font(10.0, QFont.Weight.DemiBold)

        self._fade = QPropertyAnimation(self, b"windowOpacity", self)
        self._fade.setDuration(150)
        self._fade.setEasingCurve(QEasingCurve.Type.OutCubic)
        self._slide = QPropertyAnimation(self, b"pos", self)
        self._slide.setDuration(200)
        self._slide.setEasingCurve(QEasingCurve.Type.OutCubic)

    def open_for(self, combo):
        self._items = [combo.itemText(i) for i in range(combo.count())]
        self._current = combo.currentIndex()
        self._hover = self._current
        self._pressed = -1

        card_w = combo.width()
        card_h = len(self._items) * self.ROW_H + 2 * self.PAD_V
        self.resize(card_w + 2 * MARGIN, card_h + 2 * MARGIN)

        below = combo.mapToGlobal(QPoint(0, combo.height()))
        top_edge = combo.mapToGlobal(QPoint(0, 0))
        avail = combo.screen().availableGeometry()
        card_top = below.y() + 6
        drop_down = True
        if card_top + card_h > avail.bottom():
            card_top = top_edge.y() - 6 - card_h
            drop_down = False

        target = QPoint(below.x() - MARGIN, card_top - MARGIN)
        start = target + QPoint(0, -8 if drop_down else 8)

        self.setWindowOpacity(0.0)
        self.move(start)
        self.show()
        self.setFocus()

        self._fade.stop()
        self._fade.setStartValue(0.0)
        self._fade.setEndValue(1.0)
        self._fade.start()
        self._slide.stop()
        self._slide.setStartValue(start)
        self._slide.setEndValue(target)
        self._slide.start()

    # -- helpers
    def _card(self):
        return QRectF(MARGIN, MARGIN, self.width() - 2 * MARGIN, self.height() - 2 * MARGIN)

    def _row_at(self, pos):
        card = self._card()
        if not card.contains(QPointF(pos)):
            return -1
        i = int((pos.y() - card.top() - self.PAD_V) // self.ROW_H)
        return i if 0 <= i < len(self._items) else -1

    def _choose(self, i):
        self.chosen.emit(i)
        self.close()

    # -- painting
    def paintEvent(self, event):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        card = self._card()
        radius = 20.0
        draw_glass(p, card, radius, amb_center=card.topLeft() + QPointF(40, 18), amb_radius=150, amb_alpha=34)

        for i, text in enumerate(self._items):
            row = QRectF(card.left() + 8, card.top() + self.PAD_V + i * self.ROW_H, card.width() - 16, self.ROW_H - 2)
            selected = i == self._current
            if selected:
                g = QLinearGradient(row.topLeft(), row.topRight())
                g.setColorAt(0.0, green(88))
                g.setColorAt(1.0, green(30))
                p.setPen(Qt.PenStyle.NoPen)
                p.setBrush(QBrush(g))
                p.drawRoundedRect(row, 10, 10)
                p.setBrush(Qt.BrushStyle.NoBrush)
                p.setPen(QPen(green(120), 1.0))
                p.drawRoundedRect(row.adjusted(0.5, 0.5, -0.5, -0.5), 10, 10)
            if i == self._hover:
                g = QLinearGradient(row.topLeft(), row.bottomLeft())
                g.setColorAt(0.0, QColor(255, 255, 255, 34 if not selected else 16))
                g.setColorAt(1.0, QColor(255, 255, 255, 14 if not selected else 6))
                p.setPen(Qt.PenStyle.NoPen)
                p.setBrush(QBrush(g))
                p.drawRoundedRect(row, 10, 10)

            draw_text(
                p, text, self._font_sel if selected else self._font,
                QColor(255, 255, 255, 255 if selected else 225), row.left() + 14, row.center().y(),
            )
            if selected:
                paint_note(p, QPointF(row.right() - 18, row.center().y()), 7.0, MINT)

        draw_rim(p, card, radius)

    # -- input
    def mouseMoveEvent(self, event):
        i = self._row_at(event.position())
        if i != self._hover:
            self._hover = i
            self.update()

    def mousePressEvent(self, event):
        if not self._card().contains(event.position()):
            self.close()  # click on the transparent shadow margin
            return
        self._pressed = self._row_at(event.position())

    def mouseReleaseEvent(self, event):
        i = self._row_at(event.position())
        if i >= 0 and i == self._pressed:
            self._choose(i)
        self._pressed = -1

    def leaveEvent(self, event):
        self._hover = -1
        self.update()
        super().leaveEvent(event)

    def keyPressEvent(self, event):
        k = event.key()
        n = len(self._items)
        if k == Qt.Key.Key_Escape:
            self.close()
        elif k == Qt.Key.Key_Down:
            self._hover = (self._hover + 1) % n
            self.update()
        elif k == Qt.Key.Key_Up:
            self._hover = (self._hover - 1) % n
            self.update()
        elif k in (Qt.Key.Key_Return, Qt.Key.Key_Enter, Qt.Key.Key_Space):
            if self._hover >= 0:
                self._choose(self._hover)
        else:
            super().keyPressEvent(event)

    def hideEvent(self, event):
        self.closed.emit()
        super().hideEvent(event)


class GlassCombo(QComboBox):
    """Frosted-glass selector field that opens the custom GlassPopup."""

    def __init__(self, items, default=0, parent=None):
        super().__init__(parent)
        self.addItems(items)
        self.setCurrentIndex(default)
        self.setFixedHeight(36)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self._popup = None
        self._open = False
        self._hover = False
        self._closed_at = 0.0
        self._font = make_font(10.0)

    def showPopup(self):
        # A click on the field while the popup is open closes it; don't instantly reopen.
        if time.monotonic() - self._closed_at < 0.15 or self._open:
            return
        if self._popup is None:
            self._popup = GlassPopup(self.window())
            self._popup.chosen.connect(self._on_chosen)
            self._popup.closed.connect(self._on_closed)
        self._open = True
        self.update()
        self._popup.open_for(self)

    def hidePopup(self):
        if self._popup is not None:
            self._popup.hide()

    def _on_chosen(self, i):
        self.setCurrentIndex(i)
        self.activated.emit(i)

    def _on_closed(self):
        self._open = False
        self._closed_at = time.monotonic()
        self.update()

    def wheelEvent(self, event):
        event.ignore()  # no accidental changes while scrolling over the field

    def enterEvent(self, event):
        self._hover = True
        self.update()
        super().enterEvent(event)

    def leaveEvent(self, event):
        self._hover = False
        self.update()
        super().leaveEvent(event)

    def paintEvent(self, event):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        r = QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5)
        rad = 12.0
        active = self._hover or self._open

        fill = QLinearGradient(r.topLeft(), r.bottomLeft())
        fill.setColorAt(0.0, QColor(255, 255, 255, 34 if active else 26))
        fill.setColorAt(1.0, QColor(255, 255, 255, 14 if active else 9))
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QBrush(fill))
        p.drawRoundedRect(r, rad, rad)
        if self._open:
            p.setBrush(green(26))
            p.drawRoundedRect(r, rad, rad)

        border = QLinearGradient(r.topLeft(), r.bottomLeft())
        if active:
            border.setColorAt(0.0, green(210))
            border.setColorAt(1.0, green(110))
        else:
            border.setColorAt(0.0, QColor(255, 255, 255, 80))
            border.setColorAt(1.0, QColor(255, 255, 255, 22))
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.setPen(QPen(QBrush(border), 1.0))
        p.drawRoundedRect(r, rad, rad)

        draw_text(p, self.currentText(), self._font, QColor(255, 255, 255), 14, r.center().y())

        # chevron (flips while open)
        d = -1 if self._open else 1
        cx, cy = self.width() - 20, self.height() / 2
        p.setPen(
            QPen(
                MINT if active else QColor(255, 255, 255, 170), 1.8,
                Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap, Qt.PenJoinStyle.RoundJoin,
            )
        )
        p.drawPolyline(
            [QPointF(cx - 4.5, cy - 2 * d), QPointF(cx, cy + 2.5 * d), QPointF(cx + 4.5, cy - 2 * d)]
        )


class GlassButton(QPushButton):
    """Primary action: glossy Spotify-green glass pill."""

    def __init__(self, text, parent=None):
        super().__init__(text, parent)
        self._font = make_font(10.5, QFont.Weight.Bold)

    def paintEvent(self, event):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        enabled = self.isEnabled()
        if not enabled:
            p.setOpacity(0.62)  # subtle "busy" look while a request is running
        r = QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5)
        rad = r.height() / 2
        hover, down = self.underMouse() and enabled, self.isDown()

        top = QColor(66, 232, 130) if hover else QColor(44, 214, 108)
        bot = QColor(14, 128, 56) if down else (QColor(22, 168, 76) if hover else QColor(18, 150, 68))
        body = QLinearGradient(r.topLeft(), r.bottomLeft())
        body.setColorAt(0.0, top)
        body.setColorAt(1.0, bot)
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QBrush(body))
        p.drawRoundedRect(r, rad, rad)

        p.save()
        clip = QPainterPath()
        clip.addRoundedRect(r, rad, rad)
        p.setClipPath(clip)
        gloss = QLinearGradient(r.topLeft(), QPointF(r.left(), r.center().y() + 2))
        gloss.setColorAt(0.0, QColor(255, 255, 255, 80))
        gloss.setColorAt(1.0, QColor(255, 255, 255, 0))
        p.setBrush(QBrush(gloss))
        p.drawRect(QRectF(r.left(), r.top(), r.width(), r.height() * 0.58))
        p.restore()

        rim = QLinearGradient(r.topLeft(), r.bottomLeft())
        rim.setColorAt(0.0, QColor(255, 255, 255, 130))
        rim.setColorAt(1.0, QColor(255, 255, 255, 24))
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.setPen(QPen(QBrush(rim), 1.0))
        p.drawRoundedRect(r, rad, rad)

        # note glyph + label, centred together
        fm = QFontMetricsF(self._font)
        text_w = fm.horizontalAdvance(self.text())
        glyph_w = 16.0
        total = glyph_w + 8 + text_w
        x0 = (self.width() - total) / 2
        paint_note(p, QPointF(x0 + glyph_w / 2, r.center().y()), 7.5, QColor(3, 19, 10, 230))
        draw_text(p, self.text(), self._font, QColor(3, 19, 10), x0 + glyph_w + 8, r.center().y())


# --------------------------------------------------------------------------
# Audius resolver (persistent worker thread - unchanged logic)
# --------------------------------------------------------------------------


def _norm_match(text):
    """Normalize text for forgiving artist/title matching against Audius."""
    text = str(text or "").lower()
    text = re.sub(r"\([^)]*\)", " ", text)
    text = re.sub(r"\[[^]]*\]", " ", text)
    text = re.sub(r"[^a-z0-9]+", " ", text)
    return " ".join(text.split())



class AudiusResolverWorker(QObject):
    """Background worker for Audius search and stream URL resolution."""

    succeeded = Signal(str, dict, int)
    failed = Signal(str, int)

    def __init__(self):
        super().__init__()

    @staticmethod
    def _score(track, artist, song):
        target_artist = _norm_match(artist)
        target_song = _norm_match(song)

        user = track.get("user") or {}
        got_artist = _norm_match(user.get("name"))
        got_title = _norm_match(track.get("title"))

        score = 0.0

        if target_artist and got_artist == target_artist:
            score += 0.60
        elif target_artist and target_artist in got_artist:
            score += 0.35

        if target_song and got_title == target_song:
            score += 0.40
        elif target_song and (
            target_song in got_title or got_title in target_song
        ):
            score += 0.25

        if track.get("streamable", True):
            score += 0.05

        return score

    @Slot(str, str, int)
    def resolve(self, artist, song, request_id):
        stream_resp = None

        try:
            # Step 1: Search Audius for matching tracks.
            query = f"{artist} {song}"

            resp = requests.get(
                AUDIUS_SEARCH_ENDPOINT,
                params={"query": query, "limit": 10},
                timeout=AUDIUS_TIMEOUT,
            )
            resp.raise_for_status()

            data = resp.json()
            tracks = data.get("data", [])

            if not isinstance(tracks, list) or not tracks:
                self.failed.emit(
                    MSG_PLAYBACK_UNAVAILABLE, request_id
                )
                return

            tracks = [
                track for track in tracks
                if isinstance(track, dict)
            ]

            if not tracks:
                self.failed.emit(
                    MSG_PLAYBACK_UNAVAILABLE, request_id
                )
                return

            # Step 2: Rank results by artist and title match.
            tracks.sort(
                key=lambda track: self._score(
                    track, artist, song
                ),
                reverse=True,
            )

            track = tracks[0]
            track_id = track.get("id")

            if not track_id:
                self.failed.emit(
                    MSG_PLAYBACK_UNAVAILABLE, request_id
                )
                return

            # Step 3: Resolve the stream URL without downloading
            # the entire audio response body.
            stream_url = AUDIUS_STREAM_ENDPOINT.format(
                track_id=track_id
            )

            stream_resp = requests.get(
                stream_url,
                allow_redirects=True,
                timeout=AUDIUS_TIMEOUT,
                stream=True,
            )
            stream_resp.raise_for_status()

            final_url = stream_resp.url

            if not final_url:
                self.failed.emit(
                    MSG_PLAYBACK_UNAVAILABLE, request_id
                )
                return

            # Step 4: Prepare track information for the player.
            user = track.get("user") or {}

            info = {
                "audius_id": track_id,
                "song": str(track.get("title") or song),
                "artist": str(user.get("name") or artist),
                "requested_song": song,
                "requested_artist": artist,
            }

            self.succeeded.emit(
                final_url, info, request_id
            )

        except requests.RequestException as exc:
            print(
                "[VibeTune] Audius request failed:",
                repr(exc),
            )
            self.failed.emit(
                MSG_PLAYBACK_UNAVAILABLE, request_id
            )

        except Exception as exc:
            print(
                "[VibeTune] Audius resolver error:",
                repr(exc),
            )
            self.failed.emit(
                MSG_PLAYBACK_UNAVAILABLE, request_id
            )

        finally:
            # Release the HTTP connection after reading the URL.
            if stream_resp is not None:
                stream_resp.close()



# A neutral sample used whenever audio analysis has no usable data.
_ZERO_SAMPLE = (0.0, 0.0, 0.0, 0.0, 0.0)


class AudioAnalyzer:
    """
    Safe visualizer fallback.

    The supplied source referenced AudioAnalyzer but omitted its implementation.
    This fallback keeps playback independent from visualization and provides
    subtle position-driven motion. It does not claim to measure real PCM audio.
    """

    def __init__(self, player, parent=None):
        self.player = player
        self._attached = True
        self._reset_position = 0

    def reset(self):
        self._reset_position = int(self.player.position()) if self.player else 0

    def sample(self, position_ms, playing=True):
        if not self._attached or not playing:
            return _ZERO_SAMPLE
        # Gentle deterministic movement while playing, so the visualizer remains
        # alive even when PCM-buffer analysis is unavailable.
        phase = max(0, int(position_ms) - self._reset_position) / 1000.0
        energy = 0.10 + 0.04 * (0.5 + 0.5 * math.sin(phase * 2.3))
        bass = 0.08 + 0.05 * (0.5 + 0.5 * math.sin(phase * 1.7))
        mid = 0.07 + 0.04 * (0.5 + 0.5 * math.sin(phase * 2.9 + 0.8))
        treble = 0.06 + 0.04 * (0.5 + 0.5 * math.sin(phase * 3.7 + 1.4))
        punch = 0.04 + 0.04 * (0.5 + 0.5 * math.sin(phase * 4.1))
        return (energy, bass, mid, treble, punch)

    def detach(self):
        self._attached = False


# --------------------------------------------------------------------------
# Visualizer state (smoothed audio values shared by ring, wave and rows)
# --------------------------------------------------------------------------


class VisualizerState:
    """Premium smoothing: fast-ish attack, slow release, never violent jumps."""

    def __init__(self):
        self.energy = 0.0
        self.bass = 0.0
        self.mid = 0.0
        self.treble = 0.0
        self.punch = 0.0
        self.activity = 0.0   # 0 idle -> 1 playing (0.28 while paused)
        self.phase = 0.0

    @staticmethod
    def _follow(cur, target, dt, attack=14.0, release=3.2):
        rate = attack if target > cur else release
        return ease_to(cur, target, dt * rate)

    def update(self, dt, mode, sample):
        act_target = {"playing": 1.0, "paused": 0.28}.get(mode, 0.0)
        self.activity = ease_to(
            self.activity, act_target, dt * (3.2 if act_target > self.activity else 2.0)
        )
        lv, b, m, t, tr = sample if (mode == "playing" and sample) else _ZERO_SAMPLE
        self.energy = self._follow(self.energy, lv, dt)
        self.bass = self._follow(self.bass, b, dt, 16.0, 4.0)
        self.mid = self._follow(self.mid, m, dt)
        self.treble = self._follow(self.treble, t, dt, 18.0, 5.0)
        self.punch = self._follow(self.punch, tr, dt, 28.0, 7.0)
        speed = (0.7 + 1.8 * self.energy + 1.2 * self.punch) * (0.35 + 0.65 * self.activity)
        self.phase = (self.phase + dt * speed) % (2.0 * math.pi * 6.0)


def draw_reactive_ring(p, c, r, vis, scale=1.0):
    """Audio-reactive circular boundary around the logo (opacity comes from the painter)."""
    act = vis.activity
    if act < 0.01:
        return
    e, b, t, pu, ph = vis.energy, vis.bass, vis.treble, vis.punch, vis.phase

    # soft glow behind the logo
    glow_a = int(255 * clamp01((0.05 + 0.20 * e + 0.16 * pu) * act))
    glow = QRadialGradient(c, r + 9 * scale)
    glow.setColorAt(0.0, green(0))
    glow.setColorAt(0.7, green(glow_a))
    glow.setColorAt(1.0, green(0))
    p.setPen(Qt.PenStyle.NoPen)
    p.setBrush(QBrush(glow))
    p.drawEllipse(c, r + 9 * scale, r + 9 * scale)

    grow = (0.5 + 2.2 * b + 1.4 * pu) * act
    a1 = (0.7 + 2.6 * e) * act
    a2 = (0.3 + 1.8 * t + 1.2 * pu) * act
    a3 = (0.2 + 1.2 * e) * act

    n = 96
    path = QPainterPath()
    two_pi = 2.0 * math.pi
    for i in range(n + 1):
        ang = two_pi * i / n
        rr = r + scale * (
            grow
            + a1 * 0.50 * math.sin(3 * ang + ph)
            + a2 * 0.35 * math.sin(7 * ang - ph * 1.6)
            + a3 * 0.25 * math.sin(11 * ang + ph * 2.2)
        )
        x = c.x() + rr * math.cos(ang)
        y = c.y() + rr * math.sin(ang)
        if i == 0:
            path.moveTo(x, y)
        else:
            path.lineTo(x, y)

    p.setBrush(Qt.BrushStyle.NoBrush)
    p.setPen(QPen(green(int(clamp01((0.10 + 0.16 * e) * act) * 255)), (5.0 + 3.0 * e) * scale,
                  Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap, Qt.PenJoinStyle.RoundJoin))
    p.drawPath(path)

    a_main = int(255 * clamp01((0.48 + 0.42 * e) * act))
    cg = QConicalGradient(c, math.degrees(ph * 0.6) % 360.0)
    cg.setColorAt(0.00, QColor(150, 255, 195, a_main))
    cg.setColorAt(0.35, QColor(29, 185, 84, int(a_main * 0.55)))
    cg.setColorAt(0.65, QColor(120, 240, 170, a_main))
    cg.setColorAt(1.00, QColor(150, 255, 195, a_main))
    p.setPen(QPen(QBrush(cg), (1.3 + 0.9 * e) * scale,
                  Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap, Qt.PenJoinStyle.RoundJoin))
    p.drawPath(path)


# --------------------------------------------------------------------------
# Timeline / player UI
# --------------------------------------------------------------------------


class SeekSlider(QWidget):
    """Glass timeline: real position in, scrub/seek out.  Handle appears on hover/drag."""

    scrubbing = Signal(float)   # fraction while dragging
    released = Signal(float)    # fraction on release

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFixedHeight(16)
        self.setMouseTracking(True)
        self._frac = 0.0        # displayed fraction (eased)
        self._target = 0.0      # real playback fraction
        self._seekable = False
        self._drag = False
        self._hover = False
        self._handle = 0.0      # 0..1 eased handle / thickness
        self._glow = 0.0

    def set_seekable(self, ok):
        self._seekable = bool(ok)
        self.setCursor(Qt.CursorShape.PointingHandCursor if ok else Qt.CursorShape.ArrowCursor)
        self.update()

    def set_target(self, frac, snap=False):
        self._target = clamp01(frac)
        if snap:
            self._frac = self._target
        self.update()

    def tick(self, dt, glow):
        before = (self._frac, self._handle, self._glow)
        if not self._drag:
            self._frac = ease_to(self._frac, self._target, dt * 16.0)
        want = 1.0 if (self._seekable and (self._hover or self._drag)) else 0.0
        self._handle = ease_to(self._handle, want, dt * 14.0)
        self._glow = ease_to(self._glow, glow, dt * 8.0)
        if (self._frac, self._handle, self._glow) != before:
            self.update()

    def _frac_at(self, x):
        x0, x1 = 6.0, self.width() - 6.0
        return clamp01((x - x0) / max(1.0, x1 - x0))

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton and self._seekable:
            self._drag = True
            self._frac = self._frac_at(event.position().x())
            self.scrubbing.emit(self._frac)
            self.update()
            event.accept()
            return
        event.ignore()

    def mouseMoveEvent(self, event):
        if self._drag:
            self._frac = self._frac_at(event.position().x())
            self.scrubbing.emit(self._frac)
            self.update()
            event.accept()
        else:
            event.ignore()

    def mouseReleaseEvent(self, event):
        if self._drag and event.button() == Qt.MouseButton.LeftButton:
            self._drag = False
            self._target = self._frac
            self.released.emit(self._frac)
            self.update()
            event.accept()
            return
        event.ignore()

    def enterEvent(self, event):
        self._hover = True
        super().enterEvent(event)

    def leaveEvent(self, event):
        self._hover = False
        super().leaveEvent(event)

    def paintEvent(self, event):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        x0, x1 = 6.0, self.width() - 6.0
        cy = self.height() / 2.0
        th = 3.0 + 2.0 * self._handle
        track = QRectF(x0, cy - th / 2, x1 - x0, th)

        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QColor(255, 255, 255, 44))
        p.drawRoundedRect(track, th / 2, th / 2)
        if not self._seekable:
            return

        xf = x0 + (x1 - x0) * self._frac
        if xf - x0 > 0.5:
            g = QLinearGradient(x0, 0, x1, 0)
            g.setColorAt(0.0, green(235))
            g.setColorAt(1.0, QColor(150, 255, 195, 245))
            p.setBrush(QBrush(g))
            p.drawRoundedRect(QRectF(x0, cy - th / 2, xf - x0, th), th / 2, th / 2)

        if self._glow > 0.02:
            gl = QRadialGradient(QPointF(xf, cy), 10.0)
            gl.setColorAt(0.0, green(int(110 * self._glow)))
            gl.setColorAt(1.0, green(0))
            p.setBrush(QBrush(gl))
            p.drawEllipse(QPointF(xf, cy), 10.0, 10.0)

        if self._handle > 0.04:
            rad = 5.5 * self._handle
            p.setBrush(QColor(0, 0, 0, int(70 * self._handle)))
            p.drawEllipse(QPointF(xf, cy + 1.0), rad + 0.8, rad + 0.8)
            p.setBrush(QColor(255, 255, 255, int(250 * self._handle)))
            p.drawEllipse(QPointF(xf, cy), rad, rad)


class NowPlayingBar(QWidget):
    """Compact Apple-Music-inspired "Now Playing" card with a real timeline."""

    seek_requested = Signal(int)
    toggle_requested = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setMouseTracking(True)
        self._title_new = ""
        self._artist_new = ""
        self._title_old = ""
        self._artist_old = ""
        self._tt = 1.0                 # title transition 0..1
        self._pos = 0
        self._dur = 0
        self._playing = False
        self._hold_until = 0.0         # ignore stale position events right after a seek
        self._scrub_ms = None
        self._btn_hover = False

        self._f_label = make_font(7.0, QFont.Weight.Bold, 1.1)
        self._f_title = make_font(10.5, QFont.Weight.DemiBold)
        self._f_artist = make_font(8.5)
        self._f_time = make_font(8.0)

        self.slider = SeekSlider(self)
        self.slider.scrubbing.connect(self._on_scrub)
        self.slider.released.connect(self._on_release)

    # -- state in
    def begin_track(self, title, artist):
        self._title_old, self._artist_old = self._title_new, self._artist_new
        self._title_new, self._artist_new = title, artist
        self._tt = 0.0
        self._pos = 0
        self._dur = 0
        self._scrub_ms = None
        self._hold_until = 0.0
        self.slider.set_seekable(False)
        self.slider.set_target(0.0, snap=True)
        self.update()

    def set_position(self, ms):
        if time.monotonic() < self._hold_until:
            return
        self._pos = max(0, int(ms))
        self._sync_slider()
        self.update()

    def set_duration(self, ms):
        self._dur = max(0, int(ms))
        self.slider.set_seekable(self._dur > 0)
        self._sync_slider()
        self.update()

    def set_playing(self, playing):
        self._playing = bool(playing)
        self.update()

    def reset_position(self):
        self._pos = 0
        self._scrub_ms = None
        self.slider.set_target(0.0)
        self.update()

    def _sync_slider(self):
        if self._dur > 0:
            self.slider.set_target(self._pos / self._dur)
        else:
            self.slider.set_target(0.0)

    # -- slider callbacks
    def _on_scrub(self, frac):
        self._scrub_ms = int(frac * self._dur)
        self.update()

    def _on_release(self, frac):
        ms = int(frac * self._dur)
        self._scrub_ms = None
        self._pos = ms
        self._hold_until = time.monotonic() + 0.35
        self.seek_requested.emit(ms)
        self.update()

    # -- animation (driven by the widget's existing wave timer)
    def tick(self, dt, vis):
        if self._tt < 1.0:
            self._tt = min(1.0, self._tt + dt / 0.30)
            self.update()
        glow = clamp01(vis.energy * vis.activity * 1.2) if self._playing else 0.0
        self.slider.tick(dt, glow)

    # -- layout / input
    def resizeEvent(self, event):
        self.slider.setGeometry(14, 52, max(10, self.width() - 28), 16)
        super().resizeEvent(event)

    def _btn_center(self):
        return QPointF(self.width() - 26.0, 31.0)

    def _over_btn(self, pos):
        c = self._btn_center()
        return math.hypot(pos.x() - c.x(), pos.y() - c.y()) <= 15.0

    def mouseMoveEvent(self, event):
        over = self._over_btn(event.position())
        if over != self._btn_hover:
            self._btn_hover = over
            self.setCursor(Qt.CursorShape.PointingHandCursor if over else Qt.CursorShape.ArrowCursor)
            self.update()
        event.ignore()

    def leaveEvent(self, event):
        if self._btn_hover:
            self._btn_hover = False
            self.update()
        super().leaveEvent(event)

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton and self._over_btn(event.position()):
            self.toggle_requested.emit()
            event.accept()
            return
        event.ignore()  # lets the widget be dragged from empty areas, like the header

    # -- paint
    def paintEvent(self, event):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        w = float(self.width())
        r = QRectF(0.5, 0.5, w - 1.0, NP_H - 1.0)
        rad = 16.0

        fill = QLinearGradient(r.topLeft(), r.bottomLeft())
        fill.setColorAt(0.0, QColor(255, 255, 255, 30))
        fill.setColorAt(1.0, QColor(255, 255, 255, 10))
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QBrush(fill))
        p.drawRoundedRect(r, rad, rad)
        tint = QLinearGradient(r.topLeft(), r.topRight())
        tint.setColorAt(0.0, green(40))
        tint.setColorAt(1.0, green(8))
        p.setBrush(QBrush(tint))
        p.drawRoundedRect(r, rad, rad)
        border = QLinearGradient(r.topLeft(), r.bottomLeft())
        border.setColorAt(0.0, green(120))
        border.setColorAt(1.0, green(48))
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.setPen(QPen(QBrush(border), 1.0))
        p.drawRoundedRect(r, rad, rad)

        draw_text(p, "NOW PLAYING", self._f_label, QColor(140, 255, 185, 205), 14, 13)

        avail = w - 14 - 52
        e = 1.0 - (1.0 - self._tt) ** 3
        fm_t = QFontMetricsF(self._f_title)
        fm_a = QFontMetricsF(self._f_artist)
        if self._tt < 1.0 and self._title_old:
            draw_text(p, fm_t.elidedText(self._title_old, Qt.TextElideMode.ElideRight, avail), self._f_title,
                      QColor(255, 255, 255, int(255 * (1 - e))), 14, 31 - 5 * e)
            draw_text(p, fm_a.elidedText(self._artist_old, Qt.TextElideMode.ElideRight, avail), self._f_artist,
                      QColor(255, 255, 255, int(165 * (1 - e))), 14, 46 - 5 * e)
        draw_text(p, fm_t.elidedText(self._title_new, Qt.TextElideMode.ElideRight, avail), self._f_title,
                  QColor(255, 255, 255, int(255 * e)), 14, 31 + 5 * (1 - e))
        draw_text(p, fm_a.elidedText(self._artist_new, Qt.TextElideMode.ElideRight, avail), self._f_artist,
                  QColor(255, 255, 255, int(165 * e)), 14, 46 + 5 * (1 - e))

        # play / pause button
        c = self._btn_center()
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QColor(255, 255, 255, 52 if self._btn_hover else 30))
        p.drawEllipse(c, 13.0, 13.0)
        p.setPen(QPen(QColor(255, 255, 255, 70 if self._btn_hover else 40), 1.0))
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.drawEllipse(c, 13.0, 13.0)
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QColor(255, 255, 255, 245))
        if self._playing:
            p.drawRoundedRect(QRectF(c.x() - 4.6, c.y() - 5.2, 3.2, 10.4), 1.2, 1.2)
            p.drawRoundedRect(QRectF(c.x() + 1.4, c.y() - 5.2, 3.2, 10.4), 1.2, 1.2)
        else:
            p.drawPolygon(QPolygonF([
                QPointF(c.x() - 3.6, c.y() - 5.6),
                QPointF(c.x() + 5.6, c.y()),
                QPointF(c.x() - 3.6, c.y() + 5.6),
            ]))

        # times (--:-- until the real duration is known)
        cur = self._scrub_ms if self._scrub_ms is not None else self._pos
        right = fmt_time(self._dur) if self._dur > 0 else "--:--"
        col = QColor(255, 255, 255, 150)
        draw_text(p, fmt_time(cur), self._f_time, col, 14, 77)
        fm = QFontMetricsF(self._f_time)
        draw_text(p, right, self._f_time, col, w - 14 - fm.horizontalAdvance(right), 77)


# --------------------------------------------------------------------------
# Result rows (selection animation + playing indicator)
# --------------------------------------------------------------------------


class ResultRow(QWidget):
    """One compact glass row: rank badge, song, artist and 'genre • emotion'.

    States: idle | loading | playing | paused.  Highlight, press feedback,
    badge pop and the 3 bars are eased; a private 30 FPS timer runs only
    while something is still moving.
    """
    clicked = Signal()

    ROW_H = 60

    def __init__(self, rank, song, artist, meta, parent=None):
        super().__init__(parent)
        self.setFixedHeight(self.ROW_H)
        self._rank = str(rank)
        self._song = song
        self._artist = artist
        self._meta = meta
        self._hover = False
        self._pressing = False

        self.vis = None            # shared VisualizerState (set by the widget)
        self.state = "idle"
        self._hl = 0.0             # highlight progress
        self._press = 0.0
        self._pop = 0.0
        self._bars = [4.0, 4.0, 4.0]
        self._bar_alpha = 0.0
        self._ph = 0.0

        self._clock = QElapsedTimer()
        self._timer = QTimer(self)
        self._timer.setInterval(WAVE_INTERVAL_MS)
        self._timer.timeout.connect(self._tick)

        self._f_rank = make_font(8.5, QFont.Weight.Bold)
        self._f_song = make_font(10.0, QFont.Weight.DemiBold)
        self._f_artist = make_font(8.5)
        self._f_meta = make_font(8.0)
        self.setToolTip(f"{song} - {artist}\n{meta}" if meta else f"{song} - {artist}")

    # -- state
    def set_state(self, state, instant=False):
        if state == self.state and not instant:
            return
        prev = self.state
        self.state = state
        if prev == "idle" and state != "idle" and not instant:
            self._pop = 1.0
        if instant:
            self._hl = 0.0 if state == "idle" else 1.0
            self._bar_alpha = self._bar_alpha_target()
            self._pop = 0.0
        self._ensure_timer()
        self.update()

    def set_playing(self, playing):   # backwards-compatible helper
        self.set_state("playing" if playing else "idle")

    def _bar_alpha_target(self):
        return {"playing": 1.0, "paused": 0.6, "loading": 0.45}.get(self.state, 0.0)

    def _ensure_timer(self):
        if not self._timer.isActive():
            self._clock.restart()
            self._timer.start()

    def _bar_targets(self):
        st, v, ph = self.state, self.vis, self._ph
        if st == "playing":
            if v is None:
                return [4.0 + 15.0 * (0.5 + 0.5 * math.sin(ph * (1 + 0.35 * i) + i * 2.1)) for i in range(3)]
            bands = (v.bass, v.mid, v.treble)
            out = []
            for i in range(3):
                wv = 0.5 + 0.5 * math.sin(ph * (1.0 + 0.35 * i) + i * 2.1)
                lvl = clamp01(0.62 * bands[i] + 0.18 * v.energy + 0.22 * wv * (0.35 + v.energy) + 0.18 * v.punch)
                out.append(4.0 + 15.0 * lvl)
            return out
        if st == "loading":
            return [4.5 + 2.5 * (0.5 + 0.5 * math.sin(ph * 0.5 + i)) for i in range(3)]
        return [4.0, 4.0, 4.0]

    def _tick(self):
        dt = min(self._clock.restart() / 1000.0, 0.1)
        st = self.state
        self._hl = ease_to(self._hl, 0.0 if st == "idle" else 1.0, dt * 8.0)
        self._press = ease_to(self._press, 1.0 if self._pressing else 0.0, dt * 24.0)
        self._pop *= math.exp(-dt * 3.2)
        self._ph += dt * 5.0
        ba_t = self._bar_alpha_target()
        self._bar_alpha = ease_to(self._bar_alpha, ba_t, dt * 8.0)
        targets = self._bar_targets()
        far = 0.0
        for i in range(3):
            cur = self._bars[i]
            self._bars[i] = ease_to(cur, targets[i], dt * (18.0 if targets[i] > cur else 8.0))
            far = max(far, abs(self._bars[i] - targets[i]))
        self.update()

        if st in ("idle", "paused"):
            hl_t = 0.0 if st == "idle" else 1.0
            if (abs(self._hl - hl_t) < 0.003 and self._press < 0.003 and not self._pressing
                    and self._pop < 0.003 and far < 0.05 and abs(self._bar_alpha - ba_t) < 0.01):
                self._timer.stop()

    # -- input
    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            self._pressing = True
            self._ensure_timer()
            event.accept()
            return
        super().mousePressEvent(event)

    def mouseReleaseEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            was = self._pressing
            self._pressing = False
            self._ensure_timer()
            if was and self.rect().contains(event.position().toPoint()):
                self.clicked.emit()
            event.accept()
            return
        super().mouseReleaseEvent(event)

    def enterEvent(self, event):
        self._hover = True
        self.update()
        super().enterEvent(event)

    def leaveEvent(self, event):
        self._hover = False
        if self._pressing:
            self._pressing = False
            self._ensure_timer()
        self.update()
        super().leaveEvent(event)

    # -- paint
    def paintEvent(self, event):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        r = QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5)
        rad = 14.0
        hl, hv = self._hl, (1.0 if self._hover else 0.0)
        gmix = max(0.85 * hv, hl)

        s = 1.0 - 0.018 * self._press
        if s != 1.0:
            c = r.center()
            p.translate(c)
            p.scale(s, s)
            p.translate(-c)

        fill = QLinearGradient(r.topLeft(), r.bottomLeft())
        fill.setColorAt(0.0, QColor(255, 255, 255, int(22 + 12 * hv + 8 * hl)))
        fill.setColorAt(1.0, QColor(255, 255, 255, int(8 + 6 * hv + 4 * hl)))
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QBrush(fill))
        p.drawRoundedRect(r, rad, rad)

        if hl > 0.004:
            # highlight sweeps in from the left as the row becomes active
            g = QLinearGradient(r.topLeft(), r.topRight())
            g.setColorAt(0.0, green(int(84 * hl)))
            g.setColorAt(clamp01(0.2 + 0.8 * hl), green(int(26 * hl)))
            g.setColorAt(1.0, green(int(6 * hl)))
            p.setBrush(QBrush(g))
            p.drawRoundedRect(r, rad, rad)

        border = QLinearGradient(r.topLeft(), r.bottomLeft())
        border.setColorAt(0.0, QColor(255, 255, 255, int(64 * (1.0 - 0.8 * gmix))))
        border.setColorAt(1.0, QColor(255, 255, 255, int(16 * (1.0 - 0.8 * gmix))))
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.setPen(QPen(QBrush(border), 1.0))
        p.drawRoundedRect(r, rad, rad)
        if gmix > 0.01:
            gb = QLinearGradient(r.topLeft(), r.bottomLeft())
            gb.setColorAt(0.0, green(int(170 * gmix)))
            gb.setColorAt(1.0, green(int(80 * gmix)))
            p.setPen(QPen(QBrush(gb), 1.0))
            p.drawRoundedRect(r, rad, rad)
        if hl > 0.01:
            for i in (1, 2, 3):
                p.setPen(QPen(green(int(16 * hl / i)), 1.0))
                p.drawRoundedRect(r.adjusted(i * 1.4, i * 1.4, -i * 1.4, -i * 1.4), rad - i, rad - i)

        # rank badge (gentle pop when the row becomes active)
        bs = 1.0 + 0.14 * math.sin(math.pi * (1.0 - clamp01(self._pop)))
        bc = QPointF(r.left() + 25, r.center().y())
        p.setPen(QPen(green(int(150 + 60 * hl)), 1.0))
        p.setBrush(green(int(58 + 80 * hl)))
        p.drawEllipse(bc, 12.0 * bs, 12.0 * bs)
        fm = QFontMetricsF(self._f_rank)
        draw_text(p, self._rank, self._f_rank, MINT, bc.x() - fm.horizontalAdvance(self._rank) / 2, bc.y())

        # text block (title eases from white to a faint mint when active)
        x = r.left() + 49 + 1.5 * hl
        right_reserve = 12.0 + 34.0 * clamp01(self._bar_alpha * 2.0)
        avail = max(20.0, r.right() - right_reserve - x)
        top = r.top()

        def elide(text, font):
            return QFontMetricsF(font).elidedText(text, Qt.TextElideMode.ElideRight, avail)

        title_col = QColor(int(lerp(255, 205, hl)), 255, int(lerp(255, 230, hl)))
        draw_text(p, elide(self._song, self._f_song), self._f_song, title_col, x, top + 16)
        draw_text(p, elide(self._artist, self._f_artist), self._f_artist, QColor(255, 255, 255, int(165 + 40 * hl)), x, top + 33)
        if self._meta:
            draw_text(p, elide(self._meta, self._f_meta), self._f_meta, QColor(140, 255, 185, 200), x, top + 48)

        # Apple-Music-style 3-bar indicator, driven by the real audio levels
        if self._bar_alpha > 0.02:
            cx = r.right() - 23.0
            base_y = r.center().y() + 11.0
            bar_w, gap = 3.0, 3.0
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(QBrush(green(int(225 * self._bar_alpha))))
            start_x = cx - (3 * bar_w + 2 * gap) / 2
            for i, h in enumerate(self._bars):
                p.drawRoundedRect(QRectF(start_x + i * (bar_w + gap), base_y - h, bar_w, h), 1.5, 1.5)


PANEL_QSS = """
QLabel#fieldLabel {
    color: rgba(255, 255, 255, 150);
    font-family: "Segoe UI";
    font-size: 9pt;
    background: transparent;
}
QLabel#resultTitle {
    color: rgba(255, 255, 255, 215);
    font-family: "Segoe UI";
    font-size: 9pt;
    font-weight: 600;
    background: transparent;
}
QLabel#resultMsg {
    color: rgba(255, 255, 255, 170);
    font-family: "Segoe UI";
    font-size: 9.5pt;
    background: transparent;
}
QPushButton#ghost {
    background: transparent;
    color: rgba(255, 255, 255, 140);
    border: none;
    border-radius: 12px;
    font-family: "Segoe UI";
    font-size: 9pt;
}
QPushButton#ghost:hover {
    color: white;
    background: rgba(255, 255, 255, 18);
}
QScrollArea#results {
    background: transparent;
    border: none;
}
QScrollArea#results > QWidget > QWidget {
    background: transparent;
}
QScrollBar:vertical {
    background: transparent;
    width: 6px;
    margin: 2px 0 2px 0;
}
QScrollBar::handle:vertical {
    background: rgba(255, 255, 255, 60);
    border-radius: 3px;
    min-height: 24px;
}
QScrollBar::handle:vertical:hover {
    background: rgba(29, 185, 84, 170);
}
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {
    height: 0px;
}
QScrollBar::add-page:vertical, QScrollBar::sub-page:vertical {
    background: transparent;
}
"""

MENU_QSS = """
QMenu {
    background-color: rgba(18, 22, 21, 240);
    border: 1px solid rgba(255, 255, 255, 50);
    border-radius: 14px;
    padding: 6px;
}
QMenu::item {
    color: white;
    font-family: "Segoe UI";
    font-size: 9.5pt;
    padding: 8px 22px;
    border-radius: 9px;
}
QMenu::item:selected {
    background: rgba(29, 185, 84, 80);
}
"""


# --------------------------------------------------------------------------
# The widget
# --------------------------------------------------------------------------


class AudiusPlayer(QObject):
    """GUI-thread Audius media player. Network resolution happens in a worker."""

    status = Signal(str)
    playing = Signal(dict)
    stopped = Signal()
    position_changed = Signal(int)
    duration_changed = Signal(int)
    state_changed = Signal(object)
    media_status_changed = Signal(object)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.audio = QAudioOutput(self)
        self.audio.setVolume(0.85)
        self.player = QMediaPlayer(self)
        self.player.setAudioOutput(self.audio)
        self.player.errorOccurred.connect(self._on_error)
        self.player.positionChanged.connect(
            lambda position: self.position_changed.emit(int(position))
        )
        self.player.durationChanged.connect(
            lambda duration: self.duration_changed.emit(int(duration))
        )
        self.player.playbackStateChanged.connect(lambda state: self.state_changed.emit(state))
        self.player.mediaStatusChanged.connect(lambda status: self.media_status_changed.emit(status))
        self._current = None

        # Never allowed to break playback: construction failures are contained.
        self.analyzer = AudioAnalyzer(self.player, self)

    def play_url(self, final_url, info):
        self._current = dict(info)
        self.analyzer.reset()
        self.player.stop()
        self.player.setSource(QUrl.fromUserInput(final_url))
        self.player.play()
        self.status.emit(f"Playing \u2022 {info['song']}")
        self.playing.emit(self._current.copy())

    def toggle(self):
        st = self.player.playbackState()
        if st == QMediaPlayer.PlaybackState.PlayingState:
            self.player.pause()
        elif self._current is not None:
            self.player.play()

    def seek(self, ms):
        self.analyzer.reset()
        self.player.setPosition(max(0, int(ms)))

    def stop(self):
        self.player.stop()
        self._current = None
        self.stopped.emit()

    def shutdown(self):
        self.analyzer.detach()
        self.stop()

    def _on_error(self, error, error_string):
        if error != QMediaPlayer.Error.NoError:
            print("[VibeTune] Media player error:", error_string)
            self.status.emit("Playback failed \u2014 try another song")


class VibeTuneWidget(QWidget):
    # Mouse state machine
    IDLE, PRESSED, DRAGGING = range(3)

    # Queued into the persistent Audius resolver thread.
    audio_resolve_requested = Signal(str, str, int)

    def __init__(self):
        super().__init__()

        # --- Window: frameless, always on top, tool window (no taskbar button) ---
        self.setWindowFlags(
            Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowStaysOnTopHint
            | Qt.WindowType.Tool
        )
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setWindowTitle("VibeTune")

        # --- State ---
        self._expanded = False
        self._morph = 0.0            # 0 = collapsed, 1 = expanded (animated)
        self._hover = False
        self._effect = None          # opacity effect for the controls panel
        self._geom_cache = None

        # API request state
        self._busy = False
        self._closing = False
        self._thread = None
        self._worker = None

        # Audius playback
        self._audius = AudiusPlayer(self)
        self._audius.status.connect(self._on_playback_status)
        self._audius.playing.connect(self._on_audio_playing)
        self._audius.stopped.connect(self._on_audio_stopped)
        self._audius.state_changed.connect(self._on_player_state)
        self._audio_request_id = 0

        # Row / track tracking
        self._active_row = None      # row bound to the track in the player
        self._active_key = None      # (song, artist) of that track
        self._pending_row = None     # row waiting for its stream to resolve
        self._pending_key = None

        # Shared, smoothed audio values for all visuals
        self._vis = VisualizerState()

        # One persistent resolver thread.  Keeping the event loop alive
        # eliminates the per-click QThread lifetime race.
        self._audio_thread = QThread(self)
        self._audio_worker = AudiusResolverWorker()
        self._audio_worker.moveToThread(self._audio_thread)
        self.audio_resolve_requested.connect(
            self._audio_worker.resolve,
            Qt.ConnectionType.QueuedConnection,
        )
        self._audio_worker.succeeded.connect(self._on_audius_resolved)
        self._audio_worker.failed.connect(self._on_audius_failed)
        self._audio_thread.start()

        # Static-layer cache (glass body + content) used while the card is settled
        self._cache_key = None
        self._bg_pm = None
        self._fg_pm = None

        # Click-vs-drag
        self._mouse_state = self.IDLE
        self._press_global = QPointF()
        self._last_global = QPointF()
        self._press_local = QPointF()

        # Floating
        self._phase = 0.0
        self._float_offset = 0.0
        self._clock = QElapsedTimer()
        self._clock.start()
        self._last_ms = 0
        self._wave_last_ms = 0

        # Base positions (window top-left, WITHOUT the float offset).
        # Both are shifted together when the user drags, so the widget keeps
        # floating around wherever it was dropped.
        screen = QGuiApplication.primaryScreen().availableGeometry()
        start_w = COLL_W + 2 * MARGIN
        self._collapsed_anchor = QPointF(screen.right() - start_w - 60, screen.top() + 140)
        self._expanded_anchor = QPointF(self._collapsed_anchor)

        # Fonts are created once, not per paint
        self._f_title_small = make_font(12.5, QFont.Weight.DemiBold, 0.3)
        self._f_sub_small = make_font(8.5)
        self._f_title_big = make_font(15.0, QFont.Weight.DemiBold, 0.3)
        self._f_sub_big = make_font(9.0)

        self.build_ui()

        # Timeline wiring (real QMediaPlayer values)
        self._audius.position_changed.connect(self.now_playing.set_position)
        self._audius.duration_changed.connect(self.now_playing.set_duration)

        # Morph animation (collapsed <-> expanded)
        self._anim = QPropertyAnimation(self, b"morph", self)
        self._anim.setEasingCurve(QEasingCurve.Type.OutCubic)
        self._anim.finished.connect(self._on_anim_finished)

        # Floating timer (vertical only, lightweight)
        self.float_timer = QTimer(self)
        self.float_timer.setTimerType(Qt.TimerType.PreciseTimer)
        self.float_timer.setInterval(FLOAT_INTERVAL_MS)
        self.float_timer.timeout.connect(self.update_float_animation)

        # Flowing-music timer: also advances the visualizer + timeline smoothing
        self.wave_timer = QTimer(self)
        self.wave_timer.setInterval(WAVE_INTERVAL_MS)
        self.wave_timer.timeout.connect(self._tick_wave)

        self._apply_geometry()
        self._last_ms = self._clock.elapsed()
        self._wave_last_ms = self._last_ms
        self.float_timer.start()
        self.wave_timer.start()
        self._update_cursor()

    # ------------------------------------------------------------------ UI

    def build_ui(self):
        """Controls panel for the expanded state. Decoration is painted in paintEvent."""
        self.panel = QWidget(self)
        self.panel.setFixedSize(PANEL_W, PANEL_H)
        self.panel.move(MARGIN + PAD, MARGIN + PANEL_TOP)
        self.panel.setStyleSheet(PANEL_QSS)

        # Page 1: the original controls (same layout as before, just hosted in a child widget)
        self.controls = QWidget(self.panel)
        self.controls.setGeometry(0, 0, PANEL_W, PANEL_H)

        lay = QVBoxLayout(self.controls)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(0)

        self.mood_box = self._add_field(lay, "Mood", MOODS, 0)         # Happy
        self.genre_box = self._add_field(lay, "Genre", GENRES, 0)      # Pop
        self.energy_box = self._add_field(lay, "Energy", ENERGIES, 1)  # Medium

        lay.addSpacing(6)
        self.discover_btn = GlassButton("Discover Music")
        self.discover_btn.setFixedHeight(44)
        self.discover_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.discover_btn.clicked.connect(self.on_discover)
        lay.addWidget(self.discover_btn)

        lay.addStretch(1)
        self.close_btn = QPushButton("Close")
        self.close_btn.setObjectName("ghost")
        self.close_btn.setFixedHeight(26)
        self.close_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.close_btn.clicked.connect(self.collapse)
        lay.addWidget(self.close_btn)

        # Page 2: recommendations (same footprint as the controls, shown in their place)
        self.results_view = QWidget(self.panel)
        self.results_view.setGeometry(0, 0, PANEL_W, PANEL_H)
        rv = QVBoxLayout(self.results_view)
        rv.setContentsMargins(0, 0, 0, 0)
        rv.setSpacing(8)

        head = QHBoxLayout()
        head.setContentsMargins(0, 0, 0, 0)
        head.setSpacing(6)
        self.results_title = QLabel("")
        self.results_title.setObjectName("resultTitle")
        head.addWidget(self.results_title, 1)
        self.back_btn = QPushButton("\u2039 Back")
        self.back_btn.setObjectName("ghost")
        self.back_btn.setFixedSize(64, 26)
        self.back_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.back_btn.clicked.connect(self._show_controls)
        head.addWidget(self.back_btn, 0)
        rv.addLayout(head)

        # Now-playing card: collapsed to zero height until a track starts
        self.now_playing = NowPlayingBar()
        self.now_playing.setFixedHeight(0)
        self.now_playing.hide()
        self.now_playing.seek_requested.connect(self._audius.seek)
        self.now_playing.toggle_requested.connect(self._audius.toggle)
        rv.addWidget(self.now_playing)
        self._np_shown = False
        self._np_anim = QVariantAnimation(self)
        self._np_anim.setDuration(320)
        self._np_anim.setEasingCurve(QEasingCurve.Type.OutCubic)
        self._np_anim.valueChanged.connect(lambda v: self.now_playing.setFixedHeight(int(v)))

        self.results_scroll = QScrollArea()
        self.results_scroll.setObjectName("results")
        self.results_scroll.setFrameShape(QScrollArea.Shape.NoFrame)
        self.results_scroll.setWidgetResizable(True)
        self.results_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.results_scroll.viewport().setAutoFillBackground(False)
        self.results_content = QWidget()
        self.results_content.setAutoFillBackground(False)
        self.results_layout = QVBoxLayout(self.results_content)
        self.results_layout.setContentsMargins(0, 0, 2, 0)
        self.results_layout.setSpacing(6)
        self.results_scroll.setWidget(self.results_content)
        rv.addWidget(self.results_scroll, 1)

        self.results_view.hide()
        self.panel.hide()

    def _add_field(self, layout, label_text, items, default):
        label = QLabel(label_text)
        label.setObjectName("fieldLabel")
        combo = GlassCombo(items, default)
        layout.addWidget(label)
        layout.addSpacing(5)
        layout.addWidget(combo)
        layout.addSpacing(10)
        return combo

    def _set_now_playing_visible(self, show):
        if show == self._np_shown:
            return
        self._np_shown = show
        self._np_anim.stop()
        try:
            self._np_anim.finished.disconnect(self._hide_np_if_empty)
        except (RuntimeError, TypeError):
            pass
        if show:
            self.now_playing.show()
        else:
            self._np_anim.finished.connect(self._hide_np_if_empty)
        self._np_anim.setStartValue(float(self.now_playing.height()))
        self._np_anim.setEndValue(float(NP_H if show else 0))
        self._np_anim.start()

    def _hide_np_if_empty(self):
        if not self._np_shown:
            self.now_playing.hide()

    # --------------------------------------------------------- API / Discover

    def on_discover(self):
        if self._busy or self._closing:
            return  # exactly one request at a time

        mood = self.mood_box.currentText().strip().lower()
        genre = self.genre_box.currentText().strip().lower()
        energy = self.energy_box.currentText().strip().lower()
        payload = {
            "mood": mood,
            "genre": genre,
            "energy": energy,
            "limit": RESULT_LIMIT,
        }
        self._query_label = " \u00b7 ".join(
            (self.mood_box.currentText(), self.genre_box.currentText(), self.energy_box.currentText())
        )
        print("[VibeTune] Discover requested:", payload)

        self._set_loading(True)

        # QThread + worker object: the worker is moved to the thread, so run()
        # (the blocking HTTP call) never executes on the GUI thread.
        self._thread = QThread()
        self._worker = RecommendWorker(payload)
        self._worker.moveToThread(self._thread)
        self._thread.started.connect(self._worker.run)
        self._worker.succeeded.connect(self._on_results)   # delivered on the GUI thread
        self._worker.failed.connect(self._on_failed)       # delivered on the GUI thread
        self._worker.finished.connect(self._thread.quit)
        self._worker.finished.connect(self._worker.deleteLater)
        self._thread.finished.connect(self._thread.deleteLater)
        self._thread.finished.connect(self._on_thread_finished)
        self._thread.start()

    def _set_loading(self, loading):
        self._busy = loading
        self.discover_btn.setEnabled(not loading)
        self.discover_btn.setText("Discovering..." if loading else "Discover Music")
        self.discover_btn.update()

    def _on_thread_finished(self):
        self._thread = None
        self._worker = None

    def _on_results(self, recs):
        if self._closing:
            return
        self._set_loading(False)
        print(f"[VibeTune] Received {len(recs)} recommendations")
        self._show_results(recs)

    def _on_failed(self, message):
        if self._closing:
            return
        self._set_loading(False)
        self._show_message(message)

    def has_active_request(self):
        try:
            return self._thread is not None and self._thread.isRunning()
        except RuntimeError:
            return False

    def shutdown(self):
        """Called when the app quits: stop playback and cleanly close workers."""
        if self._closing:
            return
        self._closing = True
        self._audio_request_id += 1

        for t in (self.float_timer, self.wave_timer):
            try:
                t.stop()
            except RuntimeError:
                pass
        try:
            self._np_anim.stop()
        except RuntimeError:
            pass

        # Detaches the audio analyzer, then stops the QMediaPlayer.
        try:
            self._audius.shutdown()
        except Exception:
            pass

        # The Audius worker is persistent and idle between requests, so its
        # event loop can be stopped cleanly without destroying a running thread.
        try:
            if self._audio_thread is not None:
                self._audio_thread.quit()
                self._audio_thread.wait(2000)
        except RuntimeError:
            pass

        # Recommendation request thread (network call may still be active).
        try:
            if self._thread is not None:
                self._thread.quit()
                self._thread.wait(150)
        except RuntimeError:
            pass

    # ----------------------------------------------------- results rendering

    def _clear_results(self):
        # Rows are about to be destroyed: drop every reference to them.
        self._active_row = None
        self._pending_row = None
        while self.results_layout.count():
            item = self.results_layout.takeAt(0)
            w = item.widget()
            if w is not None:
                w.setParent(None)
                w.deleteLater()

    def _switch_to_results(self):
        self.controls.hide()
        self.results_view.show()
        self.results_view.raise_()
        self.results_scroll.verticalScrollBar().setValue(0)

    def _show_controls(self):
        self.results_view.hide()
        self.controls.show()

    @staticmethod
    def _format_meta(genre, emotion):
        genres = [g.strip() for g in str(genre or "").split(",") if g.strip()][:3]
        parts = [", ".join(genres)] if genres else []
        emo = str(emotion or "").strip()
        if emo:
            parts.append(emo)
        return " \u2022 ".join(parts)

    def _player_row_state(self):
        st = self._audius.player.playbackState()
        if st == QMediaPlayer.PlaybackState.PlayingState:
            return "playing"
        if st == QMediaPlayer.PlaybackState.PausedState:
            return "paused"
        return "idle"

    def _show_results(self, recs):
        self._clear_results()
        self.results_title.setText(getattr(self, "_query_label", "Recommendations"))
        for i, rec in enumerate(recs[:RESULT_LIMIT], start=1):
            rank = rec.get("rank", i)
            song = str(rec.get("song") or "Unknown title")
            artist = str(rec.get("artist") or "Unknown artist")
            meta = self._format_meta(rec.get("genre"), rec.get("emotion"))
            row = ResultRow(rank, song, artist, meta)
            row.vis = self._vis
            row.clicked.connect(lambda rec=rec, row=row: self._play_recommendation(rec, row))
            self.results_layout.addWidget(row)

            # Re-bind a track that is still playing / loading from a previous list.
            key = (str(rec.get("song") or "").strip(), str(rec.get("artist") or "").strip())
            if self._pending_key is not None and key == self._pending_key and self._pending_row is None:
                self._pending_row = row
                row.set_state("loading", instant=True)
            elif self._active_key is not None and key == self._active_key and self._active_row is None:
                self._active_row = row
                row.set_state(self._player_row_state(), instant=True)

        self.results_layout.addSpacing(4)
        self.playback_status = QLabel("Click a song to play")
        self.playback_status.setObjectName("resultMsg")
        self.playback_status.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.results_layout.addWidget(self.playback_status)
        self.results_layout.addStretch(1)
        self._switch_to_results()

    def _play_recommendation(self, rec, row):
        if self._closing:
            return

        artist = str(rec.get("artist") or "").strip()
        song = str(rec.get("song") or "").strip()
        if not artist or not song:
            self._on_playback_status(MSG_PLAYBACK_UNAVAILABLE)
            return

        # Clicking the song that is already active toggles pause / resume.
        if row is self._active_row and self._pending_row is None and row.state in ("playing", "paused"):
            self._audius.toggle()
            return

        # A previously pending (not yet playing) row returns to normal.
        if (self._pending_row is not None and self._pending_row is not row
                and self._pending_row is not self._active_row):
            self._pending_row.set_state("idle")

        self._pending_row = row
        self._pending_key = (song, artist)
        row.set_state("loading")

        self._audio_request_id += 1
        request_id = self._audio_request_id

        self._on_playback_status(f"Finding {song}...")
        self.audio_resolve_requested.emit(artist, song, request_id)

    @Slot(str, dict, int)
    def _on_audius_resolved(self, url, info, request_id):
        if self._closing or request_id != self._audio_request_id:
            return
        self._audius.play_url(url, info)

    @Slot(str, int)
    def _on_audius_failed(self, message, request_id):
        if self._closing or request_id != self._audio_request_id:
            return
        pending = self._pending_row
        if pending is not None:
            pending.set_state(self._player_row_state() if pending is self._active_row else "idle")
        self._pending_row = None
        self._pending_key = None
        self._on_playback_status(message)

    @Slot(dict)
    def _on_audio_playing(self, info):
        # The new stream just started: hand the active state from the old row to the new one.
        if self._pending_key is not None:
            new_row = self._pending_row
            old_row = self._active_row
            if old_row is not None and old_row is not new_row:
                old_row.set_state("idle")
            self._active_row = new_row
            self._active_key = self._pending_key
            self._pending_row = None
            self._pending_key = None

        song = str(info.get("requested_song") or info.get("song") or "Music")
        artist = str(info.get("requested_artist") or info.get("artist") or "")
        self.now_playing.begin_track(song, artist)
        self.now_playing.set_playing(True)
        self._set_now_playing_visible(True)

        if self._active_row is not None:
            self._active_row.set_state("playing")
        self._on_playback_status(f"Playing \u2022 {info.get('song', 'Music')}")

    @Slot(object)
    def _on_player_state(self, state):
        """Real QMediaPlayer playback state -> row indicator + player card."""
        if self._closing:
            return
        row = self._active_row
        if state == QMediaPlayer.PlaybackState.PlayingState:
            if row is not None:
                row.set_state("playing")
            self.now_playing.set_playing(True)
        elif state == QMediaPlayer.PlaybackState.PausedState:
            if row is not None:
                row.set_state("paused")
            self.now_playing.set_playing(False)
        else:
            if row is not None:
                row.set_state("idle")
            self.now_playing.set_playing(False)
            self.now_playing.reset_position()

    @Slot()
    def _on_audio_stopped(self):
        if self._active_row is not None:
            self._active_row.set_state("idle")

    @Slot(str)
    def _on_playback_status(self, text):
        label = getattr(self, "playback_status", None)
        if label is None:
            return
        try:
            label.setText(text)
        except RuntimeError:
            pass  # label belonged to a results page that was already replaced

    def _show_message(self, text):
        self._clear_results()
        self.results_title.setText(getattr(self, "_query_label", ""))
        self.results_layout.addSpacing(48)
        msg = QLabel(text)
        msg.setObjectName("resultMsg")
        msg.setAlignment(Qt.AlignmentFlag.AlignCenter)
        msg.setWordWrap(True)
        self.results_layout.addWidget(msg)
        self.results_layout.addStretch(1)
        self._switch_to_results()

    # ------------------------------------------------------- morph property

    def _get_morph(self):
        return self._morph

    def _set_morph(self, value):
        self._morph = value
        self._apply_geometry()
        self._sync_panel()
        self.update()

    morph = Property(float, _get_morph, _set_morph)

    def _sync_panel(self):
        """Show/fade the controls only once the card is mostly grown."""
        a = clamp01((self._morph - 0.6) / 0.4)
        if a > 0.0:
            self._set_panel_opacity(a)
        show = a > 0.0
        if show == self.panel.isHidden():
            self.panel.setVisible(show)

    def _set_panel_opacity(self, a):
        # Remove the effect when fully opaque (cheaper, crisper text).
        if a >= 0.999:
            if self._effect is not None:
                self.panel.setGraphicsEffect(None)
                self._effect = None
        else:
            if self._effect is None:
                self._effect = QGraphicsOpacityEffect(self.panel)
                self.panel.setGraphicsEffect(self._effect)
            self._effect.setOpacity(a)

    # ------------------------------------------------------ geometry / float

    def _apply_geometry(self):
        """Window = interpolated size + interpolated base position + float offset."""
        m = self._morph
        w = round(lerp(COLL_W, EXP_W, m)) + 2 * MARGIN
        h = round(lerp(COLL_H, EXP_H, m)) + 2 * MARGIN
        ax = lerp(self._collapsed_anchor.x(), self._expanded_anchor.x(), m)
        ay = lerp(self._collapsed_anchor.y(), self._expanded_anchor.y(), m)
        # The float fades out as the widget expands so controls stay steady.
        ay += self._float_offset * (1.0 - m)
        key = (round(ax), round(ay), w, h)
        if key != self._geom_cache:   # only touch the window when something changed
            self._geom_cache = key
            self.setGeometry(*key)

    def update_float_animation(self):
        """Vertical-only sinusoidal float. Frame-rate independent, paused while dragging."""
        now = self._clock.elapsed()
        dt = min((now - self._last_ms) / 1000.0, 0.1)
        self._last_ms = now
        if self._mouse_state == self.DRAGGING:
            return  # never fight the user's hand
        self._phase = (self._phase + dt * 2.0 * math.pi / FLOAT_PERIOD) % (2.0 * math.pi)
        self._float_offset = FLOAT_AMPLITUDE * math.sin(self._phase)
        self._apply_geometry()

    def _update_visualizer(self, dt):
        """Feed the smoothed visualizer from the REAL player state + analyzed audio."""
        player = self._audius.player
        state = player.playbackState()
        if state == QMediaPlayer.PlaybackState.PlayingState:
            status = player.mediaStatus()
            waiting = status in (
                QMediaPlayer.MediaStatus.LoadingMedia,
                QMediaPlayer.MediaStatus.StalledMedia,
            )
            mode = "paused" if waiting else "playing"
        elif state == QMediaPlayer.PlaybackState.PausedState:
            mode = "paused"
        else:
            mode = "idle"
        sample = None
        if mode == "playing":
            sample = self._audius.analyzer.sample(player.position(), True)
        self._vis.update(dt, mode, sample)

    def _tick_wave(self):
        """One shared ~30 FPS tick: visualizer state, timeline easing and the animated band repaint."""
        if self._closing:
            return
        now = self._clock.elapsed()
        dt = min(max((now - self._wave_last_ms) / 1000.0, 0.0), 0.1)
        self._wave_last_ms = now

        self._update_visualizer(dt)
        if self.now_playing.isVisible():
            self.now_playing.tick(dt, self._vis)

        if self._anim.state() == QAbstractAnimation.State.Running:
            return  # the morph already repaints every frame
        if self._morph > 0.5:
            # header band only - stays clear of the child controls, so they are not repainted
            self.update(QRect(MARGIN, MARGIN, EXP_W, PANEL_TOP))
        elif self._vis.activity > 0.005:
            # the reactive ring reaches above the old wave band
            self.update(QRect(MARGIN, MARGIN, COLL_W, COLL_H))
        else:
            self.update(QRect(MARGIN, MARGIN + 28, COLL_W, COLL_H - 28))

    def _clamped(self, anchor, w, h):
        """Keep the card inside the available area of the screen it is on."""
        center = QPoint(int(anchor.x() + w / 2), int(anchor.y() + h / 2))
        screen = (
            QGuiApplication.screenAt(center)
            or self.screen()
            or QGuiApplication.primaryScreen()
        )
        a = screen.availableGeometry()
        x = min(max(anchor.x(), a.left() - MARGIN + 4), a.right() + 1 - w + MARGIN - 4)
        y = min(max(anchor.y(), a.top() - MARGIN + 4), a.bottom() + 1 - h + MARGIN - 4)
        return QPointF(x, y)

    def _keep_on_screen(self):
        m = self._morph
        w = lerp(COLL_W, EXP_W, m) + 2 * MARGIN
        h = lerp(COLL_H, EXP_H, m) + 2 * MARGIN
        cur = QPointF(
            lerp(self._collapsed_anchor.x(), self._expanded_anchor.x(), m),
            lerp(self._collapsed_anchor.y(), self._expanded_anchor.y(), m),
        )
        fixed = self._clamped(cur, w, h)
        d = fixed - cur
        if d.x() != 0 or d.y() != 0:
            self._collapsed_anchor = self._collapsed_anchor + d
            self._expanded_anchor = self._expanded_anchor + d
            self._apply_geometry()

    # ------------------------------------------------------ expand / collapse

    def expand(self):
        if self._expanded:
            return
        self._expanded = True
        if self._morph <= 0.001:
            # Where should the big card sit so it stays fully on screen?
            self._expanded_anchor = self._clamped(
                self._collapsed_anchor, EXP_W + 2 * MARGIN, EXP_H + 2 * MARGIN
            )
        self._ensure_float_timer()
        self._animate_to(1.0)
        self._update_cursor()

    def collapse(self):
        if not self._expanded:
            return
        self._expanded = False
        self.panel.clearFocus()
        self._ensure_float_timer()
        self._animate_to(0.0)
        self._update_cursor()

    def _ensure_float_timer(self):
        if not self.float_timer.isActive():
            self._last_ms = self._clock.elapsed()
            self.float_timer.start()

    def _animate_to(self, target):
        self._anim.stop()
        cur = self._morph
        dist = abs(target - cur)
        base = 320 if target > cur else 260
        self._anim.setStartValue(cur)
        self._anim.setEndValue(target)
        self._anim.setDuration(int(max(120, base * dist)))
        self._anim.start()

    def _on_anim_finished(self):
        # Fully expanded: the float is faded out, so the timer can rest.
        if self._expanded and self._mouse_state != self.DRAGGING:
            self.float_timer.stop()

    def _update_cursor(self):
        if self._mouse_state == self.DRAGGING:
            self.setCursor(Qt.CursorShape.ClosedHandCursor)
        elif self._expanded:
            self.setCursor(Qt.CursorShape.ArrowCursor)
        else:
            self.setCursor(Qt.CursorShape.PointingHandCursor)

    # ----------------------------------------------------------------- paint

    def _radius(self):
        return lerp(32.0, 28.0, self._morph)

    def _card_rect(self):
        return QRectF(MARGIN, MARGIN, self.width() - 2 * MARGIN, self.height() - 2 * MARGIN)

    def paintEvent(self, event):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        card = self._card_rect()

        settled = self._morph < 0.001 or self._morph > 0.999
        if settled:
            # Fast path: static glass + content come from cached pixmaps;
            # only the flowing waves/equalizer/ring are painted live in between.
            bg, fg = self._get_cache()
            p.drawPixmap(0, 0, bg)
            self.paint_flow(p, card)
            p.drawPixmap(0, 0, fg)
        else:
            self.paint_background(p, card)
            self.paint_flow(p, card)
            self.paint_foreground(p, card)

    def _get_cache(self):
        dpr = self.devicePixelRatioF()
        key = (self.width(), self.height(), dpr, self._hover, self._morph > 0.5)
        if key != self._cache_key:
            self._cache_key = key
            card = self._card_rect()
            self._bg_pm = self._render_layer(lambda q: self.paint_background(q, card), dpr)
            self._fg_pm = self._render_layer(lambda q: self.paint_foreground(q, card), dpr)
        return self._bg_pm, self._fg_pm

    def _render_layer(self, fn, dpr):
        pm = QPixmap(int(self.width() * dpr), int(self.height() * dpr))
        pm.setDevicePixelRatio(dpr)
        pm.fill(Qt.GlobalColor.transparent)
        q = QPainter(pm)
        q.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        fn(q)
        q.end()
        return pm

    def paint_background(self, p, card):
        """Glass body (shadow, glow, frosted gradients). Static -> cached."""
        t = self._morph
        center = card.topLeft() + QPointF(lerp(46, PAD + 16, t), lerp(COLL_H / 2, PAD + 24, t))
        draw_glass(
            p, card, self._radius(), hover=self._hover,
            amb_center=center, amb_radius=lerp(100, 190, t), amb_alpha=int(lerp(54, 38, t)),
        )

    def paint_foreground(self, p, card):
        """Logo, text and the glass rim. Static -> cached."""
        a_c = clamp01(1.0 - self._morph / 0.35)
        if a_c > 0.0:
            p.setOpacity(a_c)
            self.paint_collapsed(p, card)
        a_e = clamp01((self._morph - 0.6) / 0.4)
        if a_e > 0.0:
            p.setOpacity(a_e)
            self.paint_expanded(p, card)
        p.setOpacity(1.0)
        draw_rim(p, card, self._radius(), self._hover)

    def paint_flow(self, p, card):
        """Flowing music animation: layered waves + equalizer bars + audio-reactive ring (live)."""
        t = self._clock.elapsed() / 1000.0
        a_c = clamp01(1.0 - self._morph / 0.35)
        a_e = clamp01((self._morph - 0.6) / 0.4)
        vis = self._vis
        drive = vis.energy * vis.activity
        boost = 1.0 + 0.5 * drive + 0.25 * vis.punch * vis.activity

        if a_c > 0.0:
            p.save()
            clip = QPainterPath()
            clip.addRoundedRect(card, self._radius(), self._radius())
            p.setClipPath(clip)
            p.setOpacity(a_c)
            draw_wave_ribbon(p, card.left(), card.right(), card.top() + 77, 9.0 * min(boost, 1.5), card.bottom(), t)
            draw_eq_bars(p, card.right() - 18 - 21, card.center().y(), t, max_h=24.0 * (1.0 + 0.3 * drive))
            draw_reactive_ring(p, QPointF(card.left() + 46, card.center().y()), 27.5, vis, 1.0)
            p.restore()

        if a_e > 0.0:
            p.save()
            p.setOpacity(a_e)
            draw_wave_ribbon(
                p, card.left() + 1, card.right() - 1, card.top() + 77, 8.0 * min(boost, 1.4),
                card.top() + HEADER_LINE, t
            )
            draw_eq_bars(p, card.right() - PAD - 21, card.top() + PAD + 16, t, max_h=20.0 * (1.0 + 0.3 * drive))
            draw_reactive_ring(p, QPointF(card.left() + PAD + 16, card.top() + PAD + 24), 21.5, vis, 0.75)
            p.restore()

    def paint_collapsed(self, p, card):
        cx, cy = card.left() + 46, card.center().y()
        paint_logo(p, QPointF(cx, cy), 19.0)
        tx = cx + 19 + 15
        draw_text(p, "VibeTune", self._f_title_small, QColor(255, 255, 255), tx, cy - 10)
        draw_text(p, "Click to discover", self._f_sub_small, QColor(255, 255, 255, 150), tx, cy + 12)

    def paint_expanded(self, p, card):
        left, top = card.left(), card.top()
        paint_logo(p, QPointF(left + PAD + 16, top + PAD + 24), 15.0)
        tx = left + PAD + 40
        draw_text(p, "VibeTune", self._f_title_big, QColor(255, 255, 255), tx, top + PAD + 14)
        draw_text(
            p, "Music that matches your vibe", self._f_sub_big, QColor(255, 255, 255, 150), tx, top + PAD + 38
        )
        # hairline divider under the flowing header band
        y = top + HEADER_LINE
        p.setPen(QPen(QColor(255, 255, 255, 22), 1.0))
        p.drawLine(QPointF(left + PAD, y), QPointF(card.right() - PAD, y))

    # ------------------------------------------------------- mouse handling

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.RightButton:
            self.show_context_menu(event.globalPosition().toPoint())
            event.accept()
            return
        if event.button() == Qt.MouseButton.LeftButton:
            # Do NOT expand here. Just remember where the press started.
            self._mouse_state = self.PRESSED
            self._press_global = event.globalPosition()
            self._last_global = event.globalPosition()
            self._press_local = event.position()
            event.accept()
            return
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event):
        if self._mouse_state == self.IDLE or not (event.buttons() & Qt.MouseButton.LeftButton):
            return
        g = event.globalPosition()

        if self._mouse_state == self.PRESSED:
            d = g - self._press_global
            if math.hypot(d.x(), d.y()) < DRAG_THRESHOLD:
                return  # still a potential click
            # Crossed the threshold: this is a drag from now on.
            self._mouse_state = self.DRAGGING
            self._last_global = self._press_global  # catch up so the grab point stays under the cursor
            self._update_cursor()

        # DRAGGING: move both base anchors by the mouse delta (X and Y).
        delta = g - self._last_global
        self._last_global = g
        self._collapsed_anchor = self._collapsed_anchor + delta
        self._expanded_anchor = self._expanded_anchor + delta
        self._apply_geometry()

    def mouseReleaseEvent(self, event):
        if event.button() != Qt.MouseButton.LeftButton:
            super().mouseReleaseEvent(event)
            return
        state = self._mouse_state
        self._mouse_state = self.IDLE

        if state == self.DRAGGING:
            # Drag ended: the current spot is the new floating base. No click.
            self._last_ms = self._clock.elapsed()
            self._keep_on_screen()
            self._update_cursor()
            if self._expanded and self._anim.state() != QAbstractAnimation.State.Running:
                self.float_timer.stop()
        elif state == self.PRESSED:
            self._handle_click(self._press_local)
        event.accept()

    def _handle_click(self, pos):
        if not self._expanded:
            self.expand()
        elif pos.y() < MARGIN + PANEL_TOP - 12:
            # Clicking the header of the open panel folds it back into the compact pill.
            self.collapse()

    def enterEvent(self, event):
        self._hover = True
        self.update()
        super().enterEvent(event)

    def leaveEvent(self, event):
        self._hover = False
        self.update()
        super().leaveEvent(event)

    def keyPressEvent(self, event):
        if event.key() == Qt.Key.Key_Escape and self._expanded:
            self.collapse()
        else:
            super().keyPressEvent(event)

    # ----------------------------------------------------------- context menu

    def show_context_menu(self, global_pos):
        menu = QMenu(self)
        menu.setWindowFlags(
            menu.windowFlags()
            | Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.NoDropShadowWindowHint
        )
        menu.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        menu.setStyleSheet(MENU_QSS)
        action = menu.addAction("Exit VibeTune")
        action.triggered.connect(QApplication.quit)
        menu.exec(global_pos)
        menu.deleteLater()


# --------------------------------------------------------------------------
# Entry point
# --------------------------------------------------------------------------


def main():
    app = QApplication(sys.argv)
    app.setStyle("Fusion")  # consistent base for our stylesheets
    app.setFont(QFont("Segoe UI", 9))

    widget = VibeTuneWidget()
    widget.show()
    app.aboutToQuit.connect(widget.shutdown)

    code = app.exec()

    # If the user quits while a recommendation request is still in flight, don't
    # wait for the network (and don't crash with "QThread destroyed while
    # running"): exit now.  (Audio/Audius threads are already cleanly stopped.)
    if widget.has_active_request():
        sys.stdout.flush()
        os._exit(code)
    sys.exit(code)


if __name__ == "__main__":
    main()
