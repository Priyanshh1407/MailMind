"""Provenance, duplicate checks and group-safe priority splits; no private I/O."""
import hashlib
import json
import re
from collections import Counter
from .email_text import format_email_text
from .prediction import LABEL2ID


def digest(value):
    return hashlib.sha256(json.dumps(value,sort_keys=True,ensure_ascii=False,separators=(',',':')).encode()).hexdigest()


def validate_provenance(manifest):
    required=('dataset_id','source','license','task','label_semantics','limitations')
    if not isinstance(manifest,dict) or any(not manifest.get(key) for key in required):
        raise ValueError('Dataset provenance must identify source, license, task, semantics and limitations')
    if manifest['task'] != 'three_category_priority' or set(manifest['label_semantics']) != set(LABEL2ID):
        raise ValueError('Spam/ham proxy labels cannot establish three-category priority')
    if str(manifest['license']).lower() in ('unknown','unverified','unspecified'):
        raise ValueError('Verify the dataset license before training')


def text_key(row):
    return re.sub(r'\s+',' ',format_email_text(row['subject'],row['body']).casefold()).strip()


def prepare_rows(rows,manifest):
    validate_provenance(manifest)
    if not rows: raise ValueError('Dataset is empty')
    output, seen, ids, declared_groups = [], {}, set(), {}
    for row in rows:
        if not isinstance(row,dict) or any(not isinstance(row.get(key),str) for key in ('id','subject','body','human_label','group_id')):
            raise ValueError('Each row needs string id, subject, body, human_label and group_id')
        if not row['id'] or row['id'] in ids or not row['group_id']:
            raise ValueError('Row IDs must be unique and grouping must be explicit')
        ids.add(row['id'])
        for field in ('thread_id','template_id'):
            if row.get(field):
                identity=(field,row[field])
                if identity in declared_groups and declared_groups[identity]!=row['group_id']:
                    raise ValueError('A thread/template crosses declared groups; combine it before splitting')
                declared_groups[identity]=row['group_id']
        if row['human_label'] not in LABEL2ID or not (row['subject'].strip() or row['body'].strip()):
            raise ValueError('Use nonempty text and IMPORTANT, UPDATES or SPAM labels')
        key=text_key(row)
        if key in seen:
            if seen[key]['human_label'] != row['human_label']:
                raise ValueError('Duplicate text has conflicting labels; review the annotation')
            if seen[key]['group_id'] != row['group_id']:
                raise ValueError('Duplicate text connects different groups; fix grouping before splitting')
            if row['id'] < seen[key]['id']:
                output.remove(seen[key]);seen[key]=row;output.append(dict(row))
            continue
        seen[key]=row
        output.append(dict(row))
    if set(row['human_label'] for row in output) != set(LABEL2ID):
        raise ValueError('Priority training requires all three classes')
    return sorted(output,key=lambda row:row['id'])


def group_splits(rows,seed=42):
    # One component can contain multiple thread/template identifiers. Never split it.
    from sklearn.model_selection import StratifiedGroupKFold
    labels=[row['human_label'] for row in rows]
    groups=[row['group_id'] for row in rows]
    for label in LABEL2ID:
        if len({row['group_id'] for row in rows if row['human_label']==label}) < 5:
            raise ValueError('Each class needs at least five independent groups')
    folds=list(StratifiedGroupKFold(n_splits=5,shuffle=True,random_state=seed).split(rows,labels,groups))
    test=set(folds[0][1].tolist()); validation=set(folds[1][1].tolist())
    splits={'train':[row for i,row in enumerate(rows) if i not in test|validation],
            'validation':[row for i,row in enumerate(rows) if i in validation],
            'test':[row for i,row in enumerate(rows) if i in test]}
    splits={name:[{**row,'split':name} for row in entries] for name,entries in splits.items()}
    validate_splits(splits)
    return splits


def validate_splits(splits):
    if set(splits) != {'train','validation','test'}: raise ValueError('Reserve train, validation and test separately')
    seen_ids,seen_groups,seen_text=set(),set(),set()
    for name,rows in splits.items():
        if not rows or set(row['human_label'] for row in rows) != set(LABEL2ID):
            raise ValueError('Every split must contain all three classes')
        identities={row['id'] for row in rows};groups={row['group_id'] for row in rows};text={text_key(row) for row in rows}
        if seen_ids & identities or seen_groups & groups or seen_text & text:
            raise ValueError('Leakage: ID, thread/template group or text crosses split boundaries')
        seen_ids.update(identities);seen_groups.update(groups);seen_text.update(text)


def personalization_rows(train,seed=42):
    # Take complete groups from training only. Test/validation never become feedback.
    selected=[]
    for label in LABEL2ID:
        groups=sorted({row['group_id'] for row in train if row['human_label']==label},key=lambda group:digest([seed,group]))[:3]
        selected.extend(row for row in train if row['group_id'] in groups and row['human_label']==label)
    if len(selected)<6 or len({row['human_label'] for row in selected})<2:
        raise ValueError('Personalization requires nonempty, multiclass evidence')
    return sorted(selected,key=lambda row:row['id'])


def split_manifest(splits,source_hash,seed):
    return {'dataset_hash':source_hash,'seed':seed,'strategy':'stratified_group_5_fold_60_20_20',
            'splits':{name:{'ids':[row['id'] for row in rows],'groups':sorted({row['group_id'] for row in rows}),
                          'class_counts':dict(Counter(row['human_label'] for row in rows))} for name,rows in splits.items()}}
