"""Adapter tests, including the parity check.

The design rests on adapters being thin. The parity test is what enforces it:
the same operation issued over CLI, HTTP and MCP must produce the same result
and the same refusal.
"""

import contextlib
import io
import json
import threading
import urllib.error
import urllib.request

from tam import api, cli
from tam.mcp import Server, tool_descriptors

from .base import TamTestCase


def cli_capture(argv):
    """Run the CLI with an exact argv and return stdout."""
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(io.StringIO()):
        cli.main(argv)
    return buf.getvalue()


class CliMixin:
    def run_cli(self, *args, expect=0):
        buf = io.StringIO()
        err = io.StringIO()
        with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(err):
            code = cli.main(["--db", str(self.config.db_path), *args])
        self.assertEqual(code, expect, f"{args} -> {buf.getvalue()}{err.getvalue()}")
        return buf.getvalue()

    def run_json(self, *args, expect=0):
        return json.loads(self.run_cli(*args, "--json", expect=expect))


class TestCli(CliMixin, TamTestCase):
    def test_create_list_and_show_round_trip(self):
        created = self.run_json("issue", "create", "-t", "From CLI", "-p", "p1")
        key = created["data"]["key"]
        listed = self.run_json("issue", "list")
        self.assertIn(key, [i["key"] for i in listed["data"]])
        shown = self.run_json("issue", "show", key)
        self.assertEqual(shown["data"]["title"], "From CLI")
        self.assertIn("todo", shown["data"]["next_statuses"])

    def test_error_envelope_and_exit_code(self):
        out = self.run_json("issue", "show", "TAM-999", expect=3)
        self.assertFalse(out["ok"])
        self.assertEqual(out["error"]["code"], "not_found")

    def test_relative_dates_are_expanded(self):
        out = self.run_json("issue", "create", "-t", "soon", "-d", "+2d")
        self.assertRegex(out["data"]["due_date"], r"^\d{4}-\d{2}-\d{2}$")

    def test_delete_requires_confirmation(self):
        key = self.run_json("issue", "create", "-t", "doomed")["data"]["key"]
        self.run_json("issue", "delete", key, expect=4)
        self.run_json("issue", "delete", key, "--yes")
        self.run_json("issue", "show", key, expect=3)

    def test_move_reports_transition_denial(self):
        key = self.run_json("issue", "create", "-t", "x")["data"]["key"]
        out = self.run_json("issue", "move", key, "done", expect=5)
        self.assertEqual(out["error"]["code"], "transition_denied")

    def test_label_and_link_commands(self):
        a = self.run_json("issue", "create", "-t", "a")["data"]["key"]
        b = self.run_json("issue", "create", "-t", "b")["data"]["key"]
        self.run_json("label", "add", a, "urgent")
        self.assertEqual(self.run_json("issue", "show", a)["data"]["labels"], ["urgent"])
        self.run_json("link", "add", a, "--blocks", b)
        links = self.run_json("link", "list", b)["data"]
        self.assertEqual(links[0]["type"], "blocked_by")

    def test_digest_and_review_lifecycle(self):
        self.run_json("issue", "create", "-t", "late", "-d", "2000-01-01")
        digest = self.run_json("digest", "--write")["data"]
        self.assertEqual(len(digest["sections"]["overdue"]), 1)
        self.assertIsNone(self.run_json("review")["data"]["reviewed_at"])
        self.run_json("review", "--done", "handled")
        self.assertIsNotNone(self.run_json("review")["data"]["reviewed_at"])

    def test_global_flags_work_in_either_position(self):
        """Regression: subparser defaults used to wipe pre-subcommand globals."""
        self.run_cli("issue", "create", "-t", "positional", "-q")
        before = json.loads(cli_capture(
            ["--db", str(self.config.db_path), "--json", "issue", "list"]))
        after = json.loads(cli_capture(
            ["issue", "list", "--db", str(self.config.db_path), "--json"]))
        self.assertEqual(before, after)
        self.assertEqual(before["meta"]["count"], 1)

    def test_actor_flag_is_attributed(self):
        key = self.run_json("issue", "create", "-t", "x", "--actor", "daily-review",
                            )["data"]["key"]
        events = self.run_json("issue", "history", key)["data"]
        self.assertEqual(events[0]["actor"], "daily-review")

    def test_tree_nests_subtasks_under_their_parent(self):
        parent = self.run_json("issue", "create", "-t", "Stream")["data"]["key"]
        child = self.run_json("issue", "create", "-t", "Sub", "--parent", parent
                              )["data"]["key"]
        loose = self.run_json("issue", "create", "-t", "Standalone")["data"]["key"]

        data = self.run_json("issue", "tree")["data"]
        roots = {i["key"]: i for i in data}
        self.assertEqual(set(roots), {parent, loose})
        self.assertEqual([s["key"] for s in roots[parent]["subtasks"]], [child])
        self.assertEqual(roots[loose]["subtasks"], [])

        text = self.run_cli("issue", "tree")
        self.assertIn("└ " + child, text)

    def test_tree_shows_orphaned_matches_at_root(self):
        """A subtask whose parent is filtered out must not vanish."""
        parent = self.run_json("issue", "create", "-t", "Stream")["data"]["key"]
        child = self.run_json("issue", "create", "-t", "Sub", "--parent", parent,
                              "-p", "p0")["data"]["key"]
        data = self.run_json("issue", "tree", "--priority", "p0")["data"]
        self.assertEqual([i["key"] for i in data], [child])

    def test_digest_writes_stay_inside_the_configured_home(self):
        """Regression: --db alone left digest files resolving to the real root."""
        self.run_cli("issue", "create", "-t", "late", "-d", "2000-01-01", "-q")
        self.run_cli("digest", "--write", "-q")
        written = list((self.root / "data" / "digests").glob("*.md"))
        self.assertEqual(len(written), 1, "digest must land under the test root")

    def test_nudge_speaks_only_when_something_is_urgent(self):
        self.assertEqual(self.run_cli("nudge", "--plain").strip(), "")
        self.run_cli("issue", "create", "-t", "late", "-d", "2000-01-01", "-q")
        self.assertIn("1 overdue", self.run_cli("nudge", "--plain"))

    def test_nudge_falls_silent_once_reviewed(self):
        self.run_cli("issue", "create", "-t", "late", "-d", "2000-01-01", "-q")
        self.assertIn("overdue", self.run_cli("nudge", "--plain"))
        self.run_cli("review", "--done", "handled", "-q")
        self.assertEqual(self.run_cli("nudge", "--plain").strip(), "")

    def test_human_output_is_rendered(self):
        self.run_cli("issue", "create", "-t", "Renderable", "-p", "p0")
        text = self.run_cli("issue", "list")
        self.assertIn("Renderable", text)
        self.assertIn("KEY", text)


class HttpMixin:
    def start_api(self):
        self.server = api.make_server(self.config, host="127.0.0.1", port=0)
        self.token = self.server.tam_token
        thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        host, port = self.server.server_address[:2]
        self.base = f"http://{host}:{port}"

    def http(self, method, path, body=None, token="valid"):
        req = urllib.request.Request(
            self.base + path, method=method,
            data=json.dumps(body).encode() if body is not None else None,
            headers={"Content-Type": "application/json"},
        )
        if token == "valid" and self.token:
            req.add_header("Authorization", f"Bearer {self.token}")
        elif token not in ("valid", None):
            req.add_header("Authorization", f"Bearer {token}")
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                return resp.status, json.loads(resp.read())
        except urllib.error.HTTPError as exc:
            return exc.code, json.loads(exc.read())


class TestHttp(HttpMixin, TamTestCase):
    def setUp(self):
        super().setUp()
        # A token file only exists after `tam init`; write one so auth is live.
        self.config.token_path.parent.mkdir(parents=True, exist_ok=True)
        self.config.token_path.write_text("test-token-abc123\n")
        self.start_api()

    def test_health_needs_no_token(self):
        status, body = self.http("GET", "/health", token=None)
        self.assertEqual(status, 200)
        self.assertEqual(body["data"]["status"], "ok")

    def test_missing_token_is_rejected(self):
        status, body = self.http("GET", "/api/issues", token=None)
        self.assertEqual(status, 401)
        self.assertEqual(body["error"]["code"], "unauthorized")

    def test_wrong_token_is_rejected(self):
        status, _ = self.http("GET", "/api/issues", token="not-the-token")
        self.assertEqual(status, 401)

    def test_issue_crud_over_http(self):
        status, body = self.http("POST", "/api/issues",
                                 {"title": "Over HTTP", "priority": "p1"})
        self.assertEqual(status, 201)
        key = body["data"]["key"]

        status, body = self.http("GET", f"/api/issues/{key}")
        self.assertEqual(body["data"]["title"], "Over HTTP")

        status, body = self.http("PATCH", f"/api/issues/{key}", {"title": "Renamed"})
        self.assertEqual(body["data"]["title"], "Renamed")

        status, body = self.http("POST", f"/api/issues/{key}/transition",
                                 {"status": "todo"})
        self.assertEqual(body["data"]["status"], "todo")

        status, body = self.http("DELETE", f"/api/issues/{key}")
        self.assertTrue(body["data"]["deleted"])

    def test_status_cannot_be_patched(self):
        _, body = self.http("POST", "/api/issues", {"title": "x"})
        key = body["data"]["key"]
        status, body = self.http("PATCH", f"/api/issues/{key}", {"status": "done"})
        self.assertEqual(status, 400)

    def test_filters_work_over_query_string(self):
        self.http("POST", "/api/issues", {"title": "late", "due_date": "2000-01-01"})
        self.http("POST", "/api/issues", {"title": "fine", "priority": "p3"})
        _, body = self.http("GET", "/api/issues?overdue=1")
        self.assertEqual(body["meta"]["count"], 1)
        _, body = self.http("GET", "/api/issues?priority=p3")
        self.assertEqual(body["data"][0]["title"], "fine")

    def test_unknown_filter_is_rejected(self):
        status, body = self.http("GET", "/api/issues?nonsense=1")
        self.assertEqual(status, 400)
        self.assertIn("nonsense", body["error"]["message"])

    def test_error_statuses_map_correctly(self):
        self.assertEqual(self.http("GET", "/api/issues/TAM-999")[0], 404)
        self.assertEqual(self.http("GET", "/nope")[0], 404)
        self.assertEqual(self.http("POST", "/health")[0], 405)
        _, body = self.http("POST", "/api/issues", {"title": ""})
        self.assertEqual(body["error"]["code"], "validation")

    def test_transition_denial_is_409(self):
        _, body = self.http("POST", "/api/issues", {"title": "x"})
        key = body["data"]["key"]
        status, body = self.http("POST", f"/api/issues/{key}/transition",
                                 {"status": "done"})
        self.assertEqual(status, 409)
        self.assertEqual(body["error"]["code"], "transition_denied")

    def test_board_ui_is_served_with_the_token_injected(self):
        req = urllib.request.Request(self.base + "/")
        with urllib.request.urlopen(req, timeout=10) as resp:
            html = resp.read().decode()
            self.assertEqual(resp.status, 200)
            self.assertTrue(resp.headers["Content-Type"].startswith("text/html"))
        self.assertIn("TAM board", html)
        self.assertNotIn("__TAM_TOKEN__", html, "token placeholder must be replaced")
        self.assertIn('"test-token-abc123"', html)

    def test_no_cors_header_is_ever_sent(self):
        """The injected token is only safe while cross-origin reads are blocked."""
        for path in ("/", "/health", "/api/issues"):
            req = urllib.request.Request(self.base + path)
            if path.startswith("/api"):
                req.add_header("Authorization", f"Bearer {self.token}")
            with urllib.request.urlopen(req, timeout=10) as resp:
                self.assertIsNone(resp.headers.get("Access-Control-Allow-Origin"),
                                  f"{path} must not allow cross-origin reads")

    def test_foreign_host_header_is_rejected(self):
        """Blocks DNS rebinding: a hostile domain pointed at 127.0.0.1."""
        req = urllib.request.Request(self.base + "/health")
        req.add_header("Host", "evil.example.com")
        try:
            urllib.request.urlopen(req, timeout=10)
            self.fail("expected rejection")
        except urllib.error.HTTPError as exc:
            self.assertEqual(exc.code, 403)

    def test_workflow_endpoint_exposes_the_matrix(self):
        _, body = self.http("GET", "/api/workflow")
        data = body["data"]
        self.assertEqual(data["transitions"]["backlog"], ["cancelled", "todo"])
        self.assertEqual(data["transitions"]["done"], ["in_progress"])
        self.assertIn("p0", data["priorities"])
        self.assertRegex(data["today"], r"^\d{4}-\d{2}-\d{2}$")

    def test_malformed_json_body(self):
        req = urllib.request.Request(
            self.base + "/api/issues", method="POST", data=b"{not json",
            headers={"Content-Type": "application/json",
                     "Authorization": f"Bearer {self.token}"},
        )
        try:
            urllib.request.urlopen(req, timeout=10)
            self.fail("expected an error")
        except urllib.error.HTTPError as exc:
            self.assertEqual(exc.code, 400)


class McpMixin:
    def mcp_server(self):
        self.out = io.StringIO()
        return Server(self.config, out=self.out, log=io.StringIO())

    def rpc(self, server, method, params=None, req_id=1):
        server.out.seek(0)
        server.out.truncate()
        server.handle({"jsonrpc": "2.0", "id": req_id, "method": method,
                       "params": params or {}})
        raw = server.out.getvalue().strip()
        return json.loads(raw) if raw else None

    def call(self, server, name, args=None):
        resp = self.rpc(server, "tools/call", {"name": name, "arguments": args or {}})
        payload = json.loads(resp["result"]["content"][0]["text"])
        return resp["result"].get("isError", False), payload


class TestMcp(McpMixin, TamTestCase):
    def setUp(self):
        super().setUp()
        self.server = self.mcp_server()

    def test_initialize_reports_protocol_and_tools(self):
        resp = self.rpc(self.server, "initialize")
        self.assertIn("protocolVersion", resp["result"])
        self.assertEqual(resp["result"]["serverInfo"]["name"], "tam")
        self.assertIn("tools", resp["result"]["capabilities"])

    def test_notifications_produce_no_response(self):
        self.server.out.seek(0)
        self.server.out.truncate()
        self.server.handle({"jsonrpc": "2.0", "method": "notifications/initialized"})
        self.assertEqual(self.server.out.getvalue(), "")

    def test_tools_list_schemas_are_well_formed(self):
        tools = self.rpc(self.server, "tools/list")["result"]["tools"]
        self.assertEqual(len(tools), len(tool_descriptors()))
        for tool in tools:
            self.assertTrue(tool["description"])
            self.assertEqual(tool["inputSchema"]["type"], "object")
            for name in tool["inputSchema"].get("required", []):
                self.assertIn(name, tool["inputSchema"]["properties"])

    def test_unknown_method_and_tool(self):
        resp = self.rpc(self.server, "nonsense/method")
        self.assertEqual(resp["error"]["code"], -32601)
        resp = self.rpc(self.server, "tools/call", {"name": "tam_nope"})
        self.assertEqual(resp["error"]["code"], -32602)

    def test_create_and_query_through_tools(self):
        is_error, created = self.call(
            self.server, "tam_create_issue",
            {"title": "Via MCP", "priority": "p0", "labels": ["mcp"]})
        self.assertFalse(is_error)
        key = created["key"]
        _, listed = self.call(self.server, "tam_list_issues", {"labels": ["mcp"]})
        self.assertEqual(listed["count"], 1)
        _, fetched = self.call(self.server, "tam_get_issue",
                               {"key": key, "history": True})
        self.assertEqual(fetched["title"], "Via MCP")
        self.assertTrue(fetched["history"])

    def test_domain_errors_surface_as_tool_errors(self):
        is_error, payload = self.call(self.server, "tam_get_issue", {"key": "TAM-999"})
        self.assertTrue(is_error)
        self.assertEqual(payload["error"]["code"], "not_found")

    def test_missing_required_argument_is_reported(self):
        is_error, payload = self.call(self.server, "tam_get_issue", {})
        self.assertTrue(is_error)
        self.assertEqual(payload["error"]["code"], "validation")

    def test_malformed_frame_does_not_kill_the_loop(self):
        server = self.mcp_server()
        server.serve(io.StringIO(
            '{"jsonrpc":"2.0","id":1,"method":"tools/list"}\n'
            "not json at all\n"
            '{"jsonrpc":"2.0","id":2,"method":"ping"}\n'
        ))
        responses = [json.loads(l) for l in server.out.getvalue().splitlines() if l]
        self.assertEqual([r.get("id") for r in responses], [1, None, 2])
        self.assertEqual(responses[1]["error"]["code"], -32700)


class TestParity(CliMixin, HttpMixin, McpMixin, TamTestCase):
    """The same operation must behave identically on all three surfaces."""

    def setUp(self):
        super().setUp()
        self.config.token_path.parent.mkdir(parents=True, exist_ok=True)
        self.config.token_path.write_text("parity-token\n")
        self.start_api()
        self.server = self.mcp_server()

    def strip(self, issue):
        return {k: issue[k] for k in
                ("title", "status", "priority", "type", "due_date", "labels")}

    def test_creation_is_identical_across_adapters(self):
        fields = {"title": "Parity", "priority": "p1", "type": "bug",
                  "due_date": "2026-09-01", "labels": ["one", "two"]}

        via_cli = self.run_json(
            "issue", "create", "-t", fields["title"], "-p", "p1", "--type", "bug",
            "-d", "2026-09-01", "-l", "one", "two")["data"]
        _, http_body = self.http("POST", "/api/issues", fields)
        via_http = http_body["data"]
        _, via_mcp = self.call(self.server, "tam_create_issue", fields)

        self.assertEqual(self.strip(via_cli), self.strip(via_http))
        self.assertEqual(self.strip(via_http), self.strip(via_mcp))

    def test_the_same_filter_returns_the_same_issues(self):
        self.run_cli("issue", "create", "-t", "overdue one", "-d", "2000-01-01", "-q")
        self.run_cli("issue", "create", "-t", "future one", "-d", "2999-01-01", "-q")

        via_cli = [i["key"] for i in self.run_json("issue", "list", "--overdue")["data"]]
        _, http_body = self.http("GET", "/api/issues?overdue=true")
        via_http = [i["key"] for i in http_body["data"]]
        _, mcp_body = self.call(self.server, "tam_list_issues", {"overdue": True})
        via_mcp = [i["key"] for i in mcp_body["issues"]]

        self.assertEqual(via_cli, via_http)
        self.assertEqual(via_http, via_mcp)
        self.assertEqual(len(via_cli), 1)

    def test_workflow_guards_refuse_identically(self):
        key = self.run_json("issue", "create", "-t", "guarded")["data"]["key"]

        cli_err = self.run_json("issue", "move", key, "done", expect=5)["error"]["code"]
        http_status, http_body = self.http(
            "POST", f"/api/issues/{key}/transition", {"status": "done"})
        mcp_error, mcp_body = self.call(
            self.server, "tam_transition_issue", {"key": key, "status": "done"})

        self.assertEqual(cli_err, "transition_denied")
        self.assertEqual(http_body["error"]["code"], "transition_denied")
        self.assertEqual(http_status, 409)
        self.assertTrue(mcp_error)
        self.assertEqual(mcp_body["error"]["code"], "transition_denied")

    def test_blocked_reason_guard_holds_on_every_surface(self):
        keys = [self.run_json("issue", "create", "-t", f"b{i}", "-s", "todo")["data"]["key"]
                for i in range(3)]

        self.run_json("issue", "move", keys[0], "blocked", expect=4)
        http_status, _ = self.http(
            "POST", f"/api/issues/{keys[1]}/transition", {"status": "blocked"})
        mcp_error, mcp_body = self.call(
            self.server, "tam_transition_issue", {"key": keys[2], "status": "blocked"})

        self.assertEqual(http_status, 400)
        self.assertTrue(mcp_error)
        self.assertEqual(mcp_body["error"]["code"], "validation")

    def test_writes_from_one_adapter_are_visible_to_the_others(self):
        key = self.run_json("issue", "create", "-t", "shared")["data"]["key"]
        self.http("POST", f"/api/issues/{key}/comments", {"body": "from http"})
        self.call(self.server, "tam_add_comment", {"key": key, "body": "from mcp"})

        shown = self.run_json("issue", "show", key, "--comments")["data"]
        self.assertEqual([c["body"] for c in shown["comments"]],
                         ["from http", "from mcp"])
