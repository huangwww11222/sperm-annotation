"""Raw encoded inputs only; the browser creates every account and workflow."""
import os
from pathlib import Path
import shutil
import subprocess
import cv2
import numpy as np
from independent_browser_audit_runner import video, references

root = Path(__file__).resolve().parents[2]
base = Path(os.environ['INDEPENDENT_AUDIT_ROOT']).resolve()
assert base.is_relative_to(root/'work') and base.name.startswith('e2e-confirm-ux-')
base.mkdir(parents=True, exist_ok=True)
video(base/'small.avi', 640, 480, 3)
video(base/'4k.avi', 3840, 2160, 60)
references(base/'4k.avi', base/'reference-frames.json')
playable = base/'playable.mp4'
if shutil.which('ffmpeg'):
    subprocess.run(['ffmpeg', '-hide_banner', '-loglevel', 'error', '-y', '-f', 'lavfi', '-i',
                    'color=c=blue:s=640x480:r=10:d=3', '-c:v', 'libx264', '-pix_fmt', 'yuv420p',
                    '-movflags', '+faststart', str(playable)], check=True)
else:
    writer = cv2.VideoWriter(str(playable), cv2.VideoWriter_fourcc(*'avc1'), 10, (640,480))
    assert writer.isOpened(), 'A real H.264 encoder is required for native playback validation'
    for fi in range(30):
        writer.write(np.full((480,640,3), 20+fi*3, dtype=np.uint8))
    writer.release()
print('Created isolated AVI/4K/H.264 inputs and independent decoded references')
