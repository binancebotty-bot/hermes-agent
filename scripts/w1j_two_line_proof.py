"""Staging-only proof: the exact two-line outbound envelope never settles itself."""
import importlib
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "plugins"))
sys.path.insert(0, str(ROOT / "tests"))
E = importlib.import_module("wake-emitter.emitter")
from test_binding import CONV_B, GEN, _bind, _load, _payload, _rows, _worker

fake_id = "11111111-1111-4111-8111-111111111111"
envelope = (
    "RC-w1j-%s | EVENT-ID: %s WAKE from Hermes (hermes:mcp-integration)\n"
    "Reply with this exact line and nothing else: WAKE-ACK %s"
    % (fake_id, fake_id, fake_id)
)
print("line 1:", repr(envelope.splitlines()[0]))
print("line 2:", repr(envelope.splitlines()[1]))
print("_ack_reply_half(envelope):", repr(E._ack_reply_half(envelope)))
print("_ack_candidate(envelope, id) =", E._ack_candidate(envelope, fake_id))
print("_ack_candidate('WAKE-ACK id', id) =",
      E._ack_candidate("WAKE-ACK " + fake_id, fake_id))

worker, journal = _worker(Path(tempfile.mkdtemp(prefix="w1j-proof-")), _load())
_bind(worker, "w1j-proof-task", conv=CONV_B, generation=GEN)
sends = []

def stub(argv):
    sends.append(argv)
    return "DELIVERED_UNACKED"

worker._run_sender = stub
worker.on_session_end(**_payload("w1j-proof-task", turn_id="turn-proof"))
eid = [r for r in _rows(journal) if r.get("kind") == "event"][-1]["event_id"]
real_envelope = (
    "RC-w1j-%s | EVENT-ID: %s WAKE from Hermes (hermes:mcp-integration)\n"
    "Reply with this exact line and nothing else: WAKE-ACK %s" % (eid, eid, eid)
)
worker._read_pane_turns = lambda lane, timeout, window=None: [{"text": real_envelope}]
worker.on_session_end(**_payload("noop", turn_id=None, completed=False))
names = [r.get("outcome") for r in _rows(journal)
         if r.get("kind") == "outcome" and r.get("event_id") == eid]
print("sweep outcomes:", names)
print("self-settled:", any(n in ("DELIVERED_ACKED", "ACKED_ORDER_ONLY") for n in names))
print("sender invocations:", len(sends))
