"""Accuracy, abstention and regressions are separate from agreement."""
from .prediction import LABEL2ID

CATEGORIES=list(LABEL2ID)


def classification_metrics(truth,predicted):
    if len(truth)!=len(predicted) or not truth: raise ValueError('Need equal nonempty labels and predictions')
    if any(label not in LABEL2ID for label in truth) or any(label is not None and label not in LABEL2ID for label in predicted):
        raise ValueError('Unsupported evaluation category')
    columns=CATEGORIES+['ABSTAIN'];matrix=[[0 for _ in columns] for _ in CATEGORIES]
    correct,covered=0,0
    for actual,guess in zip(truth,predicted):
        matrix[CATEGORIES.index(actual)][columns.index(guess if guess is not None else 'ABSTAIN')]+=1
        covered+=guess is not None;correct+=actual==guess
    per_class={}
    for index,label in enumerate(CATEGORIES):
        tp=matrix[index][index];support=sum(matrix[index]);false_positive=sum(row[index] for i,row in enumerate(matrix) if i!=index)
        precision=tp/(tp+false_positive) if tp+false_positive else 0
        recall=tp/support if support else 0
        per_class[label]={'precision':precision,'recall':recall,'f1':2*precision*recall/(precision+recall) if precision+recall else 0,'support':support}
    urgent=CATEGORIES.index('IMPORTANT')
    return {'sample_count':len(truth),'coverage':covered/len(truth),'accuracy_including_abstention':correct/len(truth),
        'selective_accuracy':correct/covered if covered else None,'macro_f1':sum(row['f1'] for row in per_class.values())/3,
        'per_class':per_class,'confusion_matrix':{'rows':CATEGORIES,'columns':columns,'values':matrix},
        'urgent_false_negatives':sum(matrix[urgent])-matrix[urgent][urgent],
        'urgent_abstentions':matrix[urgent][-1]}


def compare_predictions(rows,before,after):
    if len(rows)!=len(before) or len(rows)!=len(after): raise ValueError('Comparison lengths differ')
    regressions=[];improvements=[]
    for row,old,new in zip(rows,before,after):
        if old==row['human_label'] and new!=old: regressions.append(row['id'])
        if new==row['human_label'] and old!=new: improvements.append(row['id'])
    return {'agreement':sum(old==new for old,new in zip(before,after))/len(rows) if rows else None,
            'regression_ids':regressions,'improvement_ids':improvements,
            'urgent_regression_ids':[row['id'] for row in rows if row['id'] in regressions and row['human_label']=='IMPORTANT']}


def select_policy(validation_rows,neighbor_sets,candidates):
    from .retrieval_policy import vote
    if not validation_rows or len(validation_rows)!=len(neighbor_sets) or any(row.get('split')!='validation' for row in validation_rows): raise ValueError('Only validation evidence may select thresholds')
    truth=[row['human_label'] for row in validation_rows]
    evaluated=[]
    for policy in candidates:
        guesses=[(result.category if (result:=vote(neighbors,policy)) else None) for neighbors in neighbor_sets]
        metrics=classification_metrics(truth,guesses)
        evaluated.append((policy,metrics))
    # Validation-only objective: urgent misses penalized, then overall F1 and coverage.
    selected=max(evaluated,key=lambda item:(-item[1]['urgent_false_negatives'],item[1]['macro_f1'],item[1]['coverage'],
                                          -item[0].max_distance,item[0].min_vote_share))
    return selected,evaluated
