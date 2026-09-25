"""
生成审查模式测试数据。

流程：
1. 用 cv2 生成一段模拟"精子视频"（几个移动的椭圆 + 尾巴），并记录每个对象每帧的 bbox
2. 上传视频到后端（POST /api/track/upload）→ 拿到 mediaId
3. 冻结 baseline（POST /api/review/baselines/freeze）→ 拿到 baselineId
4. 创建审查会话（POST /api/review/sessions）→ 拿到 sessionId

最后打印 mediaId / baselineId / sessionId，供前端审查页面直接测试。

用法：
    cd backend
    python scripts/gen_review_test_data.py
"""
from __future__ import annotations

import json
import math
import sys
import tempfile
from pathlib import Path

import cv2
import numpy as np
import requests

API = "http://localhost:8000/api"
USERNAME = "debuguser"
PASSWORD = "debug123456"

# 视频参数
WIDTH, HEIGHT = 640, 480
FPS = 15
FRAME_COUNT = 12
NUM_SPERMS = 5

# 颜色（BGR）
BG_COLOR = (28, 28, 36)          # 深色背景
SPERM_COLOR = (220, 230, 240)    # 浅色精子
TAIL_COLOR = (160, 170, 190)


def make_sperm(frame_count: int) -> list[dict]:
    """生成一个精子在所有帧的轨迹 + 每帧 bbox。"""
    # 随机初始位置、方向、速度
    rng = np.random.default_rng()
    cx = rng.uniform(80, WIDTH - 80)
    cy = rng.uniform(60, HEIGHT - 60)
    angle = rng.uniform(0, 2 * math.pi)
    speed = rng.uniform(1.5, 3.5)        # 每帧像素
    w = rng.uniform(18, 28)               # bbox 宽
    h = rng.uniform(10, 16)               # bbox 高
    frames: list[dict] = []
    for i in range(frame_count):
        # 轻微转向（游动感）
        angle += rng.uniform(-0.15, 0.15)
        cx += math.cos(angle) * speed
        cy += math.sin(angle) * speed
        # 边界反弹
        if cx < 40: cx, angle = 40, math.pi - angle
        if cx > WIDTH - 40: cx, angle = WIDTH - 40, math.pi - angle
        if cy < 30: cy, angle = 30, -angle
        if cy > HEIGHT - 30: cy, angle = HEIGHT - 30, -angle
        x1 = int(round(cx - w / 2))
        y1 = int(round(cy - h / 2))
        x2 = int(round(cx + w / 2))
        y2 = int(round(cy + h / 2))
        frames.append({
            "cx": float(cx), "cy": float(cy),
            "angle": float(angle),
            "w": float(w), "h": float(h),
            "bbox": [x1, y1, x2, y2],
        })
    return frames


def draw_frame(sperms: list[list[dict]], frame_idx: int) -> np.ndarray:
    """画一帧：背景 + 每个精子的椭圆头 + 尾巴。"""
    img = np.full((HEIGHT, WIDTH, 3), BG_COLOR, dtype=np.uint8)
    # 加一点噪点，模拟显微镜背景
    noise = rng_global.integers(0, 18, (HEIGHT, WIDTH, 3), dtype=np.uint8)
    img = cv2.add(img, noise)
    for sp in sperms:
        s = sp[frame_idx]
        cx, cy = int(s["cx"]), int(s["cy"])
        w, h = int(s["w"]), int(s["h"])
        angle_deg = math.degrees(s["angle"])
        # 头（椭圆）
        cv2.ellipse(img, (cx, cy), (w // 2, h // 2), angle_deg, 0, 360, SPERM_COLOR, -1, cv2.LINE_AA)
        # 尾巴（从头部向后延伸的曲线）
        tail_len = int(w * 2.2)
        tail_pts = []
        for t in range(8):
            tt = t / 7
            # 尾巴略带波形
            wave = math.sin(tt * math.pi * 2 + frame_idx * 0.4) * 4
            tx = cx - math.cos(s["angle"]) * (tail_len * tt)
            ty = cy - math.sin(s["angle"]) * (tail_len * tt) + wave
            tail_pts.append((int(tx), int(ty)))
        for a, b in zip(tail_pts[:-1], tail_pts[1:]):
            cv2.line(img, a, b, TAIL_COLOR, 1, cv2.LINE_AA)
    return img


rng_global = np.random.default_rng(42)


def login() -> str:
    r = requests.post(f"{API}/auth/login",
                      json={"username": USERNAME, "password": PASSWORD}, timeout=10)
    r.raise_for_status()
    return r.json()["token"]


def upload_video(token: str, video_path: Path) -> str:
    with video_path.open("rb") as f:
        r = requests.post(f"{API}/track/upload",
                          headers={"Authorization": f"Bearer {token}"},
                          files={"file": (video_path.name, f, "video/mp4")}, timeout=60)
    r.raise_for_status()
    data = r.json()
    print(f"[upload] mediaId={data['mediaId']}  frames={data['frameCount']}  "
          f"{data['width']}x{data['height']} @ {data['fps']}fps")
    return data["mediaId"]


def freeze_baseline(token: str, media_id: str, sperms: list[list[dict]]) -> str:
    frames = []
    for fi in range(FRAME_COUNT):
        objs = []
        for oi, sp in enumerate(sperms, start=1):
            objs.append({
                "objectId": oi,
                "bbox": sp[fi]["bbox"],
                "classKey": "sperm",
            })
        frames.append({"frameIndex": fi, "coverage": "objects", "objects": objs})
    r = requests.post(f"{API}/review/baselines/freeze",
                      headers={"Authorization": f"Bearer {token}"},
                      json={"mediaId": media_id, "frames": frames}, timeout=30)
    r.raise_for_status()
    bl = r.json()["baseline"]
    print(f"[freeze] baselineId={bl['id']}  frames={bl.get('frameCount')}  "
          f"objects={bl.get('objectCount')}")
    return bl["id"]


def create_session(token: str, baseline_id: str) -> str:
    r = requests.post(f"{API}/review/sessions",
                      headers={"Authorization": f"Bearer {token}"},
                      json={"baselineId": baseline_id}, timeout=30)
    r.raise_for_status()
    sess = r.json()["session"]
    print(f"[session] sessionId={sess['id']}  frames={sess.get('frameCount')}  "
          f"state={sess.get('state')}")
    return sess["id"]


def main() -> int:
    print("=" * 60)
    print("生成审查模式测试数据")
    print("=" * 60)

    # 1. 生成精子轨迹
    print(f"\n[1/4] 生成 {NUM_SPERMS} 个精子 × {FRAME_COUNT} 帧 的轨迹...")
    sperms = [make_sperm(FRAME_COUNT) for _ in range(NUM_SPERMS)]

    # 2. 渲染视频
    print(f"[2/4] 渲染视频 {WIDTH}x{HEIGHT} @ {FPS}fps ({FRAME_COUNT} 帧)...")
    fourcc = cv2.VideoWriter_fourcc(*"mp4v")
    tmp = Path(tempfile.mkdtemp())
    video_path = tmp / "sperm_test.mp4"
    writer = cv2.VideoWriter(str(video_path), fourcc, FPS, (WIDTH, HEIGHT))
    for fi in range(FRAME_COUNT):
        writer.write(draw_frame(sperms, fi))
    writer.release()
    print(f"      视频已写入: {video_path}  ({video_path.stat().st_size} bytes)")

    # 3. 上传 + 冻结 + 创建会话
    token = login()
    print(f"[3/4] 登录成功 (uid={json.loads(__import__('base64').b64decode(token.split('.')[1] + '==')).get('uid')})")

    media_id = upload_video(token, video_path)
    baseline_id = freeze_baseline(token, media_id, sperms)
    session_id = create_session(token, baseline_id)

    # 4. 输出
    print("\n" + "=" * 60)
    print("✅ 测试数据生成完成！")
    print("=" * 60)
    print(f"  mediaId    = {media_id}")
    print(f"  baselineId = {baseline_id}")
    print(f"  sessionId  = {session_id}")
    print(f"\n在前端打开 /review，找到会话即可开始审查测试。")
    print(f"视频文件保留在: {video_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
