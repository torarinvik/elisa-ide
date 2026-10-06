#!/usr/bin/env python3
"""Process contract for the native IDE's bounded LSP sidecar protocol."""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import textwrap
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
HOST = ROOT / "worker" / "lsp" / "ide_lsp_host.py"
sys.path.insert(0, os.fspath(ROOT / "worker" / "lsp"))
from ide_lsp_host import _hex_decode  # noqa: E402

SERVER = textwrap.dedent(
    r'''
    import json, os, sys

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
        body = json.dumps(message, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        sys.stdout.buffer.write(b"Content-Length: " + str(len(body)).encode() + b"\r\n\r\n" + body)
        sys.stdout.buffer.flush()

    while True:
        message = read_message()
        if message is None:
            break
        method = message.get("method")
        if method == "initialize":
            send({"jsonrpc":"2.0", "id":message["id"], "result":{
                "capabilities":{"positionEncoding":"utf-16", "textDocumentSync":1,
                    "definitionProvider":True, "workspaceSymbolProvider":True},
                "serverInfo":{"name":"fixture-elisa-lsp", "version":"test"}
            }})
        elif method in ("textDocument/didOpen", "textDocument/didChange"):
            document = message["params"]["textDocument"]
            version = document["version"]
            send({"jsonrpc":"2.0", "method":"textDocument/publishDiagnostics", "params":{
                "uri":document["uri"], "version":version,
                "diagnostics":[{"message":"diagnostic version " + str(version),
                    "severity":1, "source":"Elisa-LSP", "code":"E42",
                    "range":{"start":{"line":0,"character":4},
                             "end":{"line":0,"character":9}},
                    "relatedInformation":[{"location":{"uri":document["uri"],
                        "range":{"start":{"line":0,"character":0},
                                 "end":{"line":0,"character":3}}},
                        "message":"related declaration"}]}]
            }})
        elif method == "textDocument/definition":
            uri = os.environ.get("ELISA_LSP_DEFINITION_URI", message["params"]["textDocument"]["uri"])
            position = {"line":1,"character":8}
            if message["params"]["position"]["line"] == 5:
                result = [{"targetUri":uri, "targetRange":{"start":position,"end":position},
                    "targetSelectionRange":{"start":position,"end":position}}]
            else:
                result = {"uri":uri, "range":{"start":position,"end":position}}
            send({"jsonrpc":"2.0", "id":message["id"], "result":result})
        elif method == "workspace/symbol":
            send({"jsonrpc":"2.0", "id":message["id"], "result":[
                {"name":"entry π", "kind":12, "containerName":"target",
                    "location":{"uri":os.environ["ELISA_LSP_SYMBOL_URI"],
                        "range":{"start":{"line":2,"character":4},
                                 "end":{"line":2,"character":9}}}},
                {"name":"ignored", "kind":12,
                    "location":{"uri":"file:///tmp/not-elisa.txt",
                        "range":{"start":{"line":0,"character":0}}}},
                {"name":"remote", "kind":12,
                    "location":{"uri":"https://example.invalid/main.elisa",
                        "range":{"start":{"line":0,"character":0}}}}
            ]})
        elif method == "shutdown":
            send({"jsonrpc":"2.0", "id":message["id"], "result":None})
        elif method == "exit":
            break
    '''
)


def _command(name: str, *fields: str) -> bytes:
    encoded = [field.encode("utf-8").hex() for field in fields]
    return (" ".join((name, *encoded)) + "\n").encode("ascii")


def _batch(process: subprocess.Popen[bytes]) -> list[str]:
    records: list[str] = []
    assert process.stdout is not None
    while True:
        line = process.stdout.readline()
        if not line:
            raise AssertionError("LSP sidecar exited before completing a response batch")
        record = line.rstrip(b"\n").decode("ascii")
        if record == "Z":
            return records
        records.append(record)


def _wait_for_diagnostic(process: subprocess.Popen[bytes], expected: str) -> list[str]:
    deadline = time.monotonic() + 3.0
    latest: list[str] = []
    while time.monotonic() < deadline:
        assert process.stdin is not None
        process.stdin.write(b"POLL\n")
        process.stdin.flush()
        latest = _batch(process)
        if any(expected in record for record in latest):
            return latest
        time.sleep(0.01)
    raise AssertionError(f"timed out waiting for {expected!r}; last batch={latest!r}")


def test_hex_fields_reject_ambiguous_and_invalid_text() -> None:
    assert _hex_decode("e29c93", limit=3) == "✓"
    for malformed in ("61 62", "gg", "ff", "00"):
        try:
            _hex_decode(malformed, limit=8)
        except ValueError:
            continue
        raise AssertionError(f"accepted malformed hex field {malformed!r}")


def test_native_sidecar_reports_current_versioned_diagnostics() -> None:
    with tempfile.TemporaryDirectory(prefix="elisa-ide-lsp-host-") as temporary:
        root = Path(temporary)
        server_path = root / "fixture_lsp"
        server_path.write_text(f"#!{sys.executable}\n" + SERVER, encoding="utf-8")
        server_path.chmod(0o700)
        source_path = root / "project with spaces" / "source π.elisa"
        source_path.parent.mkdir()
        source_path.write_text("main = 1\n", encoding="utf-8")
        definition_path = source_path.parent / "definition target.elisa"
        definition_path.write_text("module target:\n    def entry() -> i64:\n        return 1\n", encoding="utf-8")

        environment = dict(os.environ)
        environment["ELISA_LSP"] = os.fspath(server_path)
        environment["ELISA_LSP_DEFINITION_URI"] = definition_path.as_uri()
        environment["ELISA_LSP_SYMBOL_URI"] = definition_path.as_uri()
        process = subprocess.Popen(
            [sys.executable, "-u", os.fspath(HOST)],
            cwd=ROOT,
            env=environment,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        try:
            assert process.stdin is not None
            process.stdin.write(_command("OPEN", os.fspath(source_path), "main = 1\n"))
            process.stdin.flush()
            opened = _batch(process)
            if not any("646961676e6f737469632076657273696f6e2031" in record for record in opened):
                opened = _wait_for_diagnostic(process, "646961676e6f737469632076657273696f6e2031")
            status = next(record for record in opened if record.startswith("S\t"))
            assert status.split("\t")[2] == "running", opened
            assert any(record.startswith("G\t") and record.endswith("\t1\t1") for record in opened), opened
            assert any(
                record.startswith("P\t1\t0\t4\t0\t9\t453432\t456c6973612d4c5350\t")
                for record in opened
            )
            related = next(record for record in opened if record.startswith("R\t"))
            assert related.endswith("\t72656c61746564206465636c61726174696f6e")

            process.stdin.write(_command("DEFINITION", os.fspath(source_path), "4:7"))
            process.stdin.flush()
            definition = _batch(process)
            target = next(record for record in definition if record.startswith("D\t"))
            assert target == f"D\t1\t8\t{definition_path.as_uri().encode('utf-8').hex()}", target

            process.stdin.write(_command("SYMBOLS", os.fspath(source_path), "entry"))
            process.stdin.flush()
            symbols = _batch(process)
            symbol = next(record for record in symbols if record.startswith("W\t"))
            assert symbol == "\t".join((
                "W", "12", "2", "4", "entry π".encode("utf-8").hex(),
                "target".encode("utf-8").hex(), definition_path.as_uri().encode("utf-8").hex(),
            )), symbol
            assert len([record for record in symbols if record.startswith("W\t")]) == 1, symbols

            process.stdin.write(_command("DEFINITION", os.fspath(source_path), "5:7"))
            process.stdin.flush()
            definition_link = _batch(process)
            assert next(record for record in definition_link if record.startswith("D\t")) == target

            changed_text = "main = \"😀\"\n"
            process.stdin.write(_command("CHANGE", os.fspath(source_path), changed_text))
            process.stdin.flush()
            changed = _batch(process)
            if not any("646961676e6f737469632076657273696f6e2032" in record for record in changed):
                changed = _wait_for_diagnostic(process, "646961676e6f737469632076657273696f6e2032")
            assert any(record.startswith("G\t") and record.endswith("\t2\t1") for record in changed), changed
            assert any("646961676e6f737469632076657273696f6e2032" in record for record in changed)
            assert not any("646961676e6f737469632076657273696f6e2031" in record for record in changed)

            process.stdin.write(b"RESTART\n")
            process.stdin.flush()
            restarted = _batch(process)
            status = next(record for record in restarted if record.startswith("S\t"))
            assert status.split("\t")[2] == "running"
            assert int(status.split("\t")[3]) == 2, status
            if not any("646961676e6f737469632076657273696f6e2032" in record for record in restarted):
                restarted = _wait_for_diagnostic(process, "646961676e6f737469632076657273696f6e2032")

            # Keep the target open to provide cross-file server context. The
            # sidecar protocol must still project only the currently selected
            # source buffer's version and diagnostics to the native shell.
            process.stdin.write(_command("OPEN", os.fspath(definition_path), "module target = 1\n"))
            process.stdin.flush()
            target_open = _batch(process)
            target_uri_hex = definition_path.resolve().as_uri().encode("utf-8").hex()
            source_uri_hex = source_path.resolve().as_uri().encode("utf-8").hex()
            assert any(record.startswith(f"G\t{target_uri_hex}\t1\t") for record in target_open), target_open
            assert not any(record.startswith(f"G\t{source_uri_hex}\t") for record in target_open), target_open

            process.stdin.write(_command("CHANGE", os.fspath(source_path), "main = 2\n"))
            process.stdin.flush()
            source_reselected = _batch(process)
            if not any(record.startswith(f"G\t{source_uri_hex}\t3\t1") for record in source_reselected):
                source_reselected = _wait_for_diagnostic(
                    process, "646961676e6f737469632076657273696f6e2033"
                )
            assert any(record.startswith(f"G\t{source_uri_hex}\t3\t1") for record in source_reselected), source_reselected
            assert not any(record.startswith(f"G\t{target_uri_hex}\t") for record in source_reselected), source_reselected

            second_root = root / "second workspace"
            second_root.mkdir()
            second_path = second_root / "second.elisa"
            second_path.write_text("module second = 1\n", encoding="utf-8")
            process.stdin.write(_command("OPEN", os.fspath(second_path), "module second = 1\n"))
            process.stdin.flush()
            second_workspace = _batch(process)
            second_uri_hex = second_path.resolve().as_uri().encode("utf-8").hex()
            assert any(record.startswith(f"G\t{second_uri_hex}\t1\t") for record in second_workspace), second_workspace
            assert not any(record.startswith(f"G\t{source_uri_hex}\t") for record in second_workspace), second_workspace

            process.stdin.write(_command("CHANGE", os.fspath(source_path), "main = 3\n"))
            process.stdin.flush()
            first_workspace_reselected = _batch(process)
            assert any(record.startswith(f"G\t{source_uri_hex}\t4\t") for record in first_workspace_reselected), first_workspace_reselected
            assert not any(record.startswith(f"G\t{second_uri_hex}\t") for record in first_workspace_reselected), first_workspace_reselected

            process.stdin.write(_command("CLOSE", os.fspath(source_path)))
            process.stdin.flush()
            closed = _batch(process)
            assert not any(record.startswith("G\t") for record in closed)

            process.stdin.write(b"SHUTDOWN\n")
            process.stdin.flush()
            _batch(process)
            process.stdin.close()
            assert process.wait(timeout=3.0) == 0
        finally:
            if process.poll() is None:
                process.kill()
                process.wait(timeout=3.0)
        assert process.stderr is not None
        stderr = process.stderr.read().decode("utf-8", errors="replace")
        assert "Traceback" not in stderr


def test_missing_server_is_reported_without_losing_the_buffer() -> None:
    with tempfile.TemporaryDirectory(prefix="elisa-ide-lsp-missing-") as temporary:
        root = Path(temporary)
        environment = dict(os.environ)
        environment["ELISA_LSP"] = os.fspath(root / "does-not-exist")
        process = subprocess.Popen(
            [sys.executable, "-u", os.fspath(HOST)],
            cwd=ROOT,
            env=environment,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        source_path = root / "main.elisa"
        source_path.write_text("main = 0\n", encoding="utf-8")
        try:
            assert process.stdin is not None
            process.stdin.write(_command("OPEN", os.fspath(source_path), "main = 0\n"))
            process.stdin.flush()
            records = _batch(process)
            status = next(record for record in records if record.startswith("S\t"))
            assert status.split("\t")[2] == "unavailable"
            assert status.split("\t")[-1]
            assert any(record.startswith("G\t") and record.endswith("\t0\t0") for record in records)
            process.stdin.write(b"SHUTDOWN\n")
            process.stdin.flush()
            _batch(process)
            process.stdin.close()
            assert process.wait(timeout=3.0) == 0
        finally:
            if process.poll() is None:
                process.kill()
                process.wait(timeout=3.0)


if __name__ == "__main__":
    test_hex_fields_reject_ambiguous_and_invalid_text()
    test_native_sidecar_reports_current_versioned_diagnostics()
    test_missing_server_is_reported_without_losing_the_buffer()
    print("test ide_lsp_host_sidecar: ok")
