"""Staging-only regression table for W1r: variant -> expected -> actual, ACK matcher.

Exits 1 if any ACTUAL differs from EXPECTED (the hole or a false negative reopened).
"""
import importlib
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "plugins"))
E = importlib.import_module("wake-emitter.emitter")

EID = "11111111-1111-4111-8111-111111111111"
SEP = "ChatGPT said:"
ACK = "WAKE-ACK " + EID
TWO_LINE = ("EVENT-ID: %s\nWAKE from Hermes (hermes:mcp-integration). Reply with this "
            "exact line and nothing else: WAKE-ACK %s" % (EID, EID))
ONE_LINE = ("RC-w1r-%s | EVENT-ID: %s WAKE from Hermes (hermes:mcp-integration)"
            % (EID, EID))
LINE2 = ("WAKE from Hermes (hermes:mcp-integration). Reply with this exact line "
         "and nothing else: WAKE-ACK " + EID)
QUOTE2 = "> " + LINE2

CASES = [
    # (variant, text, expected _ack_candidate, why)
    ("our two-line payload", TWO_LINE, False, "self-ACK"),
    ("our one-line payload", ONE_LINE, False, "self-ACK"),
    ("our line 2 alone", LINE2, False, "self-ACK (instruction, not a reply)"),
    ("payload + SEP + payload", TWO_LINE + "\n\n" + SEP + "\n" + TWO_LINE, False,
     "self-ACK quoted back"),
    ("payload + SEP + line 2", TWO_LINE + "\n\n" + SEP + "\n" + LINE2, False,
     "line 2 quoted back"),
    ("payload + SEP + nothing", TWO_LINE + "\n\n" + SEP, False, "empty reply half"),
    ("line 2 + SEP + nothing", LINE2 + "\n\n" + SEP + "\n", False, "empty reply half"),
    ("nonce + SEP + line 2", "EVENT-ID: %s\n\n%s\n%s" % (EID, SEP, LINE2), False,
     "self-ACK line 2"),
    ("payload + SEP + nonce echo", TWO_LINE + "\n\n" + SEP + "\nEVENT-ID: " + EID, False,
     "reply half has no keyword"),
    ("payload + SEP + WAKE-ACK  [REPORTED]", TWO_LINE + "\n\n" + SEP + "\n" + ACK, True,
     "combined render"),
    ("one-line + SEP + WAKE-ACK", ONE_LINE + "\n\n" + SEP + "\n" + ACK, True,
     "combined render"),
    ("quote(line 2) + SEP + WAKE-ACK", QUOTE2 + "\n\n" + SEP + "\n" + ACK, True,
     "quote then comply, no envelope marker"),
    ("quote(payload) + SEP + WAKE-ACK  [REPORTED]",
     "> " + TWO_LINE.replace("\n", "\n> ") + "\n\n" + SEP + "\n" + ACK, True,
     "quote then comply, envelope marker in quote"),
    ("WAKE-ACK alone", ACK, True, "genuine reply"),
    ("SEP + WAKE-ACK", SEP + "\n" + ACK, True, "genuine reply"),
]

bad = 0
print("%-46s %-9s %-7s %s" % ("variant", "expected", "actual", "why"))
print("-" * 92)
for name, text, expected, why in CASES:
    actual = E._ack_candidate(text, EID)
    flag = "" if actual is expected else "   <-- MISMATCH"
    bad += actual is not expected
    print("%-46s %-9s %-7s %s%s" % (name, expected, actual, why, flag))
print()
print("mismatches:", bad)
sys.exit(1 if bad else 0)
