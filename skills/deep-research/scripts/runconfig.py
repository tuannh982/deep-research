"""run.yaml: load, save, validate.

The one loader. Every command reads the run's thresholds through here, so
a bound in schemas/run.json is a bound the whole system gets — which is
why the numeric limits live in the schema rather than in an `if` somewhere.
`required_domains: 0` used to reach confidence.compute and raise
ZeroDivisionError three days into a run.
"""
import json
from pathlib import Path

import jsonschema
import yaml

import atomicio
import confidence as confidence_mod
import memory as memory_mod

RUN_FILENAME = "run.yaml"

AGENTS = ("decomposer", "searcher", "extractor", "rechecker",
          "hypothesizer", "verifier", "outliner", "synthesizer")

# task.kind -> the agent that answers it. Spec section 5's table, plus the
# two synthesis kinds. `outline` exists because code cannot dispatch a
# subagent: the outliner has to be a task in the graph for the loop to
# reach it, exactly as gate 4's adversarial verifier is.
KIND_AGENT = {
    "decompose": "decomposer",
    "search": "searcher",
    "extract": "extractor",
    # Gate 2. A separate agent from the extractor on purpose: it re-reads
    # the page with no sight of the extraction and no idea what the quote
    # is meant to prove, which is the only independence left once the
    # check stopped being a second code path.
    "recheck": "rechecker",
    "hypothesize": "hypothesizer",
    "verify": "verifier",
    "outline": "outliner",
    "synthesize": "synthesizer",
}

DEFAULT_MODELS = {
    "decomposer": "sonnet",
    "searcher": "haiku",
    "extractor": "haiku",
    # "is this string on this page" needs no judgement, only care.
    "rechecker": "haiku",
    "hypothesizer": "sonnet",
    "verifier": "sonnet",
    "outliner": "sonnet",
    "synthesizer": "sonnet",
}

SCHEMA_DIR = Path(__file__).resolve().parent.parent / "schemas"


class ConfigError(ValueError):
    """run.yaml is missing, unparseable, or does not satisfy its schema."""


class RunConfig(dict):
    """A plain dict. Named so signatures can say what they take."""


# PyYAML's SafeDumper only recognises the exact type `dict`, not
# subclasses of it, so a bare RunConfig fails `yaml.safe_dump` with
# "cannot represent an object" even though it is a dict in every way
# that matters. save() also wraps in dict(cfg) defensively, but this
# registration keeps any other yaml.safe_dump(cfg) call working too.
yaml.SafeDumper.add_representer(RunConfig, yaml.SafeDumper.represent_dict)


def _schema():
    return json.loads((SCHEMA_DIR / "run.json").read_text(encoding="utf-8"))


def default(question, models=None):
    return RunConfig({
        "version": 1,
        "question": question,
        "created_at": memory_mod.utcnow(),
        "scope": {"in_scope": [], "out_of_scope": [], "success_criteria": []},
        "config": {
            "max_depth": 4,
            "max_parallel": 6,
            "promotion_threshold": 0.67,
            "required_domains": 2,
            "min_citations": 3,
            "saturation_window": 6,
            "saturation_branches": 2,
            "max_attempts": 3,
            "fetch_timeout": 20,
            "agent_timeout": 600,
        },
        "models": dict(models or DEFAULT_MODELS),
        "preflight": {"uv": "present", "tectonic": "present"},
        "signals": {"stop_requested": False, "stop_when": None,
                    "checkpoints": []},
        "status": {"phase": "scope", "tick": 0, "halted": None},
    })


def validate(cfg):
    try:
        jsonschema.validate(cfg, _schema())
    except jsonschema.ValidationError as error:
        where = "/".join(str(p) for p in error.absolute_path) or "<root>"
        raise ConfigError(f"run.yaml: {where}: {error.message}") from None


def warnings(cfg):
    """Legal configurations that will not behave the way the user expects.

    Not errors — the run proceeds. Surfaced by `research status` and by
    `init`, because the alternative is discovering the mismatch after
    three days of research.
    """
    found = []
    config = cfg["config"]
    # Gate 3 admits evidence at exactly (min_citations, required_domains).
    # Its best case is that many citations on that many distinct domains
    # with a `supported` verdict. If that scores below the promotion
    # threshold, gate 3 keeps calling evidence sufficient while nothing is
    # ever promoted, and the loop spawns more search tasks forever.
    #
    # Since the score reads DISTINCT sources rather than citation volume,
    # the padding duplicates below no longer move it and `best` is really
    # required_domains/(required_domains+2). They are kept so this still
    # constructs the literal evidence gate 3 admits rather than a
    # shorthand for it — if the formula changes again, this keeps asking
    # the real question.
    best = confidence_mod.compute(
        [f"d{i}.example" for i in range(config["required_domains"])]
        + ["d0.example"] * max(
            0, config["min_citations"] - config["required_domains"]),
        "supported",
        required_domains=config["required_domains"],
        min_citations=config["min_citations"],
    )
    if best < config["promotion_threshold"]:
        found.append(
            f"config.promotion_threshold {config['promotion_threshold']} is "
            f"unreachable at min_citations={config['min_citations']} and "
            f"required_domains={config['required_domains']}: the best "
            f"score gate 3 admits is {best}. Nothing will ever be promoted."
        )
    if config["required_domains"] > config["min_citations"]:
        found.append(
            f"config.required_domains {config['required_domains']} exceeds "
            f"min_citations {config['min_citations']}: gate 3 asks for more "
            "distinct domains than citations, which no evidence can satisfy."
        )
    # Legal, and it switches off the one gate that measures source
    # independence. `required_domains` no longer appears in the score at
    # all — the schema's minimum of 1 used to be there because
    # confidence.compute divided by it, and now it is gate 3's own bar
    # and nothing else. At 1, gates.independence accepts three citations
    # that are all the same site: spec section 9's adversarial case,
    # admitted silently.
    if config["required_domains"] < 2:
        found.append(
            f"config.required_domains {config['required_domains']} disables "
            "gate 3's independence check: a hypothesis can be promoted on "
            "citations that all come from one registrable domain. Set it "
            "to 2 or more to require corroboration from a different site."
        )
    return found


def path_for(root):
    return Path(root) / RUN_FILENAME


def load(root):
    path = path_for(root)
    if not path.is_file():
        raise ConfigError(f"no {RUN_FILENAME} at {path}; run `research init` first")
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as error:
        raise ConfigError(f"{path} is not valid YAML: {error}") from None
    if not isinstance(raw, dict):
        raise ConfigError(f"{path} must be a mapping")
    cfg = RunConfig(raw)
    validate(cfg)
    return cfg


def save(root, cfg):
    validate(cfg)
    return atomicio.write_text(
        path_for(root),
        yaml.safe_dump(dict(cfg), sort_keys=False, allow_unicode=True),
    )
