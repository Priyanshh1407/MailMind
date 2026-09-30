"""Which model classifies an email, and how each local run is metered.

Routing rules live here so the API and the worker cannot drift apart:
- normal mode: the cloud decides; the local model is only a shadow;
- local-only mode: the local model decides;
- the /predict sandbox may borrow the cloud while the local model loads.
"""
from contextlib import nullcontext
from threading import Event

from .email_analysis import ensure_prediction_analysis
from .email_text import format_email_text
from .intelligence_contract import TokenOperation, TokenOutcome
from .llm_api import classify_email
from .local_llm import local_retrieval_expected, predict_with_token_count
from .prediction import Prediction
from .provider_policy import LOCAL_CALLS
from .token_usage import (
    EMBEDDING_MODEL_VERSION,
    estimate_text_tokens,
    failure_outcome,
    tokenizer_usage_measurement,
    unavailable_measurement,
)

SHADOW_TIMEOUT_SECONDS = 5


def local_outcome(prediction):
    if prediction.outcome in ('CLASSIFIED', 'ABSTAIN'):
        return TokenOutcome.SUCCESS.value
    if prediction.reason in ('local_inference_timeout', 'local_inference_budget_exhausted'):
        return TokenOutcome.TIMEOUT.value
    return TokenOutcome.FAILED.value


def run_local(predict, *, model, subject, body, timeout, recorder, key, operation,
              guard=nullcontext):
    """Run one local prediction on the bounded local pool and meter it once.

    `predict()` returns (Prediction, input_tokens). Failures are recorded and
    re-raised; callers decide whether a failure is fatal.
    """
    attempted = Event()
    expects_query = local_retrieval_expected(model)
    query_measurement = estimate_text_tokens(format_email_text(subject, body))

    def record(outcome, measurement, version):
        if recorder is not None:
            recorder.record(key, provider='local',
                            model_version=version or 'local-unavailable',
                            operation=operation, outcome=outcome,
                            measurement=measurement)

    def record_query(outcome):
        if recorder is not None and expects_query:
            recorder.record(f'{key}-retrieval', provider='embedding',
                            model_version=EMBEDDING_MODEL_VERSION,
                            operation=TokenOperation.QUERY_EMBEDDING.value,
                            outcome=outcome, measurement=query_measurement)

    def attempt():
        attempted.set()
        return predict()

    try:
        with guard():
            prediction, input_tokens = LOCAL_CALLS.run(attempt, timeout)
    except Exception as error:
        if attempted.is_set():
            outcome = failure_outcome(error)
            record(outcome, unavailable_measurement(), getattr(model, 'model_version', None))
            record_query(outcome)
        raise
    prediction = ensure_prediction_analysis(prediction)
    record(local_outcome(prediction),
           tokenizer_usage_measurement(input_tokens) if input_tokens is not None
           else unavailable_measurement(),
           prediction.model_version)
    record_query(TokenOutcome.FAILED.value if prediction.retrieval_status == 'unavailable'
                 else TokenOutcome.SUCCESS.value)
    return prediction


def _local_prediction(model, subject, body, account_id, settings, *, sender,
                      usage_recorder, usage_operation):
    return run_local(
        lambda: predict_with_token_count(model, subject, body, sender=sender,
                                         account_id=account_id),
        model=model, subject=subject, body=body,
        timeout=settings.classification_budget_seconds, recorder=usage_recorder,
        key='local-manual', operation=usage_operation)


def _cloud_prediction(classifier, subject, body, account_id, settings, collection_provider,
                      validator, *, sender, source_timestamp, usage_recorder, usage_operation):
    return ensure_prediction_analysis(classifier(
        sender, subject, body, account_id=account_id,
        source_timestamp=source_timestamp,
        collection_provider=collection_provider, validator=validator,
        settings=settings, usage_recorder=usage_recorder,
        usage_operation=usage_operation,
    ))


def manual_prediction(model, subject, body, account_id, settings,
                      collection_provider, validator, classifier=classify_email,
                      usage_recorder=None,
                      usage_operation=TokenOperation.MANUAL_PREDICTION.value,
                      sender='[MANUAL]', source_timestamp=None):
    """The /predict sandbox: borrow the cloud only while the local model loads."""
    if not settings.local_only and getattr(model, 'load_reason', None) == 'model_loading':
        return _cloud_prediction(
            classifier, subject, body, account_id, settings, collection_provider,
            validator, sender=sender, source_timestamp=source_timestamp,
            usage_recorder=usage_recorder, usage_operation=usage_operation)
    # The sandbox has no real sender; '[MANUAL]' is a cloud placeholder only.
    return _local_prediction(
        model, subject, body, account_id, settings, sender='',
        usage_recorder=usage_recorder, usage_operation=usage_operation)


def authoritative_prediction(model, subject, body, account_id, settings,
                             collection_provider, validator, classifier=classify_email,
                             usage_recorder=None,
                             usage_operation=TokenOperation.MANUAL_PREDICTION.value,
                             sender='[MANUAL]', source_timestamp=None):
    """Decide with the same route the worker trusts for saved mail.

    Normal mode: cloud only. The API's local model is a shadow evaluator and
    must never replace a saved decision, whether or not it has loaded.
    Local-only mode: the local model decides, with the same sender-aware
    input the worker uses and the checkpoint was trained on.
    """
    if settings.local_only:
        return _local_prediction(
            model, subject, body, account_id, settings, sender=sender,
            usage_recorder=usage_recorder, usage_operation=usage_operation)
    return _cloud_prediction(
        classifier, subject, body, account_id, settings, collection_provider,
        validator, sender=sender, source_timestamp=source_timestamp,
        usage_recorder=usage_recorder, usage_operation=usage_operation)


def shadow_prediction(model, subject, body, account_id, *, sender, usage_recorder,
                      timeout=SHADOW_TIMEOUT_SECONDS):
    """Evaluate the shadow model; it can never fail or change the decision."""
    try:
        return run_local(
            lambda: predict_with_token_count(model, subject, body, sender=sender,
                                             account_id=account_id),
            model=model, subject=subject, body=body, timeout=timeout,
            recorder=usage_recorder, key='local-shadow',
            operation=TokenOperation.LOCAL_SHADOW.value)
    except Exception as error:
        from .account_state import WorkCancelled
        if isinstance(error, WorkCancelled):
            raise
        return ensure_prediction_analysis(Prediction(
            outcome='ERROR', reason='shadow_inference_failed'))
