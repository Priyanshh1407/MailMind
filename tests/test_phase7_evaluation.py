"""Offline dataset, split, evaluation and policy regressions; no real weights."""
import copy
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock,patch
from scripts.build_priority_benchmark import build
from src.benchmark_data import prepare_rows,group_splits,validate_splits,personalization_rows,digest
from src.evaluation import classification_metrics,compare_predictions,select_policy
from src.retrieval_policy import RetrievalPolicy,vote,load_policy,EMBEDDING_ID
from src.priority_training import validate_training_rows,load_training_frame
from src.prediction import LABEL2ID,ID2LABEL,checkpoint_labels
from src.config import Settings
from src import vector_db

class DatasetTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(prefix='mailmind-phase7-')
        self.addCleanup(self.temp.cleanup)
        self.rows,self.manifest=build(self.temp.name)

    def test_benchmark_has_priority_semantics_and_original_provenance(self):
        rows=prepare_rows(self.rows,self.manifest)
        self.assertEqual(len(rows),180)
        self.assertEqual({row['human_label'] for row in rows},set(LABEL2ID))
        self.assertTrue(self.manifest['synthetic'])
        self.assertEqual(self.manifest['license'],'CC0-1.0')
        self.assertTrue(any('No response' in row['body'] for row in rows if row['human_label']=='UPDATES'))

    def test_empty_and_one_class_datasets_are_rejected(self):
        for rows in [[],[row for row in self.rows if row['human_label']=='SPAM']]:
            with self.assertRaises(ValueError): prepare_rows(rows,self.manifest)

    def test_proxy_and_unknown_license_are_rejected(self):
        for change in [{'task':'spam_ham_proxy'},{'license':'unknown'},{'source':''},{'label_semantics':{'ham':'urgent'}}]:
            with self.assertRaises(ValueError): prepare_rows(self.rows,{**self.manifest,**change})

    def test_invalid_labels_text_and_duplicate_ids_are_rejected(self):
        for change in [{'human_label':'IGNORE'},{'body':42},{'subject':'','body':''},{'group_id':''},{'id':self.rows[1]['id']}]:
            rows=copy.deepcopy(self.rows);rows[0].update(change)
            with self.assertRaises(ValueError):prepare_rows(rows,self.manifest)

    def test_duplicate_text_is_removed_without_losing_classes(self):
        duplicate={**self.rows[0],'id':'duplicate'}
        self.assertEqual(len(prepare_rows(self.rows+[duplicate],self.manifest)),180)

    def test_duplicate_text_with_conflicting_labels_is_rejected(self):
        with self.assertRaises(ValueError): prepare_rows(self.rows+[{**self.rows[0],'id':'duplicate','human_label':'SPAM'}],self.manifest)

    def test_duplicates_cannot_connect_independent_groups_silently(self):
        with self.assertRaises(ValueError):prepare_rows(self.rows+[{**self.rows[0],'id':'duplicate','group_id':'new'}],self.manifest)

    def test_declared_thread_and_template_cannot_cross_groups(self):
        for field in ['thread_id','template_id']:
            rows=copy.deepcopy(self.rows);rows[3][field]=rows[0][field]
            with self.assertRaises(ValueError):prepare_rows(rows,self.manifest)

    def test_preparation_and_duplicate_choice_are_order_independent(self):
        rows=self.rows+[{**self.rows[0],'id':'aaa-duplicate'}]
        self.assertEqual(prepare_rows(rows,self.manifest),prepare_rows(list(reversed(rows)),self.manifest))

    def test_splits_are_reproducible_and_every_class_is_present(self):
        rows=prepare_rows(self.rows,self.manifest)
        splits=group_splits(rows)
        self.assertEqual(splits,group_splits(rows))
        self.assertEqual({name:len(rows) for name,rows in splits.items()},{'train':108,'validation':36,'test':36})
        for entries in splits.values(): self.assertEqual({row['human_label'] for row in entries},set(LABEL2ID))

    def test_different_seeds_change_group_assignment(self):
        rows=prepare_rows(self.rows,self.manifest)
        self.assertNotEqual(group_splits(rows,42),group_splits(rows,43))

    def test_group_text_and_id_leakage_are_rejected(self):
        splits=group_splits(prepare_rows(self.rows,self.manifest))
        for change in [{'id':splits['train'][0]['id']},{'group_id':splits['train'][0]['group_id']},
                       {'subject':splits['train'][0]['subject'],'body':splits['train'][0]['body']}]:
            modified=copy.deepcopy(splits);modified['test'][0].update(change)
            with self.assertRaises(ValueError):validate_splits(modified)

    def test_too_few_independent_groups_is_rejected(self):
        rows=prepare_rows(self.rows,self.manifest)
        for row in rows:row['group_id']=row['human_label']
        with self.assertRaises(ValueError):group_splits(rows)

    def test_personalization_uses_training_only_and_all_classes(self):
        splits=group_splits(prepare_rows(self.rows,self.manifest));personal=personalization_rows(splits['train'])
        self.assertEqual(len(personal),27)
        self.assertEqual({row['human_label'] for row in personal},set(LABEL2ID))
        self.assertTrue({row['id'] for row in personal}<={row['id'] for row in splits['train']})
        self.assertFalse({row['group_id'] for row in personal}&{row['group_id'] for row in splits['test']})

    def test_training_rejects_one_class_or_insufficient_examples(self):
        for rows in [[],self.rows[:3],[self.rows[0],self.rows[60],self.rows[120]]]:
            with self.assertRaises(ValueError):validate_training_rows(rows)

    def test_compatibility_preparation_uses_verified_train_rows(self):
        frame=load_training_frame(self.temp.name)
        self.assertEqual(set(frame.human_label),set(LABEL2ID))
        self.assertEqual(len(frame),108)
        self.assertEqual(len(load_training_frame(self.temp.name,'personal')),27)

    def test_shared_config_mapping_cannot_drop_updates(self):
        self.assertEqual(checkpoint_labels(Mock(num_labels=3,id2label=ID2LABEL,label2id=LABEL2ID)),ID2LABEL)
        with self.assertRaises(ValueError):checkpoint_labels(Mock(num_labels=2,id2label={0:'SPAM',1:'IMPORTANT'}))

class OutputSafetyTests(unittest.TestCase):
    def test_training_refuses_to_overwrite_an_existing_checkpoint(self):
        from src.priority_training import train
        with tempfile.TemporaryDirectory() as directory:
            sentinel=Path(directory)/'config.json'
            sentinel.write_text('preserve',encoding='utf-8')
            with self.assertRaisesRegex(ValueError,'never overwritten'):
                train([],[],directory)
            self.assertEqual(sentinel.read_text(encoding='utf-8'),'preserve')


class EmbeddingCompatibilityTests(unittest.TestCase):
    def test_tuned_policy_refuses_an_unverified_embedding_collection(self):
        collection=Mock();collection.configuration={'hnsw':{'space':'cosine'},'embedding_function':object()}
        service=vector_db.VectorService(lambda:collection,policy=RetrievalPolicy(evidence='synthetic_validation'))
        with self.assertRaisesRegex(ValueError,'benchmark cosine/default embedding'):
            service.get_knn_prediction('Synthetic','Body',account_id='synthetic@example.test')
        collection.query.assert_not_called()


class MetricsTests(unittest.TestCase):
    def test_per_class_confusion_and_urgent_misses_include_abstention(self):
        metrics=classification_metrics(['SPAM','IMPORTANT','IMPORTANT','UPDATES'],['SPAM','SPAM',None,'UPDATES'])
        self.assertEqual(metrics['coverage'],.75)
        self.assertEqual(metrics['accuracy_including_abstention'],.5)
        self.assertEqual(metrics['urgent_false_negatives'],2)
        self.assertEqual(metrics['urgent_abstentions'],1)
        self.assertEqual(metrics['per_class']['SPAM']['precision'],.5)
        self.assertEqual(metrics['per_class']['IMPORTANT']['recall'],0)
        self.assertEqual(metrics['confusion_matrix']['columns'][-1],'ABSTAIN')

    def test_no_coverage_has_no_selective_accuracy(self):
        self.assertIsNone(classification_metrics(['IMPORTANT'],[None])['selective_accuracy'])

    def test_invalid_metrics_input_is_rejected(self):
        for truth,guesses in [([],[]),(['SPAM'],[]),(['IGNORE'],['SPAM']),(['SPAM'],['ERROR'])]:
            with self.assertRaises(ValueError):classification_metrics(truth,guesses)

    def test_agreement_is_not_accuracy_and_regressions_are_tracked(self):
        rows=[{'id':'urgent','human_label':'IMPORTANT'},{'id':'update','human_label':'UPDATES'}]
        result=compare_predictions(rows,['IMPORTANT','SPAM'],['SPAM','UPDATES'])
        self.assertEqual(result['agreement'],0)
        self.assertEqual(result['regression_ids'],['urgent'])
        self.assertEqual(result['urgent_regression_ids'],['urgent'])
        self.assertEqual(result['improvement_ids'],['update'])
        wrong=compare_predictions(rows,['SPAM','SPAM'],['SPAM','SPAM'])
        self.assertEqual(wrong['agreement'],1)
        self.assertEqual(classification_metrics(['IMPORTANT','UPDATES'],['SPAM','SPAM'])['accuracy_including_abstention'],0)

    def test_policy_selection_rejects_test_rows(self):
        row={'id':'one','human_label':'IMPORTANT','split':'test'}
        with self.assertRaises(ValueError):select_policy([row],[[]],[RetrievalPolicy()])

    def test_policy_selection_uses_validation_results(self):
        rows=[{'id':'one','human_label':'IMPORTANT','split':'validation'}]
        items=[{'email_id':str(i),'label':'IMPORTANT','distance':.3} for i in range(3)]
        strict=RetrievalPolicy();broader=RetrievalPolicy(max_distance=.35)
        (selected,metrics),_=select_policy(rows,[items],[strict,broader])
        self.assertEqual(selected,broader);self.assertEqual(metrics['urgent_false_negatives'],0)

class PolicyTests(unittest.TestCase):
    def test_invalid_policy_values_and_low_support_are_rejected(self):
        for values in [{'max_distance':float('nan')},{'min_support':1},{'min_vote_share':.2},{'neighbor_count':51},{'max_distance':'0.2'},{'min_margin':True}]:
            with self.assertRaises(ValueError):RetrievalPolicy(**values)

    def test_correlated_template_variants_do_not_inflate_support(self):
        items=[{'email_id':str(i),'group_id':'one-template','label':'IMPORTANT','distance':.1} for i in range(5)]
        self.assertIsNone(vote(items))

    def test_nearest_independent_group_vote_is_used(self):
        items=[{'email_id':str(i),'group_id':str(i),'label':'IMPORTANT','distance':.1} for i in range(3)]
        items.append({'email_id':'duplicate','group_id':'0','label':'SPAM','distance':.2})
        self.assertEqual(vote(items).category,'IMPORTANT')

    def test_malformed_neighbors_are_ignored_safely(self):
        items=[{'email_id':str(i),'label':'SPAM','distance':.1} for i in range(3)]
        items.extend([{'distance':None},{'distance':True},{'distance':float('nan')},{'distance':'0.1'},None])
        self.assertEqual(vote(items).category,'SPAM')

    def test_runtime_and_benchmark_use_the_same_vote(self):
        items=[{'email_id':str(i),'label':'UPDATES','distance':.1} for i in range(3)]
        with patch.object(vector_db,'search_similar_emails',return_value=items):
            self.assertEqual(vector_db.get_knn_prediction('s','b'),vote(items))

    def test_policy_artifact_requires_embedding_and_validation_provenance(self):
        with tempfile.TemporaryDirectory() as root:
            path=Path(root)/'policy.json'
            valid={'embedding_id':EMBEDDING_ID,'selection_split':'validation','dataset_hash':'synthetic-hash','validation_metrics':{'sample_count':3},'policy':RetrievalPolicy(evidence='synthetic_validation').to_dict()}
            path.write_text(json.dumps(valid),encoding='utf-8')
            self.assertEqual(load_policy(path).evidence,'synthetic_validation')
            for change in [{'embedding_id':'another-encoder'},{'selection_split':'test'},{'dataset_hash':''}]:
                path.write_text(json.dumps({**valid,**change}),encoding='utf-8')
                with self.assertRaises(ValueError):load_policy(path)

    def test_defaults_remain_conservative_until_explicit_opt_in(self):
        self.assertEqual(load_policy().max_distance,.25)
        self.assertIsNone(Settings().retrieval_policy_path)

    def test_environment_can_select_policy_without_reading_environment_files(self):
        with patch.dict('os.environ',{'MAILMIND_RETRIEVAL_POLICY_PATH':'synthetic-policy.json'}):
            self.assertEqual(Settings.from_environment().retrieval_policy_path.name,'synthetic-policy.json')
