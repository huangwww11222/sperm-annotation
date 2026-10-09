"""Isolated browser-test host. Production never imports this entry point.

Only model outputs are deterministic; tracking orchestration, storage and HTTP
routes remain real. This does not validate GPU inference or model accuracy.
"""
import argparse
import os
from pathlib import Path
from types import SimpleNamespace

root = Path(__file__).resolve().parents[2]
for name in ('APP_DATA_DIR', 'APP_DB_FILE', 'APP_STORAGE_DIR'):
    value = Path(os.environ[name]).resolve()
    assert value.is_relative_to(root/'work') and 'e2e-confirm-ux-' in str(value), name

from app import config
config.LEGACY_TRACK_DATA_DIR = Path(os.environ['APP_STORAGE_DIR'])/'unused-legacy'
from app import db, main, tracker
from app.auth import hash_password

db.init_db()
for expected, name in enumerate(('confirm-A', 'confirm-B', 'confirm-C'), 1):
    if not db.get_user(name):
        assert db.create_user(name, hash_password('confirmation-test')) == expected


class SimulatedModel:
    model_id = 'browser-test-simulated-model'
    device = 'cpu'
    torch_dtype = 'float32'

    def make_tracker_session(self, frames):
        count=sum(1 for _ in frames)
        return SimpleNamespace(count=count, source_start=frames.meta['source_frame_indices'][0], objects=[])

    def add_manual_boxes(self, session, frame, objects):
        session.objects = objects

    def propagate_manual(self, session, max_frames, start_frame_idx):
        for fi in range(start_frame_idx, min(session.count, start_frame_idx+max_frames)):
            yield SimpleNamespace(frame_idx=fi)

    def decode_tracker_output(self, session, output):
        boxes=[]
        for o in session.objects:
            box=list(o['bbox'])
            if session.source_start == 0 and output.frame_idx == 1 and o.get('name','').startswith('audit-pause'):
                box[2]=box[0]+(box[2]-box[0])*.2
            boxes.append(SimpleNamespace(object_id=o['object_id'],bbox=box,score=.99,mask_area=None,sam3_object_id=o['object_id']))
        return boxes,None


assert os.environ.get('BROWSER_SIMULATED_MODEL') == '1', 'Explicit test-only model opt-in required'
main.SAM3_ENABLED = True
tracker.get_sam3_engine = lambda *args, **kwargs: SimulatedModel()

if __name__ == '__main__':
    import uvicorn
    parser = argparse.ArgumentParser()
    parser.add_argument('--port', type=int, required=True)
    parser.add_argument('--host', default='127.0.0.1')
    args = parser.parse_args()
    print('TEST ONLY: simulated model; real API, video decoding, tracking publication and workflows', flush=True)
    uvicorn.run(main.app, host=args.host, port=args.port)
