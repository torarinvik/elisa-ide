#!/usr/bin/env python3
"""Protocol-level test for the document host (M03 core acceptance).

Drives the host over its binary protocol and checks the make-a-form / insert /
select / edit-caption / undo / delete flows used by the editor shell. Runs
from `scripts/run_tests.sh`.
"""
import json, os, shutil, struct, subprocess, sys, tempfile, time

HOST = os.path.join(os.path.dirname(__file__), "..", "..", "build", "document_host")

def req(op, a0=0, a1=0, text=None):
    payload = struct.pack("<iii", op, a0, a1)
    if text is not None:
        encoded = text.encode()
        payload += struct.pack("<i", len(encoded)) + encoded
    return payload

def run(requests):
    process = subprocess.run([HOST], input=b"".join(requests), capture_output=True, timeout=60)
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

def start_signal_host():
    return subprocess.Popen([HOST, "--signal"], stdin=subprocess.PIPE,
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE)

def signal_request(process, op, a0=0, a1=0, text=None, raw_hex=None, binary=False):
    line = "%d %d %d" % (op, a0, a1)
    if text is not None:
        line += " " + text.encode("utf-8").hex()
    elif raw_hex is not None:
        line += " " + raw_hex
    line += "\n"
    request_path = os.path.join("build", "host_request.txt")
    reply_path = os.path.join("build", "host_reply.bin")
    os.makedirs("build", exist_ok=True)
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
        with open(os.path.join(os.path.dirname(__file__), "..", "fixtures",
                               "settings-project.elisaproject.json"), encoding="utf-8") as source:
            project = json.load(source)
        project["forms"][0]["path"] = "forms/main.elisaform.json"
        project_path = os.path.join(project_dir, "Résumé project.elisaproject.json")
        with open(project_path, "w", encoding="utf-8") as target:
            json.dump(project, target, ensure_ascii=False)

        save_path = os.path.join(directory, "forms with spaces", "Räksmörgås 東京.elisaform.json")
        os.makedirs(os.path.dirname(save_path))
        process = start_signal_host()
        try:
            check("signal hello", signal_request(process, 1)[0] == 1)
            check("project open accepts a Unicode path",
                  signal_request(process, 11, text=project_path)[0] == 1)
            project_nodes = signal_request(process, 23)[0]
            check("project open loads the entry form", project_nodes == len(form["nodes"]))
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
                  signal_request(process, 31, 6)[0] == 1
                  and signal_request(process, 52, 0)[1] == "text"
                  and signal_request(process, 53, 0)[1] == "Button"
                  and signal_request(process, 62, 0)[0] == 3)
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

    check("palette lists the first components", palette == 6)
    check("insert reports a row", inserted_row >= 0)
    check("insert adds to the hierarchy", hierarchy == 2)
    check("insert adds a node", nodes == 2)
    check("selection names the button", selected == "Button")
    check("inspector shows the caption property", label == "text")
    check("caption edit applies", edited == "Hello there")
    check("undo restores the caption", after_undo == "Button")
    check("undo leaves the document dirty", dirty == 1)

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

    if failures:
        print("%d failure(s)" % len(failures))
        return 1
    print("ok document_host protocol")
    return 0

if __name__ == "__main__":
    sys.exit(main())
