"""Unit tests for the fail-closed schematic connectivity verifier."""

import subprocess


def test_verify_rejects_failed_kicad_export(tmp_path, monkeypatch):
    """A stale export must not let a failed CLI run approve a new schematic."""
    from skidl.anvil import verify_connectivity as vc

    monkeypatch.chdir(tmp_path)
    cli = tmp_path / "kicad-cli.exe"
    cli.touch()
    monkeypatch.setattr(vc, "KICAD_CLI", str(cli))

    (tmp_path / "board.kicad_sch").write_text("(kicad_sch)", encoding="utf-8")
    (tmp_path / "board.net").write_text("(export (nets))", encoding="utf-8")
    stale = tmp_path / "board_fromsch.net"
    stale.write_text("stale", encoding="utf-8")

    monkeypatch.setattr(
        vc.subprocess,
        "run",
        lambda *args, **kwargs: subprocess.CompletedProcess(args[0], returncode=1),
    )

    ok, message, unwanted, missing = vc.verify("board")

    assert not ok
    assert "could not export" in message
    assert unwanted == []
    assert missing == []
    assert not stale.exists()
