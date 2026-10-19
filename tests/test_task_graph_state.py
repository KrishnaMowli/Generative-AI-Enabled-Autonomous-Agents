import itertools

import pytest

from autonomous_collab.errors import IllegalTransitionError
from autonomous_collab.logging_utils import EventLog
from autonomous_collab.models import TaskSpec, TaskState
from autonomous_collab.task_graph import find_cycle, graph_depth, topological_order, transitive_dependencies
from autonomous_collab.task_state import ALLOWED_TRANSITIONS, TaskTracker


def spec(tid, deps=()):
    return TaskSpec(task_id=tid, task_type="visual_inspection", target_zone="A", required_capabilities=["visual_inspection"],
                    depends_on=list(deps), priority=3, estimated_duration_s=30, acceptance_criteria=["inspection_result_recorded"])


def test_find_cycle_variants():
    assert find_cycle({"a": [], "b": ["a"], "c": ["a", "b"]}) is None
    assert find_cycle({"a": ["a"]}) == ["a", "a"]
    cyc = find_cycle({"a": ["c"], "b": ["a"], "c": ["b"], "d": []})
    assert cyc[0] == cyc[-1] and set(cyc) == {"a", "b", "c"}
    assert find_cycle({"a": ["ghost"]}) is None  # unknown references are the validator's job


def test_topological_order_respects_dependencies_and_priority():
    deps = {"report": ["a", "b"], "a": [], "b": ["a"], "z": []}
    order = topological_order(deps, {"z": 5, "a": 1, "b": 1, "report": 1})
    assert order.index("a") < order.index("b") < order.index("report") and order[0] == "z"
    with pytest.raises(ValueError):
        topological_order({"a": ["b"], "b": ["a"]})


def test_depth_and_transitive_dependencies():
    deps = {"a": [], "b": ["a"], "c": ["b"], "d": ["a"]}
    assert graph_depth(deps) == 3 and graph_depth({}) == 0
    assert transitive_dependencies(deps, "c") == {"a", "b"} and transitive_dependencies(deps, "a") == set()
    with pytest.raises(ValueError):
        graph_depth({"a": ["b"], "b": ["a"]})


@pytest.mark.parametrize("src,dst", list(itertools.product(TaskState, TaskState)))
def test_every_transition_pair_is_allowed_or_rejected(src, dst):
    log = EventLog()
    tracker = TaskTracker([spec("t")], log)
    tracker.get("t").state = src
    if dst in ALLOWED_TRANSITIONS[src]:
        tracker.transition("t", dst, 3, reason="x")
        assert tracker.get("t").state == dst and log.of_type("task_transition")[-1]["to_state"] == dst.value
    else:
        with pytest.raises(IllegalTransitionError):
            tracker.transition("t", dst, 3)
        assert tracker.get("t").state == src and not log.records


def test_succeeded_is_terminal_and_recovery_only_via_pending():
    assert ALLOWED_TRANSITIONS[TaskState.SUCCEEDED] == frozenset()
    assert ALLOWED_TRANSITIONS[TaskState.FAILED] == {TaskState.PENDING}
    assert ALLOWED_TRANSITIONS[TaskState.BLOCKED] == {TaskState.PENDING}


def test_tracker_dependency_and_duplicate_handling():
    tracker = TaskTracker([spec("a"), spec("b", ["a"])], EventLog())
    assert not tracker.deps_succeeded("b")
    tracker.get("a").state = TaskState.SUCCEEDED
    assert tracker.deps_succeeded("b")
    with pytest.raises(ValueError):
        tracker.add(spec("a"))
    assert tracker.deps_map() == {"a": [], "b": ["a"]}
