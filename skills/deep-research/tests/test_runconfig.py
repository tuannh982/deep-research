"""run.yaml is the run's control panel. Every bound in its schema is
there because something downstream divides by, indexes with, or loops
over the value."""
import pytest
import yaml

import confidence
import runconfig


def test_default_carries_the_question(tmp_path):
    cfg = runconfig.default("Why is the sky blue?")
    assert cfg["question"] == "Why is the sky blue?"


def test_default_matches_the_spec_thresholds():
    """Spec sections 2, 4 and 6 fix these. A silent drift here changes
    when the loop halts and what gets promoted."""
    config = runconfig.default("q")["config"]
    assert config["max_depth"] == 4
    assert config["max_parallel"] == 6
    # 0.5 since the score reads distinct sources: gate 3's minimum
    # (2 distinct domains) is 2/(2+2). The invariant is that this
    # equals that, which test_the_default_bar... below asserts.
    assert config["promotion_threshold"] == 0.67
    assert config["required_domains"] == 2
    assert config["min_citations"] == 3
    assert config["saturation_window"] == 6
    assert config["saturation_branches"] == 2
    assert config["max_attempts"] == 3


def test_default_assigns_a_model_to_every_agent():
    models = runconfig.default("q")["models"]
    assert sorted(models) == sorted(runconfig.AGENTS)


def test_every_task_kind_maps_to_an_agent():
    """A dispatchable task whose kind has no agent would stall the loop
    with no way to make progress."""
    import json
    from pathlib import Path
    schema = json.loads(
        (Path(__file__).resolve().parents[1] / "schemas" / "task.json")
        .read_text(encoding="utf-8"))
    kinds = schema["properties"]["kind"]["enum"]
    assert sorted(runconfig.KIND_AGENT) == sorted(kinds)
    assert set(runconfig.KIND_AGENT.values()) <= set(runconfig.AGENTS)


def test_default_starts_with_no_signals_and_tick_zero():
    cfg = runconfig.default("q")
    assert cfg["signals"] == {"stop_requested": False, "stop_when": None,
                              "checkpoints": []}
    assert cfg["status"]["tick"] == 0
    assert cfg["status"]["halted"] is None


def test_save_then_load_round_trips(tmp_path):
    cfg = runconfig.default("q")
    cfg["scope"]["in_scope"] = ["a", "b"]
    runconfig.save(tmp_path, cfg)
    assert runconfig.load(tmp_path) == cfg


def test_save_writes_readable_yaml(tmp_path):
    runconfig.save(tmp_path, runconfig.default("q"))
    raw = (tmp_path / "run.yaml").read_text(encoding="utf-8")
    assert yaml.safe_load(raw)["question"] == "q"
    assert "question:" in raw


def test_load_of_a_missing_file_raises_config_error(tmp_path):
    with pytest.raises(runconfig.ConfigError, match="run.yaml"):
        runconfig.load(tmp_path)


def test_load_validates(tmp_path):
    cfg = runconfig.default("q")
    cfg["config"]["max_depth"] = -1
    (tmp_path / "run.yaml").write_text(yaml.safe_dump(cfg), encoding="utf-8")
    with pytest.raises(runconfig.ConfigError, match="max_depth"):
        runconfig.load(tmp_path)


def test_required_domains_zero_is_rejected_at_load(tmp_path):
    """Carry-forward (d). It no longer divides anything in
    confidence.compute — that was the spread term — but gate 3's
    independence() still asks for at least this many distinct domains,
    and zero would mean no independence bar at all."""
    cfg = runconfig.default("q")
    cfg["config"]["required_domains"] = 0
    (tmp_path / "run.yaml").write_text(yaml.safe_dump(cfg), encoding="utf-8")
    with pytest.raises(runconfig.ConfigError, match="required_domains"):
        runconfig.load(tmp_path)


def test_the_value_rejected_at_load_no_longer_crashes_compute(tmp_path):
    """INVERTED, and the old docstring said to do this: "Pins the reason
    for the bound, not just the bound. If confidence.compute is ever
    made zero-safe, this test says so."

    It is. `required_domains` left the arithmetic — it was the divisor
    in `spread = min(1, distinct/required_domains)` and the score now
    reads distinct sources directly — so 0 no longer raises. The
    schema's `minimum: 1` still stands, but on gate 3's own terms:
    independence() asks for at least one distinct domain."""
    assert confidence.compute(["a.com"], "supported", required_domains=0) > 0


@pytest.mark.parametrize("field", [
    "max_parallel", "min_citations", "saturation_window",
    "saturation_branches", "max_attempts",
])
def test_counting_fields_reject_zero(tmp_path, field):
    cfg = runconfig.default("q")
    cfg["config"][field] = 0
    with pytest.raises(runconfig.ConfigError, match=field):
        runconfig.validate(cfg)


def test_promotion_threshold_above_one_is_rejected(tmp_path):
    cfg = runconfig.default("q")
    cfg["config"]["promotion_threshold"] = 1.5
    with pytest.raises(runconfig.ConfigError, match="promotion_threshold"):
        runconfig.validate(cfg)


def test_an_unknown_config_key_is_rejected(tmp_path):
    cfg = runconfig.default("q")
    cfg["config"]["token_budget"] = 100000
    with pytest.raises(runconfig.ConfigError, match="token_budget"):
        runconfig.validate(cfg)


def test_an_unknown_agent_in_models_is_rejected():
    cfg = runconfig.default("q")
    cfg["models"]["summariser"] = "haiku"
    with pytest.raises(runconfig.ConfigError, match="summariser"):
        runconfig.validate(cfg)


def test_an_unknown_phase_is_rejected():
    cfg = runconfig.default("q")
    cfg["status"]["phase"] = "vibing"
    with pytest.raises(runconfig.ConfigError, match="phase"):
        runconfig.validate(cfg)


# --- reachability warnings -------------------------------------------

def test_the_default_config_warns_about_nothing():
    assert runconfig.warnings(runconfig.default("q")) == []


def test_a_gate_three_bar_below_the_promotion_threshold_warns():
    """Hand-computed. The best case gate 3 admits is `required_domains`
    distinct domains with a `supported` verdict — base =
    2/(2+2) = 0.5 — so a threshold above that is unreachable: gate 3
    keeps calling evidence sufficient while nothing is ever promoted,
    and the loop spawns more search tasks forever.

    Raising min_citations no longer triggers this, which is the point of
    the formula change: more quotes from the same two sites are not more
    independence, so they cannot rescue an unreachable threshold."""
    cfg = runconfig.default("q")
    cfg["config"]["promotion_threshold"] = 0.8
    assert confidence.compute(["a.com", "b.com"], "supported") == 0.44
    assert any("promotion_threshold" in w for w in runconfig.warnings(cfg))


def test_the_default_bar_exactly_meets_the_default_threshold():
    """Hand-computed. 3 citations over 2 DISTINCT domains, verdict
    supported: base = distinct/(distinct+2) = 2/4 = 0.5, weight = 1.0,
    so 0.5 — exactly promotion_threshold. The defaults are tight, not
    slack, which is why the warning above is worth having. The third
    citation does not enter: it is a second quote from a source already
    counted."""
    assert confidence.compute(["a.com", "a.com", "b.com"], "supported") == 0.67


def test_warnings_do_not_raise():
    cfg = runconfig.default("q")
    cfg["config"]["min_citations"] = 1
    runconfig.validate(cfg)  # still a legal config
    assert runconfig.warnings(cfg)


# --- required_domains 1 switches gate 3 off ---------------------------

def test_required_domains_of_one_is_warned_about():
    """Legal, and it switches gate 3's independence bar off:
    independence() asks for at least `required_domains` distinct
    domains, so at 1 spec section 9's adversarial case — three citations
    that are really one source — clears it exactly as if they were three
    independent domains. Nothing downstream can tell them apart, and
    nothing warned.

    The score no longer reads this value at all, which makes the warning
    MORE necessary rather than less: gate 3 is now the only thing
    standing between a single-source claim and promotion."""
    cfg = runconfig.default("q")
    cfg["config"]["required_domains"] = 1
    found = runconfig.warnings(cfg)
    assert any("required_domains" in w and "independence" in w for w in found)


def test_the_default_config_does_not_warn_about_independence():
    assert not any("independence" in w
                   for w in runconfig.warnings(runconfig.default("q")))


def test_every_task_kind_has_an_agent():
    """A kind the scheduler cannot route is a task that can never be
    dispatched, and build_packet reports it as skipped forever."""
    import json
    from pathlib import Path
    schema = json.loads((Path(runconfig.SCHEMA_DIR) / "task.json")
                        .read_text(encoding="utf-8"))
    for kind in schema["properties"]["kind"]["enum"]:
        assert kind in runconfig.KIND_AGENT


def test_every_agent_named_by_kind_agent_has_a_model():
    for agent in runconfig.KIND_AGENT.values():
        assert agent in runconfig.DEFAULT_MODELS
