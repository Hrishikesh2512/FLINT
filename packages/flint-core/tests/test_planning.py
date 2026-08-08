"""Plans with real data flow, validated before anything runs."""

from __future__ import annotations

import pytest

from flint_core.planning import Plan, PlanError, PlanStep


def plan(*steps, goal="do the thing"):
    return Plan.from_dict({"goal": goal, "steps": list(steps)})


def step(tool="web_search", **parameters):
    return {"tool": tool, "description": f"run {tool}", "parameters": parameters}


# ── the thing that was impossible before ────────────────────────────────────
def test_a_step_can_use_an_earlier_result():
    p = plan(
        step("web_search", query="mechanical engineering"),
        step("file_controller", action="write", name="notes.txt",
             content="{{step:1}}"),
    )
    resolved = p.resolve(p.steps[1], {1: "SEARCH RESULTS HERE"})
    assert resolved["content"] == "SEARCH RESULTS HERE"
    assert resolved["name"] == "notes.txt"       # untouched


def test_several_results_can_be_combined():
    p = plan(
        step("web_search", query="a"),
        step("web_search", query="b"),
        step("file_controller", action="write",
             content="First:\n{{step:1}}\n\nSecond:\n{{step:2}}"),
    )
    resolved = p.resolve(p.steps[2], {1: "AAA", 2: "BBB"})
    assert resolved["content"] == "First:\nAAA\n\nSecond:\nBBB"


def test_the_original_goal_can_be_referenced():
    p = plan(step("summarise", text="{{step:1}}", about="{{goal}}"),
             goal="explain quantum tunnelling")
    resolved = p.resolve(p.steps[0], {})
    assert resolved["about"] == "explain quantum tunnelling"


def test_references_are_found_inside_nested_parameters():
    p = plan(step("x", options={"body": ["{{step:1}}", "literal"]}))
    assert p.steps[0].references() == frozenset({1})


def test_nested_parameters_are_resolved_too():
    p = plan(step("a"), step("x", options={"body": ["{{step:1}}", "literal"]}))
    resolved = p.resolve(p.steps[1], {1: "VALUE"})
    assert resolved["options"]["body"] == ["VALUE", "literal"]


def test_whitespace_in_a_reference_is_tolerated():
    """Models write {{ step: 1 }} about as often as {{step:1}}."""
    p = plan(step("a"), step("b", content="{{ step: 1 }}"))
    assert p.steps[1].references() == frozenset({1})
    assert p.resolve(p.steps[1], {1: "X"})["content"] == "X"


def test_a_reference_to_an_empty_result_substitutes_nothing():
    """A tool handed a literal "{{step:2}}" would write it to disk as success."""
    p = plan(step("a"), step("b", content="{{step:1}}"))
    assert p.resolve(p.steps[1], {1: None})["content"] == ""
    assert p.resolve(p.steps[1], {})["content"] == ""


def test_non_string_parameters_pass_through_untouched():
    p = plan(step("x", count=5, flag=True, ratio=1.5))
    resolved = p.resolve(p.steps[0], {})
    assert resolved == {"count": 5, "flag": True, "ratio": 1.5}


# ── validation, before anything has happened ────────────────────────────────
def test_a_reference_to_a_missing_step_is_caught():
    p = plan(step("a"), step("b", content="{{step:5}}"))
    problems = p.problems()
    assert len(problems) == 1
    assert "references step 5" in problems[0] and "has 2 step" in problems[0]


def test_a_forward_reference_is_caught():
    """Reading the future means the author misunderstood the task."""
    p = plan(step("a", content="{{step:2}}"), step("b"))
    assert "has not run yet" in p.problems()[0]


def test_a_self_reference_is_caught():
    p = plan(step("a", content="{{step:1}}"))
    assert "references itself" in p.problems()[0]


def test_an_unknown_tool_is_caught():
    p = plan(step("nonexistent_tool"))
    assert "no such tool" in p.problems(known_tools={"web_search"})[0]


def test_a_valid_plan_has_no_problems():
    p = plan(step("web_search", query="x"),
             step("file_controller", content="{{step:1}}"))
    assert p.problems(known_tools={"web_search", "file_controller"}) == []
    assert p.validate({"web_search", "file_controller"}) is p


def test_validate_raises_with_every_problem_listed():
    p = plan(step("a", content="{{step:9}}"), step("b", content="{{step:1}}"))
    with pytest.raises(PlanError) as caught:
        p.validate()
    assert "references step 9" in str(caught.value)


def test_problems_are_phrased_so_a_planner_can_use_them():
    """The retry prompt is the whole reason these are returned, not raised."""
    p = plan(step("a"), step("b", content="{{step:7}}"))
    assert p.problems()[0] == (
        "step 2 references step 7, but the plan has 2 step(s)")


# ── parsing ─────────────────────────────────────────────────────────────────
def test_step_numbers_come_from_position_not_the_model():
    """Models number steps 0-based, skip, and repeat. Position is the truth."""
    p = Plan.from_dict({"goal": "g", "steps": [
        {"step": 0, "tool": "a"}, {"step": 7, "tool": "b"}, {"step": 7, "tool": "c"}]})
    assert [s.step for s in p] == [1, 2, 3]


def test_a_plan_with_no_steps_is_rejected():
    with pytest.raises(PlanError, match="no steps"):
        Plan.from_dict({"goal": "g", "steps": []})
    with pytest.raises(PlanError, match="no steps"):
        Plan.from_dict({"goal": "g"})


def test_a_step_without_a_tool_is_rejected():
    with pytest.raises(PlanError, match="has no tool"):
        Plan.from_dict({"goal": "g", "steps": [{"description": "vague"}]})


def test_non_object_input_is_rejected():
    with pytest.raises(PlanError, match="not an object"):
        Plan.from_dict(["not", "a", "plan"])
    with pytest.raises(PlanError, match="step 1 is not an object"):
        Plan.from_dict({"steps": ["just a string"]})


def test_bad_parameters_are_rejected():
    with pytest.raises(PlanError, match="parameters must be an object"):
        Plan.from_dict({"steps": [{"tool": "x", "parameters": "nope"}]})


def test_missing_parameters_default_to_empty():
    p = Plan.from_dict({"steps": [{"tool": "current_time"}]})
    assert p.steps[0].parameters == {}


def test_the_goal_falls_back_to_the_one_given():
    p = Plan.from_dict({"steps": [{"tool": "x"}]}, goal="the real goal")
    assert p.goal == "the real goal"


def test_steps_are_critical_unless_said_otherwise():
    p = Plan.from_dict({"steps": [{"tool": "a"},
                                  {"tool": "b", "critical": False}]})
    assert (p.steps[0].critical, p.steps[1].critical) == (True, False)


# ── reporting ───────────────────────────────────────────────────────────────
def test_describe_shows_the_dependencies():
    p = plan(step("web_search", query="x"),
             step("file_controller", content="{{step:1}}"),
             goal="research and save")
    described = p.describe()
    assert "Goal: research and save" in described
    assert "(uses step 1)" in described


def test_a_plan_is_a_sequence():
    p = plan(step("a"), step("b"))
    assert len(p) == 2
    assert [s.tool for s in p] == ["a", "b"]


def test_a_step_needs_a_tool_even_when_built_directly():
    with pytest.raises(PlanError, match="has no tool"):
        PlanStep(step=1, tool="  ")
