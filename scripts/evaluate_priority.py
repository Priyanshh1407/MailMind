"""Reproduce the synthetic priority experiment without Gmail, keys or cloud calls."""
import argparse
import json
import platform
import time
from pathlib import Path
from collections import Counter
from dataclasses import replace
from src.benchmark_data import prepare_rows,group_splits,personalization_rows,split_manifest,digest
from src.priority_training import train,infer
from src.evaluation import classification_metrics,compare_predictions,select_policy
from src.retrieval_policy import RetrievalPolicy,EMBEDDING_ID,vote
from src.email_text import format_email_text
from src.prediction import ID2LABEL


def write(path,value):
    path=Path(path);path.parent.mkdir(parents=True,exist_ok=True)
    path.write_text(json.dumps(value,indent=2,allow_nan=False)+'\n',encoding='utf-8')


def neighbors(queries,references,encoder):
    import numpy as np
    def vectors(rows):
        array=np.asarray(encoder([format_email_text(row['subject'],row['body']) for row in rows]),dtype=float)
        norms=np.linalg.norm(array,axis=1,keepdims=True)
        if (norms==0).any() or not np.isfinite(array).all(): raise ValueError('Invalid embedding output')
        return array/norms
    ref=vectors(references);query=vectors(queries)
    distances=np.maximum(0,1-query@ref.T)
    return [[{'email_id':references[int(index)]['id'],'group_id':references[int(index)]['group_id'],
               'label':references[int(index)]['human_label'],'distance':float(row[index])} for index in np.argsort(row)[:15]] for row in distances]


def run(args):
    import torch,transformers,sklearn
    root=Path(args.output);report=Path(args.report_dir)
    if root.exists() and any(root.iterdir()): raise ValueError('Output already contains a run. Choose a new output directory; existing checkpoints are preserved')
    manifest=json.loads((Path(args.dataset)/'provenance.json').read_text(encoding='utf-8'))
    raw=json.loads((Path(args.dataset)/'emails.json').read_text(encoding='utf-8'))
    rows=prepare_rows(raw,manifest);splits=group_splits(rows,args.seed)
    feedback=personalization_rows(splits['train'],args.seed);ids={row['id'] for row in feedback}
    base_rows=[row for row in splits['train'] if row['id'] not in ids]
    fingerprint=digest({'rows':rows,'manifest':manifest})
    frozen=split_manifest(splits,fingerprint,args.seed)
    frozen['base_train_ids']=[row['id'] for row in base_rows];frozen['personalization_ids']=sorted(ids)
    frozen['deduplicated_count']=len(raw)-len(rows)
    write(report/'split_manifest.json',frozen)
    print('Training an isolated small DistilBERT base on synthetic training groups.',flush=True)
    start=time.perf_counter();base,tokenizer,base_history=train(base_rows,splits['validation'],root/'base',seed=args.seed,epochs=args.base_epochs)
    print('Training the personalized candidate on separate training-only feedback groups.',flush=True)
    personal,personal_tokenizer,personal_history=train(feedback,splits['validation'],root/'personal',seed=args.seed,epochs=args.personal_epochs,base_path=root/'base')
    training_seconds=time.perf_counter()-start
    print('Selecting retrieval rules using validation only and Chroma default embeddings.',flush=True)
    from chromadb.utils.embedding_functions import DefaultEmbeddingFunction
    encoder=DefaultEmbeddingFunction()
    validation_neighbors=neighbors(splits['validation'],feedback,encoder)
    candidates=[RetrievalPolicy(max_distance=distance,min_support=support,min_vote_share=share,min_margin=margin,neighbor_count=15,
        version='synthetic-validation-v1',evidence='synthetic_validation') for distance in [.25,.35,.45,.55,.65]
        for support in [3,4] for share in [.75,.85,.95] for margin in [.15,.25]]
    (policy,validation_metrics),search=select_policy(splits['validation'],validation_neighbors,candidates)
    policy=replace(policy,version='synthetic-retrieval-'+digest(policy.to_dict())[:12])
    policy_artifact={'embedding_id':EMBEDDING_ID,'selection_split':'validation','dataset_hash':fingerprint,
        'policy':policy.to_dict(),'validation_metrics':validation_metrics,'limitations':['Synthetic benchmark only; real inbox calibration still required.']}
    write(report/'retrieval_policy.json',policy_artifact)
    write(report/'validation_search.json',[{'policy':candidate.to_dict(),'metrics':metrics} for candidate,metrics in search])
    print('Rules are frozen. Evaluating the untouched test groups now.',flush=True)
    test=splits['test'];truth=[row['human_label'] for row in test]
    base_guesses,base_scores,base_ms=infer(base,tokenizer,test)
    personal_guesses,personal_scores,personal_ms=infer(personal,personal_tokenizer,test)
    test_neighbors=neighbors(test,feedback,encoder)
    retrieved=[vote(items,policy) for items in test_neighbors]
    retrieval_guesses=[value.category if value else None for value in retrieved]
    hybrid=[retrieved_value or fallback for retrieved_value,fallback in zip(retrieval_guesses,personal_guesses)]
    systems={'base_local':base_guesses,'personalized_local':personal_guesses,'retrieval_only':retrieval_guesses,'hybrid':hybrid}
    results={name:classification_metrics(truth,guesses) for name,guesses in systems.items()}
    comparisons={'personalization':compare_predictions(test,base_guesses,personal_guesses),
                 'hybrid_vs_personalized':compare_predictions(test,personal_guesses,hybrid)}
    cloud={'status':'not_evaluated','reason':'No live provider or credentials used. Supply an explicitly captured result file to compare the same test IDs.'}
    if args.cloud_predictions:
        cloud_input=json.loads(Path(args.cloud_predictions).read_text(encoding='utf-8'))
        if cloud_input.get('dataset_hash')!=fingerprint or set(cloud_input.get('predictions',{}))!={row['id'] for row in test}:
            raise ValueError('Cloud results must match dataset hash and every held-out test ID exactly')
        systems['cloud']=[cloud_input['predictions'][row['id']] for row in test]
        results['cloud']=classification_metrics(truth,systems['cloud']);cloud={'status':'evaluated_from_supplied_results','model_version':cloud_input.get('model_version')}
    # Abstention operating points are frozen beforehand, not chosen using test accuracy.
    coverage={str(threshold):classification_metrics(truth,[guess if score>=threshold else None for guess,score in zip(personal_guesses,personal_scores)]) for threshold in [.5,.7,.9]}
    metrics={'dataset_id':manifest['dataset_id'],'dataset_hash':fingerprint,'scope':'synthetic_benchmark_only',
        'class_counts':{name:dict(Counter(row['human_label'] for row in subset)) for name,subset in splits.items()},
        'systems':results,'comparisons':comparisons,'cloud':cloud,'softmax_coverage_points':coverage,
        'timing':{'training_seconds':training_seconds,'base_batched_ms_per_email':base_ms,'personal_batched_ms_per_email':personal_ms,
                  'note':'CPU batched test timing, not warmed production API latency.'},
        'environment':{'python':platform.python_version(),'platform':platform.platform(),'processor':platform.processor(),
                       'torch':torch.__version__,'transformers':transformers.__version__,'sklearn':sklearn.__version__,
                       'cpu_threads':torch.get_num_threads(),'cuda_used':False},
        'hyperparameters':{'seed':args.seed,'base_epochs':args.base_epochs,'personal_epochs':args.personal_epochs,'architecture':'2-layer 96-dimensional DistilBERT initialized from scratch; tokenizer learned on base train only'},
        'checkpoint_versions':{'base':base.config.mailmind_model_version,'personal':personal.config.mailmind_model_version}}
    metrics['promotion_assessment']={'promoted':False,'scope':'synthetic_benchmark_only','urgent_personalization_regressions':len(comparisons['personalization']['urgent_regression_ids']),'reason':'No real-inbox validation; any urgent regression blocks a personalization quality claim. Runtime defaults are unchanged.'}
    write(report/'metrics.json',metrics)
    write(report/'test_predictions.json',[{'id':row['id'],'truth':row['human_label'],**{name:guesses[index] for name,guesses in systems.items()},
        'base_softmax':base_scores[index],'personal_softmax':personal_scores[index]} for index,row in enumerate(test)])
    write(root/'experiment.json',{'dataset_hash':fingerprint,'report_dir':str(report),'scope':'synthetic_benchmark_only'})
    print(json.dumps({'systems':{name:{'macro_f1':value['macro_f1'],'coverage':value['coverage'],'urgent_false_negatives':value['urgent_false_negatives']} for name,value in results.items()},'comparisons':comparisons},indent=2),flush=True)
    return metrics

if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--dataset',default='fixtures/priority_benchmark')
    parser.add_argument('--output',default='models/phase7-synthetic-v2')
    parser.add_argument('--report-dir',default='docs/evaluation/phase7')
    parser.add_argument('--seed',type=int,default=42)
    parser.add_argument('--base-epochs',type=int,default=20)
    parser.add_argument('--personal-epochs',type=int,default=5)
    parser.add_argument('--cloud-predictions',default=None)
    args=parser.parse_args();run(args)
