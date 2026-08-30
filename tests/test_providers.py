"""The provider contract, the registry, and the generic providers.

The point of this layer is that TAM contains no tool-specific knowledge. These
tests hold that line: a new tool must be addable without touching the core.
"""

import json
from unittest import mock

from tam import providers, shell
from tam.errors import ValidationError
from tam.providers import Observation, Provider

from .base import TamTestCase


class TestRegistry(TamTestCase):
    def test_builtins_are_registered(self):
        self.assertEqual(providers.available(),
                         ["command", "heartbeat", "http", "path", "process",
                          "slurm"])

    def test_unknown_provider_names_are_rejected_with_a_hint(self):
        with self.assertRaises(ValidationError) as cm:
            providers.get("nope")
        self.assertIn("available:", cm.exception.message)

    def test_describe_reports_each_provider(self):
        rows = {p["name"]: p for p in providers.describe()}
        self.assertTrue(rows["http"]["description"])
        self.assertTrue(rows["slurm"]["ref_hint"])

    def test_a_provider_can_be_registered_at_runtime(self):
        class Fake(Provider):
            name = "unit-test-fake"
            description = "test"

            def probe(self, watch):
                return Observation(providers.RUNNING)

        providers.register(Fake)
        try:
            self.assertIn("unit-test-fake", providers.available())
            self.assertEqual(providers.get("unit-test-fake").probe({}).state,
                             providers.RUNNING)
        finally:
            providers._REGISTRY.pop("unit-test-fake", None)

    def test_provider_without_a_name_is_rejected(self):
        class Nameless(Provider):
            pass
        with self.assertRaises(ValidationError):
            providers.register(Nameless)

    def test_observation_rejects_a_non_canonical_state(self):
        with self.assertRaises(ValidationError):
            Observation("RUNNING")          # Slurm's word, not ours

    def test_user_providers_load_from_a_directory(self):
        """The extension point: drop a file in, no change to TAM."""
        directory = self.root / "data" / "providers"
        directory.mkdir(parents=True, exist_ok=True)
        (directory / "acme.py").write_text(
            "from tam.providers import Provider, Observation, register, RUNNING\n"
            "class Acme(Provider):\n"
            "    name = 'acme'\n"
            "    description = 'demo'\n"
            "    def probe(self, watch):\n"
            "        return Observation(RUNNING, detail={'ref': watch['ref']})\n"
            "register(Acme)\n"
        )
        try:
            loaded = providers.load_user_providers(directory)
            self.assertIn("acme.py", loaded)
            obs = providers.get("acme").probe({"ref": "job-7"})
            self.assertEqual(obs.detail["ref"], "job-7")
        finally:
            providers._REGISTRY.pop("acme", None)

    def test_a_broken_user_provider_does_not_break_tam(self):
        directory = self.root / "data" / "providers"
        directory.mkdir(parents=True, exist_ok=True)
        (directory / "broken.py").write_text("raise RuntimeError('boom')\n")
        with mock.patch("sys.stderr"):
            loaded = providers.load_user_providers(directory)
        self.assertNotIn("broken.py", loaded)
        self.assertIn("slurm", providers.available())

    def test_missing_directory_is_harmless(self):
        self.assertEqual(providers.load_user_providers(self.root / "nope"), [])


class TestShellDecoding(TamTestCase):
    """Regression: undecodable command output marked healthy jobs `unknown`.

    `tail -c` cuts a log at a byte offset, which can split a multi-byte
    character. Training logs are full of them (progress bars). Strict decoding
    raised, the probe failed, and metric collection silently stopped.
    """

    def test_invalid_utf8_output_does_not_raise(self):
        rc, out, err = shell.run(
            ["printf", "ok \\200 truncated"], timeout=10)
        self.assertEqual(rc, 0)
        self.assertIn("ok", out)

    def test_a_truncated_multibyte_log_still_probes(self):
        log = self.root / "train.log"
        # A box-drawing character (U+2501) sliced in half, as tail -c would.
        log.write_bytes(
            "                 all  100  200  0.78  0.80  0.83  0.62\n".encode()
            + b"\xe2\x94")
        obs = providers.get("path").probe(
            {"ref": str(log), "host": None,
             "config": {"parser": "yolo", "file": str(log)}})
        self.assertEqual(obs.state, providers.PRESENT)
        self.assertAlmostEqual(obs.metrics["recall"], 0.80)


class TestShellInjection(TamTestCase):
    """A ref or path is data, not shell. The generic `command` provider runs
    arbitrary commands on purpose; `process`, `path` and log-tailing must not.
    """

    def test_a_malicious_path_ref_does_not_execute(self):
        sentinel = self.root / "pwned"
        # $(...) inside the old json.dumps quoting would have run this.
        ref = f"/definitely/missing$(touch {sentinel})"
        obs = providers.get("path").probe({"ref": ref, "host": None, "config": {}})
        self.assertEqual(obs.state, providers.MISSING)
        self.assertFalse(sentinel.exists(), "command substitution executed")

    def test_a_malicious_process_ref_does_not_execute(self):
        sentinel = self.root / "pwned-proc"
        ref = f"$(touch {sentinel})"
        providers.get("process").probe({"ref": ref, "host": None, "config": {}})
        self.assertFalse(sentinel.exists(), "command substitution executed")

    def test_a_malicious_log_path_does_not_execute(self):
        from tam.providers.builtin import _read_text
        sentinel = self.root / "pwned-log"
        _read_text(f"/missing$(touch {sentinel})")
        self.assertFalse(sentinel.exists(), "command substitution executed")

    def test_ssh_refuses_an_option_shaped_host(self):
        rc, out, err = shell.ssh("-oProxyCommand=touch /tmp/x", "true")
        self.assertEqual(rc, 1)
        self.assertIn("suspicious", err)


class TestCommandProvider(TamTestCase):
    def probe(self, ref, config=None, rc=0, out=""):
        with mock.patch.object(shell, "run", return_value=(rc, out, "")):
            return providers.get("command").probe(
                {"ref": ref, "host": None, "config": config or {}})

    def test_exit_code_decides_by_default(self):
        self.assertEqual(self.probe("true").state, providers.SUCCEEDED)
        self.assertEqual(self.probe("false", rc=1).state, providers.FAILED)

    def test_state_patterns_win_over_exit_code(self):
        obs = self.probe("gcloud ...", config={
            "state_patterns": {providers.RUNNING: r"STATE:\s*RUNNING"}},
            rc=0, out="STATE: RUNNING\n")
        self.assertEqual(obs.state, providers.RUNNING)

    def test_metrics_come_from_the_configured_parser(self):
        obs = self.probe("some-cli", config={
            "parser": "json", "paths": {"recall": "metrics.recall"}},
            out='{"metrics": {"recall": 0.71}}')
        self.assertAlmostEqual(obs.metrics["recall"], 0.71)

    def test_output_is_only_kept_when_asked(self):
        self.assertNotIn("output", self.probe("x", out="secret").detail)
        self.assertIn("output",
                      self.probe("x", config={"include_output": True},
                                 out="fine").detail)


class TestSlurmStateOverride(TamTestCase):
    """Regression: an aborted training run was reported `succeeded`.

    Slurm reported COMPLETED because the wrapper script trapped the kill,
    cleaned up and exited 0 -- while the training inside had been aborted at
    epoch 98 of 299.
    """

    def _probe(self, log_text, config):
        from tam.providers import builtin
        with mock.patch.object(builtin, "run",
                               return_value=(0, "COMPLETED|19:10:48|n1|||job", "")), \
             mock.patch.object(builtin, "_read_text", return_value=log_text):
            return providers.get("slurm").probe(
                # an explicit log, so the probe does not need scontrol to find it
                {"ref": "38574", "host": None,
                 "config": {"log": "/tmp/job.out", **config}})

    def test_scheduler_success_is_overridden_by_the_log(self):
        obs = self._probe(
            "epoch 98/299\n### TASK STOPPED - USER ABORTED - STATUS CHANGED ###\n",
            {"state_patterns": {providers.STOPPED: r"USER ABORTED"}})
        self.assertEqual(obs.state, providers.STOPPED)
        self.assertEqual(obs.native_state, "COMPLETED")
        self.assertEqual(obs.detail["state_override"]["scheduler_said"], "COMPLETED")

    def test_log_path_is_remembered_after_the_job_leaves_the_queue(self):
        """Regression: an aborted run read as `succeeded` once it ended.

        scontrol cannot resolve a finished job, so the log path had to come
        from the previous observation.
        """
        from tam.providers import builtin
        with mock.patch.object(builtin, "run", side_effect=[
                    (1, "", "Invalid job id"),                  # squeue: gone
                    (0, "COMPLETED|13:51:04|0:0|end|job", ""),  # sacct
                    (1, "", "Invalid job id"),                  # scontrol: gone
                ]), \
             mock.patch.object(builtin, "_read_text",
                               return_value="### TASK STOPPED - USER ABORTED ###"):
            obs = providers.get("slurm").probe({
                "ref": "38577", "host": None,
                "config": {"state_patterns": {providers.STOPPED: "USER ABORTED"}},
                "detail": {"log": "/nfs/slurm_logs/job.out"},   # from last probe
            })
        self.assertEqual(obs.state, providers.STOPPED)
        self.assertEqual(obs.native_state, "COMPLETED")

    def test_a_genuinely_clean_run_is_untouched(self):
        obs = self._probe("training finished normally\n",
                          {"state_patterns": {providers.STOPPED: r"USER ABORTED"}})
        self.assertEqual(obs.state, providers.SUCCEEDED)
        self.assertNotIn("state_override", obs.detail)


class TestHttpProvider(TamTestCase):
    def probe(self, config, payload, status=200):
        class FakeResponse:
            def __init__(self):
                self.status = status

            def read(self):
                return json.dumps(payload).encode()

            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

        with mock.patch("urllib.request.urlopen", return_value=FakeResponse()):
            return providers.get("http").probe(
                {"ref": "https://api.example.com/jobs/1", "host": None,
                 "config": config})

    def test_state_is_mapped_from_the_response(self):
        obs = self.probe(
            {"state_path": "status",
             "state_map": {"IN_PROGRESS": providers.RUNNING,
                           "DONE": providers.SUCCEEDED}},
            {"status": "IN_PROGRESS"})
        self.assertEqual(obs.state, providers.RUNNING)
        self.assertEqual(obs.native_state, "IN_PROGRESS")

    def test_unmapped_state_becomes_unknown_not_an_error(self):
        obs = self.probe({"state_path": "status", "state_map": {}},
                         {"status": "SOMETHING_NEW"})
        self.assertEqual(obs.state, providers.UNKNOWN)

    def test_metrics_extracted_from_json(self):
        obs = self.probe(
            {"parser": "json", "paths": {"labelled": "stats.labelled"},
             "step_path": "stats.batch"},
            {"stats": {"labelled": 1423, "batch": 3}})
        self.assertEqual(obs.metrics["labelled"], 1423.0)
        self.assertEqual(obs.step, 3)

    def test_token_is_read_from_the_environment_not_the_database(self):
        import os
        captured = {}

        class FakeResponse:
            status = 200

            def read(self):
                return b"{}"

            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

        def fake_urlopen(req, timeout=None):
            captured["auth"] = req.headers.get("Authorization")
            return FakeResponse()

        with mock.patch.dict(os.environ, {"MY_TOKEN": "s3cret"}), \
             mock.patch("urllib.request.urlopen", fake_urlopen):
            providers.get("http").probe(
                {"ref": "https://x/y", "host": None,
                 "config": {"token_env": "MY_TOKEN"}})
        self.assertEqual(captured["auth"], "Bearer s3cret")

    def test_unreachable_host_is_an_observation_not_an_exception(self):
        import urllib.error
        with mock.patch("urllib.request.urlopen",
                        side_effect=urllib.error.URLError("no route")):
            obs = providers.get("http").probe(
                {"ref": "https://x/y", "host": None, "config": {}})
        self.assertEqual(obs.state, providers.UNREACHABLE)


class TestSlurmMapping(TamTestCase):
    def test_native_states_map_into_the_canonical_vocabulary(self):
        from tam.providers.builtin import SLURM_STATES
        self.assertEqual(SLURM_STATES["RUNNING"], providers.RUNNING)
        self.assertEqual(SLURM_STATES["OUT_OF_MEMORY"], providers.FAILED)
        self.assertEqual(SLURM_STATES["COMPLETED"], providers.SUCCEEDED)
        self.assertEqual(SLURM_STATES["CANCELLED"], providers.STOPPED)

    def test_attention_states_are_tool_agnostic(self):
        for state in providers.ATTENTION:
            self.assertIn(state, providers.STATES)
        self.assertNotIn(providers.RUNNING, providers.ATTENTION)


class TestScanIsProviderAgnostic(TamTestCase):
    def test_a_custom_provider_flows_through_scan_end_to_end(self):
        class Acme(Provider):
            name = "unit-acme"
            description = "demo"

            def probe(self, watch):
                return Observation(providers.FAILED, native_state="EXPLODED",
                                   metrics={"widgets": 12.0}, step=1)

        providers.register(Acme)
        try:
            issue = self.mk("external work")
            self.svc.add_watch(issue.key, "unit-acme", "job-9")
            self.svc.scan()                      # seeds state
            result = self.svc.scan()
            row = result["results"][0]
            self.assertEqual(row["state"], providers.FAILED)
            self.assertEqual(row["native_state"], "EXPLODED")
            self.assertEqual(self.svc.latest_metrics(issue.key)["widgets"]["value"],
                             12.0)
            self.assertEqual(len(result["attention"]), 1)
        finally:
            providers._REGISTRY.pop("unit-acme", None)

    def test_a_provider_that_raises_does_not_abort_the_scan(self):
        class Exploding(Provider):
            name = "unit-boom"

            def probe(self, watch):
                raise RuntimeError("kaboom")

        providers.register(Exploding)
        try:
            issue = self.mk("x")
            other = self.mk("y")
            self.svc.add_watch(issue.key, "unit-boom", "1")
            self.svc.add_watch(other.key, "path", str(self.root))
            result = self.svc.scan()
            self.assertEqual(result["watches"], 2)
            states = {r["provider"]: r["state"] for r in result["results"]}
            self.assertEqual(states["unit-boom"], providers.UNKNOWN)
            self.assertEqual(states["path"], providers.PRESENT)
        finally:
            providers._REGISTRY.pop("unit-boom", None)
