"""Generate input video only: no uploads, workspaces, results or workflow state."""
import os
from pathlib import Path
import cv2
import numpy as np

root = Path(__file__).resolve().parents[2]
target = Path(os.environ['CRITICAL_VIDEO']).resolve()
assert target.is_relative_to(root/'work'), 'Disposable input under work/ required'
target.parent.mkdir(parents=True, exist_ok=True)
writer = cv2.VideoWriter(str(target), cv2.VideoWriter_fourcc(*'MJPG'), 10, (640, 480))
assert writer.isOpened()
for fi in range(3):
    frame = np.full((480, 640, 3), 40+fi*30, dtype=np.uint8)
    cv2.circle(frame, (250, 200), 12, (230, 230, 230), -1)
    writer.write(frame)
writer.release()
print('Created raw input video only; browser must upload and create every annotation.')
