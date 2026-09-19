"""Repeat the tracked synthetic experiment in a temporary directory; compare evidence."""
import json,tempfile,hashlib
from pathlib import Path
from argparse import Namespace
from scripts.evaluate_priority import run,write
def main():
    original=Path('docs/evaluation/phase7')
    with tempfile.TemporaryDirectory(prefix='mailmind-phase7-repro-') as directory:
     root=Path(directory)
     second=run(Namespace(dataset='fixtures/priority_benchmark',output=str(root/'models'),report_dir=str(root/'report'),seed=42,base_epochs=20,personal_epochs=5,cloud_predictions=None))
     first=json.loads((original/'metrics.json').read_text())
     checks={key:first[key]==second[key] for key in ['dataset_hash','systems','comparisons','checkpoint_versions','softmax_coverage_points']}
     checks['split_manifest']=json.loads((original/'split_manifest.json').read_text())==json.loads((root/'report/split_manifest.json').read_text())
     checks['test_predictions']=json.loads((original/'test_predictions.json').read_text())==json.loads((root/'report/test_predictions.json').read_text())
     checks['retrieval_policy']=json.loads((original/'retrieval_policy.json').read_text())==json.loads((root/'report/retrieval_policy.json').read_text())
     for role in ['base','personal']:
      def sha(path):return hashlib.sha256(path.read_bytes()).hexdigest()
      checks[role+'_weights_sha256']=sha(Path('models/phase7-synthetic-v2')/role/'model.safetensors')==sha(root/'models'/role/'model.safetensors')
     write(original/'reproducibility.json',{'same_environment':True,'seed':42,'checks':checks,'all_passed':all(checks.values()),'note':'Timing is measured independently and is not expected to match; reproducibility across other package versions or hardware is not claimed.'})
     print(checks)
     assert all(checks.values())
    
    

if __name__=='__main__':
    main()
