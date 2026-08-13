"""Configuration and filesystem layout.

The install root is resolved once, in this order:
  1. $TAM_HOME
  2. the parent of the `tam` package (i.e. the repo checkout)

Everything mutable lives under <root>/data, which is gitignored.
"""

import json
import os
import socket
from dataclasses import dataclass, field, asdict
from pathlib import Path

#: Sentinel meaning "nobody chose a name" -- resolved to agent@<hostname>.
GENERIC_ACTOR = "agent"

DEFAULTS = {
    "default_project": "TAM",
    "actor": None,
    "timezone": "Asia/Jerusalem",
    "stale_days": 7,
    "wip_limit": 3,
    "digest_dir": "data/digests",
    "api_host": "127.0.0.1",
    "api_port": 8787,
    # Extra Host header values the API will answer to, beyond the addresses it
    # can discover itself. Needed where DNS and the serving interface disagree.
    "api_allowed_hosts": [],
    # Host allowed to open the database file directly. Set by `tam init` when
    # the database lives on a network filesystem, where multi-host access
    # loses writes. Other hosts must go through the API.
    "db_host": None,
}


def default_actor():
    """`agent@<hostname>` — distinguishable without anyone maintaining names.

    Actor answers "which worker"; the issue key already answers "which task"
    and `source` answers "which run". Deriving it means there is no registry of
    names to keep, while still telling two machines apart in the audit log.
    """
    return f"agent@{socket.gethostname()}"


def root_dir():
    env = os.environ.get("TAM_HOME")
    if env:
        return Path(env).expanduser().resolve()
    return Path(__file__).resolve().parent.parent


@dataclass
class Config:
    root: Path
    default_project: str = DEFAULTS["default_project"]
    actor: str = None
    timezone: str = DEFAULTS["timezone"]
    stale_days: int = DEFAULTS["stale_days"]
    wip_limit: int = DEFAULTS["wip_limit"]
    digest_dir: str = DEFAULTS["digest_dir"]
    api_host: str = DEFAULTS["api_host"]
    api_port: int = DEFAULTS["api_port"]
    api_allowed_hosts: list = field(default_factory=list)
    db_host: str | None = DEFAULTS["db_host"]
    _overrides: dict = field(default_factory=dict, repr=False)

    @property
    def data_dir(self):
        return self.root / "data"

    @property
    def db_path(self):
        override = self._overrides.get("db_path")
        if override:
            return Path(override)
        return self.data_dir / "tam.db"

    @property
    def config_path(self):
        return self.data_dir / "config.json"

    @property
    def token_path(self):
        return self.data_dir / "api_token"

    @property
    def digest_path(self):
        p = Path(self.digest_dir)
        return p if p.is_absolute() else self.root / p

    def to_dict(self):
        out = asdict(self)
        out.pop("_overrides", None)
        out["root"] = str(self.root)
        return out

    def save(self):
        self.data_dir.mkdir(parents=True, exist_ok=True)
        payload = {k: getattr(self, k) for k in DEFAULTS}
        self.config_path.write_text(json.dumps(payload, indent=2) + "\n")


def load(db_path=None, actor=None, root=None):
    """Load config from data/config.json, falling back to defaults.

    `actor` resolves explicit argument -> $TAM_ACTOR -> config.json. The env
    var is how each agent gets its own identity in the audit log without a
    separate config file per agent.
    """
    actor = actor or os.environ.get("TAM_ACTOR")
    base = Path(root).expanduser().resolve() if root else root_dir()
    cfg = Config(root=base)
    path = cfg.config_path
    if path.exists():
        try:
            stored = json.loads(path.read_text())
        except json.JSONDecodeError:
            stored = {}
        for key in DEFAULTS:
            if key in stored:
                setattr(cfg, key, stored[key])
    # Resolution: explicit argument -> $TAM_ACTOR -> config.json -> derived.
    # The old literal "agent" is treated as unset: it named nobody.
    if not cfg.actor or cfg.actor == GENERIC_ACTOR:
        cfg.actor = default_actor()
    if db_path:
        cfg._overrides["db_path"] = str(Path(db_path).expanduser())
    if actor:
        cfg.actor = actor
    return cfg
