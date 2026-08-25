"""The answer key is unreachable from the parts of the system being graded.

`CaseTruth` holds what would have happened -- whether the customer was going to
pay anyway, how likely contact is to work, the right day to act. If any component
that makes decisions could read it, the report card would be measuring a system
that cheats, and every number in it would be worthless.

The separation is enforced here rather than promised in a docstring, and it is
checked with the import graph rather than with a text search, so a docstring that
merely *mentions* the class does not trip it and an `import` that hides behind an
alias does not slip past it.
"""

import ast
from pathlib import Path

import pytest

from app.config import PROJECT_ROOT

APP = PROJECT_ROOT / "app"

# The referee and its paperwork. These are allowed to know the answers because
# their whole job is marking, and none of them can influence a decision.
MAY_READ_TRUTH = {
    "models.py",     # defines it
    "cohort.py",     # writes it
    "simulator.py",  # the referee
    "report.py",     # the report card
    "runner.py",     # hands rows from the loader to the referee, never to a policy
}

# Everything that decides, plans, gates or acts.
DECIDERS = ["watcher.py", "policies.py", "guard.py", "thinker.py", "actions.py", "doer.py"]

FORBIDDEN_NAMES = {"CaseTruth", "would_pay_unprompted", "p_pay_if_contacted",
                   "p_pay_if_retried", "best_wait_days"}


def imported_names(path: Path) -> set[str]:
    """Every name this module pulls in, however it was spelled."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            for alias in node.names:
                names.add(alias.name)
        elif isinstance(node, ast.Import):
            for alias in node.names:
                names.add(alias.name.split(".")[-1])
    return names


def attribute_accesses(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    return {
        node.attr for node in ast.walk(tree) if isinstance(node, ast.Attribute)
    } | {
        node.id for node in ast.walk(tree) if isinstance(node, ast.Name)
    }


@pytest.mark.parametrize("filename", DECIDERS)
def test_no_decision_making_module_can_reach_the_answer_key(filename):
    path = APP / filename
    assert path.exists(), filename

    leaked = imported_names(path) & FORBIDDEN_NAMES
    assert not leaked, (
        f"{filename} imports {sorted(leaked)}. Recoup is not allowed to see what "
        "would have happened -- that is the counterfactual the report card is "
        "built on, and a policy that can read it is not being measured, it is "
        "being handed the answers."
    )

    touched = attribute_accesses(path) & FORBIDDEN_NAMES
    assert not touched, f"{filename} touches {sorted(touched)} without importing it"


def test_the_list_of_referees_is_the_whole_list():
    """If a new module starts reading the truth table, this test makes it a
    decision someone had to write down rather than something that just happened."""
    readers = set()
    for path in APP.glob("*.py"):
        if FORBIDDEN_NAMES & (imported_names(path) | attribute_accesses(path)):
            readers.add(path.name)

    unexpected = readers - MAY_READ_TRUTH
    assert not unexpected, (
        f"{sorted(unexpected)} reads CaseTruth and is not on the referee list in "
        "tests/test_isolation.py. Either it should not, or the list needs updating "
        "on purpose."
    )


def test_the_planner_prompt_never_contains_the_answer():
    """The signal handed to the model is the same one handed to the baselines."""
    from app import cohort, watcher
    from app.thinker import Thinker

    rows = cohort.build_cohort(size=20)
    signals = watcher.scan([case for case, _ in rows], as_of=cohort.AS_OF)
    brain = Thinker(offline=True)

    for signal in signals:
        prompt = brain.prompt_for(signal)
        for forbidden in FORBIDDEN_NAMES:
            assert forbidden not in prompt
        # And the words too, not just the field names.
        assert "unprompted" not in prompt.lower()
        assert "recoverable" not in prompt or "recoverability" in prompt
