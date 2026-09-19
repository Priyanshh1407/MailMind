"""Package an explicitly chosen classifier and already cached public embedding. No download."""
import argparse,json,shutil
from pathlib import Path
from src.offline_assets import CLASSIFIER_FILES,EMBEDDING_FILES,file_hash,verify_role


def prepare(model_path,embedding_path,output):
    output=Path(output)
    if output.exists() and any(output.iterdir()):raise ValueError('Choose a new empty asset directory')
    roots={'classifier':Path(model_path),'embedding':Path(embedding_path)}
    for role,root in roots.items():
        required=CLASSIFIER_FILES if role=='classifier' else EMBEDDING_FILES
        if not all((root/name).is_file() for name in required):raise ValueError('Required local assets unavailable; provision explicitly while online first')
    output.mkdir(parents=True,exist_ok=True);entries={}
    for role,root in roots.items():
        destination=output/role;destination.mkdir()
        required=CLASSIFIER_FILES if role=='classifier' else EMBEDDING_FILES
        names=list(required)+(['special_tokens_map.json'] if role=='classifier' and (root/'special_tokens_map.json').is_file() else [])
        for name in names:shutil.copyfile(root/name,destination/name)
        entries[role]={'root':str(destination.resolve()),'files':{name:file_hash(destination/name) for name in names}}
    manifest=output/'manifest.json'
    manifest.write_text(json.dumps({'schema':1,'embedding_id':'chroma-default-all-MiniLM-L6-v2','assets':entries,'note':'Local integrity inventory, not independent upstream authenticity proof. Recreate after moving directories.'},indent=2)+'\n',encoding='utf-8')
    for role in roots:verify_role(manifest,role)
    return manifest


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--model',required=True);parser.add_argument('--embedding-cache',required=True);parser.add_argument('--output',required=True)
    args=parser.parse_args();print(prepare(args.model,args.embedding_cache,args.output))
