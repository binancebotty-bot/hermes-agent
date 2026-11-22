"""W3d: normalise path components before credential matching (third review).

Ground truth is the staged matcher: a trailing dot/space and a non-equivalent
homoglyph both produced ``denied=False``.  Every assertion below is on the
RESULT a caller sees (the JSON response dict), not on an internal helper.

No real credential is used: all payloads are placeholders and all I/O is in
pytest temp dirs.
"""

from __future__ import annotations

import json
import os
import unicodedata

import pytest

try:
    from mcp_serve_plugin import file_tools
except ImportError as _exc:  # RED state: module not importable
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
    return root


def _write_secret_stub(root: Path, component: str):
    """Create <root>/<component>/key.txt with a placeholder (never a real secret)."""
    directory = root / component
    directory.mkdir()
    target = directory / "key.txt"
    target.write_text("PLACEHOLDER-NOT-A-REAL-CREDENTIAL", encoding="utf-8")
    return target


from pathlib import Path  # noqa: E402  (kept beside the helper that uses it)


# --- RED: trailing dot / trailing space (Windows name resolution) -----------

@pytest.mark.parametrize("component", ["secrets.", "secrets "])
def test_windows_trailing_dot_or_space_dir_is_refused_on_write(sandbox, component):
    """A new dir named 'secrets.'/'secrets ' becomes 'secrets' on Windows.

    create_parents=True means resolve() cannot canonicalise the not-yet-existing
    component, so the raw name reaches the matcher.  The caller must get
    CREDENTIAL_DENIED, never LANDED (dot) or a raw I/O error (space).
    """
    root = sandbox
    raw = os.path.join(str(root), component, "mcp_coordinator_token.txt")
    result = _call(_handlers(root).file_write, path=raw,
                   content="PLACEHOLDER-NOT-A-REAL-CREDENTIAL", create_parents=True)
    assert result["status"] == "REFUSED" and result["reason"] == "CREDENTIAL_DENIED", result


def test_windows_trailing_dot_dir_is_refused_on_read_when_present(sandbox):
    """With the real dir already on disk, resolve() strips the dot -- still denied."""
    root = sandbox
    _write_secret_stub(root, "secrets")
    raw = os.path.join(str(root), "secrets.", "key.txt")
    result = _call(_handlers(root).file_read, path=raw)
    assert result["status"] == "REFUSED" and result["reason"] == "CREDENTIAL_DENIED", result


# --- RED: Unicode normalisation --------------------------------------------

def test_fullwidth_confusable_dir_is_refused(sandbox):
    """NFKC maps fullwidth Latin to ASCII before matching: fullwidth 'secrets' is denied."""
    root = sandbox
    _write_secret_stub(root, "ｓｅｃｒｅｔｓ")
    result = _call(_handlers(root).file_read, path=str(root / "ｓｅｃｒｅｔｓ" / "key.txt"))
    assert result["status"] == "REFUSED" and result["reason"] == "CREDENTIAL_DENIED", result


def test_zero_width_character_in_dir_is_refused(sandbox):
    """Format characters are stripped before matching, so they cannot split a denied name."""
    root = sandbox
    _write_secret_stub(root, "se\u200bcrets")
    result = _call(_handlers(root).file_read, path=str(root / "se\u200bcrets" / "key.txt"))
    assert result["status"] == "REFUSED" and result["reason"] == "CREDENTIAL_DENIED", result


# --- RED: non-equivalent homoglyph (mixed-script refusal control) ----------

def test_mixed_script_confusable_dir_is_refused(sandbox):
    """'ѕecrets' = Cyrillic dze (U+0455) + Latin 'ecrets': forbidden mixed script.

    NFKC does NOT map U+0455 to 's', so normalisation alone cannot catch this.
    The caller must still see a refusal (via the mixed-script control), not OK.
    """
    root = sandbox
    _write_secret_stub(root, "\u0455ecrets")
    result = _call(_handlers(root).file_read, path=str(root / "\u0455ecrets" / "key.txt"))
    assert result["status"] == "REFUSED" and result["reason"] == "CREDENTIAL_DENIED", result


def test_all_cyrillic_ordinary_project_name_is_not_falsely_refused(sandbox):
    """The mixed-script guard must not reject a single-script non-Latin name."""
    root = sandbox
    target = root / "\u041f\u0440\u043e\u0435\u043a\u0442\u044b" / "main.py"  # Проекты
    target.parent.mkdir()
    target.write_text("ORDINARY-CONTENT", encoding="utf-8")
    result = _call(_handlers(root).file_read, path=str(target))
    assert result["status"] == "OK" and result["content"] == "ORDINARY-CONTENT", result


def test_nfkc_alone_does_not_map_non_equivalent_homoglyph():
    """Documented limitation motivating the mixed-script control (not a code path)."""
    assert unicodedata.normalize("NFKC", "\u0455") != "s"
    assert unicodedata.name("\u0455") == "CYRILLIC SMALL LETTER DZE"


# --- GREEN controls: the deny set and ordinary paths are unchanged ---------

def test_control_plain_secrets_path_still_refused(sandbox):
    root = sandbox
    _write_secret_stub(root, "secrets")
    result = _call(_handlers(root).file_read, path=str(root / "secrets" / "key.txt"))
    assert result["status"] == "REFUSED" and result["reason"] == "CREDENTIAL_DENIED", result


def test_control_uppercase_dir_still_refused(sandbox):
    root = sandbox
    target = root / ".SSH" / "id_rsa"
    target.parent.mkdir()
    target.write_text("PLACEHOLDER", encoding="utf-8")
    result = _call(_handlers(root).file_read, path=str(target))
    assert result["status"] == "REFUSED" and result["reason"] == "CREDENTIAL_DENIED", result


def test_control_ads_on_denied_path_still_refused(sandbox):
    root = sandbox
    _write_secret_stub(root, "secrets")
    result = _call(_handlers(root).file_read, path=str(root / "secrets" / "key.txt:$DATA"))
    assert result["status"] == "REFUSED" and result["reason"] == "CREDENTIAL_DENIED", result


def test_control_stream_on_benign_file_is_not_denied(sandbox):
    root = sandbox
    (root / "project.py").write_text("ORDINARY", encoding="utf-8")
    result = _call(_handlers(root).file_read, path=str(root / "project.py:hidden"))
    assert result.get("reason") != "CREDENTIAL_DENIED", result


@pytest.mark.parametrize("relative", ["tokenomics/report.md", "secretary/notes.txt",
                                      "login_page.html"])
def test_control_ordinary_paths_still_allowed(sandbox, relative):
    root = sandbox
    target = root.joinpath(*relative.split("/"))
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("ORDINARY-CONTENT", encoding="utf-8")
    result = _call(_handlers(root).file_read, path=str(target))
    assert result["status"] == "OK" and result["content"] == "ORDINARY-CONTENT", result
