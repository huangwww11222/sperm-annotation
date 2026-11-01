"""Install old over-sensitive review items in the regression's own video only."""
import json
import os
import sys
from pathlib import Path
from app import annotation_review as review, db


def install(media_id):
    root=Path(os.environ['APP_STORAGE_DIR']).resolve()
    assert 'e2e-confirm-ux-' in str(root) and root.is_relative_to(Path(__file__).resolve().parents[2]/'work')
    directory=root/'media'/media_id
    rows=[json.loads(s) for s in (directory/'tracker_results.json').read_text().splitlines()]
    for fi,kind in [(0,'small'),(1,'large')]:
        box=rows[fi]['objects'][0]['bbox'];cx=(box[0]+box[2])/2;cy=(box[1]+box[3])/2
        other=[cx-40,cy-29,cx+40,cy+29] if kind=='small' else [box[0]+120,box[1],box[2]+120,box[3]]
        rows[fi].setdefault('reviewEvidence',{}).setdefault('findings',[]).append(dict(kind='endpoint_disagreement' if kind=='small' else 'detector_mismatch',objectId=1,bbox=box,reverseBox=other,message='人工首尾独立追踪位置不一致，请核对头部和身份'))
    # The browser created this override through normal editing and saving.
    # Never race its automatic saves by replacing workspace_state here.
    state=json.loads((directory/'workspace_state.json').read_text())
    assert any(o.get('source')=='manual' and o.get('objectId')==1 and o.get('frameIndex')==0 for o in state['manualAnnotations'])
    x,y,x2,y2=rows[0]['objects'][0]['bbox']
    rows[0]['reviewEvidence']['findings'].append(dict(kind='head_boundary',objectId=1,bbox=[x,y,x2,y2],message='人工框不该进入清单'))
    rows[1]['reviewEvidence']['findings'].append(dict(kind='association_uncertain',objectId=2,bbox=rows[1]['objects'][1]['bbox'],message='不能显示的长关联诊断'))
    # Detector evidence must be present on two five-frame sample points to form
    # a reviewable path; a lone sample is deliberately discarded.
    for fi in [1,6,11]:
        for x in [100,300,500,700]:
            shift=(fi-1)//5*40
            bbox=[x+shift,330,x+20+shift,350]
            rows[fi].setdefault('reviewEvidence',{}).setdefault('findings',[]).append(dict(kind='unlabelled_candidate',objectId=None,bbox=bbox,message='不能显示的长漏标诊断'))
    # Distant uncached frames exercise read failure/retry and stale navigation.
    for fi,x in [(39,740),(44,600),(49,100),(54,180)]:
        rows[fi].setdefault('reviewEvidence',{}).setdefault('findings',[]).append(dict(kind='unlabelled_candidate',objectId=None,bbox=[x,390,x+8,398],message='小头部候选'))
    temp=directory/'review-fixture-results.tmp'
    temp.write_text('\n'.join(json.dumps(r) for r in rows))
    temp.replace(directory/'tracker_results.json')
    with db.connect() as c:
        row=review.latest(c,media_id);assert row and row['state']=='open'
        items=json.loads(row['items_json'])
        for fi,name in [(0,'small'),(1,'large')]:
            items.append(dict(id='legacy-endpoint-'+name,kind='doubt',findingKind='endpoint_disagreement',frame=fi,startFrame=fi,endFrame=fi,objectId=1,message='旧首尾追踪位置不一致'))
        c.execute('UPDATE annotation_reviews SET items_json=?,evidence_policy=NULL WHERE id=?',(json.dumps(items),row['id']))


if __name__=='__main__':install(sys.argv[1])
