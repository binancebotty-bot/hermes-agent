"""At-most-once delivery state machine for the wake outbox (wake mechanism, part 3).

Part 1 (``wake_enrol``) writes exactly one outbox row per settled admission.
This module is the only code allowed to *spend* such a row on a physical send.
Its whole job is one sentence: **an outbox row produces at most one physical
send, even if the process is killed at any point.**

How that is achieved
--------------------
A send is irreversible from the moment the deliverer commits ``ATTEMPTING``,
which happens *before* the transport is called.  From then on the only states
the code can write are ``DELIVERED`` (the transport returned ``OK`` *and* a
non-empty readback evidence string) or ``HELD`` (nobody knows).  ``HELD`` is
terminal for the deliverer: a held row is never re-claimed automatically, so a
restart can never turn an uncertain send into a second one.  The only way out
of ``HELD`` is ``reconcile_held`` - an explicit operator decision - which is
why it demands ``'confirm_unsent'`` / ``'confirm_abandon'`` instead of a bool.

Transitions the code can write
------------------------------
    PENDING    -> ATTEMPTING  deliverer, committed immediately before the send
    ATTEMPTING -> DELIVERED   transport returned OK + readback evidence
    ATTEMPTING -> HELD        BUSY / UNCERTAIN / exception / unknown result / no evidence
    PENDING    -> HELD        binding unresolvable: parked without ever claiming
    DELIVERED  -> (nothing)
    HELD       -> (nothing)   reconcile_held only

``claim`` also writes ATTEMPTING -> HELD when it *finds* a row already
ATTEMPTING **and that attempt's ``attempt_at`` is older than ``LEASE_SECONDS``**.
That is not a new transition but the recovery of one: an expired ATTEMPTING row
means the previous owner died mid-attempt, and by construction we cannot tell
whether the browser saw it, so it is parked instead of resent.  A live lease is
reported as ``IN_FLIGHT`` and left untouched, so a second deliverer can never
strand the winner's own attempt.  An unparseable ``attempt_at`` counts as
expired: a corrupt stamp fails closed instead of wedging the row forever.

Durability scope
----------------
``wake_enrol.connect`` opens the file with ``journal_mode=WAL`` and
``synchronous=FULL``, so every commit is fsync'd before the next statement
runs.  That is what makes the ordering above meaningful under a kill: a
committed ``ATTEMPTING`` survives process death, an uncommitted one does not.
The guarantee is scoped to **process death**; power-loss durability is NOT
claimed here.

The transport is injected: any object with ``send(envelope) -> (result,
evidence)``.  This module never imports CDP, never opens a socket and never
reads a clock; every function takes an explicit ISO-8601 UTC ``now``.
"""



import wake_enrol as we

# --- states ----------------------------------------------------------------
PENDING = "PENDING"
ATTEMPTING = "ATTEMPTING"
DELIVERED = "DELIVERED"
HELD = "HELD"

STATES = (PENDING, ATTEMPTING, DELIVERED, HELD)

# --- named results ---------------------------------------------------------
# claim()
CLAIMED = "CLAIMED"
IN_FLIGHT = "IN_FLIGHT"
UNKNOWN = "UNKNOWN"

# How long a committed ATTEMPTING stamp is honoured as a *live* attempt.  Inside
# the window another claimer reports IN_FLIGHT and writes nothing; only past it
# is the attempt treated as dead and parked HELD.
LEASE_SECONDS = 300

# finish() / transport answers
OK = "OK"
BUSY = "BUSY"
UNCERTAIN = "UNCERTAIN"
BINDING_UNRESOLVED = "BINDING_UNRESOLVED"
REFUSED_GUARD_DISABLED = "REFUSED_GUARD_DISABLED"

# The one legal target state for each transport answer.
RESULT_STATE = {
    OK: DELIVERED,
    BUSY: HELD,
    UNCERTAIN: HELD,
    BINDING_UNRESOLVED: HELD,
    REFUSED_GUARD_DISABLED: HELD,
}

# The transition table.  A target state that is not listed for the current
# state is refused with IllegalTransition - there is no fallback write.
TRANSITIONS = {
    PENDING: (ATTEMPTING, PENDING, HELD),
    ATTEMPTING: (DELIVERED, HELD),
    DELIVERED: (),
    HELD: (),
}

# Only PENDING may be claimed.  HELD is terminal for the deliverer (an operator
# reconcile is the sole exit) and DELIVERED is finished.
CLAIMABLE_STATES = (PENDING,)

# reconcile_held() accepts exactly these two words and nothing else.
CONFIRM_UNSENT = "confirm_unsent"
CONFIRM_ABANDON = "confirm_abandon"
RECONCILE_WORDS = (CONFIRM_UNSENT, CONFIRM_ABANDON)

class IllegalTransition(Exception):
    """Raised when a caller asks for a state change the machine does not allow."""


# --- schema ----------------------------------------------------------------
# The part-1 outbox has no delivery columns at all.  They are added in place
# (ALTER TABLE, one column at a time) so an old file opens exactly like a new
# one and no existing row is rewritten.
DELIVERY_COLUMNS = (
    ("state", "TEXT NOT NULL DEFAULT 'PENDING'"),
    ("attempt_at", "TEXT"),
    ("held_at", "TEXT"),
    ("evidence", "TEXT"),
    ("abandoned", "INTEGER NOT NULL DEFAULT 0"),
    ("last_error", "TEXT"),  # part 1 already declares it; kept for old files
    ("nonce", "TEXT"),
)


def connect(db_path):
    """Open the part-1 store and bring the outbox up to the delivery schema.

    Invariant: delegates to ``wake_enrol.connect`` for the WAL/FULL pragmas and
    the enrolment schema, then adds the delivery columns additively.
    """
    conn = we.connect(db_path)
    ensure_schema(conn)
    return conn


def ensure_schema(conn):
    """Create the part-1 schema if needed, then add any missing delivery columns.

    Invariant: purely additive and idempotent - a file written before this
    release keeps its rows and gains the columns with their defaults
    (``state='PENDING'``, ``abandoned=0``), so old undelivered rows become
    claimable exactly once.
    """
    we.ensure_schema(conn)
    existing = {row[1] for row in conn.execute("PRAGMA table_info(outbox)")}
    for name, declared in DELIVERY_COLUMNS:
        if name in existing:
            continue
        conn.execute("ALTER TABLE outbox ADD COLUMN %s %s" % (name, declared))
    conn.commit()


# --- row helpers -----------------------------------------------------------
ROW_COLUMNS = (
    "admission_id",
    "nonce",
    "task_key",
    "target",
    "session_id",
    "attempts",
    "state",
    "attempt_at",
    "held_at",
    "delivered_at",
    "evidence",
    "abandoned",
    "last_error",
)


def _fetch(conn, admission_id):
    """Return the delivery row as a dict, or None when the admission is unknown."""
    row = conn.execute(
        "SELECT %s FROM outbox WHERE admission_id = ?" % ", ".join(ROW_COLUMNS),
        (admission_id,),
    ).fetchone()
    if row is None:
        return None
    return dict(zip(ROW_COLUMNS, row))


# --- claim -----------------------------------------------------------------
def claim(conn, admission_id, now):
    """Take ownership of one row for one attempt.

    Returns ``('CLAIMED', row)``, ``('IN_FLIGHT',)``, ``('HELD',)``,
    ``('DELIVERED',)`` or ``('UNKNOWN',)``.  Never raises for a missing row.

    Invariants:
    * the ``PENDING -> ATTEMPTING`` write is committed *here*, before any
      transport call, so a kill after this point can never leave the row
      looking unsent;
    * the write is conditional on the state still being PENDING, so a second
      claimer (or a replay) sees ``rowcount == 0`` instead of taking a row a
      live attempt already owns;
    * an already ``ATTEMPTING`` row is a stranded attempt *only once its lease
      has expired* - past that the previous owner is taken to be dead and the
      row is parked ``HELD`` rather than resent, because whether the browser
      saw it is exactly what we cannot know.  Inside the lease it is someone
      else's live attempt: answer ``IN_FLIGHT`` and write nothing, or the loser
      would strand the winner's row and turn a good delivery into a false HELD;
    * ``HELD`` is terminal and is reported, never claimed.
    """
    if not isinstance(admission_id, str) or not admission_id:
        return (UNKNOWN,)

    _, now_text = we._now_pair(now)
    row = _fetch(conn, admission_id)
    if row is None:
        return (UNKNOWN,)

    state = row["state"]
    if state == DELIVERED:
        return (DELIVERED,)
    if state == ATTEMPTING:
        if row["nonce"] is None or row["nonce"] == "":
            return _strand(conn, admission_id, now_text)
        if not _lease_expired(row["attempt_at"], now_text):
            return (IN_FLIGHT,)
        return _strand(conn, admission_id, now_text)
    if state not in CLAIMABLE_STATES:
        if state == HELD:
            return (HELD,)
        return (UNKNOWN,)

    nonce = row["nonce"]
    if nonce is None or nonce == "":
        conn.execute(
            "UPDATE outbox SET state = ?, held_at = ?, last_error = ? "
            "WHERE admission_id = ? AND state = ?",
            (HELD, now_text, "legacy row has no nonce provenance", admission_id, state),
        )
        conn.commit()
        return (HELD,)
    cursor = conn.execute(
        "UPDATE outbox SET state = ?, attempt_at = ?, last_error = NULL, nonce = ? "
        "WHERE admission_id = ? AND state = ?",
        (ATTEMPTING, now_text, nonce, admission_id, state),
    )
    # The commit is the point of no return: once it lands, a crash can only
    # ever strand the row in ATTEMPTING, never resurrect it as PENDING.
    conn.commit()
    if cursor.rowcount != 1:
        return (UNKNOWN,)
    return (CLAIMED, _fetch(conn, admission_id))


def _lease_expired(attempt_at, now_text):
    """True when an ATTEMPTING stamp can no longer be honoured as live.

    Invariant: an unparseable ``attempt_at`` (or ``now``) returns True, so a
    corrupt timestamp fails closed into the stranded path and can never wedge a
    row in ATTEMPTING forever.
    """
    try:
        began, _ = we._now_pair(attempt_at)
        moment, _ = we._now_pair(now_text)
    except ValueError:
        return True
    age = (moment - began).total_seconds()
    return age < -60 or age >= LEASE_SECONDS


def _strand(conn, admission_id, now_text):
    """Park a row found in ATTEMPTING as HELD and report ``('HELD',)``.

    Invariant: conditional on the row still being ATTEMPTING, so it can only
    downgrade a stranded attempt - it never touches a row a live claimer moved
    on in the meantime.
    """
    cursor = conn.execute(
        "UPDATE outbox SET state = ?, held_at = ?, last_error = ? "
        "WHERE admission_id = ? AND state = ?",
        (
            HELD,
            now_text,
            "stranded ATTEMPTING: previous attempt died; it may or may not have landed",
            admission_id,
            ATTEMPTING,
        ),
    )
    conn.commit()
    if cursor.rowcount != 1:
        return (UNKNOWN,)
    return (HELD,)


# --- finish ----------------------------------------------------------------
def finish(conn, admission_id, result, evidence, now):
    """Apply the one legal transition for a transport ``result``.

    ``result`` is one of ``OK``, ``BUSY``, ``UNCERTAIN``, ``BINDING_UNRESOLVED``
    or ``REFUSED_GUARD_DISABLED``.  Returns the state the row now has as a
    1-tuple: ``('DELIVERED',)``, ``('PENDING',)`` or ``('HELD',)``.

    Invariants:
    * ``OK`` is only accepted from ATTEMPTING and only with a non-empty
      readback evidence string; an ``OK`` without evidence is treated as
      ``UNCERTAIN`` (the browser may have seen it, we cannot prove it);
    * ``BUSY``, ``UNCERTAIN``, ``BINDING_UNRESOLVED``, ``REFUSED_GUARD_DISABLED``
      and unknown outcomes are held with ``attempts + 1`` and a ``held_at`` stamp;
      only ``reconcile_held`` can re-arm a held row;
    * every write is conditional on the state the transition was validated
      against, so a row that changed under us raises instead of being
      overwritten;
    * a transition from a state the table does not list raises
      ``IllegalTransition`` - ``PENDING -> DELIVERED`` and anything from HELD
      or DELIVERED included.
    """
    if result not in RESULT_STATE:
        raise ValueError("unknown transport result: %r" % (result,))

    _, now_text = we._now_pair(now)
    row = _fetch(conn, admission_id)
    if row is None:
        raise IllegalTransition(
            "no outbox row for %r; finish() cannot invent one" % (admission_id,)
        )

    state = row["state"]
    target = RESULT_STATE[result]

    if result == OK and state != ATTEMPTING:
        raise IllegalTransition(
            "OK can only follow ATTEMPTING, not %s (%s)" % (state, admission_id)
        )
    if result in (UNCERTAIN, BINDING_UNRESOLVED) and state != ATTEMPTING:
        raise IllegalTransition(
            "%s can only follow ATTEMPTING, not %s (%s)" % (result, state, admission_id)
        )
    if target not in TRANSITIONS[state]:
        raise IllegalTransition(
            "%s -> %s is not a legal transition (%s)" % (state, target, admission_id)
        )

    if result == OK and not _has_evidence(evidence):
        return _hold(conn, admission_id, "OK without readback evidence", now_text)

    if target == DELIVERED:
        cursor = conn.execute(
            "UPDATE outbox SET state = ?, delivered_at = ?, evidence = ?, "
            "held_at = NULL, last_error = NULL, attempts = attempts + 1 "
            "WHERE admission_id = ? AND state = ?",
            (DELIVERED, now_text, evidence, admission_id, ATTEMPTING),
        )
    elif target == HELD:
        return _hold(conn, admission_id, _error_text(result, evidence), now_text)

    conn.commit()
    if cursor.rowcount != 1:
        raise IllegalTransition(
            "%s changed under finish(%s); refusing a blind overwrite" % (admission_id, result)
        )
    return (target,)


def _hold(conn, admission_id, error_text, now_text):
    """Write ATTEMPTING -> HELD with the reason; return ``('HELD',)``."""
    cursor = conn.execute(
        "UPDATE outbox SET state = ?, held_at = ?, attempts = attempts + 1, "
        "last_error = ? WHERE admission_id = ? AND state = ?",
        (HELD, now_text, error_text, admission_id, ATTEMPTING),
    )
    conn.commit()
    if cursor.rowcount != 1:
        raise IllegalTransition(
            "%s changed under finish(-> HELD); refusing a blind overwrite" % (admission_id,)
        )
    return (HELD,)


def _has_evidence(evidence):
    """True only for a non-empty string - the readback proof the transport must carry."""
    return isinstance(evidence, str) and bool(evidence.strip())


def _error_text(result, evidence):
    """Render a failure reason as ``RESULT: detail`` with the detail optional."""
    detail = evidence.strip() if _has_evidence(evidence) else ""
    return ("%s: %s" % (result, detail)) if detail else result


# --- operator reconcile ----------------------------------------------------
def reconcile_held(conn, admission_id, allow, now):
    """The only exit from HELD, and only on an explicit operator word.

    ``allow`` must be exactly ``'confirm_unsent'`` (the operator has established
    that nothing reached the browser: the row returns to PENDING with its
    attempts reset and the decision recorded) or ``'confirm_abandon'`` (the
    wake is written off: the row stays HELD and is flagged ``abandoned`` so it
    can never be picked up again).  Any other value raises ``ValueError``; a
    row that is not HELD raises ``IllegalTransition``.

    Invariant: this is operator-only by construction - it is never called from
    ``deliver_once`` or ``claim``, so no code path can talk itself out of HELD.
    """
    if allow not in RECONCILE_WORDS:
        raise ValueError(
            "reconcile_held requires allow='confirm_unsent' or "
            "'confirm_abandon', got %r" % (allow,)
        )

    _, now_text = we._now_pair(now)
    row = _fetch(conn, admission_id)
    if row is None:
        raise IllegalTransition("no outbox row for %r" % (admission_id,))
    if row["state"] != HELD:
        raise IllegalTransition(
            "reconcile_held only applies to HELD rows; %s is %s"
            % (admission_id, row["state"])
        )

    if allow == CONFIRM_UNSENT:
        conn.execute(
            "UPDATE outbox SET state = ?, attempts = 0, held_at = NULL, "
            "abandoned = 0, last_error = ? WHERE admission_id = ? AND state = ?",
            (
                PENDING,
                "operator:confirm_unsent @ %s (attempts reset)" % now_text,
                admission_id,
                HELD,
            ),
        )
        conn.commit()
        return (PENDING,)

    conn.execute(
        "UPDATE outbox SET abandoned = 1, last_error = ? "
        "WHERE admission_id = ? AND state = ?",
        ("operator:confirm_abandon @ %s" % now_text, admission_id, HELD),
    )
    conn.commit()
    return (HELD,)


# --- deliver once ----------------------------------------------------------
def deliver_once(
    conn,
    admission_id,
    transport,
    target_url,
    expected_url,
    guard_enabled,
    now,
):
    """Attempt exactly one physical send of this admission's wake.

    Returns the refusal / claim / finish 1-tuple, so the caller always reads
    the outcome as ``result[0]``: ``'REFUSED_GUARD_DISABLED'``,
    ``'BINDING_UNRESOLVED'``, ``'IN_FLIGHT'``, ``'HELD'``, ``'DELIVERED'``,
    ``'UNKNOWN'``, ``'PENDING'``.

    Invariants:
    * the guard is checked first and refuses without touching the row, so a
      disabled guard leaves a PENDING row exactly as it was;
    * the binding is compared by exact string equality before the claim; a
      mismatch parks the row HELD without ever having claimed or sent;
    * ``IN_FLIGHT`` means another deliverer holds a live lease on this row: do
      not send, do not increment, do not write - return it unchanged, so the
      winner's own finish() still finds the state it owns;
    * the envelope is ``{admission_id, session_id, target, nonce}`` and the
      nonce is persisted before the send;
    * any exception from the transport is ``UNCERTAIN``, not a retry: the
      claim was already committed, so the only honest answer is HELD.
    """
    if not guard_enabled:
        return (REFUSED_GUARD_DISABLED,)

    if target_url != expected_url:
        _park_unresolved(conn, admission_id, target_url, expected_url, now)
        return (BINDING_UNRESOLVED,)

    claimed = claim(conn, admission_id, now)
    if claimed[0] != CLAIMED:
        return claimed

    row = claimed[1]
    envelope = {
        "admission_id": row["admission_id"],
        "session_id": row["session_id"],
        "target": target_url,
        "nonce": row["nonce"],
    }

    try:
        result, evidence = transport.send(envelope)
    except Exception as exc:  # the attempt already happened; we cannot retry it
        return finish(
            conn,
            admission_id,
            UNCERTAIN,
            "%s: %s" % (type(exc).__name__, exc),
            now,
        )

    if result not in RESULT_STATE:
        return finish(
            conn,
            admission_id,
            UNCERTAIN,
            "unknown transport result: %r" % (result,),
            now,
        )
    return finish(conn, admission_id, result, evidence, now)


def _park_unresolved(conn, admission_id, target_url, expected_url, now):
    """Hold a PENDING row whose binding does not match, without claiming it.

    Invariant: only a PENDING row is touched; a HELD / DELIVERED row is left
    exactly as it is, because the refusal already stops the send.
    """
    _, now_text = we._now_pair(now)
    conn.execute(
        "UPDATE outbox SET state = ?, held_at = ?, last_error = ? "
        "WHERE admission_id = ? AND state = ?",
        (
            HELD,
            now_text,
            "binding unresolved: target %r != expected %r" % (target_url, expected_url),
            admission_id,
            PENDING,
        ),
    )
    conn.commit()
