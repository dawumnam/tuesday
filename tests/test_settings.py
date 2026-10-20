from task_design.gateway import Role
from task_design.settings import Provider, Settings, ThinkingLevel


def test_defaults_route_every_role_to_gemini():
    s = Settings(_env_file=None)
    assert s.use_local_llm is False
    for role in Role:
        assert s.route(role).provider is Provider.GEMINI
    assert s.route(Role.GENERATION).thinking is ThinkingLevel.HIGH
    assert (s.solvable_min_score, s.discrimination_min_gap, s.grading_max_stdev) == (70, 30, 5)
    assert (s.duplicate_threshold, s.max_rounds) == (0.85, 3)


def test_env_overrides_thresholds_and_a_single_role(monkeypatch):
    monkeypatch.setenv("TD_SOLVABLE_MIN_SCORE", "65")
    monkeypatch.setenv("TD_ROUTING__GRADING__MODEL", "other-model")
    s = Settings(_env_file=None)
    assert s.solvable_min_score == 65
    assert s.route(Role.GRADING).model == "other-model"
    assert s.route(Role.GENERATION).model != "other-model"


def test_local_llm_switch_moves_only_examinee_and_ai_tool(monkeypatch):
    monkeypatch.setenv("TD_USE_LOCAL_LLM", "true")
    s = Settings(_env_file=None)
    local = {r for r in Role if s.route(r).provider is Provider.LOCAL}
    assert local == {Role.EXAMINEE, Role.AI_TOOL}
