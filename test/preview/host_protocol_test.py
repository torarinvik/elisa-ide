#!/usr/bin/env python3
"""Protocol-level test for the document host (M03 core acceptance).

Drives the host over its binary protocol and checks the make-a-form / insert /
select / edit-caption / undo / delete flows used by the editor shell. Runs
from `scripts/run_tests.sh`.
"""
import copy, json, os, shutil, struct, subprocess, sys, tempfile, time

HOST = os.path.join(os.path.dirname(__file__), "..", "..", "build", "document_host")

def req(op, a0=0, a1=0, text=None):
    payload = struct.pack("<iii", op, a0, a1)
    if text is not None:
        encoded = text.encode()
        payload += struct.pack("<i", len(encoded)) + encoded
    return payload

def run(requests):
    host_cwd = tempfile.mkdtemp(prefix="elisa-ide-host-stream-")
    os.makedirs(os.path.join(host_cwd, "build"), exist_ok=True)
    try:
        process = subprocess.run([HOST], input=b"".join(requests), capture_output=True,
                                 timeout=60, cwd=host_cwd)
    finally:
        shutil.rmtree(host_cwd)
    if process.returncode != 0:
        raise SystemExit("host exited %d" % process.returncode)
    data, pos, replies = process.stdout, 0, []
    while pos < len(data):
        result, length = struct.unpack_from("<ii", data, pos)
        pos += 8
        text = data[pos:pos + length].decode(errors="replace")
        pos += length
        replies.append((result, text))
    return replies

def decode_reply(data):
    reply = decode_reply_raw(data)
    if reply is None:
        return None
    return reply[0], reply[1].decode("utf-8", errors="replace")

def decode_reply_raw(data):
    if len(data) < 8:
        return None
    result, length = struct.unpack_from("<ii", data, 0)
    if length < 0 or len(data) != 8 + length:
        return None
    return result, data[8:]

def start_signal_host(recovery_dir=None):
    environment = os.environ.copy()
    if recovery_dir is not None:
        environment["ELISA_DESIGNER_RECOVERY_DIR"] = recovery_dir
    host_cwd = tempfile.mkdtemp(prefix="elisa-ide-host-signal-")
    os.makedirs(os.path.join(host_cwd, "build"), exist_ok=True)
    process = subprocess.Popen([HOST, "--signal"], stdin=subprocess.PIPE,
                               stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                               env=environment, cwd=host_cwd)
    process.elisa_ide_test_cwd = host_cwd
    return process

def signal_request(process, op, a0=0, a1=0, text=None, raw_hex=None, binary=False):
    line = "%d %d %d" % (op, a0, a1)
    if text is not None:
        line += " " + text.encode("utf-8").hex()
    elif raw_hex is not None:
        line += " " + raw_hex
    line += "\n"
    host_cwd = getattr(process, "elisa_ide_test_cwd", os.getcwd())
    request_path = os.path.join(host_cwd, "build", "host_request.txt")
    reply_path = os.path.join(host_cwd, "build", "host_reply.bin")
    os.makedirs(os.path.dirname(request_path), exist_ok=True)
    try:
        os.unlink(reply_path)
    except FileNotFoundError:
        pass
    with open(request_path, "wb") as request_file:
        request_file.write(line.encode("ascii"))
    process.stdin.write(b"\n")
    process.stdin.flush()
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        try:
            with open(reply_path, "rb") as reply_file:
                raw_reply = reply_file.read()
                reply = decode_reply_raw(raw_reply) if binary else decode_reply(raw_reply)
            if reply is not None:
                return reply
        except FileNotFoundError:
            pass
        time.sleep(0.01)
    raise AssertionError("timed out waiting for signal-mode host reply")

def stop_signal_host(process):
    process.stdin.close()
    process.wait(timeout=10)
    if process.returncode != 0:
        raise AssertionError("signal-mode host exited %d: %s" %
                             (process.returncode, process.stderr.read().decode(errors="replace")))
    host_cwd = getattr(process, "elisa_ide_test_cwd", None)
    if host_cwd is not None:
        shutil.rmtree(host_cwd)

def check_signal_lifecycle(check):
    with tempfile.TemporaryDirectory(prefix="elisa-ui designer ") as directory:
        # Build a real, self-contained project fixture so opcode 11 exercises
        # manifest plus relative entry-form loading before Save As is tested.
        project_dir = os.path.join(directory, "project résumé")
        forms_dir = os.path.join(project_dir, "forms")
        os.makedirs(forms_dir)
        source_form = os.path.join(os.path.dirname(__file__), "..", "fixtures",
                                   "settings-form.elisaform.json")
        with open(source_form, encoding="utf-8") as source:
            form = json.load(source)
        secondary_form = copy.deepcopy(form)
        secondary_form["id"] = "form-secondary"
        secondary_form["name"] = "Secondary"
        secondary_form["moduleSymbol"] = "secondary_view"
        secondary_ids = {node["id"]: "s" + str(index)
                         for index, node in enumerate(secondary_form["nodes"])}
        for node in secondary_form["nodes"]:
            node["id"] = secondary_ids[node["id"]]
            node["children"] = [secondary_ids[child] for child in node["children"]]
            if node["id"] == secondary_ids["node-reset"]:
                node["events"]["click"]["handler"] = "SecondaryHandlers::secondary_clicked"
        secondary_form["root"] = secondary_ids[secondary_form["root"]]
        edited_text = "Grüße — 東京 😀"
        form["nodes"][1]["properties"]["text"] = edited_text
        # The host fixture path exercises document persistence, not the
        # compiler's current ColorValue encoding edge case. Omit explicit
        # colors so the saved standalone document remains independently
        # decodable by a fresh host.
        for node in form["nodes"]:
            for key in ("background", "color", "textColor", "hoverColor", "pressColor"):
                node.get("properties", {}).pop(key, None)
            if node.get("id") == "node-reset":
                node.get("properties", {}).pop("text", None)
        with open(os.path.join(forms_dir, "main.elisaform.json"), "w", encoding="utf-8") as target:
            json.dump(form, target, ensure_ascii=False)
        with open(os.path.join(forms_dir, "secondary.elisaform.json"), "w", encoding="utf-8") as target:
            json.dump(secondary_form, target, ensure_ascii=False)
        with open(os.path.join(os.path.dirname(__file__), "..", "fixtures",
                               "settings-project.elisaproject.json"), encoding="utf-8") as source:
            project = json.load(source)
        project["forms"][0]["path"] = "forms/main.elisaform.json"
        project["forms"].append({"id": "form-secondary", "path": "forms/secondary.elisaform.json"})
        project_path = os.path.join(project_dir, "Résumé project.elisaproject.json")
        with open(project_path, "w", encoding="utf-8") as target:
            json.dump(project, target, ensure_ascii=False)

        save_path = os.path.join(directory, "forms with spaces", "Räksmörgås 東京.elisaform.json")
        os.makedirs(os.path.dirname(save_path))
        process = start_signal_host()
        try:
            check("signal hello", signal_request(process, 1)[0] == 1)
            source_path = os.path.join(project_dir, "src", "main.elisa")
            os.makedirs(os.path.dirname(source_path))
            source_bytes = (
                "module demo:\r\n"
                "    # Grüße 😀\r\n"
                "    def main() -> i64:\r\n"
                "        return 0\r\n"
            ).encode("utf-8")
            with open(source_path, "wb") as source:
                source.write(source_bytes)
            check("open accepts a UTF-8 Elisa source file",
                  signal_request(process, 112, text=source_path)[0] == 1
                  and signal_request(process, 113)[0] == 1
                  and signal_request(process, 114)[1] == source_path)
            check("source line projection preserves Unicode and strips CRLF for display",
                  signal_request(process, 115)[0] == 5
                  and signal_request(process, 116, 0)[1] == "module demo:"
                  and signal_request(process, 116, 1)[1] == "    # Grüße 😀"
                  and signal_request(process, 116, 3)[1] == "        return 0"
                  and signal_request(process, 116, 4)[1] == "")
            edited_line = "    # Hallo 🐈"
            check("source edits update the retained line buffer and mark it dirty without writing the file",
                  signal_request(process, 130)[0] == 0
                  and signal_request(process, 132, 1, text=edited_line)[0] == 1
                  and signal_request(process, 130)[0] == 1
                  and signal_request(process, 116, 1) == (1, edited_line)
                  and open(source_path, "rb").read() == source_bytes)
            check("source undo and redo preserve independent Unicode buffer history",
                  signal_request(process, 136)[0] == 1
                  and signal_request(process, 134)[0] == 1
                  and signal_request(process, 130)[0] == 0
                  and signal_request(process, 116, 1)[1] == "    # Grüße 😀"
                  and open(source_path, "rb").read() == source_bytes
                  and signal_request(process, 137)[0] == 1
                  and signal_request(process, 135)[0] == 1
                  and signal_request(process, 130)[0] == 1
                  and signal_request(process, 116, 1)[1] == edited_line)
            branch_line = "    # Branch 🐈"
            check("new source edits discard the redo branch and restore through the new branch",
                  signal_request(process, 134)[0] == 1
                  and signal_request(process, 116, 1)[1] == "    # Grüße 😀"
                  and signal_request(process, 137)[0] == 1
                  and signal_request(process, 132, 1, text=branch_line)[0] == 1
                  and signal_request(process, 137)[0] == 0
                  and signal_request(process, 134)[0] == 1
                  and signal_request(process, 130)[0] == 0
                  and signal_request(process, 116, 1)[1] == "    # Grüße 😀"
                  and signal_request(process, 132, 1, text=edited_line)[0] == 1
                  and signal_request(process, 137)[0] == 0)
            check("source save atomically publishes edited UTF-8 and clears its dirty state",
                  signal_request(process, 131)[0] == 1
                  and signal_request(process, 130)[0] == 0
                  and open(source_path, "rb").read() == source_bytes.replace("    # Grüße 😀\r\n".encode(), (edited_line + "\r\n").encode()))
            externally_changed = source_bytes.replace("    def main() -> i64:\r\n".encode(), "    # external change\r\n    def main() -> i64:\r\n".encode())
            with open(source_path, "wb") as source:
                source.write(externally_changed)
            local_edit = "    def main() -> i64: # local edit"
            check("save detects an external edit and preserves the other writer's bytes",
                  signal_request(process, 132, 2, text=local_edit)[0] == 1
                  and signal_request(process, 130)[0] == 1
                  and signal_request(process, 131)[0] == 0
                  and "changed on disk" in signal_request(process, 56)[1]
                  and open(source_path, "rb").read() == externally_changed)
            check("source editor rejects embedded line breaks without changing its buffer",
                  signal_request(process, 132, 2, text="first\nsecond")[0] == 0
                  and signal_request(process, 116, 2)[1] == local_edit)
            long_line_path = os.path.join(project_dir, "src", "long-line.elisa")
            long_line_bytes = b"x" * 900 + b"\n"
            with open(long_line_path, "wb") as source:
                source.write(long_line_bytes)
            long_line_opened = signal_request(process, 112, text=long_line_path)[0] == 1
            long_line_result, long_line_projection = signal_request(process, 116, 0)
            check("lines clipped by the bounded projection are locked against destructive replacement",
                  long_line_opened and long_line_result == 2
                  and long_line_projection.endswith(" ...")
                  and signal_request(process, 136)[0] == 0
                  and signal_request(process, 132, 0, text="x")[0] == 0
                  and open(long_line_path, "rb").read() == long_line_bytes)
            check("a valid source can be reopened after a long-line inspection",
                  signal_request(process, 112, text=source_path)[0] == 1
                  and signal_request(process, 114)[1] == source_path)
            split_path = os.path.join(project_dir, "src", "split-lines.elisa")
            split_bytes = (
                "module split_lines:\r\n"
                "    # Grüße 😀\r\n"
                "    def main() -> i64:\r\n"
                "        return 0\r\n"
            ).encode("utf-8")
            with open(split_path, "wb") as source:
                source.write(split_bytes)
            check("source line byte offsets address the original UTF-8 buffer",
                  signal_request(process, 112, text=split_path)[0] == 1
                  and signal_request(process, 118, 1)[0] == len("module split_lines:\r\n".encode("utf-8")))
            unicode_line_start = signal_request(process, 118, 1)[0]
            check("source range edits reject a position inside a multibyte character",
                  signal_request(process, 133, unicode_line_start + 15,
                                 unicode_line_start + 15, text="\n")[0] == 0
                  and signal_request(process, 115)[0] == 5
                  and signal_request(process, 116, 1)[1] == "    # Grüße 😀")
            check("Enter-style source range replacement splits a line and preserves CRLF",
                  signal_request(process, 133, unicode_line_start + 14,
                                 unicode_line_start + 14, text="\n")[0] == 1
                  and signal_request(process, 115)[0] == 6
                  and signal_request(process, 116, 1)[1] == "    # Grüße "
                  and signal_request(process, 116, 2)[1] == "😀"
                  and signal_request(process, 136)[0] == 1
                  and signal_request(process, 134)[0] == 1
                  and signal_request(process, 115)[0] == 5
                  and signal_request(process, 116, 1)[1] == "    # Grüße 😀"
                  and signal_request(process, 137)[0] == 1
                  and signal_request(process, 135)[0] == 1
                  and signal_request(process, 115)[0] == 6
                  and signal_request(process, 116, 2)[1] == "😀"
                  and signal_request(process, 131)[0] == 1
                  and open(split_path, "rb").read() == split_bytes.replace(
                      "    # Grüße 😀\r\n".encode("utf-8"),
                      "    # Grüße \r\n😀\r\n".encode("utf-8")))
            joined_start = signal_request(process, 118, 1)[0] + len("    # Grüße ".encode("utf-8"))
            joined_finish = signal_request(process, 118, 2)[0]
            check("empty replacement joins source lines without changing CRLF bytes elsewhere",
                  signal_request(process, 133, joined_start, joined_finish, text="")[0] == 1
                  and signal_request(process, 115)[0] == 5
                  and signal_request(process, 116, 1)[1] == "    # Grüße 😀"
                  and signal_request(process, 134)[0] == 1
                  and signal_request(process, 115)[0] == 6
                  and signal_request(process, 116, 1)[1] == "    # Grüße "
                  and signal_request(process, 116, 2)[1] == "😀"
                  and signal_request(process, 135)[0] == 1
                  and signal_request(process, 115)[0] == 5
                  and signal_request(process, 116, 1)[1] == "    # Grüße 😀"
                  and signal_request(process, 131)[0] == 1
                  and open(split_path, "rb").read() == split_bytes)
            check("source view can switch back after a line-splitting edit",
                  signal_request(process, 112, text=source_path)[0] == 1
                  and signal_request(process, 114)[1] == source_path)
            replace_all_path = os.path.join(project_dir, "src", "replace-all.elisa")
            replace_all_bytes = (
                "module replace_all:\r\n"
                "# old old\r\n"
                "# old 😀 old\r\n"
            ).encode("utf-8")
            with open(replace_all_path, "wb") as source:
                source.write(replace_all_bytes)
            replace_all_query = "old"
            replace_all_replacement = "new"
            replace_all_payload = replace_all_query + replace_all_replacement
            replace_all_opened = signal_request(process, 112, text=replace_all_path)[0]
            replaced_count = signal_request(process, 150, len(replace_all_query.encode("utf-8")),
                                            0, text=replace_all_payload)[0]
            check("Replace All preserves CRLF and commits one full-buffer undo transaction",
                  replace_all_opened == 1 and replaced_count == 4
                  and signal_request(process, 115)[0] == 4
                  and signal_request(process, 116, 1)[1] == "# new new"
                  and signal_request(process, 116, 2)[1] == "# new 😀 new"
                  and signal_request(process, 136)[0] == 1
                  and signal_request(process, 134)[0] == 1
                  and signal_request(process, 136)[0] == 0
                  and signal_request(process, 116, 1)[1] == "# old old"
                  and signal_request(process, 116, 2)[1] == "# old 😀 old"
                  and signal_request(process, 135)[0] == 1
                  and signal_request(process, 116, 1)[1] == "# new new"
                  and signal_request(process, 131)[0] == 1
                  and open(replace_all_path, "rb").read() == replace_all_bytes.replace(b"old", b"new"))
            check("the prior handwritten source buffer can reopen after Replace All",
                  replaced_count == 4
                  and signal_request(process, 112, text=source_path)[0] == 1
                  and signal_request(process, 114)[1] == source_path)
            invalid_source = os.path.join(project_dir, "src", "invalid.elisa")
            with open(invalid_source, "wb") as source:
                source.write(b"def invalid():\n\xff\n")
            check("malformed UTF-8 source is rejected without replacing the open view",
                  signal_request(process, 112, text=invalid_source)[0] == 0
                  and signal_request(process, 114)[1] == source_path)
            check("project open accepts a Unicode path",
                  signal_request(process, 11, text=project_path)[0] == 1)
            check("opening a form clears the prior source-only view",
                  signal_request(process, 113)[0] == 0
                  and signal_request(process, 136)[0] == 0)
            project_nodes = signal_request(process, 23)[0]
            check("project open loads the entry form", project_nodes == len(form["nodes"]))
            form_path_result, form_path_text = signal_request(process, 109)
            check("host exposes the owning form path for generated source",
                  form_path_result == 1
                  and form_path_text == "forms/main.elisaform.json")
            file_count_result, _ = signal_request(process, 110)
            file_paths = [signal_request(process, 111, index)[1] for index in range(file_count_result)]
            check("workspace file projection preserves project-relative ownership",
                  file_count_result == 4
                  and file_paths[0] == "Résumé project.elisaproject.json"
                  and file_paths[1] == "forms/main.elisaform.json"
                  and file_paths[2] == "generated/SettingsExample_view.elisa"
                  and file_paths[3] == "generated/SettingsExample_view.elisa.map.json")
            generated_result, generated_source = signal_request(process, 106)
            check("generated source projection is read-only and source-shaped",
                  generated_result == 1
                  and "# Generated by Elisa IDE" in generated_source
                  and "module settings_view:" in generated_source
                  and "def build() -> bool" in generated_source)
            generated_map_result, generated_map_text = signal_request(process, 107)
            generated_map = json.loads(generated_map_text) if generated_map_text else {}
            check("generated source map is exposed with exact-byte identity",
                  generated_map_result == 1
                  and generated_map.get("format") == "elisa-ide-source-map"
                  and generated_map.get("version") == 1
                  and generated_map.get("generatedLength") == len(generated_source.encode("utf-8"))
                  and isinstance(generated_map.get("generatedHash"), str)
                  and generated_map.get("spans"))
            handler_path = os.path.join(project_dir, "src", "main_handlers.elisa")
            secondary_path = os.path.join(forms_dir, "secondary.elisaform.json")
            project["forms"][1]["path"] = "../outside.elisaform.json"
            with open(project_path, "w", encoding="utf-8") as manifest:
                json.dump(project, manifest, ensure_ascii=False)
            unsafe_path_result = signal_request(process, 117)[0]
            unsafe_path_status = signal_request(process, 56)[1]
            check("handler scan rejects manifest paths that escape the project directory",
                  unsafe_path_result == 0
                  and "non-relative project form path" in unsafe_path_status
                  and not os.path.exists(handler_path))
            project["forms"][1]["path"] = "forms/secondary.elisaform.json"
            with open(project_path, "w", encoding="utf-8") as manifest:
                json.dump(project, manifest, ensure_ascii=False)
            with open(secondary_path, "wb") as secondary:
                secondary.write(b"{ invalid project form")
            invalid_scan_result = signal_request(process, 117)[0]
            invalid_scan_status = signal_request(process, 56)[1]
            check("handler creation refuses an invalid secondary form without publishing partial source",
                  invalid_scan_result == 0
                  and "invalid or mismatched referenced form" in invalid_scan_status
                  and not os.path.exists(handler_path))
            with open(secondary_path, "w", encoding="utf-8") as secondary:
                json.dump(secondary_form, secondary, ensure_ascii=False)
            handler_open_result = signal_request(process, 117)[0]
            handler_open_status = signal_request(process, 56)[1]
            check("handler action creates a source file with stubs for connected project events",
                  handler_open_result == 1
                  and signal_request(process, 113)[0] == 1
                  and signal_request(process, 114)[1] == handler_path
                  and os.path.isfile(handler_path))
            if handler_open_result != 1:
                print("handler action status: " + handler_open_status)
            handler_lines = [signal_request(process, 116, index)[1]
                             for index in range(signal_request(process, 115)[0])]
            handler_source = "\n".join(handler_lines)
            check("created handler source groups functions by module and emits valid TODO stubs",
                  "module SettingsHandlers:" in handler_source
                  and "def name_changed(widget: usize, event: i32) -> void:" in handler_source
                  and "def reset_clicked(widget: usize, event: i32) -> void:" in handler_source
                  and "def save_clicked(widget: usize, event: i32) -> void:" in handler_source
                  and "module SecondaryHandlers:" in handler_source
                  and "def secondary_clicked(widget: usize, event: i32) -> void:" in handler_source
                  and handler_source.count("def reset_clicked(") == 1
                  and "TODO: Add this event's application behavior." in handler_source)
            with open(handler_path, "wb") as handlers:
                handlers.write(b"global mutable user_owned_handler_marker: i64 = 1\n")
            check("opening existing handler source preserves user edits",
                  signal_request(process, 117)[0] == 1
                  and signal_request(process, 114)[1] == handler_path
                  and signal_request(process, 116, 0)[1] == "global mutable user_owned_handler_marker: i64 = 1")
            nested_binding = (signal_request(process, 81, text="node-reset")[0] == 1
                              and signal_request(process, 94, 0, text="Outer::Inner::nested")[0] == 1)
            check("existing handler file still opens when a form uses a nested namespace",
                  nested_binding
                  and signal_request(process, 117)[0] == 1
                  and signal_request(process, 116, 0)[1] == "global mutable user_owned_handler_marker: i64 = 1")
            signal_request(process, 94, 0, text="SettingsHandlers::reset_clicked")
            event_offset = generated_source.index("SettingsHandlers::reset_clicked")
            check("generated source byte navigation selects its owning form node",
                  signal_request(process, 108, event_offset)[0] == 1
                  and signal_request(process, 54)[1] == "Reset")
            check("generated source byte navigation rejects out-of-range offsets",
                  signal_request(process, 108, len(generated_source.encode("utf-8")) + 1)[0] == 0
                  and signal_request(process, 54)[1] == "Reset")
            check("absent numeric defaults are projected with their type",
                  signal_request(process, 31, 0)[0] == 1
                  and signal_request(process, 52, 3)[1] == "grow"
                  and signal_request(process, 53, 3)[1] == "0"
                  and signal_request(process, 62, 3)[0] == 2)
            check("absent enum defaults are projected with their type",
                  signal_request(process, 52, 6)[1] == "crossAlignment"
                  and signal_request(process, 53, 6)[1] == "stretch"
                  and signal_request(process, 62, 6)[0] == 4)
            check("absent boolean defaults are projected with their type",
                  signal_request(process, 52, 8)[1] == "visible"
                  and signal_request(process, 53, 8)[1] == "true"
                  and signal_request(process, 62, 8)[0] == 0)
            check("absent text defaults are projected with their type",
                  signal_request(process, 81, text="node-reset")[0] == 1
                  and signal_request(process, 52, 0)[1] == "text"
                  and signal_request(process, 53, 0)[1] == "Button"
                  and signal_request(process, 62, 0)[0] == 3)
            check("property reset restores the typed registry default",
                  signal_request(process, 70, 0, text="Custom caption")[0] == 1
                  and signal_request(process, 53, 0)[1] == "Custom caption"
                  and signal_request(process, 34, 0)[0] == 1
                  and signal_request(process, 53, 0)[1] == "Button")
            check("property reset participates in undo and redo",
                  signal_request(process, 13)[0] == 1
                  and signal_request(process, 53, 0)[1] == "Custom caption"
                  and signal_request(process, 14)[0] == 1
                  and signal_request(process, 53, 0)[1] == "Button")
            check("selected button exposes its event descriptor and handler",
                  signal_request(process, 90)[0] == 1
                  and signal_request(process, 91, 0)[1] == "Click"
                  and signal_request(process, 92, 0)[1] == "SettingsHandlers::reset_clicked"
                  and signal_request(process, 93, 0)[1] == "def handler(widget: usize, event: i32) -> void")
            check("event handler replacement is undoable",
                  signal_request(process, 94, 0, text="reset_clicked")[0] == 1
                  and signal_request(process, 92, 0)[1] == "reset_clicked"
                  and signal_request(process, 13)[0] == 1
                  and signal_request(process, 92, 0)[1] == "SettingsHandlers::reset_clicked"
                  and signal_request(process, 14)[0] == 1
                  and signal_request(process, 92, 0)[1] == "reset_clicked")
            check("event disconnection is undoable",
                  signal_request(process, 95, 0)[0] == 1
                  and signal_request(process, 92, 0)[1] == ""
                  and signal_request(process, 13)[0] == 1
                  and signal_request(process, 92, 0)[1] == "reset_clicked"
                  and signal_request(process, 13)[0] == 1
                  and signal_request(process, 92, 0)[1] == "SettingsHandlers::reset_clicked")
            metadata_first = signal_request(process, 22)[0] - 2
            metadata_rows = (
                signal_request(process, 52, metadata_first)[1] == "Display name"
                and signal_request(process, 53, metadata_first)[1] == "Reset"
                and signal_request(process, 62, metadata_first)[0] == 7
                and signal_request(process, 52, metadata_first + 1)[1] == "Generated symbol"
                and signal_request(process, 53, metadata_first + 1)[1] == "reset_button"
                and signal_request(process, 62, metadata_first + 1)[0] == 7)
            check("selected component exposes separate display-name and generated-symbol rows",
                  metadata_rows)
            renamed_display = signal_request(process, 70, metadata_first, text="Apply settings")[0] == 1
            renamed_symbol = signal_request(process, 70, metadata_first + 1, text="apply_settings")[0] == 1
            generated_after_rename = signal_request(process, 106)[1]
            check("display name and generated symbol can be renamed independently in the inspector",
                  renamed_display and renamed_symbol
                  and signal_request(process, 54)[1] == "Apply settings"
                  and signal_request(process, 53, metadata_first)[1] == "Apply settings"
                  and signal_request(process, 53, metadata_first + 1)[1] == "apply_settings"
                  and "def widget_apply_settings()" in generated_after_rename)
            undo_symbol = signal_request(process, 13)[0] == 1
            undo_display = signal_request(process, 13)[0] == 1
            undone_display = signal_request(process, 53, metadata_first)[1]
            undone_symbol = signal_request(process, 53, metadata_first + 1)[1]
            redo_display = signal_request(process, 14)[0] == 1
            redo_symbol = signal_request(process, 14)[0] == 1
            check("display-name and symbol renames remain distinct undoable commands",
                  undo_symbol and undo_display
                  and undone_display == "Reset" and undone_symbol == "reset_button"
                  and redo_display and redo_symbol
                  and signal_request(process, 53, metadata_first)[1] == "Apply settings"
                  and signal_request(process, 53, metadata_first + 1)[1] == "apply_settings")
            check("failed form open keeps the previous document",
                  signal_request(process, 11,
                                 text=os.path.join(project_dir, "missing.elisaform.json"))[0] == 0
                  and signal_request(process, 23)[0] == project_nodes)
            check("a new form can be created", signal_request(process, 10)[0] == 1)
            check("new form begins with its root node", signal_request(process, 23)[0] == 1)
            check("untitled form has no Save destination", signal_request(process, 44)[0] == 0)
            inserted_row = signal_request(process, 30, 4)[0]
            check("a component can be inserted into the new form", inserted_row >= 0
                  and signal_request(process, 23)[0] == 2)
            check("the inserted component can be selected", signal_request(process, 31, 1)[0] == 1)
            check("the inserted caption can be edited",
                  signal_request(process, 70, 0, text=edited_text)[0] == 1)
            preview_result, preview_bytes = signal_request(process, 80, binary=True)
            preview_ok = preview_result == 1 and len(preview_bytes) > 28
            if preview_ok:
                magic, version, reserved, revision, width_q10, height_q10, node_count, reserved_tail = struct.unpack_from("<4sHHQIIHH", preview_bytes, 0)
                preview_ok = (magic == b"ELPF" and version == 1 and reserved == 0
                              and reserved_tail == 0 and revision >= 2
                              and width_q10 > 0 and height_q10 > 0 and node_count == 2)
                cursor = 28
                seen_ids = set()
                seen_button_text = False
                for _ in range(node_count):
                    id_length, kind, parent_index, flags, axis, cross, main, node_reserved = struct.unpack_from("<BBHHBBBB", preview_bytes, cursor)
                    cursor += 10
                    node_id = preview_bytes[cursor:cursor + id_length].decode("ascii")
                    cursor += id_length + 28 + 20
                    strings = []
                    for _field in range(4):
                        field_length = struct.unpack_from("<H", preview_bytes, cursor)[0]
                        cursor += 2
                        strings.append(preview_bytes[cursor:cursor + field_length].decode("utf-8"))
                        cursor += field_length
                    preview_ok = (preview_ok and id_length > 0 and kind in (1, 5)
                                  and node_reserved == 0 and node_id not in seen_ids
                                  and axis <= 1 and cross <= 3 and main <= 3)
                    seen_ids.add(node_id)
                    if kind == 5:
                        preview_ok = (preview_ok and parent_index == 0 and strings[0] == edited_text
                                      and (flags & 2) != 0)
                        seen_button_text = True
                    elif kind == 1:
                        preview_ok = preview_ok and parent_index == 65535
                preview_ok = preview_ok and cursor == len(preview_bytes) and seen_button_text and len(seen_ids) == 2
            check("preview snapshot contains the current revision, stable IDs, and edited UTF-8 caption", preview_ok)
            saved_nodes = signal_request(process, 23)[0]
            check("Save As creates a Unicode path", signal_request(process, 18, text=save_path)[0] == 1
                  and os.path.isfile(save_path))
            check("saved standalone form has a Save destination", signal_request(process, 44)[0] == 1)
            with open(save_path, "rb") as saved_form:
                original_bytes = saved_form.read()
            check("saved form contains the UTF-8 content", edited_text.encode("utf-8") in original_bytes)
            saved_json = json.loads(original_bytes.decode("utf-8"))
            saved_ids = [node.get("id", "") for node in saved_json.get("nodes", [])]
            saved_id_set = set(saved_ids)
            check("generated color defaults remain valid in saved JSON",
                  saved_json["nodes"][0]["properties"].get("background") == "#181b21ff"
                  and saved_json["nodes"][1]["properties"].get("color") == "#4070e0ff")
            check("saved JSON has a nonempty root and node IDs",
                  bool(saved_json.get("root")) and all(saved_ids)
                  and len(saved_ids) == len(saved_id_set)
                  and saved_json["root"] in saved_id_set)
            check("saved JSON child links resolve to nodes",
                  all(child in saved_id_set for node in saved_json.get("nodes", [])
                      for child in node.get("children", [])))
            child_ids = [child for node in saved_json.get("nodes", [])
                         for child in node.get("children", [])]
            root_id = saved_json.get("root", "")
            node_by_id = {node["id"]: node for node in saved_json["nodes"]}
            reachable = set()
            pending = [root_id] if root_id else []
            while pending:
                current = pending.pop()
                if current in reachable:
                    continue
                reachable.add(current)
                pending.extend(node_by_id.get(current, {}).get("children", []))
            check("saved JSON parent-child links form one rooted tree",
                  bool(root_id) and root_id not in child_ids
                  and len(child_ids) == len(set(child_ids))
                  and set(child_ids) == saved_id_set - {root_id}
                  and reachable == saved_id_set)
            check("Save As refuses an existing destination",
                  signal_request(process, 18, text=save_path)[0] == 0)
            with open(save_path, "rb") as saved_form:
                check("refused Save As preserves original bytes", saved_form.read() == original_bytes)
        finally:
            stop_signal_host(process)

        # A newly started process has no retained session. Opening the saved
        # standalone form must reconstruct the committed value from disk.
        reopened = start_signal_host()
        try:
            check("fresh host reopens the standalone form",
                  signal_request(reopened, 11, text=save_path)[0] == 1)
            check("reopened form has the saved nodes", signal_request(reopened, 23)[0] == saved_nodes)
            check("fresh host saves its reopened in-memory form", signal_request(reopened, 15)[0] == 1)
            with open(save_path, "rb") as saved_form:
                round_trip_bytes = saved_form.read()
            check("fresh host preserves the saved UTF-8 content",
                  edited_text.encode("utf-8") in round_trip_bytes)
            round_trip_json = json.loads(round_trip_bytes.decode("utf-8"))
            original_graph = {node["id"]: node.get("children", []) for node in saved_json["nodes"]}
            round_trip_graph = {node["id"]: node.get("children", []) for node in round_trip_json["nodes"]}
            check("fresh host round-trips root and node IDs",
                  round_trip_json.get("root") == saved_json.get("root")
                  and set(round_trip_graph) == set(original_graph))
            check("fresh host round-trips parent-child links",
                  round_trip_graph == original_graph)
            check("malformed hex UTF-8 is rejected", signal_request(reopened, 70, 0, raw_hex="c0af")[0] == -1)
            check("odd-length hex is rejected", signal_request(reopened, 70, 0, raw_hex="0")[0] == -1)
            check("non-hex payload is rejected", signal_request(reopened, 70, 0, raw_hex="gg")[0] == -1)
        finally:
            stop_signal_host(reopened)

def check_project_creation(check):
    with tempfile.TemporaryDirectory(prefix="elisa-ui project ") as directory:
        project_dir = os.path.join(directory, "New Project résumé")
        manifest_path = os.path.join(project_dir, "New Project résumé.elisaproject.json")
        form_path = os.path.join(project_dir, "forms", "main.elisaform.json")
        process = start_signal_host()
        try:
            check("project creation starts from a clean untitled form",
                  signal_request(process, 10)[0] == 1
                  and signal_request(process, 40)[0] == 0)
            check("new project accepts a path containing spaces and Unicode",
                  signal_request(process, 19, text=project_dir)[0] == 1)
            check("new project opens its starter form", signal_request(process, 23)[0] == 1
                  and signal_request(process, 40)[0] == 0
                  and signal_request(process, 44)[0] == 1)
            check("project manifest and relative entry form are written",
                  os.path.isfile(manifest_path) and os.path.isfile(form_path))
            with open(manifest_path, "rb") as source:
                manifest_bytes = source.read()
            with open(form_path, "rb") as source:
                form_bytes = source.read()
            manifest = json.loads(manifest_bytes.decode("utf-8"))
            form = json.loads(form_bytes.decode("utf-8"))
            check("new project records the folder name and relative form path",
                  manifest.get("name") == "New Project résumé"
                  and manifest.get("projectId") == "project-main"
                  and manifest.get("entryForm") == "form-main"
                  and manifest.get("forms", [{}])[0].get("path") == "forms/main.elisaform.json")
            original_form_ids = {node.get("id") for node in form.get("nodes", [])}
            check("starter form has one stable root node", form.get("id") == "form-main"
                  and form.get("root") in original_form_ids and len(original_form_ids) == 1)
            check("project creation refuses to reuse an existing folder",
                  signal_request(process, 19, text=project_dir)[0] == 0
                  and signal_request(process, 23)[0] == 1)
            with open(manifest_path, "rb") as source:
                check("existing project manifest survives the collision", source.read() == manifest_bytes)

            inserted_row = signal_request(process, 30, 4)[0]
            check("starter project form accepts an inserted control", inserted_row >= 0
                  and signal_request(process, 23)[0] == 2
                  and signal_request(process, 31, 1)[0] == 1
                  and signal_request(process, 70, 0, text="Saved in project")[0] == 1)
            check("project Save writes the relative entry form", signal_request(process, 15)[0] == 1)
            with open(form_path, "rb") as source:
                saved_form = json.loads(source.read().decode("utf-8"))
            saved_form_ids = {node.get("id") for node in saved_form.get("nodes", [])}
            check("project Save preserves node IDs and edited text",
                  len(saved_form_ids) == 2 and original_form_ids.issubset(saved_form_ids)
                  and any("Saved in project" in json.dumps(node, ensure_ascii=False)
                          for node in saved_form.get("nodes", [])))
        finally:
            stop_signal_host(process)

        reopened = start_signal_host()
        try:
            check("fresh host reopens the generated project manifest",
                  signal_request(reopened, 11, text=manifest_path)[0] == 1)
            check("fresh host reconstructs project form and IDs",
                  signal_request(reopened, 23)[0] == len(saved_form_ids)
                  and signal_request(reopened, 15)[0] == 1)
            check("fresh host preserves project handler-edit text",
                  signal_request(reopened, 31, 1)[0] == 1
                  and signal_request(reopened, 52, 0)[1] == "text"
                  and signal_request(reopened, 53, 0)[1] == "Saved in project")
        finally:
            stop_signal_host(reopened)

def check_recovery(check):
    fixture_path = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "fixtures",
                                                "settings-form.elisaform.json"))
    with tempfile.TemporaryDirectory(prefix="elisa-ide recovery ") as recovery_dir:
        first = start_signal_host(recovery_dir)
        try:
            check("recovery host opens the source form",
                  signal_request(first, 11, text=fixture_path)[0] == 1)
            check("recovery host selects the stable node",
                  signal_request(first, 81, text="node-save")[0] == 1)
            check("editing a property creates a recovery snapshot",
                  signal_request(first, 70, 0, text="Recovered value")[0] == 1
                  and signal_request(first, 101)[0] == 1)
        finally:
            stop_signal_host(first)

        second = start_signal_host(recovery_dir)
        try:
            check("fresh host detects the recovery snapshot after restart",
                  signal_request(second, 11, text=fixture_path)[0] == 1
                  and signal_request(second, 101)[0] == 1)
            check("restore applies and consumes the recovery snapshot",
                  signal_request(second, 102)[0] == 1
                  and signal_request(second, 101)[0] == 0)
            check("restored property value is visible through the inspector",
                  signal_request(second, 81, text="node-save")[0] == 1
                  and signal_request(second, 53, 0)[1] == "Recovered value")
            check("discard is an idempotent recovery operation",
                  signal_request(second, 103)[0] == 1
                  and signal_request(second, 101)[0] == 0)
        finally:
            stop_signal_host(second)

def check_external_changes(check):
    source_path = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "fixtures",
                                               "settings-form.elisaform.json"))
    with tempfile.TemporaryDirectory(prefix="elisa-ide external ") as directory:
        form_path = os.path.join(directory, "external.elisaform.json")
        with open(source_path, "rb") as source:
            original = source.read()
        with open(form_path, "wb") as target:
            target.write(original)

        process = start_signal_host()
        try:
            check("external-change fixture opens as a standalone form",
                  signal_request(process, 11, text=form_path)[0] == 1)
            changed_on_disk = original + b"\n"
            with open(form_path, "wb") as target:
                target.write(changed_on_disk)
            check("a clean document detects an external file change",
                  signal_request(process, 104)[0] == 1)
            check("reload adopts the externally changed clean file",
                  signal_request(process, 105)[0] == 1
                  and signal_request(process, 104)[0] == 0)

            check("a dirty edit remains in memory after disk changes again",
                  signal_request(process, 81, text="node-save")[0] == 1
                  and signal_request(process, 70, 0, text="Unsaved value")[0] == 1)
            externally_newer = changed_on_disk + b"\n"
            with open(form_path, "wb") as target:
                target.write(externally_newer)
            check("Save refuses a dirty document whose file changed externally",
                  signal_request(process, 104)[0] == 1
                  and signal_request(process, 15)[0] == 0
                  and signal_request(process, 104)[0] == 1)
            check("external bytes survive the refused save",
                  open(form_path, "rb").read() == externally_newer)
        finally:
            stop_signal_host(process)

def check_source_conflicts(check):
    with tempfile.TemporaryDirectory(prefix="elisa-ide source conflict ") as directory:
        source_path = os.path.join(directory, "conflict.elisa")
        initial = b"module base:\n# \xf0\x9f\x98\x80 base\n"
        with open(source_path, "wb") as target:
            target.write(initial)
        process = start_signal_host()
        try:
            check("source conflict fixture opens cleanly",
                  signal_request(process, 112, text=source_path)[0] == 1
                  and signal_request(process, 138)[0] == 0
                  and signal_request(process, 139)[0] == 0)
            emoji_offset = initial.index("😀".encode("utf-8"))
            last_base_offset = initial.rfind(b"base")
            check("source search returns exact UTF-8 byte offsets in both directions",
                  signal_request(process, 148)[0] == len(initial)
                  and signal_request(process, 147, 0, 1, text="😀")[0] == emoji_offset
                  and signal_request(process, 147, len(initial), 0, text="base")[0] == last_base_offset
                  and signal_request(process, 149, emoji_offset)[0] == 1
                  and signal_request(process, 147, 0, 1, text="missing")[0] == -1)
            check("local source edits remain undoable before external resolution",
                  signal_request(process, 132, 0, text="module local:")[0] == 1
                  and signal_request(process, 130)[0] == 1
                  and signal_request(process, 136)[0] == 1)
            external_local_conflict = b"module disk:\n"
            with open(source_path, "wb") as target:
                target.write(external_local_conflict)
            conflict_detected = signal_request(process, 138)[0] == 1
            comparison = signal_request(process, 143)[1]
            check("source external-change checks retain a bounded line comparison",
                  conflict_detected
                  and signal_request(process, 139)[0] == 1
                  and signal_request(process, 140)[0] == 1
                  and signal_request(process, 141)[0] == 2
                  and signal_request(process, 142, 0)[1] == "module disk:"
                  and "- local: module local:" in comparison
                  and "+ disk:  module disk:" in comparison)
            check("Keep Buffer rebases against disk without losing source undo history",
                  signal_request(process, 145)[0] == 1
                  and signal_request(process, 139)[0] == 0
                  and signal_request(process, 130)[0] == 1
                  and signal_request(process, 136)[0] == 1
                  and signal_request(process, 131)[0] == 1
                  and open(source_path, "rb").read() == b"module local:\n# \xf0\x9f\x98\x80 base\n")
            with open(source_path, "wb") as target:
                target.write(b"module refreshed:\n")
            check("Reload Disk replaces the buffer and clears its old history",
                  signal_request(process, 138)[0] == 1
                  and signal_request(process, 144)[0] == 1
                  and signal_request(process, 116, 0)[1] == "module refreshed:"
                  and signal_request(process, 130)[0] == 0
                  and signal_request(process, 136)[0] == 0
                  and signal_request(process, 139)[0] == 0)
            signal_request(process, 132, 0, text="module unsaved:")
            with open(source_path, "wb") as target:
                target.write(b"module newest:\n")
            check("explicit Reload Disk may discard a dirty buffer only after a new external snapshot is shown",
                  signal_request(process, 138)[0] == 1
                  and signal_request(process, 140)[0] == 1
                  and signal_request(process, 144)[0] == 1
                  and signal_request(process, 116, 0)[1] == "module newest:"
                  and signal_request(process, 130)[0] == 0
                  and signal_request(process, 136)[0] == 0)
            save_as_path = os.path.join(directory, "saved-copy.elisa")
            signal_request(process, 132, 0, text="module copied:")
            check("source Save As safely creates a new file and rebinds the buffer",
                  signal_request(process, 146, text=save_as_path)[0] == 1
                  and signal_request(process, 114)[1] == save_as_path
                  and signal_request(process, 130)[0] == 0
                  and open(save_as_path, "rb").read() == b"module copied:\n")
            collision_path = os.path.join(directory, "occupied.elisa")
            with open(collision_path, "wb") as target:
                target.write(b"keep existing bytes\n")
            signal_request(process, 132, 0, text="module still local:")
            check("source Save As refuses to replace an existing file",
                  signal_request(process, 146, text=collision_path)[0] == 0
                  and signal_request(process, 130)[0] == 1
                  and open(collision_path, "rb").read() == b"keep existing bytes\n")
        finally:
            stop_signal_host(process)

def check_stable_id_selection(check):
    fixture_path = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "fixtures",
                                                "settings-form.elisaform.json"))
    replies = run([
        req(11, text=fixture_path),
        req(81, text="node-save"), req(54), req(55), req(52, 0),
        req(81, text="node-missing"), req(54),
        req(81, text="node_save"), req(54),
        req(2),
    ])
    check("binary host opens a stable-ID selection fixture", replies[0][0] == 1)
    check("binary opcode 81 selects the matching stable node ID",
          replies[1][0] == 1 and replies[2][1] == "Save"
          and replies[3][1] == "elisa.ui.button" and replies[4][1] == "text")
    check("unknown stable ID is rejected without changing selection",
          replies[5][0] == 0 and replies[6][1] == "Save")
    check("malformed stable ID is rejected without changing selection",
          replies[7][0] == 0 and replies[8][1] == "Save")

    with tempfile.TemporaryDirectory(prefix="elisa-ide host file mode ") as directory:
        request_path = os.path.join(directory, "requests.bin")
        reply_path = os.path.join(directory, "replies.bin")
        with open(request_path, "wb") as request_file:
            request_file.write(b"".join([
                req(11, text=fixture_path), req(81, text="node-save"), req(54),
            ]))
        file_process = subprocess.run([HOST, request_path, reply_path], capture_output=True,
                                       timeout=60)
        file_replies = []
        if file_process.returncode == 0 and os.path.isfile(reply_path):
            with open(reply_path, "rb") as reply_file:
                data = reply_file.read()
            position = 0
            while position + 8 <= len(data):
                result, length = struct.unpack_from("<ii", data, position)
                position += 8
                file_replies.append((result, data[position:position + length].decode("utf-8", errors="replace")))
                position += length
        check("binary file mode accepts a text-bearing stable-ID selection",
              file_process.returncode == 0 and len(file_replies) == 3
              and file_replies[0][0] == 1 and file_replies[1][0] == 1
              and file_replies[2][1] == "Save")

    process = start_signal_host()
    try:
        check("signal host opens a stable-ID selection fixture",
              signal_request(process, 11, text=fixture_path)[0] == 1)
        selected = signal_request(process, 81, text="node-save")
        check("signal opcode 81 selects and rebuilds the inspector",
              selected[0] == 1 and signal_request(process, 54)[1] == "Save"
              and signal_request(process, 52, 0)[1] == "text")
        check("signal host rejects an empty stable ID", signal_request(process, 81, text="")[0] == 0)
        check("signal host rejects invalid ID characters without changing selection",
              signal_request(process, 81, text="node_save")[0] == 0
              and signal_request(process, 54)[1] == "Save")
        check("signal host rejects a well-formed but missing ID without changing selection",
              signal_request(process, 81, text="node-missing")[0] == 0
              and signal_request(process, 54)[1] == "Save")
        check("signal parser rejects malformed UTF-8 ID payloads",
              signal_request(process, 81, raw_hex="c0af")[0] == -1)
    finally:
        stop_signal_host(process)

def check_sibling_reorder(check):
    replies = run([
        req(1), req(10),                 # hello, clean form
        req(30, 4),                      # Button child
        req(31, 0), req(30, 3),          # Label child, after the Button
        req(31, 1),                      # Select the Button
        req(51, 1), req(51, 2), req(55),
        req(17, 0), req(51, 1),          # Moving first child backward is a no-op
        req(17, 1), req(51, 1), req(51, 2), req(55),
        req(13), req(51, 1), req(51, 2), req(55),
        req(17, 0), req(51, 1),          # Undo restored Button to first; boundary remains a no-op
        req(2),
    ])
    button_type = replies[6][1]
    label_type = replies[7][1]
    selected_before = replies[8][1]
    check("reorder fixture contains distinct Button and Label siblings",
          button_type != label_type and selected_before == button_type)
    check("moving the first sibling backward is a no-op",
          replies[9][0] == 0 and replies[10][1] == button_type)
    check("moving a selected sibling forward swaps its hierarchy position",
          replies[11][0] == 1 and replies[12][1] == label_type and replies[13][1] == button_type)
    check("forward reorder preserves stable node selection",
          replies[14][1] == button_type)
    check("undo restores the original sibling order and selection",
          replies[15][0] == 1 and replies[16][1] == button_type
          and replies[17][1] == label_type and replies[18][1] == button_type)
    check("moving the restored first sibling backward is still a no-op",
          replies[19][0] == 0 and replies[20][1] == button_type)

def check_keyboard_reparent(check):
    replies = run([
        req(1), req(10),                # root column
        req(30, 1),                     # row sibling
        req(31, 0), req(30, 2),         # panel sibling
        req(31, 0), req(30, 4),         # button sibling after panel
        req(84), req(63),               # indent into previous panel
        req(85), req(63),               # outdent after panel
        req(13), req(63), req(2),       # undo outdent
    ])
    check("indent reparents a selected node into the previous sibling container",
          replies[7][0] == 1 and replies[8][1] == "Column / Panel / Button")
    check("outdent restores the selected node to the grandparent",
          replies[9][0] == 1 and replies[10][1] == "Column / Button")
    check("undo restores the pre-outdent parent",
          replies[11][0] == 1 and replies[12][1] == "Column / Panel / Button")

def check_drag_protocol(check):
    # Drag queries are deliberately split from the mutating operations so the
    # shell can paint a target before pointer release. The same command layer
    # validates the final insert/reparent transaction again on commit.
    replies = run([
        req(1), req(10),
        req(86, 4, 0),                 # Button can be inserted into root Column
        req(87, 1, 0),                 # Insert a Row through the drop path
        req(87, 4, 0),                 # Insert Button beside the Row at root
        req(20), req(31, 2),            # Select the Button child
        req(88, 1),                     # Reparent query accepts the Row target
        req(89, 1), req(63),            # Commit and inspect stable path
        req(88, 2),                     # A leaf target is rejected
        req(13), req(63), req(2),       # One undo restores the pre-drop graph
    ])
    check("drag insertion query accepts a container target", replies[2][0] == 1)
    check("drag insertion commits the requested row and button", replies[5][0] == 3)
    check("drag reparent query accepts the selected component and row", replies[8][0] == 1)
    check("drag reparent commits through one transaction", replies[8][0] == 1 and replies[9][1] == "Column / Row / Button")
    check("drag reparent rejects a leaf target", replies[10][0] == 0)
    check("undo restores the pre-drop hierarchy", replies[11][0] == 1 and replies[12][1] == "Column / Button")

def check_clipboard_commands(check):
    # Copy stores an owned form snapshot, so deleting the source must not make
    # a later paste depend on IDs or string-pool storage in the live document.
    replies = run([
        req(1), req(10),
        req(30, 4), req(120), req(12), req(23),
        req(31, 0), req(122), req(23), req(54),
        req(121), req(23), req(13), req(23), req(2),
    ])
    check("copy succeeds for a selected component", replies[3][0] == 1)
    check("cut source deletion leaves only the root", replies[5][0] == 1)
    check("paste restores a fresh component from the clipboard", replies[8][0] == 2 and replies[9][1] == "Button")
    check("cut removes the pasted component through a command", replies[11][0] == 1)
    check("undo restores the cut component", replies[13][0] == 2)

def check_wrap_unwrap_commands(check):
    replies = run([
        req(1), req(10),
        req(30, 4), req(31, 0), req(30, 3),
        req(31, 1), req(82, 2), req(123, 0),
        req(20), req(54), req(124), req(20), req(54),
        req(13), req(20), req(2),
    ])
    check("wrapping selected siblings creates one container", replies[8][0] == 4 and replies[9][1] == "Row")
    check("unwrapping preserves child count and selects the first child", replies[11][0] == 3 and replies[12][1] == "Button")
    check("undo restores the wrapper transaction", replies[14][0] == 4)

def check_container_conversion(check):
    replies = run([
        req(1), req(10), req(30, 1), req(31, 1),
        req(51, 1), req(125, 1), req(51, 1), req(13), req(51, 1), req(2),
    ])
    check("row-to-column conversion keeps the stable hierarchy row", replies[4][1] == "elisa.ui.row" and replies[6][1] == "elisa.ui.column")
    check("undo restores the original container orientation", replies[8][1] == "elisa.ui.row")

def check_ancestor_selection(check):
    replies = run([
        req(1), req(10), req(30, 1), req(31, 1), req(30, 4), req(31, 2),
        req(63), req(126), req(63), req(126), req(63), req(126), req(2),
    ])
    check("ancestor selection climbs from a child to its immediate parent", replies[7][0] == 1 and replies[8][1] == "Column / Row")
    check("repeated ancestor selection reaches the form root", replies[9][0] == 1 and replies[10][1] == "Column")
    check("ancestor selection stops at the root", replies[11][0] == 0)

def check_hierarchy_multi_selection(check):
    replies = run([
        req(1), req(10),
        req(30, 4), req(31, 0), req(30, 3),  # Two siblings under the root
        req(20), req(31, 1), req(82, 2), req(24), req(43, 1), req(43, 2),
        req(82, 99), req(24),                 # Invalid rows preserve selection
        req(82, 1), req(24), req(43, 1), req(43, 2),
        req(31, 1), req(24), req(43, 1), req(43, 2),
        req(82, 2), req(24), req(12), req(20), req(13), req(20), req(2),
    ])
    check("multi-selection fixture contains root and two siblings", replies[5][0] == 3)
    check("modifier toggle adds a second stable hierarchy selection",
          replies[8][0] == 2 and replies[9][0] == 1 and replies[10][0] == 1)
    check("invalid hierarchy row leaves multi-selection unchanged",
          replies[11][0] == 0 and replies[12][0] == 2)
    check("modifier toggle removes an already selected row",
          replies[13][0] == 1 and replies[14][0] == 1 and replies[15][0] == 0 and replies[16][0] == 1)
    check("plain hierarchy selection collapses the set to one row",
          replies[17][0] == 1 and replies[18][0] == 1 and replies[19][0] == 1 and replies[20][0] == 0)
    check("multi-root delete removes both selected siblings",
          replies[23][0] == 1 and replies[24][0] == 1)
    check("undo restores both deleted siblings", replies[26][0] == 3)

def check_hierarchy_range_selection(check):
    replies = run([
        req(1), req(10), req(30, 4), req(31, 0), req(30, 3),
        req(31, 1), req(83, 2), req(24), req(43, 0), req(43, 1), req(43, 2),
        req(83, 0), req(24), req(43, 0), req(43, 1), req(43, 2),  # Repeated range keeps its stable anchor
        req(83, -1), req(24),                 # Invalid rows preserve selection
        req(31, 2), req(83, 0), req(24), req(43, 0), req(43, 1), req(43, 2),
        req(31, 1), req(82, 1), req(24),      # Clear, then range falls back to one row
        req(83, 2), req(24), req(43, 1), req(43, 2), req(2),
    ])
    check("forward Shift-click selects the contiguous rows from the primary",
          replies[7][0] == 2 and replies[8][0] == 0 and replies[9][0] == 1 and replies[10][0] == 1)
    check("repeated Shift-click keeps the original stable anchor",
          replies[12][0] == 2 and replies[13][0] == 1 and replies[14][0] == 1 and replies[15][0] == 0)
    check("invalid range row leaves selection unchanged", replies[16][0] == 0 and replies[17][0] == 2)
    check("reverse Shift-click includes both endpoints and intervening rows",
          replies[20][0] == 3 and replies[21][0] == 1 and replies[22][0] == 1 and replies[23][0] == 1)
    check("range selection falls back to the clicked row when the set is empty",
          replies[28][0] == 1 and replies[29][0] == 0 and replies[30][0] == 1)

def check_selection_breadcrumb(check):
    replies = run([
        req(1), req(10), req(63),
        req(30, 1), req(63),
        req(30, 4), req(63),
        req(31, 1), req(63),
        req(31, 2), req(82, 2), req(24), req(63), req(2),
    ])
    check("root selection breadcrumb names the root", replies[2][1] == "Column")
    check("nested selection breadcrumb includes its parent", replies[4][1] == "Column / Row")
    check("deep selection breadcrumb is root-to-node", replies[6][1] == "Column / Row / Button")
    check("breadcrumb follows exclusive primary selection", replies[8][1] == "Column / Row")
    check("cleared selection has an explicit breadcrumb", replies[11][0] == 0 and replies[12][1] == "(no selection)")

def check_selection_ancestors(check):
    replies = run([
        req(1), req(10), req(30, 1), req(30, 4),
        req(64), req(65, 0), req(50, 0), req(65, 1), req(50, 1),
        req(65, 2), req(50, 2), req(65, 3),
        req(82, 2), req(64), req(65, 0), req(2),
    ])
    check("ancestor projection reports the primary path depth", replies[4][0] == 3)
    check("ancestor rows resolve root-to-primary by hierarchy identity",
          replies[5][0] == 0 and replies[6][1] == "Column"
          and replies[7][0] == 1 and replies[8][1] == "Row"
          and replies[9][0] == 2 and replies[10][1] == "Button")
    check("ancestor row rejects an index past the primary", replies[11][0] == -1)
    check("empty selection has no clickable ancestor rows", replies[13][0] == 0 and replies[14][0] == -1)

    replies = run([
        req(1), req(10), req(30, 0), req(30, 0), req(30, 0), req(30, 0),
        req(30, 0), req(30, 0), req(30, 0), req(64),
        req(65, 0), req(65, 2), req(65, 7), req(65, 8), req(2),
    ])
    check("deep ancestor projection supports condensed breadcrumbs", replies[9][0] == 8)
    check("deep ancestor indices retain global hierarchy rows",
          replies[10][0] == 0 and replies[11][0] == 2 and replies[12][0] == 7)
    check("deep ancestor projection rejects levels beyond the primary", replies[13][0] == -1)

def check_problem_details(check):
    fixture_path = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "fixtures",
                                                "over-limit-events.elisaform.json"))
    replies = run([
        req(1), req(11, text=fixture_path), req(25),
        req(96, 0), req(97, 0), req(98, 0), req(99, 0), req(100, 0), req(2),
    ])
    check("invalid open retains a bounded problem projection", replies[2][0] > 0)
    check("problem projection exposes code, message, and source path",
          replies[3][1] == "too-many-nodes"
          and replies[4][1] == "node exceeds the supported event limit"
          and replies[5][1] == "nodes[0].events"
          and replies[6][0] == 1)
    check("problem navigation ignores locations from a rejected document",
          replies[7][0] == -1)

def check_primitive_preview(check):
    process = start_signal_host()
    try:
        check("primitive fixture starts with a clean form", signal_request(process, 1)[0] == 1
              and signal_request(process, 10)[0] == 1)
        inserted_rows = [signal_request(process, 30, index)[0] for index in range(6, 11)]
        check("all advertised primitive components insert", all(row >= 0 for row in inserted_rows))
        check("primitive components expand the hierarchy", signal_request(process, 23)[0] == 6)
        check("checkbox selection property edits through the typed inspector",
              signal_request(process, 31, 1)[0] == 1
              and signal_request(process, 32, 8)[0] == 1)
        result, payload = signal_request(process, 80, binary=True)
        kinds = []
        selected = False
        if result == 1 and len(payload) >= 28:
            node_count = struct.unpack_from("<H", payload, 24)[0]
            cursor = 28
            for _ in range(node_count):
                id_length, kind, parent_index, flags, axis, cross, main, reserved = struct.unpack_from("<BBHHBBBB", payload, cursor)
                cursor += 10 + id_length + 28 + 20
                for _field in range(4):
                    field_length = struct.unpack_from("<H", payload, cursor)[0]
                    cursor += 2 + field_length
                kinds.append(kind)
                selected = selected or (kind == 7 and flags & 8 != 0)
        check("preview encodes every primitive kind and selection state",
              result == 1 and sorted(kinds) == [1, 7, 8, 9, 10, 11] and selected)
    finally:
        stop_signal_host(process)

def main():
    failures = []
    def check(name, condition):
        if not condition:
            failures.append(name)
            print("FAIL:", name)

    replies = run([req(1), req(2)])
    check("hello answers", replies[0][0] == 1)

    replies = run([
        req(1), req(10),            # hello, new
        req(21),                    # palette count
        req(30, 4),                 # insert palette item 4 (button)
        req(20), req(23),           # hierarchy, node count
        req(31, 1), req(54),        # select row 1, selection name
        req(52, 0),                 # inspector label row 0
        req(70, 0, 0, "Hello there"),
        req(53, 0),                 # inspector value
        req(13),                    # undo
        req(53, 0),                 # inspector value after undo
        req(40),                    # dirty
        req(2),
    ])
    palette = replies[2][0]
    inserted_row = replies[3][0]
    hierarchy = replies[4][0]
    nodes = replies[5][0]
    selected = replies[7][1]
    label = replies[8][1]
    edited = replies[10][1]
    after_undo = replies[12][1]
    dirty = replies[13][0]

    check("palette lists every built-in component", palette == 11)
    check("insert reports a row", inserted_row >= 0)
    check("insert adds to the hierarchy", hierarchy == 2)
    check("insert adds a node", nodes == 2)
    check("selection names the button", selected == "Button")
    check("inspector shows the caption property", label == "text")
    check("caption edit applies", edited == "Hello there")
    check("undo restores the caption", after_undo == "Button")
    check("undo leaves the document dirty", dirty == 1)

    check_stable_id_selection(check)
    check_sibling_reorder(check)
    check_keyboard_reparent(check)
    check_drag_protocol(check)
    check_clipboard_commands(check)
    check_wrap_unwrap_commands(check)
    check_container_conversion(check)
    check_ancestor_selection(check)
    check_hierarchy_multi_selection(check)
    check_hierarchy_range_selection(check)
    check_selection_breadcrumb(check)
    check_selection_ancestors(check)
    check_problem_details(check)
    check_primitive_preview(check)

    # The automatically created untitled form is a clean baseline. New/Open
    # must be available when the shell starts, while the first edit must be
    # protected and undoing it must return to that baseline.
    replies = run([
        req(1), req(10), req(40),       # cold-start form is clean
        req(30, 4), req(40),            # first insertion makes it dirty
        req(13), req(40),               # undo returns to the clean baseline
        req(14), req(40),               # redo restores the dirty state
        req(2),
    ])
    check("fresh untitled form is initially clean", replies[2][0] == 0)
    check("inserting the first component makes it dirty", replies[4][0] == 1)
    check("undoing the first component restores clean state", replies[6][0] == 0)
    check("redoing the first component restores dirty state", replies[8][0] == 1)

    replies = run([
        req(1), req(10), req(30, 5), req(30, 4), req(23),
        req(31, 1), req(62, 0), req(12), req(23), req(13), req(23),
        req(2),
    ])
    before = replies[4][0]
    tag = replies[6][0]
    after_delete = replies[8][0]
    after_undo = replies[10][0]
    check("binary stream reports a typed text property", tag == 3)
    check("delete removes the selected subtree", after_delete < before)
    check("undo restores the deleted node", after_undo == before)

    check_signal_lifecycle(check)
    check_project_creation(check)
    check_recovery(check)
    check_external_changes(check)
    check_source_conflicts(check)

    if failures:
        print("%d failure(s)" % len(failures))
        return 1
    print("ok document_host protocol")
    return 0

if __name__ == "__main__":
    sys.exit(main())
