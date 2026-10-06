"""Account-scoped repository and deterministic explanation policies."""
from contextlib import nullcontext
import json
import re

from .config import CATEGORIES
from .database import connection, utc_timestamp
from .email_text import normalize_text
from .intelligence_contract import (
    AnalysisSource,
    ExplanationSignal,
    MAX_EXPLANATION_SIGNALS,
    MAX_EXPLANATION_SUMMARY_CHARS,
    MAX_SIGNAL_EVIDENCE_CHARS,
)
from .prediction import (
    ANALYSIS_VERSION,
    AnalysisSignal,
    EmailAnalysis,
    Prediction,
)


_VERSION = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,79}")

SYSTEM_REASON_MESSAGES = {
    'queued': 'Waiting for classification.',
    'running': 'Classification is currently in progress.',
    'retry': 'Classification will retry after a temporary failure.',
    'dead': 'Classification stopped: the AI rejected this email or could not give a valid answer.',
    'low_confidence': 'The model was not confident enough to choose a category.',
    'category_tie': 'The two most likely categories were too close to choose safely.',
    'provider_timeout': 'The classification provider took too long to respond.',
    'provider_connect_timeout': 'MailMind could not connect to the classification provider in time.',
    'provider_connection': 'The connection to the classification provider failed.',
    'provider_failed': 'The classification provider could not complete the request.',
    'provider_transient': 'The classification provider had a temporary failure.',
    'provider_quota': 'The classification provider temporarily refused the request.',
    'provider_auth': 'The configured classification credentials were rejected.',
    'provider_invalid_request': 'The provider rejected the classification request.',
    'provider_budget_exhausted': 'The classification request exceeded its time budget.',
    'invalid_provider_output': 'The provider returned a result MailMind could not safely use.',
    'cloud_not_configured_or_unavailable': 'No configured cloud classifier was available.',
    'fallback_not_configured': 'No configured fallback classifier was available.',
    'cloud_disabled_local_only': 'Cloud classification is disabled in local-only mode.',
    'missing_checkpoint': 'The local model checkpoint is unavailable.',
    'model_loading': 'The local model is still loading.',
    'model_process_failed': 'The local model process could not start.',
    'local_inference_failed': 'The local model could not complete this prediction.',
    'local_inference_timeout': 'The local model took too long to complete this prediction.',
    'local_inference_budget_exhausted': 'The local prediction exceeded its time budget.',
    'shadow_inference_failed': 'The local shadow model could not complete its prediction.',
    'legacy_binary_has_no_updates_coverage': 'The installed local model cannot classify all three categories.',
    'message_parse_failed': 'MailMind could not safely read enough of this message.',
    'classification_unavailable': 'No reliable classification was available.',
}

_LOCAL_SUMMARIES = {
    'IMPORTANT': 'The local classifier identified this message as requiring attention.',
    'UPDATES': 'The local classifier identified this message as a useful non-urgent update.',
    'SPAM': 'The local classifier identified this message as unwanted or promotional.',
}


def _database(db_conn, db_path):
    return nullcontext(db_conn) if db_conn is not None else connection(db_path)


def _required_text(value, name, limit):
    text = normalize_text(value, limit=limit + 1)
    if not text or len(text) > limit:
        raise ValueError(f"Invalid {name}")
    return text


def normalize_signals(signals):
    if not isinstance(signals, (list, tuple)) or len(signals) > MAX_EXPLANATION_SIGNALS:
        raise ValueError("Explanation signals must be a list of at most three items")
    allowed = {item.value for item in ExplanationSignal}
    result, seen = [], set()
    for item in signals:
        if isinstance(item, AnalysisSignal):
            signal, evidence = item.signal, item.evidence
        elif isinstance(item, str):
            signal, evidence = item, None
        elif isinstance(item, dict) and set(item).issubset({"signal", "evidence"}):
            signal, evidence = item.get("signal"), item.get("evidence")
        else:
            raise ValueError("Invalid explanation signal")
        if signal not in allowed or signal in seen:
            raise ValueError("Unknown or duplicate explanation signal")
        seen.add(signal)
        row = {"signal": signal}
        if evidence is not None:
            row["evidence"] = _required_text(
                evidence, "Signal evidence", MAX_SIGNAL_EVIDENCE_CHARS
            )
        result.append(row)
    return result


def system_email_analysis(reason):
    code = reason if reason in SYSTEM_REASON_MESSAGES else 'classification_unavailable'
    return EmailAnalysis(
        predicted_category=None,
        explanation_summary=SYSTEM_REASON_MESSAGES[code],
        signals=(),
        source=AnalysisSource.SYSTEM.value,
    )


def local_email_analysis(prediction):
    if prediction.outcome != 'CLASSIFIED':
        return system_email_analysis(
            prediction.reason or 'classification_unavailable'
        )
    signals = [AnalysisSignal(ExplanationSignal.LOCAL_MODEL_SIGNAL.value)]
    if prediction.retrieval_status == 'available' and prediction.support:
        signals.append(AnalysisSignal(ExplanationSignal.FEEDBACK_PRECEDENT.value))
    return EmailAnalysis(
        predicted_category=prediction.category,
        explanation_summary=_LOCAL_SUMMARIES[prediction.category],
        signals=tuple(signals),
        source=AnalysisSource.LOCAL_HEURISTIC.value,
        model_version=prediction.model_version or 'local-unversioned',
        retrieval_used=bool(
            prediction.retrieval_status == 'available' and prediction.support
        ),
    )


def provider_fallback_analysis(prediction):
    source = (
        prediction.source
        if prediction.source in (
            AnalysisSource.GEMINI.value,
            AnalysisSource.GROQ.value,
        )
        else AnalysisSource.LOCAL_HEURISTIC.value
    )
    if source == AnalysisSource.LOCAL_HEURISTIC.value:
        return local_email_analysis(prediction)
    label = prediction.category.title()
    return EmailAnalysis(
        predicted_category=prediction.category,
        explanation_summary=(
            f'{label} was returned by {source.title()}; '
            'no additional validated explanation was available.'
        ),
        signals=(),
        source=source,
        model_version=prediction.model_version or f'{source}-unversioned',
        retrieval_used=bool(
            prediction.retrieval_status == 'available' and prediction.support
        ),
    )


def unrecorded_email_analysis(prediction):
    """For saved mail that has a category but no stored explanation at all.

    Such mail was classified before explanations were recorded. Say so, rather
    than implying that a provider's explanation was rejected.
    """
    if prediction.source not in (AnalysisSource.GEMINI.value, AnalysisSource.GROQ.value):
        return local_email_analysis(prediction)
    return EmailAnalysis(
        predicted_category=prediction.category,
        explanation_summary=(
            'No explanation was recorded for this email: it was classified before '
            'explanations were enabled. Use "Analyze up to 20 saved emails" to add one.'
        ),
        signals=(),
        source=prediction.source,
        model_version=prediction.model_version or f'{prediction.source}-unversioned',
    )


def ensure_prediction_analysis(prediction):
    if not isinstance(prediction, Prediction):
        raise ValueError('A Prediction is required')
    if prediction.analysis is not None:
        return prediction
    analysis = (
        provider_fallback_analysis(prediction)
        if prediction.outcome == 'CLASSIFIED'
        else system_email_analysis(
            prediction.reason or 'classification_unavailable'
        )
    )
    return prediction.with_analysis(analysis)


def save_email_analysis(account_id, email_id, *, analysis_version,
                        predicted_category, explanation_summary, signals,
                        source, model_version=None, retrieval_used=False,
                        db_path=None, db_conn=None):
    if not isinstance(account_id, str) or not account_id:
        raise ValueError("account_id is required")
    if not isinstance(email_id, str) or not email_id:
        raise ValueError("email_id is required")
    if not isinstance(analysis_version, str) or not _VERSION.fullmatch(analysis_version):
        raise ValueError("Invalid analysis version")
    source = AnalysisSource(source).value
    if predicted_category is None:
        if source != AnalysisSource.SYSTEM.value:
            raise ValueError("Only system explanations may omit a predicted category")
    elif predicted_category not in CATEGORIES:
        raise ValueError("Invalid predicted category")
    summary = _required_text(
        explanation_summary, "Explanation summary", MAX_EXPLANATION_SUMMARY_CHARS
    )
    normalized_signals = normalize_signals(signals)
    if type(retrieval_used) is not bool:
        raise ValueError("retrieval_used must be a boolean")
    if model_version is not None:
        model_version = _required_text(model_version, "Model version", 160)
    stamp = utc_timestamp()
    with _database(db_conn, db_path) as conn:
        if not conn.execute(
            "SELECT 1 FROM email_logs WHERE account_id=? AND email_id=?",
            (account_id, email_id),
        ).fetchone():
            raise LookupError("Email does not belong to this account")
        conn.execute("""INSERT INTO email_analysis(
            account_id,email_id,analysis_version,predicted_category,
            explanation_summary,signals_json,source,model_version,retrieval_used,
            created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(account_id,email_id) DO UPDATE SET
                analysis_version=excluded.analysis_version,
                predicted_category=excluded.predicted_category,
                explanation_summary=excluded.explanation_summary,
                signals_json=excluded.signals_json,
                source=excluded.source,
                model_version=excluded.model_version,
                retrieval_used=excluded.retrieval_used,
                updated_at=excluded.updated_at""", (
                    account_id, email_id, analysis_version, predicted_category,
                    summary, json.dumps(normalized_signals, separators=(",", ":")),
                    source, model_version, int(retrieval_used), stamp, stamp,
                ))
        return get_email_analysis(account_id, email_id, db_conn=conn)


def save_analysis_result(account_id, email_id, analysis, *,
                         db_path=None, db_conn=None):
    if not isinstance(analysis, EmailAnalysis):
        raise ValueError('Invalid email analysis')
    return save_email_analysis(
        account_id,
        email_id,
        analysis_version=analysis.analysis_version or ANALYSIS_VERSION,
        predicted_category=analysis.predicted_category,
        explanation_summary=analysis.explanation_summary,
        signals=analysis.signals,
        source=analysis.source,
        model_version=analysis.model_version,
        retrieval_used=analysis.retrieval_used,
        db_path=db_path,
        db_conn=db_conn,
    )


def get_email_analysis(account_id, email_id, *, db_path=None, db_conn=None):
    with _database(db_conn, db_path) as conn:
        row = conn.execute(
            "SELECT * FROM email_analysis WHERE account_id=? AND email_id=?",
            (account_id, email_id),
        ).fetchone()
        if row is None:
            return None
        result = dict(row)
        result["signals"] = json.loads(result.pop("signals_json"))
        result["retrieval_used"] = bool(result["retrieval_used"])
        return result


def decision_context(item, gemini_models):
    """The whole story of one email's category, from data saved with the
    decision (no extra model call): who decided, whether a fallback answered,
    the user's past corrections given as examples, the local model's second
    opinion, the user's own label and the time taken."""
    latest = item.get('latest_prediction') if isinstance(item, dict) else None
    if not isinstance(latest, dict) or latest.get('category') not in CATEGORIES:
        return None
    source, version = latest.get('source'), latest.get('model_version')
    models = tuple(gemini_models or ())
    if source == 'gemini':
        route = ('primary' if models and version == models[0]
                 else 'fallback' if version in models else 'gemini')
    elif source in ('groq', 'local'):
        route = source
    else:
        route = 'unknown'
    support = latest.get('support')
    used = support if type(support) is int and support > 0 else 0
    status = latest.get('retrieval_status')
    lookup = ('used' if used else 'unavailable' if status == 'unavailable'
              else 'not_applicable' if status == 'rejected' or route == 'local'
              else 'none_close_enough')
    local = latest.get('local')
    second = None
    if route != 'local' and isinstance(local, dict) and local.get('category') in CATEGORIES:
        second = {'category': local['category'], 'agrees': local['category'] == latest['category']}
    elapsed = latest.get('elapsed_ms')
    human = item.get('human_label') if item.get('human_label') in CATEGORIES else None
    return {
        'decided_by': 'you' if human else 'model',
        'category': latest['category'],
        'route': route,
        'provider': source,
        'model_version': version,
        'precedents': {'used': used, 'lookup': lookup},
        'second_opinion': second,
        'elapsed_ms': float(elapsed) if isinstance(elapsed, (int, float)) and elapsed >= 0 else None,
        'your_label': human,
    }
