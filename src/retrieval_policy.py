"""Validated retrieval rules. Vote share is agreement, never measured accuracy."""
from dataclasses import asdict, dataclass
import json
import math
from pathlib import Path
from .prediction import Prediction, LABEL2ID

EMBEDDING_ID='chroma-default-all-MiniLM-L6-v2'

@dataclass(frozen=True)
class RetrievalPolicy:
    max_distance: float = .25
    min_support: int = 3
    min_vote_share: float = .75
    min_margin: float = .15
    neighbor_count: int = 5
    version: str = 'weighted-cosine-v1'
    evidence: str = 'provisional'

    def __post_init__(self):
        for name in ('max_distance','min_vote_share','min_margin'):
            value=getattr(self,name)
            if type(value) not in (int,float) or not math.isfinite(value) or not 0<=value<=1:
                raise ValueError('Invalid retrieval policy numeric limit')
        if type(self.min_support) is not int or type(self.neighbor_count) is not int or not 3<=self.min_support<=self.neighbor_count<=50:
            raise ValueError('Retrieval needs at least three independent messages/groups')
        if self.min_vote_share < .5 or not isinstance(self.version,str) or not 1<=len(self.version)<=80:
            raise ValueError('Invalid retrieval vote or version')
        if self.evidence not in ('provisional','synthetic_validation'):
            raise ValueError('Invalid retrieval evidence scope')

    def to_dict(self): return asdict(self)


def load_policy(path=None):
    if path is None: return RetrievalPolicy()
    path=Path(path)
    if path.stat().st_size>32768: raise ValueError('Policy artifact is too large')
    payload=json.loads(path.read_text(encoding='utf-8'))
    if payload.get('embedding_id') != EMBEDDING_ID or payload.get('selection_split') != 'validation':
        raise ValueError('Policy must match the runtime embedding and validation-only selection')
    if not payload.get('dataset_hash') or not payload.get('validation_metrics'):
        raise ValueError('Policy needs measured provenance')
    return RetrievalPolicy(**payload['policy'])


def eligible(distance,policy):
    return type(distance) in (int,float) and math.isfinite(distance) and 0<=distance<=policy.max_distance


def vote(neighbors,policy=None):
    policy=policy or RetrievalPolicy()
    votes,counts,seen={}, {}, set()
    valid=[item for item in neighbors if isinstance(item,dict) and eligible(item.get('distance'),policy)]
    for item in sorted(valid,key=lambda row:row['distance']):
        identity=item.get('group_id') or item.get('email_id')
        if not identity or identity in seen or item.get('label') not in LABEL2ID or not eligible(item.get('distance'),policy): continue
        seen.add(identity);label=item['label'];weight=1-item['distance']
        votes[label]=votes.get(label,0)+weight;counts[label]=counts.get(label,0)+1
    if len(seen)<policy.min_support or not votes or sum(votes.values())<=0: return None
    best=max(votes,key=lambda label:(votes[label],label));total=sum(votes.values());share=votes[best]/total
    runner=max((weight for label,weight in votes.items() if label!=best),default=0)/total
    if counts[best]<2 or share<policy.min_vote_share or share-runner<policy.min_margin: return None
    return Prediction(category=best,outcome='CLASSIFIED',source='retrieval',model_version=policy.version,
        score=share,score_kind='vote_share',vote_share=share,support=len(seen),retrieval_status='available',
        limitations=('synthetic_benchmark_only',) if policy.evidence=='synthetic_validation' else ('retrieval_thresholds_provisional',))
