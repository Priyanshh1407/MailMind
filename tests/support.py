"""Synthetic fixtures and an AST bridge for remaining legacy script checks.

Execute selected real function definitions, not copied implementations. Runtime
imports, OAuth, model loading, dotenv, and persistent Chroma clients are excluded.
Most service/ingestion checks now use normal imports. The selected legacy
classifier/redaction and training checks also use this function-level bridge.
"""

import ast
import base64
import copy
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def load_function(relative_path, name, dependencies):
    path = ROOT / relative_path
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    node = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == name)
    namespace = dict(dependencies)
    exec(compile(ast.Module(body=[node], type_ignores=[]), str(path), "exec"), namespace)
    return namespace[name]


def fixtures():
    return json.loads((ROOT / "tests/fixtures/emails.json").read_text(encoding="utf-8"))


def gmail_message(case):
    case = copy.deepcopy(case)
    body = case.get("body", "")
    if case["kind"] == "oversized":
        body = case["prefix"] * case["repeat"] + case["tail"]
    data = base64.urlsafe_b64encode(body.encode(case.get("charset", "utf-8"))).decode("ascii")
    leaf = {
        "mimeType": "text/html" if case["kind"] == "html_only" else "text/plain",
        "headers": [{"name": "Content-Type", "value": f'text/plain; charset={case.get("charset", "utf-8")}'}],
        "body": {"data": "a" if case["kind"] == "malformed" else data},
    }
    if case["kind"] == "nested_mime":
        payload = {"mimeType": "multipart/mixed", "parts": [{"mimeType": "multipart/alternative", "parts": [leaf]}]}
    elif case["kind"] == "html_only":
        leaf["headers"][0]["value"] = "text/html; charset=utf-8"
        payload = {"mimeType": "multipart/alternative", "parts": [leaf]}
    else:
        payload = leaf
    payload.setdefault("headers", []).extend([
        {"name": "From", "value": case["sender"]},
        {"name": "Subject", "value": case["subject"]},
    ])
    return {"id": case["id"], "payload": payload}


class FakeRequest:
    def __init__(self, result):
        self.result = result

    def execute(self):
        return copy.deepcopy(self.result)


class FakeGmail:
    """Only the read methods used by ingestion; never connects to a provider."""

    def __init__(self, cases):
        self.messages_by_id = {case["id"]: gmail_message(case) for case in cases}

    def users(self):
        return self

    def messages(self):
        return self

    def list(self, **kwargs):
        ids = list(self.messages_by_id)[:kwargs["maxResults"]]
        return FakeRequest({"messages": [{"id": item} for item in ids]})

    def get(self, **kwargs):
        return FakeRequest(self.messages_by_id[kwargs["id"]])
