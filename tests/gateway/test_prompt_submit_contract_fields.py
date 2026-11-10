"""The wire contract must accept the conditional-admission fields (reviewer defect 3).

The gateway rejects unknown keys with 4000/4001 before any handler runs, so a field that exists only in the handler is
a field no caller can ever send. These assertions read the contract source rather than instantiating the model, because
pydantic cannot be imported in this environment (see evidence: ImportError on pydantic_core.ArgsKwargs).
"""

import ast
import pathlib

CONTRACT = pathlib.Path(__file__).resolve().parents[2] / "tui_gateway" / "contracts" / "prompt_voice.py"


def _annotations() -> dict:
    tree = ast.parse(CONTRACT.read_text(encoding="utf-8"))
    for node in tree.body:
        if isinstance(node, ast.ClassDef) and node.name == "PromptSubmitParams":
            return {n.target.id: ast.unparse(n.annotation) for n in node.body if isinstance(n, ast.AnnAssign)}
    raise AssertionError("PromptSubmitParams not found")


def test_contract_declares_the_conditional_admission_fields():
    ann = _annotations()
    assert ann.get("expect_turn") == "int | None", ann.get("expect_turn")
    assert ann.get("expect_generation") == "int | None", ann.get("expect_generation")


def test_contract_still_forbids_unknown_keys():
    """The fields were added to the model, not smuggled past it: extra keys remain forbidden."""
    base = (CONTRACT.parent / "base.py").read_text(encoding="utf-8")
    assert 'extra="forbid"' in base


def test_params_whitelist_and_contract_agree():
    """A field accepted by the handler but not the contract (or the reverse) is the exact bug this guards."""
    controls = (CONTRACT.parents[2] / "gateway" / "session_controls.py").read_text(encoding="utf-8")
    assert "'expect_turn'" in controls and "'expect_generation'" in controls
