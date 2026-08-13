"""Provider registry and the contract every provider satisfies.

The core knows nothing about Slurm, Encord, GCP or any other tool. It knows
that a watch names a *provider*, and that a provider can be asked to observe a
reference and report back in a canonical vocabulary.

Adding a tool is therefore never a change to the core: register a provider, or
drop a Python file into `$TAM_HOME/providers/`. For many services no code is
needed at all -- the generic `command` and `http` providers cover anything with
a CLI or a REST endpoint.
"""

import json
from dataclasses import dataclass, field
from pathlib import Path

from ..errors import ValidationError

# Canonical states. Providers translate their own vocabulary into these so that
# the digest, alerts and queries never learn any one tool's words.
RUNNING = "running"
PENDING = "pending"
SUCCEEDED = "succeeded"
FAILED = "failed"
STOPPED = "stopped"
PRESENT = "present"
MISSING = "missing"
UNREACHABLE = "unreachable"
UNKNOWN = "unknown"

STATES = (RUNNING, PENDING, SUCCEEDED, FAILED, STOPPED, PRESENT, MISSING,
          UNREACHABLE, UNKNOWN)

#: States that mean "a human should probably look at this".
ATTENTION = (FAILED, STOPPED, MISSING, UNREACHABLE)
#: States that mean the work is over, successfully or not.
TERMINAL = (SUCCEEDED, FAILED, STOPPED, MISSING)


@dataclass
class Observation:
    """What a provider saw. `state` must be one of STATES."""

    state: str = UNKNOWN
    native_state: str | None = None
    detail: dict = field(default_factory=dict)
    metrics: dict = field(default_factory=dict)
    step: int | None = None

    def __post_init__(self):
        if self.state not in STATES:
            raise ValidationError(
                f"provider returned unknown state {self.state!r}; "
                f"expected one of {', '.join(STATES)}")

    def to_dict(self):
        return {"state": self.state, "native_state": self.native_state,
                "detail": self.detail, "metrics": self.metrics, "step": self.step}


class Provider:
    """Base class. Subclass, set `name`, implement `probe`.

    `probe` receives the watch as a plain dict (ref, host, config, label) and
    must return an Observation. It must not raise for ordinary failure -- an
    unreachable host is an observation, not an error.
    """

    name = ""
    description = ""
    #: Human hint shown by `tam provider list`.
    ref_hint = "reference"

    def probe(self, watch):                      # pragma: no cover - interface
        raise NotImplementedError

    # -- helpers available to every provider ------------------------------
    @staticmethod
    def config_of(watch):
        cfg = watch.get("config")
        if isinstance(cfg, str):
            try:
                return json.loads(cfg)
            except json.JSONDecodeError:
                return {}
        return cfg or {}


_REGISTRY = {}


def register(provider):
    """Register a provider instance or class. Later registrations win."""
    inst = provider() if isinstance(provider, type) else provider
    if not inst.name:
        raise ValidationError("provider must define a name")
    _REGISTRY[inst.name] = inst
    return inst


def get(name):
    _ensure_builtins()
    if name not in _REGISTRY:
        raise ValidationError(
            f"unknown provider {name!r}; available: {', '.join(available())}",
            field="provider")
    return _REGISTRY[name]


def available():
    _ensure_builtins()
    return sorted(_REGISTRY)


def describe():
    _ensure_builtins()
    return [{"name": p.name, "description": p.description,
             "ref_hint": p.ref_hint} for p in
            sorted(_REGISTRY.values(), key=lambda p: p.name)]


_builtins_loaded = False


def _ensure_builtins():
    global _builtins_loaded
    if not _builtins_loaded:
        _builtins_loaded = True
        from . import builtin  # noqa: F401 - registers on import


def load_user_providers(directory):
    """Execute every .py in `directory` so it can call register().

    This is the extension point that keeps tool-specific code out of the tree:
    drop `encord.py` in `$TAM_HOME/providers/` and it is available everywhere,
    with no change to TAM itself.
    """
    directory = Path(directory)
    if not directory.is_dir():
        return []
    _ensure_builtins()
    loaded = []
    for path in sorted(directory.glob("*.py")):
        if path.name.startswith("_"):
            continue
        namespace = {"__file__": str(path), "__name__": f"tam_provider_{path.stem}"}
        try:
            exec(compile(path.read_text(), str(path), "exec"), namespace)
            loaded.append(path.name)
        except Exception as exc:  # noqa: BLE001 - a bad plugin must not kill TAM
            import sys
            sys.stderr.write(f"tam: provider {path.name} failed to load: {exc!r}\n")
    return loaded
