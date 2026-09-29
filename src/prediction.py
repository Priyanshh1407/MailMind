"""Shared labels, enriched analysis, and honest prediction outcomes."""
from dataclasses import asdict, dataclass, replace
from enum import Enum
import math

from .intelligence_contract import (
    AnalysisSource,
    ConfidenceBand,
    DuePrecision,
    ExplanationSignal,
    ActionType,
    MAX_ACTIONS_PER_EMAIL,
    MAX_ACTION_DESCRIPTION_CHARS,
    MAX_ACTION_EVIDENCE_CHARS,
    MAX_ACTION_TITLE_CHARS,
    MAX_EXPLANATION_SIGNALS,
    MAX_EXPLANATION_SUMMARY_CHARS,
    MAX_SIGNAL_EVIDENCE_CHARS,
)


ANALYSIS_VERSION = "email-analysis-v1"


class Category(str, Enum):
    IMPORTANT = 'IMPORTANT'
    UPDATES = 'UPDATES'
    SPAM = 'SPAM'


LABEL2ID = {
    Category.SPAM.value: 0,
    Category.IMPORTANT.value: 1,
    Category.UPDATES.value: 2,
}
ID2LABEL = {value: key for key, value in LABEL2ID.items()}


def checkpoint_labels(config):
    if config.num_labels != 3:
        raise ValueError('Legacy binary checkpoints cannot classify three categories')
    mapping = {int(key): value for key, value in config.id2label.items()}
    if mapping != ID2LABEL or config.label2id != LABEL2ID:
        raise ValueError('Checkpoint category mapping does not match MailMind')
    return mapping


@dataclass(frozen=True)
class AnalysisSignal:
    signal: str
    evidence: str | None = None

    def __post_init__(self):
        ExplanationSignal(self.signal)
        if self.evidence is not None and (
                not isinstance(self.evidence, str)
                or not self.evidence
                or len(self.evidence) > MAX_SIGNAL_EVIDENCE_CHARS):
            raise ValueError('Invalid explanation evidence')


@dataclass(frozen=True)
class ActionCandidate:
    action_type: str
    title: str
    description: str
    due_at: str | None
    due_precision: str
    evidence: str
    confidence: str

    def __post_init__(self):
        ActionType(self.action_type)
        DuePrecision(self.due_precision)
        ConfidenceBand(self.confidence)
        for value, limit in (
            (self.title, MAX_ACTION_TITLE_CHARS),
            (self.description, MAX_ACTION_DESCRIPTION_CHARS),
            (self.evidence, MAX_ACTION_EVIDENCE_CHARS),
        ):
            if not isinstance(value, str) or not value or len(value) > limit:
                raise ValueError('Invalid action candidate text')
        if self.due_at is not None and (
                not isinstance(self.due_at, str) or not self.due_at):
            raise ValueError('Invalid action candidate deadline')


@dataclass(frozen=True)
class EmailAnalysis:
    predicted_category: str | None
    explanation_summary: str
    signals: tuple[AnalysisSignal, ...]
    source: str
    model_version: str | None = None
    retrieval_used: bool = False
    actions: tuple[ActionCandidate, ...] = ()
    analysis_version: str = ANALYSIS_VERSION

    def __post_init__(self):
        source = AnalysisSource(self.source).value
        if self.predicted_category is None:
            if source != AnalysisSource.SYSTEM.value:
                raise ValueError('Only system analysis may omit a category')
        else:
            Category(self.predicted_category)
        if (not isinstance(self.explanation_summary, str)
                or not self.explanation_summary
                or len(self.explanation_summary) > MAX_EXPLANATION_SUMMARY_CHARS):
            raise ValueError('Invalid explanation summary')
        if (not isinstance(self.signals, tuple)
                or len(self.signals) > MAX_EXPLANATION_SIGNALS
                or len({item.signal for item in self.signals}) != len(self.signals)):
            raise ValueError('Invalid explanation signals')
        if not all(isinstance(item, AnalysisSignal) for item in self.signals):
            raise ValueError('Invalid explanation signals')
        if type(self.retrieval_used) is not bool:
            raise ValueError('retrieval_used must be a boolean')
        if self.model_version is not None and (
                not isinstance(self.model_version, str)
                or not self.model_version
                or len(self.model_version) > 160):
            raise ValueError('Invalid analysis model version')
        if (not isinstance(self.actions, tuple)
                or len(self.actions) > MAX_ACTIONS_PER_EMAIL
                or not all(isinstance(item, ActionCandidate) for item in self.actions)):
            raise ValueError('Invalid action candidates')
        if not isinstance(self.analysis_version, str) or not self.analysis_version:
            raise ValueError('Invalid analysis version')

    def to_dict(self, *, include_actions=False):
        result = {
            'analysis_version': self.analysis_version,
            'predicted_category': self.predicted_category,
            'explanation_summary': self.explanation_summary,
            'signals': [asdict(item) for item in self.signals],
            'source': self.source,
            'model_version': self.model_version,
            'retrieval_used': self.retrieval_used,
        }
        if include_actions:
            result['actions'] = [
                {
                    'type': item.action_type,
                    'title': item.title,
                    'description': item.description,
                    'due_at': item.due_at,
                    'due_precision': item.due_precision,
                    'evidence': item.evidence,
                    'confidence': item.confidence,
                }
                for item in self.actions
            ]
        return result

    @classmethod
    def from_dict(cls, value):
        if not isinstance(value, dict):
            raise ValueError('Invalid analysis object')
        signals = tuple(
            AnalysisSignal(item['signal'], item.get('evidence'))
            for item in value.get('signals', ())
            if isinstance(item, dict)
        )
        actions = tuple(
            ActionCandidate(
                action_type=item['type'],
                title=item['title'],
                description=item['description'],
                due_at=item.get('due_at'),
                due_precision=item['due_precision'],
                evidence=item['evidence'],
                confidence=item['confidence'],
            )
            for item in value.get('actions', ())
            if isinstance(item, dict)
        )
        return cls(
            analysis_version=value.get('analysis_version', ANALYSIS_VERSION),
            predicted_category=value.get('predicted_category'),
            explanation_summary=value.get('explanation_summary'),
            signals=signals,
            source=value.get('source'),
            model_version=value.get('model_version'),
            retrieval_used=value.get('retrieval_used', False),
            actions=actions,
        )


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
    analysis: EmailAnalysis | None = None

    def __post_init__(self):
        if self.outcome not in ('CLASSIFIED', 'ABSTAIN', 'UNAVAILABLE', 'ERROR'):
            raise ValueError('Invalid prediction outcome')
        if (self.outcome == 'CLASSIFIED') != (self.category in LABEL2ID):
            raise ValueError('Only classified outcomes may have a canonical category')
        if self.outcome != 'CLASSIFIED' and self.category is not None:
            raise ValueError('Failed predictions cannot have a category')
        for value in (self.score, self.vote_share):
            if value is not None and (
                    not math.isfinite(value) or not 0 <= value <= 1):
                raise ValueError('Invalid prediction score')
        if self.support < 0 or not math.isfinite(self.elapsed_ms) or self.elapsed_ms < 0:
            raise ValueError('Invalid prediction evidence')
        if self.analysis is not None:
            if not isinstance(self.analysis, EmailAnalysis):
                raise ValueError('Invalid prediction analysis')
            if self.analysis.predicted_category != self.category:
                raise ValueError('Analysis category does not match prediction')

    def with_analysis(self, analysis):
        return replace(self, analysis=analysis)

    def to_dict(self, *, include_analysis=True, include_actions=False):
        result = {
            'category': self.category,
            'outcome': self.outcome,
            'source': self.source,
            'model_version': self.model_version,
            'score': self.score,
            'score_kind': self.score_kind,
            'vote_share': self.vote_share,
            'support': self.support,
            'elapsed_ms': self.elapsed_ms,
            'retrieval_status': self.retrieval_status,
            'reason': self.reason,
            'limitations': self.limitations,
        }
        if include_analysis and self.analysis is not None:
            result['analysis'] = self.analysis.to_dict(include_actions=include_actions)
        return result
