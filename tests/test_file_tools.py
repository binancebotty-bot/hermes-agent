"""W3 approved file tools: confinement, honest semantics, atomicity, registry safety.

All file I/O here is inside pytest temp dirs.  The real C:\\Users\\wigmore root is never
written to by these tests; the production default is checked only by signature/doc.
"""

from __future__ import annotations

import asyncio
import ctypes
import hashlib
import inspect
import json
import os
import subprocess
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from mcp_serve_plugin import bridge

try:
    from mcp_serve_plugin import file_tools
except ImportError as _exc:  # RED state: module not written yet
    file_tools = None
    _IMPORT_ERROR = _exc
else:
    _IMPORT_ERROR = None


def _ft():
    if file_tools is None:
        pytest.fail(f"file_tools module is missing: {_IMPORT_ERROR}")
    return file_tools


def _handlers(root, **kwargs):
    return _ft().FileTools(root=root, **kwargs)


def _call(handler, **kwargs):
    return json.loads(handler(**kwargs))


@pytest.fixture
def sandbox(tmp_path):
    root = tmp_path / "root"
    root.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    return root, outside


def _short_path(path: Path) -> str:
    if os.name != "nt":
        pytest.skip("Windows short-name probe is NT-only")
    buf = ctypes.create_unicode_buffer(1024)
    written = ctypes.windll.kernel32.GetShortPathNameW(str(path), buf, 1024)
    return buf.value if written else str(path)


def _link_outside(link: Path, target: Path) -> None:
    """Create a reparse point link->target without requiring admin (junction fallback)."""
    try:
        link.symlink_to(target, target_is_directory=True)
        return
    except OSError:
        pass
    result = subprocess.run(["cmd", "/c", "mklink", "/J", str(link), str(target)],
                            capture_output=True, text=True)
    if result.returncode != 0:
        pytest.skip(f"cannot create symlink/junction on this host: {result.stderr.strip()}")


# --- root confinement -------------------------------------------------------

def test_escape_via_dotdot_is_refused(sandbox):
    root, outside = sandbox
    (outside / "secret.txt").write_text("secret", encoding="utf-8")
    h = _handlers(root)
    attack = str(root / "sub" / ".." / ".." / "outside" / "secret.txt")
    result = _call(h.file_read, path=attack)
    assert result["status"] == "REFUSED" and result["reason"] == "ROOT_ESCAPE"


def test_escape_via_symlink_is_refused(sandbox):
    root, outside = sandbox
    (outside / "secret.txt").write_text("secret", encoding="utf-8")
    _link_outside(root / "linked", outside)
    h = _handlers(root)
    result = _call(h.file_read, path=str(root / "linked" / "secret.txt"))
    assert result["status"] == "REFUSED" and result["reason"] == "ROOT_ESCAPE"


def test_hard_link_to_outside_file_is_refused(sandbox):
    """A hard link inside root shares the inode of an outside file; resolve() cannot see it."""
    root, outside = sandbox
    victim = outside / "outside.txt"
    victim.write_text("OUTSIDE-CONTENT", encoding="utf-8")
    link = root / "inside_link.txt"
    try:
        os.link(victim, link)
    except (OSError, NotImplementedError) as exc:
        pytest.skip(f"hard links unavailable on this host: {exc}")
    result = _call(_handlers(root).file_read, path=str(link))
    assert result["status"] == "REFUSED" and result["reason"] == "HARD_LINK_REFUSED"


def test_absolute_path_outside_root_is_refused(sandbox):
    root, outside = sandbox
    victim = outside / "victim.txt"
    victim.write_text("outside", encoding="utf-8")
    h = _handlers(root)
    result = _call(h.file_read, path=str(victim))
    assert result["status"] == "REFUSED" and result["reason"] == "ROOT_ESCAPE"


def test_windows_short_name_escape_is_refused(sandbox):
    root, outside = sandbox
    victim = outside / "secret.txt"
    victim.write_text("secret", encoding="utf-8")
    h = _handlers(root)
    # 8.3 alias of the outside file, plus an 8.3 alias of root followed by .. .
    for attack in (_short_path(victim), str(Path(_short_path(root)) / ".." / "outside" / "secret.txt")):
        result = _call(h.file_read, path=attack)
        assert result["status"] == "REFUSED" and result["reason"] == "ROOT_ESCAPE", attack


def test_relative_path_is_refused_because_path_must_be_absolute(sandbox):
    root, _ = sandbox
    h = _handlers(root)
    result = _call(h.file_read, path="inside.txt")
    assert result["status"] == "REFUSED" and result["reason"] == "PATH_NOT_ABSOLUTE"


def test_inside_root_is_allowed(sandbox):
    root, _ = sandbox
    (root / "ok.txt").write_text("hello", encoding="utf-8")
    h = _handlers(root)
    assert _call(h.file_read, path=str(root / "ok.txt"))["content"] == "hello"


# --- file_read honesty ------------------------------------------------------

def test_read_truncation_is_truthful(sandbox):
    root, _ = sandbox
    target = root / "big.txt"
    target.write_bytes(b"0123456789" * 10)  # 100 bytes
    h = _handlers(root)
    result = _call(h.file_read, path=str(target), max_bytes=10)
    assert result["status"] == "OK"
    assert result["bytes"] == 10 and result["total_bytes"] == 100
    assert result["truncated"] is True
    assert result["content"].startswith("0123456789")
    assert "10 of 100 bytes" in result["content"]
    assert "90 bytes omitted" in result["content"]


def test_read_without_truncation_has_no_marker(sandbox):
    root, _ = sandbox
    target = root / "small.txt"
    target.write_text("all", encoding="utf-8")
    result = _call(_handlers(root).file_read, path=str(target), max_bytes=100)
    assert result["status"] == "OK" and result["truncated"] is False
    assert "TRUNCATED" not in result["content"]


def test_binary_file_refused_as_text(sandbox):
    root, _ = sandbox
    target = root / "blob.bin"
    target.write_bytes(b"\x00\x01\xff\xfePK\x03\x04")
    result = _call(_handlers(root).file_read, path=str(target))
    assert result["status"] == "REFUSED" and result["reason"] == "BINARY_FILE"


def test_partial_utf8_bytes_are_not_text():
    """A truncated multibyte sequence must not decode to an empty string."""
    assert _ft()._decode_text(b"\xe2\x82") is None


def test_partial_utf8_read_is_refused_not_empty_ok(sandbox):
    root, _ = sandbox
    target = root / "partial.txt"
    target.write_bytes(b"\xe2\x82")  # leading two bytes of a three-byte UTF-8 character
    result = _call(_handlers(root).file_read, path=str(target))
    assert result["status"] == "REFUSED" and result["reason"] == "BINARY_FILE"


def test_truncation_boundary_never_silently_drops_read_bytes(sandbox):
    root, _ = sandbox
    target = root / "unicode.txt"
    target.write_bytes("A€B".encode("utf-8"))
    result = _call(_handlers(root).file_read, path=str(target), max_bytes=2)
    assert result["status"] == "REFUSED" and result["reason"] == "BINARY_FILE"


def test_read_missing_file_reports_error(sandbox):
    root, _ = sandbox
    result = _call(_handlers(root).file_read, path=str(root / "absent.txt"))
    assert result["status"] == "ERROR" and result["reason"] == "NOT_FOUND"


def test_read_unreadable_target_reports_error(sandbox):
    root, _ = sandbox
    adir = root / "a_directory"
    adir.mkdir()
    result = _call(_handlers(root).file_read, path=str(adir))
    assert result["status"] == "ERROR" and result["reason"] == "UNREADABLE"


# --- file_write honesty -----------------------------------------------------

def test_write_lands_and_confirmation_is_derived_from_disk(sandbox, monkeypatch):
    root, _ = sandbox
    target = root / "note.txt"
    real_replace = os.replace

    def altering_replace(src, dst):
        Path(src).write_bytes(b"DISK-ACTUAL")  # contents at commit time differ from the request
        return real_replace(src, dst)

    monkeypatch.setattr(file_tools.os, "replace", altering_replace)
    result = _call(_handlers(root).file_write, path=str(target), content="REQUESTED")
    assert result["status"] == "LANDED"
    assert target.read_bytes() == b"DISK-ACTUAL"
    assert result["bytes"] == len(b"DISK-ACTUAL")
    assert result["sha256"] == hashlib.sha256(b"DISK-ACTUAL").hexdigest()
    assert result["sha256"] != hashlib.sha256(b"REQUESTED").hexdigest()


def test_write_is_atomic_when_commit_fails(sandbox, monkeypatch):
    root, _ = sandbox
    target = root / "note.txt"
    target.write_text("OLD", encoding="utf-8")

    def crash_replace(src, dst):
        raise OSError("simulated crash before rename")

    monkeypatch.setattr(file_tools.os, "replace", crash_replace)
    result = _call(_handlers(root).file_write, path=str(target), content="NEW")
    assert result["status"] == "NOT_WRITTEN" and result["reason"] == "COMMIT_FAILED"
    assert target.read_text(encoding="utf-8") == "OLD"
    assert not list(root.glob("*.tmp*")), "atomic write left a temp file behind"


def test_fdopen_failure_closes_descriptor_and_removes_temp(sandbox, monkeypatch):
    root, _ = sandbox
    target = root / "note.txt"
    seen = []

    def failing_fdopen(fd, *args, **kwargs):
        seen.append(fd)
        raise OSError("simulated fdopen failure")

    monkeypatch.setattr(file_tools.os, "fdopen", failing_fdopen)
    result = _call(_handlers(root).file_write, path=str(target), content="LEAK-PROBE")
    assert result["status"] == "NOT_WRITTEN" and result["reason"] == "COMMIT_FAILED"
    assert seen, "fdopen was never attempted"
    assert not list(root.glob("w3-*.tmp")), "fdopen failure left a temp file behind"
    with pytest.raises(OSError):
        os.fstat(seen[0])


def test_replace_failure_does_not_leave_temp_after_unlink_sharing_violation(sandbox, monkeypatch):
    root, _ = sandbox
    target = root / "note.txt"
    target.write_text("OLD", encoding="utf-8")
    real_unlink = os.unlink
    calls = []

    def flaky_unlink(path):
        calls.append(path)
        if len(calls) == 1:
            raise OSError("simulated Windows sharing violation on unlink")
        return real_unlink(path)

    def crash_replace(src, dst):
        raise OSError("simulated Windows sharing violation on replace")

    monkeypatch.setattr(file_tools.os, "replace", crash_replace)
    monkeypatch.setattr(file_tools.os, "unlink", flaky_unlink)
    result = _call(_handlers(root).file_write, path=str(target), content="LEAK-PROBE")
    assert result["status"] == "NOT_WRITTEN" and result["reason"] == "COMMIT_FAILED"
    assert not list(root.glob("w3-*.tmp")), "replace failure left a temp file behind"
    assert target.read_text(encoding="utf-8") == "OLD"


def test_write_create_parents_only_when_asked(sandbox):
    root, _ = sandbox
    target = root / "a" / "b" / "note.txt"
    h = _handlers(root)
    result = _call(h.file_write, path=str(target), content="x")
    assert result["status"] == "NOT_WRITTEN" and result["reason"] == "PARENT_NOT_FOUND"
    assert not target.exists()
    result = _call(h.file_write, path=str(target), content="x", create_parents=True)
    assert result["status"] == "LANDED" and target.read_text(encoding="utf-8") == "x"


def test_two_concurrent_writers_do_not_corrupt(sandbox):
    root, _ = sandbox
    target = root / "race.txt"
    size = 200_000
    a, b = b"A" * size, b"B" * size
    barrier = threading.Barrier(2)
    h1, h2 = _handlers(root), _handlers(root)

    def write(handler, content):
        barrier.wait()
        return _call(handler.file_write, path=str(target), content=content.decode("ascii"))

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda pair: write(*pair), [(h1, a), (h2, b)]))
    assert all(r["status"] == "LANDED" for r in results)
    final = target.read_bytes()
    assert final in (a, b), "file is interleaved/partially written"
    assert not list(root.glob("*.tmp*"))


# --- file_edit honesty ------------------------------------------------------

def test_edit_absent_match_is_not_written(sandbox):
    root, _ = sandbox
    target = root / "edit.txt"
    target.write_text("one two", encoding="utf-8")
    result = _call(_handlers(root).file_edit, path=str(target), old="missing", new="x")
    assert result["status"] == "NOT_WRITTEN" and result["reason"] == "MATCH_NOT_FOUND"
    assert target.read_text(encoding="utf-8") == "one two"


def test_edit_ambiguous_match_is_not_written(sandbox):
    root, _ = sandbox
    target = root / "edit.txt"
    target.write_text("dup dup", encoding="utf-8")
    result = _call(_handlers(root).file_edit, path=str(target), old="dup", new="x")
    assert result["status"] == "NOT_WRITTEN" and result["reason"] == "AMBIGUOUS_MATCH"
    assert result["count"] == 2
    assert target.read_text(encoding="utf-8") == "dup dup"


def test_edit_single_match_replaces_and_lands(sandbox):
    root, _ = sandbox
    target = root / "edit.txt"
    target.write_text("alpha beta alpha", encoding="utf-8")
    result = _call(_handlers(root).file_edit, path=str(target), old="beta", new="BETA")
    assert result["status"] == "LANDED"
    assert target.read_text(encoding="utf-8") == "alpha BETA alpha"
    assert result["sha256"] == hashlib.sha256(target.read_bytes()).hexdigest()


def test_edit_empty_old_is_not_written(sandbox):
    root, _ = sandbox
    target = root / "edit.txt"
    target.write_text("abc", encoding="utf-8")
    result = _call(_handlers(root).file_edit, path=str(target), old="", new="x")
    assert result["status"] == "NOT_WRITTEN" and result["reason"] == "EMPTY_OLD"
    assert target.read_text(encoding="utf-8") == "abc"


# --- credential deny list (default ON; deny_credentials=False is the full-reach opt-in) ---

def test_deny_list_is_on_by_default(sandbox):
    root, _ = sandbox
    target = root / "secrets" / "mcp_coordinator_token.txt"
    target.parent.mkdir()
    target.write_text("placeholder-not-a-real-token", encoding="utf-8")
    result = _call(_handlers(root).file_read, path=str(target))
    assert result["status"] == "REFUSED" and result["reason"] == "CREDENTIAL_DENIED"
    params = inspect.signature(_ft().register_file_tools).parameters
    assert params["deny_credentials"].default is True


def test_explicit_opt_in_restores_full_approved_reach(sandbox):
    root, _ = sandbox
    target = root / "secrets" / "token.txt"
    target.parent.mkdir()
    target.write_text("placeholder", encoding="utf-8")
    result = _call(_handlers(root, deny_credentials=False).file_read, path=str(target))
    assert result["status"] == "OK" and result["content"] == "placeholder"


@pytest.mark.parametrize("relative", [
    ".ssh/id_ed25519", ".aws/credentials", ".docker/config.json",
    ".config/gh/hosts.yml", ".config/tailscale/tailscaled.log",
    ".netrc", ".npmrc", ".hermes/secrets/coordinator_token.txt",
    "Browser/Login Data", "Editor/state.vscdb", "keys/private_key.pem",
    "backup/id_rsa", "vault/keychain.db",
])
def test_default_deny_covers_credential_materials(sandbox, relative):
    root, _ = sandbox
    target = root.joinpath(*relative.split("/"))
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("placeholder", encoding="utf-8")
    result = _call(_handlers(root).file_read, path=str(target))
    assert result["status"] == "REFUSED" and result["reason"] == "CREDENTIAL_DENIED"


def test_explicit_opt_in_still_narrows_when_requested(sandbox):
    root, _ = sandbox
    target = root / ".ssh" / "id_ed25519"
    target.parent.mkdir()
    target.write_text("key", encoding="utf-8")
    h = _handlers(root, deny_credentials=True)
    result = _call(h.file_read, path=str(target))
    assert result["status"] == "REFUSED" and result["reason"] == "CREDENTIAL_DENIED"


# --- registry safety --------------------------------------------------------

def test_file_tool_names_are_exactly_the_three_approved_capabilities():
    names = tuple(_ft().FILE_TOOL_NAMES)
    assert names == ("file_read", "file_write", "file_edit")
    assert not set(names) & {"terminal", "process", "execute_code", "shell", "run_command"}


def test_registry_has_no_terminal_or_shell_tool():
    server = bridge.create_mcp_server(event_bridge=bridge.EventBridge(), include_files=True)
    names = {t.name for t in asyncio.run(server.list_tools())}
    assert set(_ft().FILE_TOOL_NAMES) <= names
    assert not names & {"terminal", "shell", "process", "execute_code", "run_command", "bash"}


def test_files_are_opt_in_and_default_surface_is_unchanged():
    plain = bridge.create_mcp_server(event_bridge=bridge.EventBridge())
    with_files = bridge.create_mcp_server(event_bridge=bridge.EventBridge(), include_files=True)
    a = {t.name for t in asyncio.run(plain.list_tools())}
    b = {t.name for t in asyncio.run(with_files.list_tools())}
    assert not set(_ft().FILE_TOOL_NAMES) & a
    assert b - a == set(_ft().FILE_TOOL_NAMES)
