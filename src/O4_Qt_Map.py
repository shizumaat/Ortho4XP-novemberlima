"""Live slippy-map widget for the Ortho4XP Qt UI.

Renders the currently selected imagery provider at the resolution of the
current zoom (Google-Maps style), draws the 1-degree tile grid, built-tile
overlays, the selection, and per-tile build-progress badges.

Scene coordinates are webmercator pixels at zoom SCENE_ZL, so
GEO.wgs84_to_pix(lat, lon, SCENE_ZL) maps directly into the scene.

Tile fetching reuses IMG.get_wmts_image() (the same code paths used for
building tiles), so every TMS/WMTS webmercator provider works unmodified.
Fetched tiles are cached on disk under Previews/livemap/<provider>/.
"""

import math
import os
import threading
import time
from collections import deque

import requests
from PIL import Image as PILImage
from PySide6.QtCore import (
    QObject,
    QPointF,
    QRectF,
    Qt,
    QTimer,
    Signal,
)
from PySide6.QtGui import (
    QBrush,
    QColor,
    QFont,
    QPainter,
    QPen,
    QPixmap,
)
from PySide6.QtWidgets import (
    QGraphicsPixmapItem,
    QGraphicsScene,
    QGraphicsView,
)

import O4_File_Names as FNAMES
import O4_Geo_Utils as GEO
import O4_Imagery_Utils as IMG

SCENE_ZL = 19  # scene units = webmercator pixels at this zoom level
WORLD = 2 ** SCENE_ZL * 256
MIN_ZOOM = 2.0
MAX_ITEMS = 900        # loaded tile pixmaps kept in the scene
BASE_ZL = 3            # world base layer, always resident (max 64 tiles)
FETCH_DEBOUNCE_MS = 250  # settle time before network fetches start
FETCH_WORKERS = 6      # concurrent tile downloads
LEVEL_TILE_CAP = 420   # skip a pyramid level if it needs more tiles than this

# Zoom-level colors, matching the legacy UI's zone color language.
ZL_COLORS = {
    11: "#5B8DD9", 12: "#5B8DD9", 13: "#5B8DD9", 14: "#5B8DD9",
    15: "#67D6E2", 16: "#4CAF6E", 17: "#E8C33C", 18: "#F2A85C", 19: "#EF7B6D",
}


BUILD_GRADE_ZL = 17  # tiles at or above this ZL are saved at high quality
                     # so later builds can reuse them via the shared cache


def livemap_cache_dir():
    return os.path.join(FNAMES.Preview_dir, "livemap")


def provider_is_mappable(provider_code):
    """True if the provider can back the live map directly."""
    provider = IMG.providers_dict.get(provider_code)
    if not provider:
        return False
    return (
        provider.get("grid_type") == "webmercator"
        and provider.get("request_type") in ("tms", "wmts")
    )


class _FetchBridge(QObject):
    """Marshals worker-thread fetch results onto the GUI thread."""

    tile_ready = Signal(str, int, int, int, str)  # provider, z, x, y, path


class MapView(QGraphicsView):
    """The map: live imagery, grid, selection, overlays, progress badges."""

    selection_changed = Signal()
    active_changed = Signal(object)   # (lat, lon) or None
    hover_ll = Signal(float, float)
    status_message = Signal(str)
    view_changed = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self._scene = QGraphicsScene(0, 0, WORLD, WORLD, self)
        self.setScene(self._scene)
        self.setRenderHints(
            QPainter.SmoothPixmapTransform | QPainter.Antialiasing
        )
        self.setTransformationAnchor(QGraphicsView.AnchorUnderMouse)
        self.setResizeAnchor(QGraphicsView.AnchorViewCenter)
        self.setDragMode(QGraphicsView.NoDrag)
        self.setBackgroundBrush(QBrush(QColor("#233240")))
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.setVerticalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.setMouseTracking(True)
        self.grabGesture(Qt.PinchGesture)

        # Imagery state
        self._provider_code = "OSM"
        self._display_code = "OSM"   # actual source drawn (fallback aware)
        self._provider_max_zl = 18
        self._zoom = 5.0
        self._tiles = {}             # (code, z, x, y) -> QGraphicsPixmapItem
        self._tile_age = {}          # same key -> monotonic counter
        self._age_counter = 0
        self._session = requests.Session()
        self._bridge = _FetchBridge()
        self._bridge.tile_ready.connect(self._on_tile_ready)
        self._generation = 0         # bumped on provider change
        # Download machinery: a bounded worker pool drains a queue that is
        # rebuilt (coarse-to-fine) on every view settle; anything not in the
        # current "wanted" set is dropped at dequeue time, which is what
        # cancels downloads for tiles that scrolled or zoomed out of view.
        self._fetch_lock = threading.Lock()
        self._fetch_queue = deque()
        self._wanted = set()
        self._inflight = set()
        self._fetch_wakeup = threading.Event()
        self._workers_started = False

        # Overlay state
        self._built = {}             # (lat, lon) -> TileInfo
        self._installed = set()      # (lat, lon)
        self._selection = set()
        self._active = None
        self._progress = {}          # (lat, lon) -> (state, label, pct)
        self._locked = False

        # Interaction state
        self._panning = False
        self._pan_start = None
        self._press_pos = None

        self._update_timer = QTimer(self)
        self._update_timer.setSingleShot(True)
        self._update_timer.setInterval(FETCH_DEBOUNCE_MS)
        self._update_timer.timeout.connect(self._refresh_tiles)

        self._make_legend()
        self._make_scan_status()
        self._apply_zoom()

    def _make_legend(self):
        from PySide6.QtWidgets import QLabel

        chips = "".join(
            "<span style='background:%s;color:%s'>&nbsp;%d&nbsp;</span>"
            % (ZL_COLORS[z], "#1F2937", z)
            for z in (15, 16, 17, 18, 19)
        )
        self.legend = QLabel(self.viewport())
        self.legend.setTextFormat(Qt.RichText)
        self.legend.setText(
            "<div style='font-size:10px'>"
            "built · color = ZL &nbsp;"
            "<span style='background:%s;color:#1F2937'>&nbsp;≤14&nbsp;</span>"
            "%s<br>"
            "<span style='color:#FFD60A'>▣</span> selected · "
            "double border = installed · * = custom zones"
            "</div>" % (ZL_COLORS[14], chips)
        )
        self.legend.setStyleSheet(
            "background: rgba(17,24,32,190); color: #E8EAED;"
            "padding: 5px 8px; border-radius: 6px;"
        )
        self.legend.adjustSize()
        self.legend.move(10, 10)
        self.legend.show()

    def set_legend_visible(self, visible):
        self.legend.setVisible(bool(visible))

    def _make_scan_status(self):
        """Bottom-right overlay shown while installed scenery is being read
        (a label + slim progress bar; hidden when no scan is running).  A
        viewport child like the legend, so it must be re-pinned on resize
        and after every scroll (QAbstractScrollArea drags viewport children
        when panning)."""
        from PySide6.QtWidgets import (QHBoxLayout, QLabel, QProgressBar,
                                       QWidget)

        self.scan_status = QWidget(self.viewport())
        self.scan_status.setStyleSheet(
            "background: rgba(17,24,32,190); border-radius: 6px;"
        )
        layout = QHBoxLayout(self.scan_status)
        layout.setContentsMargins(8, 5, 8, 5)
        layout.setSpacing(6)
        self._scan_status_label = QLabel(self.scan_status)
        self._scan_status_label.setStyleSheet(
            "color: #E8EAED; font-size: 10px; background: transparent;"
        )
        self._scan_status_bar = QProgressBar(self.scan_status)
        self._scan_status_bar.setTextVisible(False)
        self._scan_status_bar.setFixedSize(120, 8)
        self._scan_status_bar.setStyleSheet(
            "QProgressBar { background: rgba(255,255,255,40);"
            " border: none; border-radius: 4px; }"
            "QProgressBar::chunk { background: #4C8DFF; border-radius: 4px; }"
        )
        layout.addWidget(self._scan_status_label)
        layout.addWidget(self._scan_status_bar)
        self.scan_status.hide()

    def set_scan_status(self, text, done, total):
        """Show/update the scan overlay.  ``total`` <= 0 means the extent is
        not yet known — the bar goes indeterminate (busy) instead."""
        self._scan_status_label.setText(str(text))
        if total and total > 0:
            self._scan_status_bar.setRange(0, int(total))
            self._scan_status_bar.setValue(min(int(done), int(total)))
        else:
            self._scan_status_bar.setRange(0, 0)   # busy indicator
        self.scan_status.adjustSize()
        self._place_scan_status()
        self.scan_status.show()

    def clear_scan_status(self):
        self.scan_status.hide()

    def _place_scan_status(self):
        self.scan_status.adjustSize()
        self.scan_status.move(
            self.viewport().width() - self.scan_status.width() - 10,
            self.viewport().height() - self.scan_status.height() - 10,
        )

    def _place_legend(self):
        self.legend.adjustSize()
        self.legend.move(
            10, self.viewport().height() - self.legend.height() - 10
        )

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------
    def set_provider(self, code):
        if provider_is_mappable(code):
            self._display_code = code
            provider = IMG.providers_dict[code]
            self._provider_max_zl = int(provider.get("max_zl", 18))
            note = ""
        elif provider_is_mappable("OSM"):
            self._display_code = "OSM"
            self._provider_max_zl = 18
            note = " (no live preview for %s - showing OSM)" % code
        else:
            note = " (no live map source available)"
        self._provider_code = code
        self._generation += 1
        with self._fetch_lock:
            self._fetch_queue.clear()
            self._wanted = set()
        for key in list(self._tiles):
            self._drop_tile(key)
        self.status_message.emit(
            "Imagery: %s%s" % (self._display_code, note)
        )
        self._schedule_refresh()

    def display_code(self):
        return self._display_code

    def zoom_level(self):
        return self._zoom

    def set_built(self, built):
        self._built = dict(built)
        self.viewport().update()

    def set_installed(self, installed):
        self._installed = set(installed)
        self.viewport().update()

    def selection(self):
        return set(self._selection)

    def active_tile(self):
        return self._active

    def set_active(self, lat, lon, select=True):
        self._active = (lat, lon)
        if select:
            self._selection = {(lat, lon)}
            self.selection_changed.emit()
        self.active_changed.emit(self._active)
        self.viewport().update()

    def set_selection(self, tiles):
        """Replace the selection wholesale (programmatic callers, tests)."""
        self._selection = {(int(lat), int(lon)) for (lat, lon) in tiles}
        self.selection_changed.emit()
        self.viewport().update()

    def clear_selection(self):
        self._selection = set()
        self._active = None
        self.selection_changed.emit()
        self.active_changed.emit(None)
        self.viewport().update()

    def set_progress(self, progress):
        """progress: {(lat, lon): (state, label, pct)} with state in
        'queued' | 'active' | 'indeterminate' | 'done' | 'error'."""
        self._progress = dict(progress)
        self.viewport().update()

    def set_locked(self, locked):
        self._locked = bool(locked)
        self.viewport().update()

    def center_on_tile(self, lat, lon, zoom=None):
        cx, cy = GEO.wgs84_to_pix(lat + 0.5, lon + 0.5, SCENE_ZL)
        if zoom is not None:
            self._zoom = max(MIN_ZOOM, min(float(zoom), 19.0))
            self._apply_zoom()
        self.centerOn(QPointF(cx, cy))
        self._schedule_refresh()

    def zoom_to_tiles(self, tiles, margin=0.3):
        if not tiles:
            return
        lats = [t[0] for t in tiles]
        lons = [t[1] for t in tiles]
        x0, y0 = GEO.wgs84_to_pix(
            max(lats) + 1 + margin, min(lons) - margin, SCENE_ZL
        )
        x1, y1 = GEO.wgs84_to_pix(
            min(lats) - margin, max(lons) + 1 + margin, SCENE_ZL
        )
        self.fitInView(QRectF(x0, y0, x1 - x0, y1 - y0), Qt.KeepAspectRatio)
        self._zoom = SCENE_ZL + math.log2(max(self.transform().m11(), 1e-9))
        self._zoom = max(MIN_ZOOM, min(self._zoom, 19.0))
        self._apply_zoom()
        self._schedule_refresh()

    # ------------------------------------------------------------------
    # Zooming / panning / gestures
    # ------------------------------------------------------------------
    def _apply_zoom(self):
        scale = 2.0 ** (self._zoom - SCENE_ZL)
        self.resetTransform()
        self.scale(scale, scale)
        self.view_changed.emit()

    def _zoom_by(self, delta, anchor_pos=None):
        new_zoom = max(MIN_ZOOM, min(self._zoom + delta, 19.0))
        if abs(new_zoom - self._zoom) < 1e-6:
            return
        if anchor_pos is not None:
            old_scene = self.mapToScene(anchor_pos)
        self._zoom = new_zoom
        self._apply_zoom()
        if anchor_pos is not None:
            new_scene = self.mapToScene(anchor_pos)
            d = new_scene - old_scene
            self.translate(d.x(), d.y())
        self._schedule_refresh()

    def wheelEvent(self, event):
        # Trackpad two-finger scroll and mouse wheel both zoom (owner spec).
        delta = event.pixelDelta().y() or event.angleDelta().y() / 4
        if delta:
            self._zoom_by(delta / 120.0, event.position().toPoint())
        event.accept()

    def event(self, ev):
        if ev.type() == ev.Type.Gesture:
            pinch = ev.gesture(Qt.PinchGesture)
            if pinch:
                factor = pinch.scaleFactor()
                if factor and factor > 0:
                    center = self.mapFromGlobal(
                        pinch.centerPoint().toPoint()
                    )
                    self._zoom_by(math.log2(factor), center)
                ev.accept()
                return True
        return super().event(ev)

    def mousePressEvent(self, event):
        if event.button() in (Qt.RightButton, Qt.MiddleButton):
            self._panning = True
            self._pan_start = event.position()
            self.setCursor(Qt.ClosedHandCursor)
            event.accept()
            return
        if event.button() == Qt.LeftButton:
            self._press_pos = event.position()
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event):
        scene_pos = self.mapToScene(event.position().toPoint())
        lat, lon = GEO.pix_to_wgs84(scene_pos.x(), scene_pos.y(), SCENE_ZL)
        self.hover_ll.emit(lat, lon)
        if self._panning:
            d = event.position() - self._pan_start
            self._pan_start = event.position()
            self.horizontalScrollBar().setValue(
                self.horizontalScrollBar().value() - int(d.x())
            )
            self.verticalScrollBar().setValue(
                self.verticalScrollBar().value() - int(d.y())
            )
            self._schedule_refresh()
            event.accept()
            return
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event):
        if self._panning and event.button() in (
            Qt.RightButton,
            Qt.MiddleButton,
        ):
            self._panning = False
            self.unsetCursor()
            event.accept()
            return
        if (
            event.button() == Qt.LeftButton
            and self._press_pos is not None
            and (event.position() - self._press_pos).manhattanLength() < 6
            and not self._locked
        ):
            self._handle_click(event)
        self._press_pos = None
        super().mouseReleaseEvent(event)

    def _handle_click(self, event):
        scene_pos = self.mapToScene(event.position().toPoint())
        lat, lon = GEO.pix_to_wgs84(scene_pos.x(), scene_pos.y(), SCENE_ZL)
        tile = (math.floor(lat), math.floor(lon))
        mods = event.modifiers()
        if mods & Qt.ControlModifier:  # Cmd on macOS, Ctrl elsewhere
            if tile in self._selection:
                self._selection.discard(tile)
                if self._active == tile:
                    self._active = (
                        next(iter(self._selection)) if self._selection else None
                    )
                    self.active_changed.emit(self._active)
            else:
                self._selection.add(tile)
                self._active = tile
                self.active_changed.emit(tile)
        elif mods & Qt.ShiftModifier and self._active:
            la0, lo0 = self._active
            for la in range(min(la0, tile[0]), max(la0, tile[0]) + 1):
                for lo in range(min(lo0, tile[1]), max(lo0, tile[1]) + 1):
                    self._selection.add((la, lo))
        else:
            self._selection = {tile}
            self._active = tile
            self.active_changed.emit(tile)
        self.selection_changed.emit()
        self.viewport().update()

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._place_legend()
        self._place_scan_status()

    def scrollContentsBy(self, dx, dy):
        # QAbstractScrollArea pans by scrolling the whole viewport,
        # dragging child widgets (the legend) along — re-pin it after.
        super().scrollContentsBy(dx, dy)
        self._place_legend()
        self._place_scan_status()
        self._schedule_refresh()

    # ------------------------------------------------------------------
    # Live tile layer
    # ------------------------------------------------------------------
    def _schedule_refresh(self):
        self._update_timer.start()
        self.view_changed.emit()
        self.viewport().update()

    def _fetch_zoom(self):
        z = int(round(self._zoom))
        return max(2, min(z, self._provider_max_zl, 19))

    def _visible_range(self, z):
        """Visible tile index range at zoom z, with a one-tile margin."""
        view_rect = self.mapToScene(self.viewport().rect()).boundingRect()
        span = 256 * 2 ** (SCENE_ZL - z)
        x0 = max(0, int(view_rect.left() // span) - 1)
        x1 = min(2 ** z - 1, int(view_rect.right() // span) + 1)
        y0 = max(0, int(view_rect.top() // span) - 1)
        y1 = min(2 ** z - 1, int(view_rect.bottom() // span) + 1)
        return x0, x1, y0, y1

    def _pyramid_levels(self):
        """Zoom levels to keep covered, coarse to fine — the low-res-first
        fill: the base world layer, two intermediate steps, and the level
        matching the actual view zoom."""
        zf = self._fetch_zoom()
        levels = {BASE_ZL, zf}
        if zf - 2 > BASE_ZL:
            levels.add(zf - 2)
        if zf - 4 > BASE_ZL:
            levels.add(zf - 4)
        return sorted(levels)

    def _refresh_tiles(self):
        """Rebuild the download queue for the settled view.

        Coarse levels are queued before fine ones so something renders
        quickly everywhere; replacing the queue wholesale is what cancels
        every queued download that is no longer relevant. Runs on the GUI
        thread; does no I/O itself.
        """
        code = self._display_code
        if not provider_is_mappable(code):
            return
        wanted = set()
        order = []
        for z in self._pyramid_levels():
            if z == BASE_ZL:
                x0, x1, y0, y1 = 0, 2 ** z - 1, 0, 2 ** z - 1
            else:
                x0, x1, y0, y1 = self._visible_range(z)
            if (x1 - x0 + 1) * (y1 - y0 + 1) > LEVEL_TILE_CAP:
                continue
            # Spiral-ish: order by distance from range center so the middle
            # of the screen sharpens first.
            cx, cy = (x0 + x1) / 2, (y0 + y1) / 2
            coords = sorted(
                (
                    (x, y)
                    for x in range(x0, x1 + 1)
                    for y in range(y0, y1 + 1)
                ),
                key=lambda t: abs(t[0] - cx) + abs(t[1] - cy),
            )
            for x, y in coords:
                key = (code, z, x, y)
                wanted.add(key)
                if key in self._tiles:
                    self._age_counter += 1
                    self._tile_age[key] = self._age_counter
                else:
                    order.append(key)
        with self._fetch_lock:
            self._wanted = wanted
            self._fetch_queue.clear()
            self._fetch_queue.extend(
                k for k in order if k not in self._inflight
            )
        self._fetch_wakeup.set()
        self._start_workers()
        self._prune_tiles()

    def _start_workers(self):
        if self._workers_started:
            return
        self._workers_started = True
        for _ in range(FETCH_WORKERS):
            threading.Thread(target=self._worker_loop, daemon=True).start()

    def _worker_loop(self):
        """Download worker: pulls from the queue, re-checking at every stage
        that the tile is still wanted so out-of-view work is abandoned."""
        while True:
            with self._fetch_lock:
                key = (
                    self._fetch_queue.popleft()
                    if self._fetch_queue
                    else None
                )
                if key is not None:
                    if key not in self._wanted or key in self._tiles:
                        continue
                    self._inflight.add(key)
            if key is None:
                self._fetch_wakeup.wait(timeout=0.25)
                self._fetch_wakeup.clear()
                continue
            try:
                self._fetch_tile(*key)
            finally:
                with self._fetch_lock:
                    self._inflight.discard(key)

    def _cache_path(self, code, z, x, y):
        # Same layout as IMG's shared tile cache so builds can reuse what
        # the map has already downloaded (IMG.shared_tile_cache_path).
        return os.path.join(
            livemap_cache_dir(), code, str(z), "%s_%s.jpg" % (x, y)
        )

    def _still_wanted(self, key):
        with self._fetch_lock:
            return key in self._wanted

    def _orthophotos_crop(self, code, z, x, y):
        """Serve a view tile from the build pipeline's own imagery cache.

        Builds store assembled 16x16-tile orthophotos under Orthophotos/
        (FNAMES.Imagery_dir) and reuse them across runs; the map prefers
        that cache, so imagery downloaded for a build renders on the map
        with no re-download — and stays available offline.
        """
        provider = IMG.providers_dict.get(code)
        if not provider:
            return None
        try:
            til_x_left, til_y_top = x - x % 16, y - y % 16
            latc, lonc = GEO.gtile_to_wgs84(til_x_left + 8, til_y_top + 8, z)
            path = os.path.join(
                FNAMES.jpeg_file_dir_from_attributes(
                    math.floor(latc), math.floor(lonc), z, provider
                ),
                FNAMES.jpeg_file_name_from_attributes(
                    til_x_left, til_y_top, z, code
                ),
            )
            if not os.path.isfile(path):
                return None
            big = PILImage.open(path)
            w, h = big.size
            dx, dy = x - til_x_left, y - til_y_top
            sub = big.crop(
                (
                    dx * w // 16,
                    dy * h // 16,
                    (dx + 1) * w // 16,
                    (dy + 1) * h // 16,
                )
            )
            if sub.size != (256, 256):
                sub = sub.resize((256, 256), PILImage.Resampling.BICUBIC)
            return sub.convert("RGB")
        except Exception:
            return None

    def _fetch_tile(self, code, z, x, y):
        """Worker thread. Source priority: the map's own display cache,
        then the build pipeline's Orthophotos cache (cropped), then the
        provider over the network."""
        key = (code, z, x, y)
        path = self._cache_path(code, z, x, y)
        generation = self._generation
        try:
            if not os.path.isfile(path):
                if not self._still_wanted(key):
                    return  # cancelled while queued
                image = self._orthophotos_crop(code, z, x, y)
                from_build_cache = image is not None
                if image is None:
                    provider = IMG.providers_dict[code]
                    success, image = IMG.get_wmts_image(
                        z, x, y, provider, self._session
                    )
                    if not success or generation != self._generation:
                        return
                os.makedirs(os.path.dirname(path), exist_ok=True)
                tmp = path + ".tmp%s" % threading.get_ident()
                # Build-grade quality for close-in zoom levels: these tiles
                # are reused verbatim by future tile builds. Crops from the
                # build cache are display-only, keep them light.
                quality = (
                    85
                    if from_build_cache or z < BUILD_GRADE_ZL
                    else 95
                )
                image.convert("RGB").save(tmp, "JPEG", quality=quality)
                os.replace(tmp, path)
            if self._still_wanted(key) or z <= BASE_ZL:
                self._bridge.tile_ready.emit(code, z, x, y, path)
        except Exception:
            pass

    def _on_tile_ready(self, code, z, x, y, path):
        key = (code, z, x, y)
        if code != self._display_code or key in self._tiles:
            return
        pixmap = QPixmap(path)
        if pixmap.isNull():
            return
        item = QGraphicsPixmapItem(pixmap)
        span = 256 * 2 ** (SCENE_ZL - z)
        item.setPos(x * span, y * span)
        item.setScale(span / pixmap.width())
        item.setZValue(z)  # higher zooms draw over lower ones
        item.setTransformationMode(Qt.SmoothTransformation)
        self._scene.addItem(item)
        self._age_counter += 1
        self._tiles[key] = item
        self._tile_age[key] = self._age_counter

    def _drop_tile(self, key):
        item = self._tiles.pop(key, None)
        self._tile_age.pop(key, None)
        if item is not None:
            self._scene.removeItem(item)

    def _prune_tiles(self):
        if len(self._tiles) <= MAX_ITEMS:
            return
        # The base world layer is never pruned — it is the instant fallback
        # whenever the user flings the view somewhere new.
        by_age = [
            key
            for key in sorted(self._tile_age, key=self._tile_age.get)
            if key[1] > BASE_ZL
        ]
        for key in by_age[: len(self._tiles) - MAX_ITEMS]:
            self._drop_tile(key)

    # ------------------------------------------------------------------
    # Overlays
    # ------------------------------------------------------------------
    def _tile_rect(self, lat, lon):
        x0, y0 = GEO.wgs84_to_pix(lat + 1, lon, SCENE_ZL)
        x1, y1 = GEO.wgs84_to_pix(lat, lon + 1, SCENE_ZL)
        return QRectF(x0, y0, x1 - x0, y1 - y0)

    def drawForeground(self, painter, rect):
        super().drawForeground(painter, rect)
        scale = self.transform().m11()
        tile_px = 256 * 2 ** (SCENE_ZL - 6) * scale  # 1-deg-ish size on screen

        lat_hi, lon_lo = GEO.pix_to_wgs84(rect.left(), rect.top(), SCENE_ZL)
        lat_lo, lon_hi = GEO.pix_to_wgs84(rect.right(), rect.bottom(), SCENE_ZL)
        lat_lo, lat_hi = max(-85, math.floor(lat_lo)), min(85, math.ceil(lat_hi))
        lon_lo, lon_hi = max(-180, math.floor(lon_lo)), min(
            180, math.ceil(lon_hi)
        )

        # 1-degree grid
        if self._zoom >= 4.5 and (lat_hi - lat_lo) * (lon_hi - lon_lo) < 3000:
            pen = QPen(QColor(255, 255, 255, 70))
            pen.setCosmetic(True)
            painter.setPen(pen)
            for la in range(lat_lo, lat_hi + 1):
                x0, y = GEO.wgs84_to_pix(la, lon_lo, SCENE_ZL)
                x1, _ = GEO.wgs84_to_pix(la, lon_hi, SCENE_ZL)
                painter.drawLine(QPointF(x0, y), QPointF(x1, y))
            for lo in range(lon_lo, lon_hi + 1):
                x, y0 = GEO.wgs84_to_pix(lat_hi, lo, SCENE_ZL)
                _, y1 = GEO.wgs84_to_pix(lat_lo, lo, SCENE_ZL)
                painter.drawLine(QPointF(x, y0), QPointF(x, y1))

        visible = (
            (la, lo)
            for la in range(lat_lo, lat_hi)
            for lo in range(lon_lo, lon_hi)
        )
        font = QFont(painter.font())
        font.setPointSizeF(9)
        painter.setFont(font)

        if (lat_hi - lat_lo) * (lon_hi - lon_lo) < 3000:
            for tile in visible:
                self._draw_tile_overlay(painter, tile, scale)

        if self._locked:
            pen = QPen(QColor("#FFD60A"))
            pen.setCosmetic(True)
            pen.setWidth(2)
            painter.setPen(pen)

    def _draw_tile_overlay(self, painter, tile, scale):
        built = self._built.get(tile)
        selected = tile in self._selection
        active = tile == self._active
        progress = self._progress.get(tile)
        if not (built or selected or progress):
            return
        r = self._tile_rect(*tile)
        px = r.width() * scale  # tile size on screen

        if built is not None:
            base = QColor(ZL_COLORS.get(built.zl or 0, "#9AA5B1"))
            fill = QColor(base)
            fill.setAlpha(70)
            painter.fillRect(r, fill)
            border = QPen(base.darker(140))
            border.setCosmetic(True)
            border.setWidth(2)
            painter.setPen(border)
            painter.drawRect(r)
            if tile in self._installed:
                pen = QPen(QColor(20, 28, 36, 210))
                pen.setCosmetic(True)
                pen.setWidth(3)
                painter.setPen(pen)
                inset = r.adjusted(
                    r.width() * 0.03, r.height() * 0.03,
                    -r.width() * 0.03, -r.height() * 0.03,
                )
                painter.drawRect(inset)
            if px > 44 and built.provider:
                label = "%s %s%s" % (
                    built.provider[:4],
                    built.zl if built.zl else "?",
                    "*" if built.has_zones else "",
                )
                painter.setPen(QPen(QColor(255, 255, 255, 235)))
                painter.drawText(
                    r.translated(r.width() * 0.008, r.height() * 0.008),
                    Qt.AlignCenter,
                    label,
                )
                painter.setPen(QPen(QColor(15, 23, 32, 235)))
                painter.drawText(r, Qt.AlignCenter, label)

        if selected:
            fill = QColor("#FFD60A")
            fill.setAlpha(36)
            painter.fillRect(r, fill)
            pen = QPen(QColor("#FFD60A"))
            pen.setCosmetic(True)
            pen.setWidth(3 if active else 2)
            if not active:
                pen.setStyle(Qt.DashLine)
            painter.setPen(pen)
            painter.drawRect(r)

        if progress:
            self._draw_progress_badge(painter, r, px, progress)

    def _draw_progress_badge(self, painter, r, px, progress):
        state, label, pct = progress
        center = r.center()
        radius = min(r.width(), r.height()) * 0.14
        rect = QRectF(
            center.x() - radius, center.y() - radius, 2 * radius, 2 * radius
        )
        track = QPen(QColor(255, 255, 255, 110))
        track.setCosmetic(True)
        track.setWidth(4)
        if state == "done":
            painter.setPen(QPen(QColor("#2E9E5B"), 0))
            painter.setBrush(QColor("#2E9E5B"))
            painter.drawEllipse(rect)
            painter.setPen(QPen(QColor("#FFFFFF")))
            painter.drawText(rect, Qt.AlignCenter, "✓")
        elif state == "error":
            painter.setPen(QPen(QColor("#E5372B"), 0))
            painter.setBrush(QColor("#E5372B"))
            painter.drawEllipse(rect)
            painter.setPen(QPen(QColor("#FFFFFF")))
            painter.drawText(rect, Qt.AlignCenter, "!")
        elif state == "queued":
            pen = QPen(QColor(255, 255, 255, 150))
            pen.setCosmetic(True)
            pen.setWidth(2)
            pen.setStyle(Qt.DotLine)
            painter.setPen(pen)
            painter.setBrush(Qt.NoBrush)
            painter.drawEllipse(rect)
        else:
            painter.setPen(track)
            painter.setBrush(Qt.NoBrush)
            painter.drawEllipse(rect)
            arc = QPen(QColor("#FFD60A"))
            arc.setCosmetic(True)
            arc.setWidth(4)
            arc.setCapStyle(Qt.RoundCap)
            painter.setPen(arc)
            if state == "indeterminate":
                start = int(time.time() * 400) % 5760
                painter.drawArc(rect, -start, 1600)
            else:
                painter.drawArc(rect, 90 * 16, -int(5760 * pct / 100))
                painter.setPen(QPen(QColor("#FFFFFF")))
                painter.drawText(rect, Qt.AlignCenter, str(int(pct)))
        if label and px > 90:
            painter.setPen(QPen(QColor(255, 255, 255, 230)))
            label_rect = QRectF(
                r.left(), center.y() + radius * 1.3, r.width(), radius * 2
            )
            painter.drawText(label_rect, Qt.AlignHCenter | Qt.AlignTop, label)
