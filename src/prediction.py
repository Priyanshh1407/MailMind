"""Shared labels and honest prediction outcomes. Scores are not measured accuracy."""
from dataclasses import asdict, dataclass
from enum import Enum
import math

class Category(str, Enum):
    IMPORTANT = 'IMPORTANT'
    UPDATES = 'UPDATES'
    SPAM = 'SPAM'

LABEL2ID = {Category.SPAM.value: 0, Category.IMPORTANT.value: 1, Category.UPDATES.value: 2}
ID2LABEL = {value: key for key, value in LABEL2ID.items()}

def checkpoint_labels(config):
    if config.num_labels != 3:
        raise ValueError('Legacy binary checkpoints cannot classify three categories')
    mapping = {int(key): value for key, value in config.id2label.items()}
    if mapping != ID2LABEL or config.label2id != LABEL2ID:
        raise ValueError('Checkpoint category mapping does not match MailMind')
    return mapping

@dataclass(frozen=True)
class Prediction:
    category: str | None = None
    outcome: str = 'UNAVAILABLE'
    source: str = 'local'
    model_version: str | None = None
    score: float | None = None
    score_kind: str | None = None
    vote_share: float | None = None
    support: int = 0
    elapsed_ms: float = 0
    retrieval_status: str = 'not_used'
    reason: str | None = None
    limitations: tuple = ()

    def __post_init__(self):
        if self.outcome not in ('CLASSIFIED', 'ABSTAIN', 'UNAVAILABLE', 'ERROR'):
            raise ValueError('Invalid prediction outcome')
        if (self.outcome == 'CLASSIFIED') != (self.category in LABEL2ID):
            raise ValueError('Only classified outcomes may have a canonical category')
        if self.outcome != 'CLASSIFIED' and self.category is not None:
            raise ValueError('Failed predictions cannot have a category')
        for value in (self.score, self.vote_share):
            if value is not None and (not math.isfinite(value) or not 0 <= value <= 1):
                raise ValueError('Invalid prediction score')
        if self.support < 0 or not math.isfinite(self.elapsed_ms) or self.elapsed_ms < 0:
            raise ValueError('Invalid prediction evidence')

    def to_dict(self):
        return asdict(self)
