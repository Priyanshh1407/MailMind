"""Verified local assets; offline inference never downloads missing files."""
import hashlib
import json
from pathlib import Path

EMBEDDING_FILES=('config.json','model.onnx','special_tokens_map.json','tokenizer_config.json','tokenizer.json','vocab.txt')
CLASSIFIER_FILES=('config.json','tokenizer.json','tokenizer_config.json','model.safetensors')


def file_hash(path):
    with Path(path).open('rb') as stream:
        digest=hashlib.sha256()
        for chunk in iter(lambda:stream.read(1024*1024),b''):digest.update(chunk)
        return digest.hexdigest()


def verify_role(manifest_path,role,expected_root=None):
    if manifest_path is None: raise ValueError('Offline assets need an explicit manifest')
    manifest_path=Path(manifest_path)
    if manifest_path.stat().st_size>65536:raise ValueError('Asset manifest too large')
    payload=json.loads(manifest_path.read_text(encoding='utf-8'))
    if payload.get('schema')!=1 or payload.get('embedding_id')!='chroma-default-all-MiniLM-L6-v2':raise ValueError('Unsupported offline asset manifest')
    entry=payload['assets'][role];root=Path(entry['root']).resolve()
    if expected_root is not None and root!=Path(expected_root).resolve():raise ValueError('Checkpoint does not match asset manifest')
    required=CLASSIFIER_FILES if role=='classifier' else EMBEDDING_FILES
    if not set(required)<=set(entry['files']):raise ValueError('Required assets missing from manifest')
    for relative,expected in entry['files'].items():
        path=Path(relative)
        if path.is_absolute() or '..' in path.parts:raise ValueError('Unsafe asset path')
        target=(root/path).resolve()
        if not target.is_relative_to(root) or not target.is_file() or file_hash(target)!=expected:raise ValueError('Asset missing or checksum mismatch')
    return root


def offline_embedding(manifest_path):
    root=verify_role(manifest_path,'embedding')
    from chromadb.api.types import DefaultEmbeddingFunction
    from chromadb.utils.embedding_functions.onnx_mini_lm_l6_v2 import ONNXMiniLM_L6_V2
    class NoDownloadONNX(ONNXMiniLM_L6_V2):
        def _download_model_if_not_exists(self):
            if not all((root/name).is_file() for name in EMBEDDING_FILES):raise ValueError('Cached embedding asset missing; downloads disabled')
        def _download(self,*args,**kwargs):raise ValueError('Downloads disabled')
    class CachedDefault(DefaultEmbeddingFunction):
        def __init__(self):
            self.encoder=NoDownloadONNX(preferred_providers=['CPUExecutionProvider'])
            self.encoder.DOWNLOAD_PATH=str(root.parent);self.encoder.EXTRACTED_FOLDER_NAME=root.name
        def __call__(self,input):return self.encoder(input)
    return CachedDefault()
