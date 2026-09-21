"""Closed vocabularies that live in more than one file must agree.

Each of these is a value set one module owns and another module repeats:
adding a decision kind means touching four places, and a node status that
is not in the right terminal set makes a workflow report the wrong outcome.
Nothing here tests behaviour; it tests that the copies did not drift.
"""
import re
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import fusion_core as core
import fusion_decision_cli
import fusion_decisions
import fusion_workflow


ROOT = Path(__file__).resolve().parents[1]
# Routing's options are the live candidate lanes, so it has no fixed question
# set to probe and no entry in the CLI's map. Any other kind missing from
# either is drift.
DYNAMIC_KINDS = {"routing"}
FAILURE_CLASSES = {"quota", "permission_denied", "timeout", "missing_executable", "worker_error"}


def cli_parser():
    import argparse
    parser = argparse.ArgumentParser()
    fusion_decision_cli.add_parser(parser.add_subparsers(dest="command"))
    return parser


def probe_kind_choices():
    for action in cli_parser()._subparsers._group_actions[0].choices["decisions"]._subparsers._group_actions[0].choices["probe"]._actions:
        if action.dest == "kind":
            return set(action.choices)
    raise AssertionError("decisions probe has no --kind argument")


def cli_question_kinds():
    """The kinds the CLI can actually build questions for, read from its source.

    The map is a local inside run(); importing it is not possible without
    executing the command, so this reads the literal keys.
    """
    source = (ROOT / "fusion_decision_cli.py").read_text(encoding="utf-8")
    match = re.search(r"questions = \{(.+?)\}\[args\.kind\]", source, re.S)
    assert match, "could not find the probe questions map in fusion_decision_cli.py"
    return set(re.findall(r'"([a-z_]+)":', match[1]))


class CoherenceTest(unittest.TestCase):
    def test_every_decision_kind_is_probeable_and_has_questions(self):
        expected = fusion_decisions.KINDS - DYNAMIC_KINDS
        self.assertEqual(probe_kind_choices(), expected)
        self.assertEqual(cli_question_kinds(), expected)

    def test_auto_actions_error_names_every_kind(self):
        try:
            fusion_decisions.config_for({"decisions": {"auto_actions": ["not-a-kind"]}})
        except ValueError as error:
            message = str(error)
        else:
            self.fail("an unknown auto_action must be rejected")
        for kind in fusion_decisions.KINDS:
            self.assertIn(kind, message, f"{kind} is missing from the auto_actions error")

    def test_node_statuses_all_belong_to_a_terminal_set(self):
        known = (fusion_workflow.TERMINAL_SUCCESS | fusion_workflow.TERMINAL_FAILURE
                 | fusion_workflow.TERMINAL_PAUSED | {"pending", "running"})
        source = (ROOT / "fusion_workflow.py").read_text(encoding="utf-8")
        # Every literal the runner assigns to a node's status, however it is spelled.
        assigned = set(re.findall(r'node\["status"\] = "([a-z_]+)"', source))
        assigned |= set(re.findall(r'"status": "([a-z_]+)"', source))
        assigned |= set(re.findall(r'status = "([a-z_]+)"', source))
        unknown = assigned - known - {"error"}  # dispatch failures carry a result status, not a node status
        self.assertEqual(unknown, set(), f"node statuses outside every terminal set: {sorted(unknown)}")

    def test_final_status_classifies_every_terminal_status(self):
        # A terminal status the runner can write but _final_status cannot see
        # leaves a finished workflow reporting "running" forever.
        for status in fusion_workflow.TERMINAL_FAILURE | fusion_workflow.TERMINAL_PAUSED:
            runner = object.__new__(fusion_workflow.WorkflowRunner)
            runner.nodes = {"only": {"status": status}}
            runner.spec = {"acceptance": {}}
            runner.workspace = ROOT
            self.assertNotEqual(fusion_workflow.WorkflowRunner._final_status(runner), "running",
                                f"_final_status does not classify {status}")

    def test_failure_class_returns_only_declared_categories(self):
        samples = [
            {"status": "success", "blockers": []},
            {"status": "error", "blockers": ["permission denied: Write(/etc/hosts)"]},
            {"status": "error", "blockers": ["You've hit your usage limit; resets at 5:10am"]},
            {"status": "error", "blockers": ["something nobody has seen before"]},
            {"status": "error", "blockers": [], "exit_code": 124},
            {"status": "error", "blockers": ["codex is not available on PATH"]},
            {"status": "blocked", "blockers": []},
            {},
        ]
        seen = {core.failure_class(sample) for sample in samples}
        self.assertTrue(seen <= FAILURE_CLASSES | {None}, f"undeclared failure classes: {seen - FAILURE_CLASSES - {None}}")
        # The two routing treats as lane-disqualifying must stay reachable.
        self.assertIn("quota", seen)
        self.assertIn("permission_denied", seen)


if __name__ == "__main__":
    unittest.main()
