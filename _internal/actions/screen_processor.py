"""
Screen & webcam capture for JARVIS vision.

Provides the two capture entry points main.py uses — `_capture_screen()` and
`_capture_camera()` — plus their helpers (compression, camera auto-detection,
config access). main.py grabs a frame here on demand, then injects it into the
main Gemini Live session; there is no separate vision session here.
"""
from __future__ import annotations

import io
import json
import sys
from pathlib import Path

import numpy as np

from core.lazyimport import LazyModule, installed

# OpenCV is 224 ms and is only ever touched by the webcam path — see
# core/lazyimport.py. Screen capture, which is what this action does on almost
# every call, never reaches it. numpy above stays eager: main.py already
# imports it for audio, so it is in memory before this file is read.
_CV2 = installed("cv2")

cv2 = LazyModule("cv2")

try:
    import mss
    import mss.tools
    _MSS = True
except ImportError:
    _MSS = False

try:
    import PIL.Image
    _PIL = True
except ImportError:
    _PIL = False


def _base_dir() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).parent
    return Path(__file__).resolve().parent.parent


_BASE        = _base_dir()
from core.user_data import API_CONFIG_PATH
_CONFIG_PATH = API_CONFIG_PATH


def _load_config() -> dict:
    try:
        return json.loads(_CONFIG_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _save_config_key(key: str, value) -> None:
    try:
        cfg = _load_config()
        cfg[key] = value
        _CONFIG_PATH.write_text(json.dumps(cfg, indent=4), encoding="utf-8")
    except Exception as e:
        print(f"[Vision] ⚠️  Could not save config key '{key}': {e}")


def _get_os() -> str:
    return _load_config().get("os_system", "windows").lower()


_IMG_MAX_W = 1280
_IMG_MAX_H = 720
_JPEG_Q    = 82


def _compress(img_bytes: bytes, source_format: str = "PNG") -> tuple[bytes, str]:
    if not _PIL:
        return img_bytes, f"image/{source_format.lower()}"

    try:
        img = PIL.Image.open(io.BytesIO(img_bytes)).convert("RGB")
        img.thumbnail((_IMG_MAX_W, _IMG_MAX_H), PIL.Image.BILINEAR)
        buf = io.BytesIO()
        img.save(buf, format="JPEG", quality=_JPEG_Q, optimize=False)
        return buf.getvalue(), "image/jpeg"
    except Exception as e:
        print(f"[Vision] ⚠️  Image compress failed: {e}")
        return img_bytes, f"image/{source_format.lower()}"


def _capture_screen(monitor=None) -> tuple[bytes, str]:
    """A frame from one display.

    `monitor` is a 1-based display index, 'primary', or 'all'. The default is
    None, which means the primary display — byte-for-byte what this function
    did before the argument existed, so every existing caller is unaffected.

    THE BUG THIS ARGUMENT FIXES
        The old body always grabbed `monitors[1]`, the primary. On a
        multi-screen machine that made "what's on my second monitor?" return a
        reading of the FIRST monitor, delivered with no indication that
        anything had been missed. Not a crash — a confident wrong answer, which
        is the failure mode core/outcome.py exists to prevent everywhere else
        in this project.

    Multi-display work should generally go through actions/display_manager.py,
    which can also stitch every screen into one frame and locate a window. This
    function stays because it is what main.py's vision path already calls.
    """
    if not _MSS:
        raise RuntimeError("mss is not installed. Run: pip install mss")

    if str(monitor).strip().lower() in ("all", "every"):
        # Delegate the stitch rather than duplicating it.
        try:
            from actions.display_manager import capture_all
            data, mime, _meta = capture_all()
            return data, mime
        except Exception:
            pass          # fall through to the primary rather than failing

    with mss.mss() as sct:
        monitors = sct.monitors          # [0] = all combined, [1..n] = real screens
        real     = monitors[1:] or monitors[:1]

        target = real[0]                 # primary, and the default
        if monitor not in (None, "", "primary", "main"):
            digits = "".join(c for c in str(monitor) if c.isdigit())
            if digits:
                idx = int(digits)
                if 1 <= idx <= len(real):
                    target = real[idx - 1]
                else:
                    raise RuntimeError(
                        f"display {idx} does not exist — this machine has "
                        f"{len(real)}")

        shot = sct.grab(target)
        png  = mss.tools.to_png(shot.rgb, shot.size)

    return _compress(png, "PNG")


def _cv2_backend() -> int:
    """Return the best OpenCV camera backend for the current OS."""
    if not _CV2:
        return 0
    os_name = _get_os()
    if os_name == "windows":
        return cv2.CAP_DSHOW
    if os_name == "mac":
        return cv2.CAP_AVFOUNDATION
    return cv2.CAP_ANY


def _probe_camera(index: int, backend: int, warmup: int = 5) -> bool:

    if not _CV2:
        return False
    cap = cv2.VideoCapture(index, backend)
    if not cap.isOpened():
        cap.release()
        return False
    for _ in range(warmup):
        cap.read()
    ret, frame = cap.read()
    cap.release()
    if not ret or frame is None:
        return False
    return bool(np.mean(frame) > 8)


def _detect_camera_index() -> int:

    backend = _cv2_backend()
    print("[Vision] 🔍 Auto-detecting camera...")
    for idx in range(6):
        if _probe_camera(idx, backend):
            print(f"[Vision] ✅ Camera found at index {idx}")
            _save_config_key("camera_index", idx)
            return idx
        print(f"[Vision] ⚠️  Camera index {idx}: no usable frame")

    print("[Vision] ⚠️  No camera found — defaulting to index 0")
    _save_config_key("camera_index", 0)
    return 0


def _get_camera_index() -> int:
    cfg = _load_config()
    if "camera_index" in cfg:
        return int(cfg["camera_index"])
    return _detect_camera_index()


def _capture_camera() -> tuple[bytes, str]:
    if not _CV2:
        raise RuntimeError("OpenCV (cv2) is not installed. Run: pip install opencv-python")

    index   = _get_camera_index()
    backend = _cv2_backend()
    cap     = cv2.VideoCapture(index, backend)

    if not cap.isOpened():
        raise RuntimeError(f"Camera index {index} could not be opened.")

    for _ in range(10):
        cap.read()

    ret, frame = cap.read()
    cap.release()

    if not ret or frame is None:
        raise RuntimeError("Camera returned no frame.")

    if _PIL:
        rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
        img = PIL.Image.fromarray(rgb)
        img.thumbnail((_IMG_MAX_W, _IMG_MAX_H), PIL.Image.BILINEAR)
        buf = io.BytesIO()
        img.save(buf, format="JPEG", quality=_JPEG_Q)
        return buf.getvalue(), "image/jpeg"

    _, buf = cv2.imencode(".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, _JPEG_Q])
    return buf.tobytes(), "image/jpeg"
