#!/usr/bin/env python3
"""Tiny deterministic Elisa-LSP fixture for native shell integration tests."""

import json
import os
import sys
from pathlib import Path


def read_message():
    length = None
    while True:
        line = sys.stdin.buffer.readline()
        if not line:
            return None
        if line == b"\r\n":
            break
        name, _, value = line.partition(b":")
        if name.lower() == b"content-length":
            length = int(value.strip())
    if length is None:
        return None
    return json.loads(sys.stdin.buffer.read(length).decode("utf-8"))


def send(message):
    body = json.dumps(message, separators=(",", ":")).encode("utf-8")
    sys.stdout.buffer.write(b"Content-Length: " + str(len(body)).encode() + b"\r\n\r\n" + body)
    sys.stdout.buffer.flush()


while True:
    message = read_message()
    if message is None:
        break
    method = message.get("method")
    if method == "initialize":
        send({
            "jsonrpc": "2.0",
            "id": message["id"],
            "result": {
                "capabilities": {
                    "positionEncoding": "utf-16",
                    "textDocumentSync": 1,
                    "definitionProvider": True,
                    "workspaceSymbolProvider": True,
                },
                "serverInfo": {"name": "fixture-elisa-lsp", "version": "test"},
            },
        })
    elif method in ("textDocument/didOpen", "textDocument/didChange"):
        document = message["params"]["textDocument"]
        version = document["version"]
        related_path = os.environ.get("ELISA_LSP_RELATED_PATH")
        if related_path:
            related_source = Path(related_path).expanduser()
            if not related_source.is_absolute():
                related_source = Path(__file__).resolve().parents[2] / related_source
            related_uri = related_source.resolve().as_uri()
        else:
            related_uri = document["uri"]
        related_information = []
        for related_index in range(6):
            related_information.append({
                "location": {
                    "uri": related_uri if related_index == 0 else document["uri"],
                    "range": {
                        "start": {"line": related_index * 3, "character": related_index * 2 + 2},
                        "end": {"line": related_index * 3, "character": related_index * 2 + 4},
                    },
                },
                "message": (
                    "fixture related location" if related_index == 0 else
                    "fixture second related location" if related_index == 1 else
                    "fixture related location " + str(related_index + 1)
                ),
            })
        send({
            "jsonrpc": "2.0",
            "method": "textDocument/publishDiagnostics",
            "params": {
                "uri": document["uri"],
                "version": version,
                "diagnostics": [{
                    "message": "fixture diagnostic v" + str(version),
                    "severity": 1,
                    "source": "fixture-elisa-lsp",
                    "code": "LSP-FIXTURE",
                    "range": {
                        "start": {"line": 0, "character": 7},
                        "end": {"line": 3, "character": 4},
                    },
                    "relatedInformation": related_information,
                }],
            },
        })
    elif method == "textDocument/definition":
        document_uri = message["params"]["textDocument"]["uri"]
        definition_path = os.environ.get("ELISA_LSP_DEFINITION_PATH")
        if definition_path:
            definition_file = Path(definition_path).expanduser()
            if not definition_file.is_absolute():
                definition_file = Path(__file__).resolve().parents[2] / definition_file
            definition_uri = definition_file.resolve().as_uri()
        else:
            definition_uri = document_uri
        send({
            "jsonrpc": "2.0",
            "id": message["id"],
            "result": {
                "uri": definition_uri,
                "range": {
                    "start": {"line": 1, "character": 8},
                    "end": {"line": 1, "character": 8},
                },
            },
        })
    elif method == "workspace/symbol":
        symbol_path = os.environ.get("ELISA_LSP_SYMBOL_PATH")
        if symbol_path:
            symbol_file = Path(symbol_path).expanduser()
            if not symbol_file.is_absolute():
                symbol_file = Path(__file__).resolve().parents[2] / symbol_file
            symbol_uri = symbol_file.resolve().as_uri()
        else:
            symbol_uri = message["params"].get("uri", "")
        send({
            "jsonrpc": "2.0",
            "id": message["id"],
            "result": [{
                "name": "related_entry",
                "kind": 12,
                "containerName": "related_fixture",
                "location": {
                    "uri": symbol_uri,
                    "range": {
                        "start": {"line": 1, "character": 8},
                        "end": {"line": 1, "character": 21},
                    },
                },
            }] if message["params"].get("query") == "related" else [],
        })
    elif method == "shutdown":
        send({"jsonrpc": "2.0", "id": message["id"], "result": None})
    elif method == "exit":
        break
