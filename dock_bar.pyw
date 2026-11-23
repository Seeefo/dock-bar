# dock_bar.pyw — شريط اختصارات عائم لويندوز 11 (دارك مود مينمالست)
# ---------------------------------------------------------------------------
# ملف واحد فقط، بدون إنترنت، بدون pywin32.
# المتطلبات (مرة واحدة):
#     pip install PySide6
# سجل التشخيص يكتب دائماً في: dock_bar_debug.log (بجانب الملف)
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
    Qt, QPoint, QRect, QRectF, QMimeData, Property, QObject, Signal,
    QRunnable, QThreadPool, QPropertyAnimation, QEasingCurve,
)
from PySide6.QtGui import (
    QIcon, QPixmap, QPainter, QColor, QPen, QImage, QPainterPath,
    QDrag, QImageReader,
)
from PySide6.QtWidgets import (
    QApplication, QWidget, QMenu, QSystemTrayIcon, QMessageBox,
    QInputDialog, QFileDialog, QDialog, QVBoxLayout, QHBoxLayout,
    QFormLayout, QGroupBox, QSlider, QPushButton, QBoxLayout,
    QDialogButtonBox, QColorDialog, QListWidget, QListWidgetItem,
)

_IS_WIN = sys.platform == "win32"
APP_DIR = Path(__file__).resolve().parent
CONFIG_PATH = APP_DIR / "dock_bar_config.json"
LOG_PATH = APP_DIR / "dock_bar_debug.log"
MIME_INTERNAL = "application/x-dockbar-item"

DEFAULT_CONFIG = {
    "icon_size": 40,
    "bar_padding": 9,
    "spacing": 8,
    "opacity": 0.92,
    "radius": 16,
    "orientation_h": True,
    "always_on_top": True,
    "locked": False,
    "pos": None,
    "bg_color": "#0E0F12",
    "border_color": "#26282E",
    "accent": "#E86F2C",
    "items": [],
}

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
def _log(msg: str) -> None:
    line = f"[{time.strftime('%H:%M:%S')}] {msg}"
    try:
        with open(LOG_PATH, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
def load_config() -> dict:
    cfg = dict(DEFAULT_CONFIG)
    try:
        if CONFIG_PATH.exists():
            data = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                for k, v in data.items():
                    cfg[k] = v
                return cfg
    except Exception:
        pass
    for old in (APP_DIR / "dock_pro_config.json", APP_DIR / "dock_config.json"):
        try:
            if old.exists():
                data = json.loads(old.read_text(encoding="utf-8"))
                if isinstance(data, dict):
                    for k, v in data.items():
                        if k in cfg:
                            cfg[k] = v
                    write_config(cfg)
                    _log(f"migrated config from {old.name} -> {CONFIG_PATH.name}")
                    break
        except Exception:
            pass
    return cfg

def write_config(cfg: dict) -> None:
    tmp = CONFIG_PATH.with_suffix(".tmp")
    try:
        tmp.write_text(json.dumps(cfg, indent=2, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, CONFIG_PATH)
    except Exception:
        try:
            tmp.unlink(missing_ok=True)
        except Exception:
            pass

def _expand(p: str) -> str:
    if not p:
        return ""
    try:
        return os.path.normpath(os.path.expandvars(os.path.expanduser(p)))
    except Exception:
        return p

# ---------------------------------------------------------------------------
# Win32: .lnk resolution (pure ctypes) -> (target, icon_location, icon_index)
# ---------------------------------------------------------------------------
def resolve_lnk(path: str, _depth: int = 0):
    if not _IS_WIN or _depth > 5:
        return "", "", 0
    try:
        from ctypes import wintypes

        class GUID(ctypes.Structure):
            _fields_ = [("Data1", wintypes.DWORD), ("Data2", wintypes.WORD),
                        ("Data3", wintypes.WORD), ("Data4", ctypes.c_ubyte * 8)]

        def as_guid(u):
            g = GUID()
            g.Data1, g.Data2, g.Data3 = u.time_low, u.time_mid, u.time_hi_version
            for i, b in enumerate(u.bytes[8:]):
                g.Data4[i] = b
            return g

        CLSID_ShellLink = as_guid(uuid.UUID("{00021401-0000-0000-C000-000000000046}"))
        IID_IShellLinkW = as_guid(uuid.UUID("{000214F9-0000-0000-C000-000000000046}"))
        IID_IPersistFile = as_guid(uuid.UUID("{0000010b-0000-0000-C000-000000000046}"))

        ole32 = ctypes.windll.ole32
        pShellLink = ctypes.c_void_p()
        pPersistFile = ctypes.c_void_p()
        try:
            ole32.CoInitialize(None)
            if ole32.CoCreateInstance(ctypes.byref(CLSID_ShellLink), None, 1,
                                      ctypes.byref(IID_IShellLinkW), ctypes.byref(pShellLink)) != 0:
                _log(f"resolve_lnk: CoCreateInstance failed {path!r}")
                return "", "", 0

            vtable = ctypes.cast(pShellLink, ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p)))
            QI = ctypes.WINFUNCTYPE(ctypes.c_long, ctypes.c_void_p, ctypes.POINTER(GUID),
                                    ctypes.POINTER(ctypes.c_void_p))(vtable[0][0])
            if QI(pShellLink, ctypes.byref(IID_IPersistFile), ctypes.byref(pPersistFile)) != 0:
                _log(f"resolve_lnk: QI(IPersistFile) failed {path!r}")
                return "", "", 0

            vtable_pf = ctypes.cast(pPersistFile, ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p)))
            Load = ctypes.WINFUNCTYPE(ctypes.c_long, ctypes.c_void_p, ctypes.c_wchar_p,
                                      wintypes.DWORD)(vtable_pf[0][5])
            if Load(pPersistFile, str(path), 0) != 0:
                _log(f"resolve_lnk: IPersistFile Load failed {path!r}")
                return "", "", 0

            vtable_sl = ctypes.cast(pShellLink, ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p)))
            # IShellLinkW vtable: QI=0, AddRef=1, Release=2, SetPath=3, GetPath=4,
            # ..., SetIconLocation=17, GetIconLocation=18
            GetPath = ctypes.WINFUNCTYPE(ctypes.c_long, ctypes.c_void_p, ctypes.c_wchar_p,
                                         ctypes.c_int, ctypes.c_void_p, wintypes.DWORD)(vtable_sl[0][4])
            tb = ctypes.create_unicode_buffer(1024)
            GetPath(pShellLink, tb, 1024, None, 0)
            target = _expand(tb.value)

            GetIconLocation = ctypes.WINFUNCTYPE(ctypes.c_long, ctypes.c_void_p, ctypes.c_wchar_p,
                                                 ctypes.c_int, ctypes.POINTER(ctypes.c_int))(vtable_sl[0][18])
            ib = ctypes.create_unicode_buffer(1024)
            idx = ctypes.c_int(0)
            GetIconLocation(pShellLink, ib, 1024, ctypes.byref(idx))
            icon_path = _expand(ib.value)
            if icon_path and not os.path.isabs(icon_path):
                icon_path = _expand(os.path.join(os.path.dirname(path), icon_path))

            _log(f"resolve_lnk: {path!r} -> target={target!r} icon={icon_path!r} idx={idx.value}")
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
# Win32: icon extraction -> QImage (thread-safe; QPixmap on GUI thread only)
# ---------------------------------------------------------------------------
SHGFI_ICON         = 0x000000100
SHGFI_LARGEICON    = 0x000000000
SHGFI_SMALLICON    = 0x000000001
SHGFI_ICONLOCATION = 0x000000400
GIL_NOTFILENAME    = 0x00000008
IID_IEXTRACTICONW  = "{000214FA-0000-0000-C000-000000000046}"

def _mk_guid(s):
    class _G(ctypes.Structure):
        _fields_ = [("Data1", ctypes.c_ulong), ("Data2", ctypes.c_ushort),
                    ("Data3", ctypes.c_ushort), ("Data4", ctypes.c_ubyte * 8)]
    u = uuid.UUID(s)
    g = _G()
    g.Data1, g.Data2, g.Data3 = u.time_low, u.time_mid, u.time_hi_version
    for i, b in enumerate(u.bytes[8:]):
        g.Data4[i] = b
    return g

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
    if img is None or img.isNull():
        return None
    i = img.convertToFormat(QImage.Format_ARGB32)
    if i.width() == size and i.height() == size:
        return i
    return i.scaled(size, size, Qt.KeepAspectRatio, Qt.SmoothTransformation)

def _extract_from_file(path: str, size: int, index: int = 0):
    try:
        from ctypes import wintypes
        shell32 = ctypes.windll.shell32
        user32 = ctypes.windll.user32
        large = wintypes.HICON()
        small = wintypes.HICON()
        r = shell32.SHDefExtractIconW(ctypes.c_wchar_p(str(path)), int(index), 0,
                                      ctypes.byref(large), ctypes.byref(small),
                                      (size & 0xFFFF) | ((size & 0xFFFF) << 16))
        hicon = large.value or small.value
        if not hicon and r != 0:
            shell32.ExtractIconExW(str(path), int(index), ctypes.byref(large),
                                   ctypes.byref(small), 1)
            hicon = large.value or small.value
        if not hicon:
            return None
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
    info = _SHFILEINFO()
    res = ctypes.windll.shell32.SHGetFileInfoW(ctypes.c_wchar_p(str(path)), 0,
                                               ctypes.byref(info), ctypes.sizeof(info), flags)
    return res, info

def _type_icon_by_location(path: str, size: int):
    res, info = _shgetfileinfo(path, SHGFI_ICONLOCATION)
    if not res:
        return None
    loc = (info.szDisplayName or "").strip().strip('"')
    idx = int(info.iIcon)
    if loc and not os.path.exists(_expand(loc) or "") and "," in loc:
        base, _, tail = loc.rpartition(",")
        tail = tail.strip()
        if tail.lstrip("-").isdigit():
            loc, idx = base.strip().strip('"'), int(tail)
    loc = _expand(loc or "")
    if not loc or not os.path.exists(loc):
        return None
    img = _extract_from_file(loc, size, idx)
    if img is not None and not img.isNull():
        return img
    img = _extract_from_file(loc, min(size, 48), idx)
    return img if img is not None and not img.isNull() else None

def _shell_small_icon(path: str, size: int):
    flags = SHGFI_ICON | (SHGFI_LARGEICON if size >= 32 else SHGFI_SMALLICON)
    try:
        res, info = _shgetfileinfo(path, flags)
        if not res or not info.hIcon:
            return None
        try:
            return _fix_img(_hicon_to_image(info.hIcon), size)
        finally:
            ctypes.windll.user32.DestroyIcon(info.hIcon)
    except Exception:
        return None

def _iextract_icon_image(path: str, size: int):
    """IExtractIconW عبر GetUIObjectOf: نفس الأيقونة التي يرسمها Explorer
    قبل أن يُضيف سهم الاختصار. تعمل مع .lnk والمجلدات والملفات. بلا سهم."""
    if not _IS_WIN:
        return None
    try:
        from ctypes import wintypes
        shell32 = ctypes.windll.shell32
        user32 = ctypes.windll.user32

        pDesk = ctypes.c_void_p()
        if shell32.SHGetDesktopFolder(ctypes.byref(pDesk)) != 0 or not pDesk:
            _log("_iextract_icon_image: SHGetDesktopFolder failed")
            return None
        pidl = ctypes.c_void_p()
        try:
            vt = ctypes.cast(pDesk, ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p)))
            # IShellFolder vtable: ParseDisplayName=3, GetUIObjectOf=10
            ParseDisplayName = ctypes.WINFUNCTYPE(
                ctypes.c_long, ctypes.c_void_p, wintypes.HWND, ctypes.c_void_p,
                ctypes.c_wchar_p, ctypes.POINTER(wintypes.ULONG),
                ctypes.POINTER(ctypes.c_void_p))(vt[0][3])
            attr = wintypes.ULONG(0)
            if ParseDisplayName(pDesk, None, None, str(path), ctypes.byref(attr),
                                ctypes.byref(pidl)) != 0 or not pidl:
                _log(f"_iextract_icon_image: ParseDisplayName failed {path!r}")
                return None
            gid = _mk_guid(IID_IEXTRACTICONW)
            GetUIObjectOf = ctypes.WINFUNCTYPE(
                ctypes.c_long, ctypes.c_void_p, wintypes.HWND, wintypes.UINT, ctypes.c_void_p,
                ctypes.POINTER(type(gid)), ctypes.c_void_p, ctypes.POINTER(ctypes.c_void_p))(vt[0][10])
            pidl_arr = (ctypes.c_void_p * 1)(pidl)
            iex = ctypes.c_void_p()
            hr = GetUIObjectOf(pDesk, None, 1, ctypes.cast(pidl_arr, ctypes.c_void_p),
                               ctypes.byref(gid), None, ctypes.byref(iex))
            if hr != 0 or not iex:
                _log(f"_iextract_icon_image: GetUIObjectOf failed hr={hr} {path!r}")
                return None
            try:
                vte = ctypes.cast(iex, ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p)))
                # IExtractIconW vtable: GetIconLocation=3, Extract=4
                GetIconLocation = ctypes.WINFUNCTYPE(
                    ctypes.c_long, ctypes.c_void_p, wintypes.UINT, ctypes.c_wchar_p,
                    ctypes.c_int, ctypes.POINTER(ctypes.c_int),
                    ctypes.POINTER(ctypes.c_uint))(vte[0][3])
                loc_buf = ctypes.create_unicode_buffer(1024)
                idx = ctypes.c_int(0)
                fl = ctypes.c_uint(0)
                if GetIconLocation(iex, 0, loc_buf, 1024, ctypes.byref(idx), ctypes.byref(fl)) != 0:
                    _log(f"_iextract_icon_image: GetIconLocation failed {path!r}")
                    return None
                _log(f"_iextract_icon_image: loc={loc_buf.value!r} idx={idx.value} flags={fl.value:#x} {path!r}")
                if fl.value & GIL_NOTFILENAME:
                    Extract = ctypes.WINFUNCTYPE(
                        ctypes.c_long, ctypes.c_void_p, ctypes.c_wchar_p, ctypes.c_int,
                        ctypes.POINTER(wintypes.HICON), ctypes.POINTER(wintypes.HICON),
                        wintypes.UINT)(vte[0][4])
                    large = wintypes.HICON()
                    small = wintypes.HICON()
                    nsize = (size & 0xFFFF) | ((size & 0xFFFF) << 16)
                    if Extract(iex, loc_buf.value, idx.value, ctypes.byref(large),
                               ctypes.byref(small), nsize) != 0:
                        _log(f"_iextract_icon_image: Extract failed {path!r}")
                        return None
                    hicon = large.value or small.value
                    if not hicon:
                        _log(f"_iextract_icon_image: Extract no icon {path!r}")
                        return None
                    try:
                        return _fix_img(_hicon_to_image(hicon), size)
                    finally:
                        if large.value:
                            user32.DestroyIcon(large.value)
                        if small.value:
                            user32.DestroyIcon(small.value)
                loc = _expand(loc_buf.value)
                if not loc or not os.path.exists(loc):
                    _log(f"_iextract_icon_image: loc not usable {loc!r} {path!r}")
                    return None
                return _extract_from_file(loc, size, idx.value)
            finally:
                vte = ctypes.cast(iex, ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p)))
                Release = ctypes.WINFUNCTYPE(ctypes.c_ulong, ctypes.c_void_p)(vte[0][2])
                Release(iex)
        finally:
            if pidl:
                ctypes.windll.ole32.CoTaskMemFree(pidl)
            vtd = ctypes.cast(pDesk, ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p)))
            Release = ctypes.WINFUNCTYPE(ctypes.c_ulong, ctypes.c_void_p)(vtd[0][2])
            Release(pDesk)
    except Exception as e:
        _log(f"_iextract_icon_image error {path!r}: {e}")
        return None

def _image_reader_icon(path: str, size: int):
    low = path.lower()
    if not low.endswith((".ico", ".cur", ".png", ".jpg", ".jpeg", ".bmp", ".gif", ".webp")):
        return None
    try:
        reader = QImageReader(path)
        reader.setAutoTransform(True)
        if not reader.canRead():
            return None
        img = reader.read()
        return None if img.isNull() else img
    except Exception:
        return None

def extract_icon(path: str, size: int = 96):
    path = _expand(str(path))
    if not path or not os.path.exists(path):
        _log(f"icon src for {path!r}: missing path")
        return None
    img = _image_reader_icon(path, size)
    if img is not None and not img.isNull():
        _log(f"icon src for {path!r}: reader")
        return img
    if not _IS_WIN:
        return None
    img, src = _extract_icon_src(path, size)
    if img is not None and not img.isNull():
        _log(f"icon src for {path!r}: {src}")
        return img
    _log(f"icon src for {path!r}: NONE")
    return None

def _extract_icon_src(path: str, size: int):
    """لا نستدعي SHGetFileInfoW على .lnk أبداً قبل حل الهدف — هذا ما يخبز السهم."""
    target, icon_loc, icon_idx = "", "", 0
    if path.lower().endswith(".lnk"):
        target, icon_loc, icon_idx = resolve_lnk(path)

        # Explorer يستعلم IExtractIconW من الهدف لا من الاختصار نفسه.
        if target:
            img = _iextract_icon_image(target, size)
            if img is not None and not img.isNull():
                return img, "target-iextract"
            t = _expand(target)
            if os.path.isdir(t) or os.path.isfile(t):
                img = _type_icon_by_location(t, size)
                if img is not None and not img.isNull():
                    return img, "target-location"
                img = _shell_small_icon(t, size)
                if img is not None and not img.isNull():
                    return img, "target-shell"
            else:
                _log(f"target not visible: {t!r} isdir={os.path.isdir(t)} isfile={os.path.isfile(t)}")

        if icon_loc and os.path.exists(icon_loc):
            img = _extract_from_file(icon_loc, size, icon_idx) or _type_icon_by_location(icon_loc, size)
            if img is not None and not img.isNull():
                return img, "icon-location"

        img = _type_icon_by_location(path, size)
        if img is not None and not img.isNull():
            return img, "lnk-embedded"

        if target and os.path.exists(target):
            img = _extract_from_file(target, size, 0)
            if img is not None and not img.isNull():
                return img, "target-extract"

        # أيقونة الفئة العامة للاختصار نفسه (بلا سهم)
        img = _iextract_icon_image(path, size)
        if img is not None and not img.isNull():
            return img, "lnk-iextract-class"

        # سهم أخير كحل إلزامي حتى لا تظهر مربعات حروف
        img = _shell_small_icon(path, size)
        if img is not None and not img.isNull():
            return img, "lnk-arrow-last-resort"
    else:
        if os.path.isdir(path):
            img = _type_icon_by_location(path, size)
            if img is not None and not img.isNull():
                return img, "folder-location"
            img = _iextract_icon_image(path, size)
            if img is not None and not img.isNull():
                return img, "folder-iextract"
            img = _shell_small_icon(path, size)
            if img is not None and not img.isNull():
                return img, "folder-shell"
        else:
            img = _extract_from_file(path, size, 0)
            if img is not None and not img.isNull():
                return img, "file-extract"
            img = _iextract_icon_image(path, size)
            if img is not None and not img.isNull():
                return img, "file-iextract"
            img = _type_icon_by_location(path, size)
            if img is not None and not img.isNull():
                return img, "file-location"
            img = _shell_small_icon(path, size)
            if img is not None and not img.isNull():
                return img, "file-shell"

    return None, ""

# ---------------------------------------------------------------------------
# Background icon pipeline
# ---------------------------------------------------------------------------
class _IconBus(QObject):
    got = Signal(str, int, QImage)

ICON_BUS = _IconBus()
_ICON_CACHE = {}
_ICON_PENDING = set()

def push_icon(path: str, size: int) -> tuple:
    key = (os.path.normcase(path), size)
    if key in _ICON_CACHE:
        return True, _ICON_CACHE[key]
    if key in _ICON_PENDING:
        return False, None
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
            if _IS_WIN:
                ctypes.windll.ole32.CoInitialize(None)
            img = extract_icon(self.path, self.size)
        except Exception as e:
            _log(f"icon job error {self.path!r}: {e}")
        finally:
            ok = img is not None and not img.isNull()
            _log(f"icon {self.path!r} size={self.size} -> {'ok' if ok else 'NONE'}")
            if ok:
                _ICON_CACHE[(os.path.normcase(self.path), self.size)] = img
            try:
                ICON_BUS.got.emit(self.path, self.size, img or QImage())
            except Exception:
                pass
            if _IS_WIN:
                try:
                    ctypes.windll.ole32.CoUninitialize()
                except Exception:
                    pass

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
    def __init__(self, item: DockItem, bar: "DockBar"):
        super().__init__(bar)
        self.item = item
        self.bar = bar
        self._scale = 1.0
        self._hovered = False
        self._pixmap = None
        self._press_pos = None
        self._last_icon_size = -1

        self.setMouseTracking(True)
        self.setCursor(Qt.PointingHandCursor)
        self.setAcceptDrops(True)

        self._anim = QPropertyAnimation(self, b"scale", self)
        self._anim.setDuration(170)
        self._anim.setEasingCurve(QEasingCurve.OutCubic)

        self._update_size()
        self.setToolTip(item.name)

    def _get_scale(self) -> float:
        return self._scale

    def _set_scale(self, v: float) -> None:
        self._scale = v
        self.update()

    scale = Property(float, _get_scale, _set_scale)

    def _update_size(self) -> None:
        icon_size = self.bar.config["icon_size"]
        if icon_size != self._last_icon_size:
            self._last_icon_size = icon_size
            self._pixmap = None
            try:
                ok, pm = push_icon(self.item.path, max(icon_size * 2, 96))
                if ok and pm is not None:
                    self._set_icon_img(pm)
            except Exception as e:
                _log(f"push_icon error {self.item.path!r}: {e}")

        pad = self.bar.config.get("bar_padding", 9)
        side = icon_size + pad * 2
        self.setFixedSize(side, side)

    def _set_icon_img(self, img) -> None:
        try:
            if img is None or img.isNull():
                return
            pm = QPixmap.fromImage(img.convertToFormat(QImage.Format_ARGB32))
            self._pixmap = pm
            self.update()
        except Exception as e:
            _log(f"_set_icon_img error {self.item.path!r}: {e}")

    def paintEvent(self, event) -> None:
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing, True)
        p.setRenderHint(QPainter.SmoothPixmapTransform, True)
        rect = self.rect()
        icon_size = self.bar.config["icon_size"]
        draw_size = max(1, int(round(icon_size * self._scale)))

        if self._hovered:
            r = QRectF(rect).adjusted(2, 2, -2, -2)
            hl = QColor(self.bar.config["accent"])
            hl.setAlpha(28)
            p.setPen(Qt.NoPen)
            p.setBrush(hl)
            p.drawRoundedRect(r, self.bar.config.get("radius", 16) * 0.6, self.bar.config.get("radius", 16) * 0.6)

        if self._pixmap is not None and not self._pixmap.isNull():
            x = (rect.width() - draw_size) // 2
            y = (rect.height() - draw_size) // 2
            p.drawPixmap(QRect(x, y, draw_size, draw_size), self._pixmap)
        else:
            p.setPen(QPen(QColor(self.bar.config["border_color"])))
            f = p.font()
            f.setPixelSize(max(10, draw_size // 2))
            p.setFont(f)
            p.drawText(QRect(rect), Qt.AlignCenter, (self.item.name or "?")[:1].upper())

        if self._hovered:
            color = QColor(self.bar.config["accent"])
            pen = QPen(color, 2)
            pen.setCapStyle(Qt.RoundCap)
            p.setPen(pen)
            if self.bar.config.get("orientation_h", True):
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
        self._anim.setEndValue(1.12)
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
        if self._press_pos is None:
            return
        delta = event.position().toPoint() - self._press_pos
        if delta.manhattanLength() >= QApplication.startDragDistance():
            self._start_drag()
            self._press_pos = None

    def mouseReleaseEvent(self, event) -> None:
        if event.button() == Qt.LeftButton and self._press_pos is not None:
            self._press_pos = None
            self.bar.launch_item(self.item)
            event.accept()
        else:
            super().mouseReleaseEvent(event)

    def _start_drag(self) -> None:
        try:
            idx = self.bar.items.index(self.item)
        except ValueError:
            return
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
        act_launch = menu.addAction("تشغيل")
        act_open = menu.addAction("فتح موقع الملف")
        act_rename = menu.addAction("إعادة تسمية")
        menu.addSeparator()
        act_remove = menu.addAction("إزالة من الشريط")
        chosen = menu.exec(global_pos)
        if chosen == act_launch:
            self.bar.launch_item(self.item)
        elif chosen == act_open:
            self.bar.open_location(self.item)
        elif chosen == act_rename:
            self.bar.rename_item(self.item)
        elif chosen == act_remove:
            self.bar.remove_item(self.item)

    def dragEnterEvent(self, event) -> None:
        self.bar._handle_drag_enter(event, self)

    def dragMoveEvent(self, event) -> None:
        self.bar._handle_drag_move(event, self)

    def dragLeaveEvent(self, event) -> None:
        self.bar._drop_index = -1
        self.bar.update()

    def dropEvent(self, event) -> None:
        self.bar._handle_drop(event, self)

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
        self.setMouseTracking(True)

        self._layout = QVBoxLayout(self)
        self._layout.setContentsMargins(4, 4, 4, 4)
        self._layout.setSpacing(0)
        self._row = QWidget(self)
        self._row_lay = self._make_row_layout(self._row)
        self._row_lay.setContentsMargins(0, 0, 0, 0)
        self._row_lay.setSpacing(self.config["spacing"])
        self._layout.addWidget(self._row)

        self.buttons = []
        self._dragging_window = False
        self._drag_offset = QPoint()
        self._drop_index = -1

        ICON_BUS.got.connect(self._on_icon_ready)

        self._rebuild()
        self._apply_config()
        self._restore_position()
        _log("DockBar ready")

    def wheelEvent(self, event) -> None:
        if event.modifiers() == Qt.ControlModifier:
            delta = event.angleDelta().y()
            new_size = self.config["icon_size"] + (2 if delta > 0 else -2)
            new_size = max(24, min(96, new_size))
            if new_size != self.config["icon_size"]:
                self.config["icon_size"] = new_size
                self._apply_config()
                self._save()
            event.accept()
        else:
            super().wheelEvent(event)

    def launch_item(self, item: DockItem) -> None:
        try:
            if not os.path.exists(item.path):
                QMessageBox.warning(self, "شريط الاختصارات", f"المسار غير موجود:\n{item.path}")
                return
            os.startfile(item.path)
        except Exception as e:
            QMessageBox.warning(self, "شريط الاختصارات", f"فشل التشغيل:\n{e}")

    def rename_item(self, item: DockItem) -> None:
        text, ok = QInputDialog.getText(self, "إعادة تسمية", "الاسم المعروض:", text=item.name)
        if ok and text.strip():
            item.name = text.strip()
            self._save()
            for b in self.buttons:
                if b.item is item:
                    b.setToolTip(item.name)
                    b.update()

    def remove_item(self, item: DockItem) -> None:
        if item in self.items:
            self.items.remove(item)
            self._rebuild()
            self._save()

    def open_location(self, item: DockItem) -> None:
        try:
            if os.path.isdir(item.path):
                os.startfile(item.path)
            elif os.path.exists(item.path):
                os.startfile(os.path.dirname(item.path) or ".")
        except Exception:
            pass

    def _make_row_layout(self, parent):
        lay = QBoxLayout(QBoxLayout.LeftToRight, parent)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSizeConstraint(QBoxLayout.SetFixedSize)
        return lay

    def _rebuild(self) -> None:
        while self._row_lay.count():
            it = self._row_lay.takeAt(0)
            w = it.widget()
            if w is not None:
                w.setParent(None)
                w.deleteLater()
        self.buttons = []
        self._row_lay.setDirection(QBoxLayout.TopToBottom if not self.config.get("orientation_h", True)
                                    else QBoxLayout.LeftToRight)
        self._row_lay.setSpacing(self.config["spacing"])
        for item in self.items:
            btn = DockButton(item, self)
            self.buttons.append(btn)
            self._row_lay.addWidget(btn)
        self._row_lay.activate()
        self._layout.activate()
        self.adjustSize()
        self.resize(self.sizeHint())
        self.update()

    def _apply_config(self) -> None:
        self.setWindowOpacity(max(0.2, min(1.0, self.config.get("opacity", 0.95))))
        self._row_lay.setSpacing(self.config.get("spacing", 8))
        for b in self.buttons:
            b._update_size()
        flags = Qt.FramelessWindowHint | Qt.Tool
        if self.config.get("always_on_top", True):
            flags |= Qt.WindowStaysOnTopHint
        if self.windowFlags() != flags:
            self.setWindowFlags(flags)
            if self.isVisible():
                self.show()
        self._layout.activate()
        self._row_lay.activate()
        self.adjustSize()
        self.resize(self.sizeHint())
        self.update()

    def _restore_position(self) -> None:
        self.adjustSize()
        pos = self.config.get("pos")
        if isinstance(pos, (list, tuple)) and len(pos) == 2:
            try:
                self.move(int(pos[0]), int(pos[1]))
                if QApplication.screenAt(QPoint(int(pos[0]), int(pos[1]))) is not None:
                    return
            except Exception:
                pass
        self._set_default_position()

    def _set_default_position(self) -> None:
        screen = QApplication.primaryScreen()
        if screen is None:
            return
        geo = screen.availableGeometry()
        self.adjustSize()
        self.move(geo.x() + (geo.width() - self.width()) // 2,
                  geo.y() + geo.height() - self.height() - 20)

    def _save(self) -> None:
        self.config["items"] = [it.to_dict() for it in self.items]
        write_config(self.config)

    def paintEvent(self, event) -> None:
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing, True)

        outer = QRectF(self.rect()).adjusted(1, 1, -1, -1)
        radius = max(0, int(self.config.get("radius", 16)))
        path = QPainterPath()
        path.addRoundedRect(outer, radius, radius)

        bg = QColor(self.config.get("bg_color", "#0E0F12"))
        bg.setAlpha(235)
        p.fillPath(path, bg)
        pen = QPen(QColor(self.config.get("accent", "#E86F2C") if self._drop_index >= 0
                          else self.config.get("border_color", "#26282E")),
                   2 if self._drop_index >= 0 else 1)
        p.setPen(pen)
        p.drawPath(path)

        if not self.buttons:
            p.setPen(QPen(QColor("#8A8D93")))
            p.drawText(QRectF(self.rect()), Qt.AlignCenter, "اسحب الاختصارات إلى هنا")
        elif self._drop_index >= 0:
            pt = self._compute_insert_line_pos(self._drop_index)
            pen2 = QPen(QColor(self.config.get("accent", "#E86F2C")), 3)
            pen2.setCapStyle(Qt.RoundCap)
            p.setPen(pen2)
            if self.config.get("orientation_h", True):
                p.drawLine(pt.x(), 10, pt.x(), self.height() - 10)
            else:
                p.drawLine(10, pt.y(), self.width() - 10, pt.y())
        p.end()

    def mousePressEvent(self, event) -> None:
        if event.button() == Qt.LeftButton and not self.config.get("locked"):
            self._dragging_window = True
            self._drag_offset = event.globalPosition().toPoint() - self.frameGeometry().topLeft()
            event.accept()
        else:
            super().mousePressEvent(event)

    def mouseMoveEvent(self, event) -> None:
        if self._dragging_window and not self.config.get("locked"):
            self.move(event.globalPosition().toPoint() - self._drag_offset)
            event.accept()

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
            SettingsDialog(self).exec()
            event.accept()

    def _snap_to_edge(self) -> None:
        screen = QApplication.screenAt(self.frameGeometry().center()) or QApplication.primaryScreen()
        if screen is None:
            return
        geo = screen.availableGeometry()
        r = self.frameGeometry()
        snap = 24
        x, y = self.x(), self.y()
        if abs(r.left() - geo.left()) < snap:
            x = geo.left()
        if abs(r.right() - geo.right()) < snap:
            x = geo.right() - r.width() + 1
        if abs(r.top() - geo.top()) < snap:
            y = geo.top()
        if abs(r.bottom() - geo.bottom()) < snap:
            y = geo.bottom() - r.height() + 1
        self.move(x, y)

    def contextMenuEvent(self, event) -> None:
        menu = QMenu(self)
        menu.setStyleSheet(DARK_STYLESHEET)
        menu.addAction("إضافة ملف…", self._dialog_add_file)
        menu.addAction("إضافة مجلد…", self._dialog_add_folder)
        menu.addSeparator()
        menu.addAction("تبديل الاتجاه", self._toggle_orientation)
        menu.addAction("قفل/تحرير الموضع", self._toggle_lock)
        menu.addAction("دوماً في المقدمة", self._toggle_on_top)
        menu.addSeparator()
        menu.addAction("الإعدادات…", self._open_settings)
        menu.addSeparator()
        menu.addAction("خروج", QApplication.instance().quit)
        menu.exec(event.globalPos())

    def _toggle_orientation(self) -> None:
        self.config["orientation_h"] = not self.config.get("orientation_h", True)
        self._rebuild()
        self._save()
        self.update()

    def _toggle_lock(self) -> None:
        self.config["locked"] = not self.config.get("locked", False)
        self._save()

    def _toggle_on_top(self) -> None:
        self.config["always_on_top"] = not self.config.get("always_on_top", True)
        self._apply_config()
        self._save()

    def _open_settings(self) -> None:
        SettingsDialog(self).exec()

    def _dialog_add_file(self) -> None:
        files, _ = QFileDialog.getOpenFileNames(self, "إضافة اختصارات")
        for f in files:
            self._add_item(f)

    def _dialog_add_folder(self) -> None:
        folder = QFileDialog.getExistingDirectory(self, "إضافة مجلد")
        if folder:
            self._add_item(folder)

    def _add_item(self, path: str, index: int = None) -> None:
        try:
            path = os.path.abspath(path)
        except Exception:
            return
        if not os.path.exists(path):
            return
        norm = os.path.normcase(path)
        for it in self.items:
            if os.path.normcase(it.path) == norm:
                return
        item = DockItem(path)
        if index is None or index < 0 or index > len(self.items):
            self.items.append(item)
        else:
            self.items.insert(index, item)
        self._rebuild()
        self._save()

    def _move_item(self, src: int, dst: int) -> None:
        if src < 0 or src >= len(self.items):
            return
        dst = max(0, min(dst, len(self.items)))
        if dst == src or dst == src + 1:
            return
        item = self.items.pop(src)
        if dst > src:
            dst -= 1
        self.items.insert(dst, item)
        self._rebuild()
        self._save()

    def _is_acceptable(self, mime: QMimeData) -> bool:
        return mime.hasFormat(MIME_INTERNAL) or mime.hasUrls()

    def _map_to_dock(self, event, source: QWidget) -> QPoint:
        return self.mapFromGlobal(source.mapToGlobal(event.position().toPoint()))

    def _compute_drop_index(self, pos: QPoint) -> int:
        if not self.buttons:
            return 0
        for i, b in enumerate(self.buttons):
            g = b.geometry()
            if self.config.get("orientation_h", True):
                if pos.x() < g.center().x():
                    return i
            else:
                if pos.y() < g.center().y():
                    return i
        return len(self.buttons)

    def _compute_insert_line_pos(self, index: int) -> QPoint:
        if not self.buttons:
            return QPoint(self.width() // 2, self.height() // 2)
        index = max(0, min(index, len(self.buttons)))
        spacing = self.config.get("spacing", 8)
        if self.config.get("orientation_h", True):
            if index < len(self.buttons):
                return QPoint(self.buttons[index].geometry().left() - spacing // 2 - 1, 0)
            return QPoint(self.buttons[-1].geometry().right() + spacing // 2 + 1, 0)
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
            try:
                src_idx = int(bytes(mime.data(MIME_INTERNAL)).decode("utf-8"))
            except Exception:
                src_idx = -1
            if src_idx >= 0:
                self._move_item(src_idx, target)
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
                                self.items.insert(max(0, min(target, len(self.items))), DockItem(path))
                                target += 1
                                added = True
                    except Exception:
                        pass
            if added:
                self._save()
        self._drop_index = -1
        self._rebuild()
        self.update()
        event.acceptProposedAction()

    def _on_icon_ready(self, path: str, size: int, img) -> None:
        if img is None or img.isNull():
            return
        for b in self.buttons:
            if os.path.normcase(b.item.path) == os.path.normcase(path) and size >= b._last_icon_size:
                b._set_icon_img(img)

    def dragEnterEvent(self, event) -> None:
        self._handle_drag_enter(event, self)

    def dragMoveEvent(self, event) -> None:
        self._handle_drag_move(event, self)

    def dragLeaveEvent(self, event) -> None:
        self._drop_index = -1
        self.update()

    def dropEvent(self, event) -> None:
        self._handle_drop(event, self)

# ---------------------------------------------------------------------------
# Settings dialog
# ---------------------------------------------------------------------------
DARK_STYLESHEET = """
QDialog, QMessageBox, QInputDialog, QFileDialog { background-color:#15171B; color:#E2D8B5; font-family:'Segoe UI',sans-serif; font-size:9pt; }
QLabel { color:#C8C8C8; background: transparent; }
QGroupBox { border:1px solid #2A2C30; border-radius:8px; margin-top:12px; padding-top:10px; color:#E2D8B5; font-weight:600; }
QGroupBox::title { subcontrol-origin: margin; left:10px; padding:0 4px; color:#E2D8B5; }
QSlider::groove:horizontal { height:4px; background:#2A2C30; border-radius:2px; }
QSlider::handle:horizontal { background:#E2D8B5; width:14px; margin:-6px 0; border-radius:7px; }
QSlider::handle:horizontal:hover { background:#E86F2C; }
QCheckBox { color:#C8C8C8; }
QPushButton { background-color:#1E2025; color:#E2D8B5; border:1px solid #2A2C30; padding:6px 14px; border-radius:6px; }
QPushButton:hover { border-color:#E86F2C; color:#E86F2C; }
QPushButton:pressed { background-color:#E86F2C; color:#111215; }
QListWidget { background-color:#1A1C20; border:1px solid #2A2C30; border-radius:6px; color:#C8C8C8; }
QListWidget::item { padding:4px 8px; }
QListWidget::item:selected { background-color:#E86F2C; color:#111215; }
QMenu { background-color:#1A1C20; color:#E2D8B5; border:1px solid #2A2C30; border-radius:6px; padding:4px; }
QMenu::item { padding:6px 24px; border-radius:4px; }
QMenu::item:selected { background-color:#E86F2C; color:#111215; }
QMenu::separator { height:1px; background-color:#2A2C30; margin:4px 8px; }
"""

class SettingsDialog(QDialog):
    def __init__(self, bar: DockBar):
        super().__init__(bar)
        self.bar = bar
        self._snapshot = json.loads(json.dumps(bar.config))
        self.setWindowTitle("إعدادات الشريط")
        self.setModal(True)
        self.setMinimumWidth(420)
        self.setStyleSheet(DARK_STYLESHEET)

        cfg = bar.config
        root = QVBoxLayout(self)
        root.setSpacing(10)

        app_box = QGroupBox("المظهر")
        form = QFormLayout(app_box)

        self.icon_size = self._add_slider(form, "حجم الأيقونة:", cfg["icon_size"], 24, 96)
        self.bar_padding = self._add_slider(form, "سماكة الشريط:", cfg.get("bar_padding", 9), 0, 40)
        self.spacing = self._add_slider(form, "التباعد:", cfg.get("spacing", 8), 0, 48)
        self.opacity = self._add_slider(form, "الشفافية:", int(cfg.get("opacity", 0.92) * 100), 30, 100)
        self.radius = self._add_slider(form, "استدارة الحواف:", int(cfg.get("radius", 16)), 0, 40)
        root.addWidget(app_box)

        colors = QGroupBox("الألوان")
        c_form = QFormLayout(colors)
        self.bg_btn = self._add_color_btn(c_form, "الخلفية:", cfg.get("bg_color", "#0E0F12"))
        self.border_btn = self._add_color_btn(c_form, "الإطار:", cfg.get("border_color", "#26282E"))
        self.accent_btn = self._add_color_btn(c_form, "اللون المميز:", cfg.get("accent", "#E86F2C"))
        root.addWidget(colors)

        list_box = QGroupBox("الاختصارات")
        s_lay = QVBoxLayout(list_box)
        self.list_widget = QListWidget()
        self.list_widget.setMinimumHeight(140)
        s_lay.addWidget(self.list_widget)
        btn_row = QHBoxLayout()
        add_file_btn = QPushButton("إضافة ملف…")
        add_file_btn.clicked.connect(self._add_file)
        add_folder_btn = QPushButton("إضافة مجلد…")
        add_folder_btn.clicked.connect(self._add_folder)
        remove_btn = QPushButton("إزالة المحدد")
        remove_btn.clicked.connect(self._remove_selected)
        btn_row.addWidget(add_file_btn)
        btn_row.addWidget(add_folder_btn)
        btn_row.addStretch(1)
        btn_row.addWidget(remove_btn)
        s_lay.addLayout(btn_row)
        root.addWidget(list_box)

        box = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        box.accepted.connect(self._on_accept)
        box.rejected.connect(self._on_reject)
        root.addWidget(box)

        self._refresh_list()
        for s in (self.icon_size, self.bar_padding, self.spacing, self.opacity, self.radius):
            s.valueChanged.connect(self._preview)

    def _add_slider(self, form, label, value, lo, hi) -> QSlider:
        s = QSlider(Qt.Horizontal)
        s.setRange(lo, hi)
        s.setValue(int(value))
        form.addRow(label, s)
        return s

    def _add_color_btn(self, form, label, color) -> QPushButton:
        b = QPushButton(color)
        b.setStyleSheet(f"background-color:{color}; color:#fff;")
        b.clicked.connect(lambda: self._pick_color(b))
        form.addRow(label, b)
        return b

    def _pick_color(self, btn) -> None:
        c = QColorDialog.getColor(QColor(btn.text()), self, "اختر لوناً")
        if c.isValid():
            btn.setText(c.name())
            btn.setStyleSheet(f"background-color:{c.name()}; color:#fff;")
            self._preview()

    def _refresh_list(self) -> None:
        self.list_widget.clear()
        for it in self.bar.items:
            self.list_widget.addItem(QListWidgetItem(f"{it.name}  —  {it.path}"))

    def _preview(self) -> None:
        cfg = self.bar.config
        cfg["icon_size"] = self.icon_size.value()
        cfg["bar_padding"] = self.bar_padding.value()
        cfg["spacing"] = self.spacing.value()
        cfg["opacity"] = self.opacity.value() / 100.0
        cfg["radius"] = self.radius.value()
        cfg["bg_color"] = self.bg_btn.text()
        cfg["border_color"] = self.border_btn.text()
        cfg["accent"] = self.accent_btn.text()
        self.bar._apply_config()

    def _add_file(self) -> None:
        files, _ = QFileDialog.getOpenFileNames(self, "إضافة ملفات")
        for f in files:
            self.bar._add_item(f)
        self._refresh_list()

    def _add_folder(self) -> None:
        folder = QFileDialog.getExistingDirectory(self, "إضافة مجلد")
        if folder:
            self.bar._add_item(folder)
        self._refresh_list()

    def _remove_selected(self) -> None:
        row = self.list_widget.currentRow()
        if 0 <= row < len(self.bar.items):
            self.bar.remove_item(self.bar.items[row])
            self._refresh_list()

    def _on_accept(self) -> None:
        self._preview()
        self.bar._save()
        self.accept()

    def _on_reject(self) -> None:
        self.bar.config.clear()
        self.bar.config.update(self._snapshot)
        self.bar._apply_config()
        self.reject()

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
    for x_ratio in (0.30, 0.50, 0.70):
        p.drawEllipse(QPoint(int(size * x_ratio), cy), r, r)
    p.end()
    return pm

class TrayIcon(QSystemTrayIcon):
    def __init__(self, bar: DockBar):
        super().__init__()
        self.bar = bar
        self.setIcon(QIcon(_make_tray_pixmap(64)))
        self.setToolTip("شريط الاختصارات")
        menu = QMenu()
        menu.setStyleSheet(DARK_STYLESHEET)
        act_show = menu.addAction("إظهار/إخفاء الشريط")
        act_show.triggered.connect(self._toggle_visible)
        menu.addSeparator()
        menu.addAction("الإعدادات…", self.bar._open_settings)
        menu.addAction("إضافة اختصار…", self.bar._dialog_add_file)
        menu.addAction("إضافة مجلد…", self.bar._dialog_add_folder)
        menu.addSeparator()
        menu.addAction("خروج", self._exit_app)
        self.setContextMenu(menu)
        self.activated.connect(
            lambda reason: self._toggle_visible() if reason == QSystemTrayIcon.Trigger else None)

    def _toggle_visible(self) -> None:
        if self.bar.isVisible():
            self.bar.hide()
        else:
            self.bar.show()
            self.bar.raise_()

    def _exit_app(self) -> None:
        try:
            self.hide()
        except Exception:
            pass
        QApplication.instance().quit()

# ---------------------------------------------------------------------------
# Crash hook & main
# ---------------------------------------------------------------------------
def _install_crash_hook() -> None:
    def hook(t, v, tb):
        try:
            _log("FATAL: " + "".join(traceback.format_exception(t, v, tb)))
        except Exception:
            pass
        app = QApplication.instance()
        if app is not None:
            QMessageBox.critical(None, "خطأ في الشريط",
                                 "حدث خطأ غير متوقع. التفاصيل في dock_bar_debug.log\n" + str(v))
    sys.excepthook = hook

def main() -> int:
    app = QApplication(sys.argv)
    app.setStyle("Fusion")
    app.setQuitOnLastWindowClosed(False)
    app.setApplicationName("Dock Bar")
    _install_crash_hook()
    _log(f"start pid={os.getpid()} file={Path(__file__).name}")
    _log(f"unhandled extracts in {APP_DIR}")

    bar = DockBar()
    bar.show()
    if QSystemTrayIcon.isSystemTrayAvailable():
        tray = TrayIcon(bar)
        tray.show()
        app.setAttribute(Qt.AA_DisableWindowContextHelpButton, True)

    return app.exec()

if __name__ == "__main__":
    sys.exit(main())
