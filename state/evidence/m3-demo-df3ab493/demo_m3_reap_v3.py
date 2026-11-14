"""M3 operational demonstration v3 - counts, not booleans; no live pids; mutation-receipt ready.

Additions this version carries beyond v2 (each asked for by the Coordinator):
  * CLEARS is a COUNT of the UPDATEs that actually cleared this run's stored pid, measured by sqlite's own
    trace callback on the disposable connection - never a boolean, and never inferred from the final NULL.
  * FIXTURE PROVENANCE is printed: every pid used is a child this process spawned; the real board's pids are
    never read, so "no live pid" is stated as a fact about the code path, not a promise.
  * PRODUCTION ROW COUNTS are pasted before and after for both the disposable copy and the real board, with
    the real board's full path, so isolation is measured.
  * the reused-pid case synthesises the SHAPE of the live Edge rows: our own process, its real fingerprint
    recorded, then a deliberately wrong one presented to the reaper.

Run: python demo_m3_reap_v3.py --repo <checkout> [--case A|B|C|all] [--label <text>] [--emit-dir <dir>]
"""
import hashlib
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
from pathlib import Path

CREATE_NO_WINDOW = 0x08000000 if os.name == "nt" else 0
CHILD = [sys.executable, "-c", "import time; time.sleep(300)"]
PROD_DB = Path(os.path.expanduser("~/.hermes/kanban.db"))
WRONG_FP = "epoch-deliberately-wrong|0"


def spawn_child():
    proc = subprocess.Popen(CHILD, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                            stdin=subprocess.DEVNULL, creationflags=CREATE_NO_WINDOW)
    time.sleep(0.6)
    return proc


def alive(pid):
    if os.name == "nt":
        import ctypes
        handle = ctypes.windll.kernel32.OpenProcess(0x1000, False, int(pid))
        if not handle:
            return False
        code = ctypes.c_ulong()
        ok = ctypes.windll.kernel32.GetExitCodeProcess(handle, ctypes.byref(code))
        ctypes.windll.kernel32.CloseHandle(handle)
        return bool(ok) and code.value == 259
    try:
        os.kill(int(pid), 0)
        return True
    except OSError:
        return False


def file_sha(path):
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def prod_counts():
    conn = sqlite3.connect(f"file:{PROD_DB}?mode=ro", uri=True)
    out = {t: conn.execute(f"SELECT count(*) FROM {t}").fetchone()[0]
           for t in ("tasks", "task_runs", "task_events")}
    conn.close()
    return out


class ClearCounter:
    """Counts the UPDATEs that clear a run's stored pid, from sqlite's executed statements."""

    def __init__(self):
        self.statements = []

    def __call__(self, sql):
        if sql:
            self.statements.append(sql)

    def clears_for(self, run_id):
        n = 0
        for sql in self.statements:
            norm = " ".join(sql.split())
            if norm.startswith("UPDATE task_runs") and "worker_pid = NULL" in norm \
                    and f"id = {int(run_id)}" in norm.replace("id=", "id = "):
                n += 1
        return n


def disposable(source, tmpdir, name):
    dst = Path(tmpdir) / f"{name}.db"
    shutil.copyfile(source, dst)
    conn = sqlite3.connect(str(dst))
    conn.row_factory = sqlite3.Row
    for table in ("task_events", "task_runs", "tasks"):
        try:
            conn.execute(f"DELETE FROM {table}")
        except sqlite3.Error:
            pass
    conn.commit()
    return conn, dst


def counts(conn):
    return {t: conn.execute(f"SELECT count(*) FROM {t}").fetchone()[0]
            for t in ("tasks", "task_runs", "task_events")}


def seed(conn, *, task_id, run_id, claim_lock, stored_pid, stored_fp, bindings):
    now = int(time.time())
    conn.execute("INSERT INTO tasks (id, title, status, created_at, current_run_id, worker_pid) VALUES (?,?,?,?,?,?)",
                 (task_id, "m3 demo " + task_id, "blocked", now, run_id, stored_pid))
    conn.execute("INSERT INTO task_runs (id, task_id, status, started_at, worker_pid, worker_started_at, claim_lock, ended_at) "
                 "VALUES (?,?,?,?,?,?,?,?)",
                 (run_id, task_id, "blocked", now - 900, stored_pid, stored_fp, claim_lock, now - 600))
    for pid, fp in bindings:
        conn.execute("INSERT INTO task_events (task_id, run_id, kind, payload, created_at) VALUES (?,?,?,?,?)",
                     (task_id, run_id, "worker_bound", json.dumps({"pid": pid, "started_at": fp}), now))
    conn.commit()


def run_state(conn, run_id, task_id):
    row = conn.execute("SELECT worker_pid, worker_started_at FROM task_runs WHERE id=?", (run_id,)).fetchone()
    events = [r[0] for r in conn.execute(
        "SELECT kind FROM task_events WHERE task_id=? AND run_id=? ORDER BY id", (task_id, run_id))]
    return row, events


def main():
    argv = sys.argv
    repo = Path(argv[argv.index("--repo") + 1]).resolve()
    only = argv[argv.index("--case") + 1].upper() if "--case" in argv else "ALL"
    label = argv[argv.index("--label") + 1] if "--label" in argv else ""
    emit = Path(argv[argv.index("--emit-dir") + 1]).resolve() if "--emit-dir" in argv else None
    sys.path.insert(0, str(repo))
    from hermes_cli import kanban_db_dispatch as dispatch

    host = dispatch._kb._host_prefix()
    lines, children, roles = [], [], {}
    prc = 0

    def say(text):
        print(text)
        lines.append(text)

    prod_before_counts = prod_counts()
    prod_before_sha = file_sha(PROD_DB)
    tmpdir = tempfile.mkdtemp(prefix="m3demo-")
    say(f"LABEL {label or '(none)'}")
    say(f"REPO {repo}")
    say(f"MODULE {dispatch.__file__}")
    say(f"HOST_PREFIX {host}")
    say(f"PRODUCTION_BOARD {PROD_DB}")
    say(f"PRODUCTION_BOARD_SHA256_BEFORE {prod_before_sha}")
    say(f"PRODUCTION_BOARD_COUNTS_BEFORE {json.dumps(prod_before_counts)}")
    say("FIXTURE_PROVENANCE every pid in this run is a child spawned by this script; no board pid is read or used")
    try:
        if only in ("B", "C", "ALL"):
            foreign = spawn_child(); children.append(foreign); roles["foreign"] = foreign.pid
            roles["foreign_real_fingerprint"] = dispatch._process_fingerprint(foreign.pid)

        if only in ("A", "ALL"):
            conn, dbp = disposable(PROD_DB, tmpdir, "success")
            counter = ClearCounter(); conn.set_trace_callback(counter)
            before = counts(conn)
            launcher = spawn_child(); children.append(launcher); roles["launcher"] = launcher.pid
            worker = spawn_child(); children.append(worker); roles["worker"] = worker.pid
            lfp = dispatch._process_fingerprint(launcher.pid)
            wfp = dispatch._process_fingerprint(worker.pid)
            seed(conn, task_id="t_demo_ok", run_id=901, claim_lock=host + "demo",
                 stored_pid=launcher.pid, stored_fp=lfp,
                 bindings=[(launcher.pid, lfp), (worker.pid, wfp)])
            _, ev0 = run_state(conn, 901, "t_demo_ok")
            dispatch.reap_terminal_workers(conn)
            row, ev1 = run_state(conn, 901, "t_demo_ok")
            after = counts(conn)
            clears = counter.clears_for(901)
            reaped = ev1.count("terminal_worker_reaped")
            ok = (row[0] is None and clears == 1 and reaped == 2
                  and not alive(launcher.pid) and not alive(worker.pid))
            say(f"DB_DISPOSABLE {dbp}")
            say(f"DB_COUNTS_BEFORE {json.dumps(before)}")
            say(f"DB_COUNTS_AFTER  {json.dumps(after)}")
            say(f"CASE A SUCCESS ok={ok} launcher={launcher.pid} worker={worker.pid} "
                f"launcher_fp_verified={lfp} worker_fp_verified={wfp} stored_pid_after={row[0]} "
                f"CLEARS_PER_RUN={clears} terminal_worker_reaped_events={reaped} "
                f"launcher_alive={alive(launcher.pid)} worker_alive={alive(worker.pid)} "
                f"events_before={ev0} events_after={ev1}")
            conn.set_trace_callback(None); conn.close()
            prc = 1 if not ok else prc

        if only in ("B", "ALL"):
            conn, dbp = disposable(PROD_DB, tmpdir, "failure")
            counter = ClearCounter(); conn.set_trace_callback(counter)
            before = counts(conn)
            unver = spawn_child(); children.append(unver); roles["unverified"] = unver.pid
            verd = spawn_child(); children.append(verd); roles["verified"] = verd.pid
            vfp = dispatch._process_fingerprint(verd.pid)
            seed(conn, task_id="t_demo_hold", run_id=902, claim_lock=host + "demo",
                 stored_pid=verd.pid, stored_fp=vfp,
                 bindings=[(unver.pid, dispatch.UNVERIFIED_WORKER_FINGERPRINT), (verd.pid, vfp)])
            dispatch.reap_terminal_workers(conn)
            row, ev = run_state(conn, 902, "t_demo_hold")
            after = counts(conn)
            clears = counter.clears_for(902)
            ok = row[0] == verd.pid and alive(unver.pid) and clears == 0
            say(f"DB_DISPOSABLE {dbp}")
            say(f"DB_COUNTS_BEFORE {json.dumps(before)}")
            say(f"DB_COUNTS_AFTER  {json.dumps(after)}")
            say(f"CASE B FAILURE-UNRESOLVED ok={ok} unverified_pid={unver.pid} verified_pid={verd.pid} "
                f"unverified_binding_fingerprint=UNVERIFIED stored_pid_after={row[0]} expected={verd.pid} "
                f"CLEARS_PER_RUN={clears} unverified_alive={alive(unver.pid)} events={ev}")
            conn.set_trace_callback(None); conn.close()
            prc = 1 if not ok else prc

        if only in ("C", "ALL"):
            conn, dbp = disposable(PROD_DB, tmpdir, "reused")
            counter = ClearCounter(); conn.set_trace_callback(counter)
            before = counts(conn)
            seed(conn, task_id="t_demo_reused", run_id=903, claim_lock=host + "demo",
                 stored_pid=roles["foreign"], stored_fp=WRONG_FP,
                 bindings=[(roles["foreign"], WRONG_FP)])
            dispatch.reap_terminal_workers(conn)
            row, ev = run_state(conn, 903, "t_demo_reused")
            after = counts(conn)
            clears = counter.clears_for(903)
            ok = (row[0] == roles["foreign"] and alive(roles["foreign"]) and clears == 0
                  and "PID_REUSED" in ev and "terminal_worker_reaped" not in ev)
            say(f"DB_DISPOSABLE {dbp}")
            say(f"DB_COUNTS_BEFORE {json.dumps(before)}")
            say(f"DB_COUNTS_AFTER  {json.dumps(after)}")
            say(f"CASE C FAILURE-REUSED ok={ok} synthesised_from_shape_of_live_edge_rows=true "
                f"foreign_pid={roles['foreign']} its_REAL_fingerprint={roles['foreign_real_fingerprint']} "
                f"presented_fingerprint={WRONG_FP} stored_pid_after={row[0]} CLEARS_PER_RUN={clears} "
                f"foreign_alive={alive(roles['foreign'])} PID_REUSED={'PID_REUSED' in ev} events={ev}")
            conn.set_trace_callback(None); conn.close()
            prc = 1 if not ok else prc
    finally:
        for c in children:
            try:
                if alive(c.pid):
                    c.kill()
            except Exception:
                pass
        time.sleep(0.5)
        leftovers = [c.pid for c in children if alive(c.pid)]
        prod_after_counts = prod_counts()
        prod_after_sha = file_sha(PROD_DB)
        say(f"CLEANUP ok={leftovers == []} leftover_test_processes={leftovers}")
        say(f"PRODUCTION_BOARD_COUNTS_AFTER {json.dumps(prod_after_counts)}")
        say(f"PRODUCTION_BOARD_SHA256_AFTER {prod_after_sha} equal_before={prod_after_sha == prod_before_sha} "
            f"(observation only: the live board moves under measurement; this script opens it with copyfile+hash only)")
        say("PRODUCTION_BOARD_WRITE_TARGET no - every connection created here points at the disposable path printed above")
        if leftovers:
            prc = 1
        shutil.rmtree(tmpdir, ignore_errors=True)
        say(f"DISPOSABLE_TMPDIR_REMOVED {tmpdir}")
    say(f"EXIT {prc}")
    if emit:
        emit.mkdir(parents=True, exist_ok=True)
        out = (emit / (label or "transcript")).with_suffix(".txt")
        out.write_text("\n".join(lines) + "\n", encoding="utf-8")
        print(f"WROTE {out}")
    return prc


if __name__ == "__main__":
    sys.exit(main())
