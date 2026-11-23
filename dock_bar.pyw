# dock_bar.pyw
# A minimalist dark-mode floating Dock / Shortcut bar for Windows 11.
# Required packages (install once):
#     pip install PySide6
#
# Features:
#   * Floating frameless dark dock (horizontal / vertical)
#   * Native Windows icon extraction for .exe / .lnk / folders / files
#   * Shortcut arrows removed 100% (resolves .lnk targets using pure ctypes)
#   * Drag & drop from Explorer to add shortcuts (Fixed disappearing issue)
#   * Drag to reorder internal shortcuts (with insertion indicator)
#   * Independent control for Icon Size and Bar Thickness
#   * Free move with edge snapping + position lock
#   * System tray icon with quick menu
#   * Settings dialog (icon size / bar size / spacing / opacity / orientation / etc.)
# ---------------------------------------------------------------------------

import os
import sys
import json
from pathlib import Path

from PySide6.QtCore import (
    Qt, QSize, QPoint, QPointF, QRect, QRectF, QMimeData, QFileInfo,
    QPropertyAnimation, QEasingCurve, Property, QTimer,
)
from PySide6.QtGui import (
    QIcon, QPixmap, QPainter, QColor, QPen, QImage, QPainterPath,
    QDrag, QPalette, QImageReader,
)
from PySide6.QtWidgets import (
    QApplication, QWidget, QVBoxLayout, QHBoxLayout, QBoxLayout, QLayout,
    QPushButton, QDialog, QSlider, QCheckBox, QRadioButton, QButtonGroup,
    QListWidget, QListWidgetItem, QFileDialog, QMenu, QSystemTrayIcon,
    QMessageBox, QToolTip, QFrame, QSizePolicy, QInputDialog,
    QDialogButtonBox, QGroupBox, QFormLayout, QLabel, QFileIconProvider,
    QStyle,
)

# ---------------------------------------------------------------------------
# Debug logging
# ---------------------------------------------------------------------------
_DEBUG = os.environ.get("DOCK_BAR_DEBUG", "") == "1"

def _log(msg: str) -> None:
    if _DEBUG:
        try:
            print(f"[dock-bar] {msg}", file=sys.stderr, flush=True)
        except Exception:
            pass

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

CONFIG_PATH = Path(__file__).resolve().parent / "dock_config.json"
MIME_INTERNAL = "application/x-dock-item"

DEFAULT_CONFIG = {
    "icon_size": 42,
    "bar_padding": 12,
    "spacing": 8,
    "opacity": 0.95,
    "orientation": "horizontal",
    "always_on_top": True,
    "locked": False,
    "pos": None,
    "bg_color": "#111215",
    "border_color": "#2A2C30",
    "accent_cream": "#E2D8B5",
    "accent_hover": "#E86F2C",
    "items": [],
}

DARK_STYLESHEET = """
QDialog, QMessageBox, QInputDialog, QFileDialog {
    background-color: #15171B;
    color: #E2D8B5;
    font-family: 'Segoe UI', sans-serif;
    font-size: 9pt;
}
QLabel { color: #C8C8C8; }
QGroupBox {
    border: 1px solid #2A2C30;
    border-radius: 8px;
    margin-top: 10px;
    padding-top: 10px;
    color: #E2D8B5;
    font-weight: 600;
}
QGroupBox::title {
    subcontrol-origin: margin;
    left: 10px;
    padding: 0 4px;
    color: #E2D8B5;
}
QSlider::groove:horizontal {
    height: 4px;
    background: #2A2C30;
    border-radius: 2px;
}
QSlider::handle:horizontal {
    background: #E2D8B5;
    width: 14px;
    margin: -6px 0;
    border-radius: 7px;
}
QSlider::handle:horizontal:hover { background: #E86F2C; }
QCheckBox, QRadioButton { color: #C8C8C8; }
QPushButton {
    background-color: #1E2025;
    color: #E2D8B5;
    border: 1px solid #2A2C30;
    padding: 6px 14px;
    border-radius: 6px;
}
QPushButton:hover { border-color: #E86F2C; color: #E86F2C; }
QPushButton:pressed { background-color: #E86F2C; color: #111215; }
QListWidget {
    background-color: #1A1C20;
    border: 1px solid #2A2C30;
    border-radius: 6px;
    color: #C8C8C8;
}
QListWidget::item { padding: 4px 8px; }
QListWidget::item:selected { background-color: #E86F2C; color: #111215; }
QMenu {
    background-color: #1A1C20;
    color: #E2D8B5;
    border: 1px solid #2A2C30;
    border-radius: 6px;
    padding: 4px;
}
QMenu::item { padding: 6px 24px; border-radius: 4px; }
QMenu::item:selected { background-color: #E86F2C; color: #111215; }
QMenu::separator { height: 1px; background-color: #2A2C30; margin: 4px 8px; }
"""

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

def load_config() -> dict:
    cfg = dict(DEFAULT_CONFIG)
    try:
        if CONFIG_PATH.exists():
            with open(CONFIG_PATH, "r", encoding="utf-8") as f:
                loaded = json.load(f)
            if isinstance(loaded, dict):
                for k, v in loaded.items():
                    cfg[k] = v
    except Exception:
        pass
    if "bar_padding" not in cfg:
        cfg["bar_padding"] = 12
    return cfg

def save_config(cfg: dict) -> None:
    try:
        with open(CONFIG_PATH, "w", encoding="utf-8") as f:
            json.dump(cfg, f, indent=2, ensure_ascii=False)
    except Exception:
        pass

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _expand(p: str) -> str:
    if not p: return ""
    try:
        p = os.path.expandvars(p)
        p = os.path.expanduser(p)
        return os.path.normpath(p)
    except Exception:
        return p

def _fix_size(pm: QPixmap, size: int) -> QPixmap:
    if pm.isNull(): return pm
    try: pm.setDevicePixelRatio(1.0)
    except Exception: pass
    if pm.width() == size and pm.height() == size: return pm
    return pm.scaled(size, size, Qt.KeepAspectRatio, Qt.SmoothTransformation)

# ---------------------------------------------------------------------------
# Icon extraction (Pure ctypes, No pywin32 dependency!)
# ---------------------------------------------------------------------------

def _resolve_lnk_target(path: str, _depth: int = 0):
    if _depth > 5: return "", "", 0
    try:
        import ctypes
        from ctypes import wintypes
        import uuid

        class GUID(ctypes.Structure):
            _fields_ = [("Data1", wintypes.DWORD), ("Data2", wintypes.WORD), ("Data3", wintypes.WORD), ("Data4", ctypes.c_ubyte * 8)]

        def uuid_to_guid(u):
            g = GUID()
            g.Data1, g.Data2, g.Data3 = u.time_low, u.time_mid, u.time_hi_version
            for i, b in enumerate(u.bytes[8:]): g.Data4[i] = b
            return g

        CLSID_ShellLink = uuid_to_guid(uuid.UUID("{00021401-0000-0000-C000-000000000046}"))
        IID_IShellLinkW = uuid_to_guid(uuid.UUID("{000214F9-0000-0000-C000-000000000046}"))
        IID_IPersistFile = uuid_to_guid(uuid.UUID("{0000010b-0000-0000-C000-000000000046}"))

        ole32 = ctypes.windll.ole32
        pShellLink = ctypes.c_void_p()
        pPersistFile = ctypes.c_void_p()

        try:
            ole32.CoInitialize(None)
            hr = ole32.CoCreateInstance(ctypes.byref(CLSID_ShellLink), None, 1, ctypes.byref(IID_IShellLinkW), ctypes.byref(pShellLink))
            if hr != 0: return "", "", 0

            vtable = ctypes.cast(pShellLink, ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p)))
            QueryInterface = ctypes.WINFUNCTYPE(ctypes.c_long, ctypes.c_void_p, ctypes.POINTER(GUID), ctypes.POINTER(ctypes.c_void_p))(vtable[0][0])
            hr = QueryInterface(pShellLink, ctypes.byref(IID_IPersistFile), ctypes.byref(pPersistFile))
            if hr != 0: return "", "", 0

            vtable_pf = ctypes.cast(pPersistFile, ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p)))
            Load = ctypes.WINFUNCTYPE(ctypes.c_long, ctypes.c_void_p, ctypes.c_wchar_p, wintypes.DWORD)(vtable_pf[0][5])
            hr = Load(pPersistFile, str(path), 0)
            if hr != 0: return "", "", 0

            vtable_sl = ctypes.cast(pShellLink, ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p)))
            # IShellLinkW vtable: QI=0, AddRef=1, Release=2, SetPath=3, GetPath=4...
            # (SetPath comes BEFORE GetPath in the interface declaration)
            GetPath = ctypes.WINFUNCTYPE(ctypes.c_long, ctypes.c_void_p, ctypes.c_wchar_p, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD)(vtable_sl[0][4])
            target_buf = ctypes.create_unicode_buffer(1024)
            GetPath(pShellLink, target_buf, 1024, None, 0)
            target = _expand(target_buf.value)

            # ... SetIconLocation=17, GetIconLocation=18
            GetIconLocation = ctypes.WINFUNCTYPE(ctypes.c_long, ctypes.c_void_p, ctypes.c_wchar_p, ctypes.c_int, ctypes.POINTER(ctypes.c_int))(vtable_sl[0][18])
            icon_buf = ctypes.create_unicode_buffer(1024)
            icon_index = ctypes.c_int(0)
            GetIconLocation(pShellLink, icon_buf, 1024, ctypes.byref(icon_index))
            icon_path = _expand(icon_buf.value)

            if icon_path and not os.path.isabs(icon_path):
                icon_path = _expand(os.path.join(os.path.dirname(path), icon_path))

            _log(f"lnk={path!r} target={target!r} icon={icon_path!r} idx={icon_index.value}")
            return target, icon_path, icon_index.value
        finally:
            if pPersistFile:
                vtable_pf = ctypes.cast(pPersistFile, ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p)))
                Release = ctypes.WINFUNCTYPE(ctypes.c_ulong, ctypes.c_void_p)(vtable_pf[0][2])
                Release(pPersistFile)
            if pShellLink:
                vtable_sl = ctypes.cast(pShellLink, ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p)))
                Release = ctypes.WINFUNCTYPE(ctypes.c_ulong, ctypes.c_void_p)(vtable_sl[0][2])
                Release(pShellLink)
            ole32.CoUninitialize()
    except Exception as e:
        _log(f"_resolve_lnk_target failed for {path!r}: {e}")
        return "", "", 0

def _extract_shell_icon(path: str, size: int):
    if not path: return None
    try:
        import ctypes
        from ctypes import wintypes

        shell32 = ctypes.windll.shell32
        user32 = ctypes.windll.user32

        SHGFI_ICON         = 0x000000100
        SHGFI_LARGEICON    = 0x000000000
        SHGFI_SMALLICON    = 0x000000001
        SHGFI_ICONLOCATION = 0x000000400

        class SHFILEINFO(ctypes.Structure):
            _fields_ = [
                ("hIcon", wintypes.HICON), ("iIcon", ctypes.c_int), ("dwAttributes", wintypes.DWORD),
                ("szDisplayName", ctypes.c_wchar * 1024), ("szTypeName", ctypes.c_wchar * 80),
            ]

        def _clean_iconlocation(loc: str, idx: int):
            loc = (loc or "").strip().strip('"')
            if loc and not os.path.exists(_expand(loc) or loc) and "," in loc:
                base, _, tail = loc.rpartition(",")
                tail = tail.strip()
                if tail.lstrip("-").isdigit():
                    loc = base.strip().strip('"')
                    try: idx = int(tail)
                    except Exception: pass
            return _expand(loc), idx

        # 1) Ask the shell where the icon for this file/folder/type lives, then
        #    extract the icon at the requested size (crisp, not a 32px upscale).
        try:
            info = SHFILEINFO()
            if shell32.SHGetFileInfoW(ctypes.c_wchar_p(path), 0, ctypes.byref(info), ctypes.sizeof(info), SHGFI_ICONLOCATION):
                loc, idx = _clean_iconlocation(info.szDisplayName, int(info.iIcon))
                _log(f"iconlocation {path!r} -> {loc!r} idx={idx}")
                if loc and os.path.exists(loc):
                    pix = _extract_win_icon(loc, size, idx)
                    if pix is not None and not pix.isNull(): return pix
        except Exception:
            pass

        # 2) Fallback: classic shell icon handle (usually 32x32, upscaled later)
        info = SHFILEINFO()
        flags = SHGFI_ICON | (SHGFI_LARGEICON if size >= 32 else SHGFI_SMALLICON)
        res = shell32.SHGetFileInfoW(ctypes.c_wchar_p(path), 0, ctypes.byref(info), ctypes.sizeof(info), flags)
        
        if not res or not info.hIcon: return None
        try:
            img = QImage.fromWinHICON(info.hIcon)
            if img.isNull(): return None
            return _fix_size(QPixmap.fromImage(img), size)
        finally:
            user32.DestroyIcon(info.hIcon)
    except Exception:
        return None

def _icon_from_file(path: str, size: int, index: int = 0):
    if not path or not os.path.exists(path): return None
    
    # 1. Folders
    if os.path.isdir(path): 
        pix = _extract_shell_icon(path, size)
        if pix is not None: return pix
        # Fallback for folders
        try:
            provider = QFileIconProvider()
            icon = provider.icon(QFileInfo(path))
            pm = icon.pixmap(QSize(size, size))
            if not pm.isNull() and pm.width() > 0: return _fix_size(pm, size)
        except Exception: pass
        return None

    # 2. ICO / CUR files
    low = path.lower()
    if low.endswith((".ico", ".cur")):
        try:
            icon = QIcon(path)
            sizes = icon.availableSizes()
            if sizes:
                largest = max(sizes, key=lambda s: s.width() * s.height())
                pm = icon.pixmap(largest)
                if not pm.isNull() and pm.width() > 0: return _fix_size(pm, size)
        except Exception: pass

        try:
            reader = QImageReader(path)
            if reader.canRead():
                img = reader.read()
                if not img.isNull(): return _fix_size(QPixmap.fromImage(img), size)
        except Exception: pass

    # 3. Extract via Win32
    pix = _extract_win_icon(path, size, index)
    if pix is not None and not pix.isNull(): return pix

    # 4. Shell fallback (no overlay)
    pix = _extract_shell_icon(path, size)
    if pix is not None and not pix.isNull(): return pix

    return None

def extract_icon_for_path(path: str, size: int = 64) -> QPixmap:
    path = _expand(str(path))
    is_lnk = path.lower().endswith(".lnk")
    target, icon_path, icon_index = "", "", 0
    
    if is_lnk:
        target, icon_path, icon_index = _resolve_lnk_target(path)
        
        # 1. If target is a folder, use shell icon of the folder (reads desktop.ini)
        if target and os.path.isdir(target):
            pix = _extract_shell_icon(target, size)
            if pix is not None and not pix.isNull(): return pix
                
        # 2. Shell icon of the .lnk itself (what Explorer renders,
        #    incl. custom folder icons / .pyw type icons)
        pix = _extract_shell_icon(path, size)
        if pix is not None and not pix.isNull(): return pix
                
        # 3. Custom icon location of the .lnk itself
        if icon_path and os.path.exists(icon_path):
            pix = _icon_from_file(icon_path, size, icon_index)
            if pix is not None: return pix
        
        # 4. Fallback to target path (file or folder)
        if target and os.path.exists(target):
            pix = _icon_from_file(target, size, 0)
            if pix is not None: return pix
    else:
        # Normal file
        pix = _icon_from_file(path, size, 0)
        if pix is not None: return pix

    # Fallback to QFileIconProvider
    try:
        provider = QFileIconProvider()
        fallback_path = target if is_lnk and target else path
        icon = provider.icon(QFileInfo(fallback_path))
        pm = icon.pixmap(QSize(size, size))
        if not pm.isNull() and pm.width() > 0: return _fix_size(pm, size)
    except Exception: pass

    # Last resort: generic file icon
    try:
        style = QApplication.instance().style()
        pm = style.standardIcon(QStyle.SP_FileIcon).pixmap(QSize(size, size))
        if not pm.isNull(): return _fix_size(pm, size)
    except Exception: pass

    pm = QPixmap(size, size)
    pm.fill(Qt.transparent)
    return pm

# ---------------------------------------------------------------------------
# Dock item model
# ---------------------------------------------------------------------------
class DockItem:
    def __init__(self, path: str, name: str = None, args: str = ""):
        self.path = str(path)
        self.name = name or Path(self.path).stem
        self.args = args

    def to_dict(self) -> dict:
        return {"path": self.path, "name": self.name, "args": self.args}

    @staticmethod
    def from_dict(d: dict) -> "DockItem":
        return DockItem(d.get("path", ""), d.get("name"), d.get("args", ""))

# ---------------------------------------------------------------------------
# Dock button
# ---------------------------------------------------------------------------
class DockButton(QWidget):
    def __init__(self, item: DockItem, dock_bar: "DockBar"):
        super().__init__(dock_bar)
        self.item = item
        self.dock_bar = dock_bar
        self._scale = 1.0
        self._hovered = False
        self._pixmap = None
        self._press_pos = None
        self._last_icon_size = -1

        self.setMouseTracking(True)
        self.setCursor(Qt.PointingHandCursor)
        self.setAcceptDrops(True)

        self._anim = QPropertyAnimation(self, b"scale", self)
        self._anim.setDuration(150)
        self._anim.setEasingCurve(QEasingCurve.OutCubic)

        self._update_size()
        self.setToolTip(self.item.name)

    def _get_scale(self) -> float: return self._scale
    def _set_scale(self, v: float) -> None:
        self._scale = v
        self.update()
    scale = Property(float, _get_scale, _set_scale)

    def _update_size(self) -> None:
        icon_size = self.dock_bar.config["icon_size"]
        if icon_size != self._last_icon_size:
            self._last_icon_size = icon_size
            try:
                self._pixmap = extract_icon_for_path(self.item.path, max(icon_size * 2, 96))
            except Exception:
                self._pixmap = QPixmap(icon_size, icon_size)
                
        pad = self.dock_bar.config.get("bar_padding", 12)
        side = icon_size + pad * 2
        self.setFixedSize(side, side)

    def paintEvent(self, event) -> None:
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing, True)
        p.setRenderHint(QPainter.SmoothPixmapTransform, True)

        rect = self.rect()
        icon_size = self.dock_bar.config["icon_size"]
        draw_size = max(1, int(round(icon_size * self._scale)))

        if self._pixmap is not None and not self._pixmap.isNull():
            x = (rect.width() - draw_size) // 2
            y = (rect.height() - draw_size) // 2
            if self._hovered: y -= 2
            p.drawPixmap(QRect(x, y, draw_size, draw_size), self._pixmap)

        if self._hovered:
            color = QColor(self.dock_bar.config["accent_hover"])
            pen = QPen(color, 2)
            pen.setCapStyle(Qt.RoundCap)
            p.setPen(pen)
            if self.dock_bar.config["orientation"] == "horizontal":
                y = rect.height() - 3
                p.drawLine(int(rect.width() * 0.25), y, int(rect.width() * 0.75), y)
            else:
                x = rect.width() - 3
                p.drawLine(x, int(rect.height() * 0.25), x, int(rect.height() * 0.75))

        p.end()

    def enterEvent(self, event) -> None:
        self.raise_()
        self._hovered = True
        self._anim.stop()
        self._anim.setStartValue(self._scale)
        self._anim.setEndValue(1.15)
        self._anim.start()
        self.update()

    def leaveEvent(self, event) -> None:
        self._hovered = False
        self._anim.stop()
        self._anim.setStartValue(self._scale)
        self._anim.setEndValue(1.0)
        self._anim.start()
        self.update()

    def mousePressEvent(self, event) -> None:
        if event.button() == Qt.LeftButton:
            self._press_pos = event.position().toPoint()
            event.accept()
        elif event.button() == Qt.RightButton:
            self._show_context_menu(event.globalPosition().toPoint())
            event.accept()
        else:
            super().mousePressEvent(event)

    def mouseMoveEvent(self, event) -> None:
        if self._press_pos is None: return
        delta = event.position().toPoint() - self._press_pos
        if delta.manhattanLength() >= QApplication.startDragDistance():
            self._start_drag()
            self._press_pos = None

    def mouseReleaseEvent(self, event) -> None:
        if event.button() == Qt.LeftButton and self._press_pos is not None:
            self._press_pos = None
            self.dock_bar.launch_item(self.item)
            event.accept()
        else:
            super().mouseReleaseEvent(event)

    def _start_drag(self) -> None:
        try: idx = self.dock_bar.items.index(self.item)
        except ValueError: return
        drag = QDrag(self)
        mime = QMimeData()
        mime.setData(MIME_INTERNAL, str(idx).encode("utf-8"))
        drag.setMimeData(mime)
        if self._pixmap is not None and not self._pixmap.isNull():
            drag.setPixmap(self._pixmap)
            drag.setHotSpot(QPoint(self._pixmap.width() // 2, self._pixmap.height() // 2))
        drag.exec(Qt.MoveAction)

    def _show_context_menu(self, global_pos: QPoint) -> None:
        menu = QMenu(self)
        menu.setStyleSheet(DARK_STYLESHEET)
        act_launch = menu.addAction("Launch")
        act_rename = menu.addAction("Rename...")
        menu.addSeparator()
        act_open = menu.addAction("Open File Location")
        menu.addSeparator()
        act_remove = menu.addAction("Remove from Dock")

        chosen = menu.exec(global_pos)
        if chosen == act_launch: self.dock_bar.launch_item(self.item)
        elif chosen == act_rename: self.dock_bar.rename_item(self.item)
        elif chosen == act_open: self.dock_bar.open_location(self.item)
        elif chosen == act_remove: self.dock_bar.remove_item(self.item)

    def dragEnterEvent(self, event) -> None: self.dock_bar._handle_drag_enter(event, self)
    def dragMoveEvent(self, event) -> None: self.dock_bar._handle_drag_move(event, self)
    def dragLeaveEvent(self, event) -> None:
        self.dock_bar._drop_index = -1
        self.dock_bar.update()
    def dropEvent(self, event) -> None: self.dock_bar._handle_drop(event, self)

# ---------------------------------------------------------------------------
# Dock bar
# ---------------------------------------------------------------------------
class DockBar(QWidget):
    def __init__(self):
        super().__init__()
        self.config = load_config()
        self.items = [DockItem.from_dict(d) for d in self.config.get("items", [])]

        self.setWindowFlags(Qt.FramelessWindowHint | Qt.Tool | Qt.WindowStaysOnTopHint)
        self.setAttribute(Qt.WA_TranslucentBackground, True)
        self.setAcceptDrops(True)

        self._layout = QBoxLayout(QBoxLayout.LeftToRight, self)
        self._layout.setContentsMargins(6, 6, 6, 6)
        self._layout.setSpacing(self.config["spacing"])
        self._layout.setSizeConstraint(QLayout.SetFixedSize)

        self.buttons = []
        self._dragging_window = False
        self._drag_offset = QPoint()
        self._drop_index = -1

        self._rebuild()
        self._apply_config(full=True)
        self._restore_position()

    def wheelEvent(self, event) -> None:
        if event.modifiers() == Qt.ControlModifier:
            delta = event.angleDelta().y()
            if delta > 0: new_size = min(96, self.config["icon_size"] + 2)
            else: new_size = max(24, self.config["icon_size"] - 2)
            
            if new_size != self.config["icon_size"]:
                self.config["icon_size"] = new_size
                self._apply_config()
                save_config(self.config)
            event.accept()
        else:
            super().wheelEvent(event)

    def launch_item(self, item: DockItem) -> None:
        try:
            path = item.path
            if not os.path.exists(path):
                QMessageBox.warning(self, "Dock", f"Path not found:\n{path}")
                return
            os.startfile(path)
        except Exception as e:
            QMessageBox.warning(self, "Dock", f"Failed to launch:\n{e}")

    def rename_item(self, item: DockItem) -> None:
        text, ok = QInputDialog.getText(self, "Rename", "Display name:", text=item.name)
        if ok and text.strip():
            item.name = text.strip()
            self._save()
            for b in self.buttons:
                if b.item is item: b.setToolTip(item.name)

    def remove_item(self, item: DockItem) -> None:
        if item in self.items:
            self.items.remove(item)
            self._rebuild()
            self._save()

    def open_location(self, item: DockItem) -> None:
        try:
            path = item.path
            if os.path.isdir(path): os.startfile(path)
            elif os.path.exists(path): os.startfile(os.path.dirname(path) or ".")
        except Exception: pass

    def _rebuild(self) -> None:
        while self._layout.count():
            it = self._layout.takeAt(0)
            w = it.widget()
            if w is not None:
                w.setParent(None)
                w.deleteLater()
        self.buttons = []

        if self.config["orientation"] == "horizontal":
            self._layout.setDirection(QBoxLayout.LeftToRight)
        else:
            self._layout.setDirection(QBoxLayout.TopToBottom)

        self._layout.setSpacing(self.config["spacing"])
        self._layout.setContentsMargins(6, 6, 6, 6)

        for item in self.items:
            btn = DockButton(item, self)
            self.buttons.append(btn)
            self._layout.addWidget(btn)

        self._layout.activate()
        self.adjustSize()
        self.resize(self.sizeHint())

    def _apply_config(self, full: bool = False) -> None:
        self.setWindowOpacity(max(0.15, min(1.0, self.config["opacity"])))
        self._layout.setSpacing(self.config["spacing"])

        if self.config["orientation"] == "horizontal":
            self._layout.setDirection(QBoxLayout.LeftToRight)
        else:
            self._layout.setDirection(QBoxLayout.TopToBottom)

        for b in self.buttons: b._update_size()

        flags = Qt.FramelessWindowHint | Qt.Tool
        if self.config["always_on_top"]: flags |= Qt.WindowStaysOnTopHint
        if self.windowFlags() != flags:
            self.setWindowFlags(flags)
            if self.isVisible(): self.show()

        self._layout.activate()
        self.adjustSize()
        self.resize(self.sizeHint())
        self.update()

    def _restore_position(self) -> None:
        self.adjustSize()
        pos = self.config.get("pos")
        if isinstance(pos, (list, tuple)) and len(pos) == 2:
            try:
                self.move(int(pos[0]), int(pos[1]))
                if QApplication.screenAt(QPoint(int(pos[0]), int(pos[1]))) is None:
                    self._set_default_position()
                return
            except Exception: pass
        self._set_default_position()

    def _set_default_position(self) -> None:
        screen = QApplication.primaryScreen()
        if screen is None: return
        geo = screen.availableGeometry()
        self.adjustSize()
        x = geo.x() + (geo.width() - self.width()) // 2
        y = geo.y() + geo.height() - self.height() - 20
        self.move(x, y)

    def _save(self) -> None:
        self.config["items"] = [it.to_dict() for it in self.items]
        save_config(self.config)

    def paintEvent(self, event) -> None:
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing, True)

        outer = QRectF(self.rect()).adjusted(1, 1, -1, -1)
        path = QPainterPath()
        path.addRoundedRect(outer, 14, 14)

        p.fillPath(path, QColor(self.config["bg_color"]))
        pen = QPen(QColor(self.config["accent_hover"] if self._drop_index >= 0 else self.config["border_color"]), 2 if self._drop_index >= 0 else 1)
        p.setPen(pen)
        p.drawPath(path)

        if self._drop_index >= 0 and self.buttons:
            pt = self._compute_insert_line_pos(self._drop_index)
            pen2 = QPen(QColor(self.config["accent_hover"]), 3)
            pen2.setCapStyle(Qt.RoundCap)
            p.setPen(pen2)
            if self.config["orientation"] == "horizontal": p.drawLine(pt.x(), 8, pt.x(), self.height() - 8)
            else: p.drawLine(8, pt.y(), self.width() - 8, pt.y())

        p.end()

    def mousePressEvent(self, event) -> None:
        if event.button() == Qt.LeftButton and not self.config["locked"]:
            self._dragging_window = True
            self._drag_offset = event.globalPosition().toPoint() - self.frameGeometry().topLeft()
            event.accept()
        else: super().mousePressEvent(event)

    def mouseMoveEvent(self, event) -> None:
        if self._dragging_window and not self.config["locked"]:
            self.move(event.globalPosition().toPoint() - self._drag_offset)
            event.accept()

    def mouseReleaseEvent(self, event) -> None:
        if self._dragging_window:
            self._dragging_window = False
            self._snap_to_edge()
            self.config["pos"] = [self.x(), self.y()]
            save_config(self.config)
            event.accept()
        else: super().mouseReleaseEvent(event)

    def mouseDoubleClickEvent(self, event) -> None:
        if event.button() == Qt.LeftButton:
            self._open_settings()
            event.accept()

    def _snap_to_edge(self) -> None:
        screen = QApplication.screenAt(self.frameGeometry().center()) or QApplication.primaryScreen()
        if screen is None: return
        geo = screen.availableGeometry()
        r = self.frameGeometry()
        snap = 24
        x, y = self.x(), self.y()
        if abs(r.left() - geo.left()) < snap: x = geo.left()
        if abs(r.right() - geo.right()) < snap: x = geo.right() - r.width() + 1
        if abs(r.top() - geo.top()) < snap: y = geo.top()
        if abs(r.bottom() - geo.bottom()) < snap: y = geo.bottom() - r.height() + 1
        self.move(x, y)

    def contextMenuEvent(self, event) -> None:
        menu = QMenu(self)
        menu.setStyleSheet(DARK_STYLESHEET)
        menu.addAction("Add File...", self._dialog_add_file)
        menu.addAction("Add Folder...", self._dialog_add_folder)
        menu.addSeparator()
        menu.addAction("Toggle Orientation", self._toggle_orientation)
        menu.addAction("Toggle Lock Position", self._toggle_lock)
        menu.addAction("Toggle Always On Top", self._toggle_on_top)
        menu.addSeparator()
        menu.addAction("Settings...", self._open_settings)
        menu.addSeparator()
        menu.addAction("Exit", QApplication.instance().quit)
        menu.exec(event.globalPos())

    def _toggle_orientation(self) -> None:
        self.config["orientation"] = "vertical" if self.config["orientation"] == "horizontal" else "horizontal"
        self._apply_config(full=True)
        self._rebuild()
        save_config(self.config)

    def _toggle_lock(self) -> None:
        self.config["locked"] = not self.config["locked"]
        save_config(self.config)

    def _toggle_on_top(self) -> None:
        self.config["always_on_top"] = not self.config["always_on_top"]
        self._apply_config(full=True)
        save_config(self.config)

    def _open_settings(self) -> None:
        SettingsDialog(self).exec()

    def _dialog_add_file(self) -> None:
        files, _ = QFileDialog.getOpenFileNames(self, "Add Shortcuts")
        for f in files: self._add_item(f)

    def _dialog_add_folder(self) -> None:
        folder = QFileDialog.getExistingDirectory(self, "Add Folder")
        if folder: self._add_item(folder)

    def _add_item(self, path: str, index: int = None) -> None:
        try: path = os.path.abspath(path)
        except Exception: return
        if not os.path.exists(path): return
        norm = os.path.normcase(path)
        for it in self.items:
            if os.path.normcase(it.path) == norm: return
        item = DockItem(path)
        if index is None or index < 0 or index > len(self.items): self.items.append(item)
        else: self.items.insert(index, item)
        self._rebuild()
        self._save()

    def _move_item(self, src: int, dst: int) -> None:
        if src < 0 or src >= len(self.items): return
        if dst < 0: dst = 0
        if dst > len(self.items): dst = len(self.items)
        if dst == src or dst == src + 1: return
        item = self.items.pop(src)
        if dst > src: dst -= 1
        self.items.insert(dst, item)
        self._rebuild()
        self._save()

    def _is_acceptable(self, mime: QMimeData) -> bool:
        return mime.hasFormat(MIME_INTERNAL) or mime.hasUrls()

    def _map_to_dock(self, event, source: QWidget) -> QPoint:
        return self.mapFromGlobal(source.mapToGlobal(event.position().toPoint()))

    def _compute_drop_index(self, pos: QPoint) -> int:
        if not self.buttons: return 0
        for i, b in enumerate(self.buttons):
            g = b.geometry()
            if self.config["orientation"] == "horizontal":
                if pos.x() < g.center().x(): return i
            else:
                if pos.y() < g.center().y(): return i
        return len(self.buttons)

    def _compute_insert_line_pos(self, index: int) -> QPoint:
        if not self.buttons: return QPoint(self.width() // 2, self.height() // 2)
        index = max(0, min(index, len(self.buttons)))
        spacing = self.config["spacing"]
        if self.config["orientation"] == "horizontal":
            if index < len(self.buttons):
                return QPoint(self.buttons[index].geometry().left() - spacing // 2 - 1, 0)
            return QPoint(self.buttons[-1].geometry().right() + spacing // 2 + 1, 0)
        else:
            if index < len(self.buttons):
                return QPoint(0, self.buttons[index].geometry().top() - spacing // 2 - 1)
            return QPoint(0, self.buttons[-1].geometry().bottom() + spacing // 2 + 1)

    def _handle_drag_enter(self, event, source: QWidget) -> None:
        if self._is_acceptable(event.mimeData()):
            event.acceptProposedAction()
            self._drop_index = self._compute_drop_index(self._map_to_dock(event, source))
            self.update()

    def _handle_drag_move(self, event, source: QWidget) -> None:
        if self._is_acceptable(event.mimeData()):
            event.acceptProposedAction()
            self._drop_index = self._compute_drop_index(self._map_to_dock(event, source))
            self.update()

    def _handle_drop(self, event, source: QWidget) -> None:
        pos = self._map_to_dock(event, source)
        mime = event.mimeData()
        target = self._compute_drop_index(pos)
        if mime.hasFormat(MIME_INTERNAL):
            try: src_idx = int(bytes(mime.data(MIME_INTERNAL)).decode("utf-8"))
            except Exception: src_idx = -1
            if src_idx >= 0: self._move_item(src_idx, target)
        elif mime.hasUrls():
            added = False
            for url in mime.urls():
                path = url.toLocalFile()
                if path:
                    try:
                        path = os.path.abspath(path)
                        if os.path.exists(path):
                            norm = os.path.normcase(path)
                            if not any(os.path.normcase(it.path) == norm for it in self.items):
                                item = DockItem(path)
                                if target < 0: target = 0
                                if target > len(self.items): target = len(self.items)
                                self.items.insert(target, item)
                                target += 1
                                added = True
                    except Exception:
                        pass
            if added:
                self._save()
                QTimer.singleShot(10, self._rebuild)
        self._drop_index = -1
        self.update()
        event.acceptProposedAction()

    def dragEnterEvent(self, event) -> None: self._handle_drag_enter(event, self)
    def dragMoveEvent(self, event) -> None: self._handle_drag_move(event, self)
    def dragLeaveEvent(self, event) -> None:
        self._drop_index = -1
        self.update()
    def dropEvent(self, event) -> None: self._handle_drop(event, self)

# ---------------------------------------------------------------------------
# Settings dialog
# ---------------------------------------------------------------------------
class SettingsDialog(QDialog):
    def __init__(self, dock_bar: DockBar):
        super().__init__(dock_bar)
        self.dock_bar = dock_bar
        self.setWindowTitle("Dock Settings")
        self.setModal(True)
        self.setMinimumWidth(460)
        self.setStyleSheet(DARK_STYLESHEET)

        root = QVBoxLayout(self)
        root.setSpacing(10)

        appearance = QGroupBox("Appearance")
        form = QFormLayout(appearance)

        self.icon_size_slider = QSlider(Qt.Horizontal)
        self.icon_size_slider.setRange(24, 96)
        self.icon_size_slider.setValue(dock_bar.config["icon_size"])
        form.addRow("Icon Size:", self.icon_size_slider)

        self.bar_padding_slider = QSlider(Qt.Horizontal)
        self.bar_padding_slider.setRange(0, 40)
        self.bar_padding_slider.setValue(dock_bar.config.get("bar_padding", 12))
        form.addRow("Bar Thickness:", self.bar_padding_slider)

        self.spacing_slider = QSlider(Qt.Horizontal)
        self.spacing_slider.setRange(0, 32)
        self.spacing_slider.setValue(dock_bar.config["spacing"])
        form.addRow("Spacing:", self.spacing_slider)

        self.opacity_slider = QSlider(Qt.Horizontal)
        self.opacity_slider.setRange(30, 100)
        self.opacity_slider.setValue(int(dock_bar.config["opacity"] * 100))
        form.addRow("Opacity:", self.opacity_slider)

        root.addWidget(appearance)

        behavior = QGroupBox("Behavior")
        bform = QFormLayout(behavior)

        self.h_radio = QRadioButton("Horizontal")
        self.v_radio = QRadioButton("Vertical")
        grp = QButtonGroup(self)
        grp.addButton(self.h_radio)
        grp.addButton(self.v_radio)
        if dock_bar.config["orientation"] == "horizontal": self.h_radio.setChecked(True)
        else: self.v_radio.setChecked(True)

        orient_wrap = QWidget()
        o_lay = QHBoxLayout(orient_wrap)
        o_lay.setContentsMargins(0, 0, 0, 0)
        o_lay.addWidget(self.h_radio)
        o_lay.addWidget(self.v_radio)
        o_lay.addStretch(1)
        bform.addRow("Orientation:", orient_wrap)

        self.on_top_check = QCheckBox("Always on top")
        self.on_top_check.setChecked(dock_bar.config["always_on_top"])
        bform.addRow(self.on_top_check)

        self.locked_check = QCheckBox("Lock position")
        self.locked_check.setChecked(dock_bar.config["locked"])
        bform.addRow(self.locked_check)

        root.addWidget(behavior)

        shortcuts = QGroupBox("Shortcuts")
        s_lay = QVBoxLayout(shortcuts)

        self.list_widget = QListWidget()
        self.list_widget.setMinimumHeight(140)
        self._refresh_list()
        s_lay.addWidget(self.list_widget)

        btn_row = QHBoxLayout()
        self.add_file_btn = QPushButton("Add File...")
        self.add_folder_btn = QPushButton("Add Folder...")
        self.remove_btn = QPushButton("Remove Selected")
        btn_row.addWidget(self.add_file_btn)
        btn_row.addWidget(self.add_folder_btn)
        btn_row.addStretch(1)
        btn_row.addWidget(self.remove_btn)
        s_lay.addLayout(btn_row)

        root.addWidget(shortcuts)

        box = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        box.accepted.connect(self._on_accept)
        box.rejected.connect(self.reject)
        root.addWidget(box)

        self.icon_size_slider.valueChanged.connect(self._preview)
        self.bar_padding_slider.valueChanged.connect(self._preview)
        self.spacing_slider.valueChanged.connect(self._preview)
        self.opacity_slider.valueChanged.connect(self._preview)
        self.h_radio.toggled.connect(self._preview)
        self.v_radio.toggled.connect(self._preview)
        self.on_top_check.toggled.connect(self._preview)
        self.add_file_btn.clicked.connect(self._add_file)
        self.add_folder_btn.clicked.connect(self._add_folder)
        self.remove_btn.clicked.connect(self._remove_selected)

    def _refresh_list(self) -> None:
        self.list_widget.clear()
        for it in self.dock_bar.items:
            self.list_widget.addItem(QListWidgetItem(f"{it.name}   —   {it.path}"))

    def _preview(self) -> None:
        cfg = self.dock_bar.config
        cfg["icon_size"] = self.icon_size_slider.value()
        cfg["bar_padding"] = self.bar_padding_slider.value()
        cfg["spacing"] = self.spacing_slider.value()
        cfg["opacity"] = self.opacity_slider.value() / 100.0
        cfg["orientation"] = "horizontal" if self.h_radio.isChecked() else "vertical"
        cfg["always_on_top"] = self.on_top_check.isChecked()
        self.dock_bar._apply_config(full=True)

    def _add_file(self) -> None:
        files, _ = QFileDialog.getOpenFileNames(self, "Add Files")
        for f in files: self.dock_bar._add_item(f)
        self._refresh_list()

    def _add_folder(self) -> None:
        folder = QFileDialog.getExistingDirectory(self, "Add Folder")
        if folder: self.dock_bar._add_item(folder)
        self._refresh_list()

    def _remove_selected(self) -> None:
        row = self.list_widget.currentRow()
        if 0 <= row < len(self.dock_bar.items):
            self.dock_bar.remove_item(self.dock_bar.items[row])
            self._refresh_list()

    def _on_accept(self) -> None:
        self.dock_bar.config["locked"] = self.locked_check.isChecked()
        self.dock_bar._save()
        self.accept()

# ---------------------------------------------------------------------------
# Tray icon
# ---------------------------------------------------------------------------
def _make_tray_pixmap(size: int = 64) -> QPixmap:
    pm = QPixmap(size, size)
    pm.fill(Qt.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.Antialiasing, True)
    p.setBrush(QColor("#111215"))
    p.setPen(QPen(QColor("#E2D8B5"), 2))
    p.drawRoundedRect(3, 3, size - 6, size - 6, size // 5, size // 5)
    p.setBrush(QColor("#E86F2C"))
    p.setPen(Qt.NoPen)
    r = max(2, size // 12)
    cy = size // 2
    for x_ratio in (0.30, 0.50, 0.70): p.drawEllipse(QPoint(int(size * x_ratio), cy), r, r)
    p.end()
    return pm

class TrayIcon(QSystemTrayIcon):
    def __init__(self, dock_bar: DockBar):
        super().__init__()
        self.dock_bar = dock_bar
        self.setIcon(QIcon(_make_tray_pixmap(64)))
        self.setToolTip("Dock Bar")
        self._build_menu()
        self.activated.connect(self._on_activated)

    def _build_menu(self) -> None:
        menu = QMenu()
        menu.setStyleSheet(DARK_STYLESHEET)
        menu.aboutToShow.connect(self._refresh_menu_state)

        self.act_show = menu.addAction("Show / Hide Dock")
        self.act_show.triggered.connect(self._toggle_dock_visibility)
        menu.addSeparator()
        self.act_settings = menu.addAction("Settings...")
        self.act_settings.triggered.connect(self.dock_bar._open_settings)
        self.act_add_file = menu.addAction("Add Shortcut...")
        self.act_add_file.triggered.connect(self.dock_bar._dialog_add_file)
        self.act_add_folder = menu.addAction("Add Folder...")
        self.act_add_folder.triggered.connect(self.dock_bar._dialog_add_folder)
        menu.addSeparator()
        self.act_orientation = menu.addAction("Toggle Orientation")
        self.act_orientation.triggered.connect(self.dock_bar._toggle_orientation)
        self.act_lock = menu.addAction("Toggle Lock Position")
        self.act_lock.triggered.connect(self.dock_bar._toggle_lock)
        self.act_top = menu.addAction("Toggle Always On Top")
        self.act_top.triggered.connect(self.dock_bar._toggle_on_top)
        menu.addSeparator()
        self.act_exit = menu.addAction("Exit")
        self.act_exit.triggered.connect(self._exit_app)

        self.setContextMenu(menu)

    def _refresh_menu_state(self) -> None:
        cfg = self.dock_bar.config
        self.act_lock.setText("Unlock Position" if cfg["locked"] else "Lock Position")
        self.act_top.setText("Disable Always On Top" if cfg["always_on_top"] else "Enable Always On Top")
        self.act_orientation.setText("Switch to Vertical" if cfg["orientation"] == "horizontal" else "Switch to Horizontal")
        self.act_show.setText("Hide Dock" if self.dock_bar.isVisible() else "Show Dock")

    def _toggle_dock_visibility(self) -> None:
        if self.dock_bar.isVisible(): self.dock_bar.hide()
        else:
            self.dock_bar.show()
            self.dock_bar.raise_()

    def _on_activated(self, reason) -> None:
        if reason == QSystemTrayIcon.Trigger: self._toggle_dock_visibility()

    def _exit_app(self) -> None:
        try: self.hide()
        except Exception: pass
        QApplication.instance().quit()

# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------
def main() -> int:
    app = QApplication(sys.argv)
    app.setQuitOnLastWindowClosed(False)
    app.setApplicationName("Dock Bar")

    dock = DockBar()
    dock.show()

    if QSystemTrayIcon.isSystemTrayAvailable():
        tray = TrayIcon(dock)
        tray.show()
        app._tray = tray

    return app.exec()

if __name__ == "__main__":
    sys.exit(main())
