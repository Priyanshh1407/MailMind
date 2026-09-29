'''Durable local semantic indexing and deterministic hybrid search ranking.'''
from hashlib import sha256
import math
from threading import Event
from uuid import uuid4

from .database import utc_timestamp
from .email_text import format_email_text, normalize_subject
from .logging_utils import log_event
from .vector_lock import vector_write_lock
from .account_state import WorkCancelled, AccessDenied
from .provider_policy import SEARCH_EMBEDDING_CALLS, SEARCH_COLLECTION_CALLS
from .vector_db import embed_search_documents
from .intelligence_contract import TokenOperation, TokenOutcome
from .token_usage import (
    EMBEDDING_MODEL_VERSION,
    TokenRecorder,
    estimate_text_tokens,
    usage_request_prefix,
)

MIN_SEMANTIC_QUERY = 3
MAX_SEMANTIC_RESULTS = 100
MAX_SEMANTIC_DISTANCE = 0.70
# Local ONNX model construction is noticeably slower than a normal provider
# request, especially while the shadow classifier is also starting. Keep the
# work bounded, but allow a cold local model enough time to become reusable.
SEARCH_QUERY_TIMEOUT_SECONDS = 10
SEARCH_INDEX_TIMEOUT_SECONDS = 30
MAX_SEARCH_DOCUMENT_CHARS = 6000


def vector_id(account_id, email_id):
    return sha256(('search\0' + account_id + '\0' + str(email_id)).encode()).hexdigest()


def _search_document(sender, subject, body):
    searchable_body=('From: ' + (sender or '') + '\n\n' + (body or '')).strip()
    # The source email remains stored in full. The derived semantic document is
    # bounded because ONNX tokenization of a 32k-character message can take
    # minutes and must never delay local indexing indefinitely.
    return format_email_text(subject, searchable_body)[:MAX_SEARCH_DOCUMENT_CHARS].rstrip()


def add_email_to_search_index(email_id, sender, subject, body, *, account_id, collection, embedding=None):
    arguments = dict(
        documents=[_search_document(sender, subject, body)],
        metadatas=[{'account_id': account_id, 'email_id': str(email_id),
                    'sender': sender or '', 'subject': normalize_subject(subject)}],
        ids=[vector_id(account_id, email_id)],
    )
    if embedding is not None:
        arguments['embeddings'] = [embedding]
    collection.upsert(**arguments)


def semantic_candidates(query, account_id, collection, *, limit=MAX_SEMANTIC_RESULTS):
    text = (query or '').strip()
    if len(text) < MIN_SEMANTIC_QUERY:
        return []
    if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= MAX_SEMANTIC_RESULTS:
        raise ValueError('Semantic result limit must be between 1 and 100')
    result = collection.query(query_texts=[text], n_results=limit,
                              where={'account_id': account_id})
    metadatas = result.get('metadatas') if isinstance(result, dict) else None
    distances = result.get('distances') if isinstance(result, dict) else None
    rows = zip(metadatas[0], distances[0]) if (
        isinstance(metadatas, list) and metadatas and isinstance(metadatas[0], list)
        and isinstance(distances, list) and distances and isinstance(distances[0], list)) else ()
    output, seen = [], set()
    for raw_metadata, distance in rows:
        metadata = raw_metadata if isinstance(raw_metadata, dict) else {}
        identity = metadata.get('email_id')
        if metadata.get('account_id') != account_id or not isinstance(identity, str) or not identity or identity in seen:
            continue
        if not isinstance(distance, (int, float)) or isinstance(distance, bool) or not math.isfinite(distance):
            continue
        if distance < 0 or distance > MAX_SEMANTIC_DISTANCE:
            continue
        seen.add(identity)
        output.append((identity, float(distance)))
    return output


def hybrid_rank(lexical_ids, semantic_rows):
    '''Exact text leads; semantic similarity adds related wording.'''
    scores = {}
    for rank, identity in enumerate(lexical_ids, 1):
        scores[identity] = scores.get(identity, 0.0) + 2.0 / (20 + rank)
    for rank, (identity, distance) in enumerate(semantic_rows, 1):
        similarity = max(0.0, 1.0 - distance)
        scores[identity] = scores.get(identity, 0.0) + similarity / (20 + rank)
    lexical_order = {identity: rank for rank, identity in enumerate(lexical_ids)}
    semantic_order = {identity: rank for rank, (identity, _distance) in enumerate(semantic_rows)}
    return sorted(scores, key=lambda identity: (
        -scores[identity], lexical_order.get(identity, 10**9),
        semantic_order.get(identity, 10**9), identity))


def reconcile_search_index(manager, context, collection_provider, limit=10, *, max_attempts=3,
                           embedding_provider=None, bounded=True, require_connected=True):
    if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= 50:
        raise ValueError('Search indexing limit must be between 1 and 50')
    with manager.guard(context, connected=require_connected) as conn:
        rows = conn.execute('''SELECT i.*,e.sender,e.subject,e.body FROM email_search_index i
            JOIN email_logs e USING(account_id,email_id)
            WHERE i.account_id=? AND i.indexing_state!='indexed' AND i.attempt_count<?
            ORDER BY i.updated_at,i.email_id LIMIT ?''',
            (context.account_id,max_attempts,limit)).fetchall()
    if not rows:
        return {'indexed': 0, 'failed': 0}
    documents = [_search_document(row['sender'], row['subject'], row['body']) for row in rows]
    timeout = SEARCH_INDEX_TIMEOUT_SECONDS
    embed = embedding_provider or (lambda values: embed_search_documents(values, settings=manager.settings))
    usage_recorder = TokenRecorder(
        context.account_id,
        usage_request_prefix(
            'search-index', context.account_id,
            *[f"{row['email_id']}:{row['attempt_count']}" for row in rows],
            uuid4().hex,
        ),
        db_path=manager.settings.db_path,
        enabled=manager.settings.token_collection_enabled,
    )
    embedding_measurement = estimate_text_tokens(documents)
    embedding_attempted = Event()
    embedding_recorded = False

    def run_embedding():
        embedding_attempted.set()
        return embed(documents)

    # Model startup/download and Chroma startup happen before the cross-process
    # write lock. A timed-out bounded call may keep warming in the background,
    # but can no longer freeze Gmail or hold the vector store lock.
    try:
        if bounded:
            embeddings = SEARCH_EMBEDDING_CALLS.run(run_embedding, timeout)
        else:
            # The dedicated indexer process owns this potentially slow native
            # work. It must not delegate ONNX/Chroma startup back into the
            # Gmail worker process.
            embeddings = run_embedding()
        if embedding_attempted.is_set():
            usage_recorder.record(
                'documents', provider='embedding',
                model_version=EMBEDDING_MODEL_VERSION,
                operation=TokenOperation.DOCUMENT_EMBEDDING.value,
                outcome=TokenOutcome.SUCCESS.value,
                measurement=embedding_measurement,
            )
            embedding_recorded = True
        collection = (SEARCH_COLLECTION_CALLS.run(collection_provider, timeout)
                      if bounded else collection_provider())
    except (WorkCancelled, AccessDenied):
        if embedding_attempted.is_set() and not embedding_recorded:
            usage_recorder.record(
                'documents', provider='embedding',
                model_version=EMBEDDING_MODEL_VERSION,
                operation=TokenOperation.DOCUMENT_EMBEDDING.value,
                outcome=TokenOutcome.CANCELLED.value,
                measurement=embedding_measurement,
            )
        raise
    except TimeoutError as error:
        if embedding_attempted.is_set() and not embedding_recorded:
            usage_recorder.record(
                'documents', provider='embedding',
                model_version=EMBEDDING_MODEL_VERSION,
                operation=TokenOperation.DOCUMENT_EMBEDDING.value,
                outcome=TokenOutcome.TIMEOUT.value,
                measurement=embedding_measurement,
            )
        log_event('email_search_index_deferred', error=error)
        return {'indexed': 0, 'failed': 0}
    except Exception as error:
        if embedding_attempted.is_set() and not embedding_recorded:
            usage_recorder.record(
                'documents', provider='embedding',
                model_version=EMBEDDING_MODEL_VERSION,
                operation=TokenOperation.DOCUMENT_EMBEDDING.value,
                outcome=TokenOutcome.FAILED.value,
                measurement=embedding_measurement,
            )
        log_event('email_search_index_failed', error=error)
        with manager.guard(context, connected=require_connected) as conn:
            for row in rows:
                conn.execute('''UPDATE email_search_index SET indexing_state='failed',attempt_count=attempt_count+1,updated_at=?
                    WHERE account_id=? AND email_id=? AND indexing_state!='indexed' ''',
                    (utc_timestamp(), context.account_id, row['email_id']))
        return {'indexed': 0, 'failed': len(rows)}

    if len(embeddings) != len(rows):
        raise ValueError('Search embedding count did not match the selected rows')
    try:
        with vector_write_lock(manager.settings.data_dir):
            active = []
            with manager.guard(context, connected=require_connected) as conn:
                for row, document, embedding in zip(rows, documents, embeddings):
                    current = conn.execute('SELECT indexing_state FROM email_search_index WHERE account_id=? AND email_id=?',
                                           (context.account_id, row['email_id'])).fetchone()
                    if current and current['indexing_state'] != 'indexed':
                        active.append((row, document, embedding))
            if not active:
                return {'indexed': 0, 'failed': 0}
            collection.upsert(
                documents=[item[1] for item in active],
                metadatas=[{'account_id': context.account_id, 'email_id': str(item[0]['email_id']),
                            'sender': item[0]['sender'] or '', 'subject': normalize_subject(item[0]['subject'])}
                           for item in active],
                ids=[vector_id(context.account_id, item[0]['email_id']) for item in active],
                embeddings=[item[2] for item in active],
            )
    except (WorkCancelled, AccessDenied):
        raise
    except TimeoutError as error:
        log_event('email_search_index_deferred', error=error)
        return {'indexed': 0, 'failed': 0}
    except Exception as error:
        log_event('email_search_index_failed', error=error)
        with manager.guard(context, connected=require_connected) as conn:
            for row in rows:
                conn.execute('''UPDATE email_search_index SET indexing_state='failed',attempt_count=attempt_count+1,updated_at=?
                    WHERE account_id=? AND email_id=? AND indexing_state!='indexed' ''',
                    (utc_timestamp(), context.account_id, row['email_id']))
        return {'indexed': 0, 'failed': len(rows)}

    indexed = 0
    with manager.guard(context, connected=require_connected) as conn:
        for row, _document, _embedding in active:
            indexed += conn.execute('''UPDATE email_search_index SET indexing_state='indexed',attempt_count=attempt_count+1,updated_at=?
                WHERE account_id=? AND email_id=? AND indexing_state!='indexed' ''',
                (utc_timestamp(), context.account_id, row['email_id'])).rowcount
    return {'indexed': indexed, 'failed': 0}
