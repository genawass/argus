"""Template for a custom watch provider.

Copy to $TAM_HOME/data/providers/ and edit. It is loaded by the CLI, the API,
the MCP server and the scan timer alike -- no change to TAM itself.

    cp example_custom.py /mnt/datasets/tam/data/providers/myservice.py
    tam provider list
    tam watch add TAM-1 myservice job-42
"""

import os

from tam.providers import (FAILED, PENDING, RUNNING, SUCCEEDED, UNKNOWN,
                           UNREACHABLE, Observation, Provider, register)
from tam.shell import run

# Map the service's own vocabulary onto TAM's. Keep this explicit: an unmapped
# state becoming `unknown` is a visible gap, whereas guessing hides one.
STATE_MAP = {
    "QUEUED": PENDING,
    "IN_PROGRESS": RUNNING,
    "COMPLETE": SUCCEEDED,
    "ERROR": FAILED,
    "ABORTED": FAILED,
}


class MyServiceProvider(Provider):
    name = "myservice"
    description = "Jobs on My Service"
    ref_hint = "job id"

    def probe(self, watch):
        ref = watch["ref"]
        config = self.config_of(watch)

        token = os.environ.get(config.get("token_env", "MYSERVICE_TOKEN"))
        if not token:
            # A missing credential is a fact about the world, not a crash.
            return Observation(UNKNOWN,
                               detail={"error": "MYSERVICE_TOKEN is not set"})

        rc, out, err = run(["myservice-cli", "status", ref], timeout=20)
        if rc != 0:
            return Observation(UNREACHABLE,
                               detail={"exit_code": rc, "stderr": err[:400]})

        native = out.strip().splitlines()[-1] if out.strip() else ""
        return Observation(
            state=STATE_MAP.get(native.upper(), UNKNOWN),
            native_state=native,
            detail={"job": ref},
            # Anything numeric returned here is stored as a metric and can carry
            # a target, e.g. `tam target set TAM-1 items_done '>=' 1000`.
            metrics={},
            step=None,
        )


register(MyServiceProvider)
