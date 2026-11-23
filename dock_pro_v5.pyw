# dock_pro.pyw
# DockPro - Advanced floating Dock bar for Windows 11 (dark, macOS-style magnify)
# Requires:  pip install PySide6
# ---------------------------------------------------------------------------
# Highlights vs the old dock_bar.pyw:
#   * Icons are extracted in BACKGROUND THREADS (UI can never freeze,
#     and one bad shortcut can never break drag & drop)
#   * macOS-style cursor magnification wave across the bar
#   * Custom colors (background / border / accent) from Settings
#   * Correct IShellLinkW vtable (GetPath=4, GetIconLocation=18)
#   * Large crisp icons via SHGFI_ICONLOCATION + SHDefExtractIconW
#   * Atomic config saving + auto-migration from old dock_config.json
#   * Empty-state hint in Arabic: drag shortcuts onto the bar
#   * Insert-position indicator while dragging, reorder inside the dock
#   * System tray + floating dark settings dialog (live preview)
# ---------------------------------------------------------------------------

import os
import sys
import json
import time
import uuid
import ctypes
import traceback
from pathlib import Path

from PySide6.QtCore import (
    Qt, QPoint, QRect, QRectF, QMimeData,
    Property, QTimer, QObject, Signal, QRunnable, QThreadPool,
)
from PySide6.QtGui import (
    QIcon, QPixmap, QPainter, QColor, QPen, QImage, QPainterPath,
    QDrag, QImageReader,
)
from PySide6.QtWidgets import (
    QApplication, QWidget, QVBoxLayout, QHBoxLayout, QBoxLayout, QLayout,
    QPushButton, QDialog, QSlider, QCheckBox, QRadioButton, QButtonGroup,
    QListWidget, QListWidgetItem, QFileDialog, QMenu, QSystemTrayIcon,
    QMessageBox, QInputDialog, QDialogButtonBox, QGroupBox, QFormLayout,
    QColorDialog,
)

_IS_WIN = sys.platform == "win32"

# ---------------------------------------------------------------------------
# Logging (writes to dock_pro_debug.log when DOCK_BAR_DEBUG=1, pyw has no console)
# ---------------------------------------------------------------------------
_DEBUG = os.environ.get("DOCK_BAR_DEBUG", "") == "1"

def _log(msg: str) -> None:
    line = f"[{time.strftime('%H:%M:%S')}] {msg}"
    try:
        with open(_APP_DIR / "dock_pro_debug.log", "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception: pass
    if _DEBUG:
        try:
            print(f"[dockpro] {line}", file=sys.stderr, flush=True)
        except Exception: pass

# ---------------------------------------------------------------------------
# Constants & config
# ---------------------------------------------------------------------------
APP_DIR = Path(__file__).resolve().parent
_APP_DIR = APP_DIR
CONFIG_PATH = APP_DIR / "dock_pro_config.json"
OLD_CONFIG_PATH = APP_DIR / "dock_config.json"
MIME_INTERNAL = "application/x-dockpro-item"

DEFAULT_CONFIG = {
    "icon_size": 44,
    "bar_padding": 12,
    "spacing": 8,
    "opacity": 0.95,
    "orientation": "horizontal",
    "always_on_top": True,
    "locked": False,
    "magnify": True,
    "pos": None,
    "radius": 18,
    "bg_color": "#101216",
    "border_color": "#26282E",
    "accent": "#E86F2C",
    "items": [],
}

def load_config() -> dict:
    if not CONFIG_PATH.exists() and OLD_CONFIG_PATH.exists():
        try:
            merged = dict(DEFAULT_CONFIG)
            old = json.loads(OLD_CONFIG_PATH.read_text(encoding="utf-8"))
            if isinstance(old, dict):
                for k, v in old.items():
                    if k in merged or k == "items":
                        merged[k] = v
            write_config(merged)
            return merged
        except Exception: pass
    cfg = dict(DEFAULT_CONFIG)
    try:
        if CONFIG_PATH.exists():
            data = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                for k, v in data.items(): cfg[k] = v
    except Exception: pass
    return cfg

def write_config(cfg: dict) -> None:
    tmp = CONFIG_PATH.with_suffix(".tmp")
    try:
        tmp.write_text(json.dumps(cfg, indent=2, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, CONFIG_PATH)
    except Exception:
        try: tmp.unlink(missing_ok=True)
        except Exception: pass

def _expand(p: str) -> str:
    if not p: return ""
    try:
        return os.path.normpath(os.path.expandvars(os.path.expanduser(p)))
    except Exception:
        return p

# ---------------------------------------------------------------------------
# Win32: .lnk resolution (pure ctypes) -> (target, icon_location, icon_index)
# ---------------------------------------------------------------------------
def resolve_lnk(path: str, _depth: int = 0):
    if not _IS_WIN or _depth > 5: return "", "", 0
    try:
        from ctypes import wintypes

        class GUID(ctypes.Structure):
            _fields_ = [("Data1", wintypes.DWORD), ("Data2", wintypes.WORD), ("Data3", wintypes.WORD), ("Data4", ctypes.c_ubyte * 8)]

        def as_guid(u):
            g = GUID()
            g.Data1, g.Data2, g.Data3 = u.time_low, u.time_mid, u.time_hi_version
            for i, b in enumerate(u.bytes[8:]): g.Data4[i] = b
            return g

        CLSID_ShellLink = as_guid(uuid.UUID("{00021401-0000-0000-C000-000000000046}"))
        IID_IShellLinkW = as_guid(uuid.UUID("{000214F9-0000-0000-C000-000000000046}"))
        IID_IPersistFile = as_guid(uuid.UUID("{0000010b-0000-0000-C000-000000000046}"))

        ole32 = ctypes.windll.ole32
        pShellLink = ctypes.c_void_p()
        pPersistFile = ctypes.c_void_p()
        try:
            ole32.CoInitialize(None)
            if ole32.CoCreateInstance(ctypes.byref(CLSID_ShellLink), None, 1, ctypes.byref(IID_IShellLinkW), ctypes.byref(pShellLink)) != 0:
                return "", "", 0

            vtable = ctypes.cast(pShellLink, ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p)))
            QI = ctypes.WINFUNCTYPE(ctypes.c_long, ctypes.c_void_p, ctypes.POINTER(GUID), ctypes.POINTER(ctypes.c_void_p))(vtable[0][0])
            if QI(pShellLink, ctypes.byref(IID_IPersistFile), ctypes.byref(pPersistFile)) != 0:
                return "", "", 0

            vtable_pf = ctypes.cast(pPersistFile, ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p)))
            Load = ctypes.WINFUNCTYPE(ctypes.c_long, ctypes.c_void_p, ctypes.c_wchar_p, wintypes.DWORD)(vtable_pf[0][5])
            if Load(pPersistFile, str(path), 0) != 0:
                return "", "", 0

            vtable_sl = ctypes.cast(pShellLink, ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p)))
            # IShellLinkW vtable: QI=0, AddRef=1, Release=2, SetPath=3, GetPath=4,
            # ..., SetIconLocation=17, GetIconLocation=18
            GetPath = ctypes.WINFUNCTYPE(ctypes.c_long, ctypes.c_void_p, ctypes.c_wchar_p, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD)(vtable_sl[0][4])
            tb = ctypes.create_unicode_buffer(1024)
            GetPath(pShellLink, tb, 1024, None, 0)
            target = _expand(tb.value)

            GetIconLocation = ctypes.WINFUNCTYPE(ctypes.c_long, ctypes.c_void_p, ctypes.c_wchar_p, ctypes.c_int, ctypes.POINTER(ctypes.c_int))(vtable_sl[0][18])
            ib = ctypes.create_unicode_buffer(1024)
            idx = ctypes.c_int(0)
            GetIconLocation(pShellLink, ib, 1024, ctypes.byref(idx))
            icon_path = _expand(ib.value)
            if icon_path and not os.path.isabs(icon_path):
                icon_path = _expand(os.path.join(os.path.dirname(path), icon_path))

            return target, icon_path, idx.value
        finally:
            for ptr in (pPersistFile, pShellLink):
                vt = ctypes.cast(ptr, ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p)))
                Release = ctypes.WINFUNCTYPE(ctypes.c_ulong, ctypes.c_void_p)(vt[0][2])
                Release(ptr)
            ole32.CoUninitialize()
    except Exception as e:
        _log(f"resolve_lnk error {path!r}: {e}")
        return "", "", 0

# ---------------------------------------------------------------------------
# Win32: icon extraction -> QImage (thread-safe; QPixmap created on GUI thread)
# ---------------------------------------------------------------------------
SHGFI_ICON         = 0x000000100
SHGFI_LARGEICON    = 0x000000000
SHGFI_SMALLICON    = 0x000000001
SHGFI_ICONLOCATION = 0x000000400

SIIGBF_BIGGERSIZEOK = 0x00000001
SIIGBF_ICONONLY     = 0x00000004

def _mk_guid(s):
    import uuid
    class _G(ctypes.Structure):
        _fields_ = [("Data1", ctypes.c_ulong), ("Data2", ctypes.c_ushort),
                    ("Data3", ctypes.c_ushort), ("Data4", ctypes.c_ubyte * 8)]
    u = uuid.UUID(s)
    g = _G()
    g.Data1, g.Data2, g.Data3 = u.time_low, u.time_mid, u.time_hi_version
    for i, b in enumerate(u.bytes[8:]):
        g.Data4[i] = b
    return g

IID_SHELL_IMAGE_FACTORY = "{bcc18b79-ba16-442f-80c4-8a59c30c463b}"

def _hicon_to_image(hicon):
    try:
        from ctypes import wintypes

        class ICONINFO(ctypes.Structure):
            _fields_ = [("fIcon", wintypes.BOOL), ("xHotspot", wintypes.DWORD),
                        ("yHotspot", wintypes.DWORD), ("hbmMask", wintypes.HBITMAP),
                        ("hbmColor", wintypes.HBITMAP)]

        class BITMAP(ctypes.Structure):
            _fields_ = [("bmType", ctypes.c_long), ("bmWidth", ctypes.c_long),
                        ("bmHeight", ctypes.c_long), ("bmWidthBytes", ctypes.c_long),
                        ("bmPlanes", wintypes.WORD), ("bmBitsPixel", wintypes.WORD),
                        ("bmBits", ctypes.c_void_p)]

        class BITMAPINFOHEADER(ctypes.Structure):
            _fields_ = [("biSize", wintypes.DWORD), ("biWidth", ctypes.c_long),
                        ("biHeight", ctypes.c_long), ("biPlanes", wintypes.WORD),
                        ("biBitCount", wintypes.WORD), ("biCompression", wintypes.DWORD),
                        ("biSizeImage", wintypes.DWORD), ("biXPelsPerMeter", ctypes.c_long),
                        ("biYPelsPerMeter", ctypes.c_long), ("biClrUsed", wintypes.DWORD),
                        ("biClrImportant", wintypes.DWORD)]

        class BITMAPINFO(ctypes.Structure):
            _fields_ = [("bmiHeader", BITMAPINFOHEADER), ("bmiColors", ctypes.c_ubyte * 256)]

        user32 = ctypes.windll.user32
        gdi32 = ctypes.windll.gdi32

        gdi32.CreateCompatibleDC.restype = wintypes.HDC
        gdi32.CreateCompatibleDC.argtypes = [wintypes.HDC]
        gdi32.GetObjectW.restype = ctypes.c_int
        gdi32.GetObjectW.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p]
        gdi32.GetDIBits.restype = ctypes.c_int
        gdi32.GetDIBits.argtypes = [wintypes.HDC, wintypes.HBITMAP, wintypes.UINT, wintypes.UINT,
                                    ctypes.c_void_p, ctypes.c_void_p, wintypes.UINT]
        gdi32.DeleteObject.argtypes = [wintypes.HANDLE]
        gdi32.DeleteDC.argtypes = [wintypes.HDC]
        user32.GetIconInfo.restype = wintypes.BOOL
        user32.GetIconInfo.argtypes = [wintypes.HICON, ctypes.POINTER(ICONINFO)]

        info = ICONINFO()
        if not user32.GetIconInfo(wintypes.HICON(hicon), ctypes.byref(info)):
            return None
        try:
            bm = BITMAP()
            w = h = 0
            if info.hbmColor and gdi32.GetObjectW(wintypes.HBITMAP(info.hbmColor),
                                                  ctypes.sizeof(BITMAP), ctypes.byref(bm)):
                w, h = bm.bmWidth, bm.bmHeight
            if w <= 0 or w > 512 or h <= 0 or h > 512:
                return None
            stride = w * 4
            buf = (ctypes.c_ubyte * (stride * h))()
            bmi = BITMAPINFO()
            bmi.bmiHeader.biSize = ctypes.sizeof(BITMAPINFOHEADER)
            bmi.bmiHeader.biWidth = w
            bmi.bmiHeader.biHeight = -h
            bmi.bmiHeader.biPlanes = 1
            bmi.bmiHeader.biBitCount = 32
            bmi.bmiHeader.biCompression = 0
            dc = gdi32.CreateCompatibleDC(None)
            lines = gdi32.GetDIBits(dc, wintypes.HBITMAP(info.hbmColor), 0, h,
                                    buf, ctypes.byref(bmi), 0)
            gdi32.DeleteDC(dc)
            if lines != h:
                return None
            data = bytearray(buf)
            if not any(data[3::4]):
                mstride = ((w + 31) // 32) * 4
                mbuf = (ctypes.c_ubyte * (mstride * h))()
                bmi2 = BITMAPINFO()
                bmi2.bmiHeader.biSize = ctypes.sizeof(BITMAPINFOHEADER)
                bmi2.bmiHeader.biWidth = w
                bmi2.bmiHeader.biHeight = -h
                bmi2.bmiHeader.biPlanes = 1
                bmi2.bmiHeader.biBitCount = 1
                bmi2.bmiHeader.biCompression = 0
                dc = gdi32.CreateCompatibleDC(None)
                mlines = gdi32.GetDIBits(dc, wintypes.HBITMAP(info.hbmMask), 0, h,
                                         mbuf, ctypes.byref(bmi2), 0)
                gdi32.DeleteDC(dc)
                if mlines == h:
                    mask = bytes(mbuf)
                    for y in range(h):
                        row = mask[y * mstride:(y + 1) * mstride]
                        base = y * stride + 3
                        for x in range(w):
                            a = (row[x >> 3] >> (7 - (x & 7))) & 1
                            data[base + x * 4] = 0 if a else 255
                else:
                    for i in range(3, len(data), 4):
                        data[i] = 255
            img = QImage(bytes(data), w, h, stride, QImage.Format_ARGB32).copy()
            return None if img.isNull() else img
        finally:
            gdi32.DeleteObject(wintypes.HBITMAP(info.hbmMask))
            if info.hbmColor:
                gdi32.DeleteObject(wintypes.HBITMAP(info.hbmColor))
    except Exception as e:
        _log(f"_hicon_to_image error: {e}")
        return None

def _fix_img(img, size):
    if img is None or img.isNull(): return None
    i = img.convertToFormat(QImage.Format_ARGB32)
    if i.width() == size and i.height() == size: return i
    return i.scaled(size, size, Qt.KeepAspectRatio, Qt.SmoothTransformation)

def _extract_from_file(path: str, size: int, index: int = 0):
    try:
        from ctypes import wintypes
        shell32 = ctypes.windll.shell32
        user32 = ctypes.windll.user32
        large = wintypes.HICON()
        small = wintypes.HICON()
        r = shell32.SHDefExtractIconW(ctypes.c_wchar_p(path), int(index), 0,
                                      ctypes.byref(large), ctypes.byref(small),
                                      (size & 0xFFFF) | ((size & 0xFFFF) << 16))
        hicon = large.value or small.value
        if not hicon and r != 0:
            shell32.ExtractIconExW(str(path), int(index), ctypes.byref(large), ctypes.byref(small), 1)
            hicon = large.value or small.value
        if not hicon: return None
        try:
            return _fix_img(_hicon_to_image(hicon), size)
        finally:
            user32.DestroyIcon(hicon)
    except Exception as e:
        _log(f"_extract_from_file error {path!r}: {e}")
        return None

class _SHFILEINFO(ctypes.Structure):
    _fields_ = [
        ("hIcon", ctypes.c_void_p), ("iIcon", ctypes.c_int), ("dwAttributes", ctypes.c_uint),
        ("szDisplayName", ctypes.c_wchar * 1024), ("szTypeName", ctypes.c_wchar * 80),
    ]

def _shgetfileinfo(path: str, flags: int):
    shell32 = ctypes.windll.shell32
    info = _SHFILEINFO()
    res = shell32.SHGetFileInfoW(ctypes.c_wchar_p(path), 0, ctypes.byref(info), ctypes.sizeof(info), flags)
    return res, info

def _type_icon_by_location(path: str, size: int):
    res, info = _shgetfileinfo(path, SHGFI_ICONLOCATION)
    if not res: return None
    loc = (info.szDisplayName or "").strip().strip('"')
    idx = int(info.iIcon)
    if loc and not os.path.exists(_expand(loc)) and "," in loc:
        base, _, tail = loc.rpartition(",")
        tail = tail.strip()
        if tail.lstrip("-").isdigit():
            loc, idx = base.strip().strip('"'), int(tail)
    loc = _expand(loc or "")
    if not loc or not os.path.exists(loc): return None
    img = _extract_from_file(loc, size, idx)
    if img is not None and not img.isNull(): return img
    img = _extract_from_file(loc, min(size, 48), idx)
    return img if img is not None and not img.isNull() else None

def _shell_small_icon(path: str, size: int):
    flags = SHGFI_ICON | (SHGFI_LARGEICON if size >= 32 else SHGFI_SMALLICON)
    try:
        res, info = _shgetfileinfo(path, flags)
        if not res or not info.hIcon: return None
        try:
            return _fix_img(_hicon_to_image(info.hIcon), size)
        finally:
            ctypes.windll.user32.DestroyIcon(info.hIcon)
    except Exception:
        return None

def _hbitmap_to_image(hbm):
    try:
        from ctypes import wintypes

        class BITMAP(ctypes.Structure):
            _fields_ = [("bmType", ctypes.c_long), ("bmWidth", ctypes.c_long),
                        ("bmHeight", ctypes.c_long), ("bmWidthBytes", ctypes.c_long),
                        ("bmPlanes", wintypes.WORD), ("bmBitsPixel", wintypes.WORD),
                        ("bmBits", ctypes.c_void_p)]

        class BITMAPINFOHEADER(ctypes.Structure):
            _fields_ = [("biSize", wintypes.DWORD), ("biWidth", ctypes.c_long),
                        ("biHeight", ctypes.c_long), ("biPlanes", wintypes.WORD),
                        ("biBitCount", wintypes.WORD), ("biCompression", wintypes.DWORD),
                        ("biSizeImage", wintypes.DWORD), ("biXPelsPerMeter", ctypes.c_long),
                        ("biYPelsPerMeter", ctypes.c_long), ("biClrUsed", wintypes.DWORD),
                        ("biClrImportant", wintypes.DWORD)]

        class BITMAPINFO(ctypes.Structure):
            _fields_ = [("bmiHeader", BITMAPINFOHEADER), ("bmiColors", ctypes.c_ubyte * 256)]

        gdi32 = ctypes.windll.gdi32
        gdi32.CreateCompatibleDC.restype = wintypes.HDC
        gdi32.CreateCompatibleDC.argtypes = [wintypes.HDC]
        gdi32.GetObjectW.restype = ctypes.c_int
        gdi32.GetObjectW.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p]
        gdi32.GetDIBits.restype = ctypes.c_int
        gdi32.GetDIBits.argtypes = [wintypes.HDC, wintypes.HBITMAP, wintypes.UINT, wintypes.UINT,
                                    ctypes.c_void_p, ctypes.c_void_p, wintypes.UINT]
        gdi32.DeleteDC.argtypes = [wintypes.HDC]

        bm = BITMAP()
        if not gdi32.GetObjectW(wintypes.HBITMAP(hbm), ctypes.sizeof(BITMAP), ctypes.byref(bm)):
            return None
        w, h = bm.bmWidth, abs(bm.bmHeight)
        if w <= 0 or w > 1024 or h <= 0 or h > 1024:
            return None
        stride = w * 4
        buf = (ctypes.c_ubyte * (stride * h))()
        bmi = BITMAPINFO()
        bmi.bmiHeader.biSize = ctypes.sizeof(BITMAPINFOHEADER)
        bmi.bmiHeader.biWidth = w
        bmi.bmiHeader.biHeight = -h
        bmi.bmiHeader.biPlanes = 1
        bmi.bmiHeader.biBitCount = 32
        bmi.bmiHeader.biCompression = 0
        dc = gdi32.CreateCompatibleDC(None)
        lines = gdi32.GetDIBits(dc, wintypes.HBITMAP(hbm), 0, h, buf, ctypes.byref(bmi), 0)
        gdi32.DeleteDC(dc)
        if lines != h:
            return None
        data = bytearray(buf)
        if not any(data[3::4]):
            _log(f"_hbitmap_to_image: no alpha, rejected {w}x{h}")
            return None
        img = QImage(bytes(data), w, h, stride, QImage.Format_ARGB32).copy()
        return None if img.isNull() else img
    except Exception as e:
        _log(f"_hbitmap_to_image error: {e}")
        return None

def _shell_item_image(path: str, size: int):
    """IShellItemImageFactory::GetImage with SIIGBF_ICONONLY.
    Returns the real icon at (near-)requested size WITHOUT the shortcut
    arrow overlay and WITHOUT thumbnail downscaling."""
    if not _IS_WIN: return None
    try:
        from ctypes import wintypes

        class _SIZE(ctypes.Structure):
            _fields_ = [("cx", ctypes.c_long), ("cy", ctypes.c_long)]

        shell32 = ctypes.windll.shell32
        gdi32 = ctypes.windll.gdi32
        gdi32.DeleteObject.argtypes = [wintypes.HANDLE]
        shell32.SHCreateItemFromParsingName.restype = ctypes.c_long
        shell32.SHCreateItemFromParsingName.argtypes = [
            ctypes.c_wchar_p, ctypes.c_void_p, ctypes.c_void_p, ctypes.POINTER(ctypes.c_void_p)]

        punk = ctypes.c_void_p()
        hr = shell32.SHCreateItemFromParsingName(
            str(path), None, ctypes.byref(_mk_guid(IID_SHELL_IMAGE_FACTORY)), ctypes.byref(punk))
        if hr != 0 or not punk: return None
        try:
            vt = ctypes.cast(punk, ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p)))
            GetImage = ctypes.WINFUNCTYPE(
                ctypes.c_long, ctypes.c_void_p, _SIZE, ctypes.c_uint,
                ctypes.POINTER(wintypes.HBITMAP))(vt[0][3])
            hbm = wintypes.HBITMAP()
            hr = GetImage(punk, _SIZE(int(size), int(size)),
                          SIIGBF_ICONONLY | SIIGBF_BIGGERSIZEOK, ctypes.byref(hbm))
            if hr != 0 or not hbm.value: return None
            try:
                img = _hbitmap_to_image(hbm.value)
            finally:
                gdi32.DeleteObject(wintypes.HBITMAP(hbm.value))
            if img is None or img.isNull(): return None
            return _fix_img(img, size)
        finally:
            vp = ctypes.cast(punk, ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p)))
            Release = ctypes.WINFUNCTYPE(ctypes.c_ulong, ctypes.c_void_p)(vp[0][2])
            Release(punk)
    except Exception as e:
        _log(f"_shell_item_image error {path!r}: {e}")
        return None

def _image_reader_icon(path: str, size: int):
    low = path.lower()
    if not low.endswith((".ico", ".cur", ".png", ".jpg", ".jpeg", ".bmp", ".gif", ".webp", ".svg")):
        return None
    try:
        reader = QImageReader(path)
        reader.setAutoTransform(True)
        if not reader.canRead(): return None
        img = reader.read()
        return None if img.isNull() else img
    except Exception:
        return None

def extract_icon(path: str, size: int = 96):
    path = _expand(str(path))
    if not path or not os.path.exists(path): return None
    img = _image_reader_icon(path, size)
    if img is not None and not img.isNull(): return img

    if not _IS_WIN: return None

    target, icon_loc, icon_idx = "", "", 0
    if path.lower().endswith(".lnk"):
        target, icon_loc, icon_idx = resolve_lnk(path)

        # 1. Target first: its own icon never carries the shortcut arrow
        if target:
            t = _expand(target)
            if os.path.isdir(t) or os.path.isfile(t):
                img = _shell_item_image(t, size) or _type_icon_by_location(t, size)
                if img is not None and not img.isNull(): return img
                img = _shell_small_icon(t, size)
                if img is not None and not img.isNull(): return img

        # 2. Raw embedded icon of the .lnk (no overlay from SHDefExtractIconW)
        if icon_loc and os.path.exists(icon_loc):
            img = _extract_from_file(icon_loc, size, icon_idx) or _type_icon_by_location(icon_loc, size)
            if img is not None and not img.isNull(): return img

        img = _type_icon_by_location(path, size)
        if img is not None and not img.isNull(): return img

        # 3. Modern factory with SIIGBF_ICONONLY -> icon WITHOUT shortcut arrow
        img = _shell_item_image(path, size)
        if img is not None and not img.isNull(): return img

        # 4. Raw extraction from the target file as last resort
        if target and os.path.exists(target):
            img = _extract_from_file(target, size, 0)
            if img is not None and not img.isNull(): return img
    else:
        if os.path.isdir(path):
            img = _shell_item_image(path, size) or _type_icon_by_location(path, size) or _shell_small_icon(path, size)
        else:
            img = (_extract_from_file(path, size, 0) or _type_icon_by_location(path, size)
                   or _shell_item_image(path, size) or _shell_small_icon(path, size))
        if img is not None and not img.isNull(): return img

    return None

# ---------------------------------------------------------------------------
# Background icon pipeline (thread-safe UI)
# ---------------------------------------------------------------------------
class _IconBus(QObject):
    got = Signal(str, int, QImage)

ICON_BUS = _IconBus()
_ICON_CACHE = {}
_ICON_PENDING = set()

def push_icon(path: str, size: int) -> tuple:
    key = (os.path.normcase(path), size)
    if key in _ICON_CACHE: return True, _ICON_CACHE[key]
    if key in _ICON_PENDING: return False, None
    _ICON_PENDING.add(key)
    QThreadPool.globalInstance().start(_IconJob(path, size))
    return False, None

class _IconJob(QRunnable):
    def __init__(self, path: str, size: int):
        super().__init__()
        self.path, self.size = str(path), int(size)
        self.setAutoDelete(True)

    def run(self):
        img = None
        try:
            if _IS_WIN: ctypes.windll.ole32.CoInitialize(None)
            img = extract_icon(self.path, self.size)
        except Exception as e:
            _log(f"icon job error {self.path!r}: {e}")
        finally:
            _log(f"icon {self.path!r} size={self.size} -> {'ok' if img is not None and not img.isNull() else 'NONE'}")
            try: ICON_BUS.got.emit(self.path, self.size, img or QImage())
            except Exception: pass
            if _IS_WIN:
                try: ctypes.windll.ole32.CoUninitialize()
                except Exception: pass

# ---------------------------------------------------------------------------
# Item model
# ---------------------------------------------------------------------------
class DockItem:
    def __init__(self, path: str, name: str = None, args: str = ""):
        self.path = str(path)
        self.name = name or (Path(self.path).stem or "?")
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
        self._target = 1.0
        self._hovered = False
        self._pixmap = None
        self._press_pos = None
        self._last_size = -1

        self.setMouseTracking(True)
        self.setCursor(Qt.PointingHandCursor)
        self.setAcceptDrops(True)
        self.setFixedSize(self._side(), self._side())
        self.setToolTip(f"{item.name}\n{item.path}")

    def _side(self) -> int:
        return self.dock_bar.config["icon_size"] + self.dock_bar.config.get("bar_padding", 12) * 2

    def _get_scale(self) -> float: return self._scale
    def _set_scale(self, v: float) -> None:
        self._scale = max(1.0, min(1.6, v))
        self.update()
    scale = Property(float, _get_scale, _set_scale)

    def refresh(self) -> None:
        self.setFixedSize(self._side(), self._side())
        self._request_icon(force=True)
        self.update()

    def _request_icon(self, force: bool = False) -> None:
        size = self.dock_bar.config["icon_size"] * 2
        if not force and size == self._last_size: return
        self._last_size = size
        self._pixmap = None
        has, img = push_icon(self.item.path, size)
        if has and img is not None and not img.isNull():
            self._pixmap = QPixmap.fromImage(img)

    def set_icon(self, img: QImage) -> None:
        if img is not None and not img.isNull():
            self._pixmap = QPixmap.fromImage(img)
            self.update()

    # ---------------- painting ----------------
    def paintEvent(self, event) -> None:
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing, True)
        p.setRenderHint(QPainter.SmoothPixmapTransform, True)
        rect = self.rect()
        icon_size = self.dock_bar.config["icon_size"]
        draw_size = max(1, int(round(icon_size * self._scale)))
        if self._hovered: draw_size = int(draw_size * 1.06)

        if self._pixmap is not None and not self._pixmap.isNull():
            x = (rect.width() - draw_size) // 2
            y = (rect.height() - draw_size) // 2 - (2 if self._hovered else 0)
            p.drawPixmap(QRect(x, y, draw_size, draw_size), self._pixmap)
        else:
            x = (rect.width() - draw_size) // 2
            y = (rect.height() - draw_size) // 2
            p.setPen(Qt.NoPen)
            p.setBrush(QColor(255, 255, 255, 18))
            p.drawEllipse(QRectF(x, y, draw_size, draw_size))
            accent = QColor(self.dock_bar.config["accent"])
            accent.setAlpha(180)
            p.setPen(QPen(accent))
            f = p.font()
            f.setPointSize(max(8, draw_size // 3)); f.setBold(True); p.setFont(f)
            letter = (self.item.name or "?")[:1].upper()
            p.drawText(QRectF(x, y, draw_size, draw_size), Qt.AlignCenter, letter)

        if self._hovered:
            color = QColor(self.dock_bar.config["accent"])
            pen = QPen(color, 2); pen.setCapStyle(Qt.RoundCap)
            p.setPen(pen)
            if self.dock_bar.config["orientation"] == "horizontal":
                y = rect.height() - 3
                p.drawLine(int(rect.width() * 0.25), y, int(rect.width() * 0.75), y)
            else:
                x = rect.width() - 3
                p.drawLine(x, int(rect.height() * 0.25), x, int(rect.height() * 0.75))
        p.end()

    # ---------------- mouse ----------------
    def enterEvent(self, event) -> None:
        self.raise_()
        self._hovered = True
        self.update()

    def leaveEvent(self, event) -> None:
        self._hovered = False
        self._target = 1.0
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
        self.dock_bar._update_magnification(event.globalPosition().toPoint())
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
        pm = self._pixmap
        if pm is not None and not pm.isNull():
            drag.setPixmap(pm)
            drag.setHotSpot(QPoint(pm.width() // 2, pm.height() // 2))
        else:
            ph = QPixmap(self.width(), self.height())
            ph.fill(Qt.transparent)
            drag.setPixmap(ph)
            drag.setHotSpot(self.rect().center())
        drag.exec(Qt.MoveAction)

    def _show_context_menu(self, global_pos: QPoint) -> None:
        menu = QMenu(self)
        menu.setStyleSheet(STYLESHEET)
        a_launch = menu.addAction("Launch")
        a_open = menu.addAction("Open File Location")
        a_rename = menu.addAction("Rename\u2026")
        menu.addSeparator()
        a_remove = menu.addAction("Remove from Dock")
        chosen = menu.exec(global_pos)
        if chosen == a_launch: self.dock_bar.launch_item(self.item)
        elif chosen == a_open: self.dock_bar.open_location(self.item)
        elif chosen == a_rename: self.dock_bar.rename_item(self.item)
        elif chosen == a_remove: self.dock_bar.remove_item(self.item)

    # ---------------- drop-through (bar handles everything) ----------------
    def dragEnterEvent(self, event) -> None: self.dock_bar._handle_drag_enter(event, self)
    def dragMoveEvent(self, event) -> None: self.dock_bar._handle_drag_move(event, self)
    def dropEvent(self, event) -> None: self.dock_bar._handle_drop(event, self)
    def dragLeaveEvent(self, event) -> None:
        self.dock_bar._drop_index = -1
        self.dock_bar.update()

STYLESHEET = """
QDialog, QMessageBox, QInputDialog, QFileDialog {
    background-color: #15171B; color: #E2D8B5;
    font-family: 'Segoe UI', sans-serif; font-size: 9pt;
}
QLabel { color: #C8C8C8; background: transparent; }
QGroupBox {
    border: 1px solid #2A2C30; border-radius: 8px; margin-top: 10px;
    padding-top: 10px; color: #E2D8B5; font-weight: 600; background: transparent;
}
QGroupBox::title { subcontrol-origin: margin; left: 10px; padding: 0 4px; color: #E2D8B5; }
QSlider::groove:horizontal { height: 4px; background: #2A2C30; border-radius: 2px; }
QSlider::handle:horizontal { background: #E2D8B5; width: 14px; margin: -6px 0; border-radius: 7px; }
QSlider::handle:horizontal:hover { background: #E86F2C; }
QCheckBox, QRadioButton { color: #C8C8C8; }
QPushButton {
    background-color: #1E2025; color: #E2D8B5; border: 1px solid #2A2C30;
    padding: 6px 14px; border-radius: 6px;
}
QPushButton:hover { border-color: #E86F2C; color: #E86F2C; }
QPushButton:pressed { background-color: #E86F2C; color: #111215; }
QListWidget {
    background-color: #1A1C20; border: 1px solid #2A2C30;
    border-radius: 6px; color: #C8C8C8;
}
QListWidget::item { padding: 4px 8px; }
QListWidget::item:selected { background-color: #E86F2C; color: #111215; }
QMenu {
    background-color: #1A1C20; color: #E2D8B5; border: 1px solid #2A2C30;
    border-radius: 6px; padding: 4px;
}
QMenu::item { padding: 6px 24px; border-radius: 4px; }
QMenu::item:selected { background-color: #E86F2C; color: #111215; }
QMenu::separator { height: 1px; background-color: #2A2C30; margin: 4px 8px; }
"""

# ---------------------------------------------------------------------------
# Dock bar
# ---------------------------------------------------------------------------
class DockBar(QWidget):
    def __init__(self):
        super().__init__()
        self.config = load_config()
        self.items = [DockItem.from_dict(d) for d in self.config.get("items", [])
                      if isinstance(d, dict) and d.get("path")]

        self.setWindowFlags(Qt.FramelessWindowHint | Qt.Tool | Qt.WindowStaysOnTopHint)
        self.setAttribute(Qt.WA_TranslucentBackground, True)
        self.setAcceptDrops(True)
        self.setMouseTracking(True)

        self._layout = QBoxLayout(QBoxLayout.LeftToRight, self)
        self._layout.setContentsMargins(6, 6, 6, 6)
        self._layout.setSizeConstraint(QLayout.SetFixedSize)

        self.buttons = []
        self._dragging_window = False
        self._drag_offset = QPoint()
        self._drop_index = -1

        ICON_BUS.got.connect(self._on_icon_ready)
        self._mag_timer = QTimer(self)
        self._mag_timer.setInterval(16)
        self._mag_timer.timeout.connect(self._mag_step)

        self._rebuild()
        self._apply_flags()
        self._restore_position()
        QTimer.singleShot(0, lambda: write_config(self.config))

    # ---------------- icons (async) ----------------
    def _on_icon_ready(self, path: str, size: int, img: QImage) -> None:
        key = (os.path.normcase(path), int(size))
        _ICON_PENDING.discard(key)
        if img is not None and not img.isNull():
            _ICON_CACHE[key] = img
            if len(_ICON_CACHE) > 400:
                for k in list(_ICON_CACHE.keys())[:100]: _ICON_CACHE.pop(k, None)
        else:
            _ICON_CACHE[key] = None
        for b in self.buttons:
            try:
                if os.path.normcase(b.item.path) == key[0] and b._last_size == int(size):
                    b.set_icon(img)
            except Exception: pass

    # ---------------- magnification wave ----------------
    def _update_magnification(self, gpos: QPoint) -> None:
        if not self.config.get("magnify", True) or not self.buttons:
            self._reset_magnification(); return
        horiz = self.config["orientation"] == "horizontal"
        radius = self.buttons[0].width() * 1.9
        for b in self.buttons:
            c = b.geometry().center()
            d = (gpos.x() - c.x()) if horiz else (gpos.y() - c.y())
            ad = abs(d)
            if ad > radius:
                b._target = 1.0; continue
            t = 1.0 - (ad / radius)
            b._target = 1.0 + 0.30 * t * t
        if not self._mag_timer.isActive(): self._mag_timer.start()

    def _reset_magnification(self) -> None:
        for b in self.buttons: b._target = 1.0
        if not self._mag_timer.isActive(): self._mag_timer.start()

    def _mag_step(self) -> None:
        done = True
        for b in self.buttons:
            target = b._target
            cur = b.scale
            if abs(cur - target) < 0.005:
                if cur != target: b._set_scale(target)
            else:
                done = False
                b._set_scale(cur + (target - cur) * 0.35)
        if done: self._mag_timer.stop()

    def leaveEvent(self, event) -> None:
        self._reset_magnification()
        self._drop_index = -1
        self.update()

    def mouseMoveEvent(self, event) -> None:
        if self._dragging_window and not self.config["locked"]:
            self.move(event.globalPosition().toPoint() - self._drag_offset)
            event.accept()
        else:
            self._update_magnification(event.globalPosition().toPoint())

    def wheelEvent(self, event) -> None:
        if event.modifiers() == Qt.ControlModifier:
            delta = event.angleDelta().y()
            if delta > 0: new_size = min(96, self.config["icon_size"] + 2)
            else: new_size = max(24, self.config["icon_size"] - 2)
            if new_size != self.config["icon_size"]:
                self.config["icon_size"] = new_size
                self._refresh_sizes()
                write_config(self.config)
            event.accept()
        else:
            super().wheelEvent(event)

    # ---------------- items ----------------
    def launch_item(self, item: DockItem) -> None:
        try:
            path = item.path
            if not os.path.exists(path):
                QMessageBox.warning(self, "DockPro", f"Path not found:\n{path}")
                return
            if item.args and os.path.isfile(path) and path.lower().endswith(".exe"):
                import shlex, subprocess
                subprocess.Popen([path, *shlex.split(item.args)], close_fds=True)
            else:
                os.startfile(path)
        except Exception as e:
            QMessageBox.warning(self, "DockPro", f"Failed to launch:\n{e}")

    def open_location(self, item: DockItem) -> None:
        try:
            path = item.path
            if os.path.isdir(path): os.startfile(path)
            elif os.path.exists(path): os.startfile(os.path.dirname(path) or ".")
        except Exception: pass

    def rename_item(self, item: DockItem) -> None:
        text, ok = QInputDialog.getText(self, "Rename", "Display name:", text=item.name)
        if ok and text.strip():
            item.name = text.strip()
            self._save()
            for b in self.buttons:
                if b.item is item: b.setToolTip(f"{item.name}\n{item.path}")

    def remove_item(self, item: DockItem) -> None:
        if item in self.items:
            self.items.remove(item)
            self._save()
            self._rebuild_soon()

    def add_item(self, path: str, index: int = None) -> bool:
        try: path = os.path.abspath(path)
        except Exception: return False
        if not os.path.exists(path): return False
        norm = os.path.normcase(path)
        if any(os.path.normcase(it.path) == norm for it in self.items): return False
        item = DockItem(path)
        if index is None or index < 0 or index > len(self.items):
            self.items.append(item)
        else:
            self.items.insert(index, item)
        self._save()
        self._rebuild_soon()
        return True

    def move_item(self, src: int, dst: int) -> None:
        if src < 0 or src >= len(self.items): return
        dst = max(0, min(dst, len(self.items)))
        if dst in (src, src + 1): return
        item = self.items.pop(src)
        if dst > src: dst -= 1
        self.items.insert(dst, item)
        self._save()
        self._rebuild_soon()

    def _save(self) -> None:
        self.config["items"] = [it.to_dict() for it in self.items]
        write_config(self.config)

    # ---------------- layout ----------------
    def _rebuild(self) -> None:
        _log(f"rebuild: {len(self.items)} items")
        while self._layout.count():
            it = self._layout.takeAt(0)
            w = it.widget()
            if w is not None:
                w.deleteLater()
        self.buttons = []

        self._layout.setDirection(QBoxLayout.LeftToRight if self.config["orientation"] == "horizontal"
                                  else QBoxLayout.TopToBottom)
        self._layout.setSpacing(self.config["spacing"])
        self._layout.setContentsMargins(6, 6, 6, 6)

        for item in self.items:
            try:
                btn = DockButton(item, self)
                btn._request_icon()
            except Exception:
                _log(f"button build failed {item.path!r}:\n{traceback.format_exc()}")
                continue
            self.buttons.append(btn)
            self._layout.addWidget(btn)

        side = self.buttons[0].height() if self.buttons else self.config["icon_size"] + self.config.get("bar_padding", 12) * 2
        if not self.buttons:
            spacer = QWidget(self)
            if self.config["orientation"] == "horizontal":
                spacer.setFixedSize(150, side)
            else:
                spacer.setFixedSize(side, 60)
            self._layout.addWidget(spacer)

        if self.config["orientation"] == "horizontal":
            self.setMinimumSize(160, 0)
            self.setMaximumHeight(side + 12)
        else:
            self.setMinimumSize(0, 60)
            self.setMaximumWidth(side + 12)

        self._layout.activate()
        self.adjustSize()
        self.resize(self.sizeHint())

    def _rebuild_soon(self) -> None:
        # NEVER rebuild synchronously inside a drop/mouse event: the receiving
        # widget must survive its own event first.
        if getattr(self, "_rebuild_pending", False): return
        self._rebuild_pending = True
        QTimer.singleShot(0, self._rebuild_now)

    def _rebuild_now(self) -> None:
        self._rebuild_pending = False
        self._rebuild()

    def _refresh_sizes(self) -> None:
        self._rebuild_soon()

    def _apply_flags(self) -> None:
        self.setWindowOpacity(max(0.15, min(1.0, self.config["opacity"])))
        flags = Qt.FramelessWindowHint | Qt.Tool
        if self.config["always_on_top"]: flags |= Qt.WindowStaysOnTopHint
        if self.windowFlags() != flags:
            self.setWindowFlags(flags)
            if self.isVisible(): self.show()

    def apply_live(self) -> None:
        self._apply_flags()
        self._rebuild()

    def _restore_position(self) -> None:
        self.adjustSize()
        pos = self.config.get("pos")
        if isinstance(pos, (list, tuple)) and len(pos) == 2:
            try:
                self.move(int(pos[0]), int(pos[1]))
                if QApplication.screenAt(QPoint(int(pos[0]), int(pos[1]))) is None:
                    self._default_position()
                return
            except Exception: pass
        self._default_position()

    def _default_position(self) -> None:
        screen = QApplication.primaryScreen()
        if screen is None: return
        geo = screen.availableGeometry()
        self.adjustSize()
        x = geo.x() + (geo.width() - self.width()) // 2
        y = geo.y() + geo.height() - self.height() - 18
        self.move(x, y)

    # ---------------- painting ----------------
    def paintEvent(self, event) -> None:
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing, True)
        border_w = 2 if self._drop_index >= 0 else 1
        outer = QRectF(self.rect()).adjusted(1, 1, -1, -1)
        path = QPainterPath()
        path.addRoundedRect(outer, float(self.config.get("radius", 18)), float(self.config.get("radius", 18)))
        p.fillPath(path, QColor(self.config["bg_color"]))
        p.setPen(QPen(QColor(self.config["accent"] if self._drop_index >= 0 else self.config["border_color"]), border_w))
        p.drawPath(path)

        if not self.buttons:
            p.setPen(QPen(QColor(226, 216, 181, 110)))
            f = p.font(); f.setPointSize(9); p.setFont(f)
            p.drawText(self.rect(), Qt.AlignCenter, "\u0627\u0633\u062d\u0628 \u0627\u0644\u0627\u062e\u062a\u0635\u0627\u0631\u0627\u062a \u0625\u0644\u0649 \u0647\u0646\u0627")
        elif self._drop_index >= 0:
            pt = self._insert_line_pos(self._drop_index)
            pen = QPen(QColor(self.config["accent"]), 3); pen.setCapStyle(Qt.RoundCap)
            p.setPen(pen)
            if self.config["orientation"] == "horizontal":
                p.drawLine(pt.x(), 8, pt.x(), self.height() - 8)
            else:
                p.drawLine(8, pt.y(), self.width() - 8, pt.y())
        p.end()

    # ---------------- move / snap ----------------
    def mousePressEvent(self, event) -> None:
        if event.button() == Qt.LeftButton and not self.config["locked"]:
            self._dragging_window = True
            self._drag_offset = event.globalPosition().toPoint() - self.frameGeometry().topLeft()
            event.accept()
        else:
            super().mousePressEvent(event)

    def mouseReleaseEvent(self, event) -> None:
        if self._dragging_window:
            self._dragging_window = False
            self._snap_to_edge()
            self.config["pos"] = [self.x(), self.y()]
            write_config(self.config)
            event.accept()
        else:
            super().mouseReleaseEvent(event)

    def mouseDoubleClickEvent(self, event) -> None:
        if event.button() == Qt.LeftButton:
            self.open_settings()
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

    # ---------------- menus / toggles ----------------
    def contextMenuEvent(self, event) -> None:
        menu = QMenu(self)
        menu.setStyleSheet(STYLESHEET)
        menu.addAction("Add File\u2026", self._dialog_add_file)
        menu.addAction("Add Folder\u2026", self._dialog_add_folder)
        menu.addSeparator()
        menu.addAction("Toggle Orientation", self._toggle_orientation)
        menu.addAction("Toggle Lock Position", self._toggle_lock)
        menu.addAction("Toggle Always On Top", self._toggle_on_top)
        menu.addAction("Toggle Magnification", self._toggle_magnify)
        menu.addAction("Reload Icons", self.reload_icons)
        menu.addSeparator()
        menu.addAction("Settings\u2026", self.open_settings)
        menu.addSeparator()
        menu.addAction("Exit", QApplication.instance().quit)
        menu.exec(event.globalPos())

    def _toggle_orientation(self) -> None:
        self.config["orientation"] = "vertical" if self.config["orientation"] == "horizontal" else "horizontal"
        self._save()
        self._rebuild_now()
        self._restore_position()

    def _toggle_lock(self) -> None:
        self.config["locked"] = not self.config["locked"]
        self._save()

    def _toggle_on_top(self) -> None:
        self.config["always_on_top"] = not self.config["always_on_top"]
        self._apply_flags()
        self._save()

    def _toggle_magnify(self) -> None:
        self.config["magnify"] = not self.config.get("magnify", True)
        self._reset_magnification()
        self._save()

    def open_settings(self) -> None:
        SettingsDialog(self).exec()

    def reload_icons(self) -> None:
        _ICON_CACHE.clear(); _ICON_PENDING.clear()
        self._refresh_sizes()

    def _dialog_add_file(self) -> None:
        files, _ = QFileDialog.getOpenFileNames(self, "Add Shortcuts")
        for f in files: self.add_item(f)

    def _dialog_add_folder(self) -> None:
        folder = QFileDialog.getExistingDirectory(self, "Add Folder")
        if folder: self.add_item(folder)

    # ---------------- drag & drop ----------------
    def _acceptable(self, mime: QMimeData) -> bool:
        return mime.hasFormat(MIME_INTERNAL) or mime.hasUrls()

    def _map_in(self, event, source: QWidget) -> QPoint:
        return self.mapFromGlobal(source.mapToGlobal(event.position().toPoint()))

    def _drop_index_at(self, pos: QPoint) -> int:
        if not self.buttons: return 0
        for i, b in enumerate(self.buttons):
            g = b.geometry()
            if self.config["orientation"] == "horizontal":
                if pos.x() < g.center().x(): return i
            else:
                if pos.y() < g.center().y(): return i
        return len(self.buttons)

    def _insert_line_pos(self, index: int) -> QPoint:
        if not self.buttons: return QPoint(self.width() // 2, self.height() // 2)
        index = max(0, min(index, len(self.buttons)))
        s = self.config["spacing"]
        if self.config["orientation"] == "horizontal":
            if index < len(self.buttons):
                return QPoint(self.buttons[index].geometry().left() - s // 2 - 1, 0)
            return QPoint(self.buttons[-1].geometry().right() + s // 2 + 1, 0)
        if index < len(self.buttons):
            return QPoint(0, self.buttons[index].geometry().top() - s // 2 - 1)
        return QPoint(0, self.buttons[-1].geometry().bottom() + s // 2 + 1)

    def _handle_drag_enter(self, event, source: QWidget) -> None:
        _log(f"drag enter: urls={event.mimeData().hasUrls()} internal={event.mimeData().hasFormat(MIME_INTERNAL)}")
        if self._acceptable(event.mimeData()):
            event.acceptProposedAction()
            self._drop_index = self._drop_index_at(self._map_in(event, source))
            self.update()

    def _handle_drag_move(self, event, source: QWidget) -> None:
        if self._acceptable(event.mimeData()):
            event.acceptProposedAction()
            self._drop_index = self._drop_index_at(self._map_in(event, source))
            self.update()

    def _handle_drop(self, event, source: QWidget) -> None:
        pos = self._map_in(event, source)
        mime = event.mimeData()
        target = self._drop_index_at(pos)
        if mime.hasFormat(MIME_INTERNAL):
            try: src_idx = int(bytes(mime.data(MIME_INTERNAL)).decode("utf-8"))
            except Exception: src_idx = -1
            if src_idx >= 0: self.move_item(src_idx, target)
        elif mime.hasUrls():
            added = 0
            for url in mime.urls():
                path = url.toLocalFile()
                if not path: continue
                try: path = os.path.abspath(path)
                except Exception: continue
                if not os.path.exists(path): continue
                if self.add_item(path, target):
                    target += 1
                    added += 1
            _log(f"drop: {added} item(s) added from {len(mime.urls())} urls")
        self._drop_index = -1
        self.update()
        event.acceptProposedAction()

    def dragEnterEvent(self, event) -> None: self._handle_drag_enter(event, self)
    def dragMoveEvent(self, event) -> None: self._handle_drag_move(event, self)
    def dropEvent(self, event) -> None: self._handle_drop(event, self)
    def dragLeaveEvent(self, event) -> None:
        self._drop_index = -1
        self.update()

# ---------------------------------------------------------------------------
# Settings dialog (live preview)
# ---------------------------------------------------------------------------
class SettingsDialog(QDialog):
    def __init__(self, dock_bar: DockBar):
        super().__init__(dock_bar)
        self.dock_bar = dock_bar
        self.setWindowTitle("DockPro Settings")
        self.setModal(True)
        self.setMinimumWidth(480)
        self.setStyleSheet(STYLESHEET)

        root = QVBoxLayout(self)
        root.setSpacing(10)

        appearance = QGroupBox("Appearance")
        form = QFormLayout(appearance)

        self.icon_size = QSlider(Qt.Horizontal); self.icon_size.setRange(24, 96); self.icon_size.setValue(dock_bar.config["icon_size"])
        form.addRow("Icon Size:", self.icon_size)

        self.padding = QSlider(Qt.Horizontal); self.padding.setRange(0, 40); self.padding.setValue(dock_bar.config.get("bar_padding", 12))
        form.addRow("Bar Thickness:", self.padding)

        self.spacing = QSlider(Qt.Horizontal); self.spacing.setRange(0, 32); self.spacing.setValue(dock_bar.config["spacing"])
        form.addRow("Spacing:", self.spacing)

        self.opacity = QSlider(Qt.Horizontal); self.opacity.setRange(30, 100); self.opacity.setValue(int(dock_bar.config["opacity"] * 100))
        form.addRow("Opacity:", self.opacity)

        self.radius = QSlider(Qt.Horizontal); self.radius.setRange(0, 40); self.radius.setValue(dock_bar.config.get("radius", 18))
        form.addRow("Corner Radius:", self.radius)

        colors_row = QHBoxLayout()
        self.bg_btn = QPushButton(self._color_text(dock_bar.config["bg_color"]))
        self.border_btn = QPushButton(self._color_text(dock_bar.config["border_color"]))
        self.accent_btn = QPushButton(self._color_text(dock_bar.config["accent"]))
        for w in (self.bg_btn, self.border_btn, self.accent_btn):
            colors_row.addWidget(w)
        colors_row.addStretch(1)
        form.addRow("Colors (bg / border / accent):", colors_row)
        root.addWidget(appearance)

        behavior = QGroupBox("Behavior")
        bform = QFormLayout(behavior)

        self.h_radio = QRadioButton("Horizontal")
        self.v_radio = QRadioButton("Vertical")
        grp = QButtonGroup(self); grp.addButton(self.h_radio); grp.addButton(self.v_radio)
        (self.h_radio if dock_bar.config["orientation"] == "horizontal" else self.v_radio).setChecked(True)
        wrap = QWidget(); lo = QHBoxLayout(wrap); lo.setContentsMargins(0, 0, 0, 0)
        lo.addWidget(self.h_radio); lo.addWidget(self.v_radio); lo.addStretch(1)
        bform.addRow("Orientation:", wrap)

        self.mag_check = QCheckBox("Cursor magnification"); self.mag_check.setChecked(bool(dock_bar.config.get("magnify", True)))
        bform.addRow(self.mag_check)

        self.on_top = QCheckBox("Always on top"); self.on_top.setChecked(dock_bar.config["always_on_top"])
        bform.addRow(self.on_top)

        self.locked = QCheckBox("Lock position"); self.locked.setChecked(dock_bar.config["locked"])
        bform.addRow(self.locked)
        root.addWidget(behavior)

        shortcuts = QGroupBox("Shortcuts")
        s_lay = QVBoxLayout(shortcuts)
        self.list_widget = QListWidget(); self.list_widget.setMinimumHeight(140)
        self._refresh_list()
        s_lay.addWidget(self.list_widget)
        row = QHBoxLayout()
        add_f = QPushButton("Add File\u2026"); add_d = QPushButton("Add Folder\u2026"); rem = QPushButton("Remove Selected")
        self.up_btn = QPushButton("Move Up"); self.down_btn = QPushButton("Move Down")
        for w in (add_f, add_d, rem): row.addWidget(w)
        row.addStretch(1)
        row.addWidget(self.up_btn); row.addWidget(self.down_btn)
        s_lay.addLayout(row)
        root.addWidget(shortcuts)

        box = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        box.accepted.connect(self._on_accept)
        box.rejected.connect(self.reject)
        root.addWidget(box)

        self.icon_size.valueChanged.connect(self._preview)
        self.padding.valueChanged.connect(self._preview)
        self.spacing.valueChanged.connect(self._preview)
        self.opacity.valueChanged.connect(self._preview)
        self.radius.valueChanged.connect(self._preview)
        self.h_radio.toggled.connect(self._preview)
        self.v_radio.toggled.connect(self._preview)
        self.mag_check.toggled.connect(self._preview)
        self.on_top.toggled.connect(self._preview)
        self.bg_btn.clicked.connect(lambda: self._pick_color("bg_color"))
        self.border_btn.clicked.connect(lambda: self._pick_color("border_color"))
        self.accent_btn.clicked.connect(lambda: self._pick_color("accent"))
        add_f.clicked.connect(self._add_file)
        add_d.clicked.connect(self._add_folder)
        rem.clicked.connect(self._remove_selected)
        self.up_btn.clicked.connect(lambda: self._shift(-1))
        self.down_btn.clicked.connect(lambda: self._shift(1))

    def _color_text(self, c: str) -> str:
        return f"  {c}  "

    def _pick_color(self, key: str) -> None:
        cur = QColor(self.dock_bar.config.get(key, "#000000"))
        col = QColorDialog.getColor(cur, self)
        if col.isValid():
            self.dock_bar.config[key] = col.name()
            self.bg_btn.setText(self._color_text(self.dock_bar.config["bg_color"]))
            self.border_btn.setText(self._color_text(self.dock_bar.config["border_color"]))
            self.accent_btn.setText(self._color_text(self.dock_bar.config["accent"]))
            self.dock_bar.update()

    def _refresh_list(self) -> None:
        self.list_widget.clear()
        for it in self.dock_bar.items:
            self.list_widget.addItem(QListWidgetItem(f"{it.name}   \u2014   {it.path}"))

    def _preview(self) -> None:
        cfg = self.dock_bar.config
        cfg["icon_size"] = self.icon_size.value()
        cfg["bar_padding"] = self.padding.value()
        cfg["spacing"] = self.spacing.value()
        cfg["opacity"] = self.opacity.value() / 100.0
        cfg["radius"] = self.radius.value()
        cfg["orientation"] = "horizontal" if self.h_radio.isChecked() else "vertical"
        cfg["magnify"] = self.mag_check.isChecked()
        cfg["always_on_top"] = self.on_top.isChecked()
        self.dock_bar.apply_live()

    def _add_file(self) -> None:
        files, _ = QFileDialog.getOpenFileNames(self, "Add Files", "", "All Files (*.*)")
        for f in files: self.dock_bar.add_item(f)
        self._refresh_list()

    def _add_folder(self) -> None:
        folder = QFileDialog.getExistingDirectory(self, "Add Folder")
        if folder: self.dock_bar.add_item(folder)
        self._refresh_list()

    def _remove_selected(self) -> None:
        row = self.list_widget.currentRow()
        if 0 <= row < len(self.dock_bar.items):
            self.dock_bar.remove_item(self.dock_bar.items[row])
            self._refresh_list()

    def _shift(self, direction: int) -> None:
        row = self.list_widget.currentRow()
        new_row = row + direction
        if 0 <= row < len(self.dock_bar.items) and 0 <= new_row < len(self.dock_bar.items):
            items = self.dock_bar.items
            items[row], items[new_row] = items[new_row], items[row]
            self.dock_bar._save()
            self.dock_bar._rebuild_soon()
            self._refresh_list()
            self.list_widget.setCurrentRow(new_row)

    def _on_accept(self) -> None:
        self.dock_bar.config["locked"] = self.locked.isChecked()
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
    r = max(2, size // 12); cy = size // 2
    for x_ratio in (0.30, 0.50, 0.70):
        p.drawEllipse(QPoint(int(size * x_ratio), cy), r, r)
    p.end()
    return pm

class TrayIcon(QSystemTrayIcon):
    def __init__(self, dock_bar: DockBar):
        super().__init__()
        self.dock_bar = dock_bar
        self.setIcon(QIcon(_make_tray_pixmap(64)))
        self.setToolTip("DockPro")
        self._build_menu()
        self.activated.connect(self._on_activated)

    def _build_menu(self) -> None:
        menu = QMenu()
        menu.setStyleSheet(STYLESHEET)
        menu.aboutToShow.connect(self._refresh_state)
        self.a_show = menu.addAction("Show / Hide Dock")
        self.a_show.triggered.connect(self._toggle_visible)
        menu.addSeparator()
        self.a_settings = menu.addAction("Settings\u2026")
        self.a_settings.triggered.connect(self.dock_bar.open_settings)
        self.a_add_file = menu.addAction("Add Shortcut\u2026")
        self.a_add_file.triggered.connect(self.dock_bar._dialog_add_file)
        self.a_add_folder = menu.addAction("Add Folder\u2026")
        self.a_add_folder.triggered.connect(self.dock_bar._dialog_add_folder)
        menu.addSeparator()
        self.a_orientation = menu.addAction("Toggle Orientation")
        self.a_orientation.triggered.connect(self.dock_bar._toggle_orientation)
        self.a_lock = menu.addAction("Toggle Lock Position")
        self.a_lock.triggered.connect(self.dock_bar._toggle_lock)
        self.a_top = menu.addAction("Toggle Always On Top")
        self.a_top.triggered.connect(self.dock_bar._toggle_on_top)
        self.a_mag = menu.addAction("Toggle Magnification")
        self.a_mag.triggered.connect(self.dock_bar._toggle_magnify)
        self.a_reload = menu.addAction("Reload Icons")
        self.a_reload.triggered.connect(self.dock_bar.reload_icons)
        menu.addSeparator()
        self.a_exit = menu.addAction("Exit")
        self.a_exit.triggered.connect(self._exit_app)
        self.setContextMenu(menu)

    def _refresh_state(self) -> None:
        cfg = self.dock_bar.config
        self.a_lock.setText("Unlock Position" if cfg["locked"] else "Lock Position")
        self.a_top.setText("Disable Always On Top" if cfg["always_on_top"] else "Enable Always On Top")
        self.a_orientation.setText("Switch to Vertical" if cfg["orientation"] == "horizontal" else "Switch to Horizontal")
        self.a_mag.setText("Disable Magnification" if cfg.get("magnify", True) else "Enable Magnification")
        self.a_show.setText("Hide Dock" if self.dock_bar.isVisible() else "Show Dock")

    def _toggle_visible(self) -> None:
        if self.dock_bar.isVisible(): self.dock_bar.hide()
        else:
            self.dock_bar.show()
            self.dock_bar.raise_()

    def _on_activated(self, reason) -> None:
        if reason == QSystemTrayIcon.Trigger: self._toggle_visible()

    def _exit_app(self) -> None:
        try: self.hide()
        except Exception: pass
        QApplication.instance().quit()

# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------
def _install_crash_hook() -> None:
    import traceback as _tb

    def _hook(etype, value, tb):
        text = "".join(_tb.format_exception(etype, value, tb))
        try:
            with open(APP_DIR / "dock_pro_debug.log", "a", encoding="utf-8") as f:
                f.write("\n=== CRASH ===\n" + text)
        except Exception: pass
        try:
            from PySide6.QtWidgets import QApplication as _A, QMessageBox as _M
            app = _A.instance()
            if app is not None:
                _M.critical(None, "DockPro Error", text[-3000:])
        except Exception: pass

    sys.excepthook = _hook

def main() -> int:
    _install_crash_hook()
    app = QApplication(sys.argv)
    app.setStyle("Fusion")
    app.setQuitOnLastWindowClosed(False)
    app.setApplicationName("DockPro")

    dock = DockBar()
    dock.show()

    tray = None
    if QSystemTrayIcon.isSystemTrayAvailable():
        tray = TrayIcon(dock)
        tray.show()
    app._tray = tray

    return app.exec()

if __name__ == "__main__":
    try:
        sys.exit(main())
    except SystemExit:
        raise
    except BaseException:
        text = traceback.format_exc()
        try:
            with open(APP_DIR / "dock_pro_debug.log", "a", encoding="utf-8") as f:
                f.write("\n=== FATAL ===\n" + text)
        except Exception: pass
        try:
            from PySide6.QtWidgets import QApplication as _A, QMessageBox as _M
            app = _A.instance() or _A(sys.argv)
            _M.critical(None, "DockPro Fatal Error", text[-3000:])
        except Exception: pass
        sys.exit(1)
