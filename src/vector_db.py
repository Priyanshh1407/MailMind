from threading import Lock
import hashlib
import math
from .prediction import Prediction, Category
from .retrieval_policy import RetrievalPolicy, eligible, vote
from .config import Settings, LEGACY_ACCOUNT
from .email_text import format_email_text, normalize_subject

EMBEDDED_CHROMA_API = 'chromadb.api.rust.RustBindingsAPI'

# Collection access is lazy and synchronized; there is no import-time client.
_collection = None
_lock = Lock()

# Chroma's default local embedding may download assets on first query. Imports
# and collection construction do not deliberately issue embedding requests;
# local-only mode instead requires a verified, no-download cache.
def create_vector_collection(path=None, *, settings=None):
    """Explicit runtime factory; importing this module creates no client/files."""
    config=settings or Settings.from_environment()
    embedding=None
    if config.local_only:
        from .offline_assets import offline_embedding
        embedding=offline_embedding(config.asset_manifest_path)
    import chromadb
    from chromadb.config import Settings as ChromaSettings
    client = chromadb.PersistentClient(
        path=str(path or Settings.from_environment().data_dir / "chroma_db"),
        settings=ChromaSettings(chroma_api_impl=EMBEDDED_CHROMA_API, allow_reset=False, anonymized_telemetry=False),
    )
    server = getattr(client, '_server', None)
    if type(server).__module__ != 'chromadb.api.rust' or type(server).__name__ != 'RustBindingsAPI':
        raise RuntimeError('MailMind requires Chroma embedded Rust storage; HTTP/server clients are forbidden')
    collection = client.get_or_create_collection(name="mailmind_emails", metadata={"hnsw:space": "cosine"}, **({"embedding_function":embedding} if embedding is not None else {}))
    configuration = collection.configuration
    if configuration.get('hnsw', {}).get('space') != 'cosine':
        raise ValueError('The feedback collection must use cosine distance; rebuild its derived index')
    return collection


def get_collection():
    global _collection
    with _lock:
        if _collection is None:
            _collection = create_vector_collection()
        return _collection

def add_email_to_vector_db(email_id, subject, body, label, *, account_id=LEGACY_ACCOUNT, collection=None, revision_id=None):
    """
    Adds an email and its user-defined label to the Vector DB.
    """
    label = Category(label).value
    text_content = format_email_text(subject, body)
    
    # Repeating a revision safely replaces the same account/message entry.
    target = collection if collection is not None else get_collection()
    vector_id = hashlib.sha256((account_id + '\0' + str(email_id)).encode()).hexdigest()
    target.upsert(
        documents=[text_content],
        metadatas=[{"label": label, "subject": normalize_subject(subject), "account_id": account_id, "email_id": str(email_id), **({'revision_id': revision_id} if revision_id is not None else {})}],
        ids=[vector_id]
    )

def search_similar_emails(subject, body, k=3, *, account_id=None, collection=None, validator=None, policy=None):
    """
    Searches the Vector DB for the k most similar emails.
    Returns a list of dicts with their text and label.
    """
    if not isinstance(k, int) or isinstance(k, bool) or not 1 <= k <= 50:
        raise ValueError('Result limit must be between 1 and 50')
    if not account_id or account_id == LEGACY_ACCOUNT:
        return []
    text_content = format_email_text(subject, body)
    
    target = collection if collection is not None else get_collection()
    results = target.query(
        query_texts=[text_content],
        n_results=k,
        where={"account_id": account_id},
    )
    
    similar_emails = []
    seen = set()
    documents = results.get('documents') if isinstance(results, dict) else None
    metadatas = results.get('metadatas') if isinstance(results, dict) else None
    distances = results.get('distances') if isinstance(results, dict) else None
    rows = zip(documents[0], metadatas[0], distances[0]) if (isinstance(documents,list) and documents and isinstance(documents[0],list) and
        isinstance(metadatas,list) and metadatas and isinstance(metadatas[0],list) and
        isinstance(distances,list) and distances and isinstance(distances[0],list)) else ()
    for doc_text, raw_metadata, distance in rows:
            metadata = raw_metadata if isinstance(raw_metadata, dict) else {}
            identity = metadata.get('email_id')
            if not isinstance(doc_text, str) or not isinstance(identity, str) or not identity or identity in seen:
                continue
            if metadata.get('account_id') != account_id:
                continue
            label = metadata.get('label')
            if label not in tuple(item.value for item in Category) or not eligible(distance,policy or RetrievalPolicy()):
                continue
            if validator is not None and not validator(metadata):
                continue
            seen.add(identity)
            
            similar_emails.append({
                "text": doc_text,
                "label": label,
                "distance": distance,
                "email_id": metadata.get('email_id'), "group_id": metadata.get('group_id'), "revision_id": metadata.get('revision_id')
            })
            
    return similar_emails

def relevant_distance(distance):
    # Conservative default; Phase 7 synthetic thresholds require explicit opt-in.
    return isinstance(distance, (int, float)) and not isinstance(distance, bool) and math.isfinite(distance) and 0 <= distance <= 0.25


def get_knn_prediction(subject, body, k=5, *, account_id=None, collection=None, validator=None, policy=None):
    policy=policy or RetrievalPolicy()
    similar=search_similar_emails(subject,body,k=k,account_id=account_id,collection=collection,validator=validator,policy=policy)
    return vote(similar,policy)


class VectorService:
    """Injected collection provider, shared by feedback and model inference."""
    def __init__(self, collection_provider, validator=None, policy=None):
        self.collection_provider = collection_provider
        self.validator = validator
        self.policy = policy or RetrievalPolicy()

    def get_knn_prediction(self, subject, body, k=5, *, account_id=None):
        collection = self.collection_provider()
        if self.policy.evidence != 'provisional':
            from chromadb.api.types import DefaultEmbeddingFunction
            configuration = getattr(collection, 'configuration', {})
            if not isinstance(configuration, dict) or configuration.get('hnsw', {}).get('space') != 'cosine' or not isinstance(configuration.get('embedding_function'), DefaultEmbeddingFunction):
                raise ValueError('Evaluated policy requires the benchmark cosine/default embedding collection')
        return get_knn_prediction(subject, body, self.policy.neighbor_count if self.policy.evidence != "provisional" else k, account_id=account_id, collection=collection, validator=self.validator, policy=self.policy)
