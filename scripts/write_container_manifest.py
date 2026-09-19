"""Verify local demo assets and write the inventory for their fixed container mount."""
import json
from pathlib import Path
from src.offline_assets import verify_role


def write(root='models/offline-demo-v1'):
    root=Path(root);manifest=root/'manifest.json'
    for role in ['classifier','embedding']:verify_role(manifest,role)
    data=json.loads(manifest.read_text(encoding='utf-8'))
    for role in data['assets']:data['assets'][role]['root']='/app/models/'+role
    (root/'manifest.container.json').write_text(json.dumps(data,indent=2)+'\n',encoding='utf-8')

if __name__=='__main__':write()
