"""Wake-enrolment store: part 1 of the wake-delivery mechanism.

A GPT-side client pre-registers a task key *before* the work exists.  When that
task later settles with a terminal ``completed`` outcome, this module creates
exactly one durable outbox row that a separate deliverer (part 2) hands to a
browser tab.  Nothing here touches the network, starts a daemon, or drives a
browser: it is a library plus tests only.

Invariants
----------
* One enrolment per task key and one outbox row per admission id, both enforced
  by PRIMARY KEY at write time, never by a read-then-write guess.
* An outbox row exists only for a settlement whose outcome is exactly
  ``completed`` and only when a matching *open* enrolment exists.
* An enrolment is single-use: the settlement that enqueues a row consumes it,
  and the consume is a conditional UPDATE that runs *before* the insert, so
  exactly one settlement can ever win an enrolment.
* A crash between that consume and the insert loses no wake permanently: the
  orphan is reclaimed by ``reconcile()``.
* An expired enrolment can never produce a row, whether or not the caller swept
  it first.
* Every function takes an explicit ISO-8601 UTC ``now``; this module never reads
  a clock, so callers and tests stay deterministic.
"""

import datetime
import hmac
import os
import sqlite3
import uuid

# --- named results ---------------------------------------------------------
# enrol()
ENROLLED = "ENROLLED"
ALREADY_ENROLLED = "ALREADY_ENROLLED"
EMPTY_TASK_KEY = "EMPTY_TASK_KEY"
EMPTY_TARGET = "EMPTY_TARGET"

# resolve_and_enqueue()
NOT_COMPLETED = "NOT_COMPLETED"
NOT_ENROLLED = "NOT_ENROLLED"
DUPLICATE = "DUPLICATE"

# mark_delivered()
DELIVERED = "DELIVERED"
ALREADY_DELIVERED = "ALREADY_DELIVERED"

# mark_ack()
ACKED = "ACKED"
WRONG_NONCE = "WRONG_NONCE"
NOT_DELIVERED = "NOT_DELIVERED"
UNKNOWN = "UNKNOWN"

# resolve_operation()
CREATED = "CREATED"
REPLAY = "REPLAY"
MISMATCH = "MISMATCH"

# link_admission()
LINKED = "LINKED"
CONFLICT = "CONFLICT"

OUTCOME_COMPLETED = "completed"

STATE_OPEN = "open"
STATE_CONSUMED = "consumed"
STATE_EXPIRED = "expired"

SCHEMA = """
CREATE TABLE IF NOT EXISTS enrolments (
    task_key    TEXT PRIMARY KEY,
    target      TEXT NOT NULL,
    enrolled_at TEXT NOT NULL,
    expires_at  TEXT NOT NULL,
    state       TEXT NOT NULL DEFAULT 'open',
    operation_id   TEXT UNIQUE,
    principal      TEXT,
    session_id     TEXT,
    payload_digest TEXT,
    admission_id   TEXT,
    ttl_seconds    INTEGER
);
CREATE TABLE IF NOT EXISTS outbox (
    admission_id TEXT PRIMARY KEY,
    task_key     TEXT NOT NULL,
    target       TEXT NOT NULL,
    session_id   TEXT,
    outcome      TEXT NOT NULL,
    created_at   TEXT NOT NULL,
    attempts     INTEGER NOT NULL DEFAULT 0,
    delivered_at TEXT,
    ack_at       TEXT,
    last_error   TEXT,
    nonce        TEXT
);
"""

OUTBOX_COLUMNS = (
    "admission_id",
    "task_key",
    "target",
    "session_id",
    "outcome",
    "created_at",
    "attempts",
    "delivered_at",
    "ack_at",
    "last_error",
    "nonce",
)


# --- time handling ---------------------------------------------------------
def _parse_iso(value):
    """Parse ``...Z`` or ``...+00:00`` into an aware UTC datetime.

    Invariant: timestamps are only ever compared as datetimes, never as
    arbitrary strings, so a caller may pass either ISO-8601 spelling.
    """
    if not isinstance(value, str) or not value.strip():
        raise ValueError("timestamp must be a non-empty ISO-8601 string")
    text = value.strip()
    if text.endswith(("Z", "z")):
        text = text[:-1] + "+00:00"
    try:
        moment = datetime.datetime.fromisoformat(text)
    except ValueError as exc:
        raise ValueError("not an ISO-8601 timestamp: %r" % (value,)) from exc
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=datetime.timezone.utc)
    return moment.astimezone(datetime.timezone.utc)


def _iso(moment):
    """Render a datetime as the canonical ``YYYY-MM-DDTHH:MM:SSZ`` form.

    Invariant: every stored timestamp uses this one fixed-width UTC form, so
    ORDER BY / <= on the stored text is also chronological ordering.
    """
    return moment.astimezone(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _now_pair(now):
    """Return (parsed datetime, canonical string) for a caller-supplied ``now``."""
    moment = _parse_iso(now)
    return moment, _iso(moment)


# --- store -----------------------------------------------------------------
def connect(db_path):
    """Open the sqlite store at ``db_path`` and guarantee the schema exists.

    Invariant: the caller owns the file location (tests pass a temp file); the
    two-table schema is created idempotently and committed before return.

    Durability scope
    ----------------
    The file is opened in WAL mode with ``synchronous=FULL``, so a commit is
    fsync'd to the log before the statement returns and the next action runs.
    That is the guarantee the delivery state machine depends on: a *process
    death* (SIGKILL, ``os._exit``, a crashed interpreter) at any point cannot
    make a committed transaction disappear, and cannot make an uncommitted one
    appear.  It is scoped to process death only - power-loss / torn-media
    durability is NOT claimed here and is not tested.
    """
    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=FULL")
    conn.commit()
    ensure_schema(conn)
    return conn


def ensure_schema(conn):
    """Create both tables if missing, then bring an older file up to date.

    Invariant: safe to call on a connection the caller built itself, and never
    destructive - existing rows are left untouched.  A file written by an
    earlier release keeps its table and gains the operation columns in place.
    """
    conn.executescript(SCHEMA)
    _migrate(conn)
    existing = {row[1] for row in conn.execute("PRAGMA table_info(outbox)")}
    if "nonce" not in existing:
        conn.execute("ALTER TABLE outbox ADD COLUMN nonce TEXT")
    conn.commit()


# --- schema migration ------------------------------------------------------
# A wake-enrolment file written before operation-idempotency has no operation
# columns at all.  ``CREATE TABLE IF NOT EXISTS`` leaves such a table alone, so
# the columns are added one at a time and an old file opens exactly like a new
# one.
ENROLMENT_COLUMNS_ADDED = (
    ("operation_id", "TEXT"),
    ("principal", "TEXT"),
    ("session_id", "TEXT"),
    ("payload_digest", "TEXT"),
    ("admission_id", "TEXT"),
    ("ttl_seconds", "INTEGER"),
)

# ``TEXT UNIQUE`` is a table constraint, and sqlite's ALTER TABLE cannot add
# one.  A named unique index is the same rule in a form the ALTER path can
# apply, so a migrated file enforces one operation per id exactly like a fresh
# one (which gets the index instead of a duplicate constraint).
OPERATION_INDEX = (
    "CREATE UNIQUE INDEX IF NOT EXISTS ux_enrolments_operation_id "
    "ON enrolments(operation_id)"
)


def _migrate(conn):
    """Add any missing operation columns to an existing table; return their names.

    Invariant: purely additive.  No column is dropped, no row is rewritten and
    no existing value is touched, so a file from a previous release is readable
    by both this release and the one that wrote it.
    """
    existing = {row[1] for row in conn.execute("PRAGMA table_info(enrolments)")}
    added = []
    for name, declared in ENROLMENT_COLUMNS_ADDED:
        if name in existing:
            continue
        conn.execute("ALTER TABLE enrolments ADD COLUMN %s %s" % (name, declared))
        added.append(name)
    if "operation_id" in added:
        conn.execute(OPERATION_INDEX)
    conn.commit()
    return tuple(added)


def _fetch_enrolment(conn, task_key):
    """Return (task_key, target, state, expires_at) for an exact task key, or None.

    Invariant: the match is exact string equality performed by sqlite's ``=``
    on TEXT - no prefix, case-folding or trimming is applied to either side.
    """
    return conn.execute(
        "SELECT task_key, target, state, expires_at FROM enrolments WHERE task_key = ?",
        (task_key,),
    ).fetchone()


def _outbox_has(conn, admission_id):
    """True when this admission id is already durably enqueued."""
    return (
        conn.execute(
            "SELECT 1 FROM outbox WHERE admission_id = ?", (admission_id,)
        ).fetchone()
        is not None
    )


# --- enrolment -------------------------------------------------------------
def enrol(conn, task_key, target, ttl_seconds, now):
    """Pre-register ``task_key`` so a later successful settlement can wake ``target``.

    Returns ``ENROLLED``, ``EMPTY_TASK_KEY``, ``EMPTY_TARGET`` or
    ``ALREADY_ENROLLED`` - a named result, never a swallowed exception.

    Invariant: the first enrolment for a task key wins.  The insert is
    ``OR IGNORE`` on the PRIMARY KEY, so a repeated enrol can never rewrite the
    stored target, and the caller learns it was a repeat from the result code.
    """
    if not isinstance(task_key, str) or not task_key.strip():
        return EMPTY_TASK_KEY
    if not isinstance(target, str) or not target.strip():
        return EMPTY_TARGET

    moment, now_text = _now_pair(now)
    expires_at = _iso(moment + datetime.timedelta(seconds=int(ttl_seconds)))
    cursor = conn.execute(
        "INSERT OR IGNORE INTO enrolments "
        "(task_key, target, enrolled_at, expires_at, state, ttl_seconds) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (task_key, target, now_text, expires_at, STATE_OPEN, int(ttl_seconds)),
    )
    conn.commit()
    return ENROLLED if cursor.rowcount == 1 else ALREADY_ENROLLED


def sweep_expired(conn, now):
    """Mark every open enrolment whose TTL has passed as ``expired``; return the count.

    Invariant: expiry is ``expires_at <= now`` on canonical UTC text, and it is
    one-way - a swept enrolment is never reopened, so it can no longer admit a
    settlement.
    """
    _, now_text = _now_pair(now)
    cursor = conn.execute(
        "UPDATE enrolments SET state = ? WHERE state = ? AND expires_at <= ?",
        (STATE_EXPIRED, STATE_OPEN, now_text),
    )
    conn.commit()
    return cursor.rowcount


# --- settlement resolution -------------------------------------------------
def resolve_and_enqueue(conn, row, now):
    """Turn a committed terminal settlement into one durable outbox row.

    ``row`` is a mapping with ``admission_id``, ``request_id``, ``session_id``
    and ``outcome``.  Returns ``ENROLLED``, ``NOT_COMPLETED``, ``NOT_ENROLLED``
    or ``DUPLICATE``.

    Invariants:
    * only ``outcome == 'completed'`` can enqueue anything;
    * the enrolment is found by exact ``task_key == row['request_id']`` and must
      be ``open`` and unexpired;
    * exclusivity is decided by a *single conditional UPDATE* that spends the
      enrolment (``open`` -> ``consumed``) before any row is inserted.  Sqlite
      serialises writers, so exactly one settlement can ever see ``rowcount ==
      1`` for an enrolment; two admissions settling concurrently against the
      same open enrolment yield one wake, and the loser learns it lost (one row,
      one ``ENROLLED``) instead of both enqueueing off one enrolment;
    * a loser never inserts: the consume is the gate, and the insert that follows
      cannot be reached by a settlement that did not win the enrolment;
    * replay is still answered, not crashed: a settlement that arrives against a
      spent enrolment returns ``DUPLICATE`` when the outbox already holds that
      exact admission id and ``NOT_ENROLLED`` otherwise;
    * a crash between the consume and the insert leaves an *orphan* - a
      ``consumed`` enrolment whose admission has no outbox row - which
      ``reconcile()`` finds by joining consumed enrolments to terminal
      ``completed`` admissions and inserts exactly once.
    """
    admission_id = row["admission_id"]
    request_id = row["request_id"]
    session_id = row["session_id"] if "session_id" in row else None
    outcome = row["outcome"]

    if outcome != OUTCOME_COMPLETED:
        return NOT_COMPLETED
    if not admission_id or not request_id:
        return NOT_ENROLLED

    _, now_text = _now_pair(now)
    enrolment = _fetch_enrolment(conn, request_id)
    if enrolment is None:
        return NOT_ENROLLED

    _, target, state, expires_at = enrolment
    if state == STATE_EXPIRED:
        return NOT_ENROLLED
    if state == STATE_OPEN and _parse_iso(now_text) >= _parse_iso(expires_at):
        # Defence in depth: even if the caller never swept, an expired TTL can
        # not wake anything.  Record the expiry so the state stays truthful.
        conn.execute(
            "UPDATE enrolments SET state = ? WHERE task_key = ? AND state = ?",
            (STATE_EXPIRED, request_id, STATE_OPEN),
        )
        conn.commit()
        return NOT_ENROLLED

    # The gate.  Whoever flips this enrolment out of ``open`` owns it; sqlite
    # serialises writers, so a concurrent settlement blocks here, then sees
    # ``rowcount == 0`` and is refused without inserting anything.
    gate = conn.execute(
        "UPDATE enrolments SET state = ? WHERE task_key = ? AND state = ?",
        (STATE_CONSUMED, request_id, STATE_OPEN),
    )
    if gate.rowcount != 1:
        # We did not win the enrolment.  The only legitimate way in is a replay
        # of the admission that did win it - and the outbox, not a prior read,
        # answers that: if its row is already there this is a re-delivery of the
        # same settlement, otherwise a different admission lost the race.
        replaced = _outbox_has(conn, admission_id)
        conn.commit()
        return DUPLICATE if replaced else NOT_ENROLLED

    # The enrolment is exclusively ours, so no concurrent settlement can reach
    # this insert: a plain INSERT is the truthful write (a duplicate here would
    # be a bug, and must raise rather than be silently tolerated).
    conn.execute(
        "INSERT INTO outbox "
        "(admission_id, task_key, target, session_id, outcome, created_at, nonce) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (admission_id, request_id, target, session_id, OUTCOME_COMPLETED, now_text, os.urandom(16).hex()),
    )
    conn.commit()
    return ENROLLED


def reconcile(conn, rows, now=None):
    """Re-insert the wake rows lost by a crash between the consume and the insert.

    ``rows`` are terminal admission mappings - the same shape ``resolve_and_enqueue``
    takes (``admission_id``, ``request_id``, ``session_id``, ``outcome``), each
    optionally carrying the ``created_at`` stamp to preserve.  Returns how many
    outbox rows it actually inserted.

    Invariants:
    * only a *consumed* enrolment is reclaimed: an open one is not settled yet,
      an expired one may never wake anything, and one with no enrolment at all
      never had a claim;
    * only ``outcome == 'completed'`` rows are considered, exactly as in
      ``resolve_and_enqueue``;
    * the insert is ``OR IGNORE`` on the outbox PRIMARY KEY and the count comes
      from ``rowcount``, so reconciling twice (or racing a resurrected
      settlement) inserts nothing the second time - repair only fills the hole,
      it never re-sends a wake.
    """
    inserted = 0
    for row in rows:
        admission_id = row["admission_id"]
        request_id = row["request_id"]
        if row["outcome"] != OUTCOME_COMPLETED or not admission_id or not request_id:
            continue

        found = conn.execute(
            "SELECT target FROM enrolments WHERE state = ? AND task_key = ?",
            (STATE_CONSUMED, request_id),
        ).fetchone()
        if found is None:
            continue
        target = found[0]

        session_id = row["session_id"] if "session_id" in row else None
        created_at = row["created_at"] if "created_at" in row else now
        if created_at is None:
            raise ValueError(
                "reconcile needs a created_at on the row or an explicit now"
            )
        created_text = _now_pair(created_at)[1]

        cursor = conn.execute(
            "INSERT OR IGNORE INTO outbox "
            "(admission_id, task_key, target, session_id, outcome, created_at, nonce) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                admission_id,
                request_id,
                target,
                session_id,
                OUTCOME_COMPLETED,
                created_text,
                os.urandom(16).hex(),
            ),
        )
        if cursor.rowcount == 1:
            inserted += 1
    conn.commit()
    return inserted


def mint_key():
    """Mint the task key for one wake enrolment: 32 lowercase hex characters.

    Invariant: the value is ``uuid4().hex`` - 128 bits from the OS entropy
    source, so it is not guessable and not reused in practice.

    The caller must submit the work with *exactly* this value as its message id
    or ``Idempotency-Key``, because the admission's ``request_id`` is that value:
    the API path derives ``request_id`` from the request's message id / header
    (``gateway/platforms/api_server.py:3464-3481``) and the a2a path passes
    ``params['input_id']`` as the request id (``gateway/session_a2a.py:138``).
    Enrolment matches on exact equality, so a submission under any other id
    enrols nothing and can never wake this target.
    """
    return uuid.uuid4().hex


# --- operation bindings ----------------------------------------------------
def resolve_operation(
    conn,
    operation_id,
    principal,
    session_id,
    payload_digest,
    destination,
    ttl_seconds,
    now,
):
    """Bind a client-supplied ``operation_id`` to exactly one task key.

    Returns ``(CREATED, task_key)`` the first time an operation id is seen,
    ``(REPLAY, task_key, admission_id)`` when the same caller repeats the same
    request, and ``(MISMATCH, field)`` when any binding differs.

    Invariants:
    * the match is the whole tuple ``(principal, session_id, payload_digest,
      destination)`` - every field must agree, so a retry can never inherit a
      key that was minted for a different caller, session, payload or target;
    * a mismatch is refused *by field name* and the stored row is left
      untouched: a repeated operation id never rewrites a binding and never
      mints a second key, so one operation id can only ever produce one
      enrolment;
    * ``admission_id`` stays NULL until ``link_admission`` records the
      submission the key was actually sent under - a replay before that point
      therefore reports ``None`` rather than inventing an admission;
    * the task key is the message id the work must be submitted under, so the
      settlement that wakes this enrolment carries it as ``request_id``.
    """
    if not isinstance(operation_id, str) or not operation_id.strip():
        raise ValueError("resolve_operation needs a non-empty operation_id")

    row = conn.execute(
        "SELECT task_key, principal, session_id, payload_digest, target, admission_id, ttl_seconds "
        "FROM enrolments WHERE operation_id = ?",
        (operation_id,),
    ).fetchone()

    if row is None:
        moment, now_text = _now_pair(now)
        expires_at = _iso(moment + datetime.timedelta(seconds=int(ttl_seconds)))
        task_key = mint_key()
        conn.execute(
            "INSERT INTO enrolments "
            "(task_key, target, enrolled_at, expires_at, state, "
            " operation_id, principal, session_id, payload_digest, admission_id, ttl_seconds) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, ?)",
            (
                task_key,
                destination,
                now_text,
                expires_at,
                STATE_OPEN,
                operation_id,
                principal,
                session_id,
                payload_digest,
                int(ttl_seconds),
            ),
        )
        conn.commit()
        return (CREATED, task_key)

    (
        stored_key,
        stored_principal,
        stored_session,
        stored_digest,
        stored_target,
        admission_id,
        stored_ttl_seconds,
    ) = row

    try:
        stored_ttl_seconds = int(stored_ttl_seconds)
        given_ttl_seconds = int(ttl_seconds)
    except (TypeError, ValueError, OverflowError):
        return (MISMATCH, "ttl_seconds")

    for field, stored, given in (
        ("principal", stored_principal, principal),
        ("session_id", stored_session, session_id),
        ("payload_digest", stored_digest, payload_digest),
        ("destination", stored_target, destination),
        ("ttl_seconds", stored_ttl_seconds, given_ttl_seconds),
    ):
        if stored != given:
            return (MISMATCH, field)

    return (REPLAY, stored_key, admission_id)


def link_admission(conn, operation_id, admission_id):
    """Record the admission the operation's key was submitted under.

    Returns ``LINKED``, ``CONFLICT`` or ``UNKNOWN``.

    Invariants: the link is written by one conditional UPDATE that only fills a
    NULL, so the first admission wins and a repeated call with the same value
    is a no-op; a *different* admission for the same operation id is a
    ``CONFLICT`` and changes nothing, because one operation may only ever own
    one admission.
    """
    if not admission_id:
        return UNKNOWN
    cursor = conn.execute(
        "UPDATE enrolments SET admission_id = ? "
        "WHERE operation_id = ? AND admission_id IS NULL",
        (admission_id, operation_id),
    )
    conn.commit()
    if cursor.rowcount == 1:
        return LINKED

    row = conn.execute(
        "SELECT admission_id FROM enrolments WHERE operation_id = ?",
        (operation_id,),
    ).fetchone()
    if row is None:
        return UNKNOWN
    return LINKED if row[0] == admission_id else CONFLICT


def ack_context(conn, admission_id):
    """Return the row an ack must be judged against, or None.

    Shape: ``(task_key, wake_session_id, delivered_at, enrolment_principal,
    enrolment_session_id)``.  The wake's own session and the enrolment that
    authorised it are read together, because an ack is only legitimate for the
    session and principal the wake was addressed to.
    """
    return conn.execute(
        "SELECT o.task_key, o.session_id, o.delivered_at, e.principal, e.session_id "
        "FROM outbox o LEFT JOIN enrolments e ON e.task_key = o.task_key "
        "WHERE o.admission_id = ?",
        (admission_id,),
    ).fetchone()


# --- delivery / ack --------------------------------------------------------
def mark_delivered(conn, admission_id, now):
    """Stamp ``delivered_at`` the first time a row reaches the browser tab.

    Returns ``DELIVERED``, ``ALREADY_DELIVERED`` or ``UNKNOWN``.

    Invariant: delivery is stamped at most once - the first delivery time is
    preserved so retries can never mask how long a wake actually took.
    """
    _, now_text = _now_pair(now)
    cursor = conn.execute(
        "UPDATE outbox SET delivered_at = ? WHERE admission_id = ? AND delivered_at IS NULL",
        (now_text, admission_id),
    )
    conn.commit()
    if cursor.rowcount == 1:
        return DELIVERED
    if _outbox_has(conn, admission_id):
        return ALREADY_DELIVERED
    return UNKNOWN


def mark_ack(conn, admission_id, nonce_provided, now):
    """Record the tab's acknowledgement of a delivered wake.

    Returns ``ACKED``, ``NOT_DELIVERED``, ``WRONG_NONCE`` or ``UNKNOWN``.

    Invariants: a row must exist and must already be delivered before a nonce is
    even considered, and the nonce must match the expected one in constant
    time.  A rejected ack never writes ``ack_at``.
    """
    _, now_text = _now_pair(now)
    row = conn.execute(
        "SELECT delivered_at, nonce FROM outbox WHERE admission_id = ?", (admission_id,)
    ).fetchone()
    if row is None:
        return UNKNOWN
    if row[0] is None:
        return NOT_DELIVERED
    if not _nonces_equal(nonce_provided, row[1]):
        return WRONG_NONCE

    conn.execute(
        "UPDATE outbox SET ack_at = COALESCE(ack_at, ?) WHERE admission_id = ?",
        (now_text, admission_id),
    )
    conn.commit()
    return ACKED


def _nonces_equal(provided, expected):
    """Constant-time nonce comparison that treats a missing nonce as a mismatch."""
    if provided is None or expected is None:
        return False
    return hmac.compare_digest(str(provided), str(expected))


# --- part-2 handoff --------------------------------------------------------
def pending(conn):
    """Return undelivered outbox rows, oldest first, as a list of dicts.

    Invariant: this is the deliverer's work queue - ``delivered_at IS NULL``
    only, ordered by ``created_at`` with ``admission_id`` as a stable tie-break
    so two runs of the deliverer see the same order.
    """
    rows = conn.execute(
        "SELECT %s FROM outbox WHERE delivered_at IS NULL "
        "ORDER BY created_at, admission_id" % ", ".join(OUTBOX_COLUMNS)
    ).fetchall()
    return [dict(zip(OUTBOX_COLUMNS, row)) for row in rows]
