"""Owner-approved file capabilities for ``hermes mcp-serve`` (W3).

Three tools only: ``file_read``, ``file_write``, ``file_edit``.  No terminal, shell
or process tool exists in this module or in the registry it registers onto.

Approved scope (DC-MVP-SPEC.md): root ``C:\\Users\\wigmore`` and all subdirectories,
explicit absolute path required, file read/write/edit, no terminal.  The default
root below is ``Path.home()`` -- on the approved host that is C:\\Users\\wigmore.
Tests pass a temp root, so no real file is touched during verification.

Credential deny list is **OFF by default** (``deny_credentials=False``) so the tool
matches the approval exactly, including reachability of the credential stores the
spec explicitly notes.  Turning it on narrows scope: paths whose resolved relative
parts contain .ssh/.aws/.env or secret/token/credential/password/private-key material
are refused with ``CREDENTIAL_DENIED``.

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
import tempfile
from pathlib import Path

DEFAULT_ROOT = str(Path.home())
DEFAULT_MAX_BYTES = 65536
FILE_TOOL_NAMES = ("file_read", "file_write", "file_edit")

_DENY_PARTS = {".ssh", ".aws", ".env", ".npmrc", ".netrc"}
_DENY_TOKENS = ("secret", "token", "credential", "password", "private_key",
                "id_rsa", "id_ed25519")


def _decode_text(data: bytes) -> str | None:
    """Strict UTF-8 text, or None for binary. Back off up to 3 bytes so a character
    split by the truncation boundary is not misreported as binary."""
    if b"\x00" in data:
        return None
    for cut in range(4):
        candidate = data[: len(data) - cut] if cut else data
        try:
            return candidate.decode("utf-8")
        except UnicodeDecodeError:
            continue
    return None


def _is_credential_path(relative: Path) -> bool:
    parts = [part.lower() for part in relative.parts]
    return any(p in _DENY_PARTS or any(token in p for token in _DENY_TOKENS) for p in parts)


class FileTools:
    """Handlers bound to one approved root. Shared state is configuration only."""

    def __init__(self, root=DEFAULT_ROOT, max_bytes: int = DEFAULT_MAX_BYTES,
                 deny_credentials: bool = False):
        self.root = Path(root).resolve()
        self.max_bytes = max_bytes
        self.deny_credentials = deny_credentials

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
        if self.deny_credentials:
            relative = resolved.relative_to(self.root)
            if _is_credential_path(relative):
                return None, {"status": "REFUSED", "reason": "CREDENTIAL_DENIED", "path": str(resolved)}
        return resolved, None

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
            raw = resolved.read_bytes()
        except OSError as exc:
            return self._read_error(exc, resolved)
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

    def _commit(self, dest: Path, data: bytes, create_parents: bool) -> str:
        """Write temp + fsync + os.replace, then confirm from the file on disk."""
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
            if not self._inside_root(dest.resolve()):
                return json.dumps({"status": "REFUSED", "reason": "ROOT_ESCAPE", "path": str(dest)})
        fd, temp_name = tempfile.mkstemp(prefix="w3-", suffix=".tmp", dir=str(parent))
        try:
            with os.fdopen(fd, "wb") as handle:
                handle.write(data)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temp_name, dest)
        except OSError as exc:
            try:
                os.unlink(temp_name)
            except OSError:
                pass
            return json.dumps({"status": "NOT_WRITTEN", "reason": "COMMIT_FAILED",
                               "path": str(dest), "detail": str(exc)})
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
            raw = resolved.read_bytes()
        except OSError as exc:
            return self._read_error(exc, resolved)
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
                        deny_credentials: bool = False) -> int:
    """Register exactly the three approved file tools on an MCP server; return their count.

    deny_credentials defaults to False to match the approved scope exactly.  There is
    deliberately no terminal/shell tool registration path here.
    """
    handlers = FileTools(root=root, max_bytes=max_bytes, deny_credentials=deny_credentials)
    for name in FILE_TOOL_NAMES:
        mcp.tool()(getattr(handlers, name))
    return len(FILE_TOOL_NAMES)
