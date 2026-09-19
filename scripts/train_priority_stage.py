"""Safe stage entry points: explicit provenance, grouped validation, no test training."""
import argparse
import json
from pathlib import Path
from src.benchmark_data import prepare_rows,group_splits,personalization_rows,digest
from src.priority_training import train


def main(stage):
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--dataset',default='fixtures/priority_benchmark')
    parser.add_argument('--output',required=True)
    parser.add_argument('--seed',type=int,default=42)
    parser.add_argument('--epochs',type=int,default=20 if stage=='base' else 5)
    parser.add_argument('--base-checkpoint',required=stage=='personal')
    args=parser.parse_args();root=Path(args.dataset);output=Path(args.output)
    if output.exists() and any(output.iterdir()): raise ValueError('Choose a new output directory; never overwrite an existing checkpoint')
    manifest=json.loads((root/'provenance.json').read_text(encoding='utf-8'))
    rows=prepare_rows(json.loads((root/'emails.json').read_text(encoding='utf-8')),manifest)
    splits=group_splits(rows,args.seed);personal=personalization_rows(splits['train'],args.seed)
    ids={row['id'] for row in personal}
    selected=[row for row in splits['train'] if row['id'] not in ids] if stage=='base' else personal
    _,_,history=train(selected,splits['validation'],output,seed=args.seed,epochs=args.epochs,base_path=args.base_checkpoint if stage=='personal' else None)
    (output/'dataset_manifest.json').write_text(json.dumps({'dataset_hash':digest(rows),'selection_split':'validation','test_ids':[row['id'] for row in splits['test']],
        'training_ids':[row['id'] for row in selected],'seed':args.seed},indent=2)+'\n',encoding='utf-8')
    print(f'Saved {stage} candidate. Evaluate held-out labels before any promotion.')
