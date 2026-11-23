import sys, threading, tempfile, os
from pathlib import Path

ROOT = Path(r"C:\Users\wigmore\.hermes\hermes-agent")
sys.path.insert(0, str(ROOT))

import hermes_constants
hermes_constants._cached_default_hermes_root = None

import kanban_db_connect as kbc
import kanban_db as kb
from kanban_db import close_review_loop
def run_case():
    home = Path(tempfile.mkdtemp()) / "hermes_home"
    home.mkdir()
    os.environ["HERMES_HOME"] = str(home)
    for var in ("HERMES_KANBAN_DB","HERMES_KANBAN_WORKSPACES_ROOT","HERMES_KANBAN_HOME","HERMES_KANBAN_BOARD"):
        os.environ.pop(var, None)
    kb._INITIALIALIZED_PATHS.clear()
    c = kbc.connect()
    try:
        fix = kb.create_task(c, title="FIX", body="x", assignee="default",
                             provider_override="openrouter", model_override="nvidia/nemotron-3-ultra-550b-a55b",
                             idempotency_key="fix-seed")
        review = kb.create_task(c, title="REVIEW", body="r", assignee="default", parents=(fix,))
        results = []
        def worker():
            r = close_review_loop(c, review, "CHANGES_REQUIRED", "sha-1111-2222",
                                  blockers=["b1"], created_by="user", from_envelope=None)
            results.append(r)
        threads = [threading.Thread(target=worker) for _ in range(2)]
        for t in threads: t.start()
        for t in threads: t.join()
        n = c.execute("SELECT COUNT(*) FROM task_links WHERE parent_id=?", (review,)).fetchone()[0]
        print("REWORK_CARDS:", n)
        for r in results:
            print("  verdict=%s idempotent=%s generation=%s rework_card=%s" % (
                r.get("verdict"), r.get("idempotent"), r.get("generation"), r.get("rework_card")))
        c.close()
    finally:
        os.environ.pop("HERMES_HOME", None)

if __name__ == "__main__":
    run_case()
