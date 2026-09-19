from __future__ import annotations

import os
from pathlib import Path

# 读取 .env 文件
try:
    from dotenv import load_dotenv
    load_dotenv(Path(__file__).resolve().parents[1] / ".env")
except ImportError:
    pass

BACKEND_DIR = Path(__file__).resolve().parents[1]
TRACK_DATA_DIR = BACKEND_DIR / "track_data"
TRACK_MODUL_DIR = BACKEND_DIR / "track_modul"
DATA_DIR = BACKEND_DIR / "data"
DB_FILE = DATA_DIR / "app.db"

HOST = os.getenv("BACKEND_HOST", "127.0.0.1")
PORT = int(os.getenv("BACKEND_PORT", "3000"))
JWT_SECRET = os.getenv("JWT_SECRET", "dev-secret-change-me")
TOKEN_EXPIRES_IN_SEC = 7 * 24 * 60 * 60

DEFAULT_MODEL = TRACK_MODUL_DIR / "facebook--sam3" / "snapshots" / "master"
MODEL_ID = os.getenv("SAM3_MODEL_ID", str(DEFAULT_MODEL))
DEVICE = os.getenv("SAM3_DEVICE", "cuda")
DTYPE = os.getenv("SAM3_DTYPE", "bfloat16")

MAX_VIDEO_BYTES = 2 * 1024 * 1024 * 1024
# 每次点击 AI Tracking 最多处理的帧数。
# 修改这里即可全局调整，例如改成 10；前后端都会自动使用这个值。
TRACK_FRAMES = int(os.getenv("SAM3_TRACK_FRAMES", "120"))
if TRACK_FRAMES < 1:
    TRACK_FRAMES = 1

# AI Tracking 新流程：先用帧差法预估需要向后处理多少帧，再交给 SAM3。
FRAME_DIFF_MAX_SEARCH_FRAMES = int(os.getenv("FRAME_DIFF_MAX_SEARCH_FRAMES", "120"))
FRAME_DIFF_CONFIRM_FRAMES = int(os.getenv("FRAME_DIFF_CONFIRM_FRAMES", "3"))
FRAME_DIFF_MAX_CONFIRM_MISS = int(os.getenv("FRAME_DIFF_MAX_CONFIRM_MISS", "2"))
FRAME_DIFF_THRESHOLD = int(os.getenv("FRAME_DIFF_THRESHOLD", "12"))
FRAME_DIFF_KNOWN_MARGIN = int(os.getenv("FRAME_DIFF_KNOWN_MARGIN", "25"))
FRAME_DIFF_MIN_AREA = int(os.getenv("FRAME_DIFF_MIN_AREA", "15"))
FRAME_DIFF_MIN_AREA_RATIO = float(os.getenv("FRAME_DIFF_MIN_AREA_RATIO", "0.55"))
FRAME_DIFF_TEMPLATE_THRESHOLD = float(os.getenv("FRAME_DIFF_TEMPLATE_THRESHOLD", "0.35"))
FRAME_DIFF_ROI_RECT = os.getenv("FRAME_DIFF_ROI_RECT", "")
FRAME_DIFF_VERBOSE_LOG = os.getenv("FRAME_DIFF_VERBOSE_LOG", "1").strip().lower() not in {"0", "false", "no"}

# Raw-frame mode: SAM3 uses the original video FPS and source frame numbers.
