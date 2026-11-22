"""Owner-approved file capabilities for ``hermes mcp-serve`` (W3).

Three tools only: ``file_read``, ``file_write``, ``file_edit``.  No terminal, shell
or process tool exists in this module or in the registry it registers onto.

Approved scope (DC-MVP-SPEC.md): root ``C:\\Users\\wigmore`` and all subdirectories,
explicit absolute path required, file read/write/edit, no terminal.  The default
root below is ``Path.home()`` -- on the approved host that is C:\\Users\\wigmore.
Tests pass a temp root, so no real file is touched during verification.

Credential deny list is **ON by default** (``deny_credentials=True``): the credential
that authenticates an MCP client must not be readable by the tools it authorises.
Matching is on PATH COMPONENTS and WHOLE WORDS, never on substrings: a directory
component equal to a denied name (.ssh, .aws, .docker, secrets, ...) or a denied
two-part sequence (.config/gh, .config/tailscale), or a filename that is a known
credential filename, has a key/cert extension, or contains a credential word on a
word boundary, is refused with ``CREDENTIAL_DENIED``. Ordinary names such as
``tokenomics/`` or ``secretary/`` are not denied. The explicit opt-in
``deny_credentials=False`` restores the full approved reach deliberately.

A hard link is refused with ``HARD_LINK_REFUSED`` by default: a file with st_nlink > 1
can alias a file outside the root while its resolved path still looks in-root. The
link check and the read share ONE descriptor (os.open -> os.fstat -> read), so the
object validated is the object read and a swap between check and read cannot leak.
``allow_internal_hard_links=True`` is the explicit opt-in for hard-linked files; the
README documents the trade-off.

Response contract (JSON, never a raised exception for an expected condition):
* read   -> OK | REFUSED | ERROR
* write  -> LANDED | NOT_WRITTEN | REFUSED
* edit   -> LANDED | NOT_WRITTEN | REFUSED
``reason`` is always present on a failure.  LANDED is computed by reading the file
back from disk after the atomic rename, not by echoing the caller's input.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
import tempfile
import unicodedata
from pathlib import Path

DEFAULT_ROOT = str(Path.home())
DEFAULT_MAX_BYTES = 65536
FILE_TOOL_NAMES = ("file_read", "file_write", "file_edit")

# Deny by default: the credential authenticating the client must not be readable by
# the tools it authorises. Only exact PATH COMPONENTS and WHOLE WORDS count: a
# directory name is compared for equality, never as a substring of a longer name.
_DENY_DIR_NAMES = frozenset({".ssh", ".aws", ".docker", ".env", "secret", "secrets"})
_DENY_DIR_SEQUENCES = ((".config", "gh"), (".config", "tailscale"))
_DENY_FILENAMES = frozenset({
    "id_rsa", "id_ed25519", ".netrc", ".npmrc", "credentials", "known_hosts",
    "state.vscdb", "login data", "private_key", "keychain", "secrets",
})
_DENY_EXTENSIONS = frozenset({".pem", ".key", ".pfx", ".p12", ".keystore"})
_DENY_WORDS = ("token", "secret", "credential", "password", "private_key",
               "id_rsa", "id_ed25519", "login", "keychain")
_WORD_RE = re.compile(r"\b(?:" + "|".join(
    re.escape(word) for word in sorted(_DENY_WORDS, key=len, reverse=True)) + r")\b")


def _decode_text(data: bytes) -> str | None:
    """Strict UTF-8 text, or None when the bytes are not complete text.

    A truncation boundary must never silently drop bytes it already read: backing
    off over a split character hides those bytes while still reporting them in
    ``bytes``. So non-empty bytes that do not fully decode are not text, and an
    empty decode of non-empty bytes is None -- never "".
    """
    if b"\x00" in data:
        return None
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        return None


def _normalise_part(part: str) -> str:
    """Canonical form of one path component BEFORE any credential comparison.

    NFKC folds compatibility lookalikes (fullwidth Latin, ligatures, composed
    forms); Unicode format characters (zero-width space/joiners, bidi marks) are
    dropped so they cannot split a denied name; casefold keeps matching
    case-insensitive; rstrip mirrors Windows name resolution, which strips
    trailing dots and spaces from every path component.
    """
    text = unicodedata.normalize("NFKC", part)
    text = "".join(ch for ch in text if unicodedata.category(ch) != "Cf")
    return text.casefold().rstrip(". ")


_SCRIPT_NAMES = ("LATIN", "CYRILLIC", "GREEK", "ARMENIAN", "GEORGIAN", "HEBREW",
                 "ARABIC", "HIRAGANA", "KATAKANA", "HANGUL", "CJK", "DEVANAGARI",
                 "THAI", "BENGALI", "TAMIL", "KANNADA", "MALAYALAM")


def _scripts_in(part: str) -> set[str]:
    """Scripts used by the alphabetic characters of a component.

    NFKC cannot map a homoglyph with no canonical equivalence (Cyrillic dze for
    Latin 's', for example). A component that mixes scripts is therefore refused
    outright; a single-script component is left to the normal deny matching.
    """
    scripts = set()
    for ch in part:
        if not ch.isalpha():
            continue
        name = unicodedata.name(ch, "")
        for script in _SCRIPT_NAMES:
            if script in name:
                scripts.add(script)
                break
        else:
            scripts.add("OTHER")
    return scripts


def _is_credential_path(relative: Path) -> bool:
    """Deny by exact directory components and whole filename words, never substrings.

    Each component is normalised first (NFKC, format-character stripping,
    Windows trailing dot/space stripping, casefold) and a mixed-script component
    is refused, so confusables and Windows name aliases cannot slip past the
    comparison. A directory component only counts when it EQUALS a denied name
    (or completes a denied two-part sequence). The filename additionally counts
    when it equals a known credential filename, has a key/cert extension, or
    contains a credential vocabulary word on a word boundary -- so ``token`` is
    denied while ``tokenomics``/``tokenizer``/``secretary``/``login_page`` are
    ordinary names.
    """
    parts = [_normalise_part(part) for part in relative.parts]
    if not parts:
        return False
    if any(len(_scripts_in(part)) > 1 for part in parts):
        return True
    parents, filename = parts[:-1], parts[-1]
    if any(name in _DENY_DIR_NAMES for name in parents):
        return True
    if any(tuple(parents[i:i + len(seq)]) == seq
           for seq in _DENY_DIR_SEQUENCES for i in range(len(parents))):
        return True
    if (filename in _DENY_FILENAMES
            or filename == ".env" or filename.startswith(".env.")
            or Path(filename).suffix.lower() in _DENY_EXTENSIONS
            or _WORD_RE.search(filename)):
        return True
    return False


class FileTools:
    """Handlers bound to one approved root. Shared state is configuration only."""

    def __init__(self, root=DEFAULT_ROOT, max_bytes: int = DEFAULT_MAX_BYTES,
                 deny_credentials: bool = True, allow_internal_hard_links: bool = False):
        self.root = Path(root).resolve()
        self.max_bytes = max_bytes
        self.deny_credentials = deny_credentials
        self.allow_internal_hard_links = allow_internal_hard_links

    def _inside_root(self, resolved: Path) -> bool:
        root = os.path.normcase(str(self.root))
        candidate = os.path.normcase(str(resolved))
        return candidate == root or candidate.startswith(root + os.sep)

    def _resolve(self, raw):
        """Return (resolved_path, None) or (None, refusal_dict). Resolves real path --
        symlinks/junctions, 8.3 short names and .. -- before any containment decision."""
        if not isinstance(raw, str) or not raw.strip() or not Path(raw).is_absolute():
            return None, {"status": "REFUSED", "reason": "PATH_NOT_ABSOLUTE"}
        resolved = Path(raw).resolve()
        if not self._inside_root(resolved):
            return None, {"status": "REFUSED", "reason": "ROOT_ESCAPE", "path": str(resolved)}
        # Path resolution only. The hard-link decision is made on the descriptor
        # that is actually read (see _open_checked), never on a separate path stat.
        if self.deny_credentials:
            relative = resolved.relative_to(self.root)
            if _is_credential_path(relative):
                return None, {"status": "REFUSED", "reason": "CREDENTIAL_DENIED", "path": str(resolved)}
        return resolved, None

    def _hard_link_refusal(self, info, path: Path) -> dict | None:
        """Named refusal for a regular file with extra links, unless explicitly allowed."""
        if (stat.S_ISREG(info.st_mode) and info.st_nlink > 1
                and not self.allow_internal_hard_links):
            return {"status": "REFUSED", "reason": "HARD_LINK_REFUSED", "path": str(path)}
        return None

    def _link_refusal(self, path: Path) -> dict | None:
        """Stat-based check for write paths that never read bytes (nothing to swap)."""
        try:
            info = os.stat(path)
        except OSError:
            return None  # a missing file has no extra links; commit reports the real I/O error
        return self._hard_link_refusal(info, path)

    def _open_checked(self, path: Path):
        """Open ONCE, fstat THAT DESCRIPTOR, return it for the read on the same fd.

        The object validated is the object read: a path swap after open cannot
        change what an already-open descriptor refers to. Returns (fd, refusal)
        or (fd, None); open/fstat OSErrors propagate to the caller's read error.
        """
        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
        fd = os.open(path, flags)
        try:
            info = os.fstat(fd)
        except OSError:
            os.close(fd)
            raise
        if not stat.S_ISREG(info.st_mode):
            os.close(fd)
            return None, {"status": "ERROR", "reason": "UNREADABLE", "path": str(path)}
        refusal = self._hard_link_refusal(info, path)
        if refusal:
            os.close(fd)
            return None, refusal
        return fd, None

    @staticmethod
    def _read_fd(fd: int) -> bytes:
        """Read an already-validated descriptor to EOF; the caller closes it."""
        chunks = []
        while True:
            chunk = os.read(fd, 65536)
            if not chunk:
                return b"".join(chunks)
            chunks.append(chunk)

    @staticmethod
    def _read_error(exc: OSError, path: Path) -> str:
        reason = "NOT_FOUND" if isinstance(exc, FileNotFoundError) else "UNREADABLE"
        return json.dumps({"status": "ERROR", "reason": reason, "path": str(path), "detail": str(exc)})

    def file_read(self, path: str, max_bytes: int = DEFAULT_MAX_BYTES) -> str:
        """Read text under the approved root. Bounded; truncation is marked with exact byte counts.

        path: absolute path within the approved root (required).
        max_bytes: maximum bytes returned; default 65536.
        """
        resolved, refusal = self._resolve(path)
        if refusal:
            return json.dumps(refusal)
        try:
            limit = max(1, int(max_bytes))
        except (TypeError, ValueError):
            return json.dumps({"status": "ERROR", "reason": "BAD_MAX_BYTES", "path": str(resolved)})
        try:
            fd, refusal = self._open_checked(resolved)
        except OSError as exc:
            return self._read_error(exc, resolved)
        if refusal:
            return json.dumps(refusal)
        try:
            raw = self._read_fd(fd)
        except OSError as exc:
            return self._read_error(exc, resolved)
        finally:
            os.close(fd)
        sample = raw[:limit]
        text = _decode_text(sample)
        if text is None:
            return json.dumps({"status": "REFUSED", "reason": "BINARY_FILE", "path": str(resolved)})
        truncated = len(raw) > len(sample)
        if truncated:
            text += (f"\n[TRUNCATED: showing first {len(sample)} of {len(raw)} bytes; "
                     f"{len(raw) - len(sample)} bytes omitted]")
        return json.dumps({"status": "OK", "path": str(resolved), "bytes": len(sample),
                           "total_bytes": len(raw), "truncated": truncated, "content": text})

    @staticmethod
    def _discard_temp(temp_name: str) -> None:
        """Unlink an uncommitted temp file. One retry: on Windows a sharing violation
        can clear as the last handle closes, and `except OSError: pass` hid it."""
        for _ in range(2):
            try:
                os.unlink(temp_name)
                return
            except OSError:
                continue

    def _commit(self, dest: Path, data: bytes, create_parents: bool) -> str:
        """Write temp + fsync + os.replace, then confirm from the file on disk.

        ``dest`` is the single path validated by _resolve; no second resolve is
        performed here, so the path checked is exactly the path written.
        """
        refusal = self._link_refusal(dest)
        if refusal:
            return json.dumps(refusal)
        parent = dest.parent
        if not parent.is_dir():
            if not create_parents:
                return json.dumps({"status": "NOT_WRITTEN", "reason": "PARENT_NOT_FOUND",
                                   "path": str(dest)})
            try:
                parent.mkdir(parents=True, exist_ok=True)
            except OSError as exc:
                return json.dumps({"status": "NOT_WRITTEN", "reason": "PARENT_CREATE_FAILED",
                                   "path": str(dest), "detail": str(exc)})
            if not self._inside_root(parent):
                return json.dumps({"status": "REFUSED", "reason": "ROOT_ESCAPE", "path": str(dest)})
        fd, temp_name = tempfile.mkstemp(prefix="w3-", suffix=".tmp", dir=str(parent))
        committed = False
        failure = None
        try:
            try:
                handle = os.fdopen(fd, "wb")
            except OSError:
                os.close(fd)  # fdopen failed: descriptor is still ours; release before cleanup
                raise
            with handle:
                handle.write(data)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temp_name, dest)
            committed = True
        except OSError as exc:
            failure = exc
        finally:
            # Always drop the temp when the commit did not succeed; with the descriptor
            # closed, an open handle cannot keep written content alive on Windows.
            if not committed:
                self._discard_temp(temp_name)
        if not committed:
            return json.dumps({"status": "NOT_WRITTEN", "reason": "COMMIT_FAILED",
                               "path": str(dest), "detail": str(failure)})
        try:
            disk = dest.read_bytes()  # confirmation from disk, not from the request
        except OSError as exc:
            return json.dumps({"status": "NOT_WRITTEN", "reason": "VERIFY_FAILED",
                               "path": str(dest), "detail": str(exc)})
        return json.dumps({"status": "LANDED", "path": str(dest), "bytes": len(disk),
                           "sha256": hashlib.sha256(disk).hexdigest()})

    def file_write(self, path: str, content: str, create_parents: bool = False) -> str:
        """Create or replace a file atomically at an explicit absolute path in root.

        path: absolute path within the approved root (required).
        content: UTF-8 text to write.
        create_parents: create missing parent directories only when true.
        """
        resolved, refusal = self._resolve(path)
        if refusal:
            return json.dumps(refusal)
        return self._commit(resolved, content.encode("utf-8"), create_parents)

    def file_edit(self, path: str, old: str, new: str) -> str:
        """Replace one exact match of old with new. Absent/ambiguous match -> NOT_WRITTEN.

        path: absolute path within the approved root (required).
        old: exact text to replace (must be non-empty and occur exactly once).
        new: replacement text.
        """
        resolved, refusal = self._resolve(path)
        if refusal:
            return json.dumps(refusal)
        if not old:
            return json.dumps({"status": "NOT_WRITTEN", "reason": "EMPTY_OLD", "path": str(resolved)})
        try:
            fd, refusal = self._open_checked(resolved)
        except OSError as exc:
            return self._read_error(exc, resolved)
        if refusal:
            return json.dumps(refusal)
        try:
            raw = self._read_fd(fd)
        except OSError as exc:
            return self._read_error(exc, resolved)
        finally:
            os.close(fd)
        text = _decode_text(raw)
        if text is None:
            return json.dumps({"status": "REFUSED", "reason": "BINARY_FILE", "path": str(resolved)})
        count = text.count(old)
        if count == 0:
            return json.dumps({"status": "NOT_WRITTEN", "reason": "MATCH_NOT_FOUND", "path": str(resolved)})
        if count > 1:
            return json.dumps({"status": "NOT_WRITTEN", "reason": "AMBIGUOUS_MATCH",
                               "count": count, "path": str(resolved)})
        return self._commit(resolved, text.replace(old, new, 1).encode("utf-8"), False)


def register_file_tools(mcp, *, root=DEFAULT_ROOT, max_bytes: int = DEFAULT_MAX_BYTES,
                        deny_credentials: bool = True,
                        allow_internal_hard_links: bool = False) -> int:
    """Register exactly the three approved file tools on an MCP server; return their count.

    Credentials are denied by default. Pass ``deny_credentials=False`` to deliberately
    restore the full approved reach. Hard links are refused by default; pass
    ``allow_internal_hard_links=True`` only when the root is trusted to contain every
    alias (see README). There is no terminal/shell registration path here.
    """
    handlers = FileTools(root=root, max_bytes=max_bytes, deny_credentials=deny_credentials,
                         allow_internal_hard_links=allow_internal_hard_links)
    for name in FILE_TOOL_NAMES:
        mcp.tool()(getattr(handlers, name))
    return len(FILE_TOOL_NAMES)
