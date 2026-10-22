"""Regression tests for the live board bridge's track ops (delete_track_at, set_track_width).

Talks the same one-JSON-line-per-request protocol as skidl_mcp_server._live_call, straight to
the app's tool server (ANVIL_MCP_PORT, default 5571). Needs the Anvil CAD app RUNNING with a
board open in the PCB editor and the tool server started -- the whole point is to test the
LIVE handler, so there is no mock. Skips cleanly when the app is not reachable.

Every edit the test makes is undoable in the app and the test deletes everything it adds, in a
far-off corner of the sheet, so the open board ends unchanged.

Covers the regression where delete_track_at only hit-tested segment ENDPOINTS, so deleting at
the midpoint of a freshly added track failed with "No track or via found":
  - add_track then delete_track_at at the midpoint (the original bug)
  - delete_track_at at an endpoint, with and without a layer, case-insensitively
  - wrong-layer and far-away misses report the nearest candidate instead of a bare "not found"
  - set_track_width round-trip, verified by reading the board back (include_tracks)

Run standalone:  python tests/test_live_track_ops.py
Or via pytest:   pytest tests/test_live_track_ops.py -v
"""

import json
import os
import socket

HOST = "127.0.0.1"
PORT = int(os.environ.get("ANVIL_MCP_PORT", "5571"))

# A corner no real layout uses, so hit tests can't touch the user's own routing.
X0, Y0 = -8000, -6000          # mils; track runs horizontally from (X0, Y0)
X1 = X0 + 1000                 # 1000 mils long => midpoint is 500 mils from each end,
MIDX = (X0 + X1) // 2          # far outside the default 20 mil endpoint radius
WIDTH = 10                     # mils
NEW_WIDTH = 25                 # mils


def call(tool: str, **input_) -> dict:
    with socket.create_connection((HOST, PORT), timeout=30.0) as s:
        s.sendall((json.dumps({"tool": tool, "input": input_}) + "\n").encode("utf-8"))
        s.settimeout(30.0)
        buf = b""
        while not buf.endswith(b"\n"):
            chunk = s.recv(65536)
            if not chunk:
                break
            buf += chunk
    return json.loads(buf.decode("utf-8", "replace").strip() or "{}")


def server_reachable() -> bool:
    try:
        return bool(call("get_board").get("ok"))
    except OSError:
        return False


def track_count() -> int:
    r = call("get_board")
    assert r.get("ok"), r
    return r["track_count"]


def add_test_track(width=WIDTH):
    r = call("add_track", points=[[X0, Y0], [X1, Y0]], layer="F.Cu", width_mils=width)
    assert r.get("ok"), f"add_track failed: {r}"


def find_test_track(tracks):
    """The dumped segment matching our endpoints, in either direction."""
    want = {(X0, Y0, X1, Y0), (X1, Y0, X0, Y0)}
    for t in tracks:
        if t.get("type") != "track":
            continue
        key = (t.get("start_x_mils"), t.get("start_y_mils"),
               t.get("end_x_mils"), t.get("end_y_mils"))
        if key in want:
            return t
    return None


def test_live_track_ops():
    if not server_reachable():
        try:
            import pytest
            pytest.skip(f"Anvil CAD tool server not reachable on {HOST}:{PORT}")
        except ImportError:
            print(f"SKIP: Anvil CAD tool server not reachable on {HOST}:{PORT}")
            return

    baseline = track_count()

    # --- 1. the original bug: delete at the MIDPOINT of a track added in-session ---
    add_test_track()
    assert track_count() == baseline + 1
    r = call("delete_track_at", x_mils=MIDX, y_mils=Y0)
    assert r.get("ok"), f"midpoint delete (no layer) failed: {r}"
    assert track_count() == baseline

    # --- 2. delete at an ENDPOINT, layer given case-insensitively ---
    add_test_track()
    r = call("delete_track_at", x_mils=X0, y_mils=Y0, layer="f.cu")
    assert r.get("ok"), f"endpoint delete (layer 'f.cu') failed: {r}"
    assert track_count() == baseline

    # --- 3. wrong layer misses, and the error names the nearest candidate ---
    add_test_track()
    r = call("delete_track_at", x_mils=MIDX, y_mils=Y0, layer="B.Cu")
    assert not r.get("ok"), f"delete on the wrong layer should miss: {r}"
    msg = r.get("message", "")
    assert "B.Cu" in msg, f"miss should name the searched layer: {msg}"

    # a far-away point misses too, but reports how far the nearest track is
    r = call("delete_track_at", x_mils=MIDX, y_mils=Y0 - 500)
    assert not r.get("ok"), f"far-away delete should miss: {r}"
    assert "Nearest" in r.get("message", ""), f"miss should report the nearest candidate: {r}"

    # an unknown layer name is an explicit error, not a silent all-layers search
    r = call("delete_track_at", x_mils=MIDX, y_mils=Y0, layer="No.Such.Layer")
    assert not r.get("ok") and "Unknown layer" in r.get("message", ""), r

    # --- 4. set_track_width round-trip, verified by reading the board back ---
    r = call("set_track_width", x_mils=MIDX, y_mils=Y0, layer="F.Cu", width_mils=NEW_WIDTH)
    assert r.get("ok"), f"set_track_width failed: {r}"

    board = call("get_board", include_tracks=True)
    assert board.get("ok"), board
    seg = find_test_track(board.get("tracks", []))
    assert seg is not None, "test track missing from the include_tracks dump"
    assert seg["width_mils"] == NEW_WIDTH, f"width did not round-trip: {seg}"
    assert seg["layer"] == "F.Cu", seg

    # width_mils is required
    r = call("set_track_width", x_mils=MIDX, y_mils=Y0)
    assert not r.get("ok") and "width_mils" in r.get("message", ""), r

    # --- 5. cleanup: the widened track still deletes at its midpoint ---
    r = call("delete_track_at", x_mils=MIDX, y_mils=Y0, layer="F.Cu")
    assert r.get("ok"), f"cleanup delete failed: {r}"
    assert track_count() == baseline, "board track count changed after cleanup"

    print("OK: all live track-op regression checks passed")


if __name__ == "__main__":
    test_live_track_ops()
