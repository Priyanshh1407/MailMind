from dataclasses import replace
from time import perf_counter
import atexit
from contextlib import redirect_stdout
import json
import os
from pathlib import Path
from queue import Empty, Queue
import re
import subprocess
import sys
from threading import Lock, Thread
from . import vector_db
from .config import Settings
from .prediction import Prediction, checkpoint_labels
from .logging_utils import log_event
from .email_text import format_email_text, format_model_text, normalize_subject, normalize_text, MODEL_MAX_TOKENS


def _settings_payload(settings, model_path):
    return {
        'data_dir': str(settings.data_dir),
        'model_path': str(model_path or settings.model_path),
        'retrieval_policy_path': str(settings.retrieval_policy_path) if settings.retrieval_policy_path else None,
        'local_only': settings.local_only,
        'asset_manifest_path': str(settings.asset_manifest_path) if settings.asset_manifest_path else None,
        'busy_timeout_ms': settings.busy_timeout_ms,
        'poll_interval_seconds': settings.poll_interval_seconds,
        'batch_size': settings.batch_size,
        'gmail_page_size': settings.gmail_page_size,
        'gmail_max_pages': settings.gmail_max_pages,
        'auto_mark_read': settings.auto_mark_read,
        'provider_timeout_seconds': settings.provider_timeout_seconds,
        'classification_budget_seconds': settings.classification_budget_seconds,
        'worker_lease_seconds': settings.worker_lease_seconds,
        'max_processing_attempts': settings.max_processing_attempts,
    }


def _model_service(payload):
    """Run native ML imports behind a line-delimited local subprocess protocol."""
    protocol = sys.stdout
    raw = json.loads(payload)
    for name in ('data_dir', 'model_path', 'retrieval_policy_path', 'asset_manifest_path'):
        if raw.get(name) is not None:
            raw[name] = Path(raw[name])
    settings = Settings(**raw)
    try:
        from .feedback import current_vector
        from .retrieval_policy import load_policy
        cached = []
        vector_lock = Lock()
        def collection_provider():
            with vector_lock:
                if not cached:
                    cached.append(vector_db.create_vector_collection(settings.data_dir / 'chroma_db', settings=settings))
                return cached[0]
        service = vector_db.VectorService(
            collection_provider,
            lambda metadata: current_vector(metadata, settings.db_path),
            policy=load_policy(settings.retrieval_policy_path),
        )
        with redirect_stdout(sys.stderr):
            model = MailMindModel(settings.model_path, settings=settings, vector_service=service)
        state = {'type': 'status', 'model_loaded': bool(model.model_loaded),
                 'model_version': model.model_version, 'training_scope': model.training_scope,
                 'load_reason': model.load_reason}
    except BaseException as error:
        with redirect_stdout(sys.stderr):
            log_event('model_process_failed', error=error)
        state = {'type': 'status', 'model_loaded': False, 'model_version': None,
                 'training_scope': None, 'load_reason': 'model_process_failed'}
        model = None
    protocol.write(json.dumps(state, separators=(',', ':')) + '\n')
    protocol.flush()
    if model is None:
        return
    for line in sys.stdin:
        try:
            request = json.loads(line)
            if request.get('command') == 'stop':
                return
            with redirect_stdout(sys.stderr):
                prediction = model.predict(request['subject'], request['body'],
                                           sender=request.get('sender', ''),
                                           account_id=request.get('account_id'))
            response = {'type': 'prediction', 'id': request['id'],
                        'prediction': prediction.to_dict()}
        except BaseException as error:
            with redirect_stdout(sys.stderr):
                log_event('local_inference_process_failed', error=error)
            response = {'type': 'prediction', 'id': request.get('id') if isinstance(request, dict) else None,
                        'prediction': Prediction(outcome='ERROR', source='local',
                                                 reason='local_inference_failed').to_dict()}
        protocol.write(json.dumps(response, separators=(',', ':')) + '\n')
        protocol.flush()


class DeferredMailMindModel:
    """Load native ML code outside the serving process and expose honest readiness."""
    def __init__(self, model_path=None, *, settings=None, _test_factory=None, **kwargs):
        self._model = None
        self._lock = Lock()
        self._model_loaded = False
        self._model_version = None
        self._training_scope = None
        self._load_reason = 'model_loading'
        self._closed = False
        self._request_id = 0
        self._responses = Queue()
        if _test_factory is not None:
            self._thread = Thread(target=self._load_for_test,
                                  args=(_test_factory, (model_path,) if model_path is not None else (), kwargs),
                                  daemon=True, name='mailmind-test-model-loader')
            self._process = None
            self._thread.start()
            return
        config = settings or Settings.from_environment(load_file=True)
        payload = json.dumps(_settings_payload(config, model_path), separators=(',', ':'))
        flags = subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0
        self._process = subprocess.Popen(
            [sys.executable, '-m', 'src.local_llm', '--model-service', payload],
            cwd=Path(__file__).resolve().parents[1],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True, bufsize=1,
            creationflags=flags,
        )
        self._thread = Thread(target=self._watch_output, daemon=True, name='mailmind-model-status')
        self._thread.start()
        atexit.register(self.close)

    def _load_for_test(self, factory, args, kwargs):
        model = factory(*args, **kwargs)
        with self._lock:
            self._model = model

    def _watch_output(self):
        try:
            for line in self._process.stdout:
                message = json.loads(line)
                if message.get('type') == 'status':
                    with self._lock:
                        self._model_loaded = bool(message['model_loaded'])
                        self._model_version = message['model_version']
                        self._training_scope = message['training_scope']
                        self._load_reason = message['load_reason']
                elif message.get('type') == 'prediction':
                    self._responses.put(message)
        except (AttributeError, EOFError, OSError, ValueError, json.JSONDecodeError):
            pass
        finally:
            with self._lock:
                if self._load_reason == 'model_loading':
                    self._load_reason = 'model_process_failed'

    def _current(self):
        with self._lock:
            return self._model

    @property
    def model_loaded(self):
        model = self._current()
        if model is not None:
            return bool(model.model_loaded)
        with self._lock:
            return self._model_loaded

    @property
    def model_version(self):
        model = self._current()
        if model is not None:
            return model.model_version
        with self._lock:
            return self._model_version

    @property
    def training_scope(self):
        model = self._current()
        if model is not None:
            return model.training_scope
        with self._lock:
            return self._training_scope

    @property
    def load_reason(self):
        model = self._current()
        if model is not None:
            return model.load_reason
        with self._lock:
            if self._process is not None and self._process.poll() is not None and self._load_reason == 'model_loading':
                return 'model_process_failed'
            return self._load_reason

    def predict(self, subject, body, *, sender='', account_id=None):
        model = self._current()
        if model is not None:
            return model.predict(subject, body, sender=sender, account_id=account_id)
        if not self.model_loaded:
            return Prediction(outcome='UNAVAILABLE', source='local', reason=self.load_reason)
        with self._lock:
            if self._closed or self._process is None or self._process.poll() is not None:
                return Prediction(outcome='UNAVAILABLE', source='local', reason='model_process_failed')
            self._request_id += 1
            request_id = self._request_id
            try:
                self._process.stdin.write(json.dumps({
                    'id': request_id, 'subject': subject, 'body': body, 'sender': sender, 'account_id': account_id,
                }, separators=(',', ':')) + '\n')
                self._process.stdin.flush()
            except (BrokenPipeError, OSError, ValueError):
                return Prediction(outcome='UNAVAILABLE', source='local', reason='model_process_failed')
        try:
            while True:
                message = self._responses.get(timeout=15)
                if message.get('id') == request_id:
                    return Prediction(**message['prediction'])
        except Empty:
            return Prediction(outcome='ERROR', source='local', reason='local_inference_timeout')

    def close(self):
        with self._lock:
            if self._closed:
                return
            self._closed = True
            process = self._process
            if process is None:
                return
            if process.poll() is None:
                try:
                    process.stdin.write('{"command":"stop"}\n')
                    process.stdin.flush()
                    process.stdin.close()
                except (BrokenPipeError, OSError, ValueError):
                    pass
        try:
            process.wait(timeout=2)
        except subprocess.TimeoutExpired:
            process.terminate()
            try:
                process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()

class MailMindModel:
    def __init__(self, model_path=None, *, vector_service=None, settings=None):
        self.vector_service = vector_service or vector_db
        self.model_loaded = False
        self.model_version = None
        self.training_scope = None
        self.load_reason = 'missing_or_invalid_checkpoint'
        try:
            config=settings or Settings.from_environment()
            if config.local_only:
                os.environ['HF_HUB_OFFLINE']='1'
                os.environ['HF_HUB_DISABLE_TELEMETRY']='1'
                if vector_service is None:
                    from threading import Lock
                    vector_lock=Lock();cached=[]
                    def collection_provider():
                        with vector_lock:
                            if not cached:cached.append(vector_db.create_vector_collection(config.data_dir/'chroma_db',settings=config))
                            return cached[0]
                    self.vector_service=vector_db.VectorService(collection_provider)
                from .offline_assets import verify_role
                verify_role(config.asset_manifest_path,'classifier',model_path or config.model_path)
            from transformers import AutoTokenizer, AutoModelForSequenceClassification
            path = str(model_path or Settings.from_environment().model_path)
            self.tokenizer = AutoTokenizer.from_pretrained(path, local_files_only=True,token=False)
            self.model = AutoModelForSequenceClassification.from_pretrained(path, local_files_only=True,token=False)
            if self.model.config.num_labels == 2:
                self.model_version = 'legacy-binary'
                self.load_reason = 'legacy_binary_has_no_updates_coverage'
                return
            self.id2label = checkpoint_labels(self.model.config)
            scope=getattr(self.model.config,"mailmind_training_scope",None)
            self.training_scope=scope if scope in ('synthetic_benchmark_only','private_user_approved_inbox') else None
            self.model.eval()
            version = getattr(self.model.config, 'mailmind_model_version', None)
            self.model_version = version if isinstance(version, str) and re.fullmatch(r'[A-Za-z0-9._:-]{1,80}', version) else 'local-three-class-unversioned'
            self.model_loaded = True
            self.load_reason = 'ready'
        except Exception as error:
            log_event('model_load_failed', error=error)

    def predict(self, subject, body, *, sender='', account_id=None):
        start = perf_counter()
        sender = normalize_text(sender, limit=1000)
        subject, body = normalize_subject(subject), normalize_text(body)
        retrieval_status = 'rejected'
        # Evaluate the private shadow checkpoint directly so retrieval feedback
        # cannot mask its actual three-class model performance.
        if getattr(self, 'training_scope', None) != 'private_user_approved_inbox':
            try:
                result = self.vector_service.get_knn_prediction(subject, body, k=5, account_id=account_id)
                if result is not None:
                    return replace(result, elapsed_ms=(perf_counter()-start)*1000)
            except Exception as error:
                retrieval_status = 'unavailable'
                log_event('retrieval_failed', error=error)
        if not self.model_loaded:
            return Prediction(outcome='ABSTAIN' if self.model_version == 'legacy-binary' else 'UNAVAILABLE',
                              model_version=self.model_version, reason=self.load_reason,
                              retrieval_status=retrieval_status, elapsed_ms=(perf_counter()-start)*1000)
        try:
            import torch
            import torch.nn.functional as F
            model_text = format_model_text(sender, subject, body) if sender else format_email_text(subject, body)
            inputs = self.tokenizer(model_text, return_tensors='pt',
                                    truncation=True, max_length=MODEL_MAX_TOKENS)
            with torch.no_grad():
                probabilities = F.softmax(self.model(**inputs).logits, dim=-1)
            index = torch.argmax(probabilities, dim=-1).item()
            return Prediction(category=self.id2label[index], outcome='CLASSIFIED', model_version=self.model_version,
                              score=float(probabilities[0][index].item()), score_kind='softmax',
                              retrieval_status=retrieval_status, elapsed_ms=(perf_counter()-start)*1000,
                              limitations=('synthetic_benchmark_only','not_production_validated') if getattr(self,"training_scope",None) else ('checkpoint_quality_not_yet_evaluated',))
        except Exception as error:
            log_event('local_inference_failed', error=error)
            return Prediction(outcome='ERROR', model_version=self.model_version, reason='local_inference_failed',
                              retrieval_status=retrieval_status, elapsed_ms=(perf_counter()-start)*1000)


if __name__ == '__main__':
    if len(sys.argv) == 3 and sys.argv[1] == '--model-service':
        _model_service(sys.argv[2])
    else:
        raise SystemExit('This module is an internal MailMind model service.')
