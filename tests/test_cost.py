"""Tests for cost.py run-log and session transcript summarisation."""
import _env  # noqa: F401  — isolate HOME/XDG/PATH before scripts/hooks
import json
import io
import os
import sys
import tempfile
import time
import unittest
from contextlib import redirect_stdout
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

SCRIPTS = Path(__file__).resolve().parents[1] / "skills" / "delegate" / "scripts"
sys.path.insert(0, str(SCRIPTS))
os.environ["DELEGATE_CATALOG_LOCAL"] = ""

import cost  # noqa: E402


def _run_record(**kwargs):
    base = {
        "v": 1,
        "ts": "2026-10-09T12:00:00Z",
        "repo": "toy",
        "repo_path": "/tmp/toy",
        "run": "codex-1",
        "agent": "codex",
        "model": "gpt-6.1-sol",
        "effort": "xhigh",
        "mode": "workspace-write",
        "links": [],
        "base": "abc",
        "outcome": "applied",
        "files": 1,
        "insertions": 2,
        "deletions": 0,
        "rounds": [{
            "n": 1, "model": "gpt-6.1-sol", "effort": "xhigh",
            "started": "2026-10-09T12:00:00Z", "minutes": 1.5,
            "exit": 0, "verify": 0,
            "usage": {"input": 100, "cached_input": 50, "cache_write": 10,
                      "output": 20, "reasoning_output": 5},
        }],
        "reviews": [],
    }
    base.update(kwargs)
    return base


class TestRunLog(unittest.TestCase):
    def test_summarise_synthetic_log(self):
        with tempfile.NamedTemporaryFile("w", suffix=".jsonl", delete=False) as f:
            path = Path(f.name)
            f.write(json.dumps(_run_record()) + "\n")
            f.write(json.dumps(_run_record(
                run="cursor-1", agent="cursor", model="auto", outcome="discarded",
                rounds=[{
                    "n": 1, "model": "auto", "effort": None,
                    "started": "2026-10-09T13:00:00Z", "minutes": 0.5,
                    "exit": 0, "verify": 0,
                    "usage": {"input": 10, "cached_input": 200, "cache_write": 0,
                              "output": 5, "reasoning_output": None},
                }],
            )) + "\n")
            f.write(json.dumps(_run_record(
                ts="2026-09-01T00:00:00Z", run="old",
            )) + "\n")
        try:
            rows = cost.load_run_log(path, since="2026-10-01")
            self.assertEqual(len(rows), 2)
            groups = cost.summarise_runs(rows)
            self.assertEqual(groups[("codex", "gpt-6.1-sol")]["runs"], 1)
            self.assertEqual(groups[("codex", "gpt-6.1-sol")]["applied"], 1)
            self.assertEqual(groups[("codex", "gpt-6.1-sol")]["usage"]["input"], 100)
            self.assertEqual(groups[("cursor", "auto")]["discarded"], 1)
            self.assertEqual(groups[("cursor", "auto")]["usage"]["cached_input"], 200)
            text = cost.render_runs(rows, groups, since="2026-10-01", log_path=path)
            self.assertIn("gpt-6.1-sol", text)
            self.assertIn("2 runs", text)
        finally:
            path.unlink(missing_ok=True)

    def test_since_bare_date_is_local(self):
        with tempfile.NamedTemporaryFile("w", suffix=".jsonl", delete=False) as f:
            path = Path(f.name)
            f.write(json.dumps(_run_record(
                ts="2026-10-10T23:09:00+00:00", run="late-utc",
            )) + "\n")
        self.addCleanup(path.unlink, missing_ok=True)

        old_tz = os.environ.get("TZ")

        def restore_tz():
            if old_tz is None:
                os.environ.pop("TZ", None)
            else:
                os.environ["TZ"] = old_tz
            time.tzset()

        self.addCleanup(restore_tz)

        os.environ["TZ"] = "Europe/Berlin"
        time.tzset()
        self.assertEqual(len(cost.load_run_log(path, since="2026-10-11")), 1)

        os.environ["TZ"] = "UTC"
        time.tzset()
        self.assertEqual(len(cost.load_run_log(path, since="2026-10-11")), 0)

    def test_since_explicit_offset_honoured(self):
        with tempfile.NamedTemporaryFile("w", suffix=".jsonl", delete=False) as f:
            path = Path(f.name)
            f.write(json.dumps(_run_record(
                ts="2026-10-10T23:09:00+00:00", run="late-utc",
            )) + "\n")
        self.addCleanup(path.unlink, missing_ok=True)
        # 23:09 UTC is before 2026-10-11T00:00+00:00 and after 2026-10-11T00:00+02:00
        self.assertEqual(len(cost.load_run_log(path, since="2026-10-11T00:00:00+00:00")), 0)
        self.assertEqual(len(cost.load_run_log(path, since="2026-10-11T00:00:00+02:00")), 1)


class TestSession(unittest.TestCase):
    def test_dedupe_and_skip_synthetic(self):
        # Two lines share message.id (duplicated usage); one synthetic; one other id.
        lines = [
            {"type": "user", "message": {"content": "zq-private-prompt"}},
            {
                "type": "assistant",
                "requestId": "req_a",
                "message": {
                    "id": "msg_1",
                    "model": "claude-opus-5-5",
                    "usage": {
                        "input_tokens": 10,
                        "cache_read_input_tokens": 1000,
                        "cache_creation_input_tokens": 100,
                        "output_tokens": 20,
                    },
                },
            },
            {
                "type": "assistant",
                "requestId": "req_a",
                "message": {
                    "id": "msg_1",
                    "model": "claude-opus-5-5",
                    "usage": {
                        "input_tokens": 10,
                        "cache_read_input_tokens": 1000,
                        "cache_creation_input_tokens": 100,
                        "output_tokens": 20,
                    },
                },
            },
            {
                "type": "assistant",
                "requestId": "req_synth",
                "message": {
                    "id": "msg_synth",
                    "model": "<synthetic>",
                    "usage": {
                        "input_tokens": 999,
                        "cache_read_input_tokens": 999,
                        "cache_creation_input_tokens": 999,
                        "output_tokens": 999,
                    },
                },
            },
            {
                "type": "assistant",
                "requestId": "req_b",
                "message": {
                    "id": "msg_2",
                    "model": "claude-opus-5-5",
                    "usage": {
                        "input_tokens": 5,
                        "cache_read_input_tokens": 2000,
                        "cache_creation_input_tokens": 50,
                        "output_tokens": 30,
                    },
                },
            },
        ]
        with tempfile.NamedTemporaryFile("w", suffix=".jsonl", delete=False) as f:
            path = Path(f.name)
            for obj in lines:
                f.write(json.dumps(obj) + "\n")
        try:
            calls = cost.parse_assistant_usage(path)
            self.assertEqual(len(calls), 2)
            summary = cost.summarise_session(
                calls, {"input": 1, "cache_read": 0.1, "cache_write": 2.0, "output": 5.0}, None
            )
            self.assertEqual(summary["calls"], 2)
            self.assertEqual(summary["input"], 15)
            self.assertEqual(summary["cache_read"], 3000)
            self.assertEqual(summary["cache_creation"], 150)
            self.assertEqual(summary["output"], 50)
            self.assertEqual(summary["context_first"], 10 + 1000 + 100)
            self.assertEqual(summary["context_last"], 5 + 2000 + 50)
            self.assertEqual(summary["context_max"], 5 + 2000 + 50)
            text = cost.render_session(path, summary)
            self.assertIn("calls: 2", text)
            self.assertNotIn("zq-private-prompt", text)  # privacy: no message text
            self.assertIn("next call re-reads", text)
        finally:
            path.unlink(missing_ok=True)

    def test_fallback_request_id(self):
        lines = [
            {
                "type": "assistant",
                "requestId": "req_only",
                "message": {
                    "model": "claude-sonnet-5-5",
                    "usage": {
                        "input_tokens": 1,
                        "cache_read_input_tokens": 2,
                        "cache_creation_input_tokens": 3,
                        "output_tokens": 4,
                    },
                },
            },
            {
                "type": "assistant",
                "requestId": "req_only",
                "message": {
                    "model": "claude-sonnet-5-5",
                    "usage": {
                        "input_tokens": 1,
                        "cache_read_input_tokens": 2,
                        "cache_creation_input_tokens": 3,
                        "output_tokens": 4,
                    },
                },
            },
        ]
        with tempfile.NamedTemporaryFile("w", suffix=".jsonl", delete=False) as f:
            path = Path(f.name)
            for obj in lines:
                f.write(json.dumps(obj) + "\n")
        try:
            calls = cost.parse_assistant_usage(path)
            self.assertEqual(len(calls), 1)
            self.assertEqual(calls[0]["output"], 4)
        finally:
            path.unlink(missing_ok=True)

    def test_resolve_session_prefix(self):
        import os
        from unittest import mock

        with tempfile.TemporaryDirectory() as cfg:
            proj = Path(cfg) / "projects" / "-tmp-toy"
            proj.mkdir(parents=True)
            for sid in ("55a11d16-aaaa", "55b00000-bbbb"):
                (proj / f"{sid}.jsonl").write_text("")
            with mock.patch.dict(os.environ, {"CLAUDE_CONFIG_DIR": cfg}):
                self.assertEqual(cost.resolve_session("55a1").name, "55a11d16-aaaa.jsonl")
                self.assertEqual(cost.resolve_session("55a11d16-aaaa").name, "55a11d16-aaaa.jsonl")
                with self.assertRaises(SystemExit) as cm:
                    cost.resolve_session("55")
                self.assertIn("ambiguous", str(cm.exception))

    def test_resolve_latest_from_subfolder(self):
        from unittest import mock

        with tempfile.TemporaryDirectory() as cfg, tempfile.TemporaryDirectory() as work:
            sub = Path(work) / "upstream" / "drafts"
            sub.mkdir(parents=True)
            proj = Path(cfg) / "projects" / cost.munge_cwd(work)
            proj.mkdir(parents=True)
            (proj / "abc.jsonl").write_text("")
            with mock.patch.dict(os.environ, {"CLAUDE_CONFIG_DIR": cfg}), \
                    mock.patch("cost.os.getcwd", return_value=str(sub)):
                self.assertEqual(cost.resolve_session("latest"), proj / "abc.jsonl")
            with mock.patch.dict(os.environ, {"CLAUDE_CONFIG_DIR": cfg}), \
                    mock.patch("cost.os.getcwd", return_value="/nonexistent-delegate-test"):
                with self.assertRaises(SystemExit) as cm:
                    cost.resolve_session("latest")
                self.assertIn("or its parents", str(cm.exception))

    def test_idle_rebuilds_gap_and_no_timestamp(self):
        weights = {"input": 1, "cache_read": 0.1, "cache_write": 2.0, "output": 5.0}
        base = datetime(2026, 10, 1, 12, 0, 0, tzinfo=timezone.utc)

        def assistant(mid, minutes, cache_creation, with_ts=True):
            ts = (base + timedelta(minutes=minutes)).strftime("%Y-%m-%dT%H:%M:%SZ")
            obj = {
                "type": "assistant",
                "requestId": mid,
                "message": {
                    "id": mid,
                    "model": "claude-opus-5-5",
                    "usage": {
                        "input_tokens": 1,
                        "cache_read_input_tokens": 100,
                        "cache_creation_input_tokens": cache_creation,
                        "output_tokens": 1,
                    },
                },
            }
            if with_ts:
                obj["timestamp"] = ts
            return obj

        lines = [
            assistant("m1", 0, 1000),
            assistant("m2", 59, 2000),          # 59 min — not idle
            assistant("m3", 59, 9999, with_ts=False),  # no timestamp — never counts, restarts the clock
            assistant("m4", 59 + 61, 3000),     # 61 min after m2, but m3 came between — not counted
            assistant("m5", 59 + 61 + 61, 5000),  # 61 min after m4 — idle rebuild
        ]
        with tempfile.NamedTemporaryFile("w", suffix=".jsonl", delete=False) as f:
            path = Path(f.name)
            for obj in lines:
                f.write(json.dumps(obj) + "\n")
        self.addCleanup(path.unlink, missing_ok=True)

        calls = cost.parse_assistant_usage(path)
        self.assertEqual(len(calls), 5)
        summary = cost.summarise_session(calls, weights, None)
        self.assertEqual(len(summary["idle_rebuilds"]), 1)
        self.assertEqual(summary["idle_rebuilds"][0]["gap_min"], 61)
        self.assertEqual(summary["idle_rebuilds"][0]["cache_creation"], 5000)
        self.assertEqual(summary["idle_rebuild_units"], 5000 * weights["cache_write"])
        text = cost.render_session(path, summary)
        self.assertIn("idle rebuilds (gap >= 60 min): 1", text)
        self.assertIn("61 min", text)
        self.assertIn("of cost units", text)
        payload = json.loads(cost.render_session(path, summary, as_json=True))
        self.assertIn("idle_rebuilds", payload)
        self.assertIn("idle_rebuild_units", payload)
        self.assertEqual(len(payload["idle_rebuilds"]), 1)

        # No gaps → none
        tight = [
            assistant("a", 0, 100),
            assistant("b", 10, 100),
        ]
        with tempfile.NamedTemporaryFile("w", suffix=".jsonl", delete=False) as f:
            path2 = Path(f.name)
            for obj in tight:
                f.write(json.dumps(obj) + "\n")
        self.addCleanup(path2.unlink, missing_ok=True)
        summary2 = cost.summarise_session(cost.parse_assistant_usage(path2), weights, None)
        self.assertEqual(summary2["idle_rebuilds"], [])
        self.assertEqual(summary2["idle_rebuild_units"], 0)
        self.assertIn("idle rebuilds (gap >= 60 min): none", cost.render_session(path2, summary2))


class TestByMode(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.session_log = Path(tmp.name) / "sessions.jsonl"
        self.run_log = Path(tmp.name) / "runs.jsonl"
        self.weights = {"orchestrator": 0.5, "claude-only": 0.25, "vanilla": 0.25}
        # The default saved fit sits next to the quota log: keep the real one out.
        env = mock.patch.dict(os.environ, {"CLAUDE_DELEGATE_QUOTA_LOG": str(Path(tmp.name) / "quota" / "quota.jsonl")})
        env.start()
        self.addCleanup(env.stop)
        self.calibration = {"path": "/x/calibration.json", "saved_at": "2026-10-20T12:00:00+02:00",
                            "span": ["2026-10-13T08:00:00+00:00", "2026-10-20T09:00:00+00:00"],
                            "readings": 471, "since": None,
                            "pools": {"claude-5h": {"per_usd": 2.0, "p5": 1.9, "p95": 2.1},
                                      "claude-7d": {"per_usd": 0.5, "p5": 0.4, "p95": 0.6},
                                      "other-pool": {"per_usd": 9.0, "p5": None, "p95": None}}}

    def start(self, session="session-a", mode="orchestrator", **fields):
        return {"event": "start", "session": session, "ts": "2026-10-13T10:00:00+02:00",
                "mode": mode, "u": 0.0, "forced": False, "excluded": False, "unassigned": False,
                "repo": "owner/toy", "repo_dir": "toy", "weights": self.weights, **fields}

    def summary(self, session="session-a", **fields):
        return {"event": "summary", "session": session, "ts": "2026-10-13T11:00:00+02:00",
                "calls": 10, "subagent_calls": 2, "cost_units": 1000, "usd_list_price": 1.25,
                "blocked": 3, "quota": None, **fields}

    def delegate(self, session="session-a", command="launch", run="worker"):
        return {"event": "delegate", "session": session, "command": command, "run": run}

    def write(self, path, rows):
        path.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")

    def result(self, sessions, runs=(), **options):
        self.write(self.session_log, sessions)
        self.write(self.run_log, runs)
        return cost.by_mode(cost.load_jsonl(self.session_log), cost.load_jsonl(self.run_log), **options)

    def test_drawn_classification_and_no_start_sessions(self):
        rows = [self.start(), self.summary(), self.start("excluded", u=0.9, excluded=True),
                self.start("forced", u=0.3, forced=True), self.start("unassigned", unassigned=True),
                self.start("other", u=None), self.summary("no-start"), self.summary("no-start"),
                self.delegate("another-no-start", command="ask")]
        result = self.result(rows)
        self.assertEqual(result["not_drawn"], {"excluded": 1, "forced": 1, "unassigned": 1, "other": 1})
        self.assertEqual(result["skipped_no_start"], 2)
        self.assertEqual([row["session"] for row in result["sessions"]], ["session-a"])
        self.assertEqual(result["arms"]["orchestrator"]["sessions"], 1)  # u == 0 is drawn
        self.assertIn("not drawn: excluded 1, forced 1, unassigned 1, other 1", cost.render_by_mode(result))
        self.assertIn("skipped: 2 sessions without a start row", cost.render_by_mode(result))

    def test_first_start_and_last_summary_end_win_in_file_order(self):
        result = self.result([
            self.start(mode="claude-only"), self.summary(calls=900),
            {"event": "end", "session": "session-a", "reroll": False},
            self.start(mode="vanilla", u=None, forced=True, inherited=True,
                       ts="2026-10-14T10:00:00+02:00", weights={"vanilla": 1}),
            self.summary(calls=4, subagent_calls=1, cost_units=250, usd_list_price=0.5,
                         blocked=2, ts="2026-10-12T10:00:00Z", mode="vanilla", reroll=False),
            {"event": "end", "session": "session-a", "reroll": True},
        ])
        arm = result["arms"]["claude-only"]
        self.assertEqual((arm["sessions"], arm["summarised"], arm["rerolls"], arm["pending"]), (1, 1, 1, 0))
        self.assertEqual((arm["calls"], arm["subagent_calls"], arm["cost_units"],
                          arm["usd_list_price"], arm["blocked"]), (4, 1, 250, 0.5, 2))
        self.assertEqual(result["sessions"][0]["start"], "2026-10-13T10:00:00+02:00")
        self.assertEqual(result["weights"]["configs"], [{"weights": self.weights, "sessions": 1}])
        self.assertEqual(result["arms"]["vanilla"]["sessions"], 0)
        self.assertEqual(self.result([self.start(), {"event": "end", "session": "session-a", "reroll": True},
                                      {"event": "end", "session": "session-a", "reroll": False}])
                         ["arms"]["orchestrator"]["rerolls"], 0)

    def test_pending_has_no_cost_and_delegate_counts_come_from_events(self):
        rows = [self.start(), self.summary(delegate={"launch": 99}), self.start("pending"),
                self.delegate(), self.delegate(command="followup"), self.delegate(command="review"),
                self.delegate(command="ask"), self.delegate("pending", command="ask"),
                self.delegate(command="status"), {"event": "end", "session": "pending", "reroll": True}]
        result = self.result(rows)
        arm = result["arms"]["orchestrator"]
        self.assertEqual((arm["sessions"], arm["pending"], arm["summarised"], arm["rerolls"]), (2, 1, 1, 1))
        self.assertEqual((arm["calls"], arm["cost_units"], arm["usd_list_price"]), (10, 1000, 1.25))
        self.assertEqual(arm["delegate"], {"launch": 1, "followup": 1, "review": 1, "ask": 2})
        self.assertEqual((arm["calls_per_session"], arm["usd_per_session"]), (10, 1.25))
        self.assertEqual(arm["usd_null_sessions"], 0)
        self.assertIn("reroll/pending", cost.render_by_mode(result, sessions=True))

    def test_sessions_table_prints_no_repo_names(self):
        result = self.result([self.start(repo="owner/secret-project", repo_dir="secret-project"), self.summary()])
        text = cost.render_by_mode(result, sessions=True)
        self.assertIn("session-", text)
        self.assertNotIn("secret-project", text)
        self.assertIn("owner/secret-project", cost.render_by_mode(result, as_json=True, sessions=True))

    def test_since_is_earliest_start_local_midnight_and_offset_aware(self):
        env = mock.patch.dict(os.environ, {"TZ": "Europe/Berlin"})
        env.start()
        self.addCleanup(time.tzset)
        self.addCleanup(env.stop)
        time.tzset()
        rows = [self.start("before", ts="2026-10-10T21:59:59Z"), self.summary("before"),
                self.start("before", ts="2026-10-12T00:00:00Z", inherited=True),
                self.start("boundary", ts="2026-10-10T22:00:00Z"),
                self.start("naive-utc", ts="2026-10-10T23:09:00"),
                self.start("missing-time", ts=None), self.start("bad-time", ts="bad"),
                self.start("old-excluded", ts="2026-10-10T21:59:59Z", excluded=True)]
        result = self.result(rows, since="2026-10-11")
        self.assertEqual([s["session"] for s in result["sessions"]], ["boundary", "naive-utc"])
        self.assertEqual(result["sessions"][0]["start_date"], "2026-10-11")
        self.assertEqual(result["not_drawn"]["excluded"], 0)
        explicit = self.result(rows, since="2026-10-10T23:00:00Z")
        self.assertEqual([s["session"] for s in explicit["sessions"]], ["naive-utc"])
        self.assertIn("since 2026-10-11", cost.render_by_mode(result))
        with self.assertRaises(ValueError):
            self.result(rows, since="bad")

    def test_explicit_join_is_authoritative_and_tokens_use_review_agent(self):
        rows = [self.start(), self.summary(), self.start("session-b", mode="vanilla"),
                self.delegate("session-b", run="codex-1")]
        run = _run_record(session="session-a", repo="unrelated", reviews=[
            {"agent": "cursor", "usage": {"input": 7, "cached_input": None, "output": 9}},
            {"agent": "codex", "usage": None}], rounds=[
                {"usage": {"input": 100, "cached_input": 50, "output": 20}}, {"usage": None},
                {"usage": {"input": 30, "cached_input": 2, "output": None}}])
        # Explicit ids need neither a launch event, matching repo nor a later timestamp.
        result = self.result(rows, [run, run, _run_record(run="discard", session="session-a",
                                                       outcome="discarded", rounds=[]),
                                    _run_record(run="missing", session="unknown")])
        arm = result["arms"]["orchestrator"]
        self.assertEqual((arm["runs"], arm["applied"], arm["discarded"]), (2, 1, 1))
        self.assertEqual(arm["delegate_tokens"], {
            "codex": {"input": 130, "cached_input": 52, "output": 20},
            "cursor": {"input": 7, "cached_input": 0, "output": 9}})
        self.assertEqual(result["arms"]["vanilla"]["runs"], 0)
        self.assertIn("delegate tokens by agent", cost.render_by_mode(result))

    def test_legacy_join_latest_earlier_start_repo_and_at_most_one_session(self):
        rows = [self.start("first", ts="2026-10-13T08:00:00Z"), self.delegate("first"),
                self.start("second", mode="claude-only", ts="2026-10-13T12:00:00+02:00"),
                self.delegate("second"), self.start("wrong-repo", repo_dir="other"),
                self.delegate("wrong-repo"), self.start("no-launch", mode="vanilla"),
                self.start("future", ts="2026-10-14T08:00:00Z"), self.delegate("future"),
                self.start("second", ts="2026-10-15T08:00:00Z", inherited=True, u=None)]
        runs = [_run_record(run="worker", ts="2026-10-13T09:00:00Z"),
                _run_record(run="worker", ts="2026-10-13T10:00:00Z", session=None),
                _run_record(run="worker", ts="2026-10-13T10:00:00Z"),  # duplicate
                _run_record(run="worker", ts="2026-10-13T07:59:59Z"),
                _run_record(run="worker", ts="bad"), _run_record(run="worker", ts=None),
                _run_record(run="worker", repo="no-match", ts="2026-10-13T11:00:00Z"),
                _run_record(run="not-launched", ts="2026-10-13T11:00:00Z")]
        result = self.result(rows, runs)
        self.assertEqual({s["session"]: s["runs"] for s in result["sessions"]},
                         {"first": 1, "second": 1, "wrong-repo": 0, "no-launch": 0, "future": 0})
        self.assertEqual(sum(a["runs"] for a in result["arms"].values()), 2)

    def test_non_drawn_owner_and_explicit_unknown_do_not_fall_back(self):
        rows = [self.start(), self.delegate(),
                self.start("excluded", ts="2026-10-13T09:00:00Z", excluded=True),
                self.delegate("excluded"), self.start("no-launch", ts="2026-10-13T11:00:00Z")]
        runs = [_run_record(run="worker", ts="2026-10-13T10:00:00Z"),
                _run_record(run="worker", ts="2026-10-13T10:01:00Z", session="unknown"),
                _run_record(run="worker", ts="2026-10-13T10:02:00Z", session="excluded")]
        result = self.result(rows, runs)
        self.assertEqual(result["arms"]["orchestrator"]["runs"], 0)

    def test_pool_sums_means_null_usd_and_dynamic_pool_names(self):
        rows = [self.start(), self.summary(quota={"pools": {
            "claude-5h": {"delta": 12.0}, "claude-7d": {"delta": None}, "new-pool": {"delta": 0}}}),
            self.start("second"), self.summary("second", usd_list_price=None, quota={"pools": {
                "claude-5h": {"delta": 6.0}, "codex-5h": {"delta": None}}}),
            self.start("third"), self.summary("third", usd_list_price=2.0, quota=None),
            self.start("fourth"), self.summary("fourth", quota={"pools": {"claude-5h": {"delta": -2.0}}}),
            self.start("pending")]
        result = self.result(rows)
        arm = result["arms"]["orchestrator"]
        self.assertEqual(arm["pools"]["claude-5h"], {"sum": 16.0, "n": 3, "mean": 16 / 3})
        self.assertEqual(arm["pools"]["new-pool"], {"sum": 0, "n": 1, "mean": 0})
        self.assertEqual(arm["pools"]["codex-5h"], {"sum": 0, "n": 0, "mean": None})
        self.assertEqual((arm["usd_list_price"], arm["usd_null_sessions"], arm["usd_per_session"]), (4.5, 1, 1.5))
        text = cost.render_by_mode(result)
        self.assertIn("16.0 (3)", text)
        claude = text.split("run `delegate cost --calibrate --save`\n")[1].splitlines()
        self.assertEqual(claude[0].split("  ")[0], "MODE")
        self.assertIn("  claude-5h EST  READ", claude[0])
        self.assertEqual(claude[0].split(), ["MODE", "claude-5h", "EST", "READ", "claude-7d", "EST", "READ"])
        self.assertEqual(claude[1].split(), ["orchestrator", "-", "16.0", "(3)", "-", "-"])
        other = text.split("other pools, READ:\n")[1].splitlines()[0]
        self.assertEqual(other.split(), ["MODE", "codex-5h", "new-pool"])
        only_other = cost.render_by_mode(self.result([self.start(), self.summary(quota={"pools": {
            "codex-5h": {"delta": 4.0}}})]))
        self.assertIn("pool points used, READ (sum, n):\nMODE          codex-5h\norchestrator  4.0 (1)", only_other)
        self.assertNotIn("READ:", only_other)

    def test_estimated_points_are_usd_times_the_saved_rate(self):
        rows = [self.start(), self.summary(quota={"pools": {"claude-5h": {"delta": 12.0}}}),
                self.start("null-usd"), self.summary("null-usd", usd_list_price=None),
                self.start("third"), self.summary("third", usd_list_price=2.0),
                self.start("vanilla", mode="vanilla"), self.summary("vanilla", usd_list_price=0.0),
                self.start("pending")]
        before = json.dumps(self.calibration)
        result = self.result(rows, calibration=self.calibration)
        self.assertEqual(json.dumps(self.calibration), before)
        sessions = {s["session"]: s for s in result["sessions"]}
        self.assertEqual(sessions["session-a"]["est_pools"], {"claude-5h": 2.5, "claude-7d": 0.625})
        self.assertEqual(sessions["null-usd"]["est_pools"], {})
        self.assertEqual(sessions["pending"]["est_pools"], {})
        arm = result["arms"]["orchestrator"]
        self.assertEqual(arm["est_pools"], {"claude-5h": {"sum": 6.5, "n": 2, "mean": 3.25},
                                            "claude-7d": {"sum": 1.625, "n": 2, "mean": 0.8125}})
        self.assertEqual(arm["pools"]["claude-5h"], {"sum": 12.0, "n": 1, "mean": 12.0})
        self.assertEqual(result["arms"]["vanilla"]["est_pools"]["claude-5h"], {"sum": 0.0, "n": 1, "mean": 0.0})
        self.assertEqual(result["arms"]["claude-only"]["est_pools"]["claude-5h"]["n"], 0)
        self.assertIs(result["calibration"], self.calibration)
        text = cost.render_by_mode(result, sessions=True)
        self.assertIn("EST = list-price USD x points per USD (claude-5h 2, claude-7d 0.5; fit saved 2026-10-20, "
                      "readings 2026-10-13 to 2026-10-20)", text)
        self.assertNotIn("other-pool", text)
        claude = text.split("readings 2026-10-13 to 2026-10-20)\n")[1].splitlines()
        self.assertEqual(claude[1].split(), ["orchestrator", "6.5", "(2)", "12.0", "(1)", "1.6", "(2)", "-"])
        self.assertEqual(claude[3].split(), ["vanilla", "0.0", "(1)", "-", "0.0", "(1)", "-"])
        self.assertTrue(claude[4].startswith("READ: the SessionStart-to-SessionEnd delta"))
        means = text.split("pool points per measured session (mean):\n")[1].splitlines()
        self.assertEqual(means[1].split(), ["orchestrator", "3.2", "12.0", "0.8", "-"])
        header = text.split("sessions:\n")[1].splitlines()[0].split()
        self.assertEqual(header[6:12], ["CLAUDE-5H", "EST", "READ", "CLAUDE-7D", "EST", "READ"])
        row = next(line for line in text.splitlines() if line.startswith("session-"))
        self.assertEqual(row.split()[6:10], ["2.5", "12.0", "0.6", "-"])

    def test_no_saved_fit_prints_dashes_and_where_to_save(self):
        read = [self.start(), self.summary(quota={"pools": {"claude-7d": {"delta": 3.0}}})]
        for calibration, where in ((None, ""), ({**self.calibration, "pools": {}},
                                               " (none usable at /x/calibration.json)")):
            with self.subTest(calibration=calibration):
                result = self.result(read, calibration=calibration)
                self.assertEqual(result["sessions"][0]["est_pools"], {})
                self.assertEqual(result["arms"]["orchestrator"]["est_pools"], {})
                text = cost.render_by_mode(result)
                self.assertIn("EST needs a saved fit: run `delegate cost --calibrate --save`" + where + "\n", text)
                self.assertIn("\norchestrator  -              3.0 (1)\n", text)
        # No readings in the summaries: the hint still shows, since estimates need only a fit.
        unread = [self.start(), self.summary()]
        text = cost.render_by_mode(self.result(unread, calibration={"path": "/x/c.json", "pools": {}}))
        self.assertIn("pool points used: no quota readings in the summaries yet\nClaude pool points: EST needs a saved "
                      "fit: run `delegate cost --calibrate --save` (none usable at /x/c.json)\n", text)
        self.assertNotIn("EST", cost.render_by_mode(self.result(unread)))
        only_codex = cost.render_by_mode(self.result([self.start(), self.summary(quota={"pools": {
            "codex-5h": {"delta": 1.0}}})], calibration={"pools": {}}))
        self.assertIn("vanilla       -\nClaude pool points: EST needs a saved fit", only_codex)

    def test_weights_warning_distinct_configs_and_unknowns(self):
        rows = [self.start(), self.start("same", weights=dict(reversed(list(self.weights.items())))),
                self.start("unknown", weights=None), self.start("missing")]
        del rows[-1]["weights"]
        result = self.result(rows)
        self.assertEqual(result["weights"], {"configs": [{"weights": self.weights, "sessions": 2}], "unknown": 2})
        self.assertNotIn("warning", cost.render_by_mode(result))
        rows += [self.start("different", weights={"orchestrator": 1.0}),
                 self.start("excluded", excluded=True, weights={"vanilla": 1.0})]
        result = self.result(rows)
        text = cost.render_by_mode(result)
        self.assertIn("compare arms within one config", text)
        for config in result["weights"]["configs"]:
            self.assertIn(json.dumps(config["weights"], sort_keys=True), text)
            self.assertIn(f"({config['sessions']} sessions)", text)
        self.assertEqual(len(result["weights"]["configs"]), 2)
        self.assertNotIn("warning", cost.render_by_mode(self.result(rows, since="2026-10-14")))

    def test_ghosts_count_in_sessions_only(self):
        zero = {"pools": {"claude-5h": {"delta": 0.0}, "claude-7d": {"delta": 0.0}}}
        rows = [self.start("ghost"), self.summary("ghost", transcript=None, calls=0, subagent_calls=0,
                                                  cost_units=0, usd_list_price=None, blocked=0, quota=zero),
                self.start("real"), self.summary("real", transcript="/x/real.jsonl", usd_list_price=None,
                                                 quota={"pools": {"claude-7d": {"delta": 4.0}}}),
                self.start("no-calls"), self.summary("no-calls", transcript="/x/no-calls.jsonl", calls=0,
                                                     usd_list_price=0.5),
                self.start("pending")]
        result = self.result(rows)
        arm = result["arms"]["orchestrator"]
        self.assertEqual((arm["sessions"], arm["ghosts"], arm["summarised"], arm["pending"]), (4, 1, 2, 1))
        self.assertEqual((arm["calls"], arm["calls_per_session"], arm["usd_null_sessions"]), (10, 5, 1))
        self.assertEqual(arm["pools"]["claude-7d"], {"sum": 4.0, "n": 1, "mean": 4.0})
        self.assertEqual(arm["pools"]["claude-5h"], {"sum": 0, "n": 0, "mean": None})
        self.assertEqual({s["session"]: s["ghost"] for s in result["sessions"]},
                         {"ghost": True, "real": False, "no-calls": False, "pending": False})
        text = cost.render_by_mode(result, sessions=True)
        lines = text.splitlines()
        self.assertEqual(lines[1].split()[:4], ["MODE", "SESS", "GHOST", "REROLL"])
        self.assertEqual(lines[2].split()[:3], ["orchestrator", "4", "1"])
        self.assertIn("GHOST: drawn sessions that ended with no transcript and no calls", text)
        self.assertEqual(next(l for l in lines if l.startswith("ghost ")).split()[3], "ghost")
        self.assertNotIn("GHOST:", cost.render_by_mode(self.result(rows[2:])))

    def test_empty_arms_have_null_means_and_print_dashes_in_fixed_order(self):
        result = self.result([])
        self.assertEqual(list(result["arms"]), ["orchestrator", "claude-only", "vanilla"])
        for arm in result["arms"].values():
            self.assertEqual(arm["sessions"], 0)
            self.assertIsNone(arm["usd_per_session"])
            self.assertIsNone(arm["calls_per_session"])
        text = cost.render_by_mode(result)
        self.assertEqual([line.split()[0] for line in text.splitlines()[2:5]], list(cost.MODES))
        self.assertTrue(all(line.split()[8:10] == ["-", "-"] for line in text.splitlines()[2:5]))
        self.assertTrue(all(line == line.rstrip() for line in text.splitlines()))
        self.assertIn("pool points used: no quota readings in the summaries yet", text)

    def test_json_cli_paths_sessions_always_present_and_no_writes(self):
        self.write(self.session_log, [self.start("abcdefgh-long"), self.summary("abcdefgh-long")])
        self.write(self.run_log, [_run_record(session="abcdefgh-long")])
        before = (self.session_log.read_bytes(), self.run_log.read_bytes())
        out = io.StringIO()
        with redirect_stdout(out):
            cost.main(["--by-mode", "--session-log", str(self.session_log), "--log", str(self.run_log), "--json"])
        payload = json.loads(out.getvalue())
        self.assertEqual(set(payload), {"log", "run_log", "since", "calibration", "arms", "not_drawn",
                                        "skipped_no_start", "weights", "sessions", "item_order"})
        self.assertEqual(payload["calibration"]["path"], str(self.session_log.parent / "quota" / "calibration.json"))
        self.assertEqual(payload["calibration"]["pools"], {})
        saved = self.session_log.parent / "saved.json"
        saved.write_text(json.dumps({"saved_at": "2026-10-20T12:00:00+02:00", "pools": [
            {"pool": "claude-7d", "headline": {"unit": "usd", "per_unit": 0.4, "p5": 0.3, "p95": 0.5}}]}))
        out = io.StringIO()
        with redirect_stdout(out):
            cost.main(["--by-mode", "--session-log", str(self.session_log), "--log", str(self.run_log), "--json",
                       "--calibration", str(saved)])
        self.assertEqual(json.loads(out.getvalue())["sessions"][0]["est_pools"], {"claude-7d": 0.5})
        self.assertEqual((payload["log"], payload["run_log"]), (str(self.session_log), str(self.run_log)))
        self.assertEqual(payload["sessions"][0]["session"], "abcdefgh-long")
        self.assertEqual(payload["arms"]["orchestrator"]["applied"], 1)
        out = io.StringIO()
        with redirect_stdout(out):
            cost.main(["--by-mode", "--session-log", str(self.session_log), "--log", str(self.run_log), "--sessions"])
        self.assertIn("abcdefgh", out.getvalue())
        self.assertIn("sessions:", out.getvalue())
        self.assertEqual(before, (self.session_log.read_bytes(), self.run_log.read_bytes()))

    def test_cli_defaults_use_session_log_env_and_conflicting_flags_error(self):
        self.write(self.session_log, [self.start()])
        self.write(self.run_log, [])
        with mock.patch.dict(os.environ, {"CLAUDE_SESSION_LOG": str(self.session_log)}):
            out = io.StringIO()
            with redirect_stdout(out):
                cost.main(["--by-mode", "--log", str(self.run_log), "--json"])
            self.assertEqual(json.loads(out.getvalue())["log"], str(self.session_log))
        for session in ("latest", ""):
            with self.subTest(session=session), self.assertRaises(SystemExit) as exc:
                cost.main(["--by-mode", "--session", session])
            self.assertIn("--by-mode cannot be combined with --session", str(exc.exception))

    def test_loader_ignores_missing_blank_bad_and_non_object_rows(self):
        self.assertEqual(cost.load_jsonl(self.session_log), [])
        self.session_log.write_text('bad json\n\n[]\nnull\n42\n' + json.dumps(self.start()) + '\n')
        self.assertEqual(cost.load_jsonl(self.session_log), [self.start()])

    def test_pure_aggregation_preserves_inputs_and_repo_filter(self):
        rows = [self.start(), self.summary(), self.start("other", repo_dir="other")]
        runs = [_run_record(session="session-a")]
        before = json.dumps([rows, runs])
        result = cost.by_mode(rows, runs, repo="toy")
        self.assertEqual(json.dumps([rows, runs]), before)
        self.assertEqual([s["session"] for s in result["sessions"]], ["session-a"])
        self.assertEqual(result, cost.by_mode(rows, runs, repo="toy"))

    def test_original_run_cli_without_by_mode_is_unchanged(self):
        self.write(self.run_log, [_run_record()])
        out = io.StringIO()
        with redirect_stdout(out):
            cost.main(["--log", str(self.run_log), "--json"])
        payload = json.loads(out.getvalue())
        self.assertEqual(payload["runs"], 1)
        self.assertIn("by_agent_model", payload)
        self.assertNotIn("arms", payload)


class TestByModeFits(unittest.TestCase):
    T = "2026-10-26T17:01:00Z"
    BEFORE = "2026-10-26T17:00:59Z"
    AFTER = "2026-10-26T17:02:00Z"

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.session_log = self.root / "sessions.jsonl"
        self.run_log = self.root / "runs.jsonl"
        self.run_log.write_text("")
        self.quota_log = self.root / "quota.jsonl"
        env = mock.patch.dict(os.environ, {"CLAUDE_DELEGATE_QUOTA_LOG": str(self.quota_log)})
        env.start()
        self.addCleanup(env.stop)

    def fit(self, name="calibration.json", since=None, rates=(2.0, 0.5), **fields):
        path = self.root / name
        saved = {"since": since, "saved_at": "2026-10-27T12:00:00Z",
                 "span": ["2026-10-20T08:00:00Z", "2026-10-27T09:00:00Z"],
                 "readings": 40, "pools": [
                     {"pool": pool, "headline": {"unit": "usd", "per_unit": rate}}
                     for pool, rate in zip(cost.ESTIMATED_POOLS, rates)], **fields}
        path.write_text(json.dumps(saved))
        return path

    def two_fits(self, **fields):
        self.fit("calibration-old.json")
        self.fit(since=self.T, rates=(0.4, 0.1), **fields)

    def session(self, name, ts, mode="orchestrator", usd=2.0, quota=None, **fields):
        return [{"event": "start", "session": name, "ts": ts, "mode": mode,
                 "u": 0.0, "repo": "owner/private-toy", "repo_dir": "toy", **fields},
                {"event": "summary", "session": name, "transcript": "toy.jsonl",
                 "calls": 1, "usd_list_price": usd, "quota": quota}]

    def readings(self, start=None, end=None, claude=4.0, codex=3.0):
        return {"start": start, "end": end, "pools": {
            "claude-5h": {"delta": claude}, "claude-7d": {"delta": None},
            "codex-5h": {"delta": codex}}}

    def discover(self):
        import calibrate
        return calibrate.saved_fits()

    def result(self, rows, **kwargs):
        fits, _ = self.discover()
        before = json.dumps([rows, fits])
        result = cost.by_mode(rows, [], fits=fits, **kwargs)
        self.assertEqual(json.dumps([rows, fits]), before)
        return result

    def cli(self, rows, *flags):
        self.session_log.write_text("".join(json.dumps(row) + "\n" for row in rows))
        out = io.StringIO()
        with redirect_stdout(out):
            cost.main(["--by-mode", "--session-log", str(self.session_log),
                       "--log", str(self.run_log), *flags])
        return out.getvalue()

    def test_single_discovered_fit_matches_explicit_text_and_applies_everywhere(self):
        path = self.fit(since=self.T)
        rows = self.session("before", self.BEFORE) + self.session("invalid", "bad")
        for flags in ((), ("--sessions",), ("--since", self.BEFORE),
                      ("--until", self.T), ("--repo", "toy", "--sessions")):
            with self.subTest(flags=flags):
                self.assertEqual(self.cli(rows, *flags), self.cli(rows, *flags, "--calibration", str(path)))
        result = json.loads(self.cli(rows, "--json"))
        self.assertNotIn("fits", result)
        self.assertNotIn("fits", result["arms"]["orchestrator"])
        self.assertEqual([s["est_pools"]["claude-5h"] for s in result["sessions"]], [4.0, 4.0])

    def test_half_open_periods_compare_instants_and_keep_the_first_start(self):
        self.two_fits()
        # Same instant as T, expressed with a non-UTC offset.
        self.fit(since="2026-10-26T19:01:00+02:00", rates=(0.4, 0.1))
        rows = (self.session("before", self.BEFORE) + self.session("at", self.T)
                + self.session("after", self.AFTER) + self.session("missing", None)
                + self.session("invalid", "bad") + self.session("wrong-type", 42))
        rows.append({**rows[0], "ts": self.AFTER})  # resumption cannot change its fit
        result = self.result(rows)
        self.assertEqual([s["fit"] for s in result["sessions"]], ["F1", "F2", "F2", None, None, None])
        self.assertEqual([s["est_pools"].get("claude-5h") for s in result["sessions"]],
                         [4.0, 0.8, 0.8, None, None, None])
        f1, f2 = result["fits"]
        self.assertIsNone(f1["from"])
        self.assertEqual(cost._row_ts(f1["until"]), cost._row_ts(self.T))
        self.assertEqual(f2["from"], f1["until"])
        self.assertIsNone(f2["until"])
        self.assertEqual(f2["since"], "2026-10-26T19:01:00+02:00")
        text = self.cli(rows, "--sessions")
        lines = text.splitlines()
        self.assertTrue(lines[1].startswith("F1 = calibration-old.json: sessions starting before 2026-10-26 17:01 UTC;"))
        self.assertTrue(lines[2].startswith("F2 = calibration.json: sessions starting from 2026-10-26 17:01 UTC;"))
        self.assertIn("claude-5h 0.4, claude-7d 0.1; fit saved 2026-10-27, readings 2026-10-20 to 2026-10-27", lines[2])
        self.assertNotIn("private-toy", text)

    def test_before_earliest_finite_fit_has_no_estimate_or_fit(self):
        self.fit("calibration-old.json", since=self.T)
        self.fit(since=self.AFTER, rates=(0.4, 0.1))
        rows = self.session("early", self.BEFORE) + self.session("covered", self.T)
        result = self.result(rows)
        self.assertIsNone(result["sessions"][0]["fit"])
        self.assertEqual(result["sessions"][0]["est_pools"], {})
        text = cost.render_by_mode(result, sessions=True)
        header, early = text.split("sessions:\n")[1].splitlines()[:2]
        self.assertEqual(header.split()[:4], ["SESSION", "START", "FIT", "MODE"])
        self.assertEqual(early.split()[2], "-")
        self.assertIn("F1", result["arms"]["orchestrator"]["fits"])
        self.assertNotIn("F2", result["arms"]["orchestrator"]["fits"])
        self.assertIn("F2 = calibration.json:", text)  # loaded fits always have a legend

    def test_identical_copy_is_deduped_and_prefers_default_filename(self):
        path = self.fit()
        rows = self.session("one", self.BEFORE)
        original = self.cli(rows, "--sessions")
        (self.root / "calibration-copy.json").write_bytes(path.read_bytes())
        fits, notes = self.discover()
        self.assertEqual([f["path"] for f in fits], [str(path)])
        self.assertEqual(notes, [])
        self.assertEqual(self.cli(rows, "--sessions"), original)
        self.assertNotIn("fits", json.loads(self.cli(rows, "--json")))

    def test_same_since_uses_newest_saved_at_and_warns_once(self):
        for since, equivalent in ((None, None), (self.T, "2026-10-26T19:01:00+02:00")):
            with self.subTest(since=since):
                self.fit(since=since, saved_at="2026-10-27T13:00:00+02:00")
                winner = self.fit("calibration-new.json", since=equivalent, rates=(5.0, 1.0),
                                  saved_at="2026-10-27T12:00:00Z")
                fits, notes = self.discover()
                self.assertEqual([f["path"] for f in fits], [str(winner)])
                self.assertEqual(len(notes), 1)
                self.assertTrue(notes[0].startswith("warning:"))
                self.assertIn("calibration.json and calibration-new.json", notes[0])
                text = self.cli(self.session("one", self.BEFORE))
                self.assertEqual(text.count("warning:"), 1)
                self.assertEqual(self.result(self.session("one", self.BEFORE))["sessions"][0]
                                 ["est_pools"]["claude-5h"], 10.0)
        # The default can also be the newer save; file iteration order does not win.
        self.fit(since=self.T, saved_at="2026-10-28T12:00:00Z")
        self.assertEqual(Path(self.discover()[0][0]["path"]).name, "calibration.json")

    def test_invalid_files_are_not_periods_and_emit_one_note_each(self):
        self.two_fits()
        broken = self.root / "calibration-broken.json"
        for content in ("invalid JSON", '[]', '{"pools": {}}'):
            with self.subTest(content=content):
                broken.write_text(content)
                fits, notes = self.discover()
                self.assertEqual(len(fits), 2)
                self.assertEqual(len(notes), 1)
                self.assertTrue(notes[0].startswith("note:"))
                self.assertIn("calibration-broken.json", notes[0])
                text = self.cli(self.session("one", self.BEFORE))
                self.assertEqual(text.count("note: skipped calibration fit calibration-broken.json:"), 1)
        # An unreadable candidate is also skipped, without losing the good fits.
        read_bytes = Path.read_bytes
        def read(path):
            if path == broken:
                raise OSError("unreadable")
            return read_bytes(path)
        with mock.patch.object(Path, "read_bytes", read):
            fits, notes = self.discover()
        self.assertEqual(len(fits), 2)
        self.assertEqual(len(notes), 1)
        self.assertIn("unreadable", notes[0])
        broken.write_bytes(b'\xff')
        self.assertEqual(len(self.discover()[0]), 2)
        self.assertIn("calibration-broken.json", self.discover()[1][0])

    def test_unparseable_fit_boundary_is_skipped_and_cannot_supply_estimates(self):
        path = self.fit(since="bad timestamp")
        rows = self.session("one", self.BEFORE)
        fits, notes = self.discover()
        self.assertEqual(fits, [])
        self.assertEqual(len(notes), 1)
        self.assertIn("invalid since timestamp", notes[0])
        result = json.loads(self.cli(rows, "--json"))
        self.assertEqual(result["sessions"][0]["est_pools"], {})
        # Pinning a single fit still ignores its since, as before.
        pinned = json.loads(self.cli(rows, "--json", "--calibration", str(path)))
        self.assertEqual(pinned["sessions"][0]["est_pools"]["claude-5h"], 4.0)

    def test_fit_with_no_usable_rates_keeps_its_period(self):
        self.two_fits(pools=[{"pool": "claude-5h", "headline": {"unit": "usd", "per_unit": 0}}])
        result = self.result(self.session("before", self.BEFORE) + self.session("after", self.T))
        self.assertEqual([s["fit"] for s in result["sessions"]], ["F1", "F2"])
        self.assertEqual(result["sessions"][1]["est_pools"], {})
        self.assertEqual(result["arms"]["orchestrator"]["fits"]["F2"]["est_pools"], {})
        self.assertEqual(len(result["fits"]), 2)
        text = cost.render_by_mode(result)
        legend = next(line for line in text.splitlines() if line.startswith("F2 ="))
        self.assertIn("none usable", legend)
        self.assertIn("fit saved 2026-10-27, readings 2026-10-20 to 2026-10-27", legend)

    def test_explicit_calibration_pins_one_fit_despite_siblings(self):
        self.two_fits()
        rows = self.session("before", self.BEFORE) + self.session("at", self.T)
        result = json.loads(self.cli(rows, "--calibration", str(self.root / "calibration.json"), "--json"))
        self.assertNotIn("fits", result)
        self.assertEqual([s["est_pools"]["claude-5h"] for s in result["sessions"]], [0.8, 0.8])
        text = self.cli(rows, "--calibration", str(self.root / "calibration-old.json"), "--sessions")
        self.assertNotIn("FIT", text)
        self.assertNotIn("F1 =", text)
        old = json.loads(self.cli(rows, "--calibration", str(self.root / "calibration-old.json"), "--json"))
        self.assertEqual([s["est_pools"]["claude-5h"] for s in old["sessions"]], [4.0, 4.0])

    def test_per_fit_sums_means_and_other_pools_stay_separate(self):
        self.two_fits()
        rows = []
        for mode, factor in (("orchestrator", 1), ("claude-only", 10)):
            for label, ts, dollars in (("old-a", self.BEFORE, 1), ("old-b", self.BEFORE, 3),
                                       ("new-a", self.T, 2), ("new-b", self.AFTER, 6)):
                rows += self.session(f"{mode}-{label}", ts, mode, usd=dollars * factor,
                                     quota=self.readings(ts, ts, claude=dollars * factor, codex=2.0))
        result = self.result(rows)
        for mode, factor in (("orchestrator", 1), ("claude-only", 10)):
            arm = result["arms"][mode]
            self.assertEqual(arm["est_pools"], {})
            self.assertFalse(set(cost.ESTIMATED_POOLS) & set(arm["pools"]))
            self.assertEqual(arm["pools"]["codex-5h"], {"sum": 8.0, "n": 4, "mean": 2.0})
            for label, dollars, rate in (("F1", 4, 2.0), ("F2", 8, 0.4)):
                entry = arm["fits"][label]
                self.assertEqual(entry["est_pools"]["claude-5h"],
                                 {"sum": dollars * factor * rate, "n": 2, "mean": dollars * factor * rate / 2})
                self.assertEqual(entry["pools"]["claude-5h"],
                                 {"sum": dollars * factor, "n": 2, "mean": dollars * factor / 2})
        text = cost.render_by_mode(result)
        sums = text.split("Claude pool points (sum, n), per fit:\n")[1].split("READ:")[0].splitlines()
        means = text.split("pool points per measured session (mean):\n")[1].split("other pools")[0].splitlines()
        self.assertEqual(sums[0].split()[:2], ["MODE", "FIT"])
        self.assertEqual(means[0].split()[:2], ["MODE", "FIT"])
        self.assertEqual(sums[1].split(), ["orchestrator", "F1", "8.0", "(2)", "4.0", "(2)", "2.0", "(2)", "-"])
        self.assertEqual(sums[2].split(), ["orchestrator", "F2", "3.2", "(2)", "8.0", "(2)", "0.8", "(2)", "-"])
        self.assertEqual(sums[3].split(), ["claude-only", "F1", "80.0", "(2)", "40.0", "(2)", "20.0", "(2)", "-"])
        self.assertEqual(sums[4].split(), ["claude-only", "F2", "32.0", "(2)", "80.0", "(2)", "8.0", "(2)", "-"])
        self.assertEqual(means[1].split(), ["orchestrator", "F1", "4.0", "2.0", "1.0", "-"])
        self.assertEqual(means[2].split(), ["orchestrator", "F2", "1.6", "4.0", "0.4", "-"])
        self.assertEqual(means[3].split(), ["claude-only", "F1", "40.0", "20.0", "10.0", "-"])
        self.assertEqual(means[4].split(), ["claude-only", "F2", "16.0", "40.0", "4.0", "-"])
        other = text.split("other pools, READ:\n")[1].splitlines()
        self.assertEqual(other[0].split(), ["MODE", "codex-5h"])
        self.assertEqual(other[1].split(), ["orchestrator", "8.0", "(4)"])

    def test_crossing_read_is_omitted_but_estimate_and_other_pools_remain(self):
        self.two_fits()
        rows = (self.session("crossing", self.BEFORE, quota=self.readings(self.BEFORE, self.T, claude=-98.0))
                + self.session("null", self.BEFORE, quota=self.readings(self.BEFORE, self.T, claude=None))
                + self.session("missing", self.BEFORE, quota=self.readings(None, self.BEFORE))
                + self.session("kept", self.BEFORE, quota=self.readings(self.BEFORE, self.BEFORE))
                + self.session("newbad", self.T, mode="claude-only", quota=self.readings(self.BEFORE, self.AFTER))
                + self.session("newkept", self.T, quota=self.readings(self.T, self.AFTER)))
        result = self.result(rows)
        arm = result["arms"]["orchestrator"]
        self.assertEqual(arm["fits"]["F1"]["read_crossing"], 2)
        self.assertEqual(arm["fits"]["F1"]["pools"]["claude-5h"], {"sum": 4.0, "n": 1, "mean": 4.0})
        self.assertEqual(arm["fits"]["F1"]["est_pools"]["claude-5h"]["sum"], 16.0)
        self.assertEqual(arm["pools"]["codex-5h"]["sum"], 15.0)
        self.assertEqual(result["arms"]["claude-only"]["fits"]["F2"]["read_crossing"], 1)
        self.assertEqual([s["read_crossing"] for s in result["sessions"]], [True, False, True, False, True, False])
        text = cost.render_by_mode(result, sessions=True)
        self.assertIn("READ left out for 3 sessions whose quota readings cross a fit boundary", text)
        table = text.split("sessions:\n")[1].splitlines()
        self.assertEqual(table[1].split()[2:5], ["F1", "orchestrator", "cross"])
        self.assertEqual(table[1].split()[7:9], ["4.0", "-98.0"])  # the session still shows raw READ
        self.assertEqual(table[2].split()[4], "-")


class TestListPrice(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.path = Path(tmp.name) / "session.jsonl"
        self.agents = self.path.with_suffix("") / "subagents"
        self.agents.mkdir(parents=True)
        env = mock.patch.dict(os.environ, {"DELEGATE_CATALOG_LOCAL": ""})
        env.start()
        self.addCleanup(env.stop)
        self.weights, self.prices = cost.load_catalog_cost()

    def assistant(self, mid, model="claude-opus-5-5", inp=10, read=1000,
                  write=100, output=20, split=None, **modifiers):
        usage = {"input_tokens": inp, "cache_read_input_tokens": read,
                 "cache_creation_input_tokens": write, "output_tokens": output,
                 **modifiers}
        if split is not None:
            usage["cache_creation"] = {
                "ephemeral_5m_input_tokens": split[0],
                "ephemeral_1h_input_tokens": split[1],
            }
        return {"type": "assistant", "timestamp": "2026-10-13T12:00:00Z",
                "message": {"id": mid, "model": model, "usage": usage}}

    def write(self, path, *rows):
        path.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")

    def summary(self):
        return cost.summarise_session(cost.parse_session_usage(self.path), self.weights, self.prices)

    def test_all_subagent_files_deduplicated_main_first_and_meta_ignored(self):
        main = self.assistant("duplicate", split=(0, 100))
        fallback = self.assistant(None, model="claude-haiku-5-5", split=(100, 0))
        fallback["requestId"] = "request-only"
        second = self.assistant("second", model="claude-haiku-5-5")
        self.write(self.path, main, main)
        self.write(self.agents / "agent-a.jsonl", fallback, main)
        self.write(self.agents / "agent-b.jsonl", fallback, second, second)
        self.write(self.agents / "agent-b.meta.json", self.assistant("metadata"))
        self.write(self.agents / "other.jsonl", self.assistant("not-an-agent"))

        calls = cost.parse_session_usage(self.path)
        self.assertEqual([c["id"] for c in calls], ["duplicate", "request-only", "second"])
        self.assertEqual([c["source"] for c in calls], ["main", "subagents", "subagents"])
        self.assertEqual(calls[0]["timestamp"], main["timestamp"])
        combined = self.summary()
        historical = cost.summarise_session(cost.parse_assistant_usage(self.path), self.weights, None)
        for key in ("calls", "models", "input", "cache_read", "cache_creation", "output",
                    "context_first", "context_last", "context_max", "cost_units", "cost_parts",
                    "idle_rebuilds", "idle_rebuild_units"):
            self.assertEqual(combined[key], historical[key], key)
        self.assertEqual(combined["subagent_calls"], 2)
        self.assertEqual([(r["source"], r["model"], r["calls"]) for r in combined["by_model"]],
                         [("main", "claude-opus-5-5", 1), ("subagents", "claude-haiku-5-5", 2)])
        self.assertAlmostEqual(combined["usd"], combined["usd_main"] + combined["usd_subagents"])
        self.assertEqual(combined["usd_unpriced"], [])

    def test_cache_write_durations_and_unsplit_remainder(self):
        self.write(self.path,
                   self.assistant("split", split=(20, 70)),  # remaining ten at 5m
                   self.assistant("unsplit"))
        # Explicit null has the same interpretation as an absent split.
        with self.path.open("a", encoding="utf-8") as stream:
            null = self.assistant("explicit-null")
            null["message"]["usage"]["cache_creation"] = None
            stream.write(json.dumps(null) + "\n")
        calls = cost.parse_session_usage(self.path)
        self.assertEqual((calls[0]["cache_write_5m"], calls[0]["cache_write_1h"],
                          calls[0]["cache_write_unsplit"]), (20, 70, 10))
        summary = self.summary()
        row = summary["by_model"][0]
        self.assertEqual((row["cache_write_5m"], row["cache_write_1h"],
                          row["cache_write_unsplit"]), (20, 70, 210))
        expected = (30 * 4 + 3000 * 0.2 + 230 * 5 + 70 * 8 + 60 * 20) / 1e6
        self.assertAlmostEqual(summary["usd"], expected, places=12)
        self.assertEqual(summary["usd_subagents"], 0.0)

    def test_haiku_threshold_prices_all_classes_per_call(self):
        self.write(self.path,
                   self.assistant("base", model="claude-haiku-5-5", inp=100, read=99870,
                                  write=30, output=20, split=(10, 10)),
                   self.assistant("above", model="claude-haiku-5-5", inp=101, read=99870,
                                  write=30, output=20, split=(10, 10)))
        base, above = cost.parse_session_usage(self.path)
        base_usd = (100 * 0.1 + 99870 * 0.01 + 20 * 0.125 + 10 * 0.2 + 20 * 0.5) / 1e6
        above_usd = (101 * 0.5 + 99870 * 0.05 + 20 * 0.625 + 10 * 1.0 + 20 * 2.5) / 1e6
        self.assertAlmostEqual(cost.price_call(base, self.prices)[0], base_usd, places=12)
        self.assertAlmostEqual(cost.price_call(above, self.prices)[0], above_usd, places=12)
        summary = self.summary()
        self.assertAlmostEqual(summary["usd"], base_usd + above_usd, places=12)
        self.assertAlmostEqual(summary["by_model"][0]["usd"], base_usd + above_usd, places=12)

    def test_unpriced_modifiers_unknown_model_and_partial_group(self):
        self.write(self.path,
                   self.assistant("fast-a", speed="fast"), self.assistant("fast-b", speed="fast"),
                   self.assistant("regional", inference_geo="us"),
                   self.assistant("standard", speed="standard", inference_geo="global"),
                   self.assistant("default"))
        self.write(self.agents / "agent-a.jsonl", self.assistant("unknown", model="unknown"))
        summary = self.summary()
        self.assertEqual(summary["usd_unpriced"], [
            {"model": "claude-opus-5-5", "source": "main", "calls": 1, "reason": "inference_geo=us"},
            {"model": "claude-opus-5-5", "source": "main", "calls": 2, "reason": "speed=fast"},
            {"model": "unknown", "source": "subagents", "calls": 1, "reason": "no price"},
        ])
        expected = 2 * (10 * 4 + 1000 * 0.2 + 100 * 5 + 20 * 20) / 1e6
        self.assertAlmostEqual(summary["usd"], expected, places=12)
        main, agent = summary["by_model"]
        self.assertEqual((main["calls"], main["input"], main["cache_read"],
                          main["cache_write_unsplit"], main["output"]), (5, 50, 5000, 500, 100))
        self.assertEqual((agent["calls"], agent["input"], agent["cache_read"],
                          agent["cache_write_unsplit"], agent["output"]), (1, 10, 1000, 100, 20))
        self.assertIsNone(agent["usd"])
        text = cost.render_session(self.path, summary)
        for group in summary["usd_unpriced"]:
            self.assertIn(group["reason"], text)

    def test_model_exact_then_longest_prefix_and_date_suffix(self):
        self.write(self.path, self.assistant("dated", model="claude-haiku-5-5-20261001"))
        summary = self.summary()
        expected = (10 * 0.1 + 1000 * 0.01 + 100 * 0.125 + 20 * 0.5) / 1e6
        self.assertAlmostEqual(summary["usd"], expected, places=12)
        self.assertEqual(summary["by_model"][0]["model"], "claude-haiku-5-5-20261001")
        prices = {"models": {"claude-haiku": {"tag": "short"},
                             "claude-haiku-5-5": {"tag": "long"},
                             "claude-haiku-5-5-20261001": {"tag": "exact"}}}
        self.assertEqual(cost.model_price("claude-haiku-5-5-20261001", prices)["tag"], "exact")
        self.assertEqual(cost.model_price("claude-haiku-5-5-20261002", prices)["tag"], "long")
        self.assertIsNone(cost.model_price("claude-haiku-5-50", self.prices))

    def test_empty_or_all_unpriced_and_catalog_fallback(self):
        self.write(self.path)
        summary = self.summary()
        self.assertIsNone(summary["usd"])
        self.assertEqual((summary["usd_main"], summary["usd_subagents"]), (0.0, 0.0))
        self.assertEqual(summary["by_model"], [])
        self.assertEqual(summary["prices_as_of"], "2026-10-06")
        self.write(self.path, self.assistant("unknown", model=None))
        summary = self.summary()
        self.assertIsNone(summary["usd"])
        self.assertIsNone(summary["by_model"][0]["usd"])
        self.assertEqual(summary["usd_unpriced"][0]["reason"], "no price")
        for contents in (None, "bad json", "{}"):
            catalog = self.path.parent / "catalog.json"
            if contents is not None:
                catalog.write_text(contents, encoding="utf-8")
            with mock.patch("cost.CATALOG", catalog):
                weights, prices = cost.load_catalog_cost()
            self.assertEqual(weights, cost.DEFAULT_WEIGHTS)
            self.assertIsNone(prices)
            if catalog.exists():
                catalog.unlink()

    def test_cli_json_schema_precision_and_historical_text_lines(self):
        self.write(self.path, self.assistant("main"))
        buf = io.StringIO()
        with redirect_stdout(buf):
            cost.main(["--session", str(self.path), "--json"])
        payload = json.loads(buf.getvalue())
        self.assertNotIn("usd_per_mtok_input", payload)
        self.assertAlmostEqual(payload["usd"], 0.00114, places=12)
        self.assertEqual(set(payload["by_model"][0]),
                         {"model", "source", "calls", *cost.TOKEN_CLASSES, "usd"})
        text = cost.render_session(self.path, payload)
        old_lines = [line for line in text.splitlines()
                     if not line.startswith(("list price:", "by model:", "model ", "claude-opus"))]
        self.assertEqual(old_lines, [
            "session: session.jsonl  (claude-opus-5-5)", "calls: 1",
            "tokens: input 10  cache_creation 100  cache_read 1,000  output 20",
            "context/call: first 1.1k  last 1.1k  max 1.1k",
            "cost units: 410  (input 2.4%  cache_read 24.4%  cache_write 48.8%  output 24.4%)",
            "idle rebuilds (gap >= 60 min): none", "next call re-reads ≈ 1.1k tokens",
        ])
        self.assertIn("list price: $0.00  (main $0.00, subagents $0.00; prices as of 2026-10-06)", text)
        self.assertIn("by model: (prices as of 2026-10-06; USD)", text)

class TestUntilAndPreregCap(unittest.TestCase):
    def setUp(self):
        import prereg
        self.prereg = prereg
        tmp = tempfile.TemporaryDirectory(prefix="cost-prereg-")
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.session_log, self.run_log = self.root / "sessions", self.root / "runs"
        self.run_log.write_text("")
        env = mock.patch.dict(os.environ, {"CLAUDE_SESSION_LOG": str(self.session_log),
                                          "CLAUDE_DELEGATE_QUOTA_LOG": str(self.root / "quota")})
        env.start()
        self.addCleanup(env.stop)

    def write(self, path, rows):
        path.write_text("".join(json.dumps(row) + "\n" for row in rows))

    def rows(self):
        rows = []
        for session, ts, usd in (("period1", "2026-10-26T17:20:50Z", 2),
                                 ("period2", "2026-10-26T17:20:51Z", 999),
                                 ("confirmatory", "2026-11-02T00:00:00Z", 999)):
            rows += [{"event": "start", "session": session, "ts": ts, "mode": "orchestrator", "u": .1},
                     {"event": "summary", "session": session, "transcript": "/missing", "calls": 1,
                      "usd_list_price": usd}]
        return rows

    def invoke(self, *args, now=None):
        out = io.StringIO()
        now = now or self.prereg.T0
        with redirect_stdout(out), mock.patch.object(self.prereg, "_now", return_value=now):
            cost.main(["--log", str(self.run_log), *args])
        return out.getvalue()

    def test_until_exclusive_by_mode_first_start_and_combined_since(self):
        rows = self.rows()
        rows += [{"event": "start", "session": "period1", "ts": "2026-11-05T00:00:00Z", "u": None,
                  "mode": "vanilla"}]
        result = cost.by_mode(rows, [], until="2026-10-26T17:20:51Z")
        self.assertEqual([s["session"] for s in result["sessions"]], ["period1"])
        self.assertIn("until 2026-10-26T17:20:51Z", cost.render_by_mode(result))
        result = cost.by_mode(rows, [], since="2026-10-26T17:20:51Z", until="2026-10-31T00:29:58Z")
        self.assertEqual([s["session"] for s in result["sessions"]], ["period2"])
        self.assertFalse(cost.by_mode(rows, [], since="2026-11-02T00:00:00Z",
                                      until="2026-11-02T00:00:00Z")["sessions"])

    def test_until_run_report_strict_ts_with_since_and_local_naive_dates(self):
        rows = [_run_record(run=str(i), ts=ts) for i, ts in enumerate(("2026-10-26T17:20:50Z",
                "2026-10-26T17:20:51Z", "2026-10-31T00:29:58Z"))]
        rows.append(_run_record(run="missing", ts=None))
        self.write(self.run_log, rows)
        loaded = cost.load_run_log(self.run_log, since="2026-10-26T17:20:51Z", until="2026-10-31T00:29:58Z")
        self.assertEqual([s["run"] for s in loaded], ["1"])
        payload = json.loads(self.invoke("--since", "2026-10-26T17:20:51Z", "--until", "2026-10-31T00:29:58Z", "--json"))
        self.assertEqual(payload["runs"], 1)
        self.assertIn("until 2026-10-31T00:29:58Z", self.invoke("--until", "2026-10-31T00:29:58Z"))
        old_tz = os.environ.get("TZ")
        try:
            os.environ["TZ"] = "Europe/Berlin"
            time.tzset()
            for until in ("2026-10-27", "2026-10-27T00:00:00"):
                self.assertEqual(cost.load_run_log(self.run_log, until=until), rows[:2])
                self.assertEqual(len(cost.by_mode(self.rows(), [], until=until)["sessions"]), 2)
        finally:
            if old_tz is None:
                os.environ.pop("TZ", None)
            else:
                os.environ["TZ"] = old_tz
            time.tzset()

    def test_until_refused_for_calibrate_and_session(self):
        for flags in (("--calibrate",), ("--session", "latest"), ("--session", "")):
            with self.assertRaises(SystemExit) as exc:
                self.invoke("--until", "2026-11-01", *flags)
            self.assertIn("--until cannot", str(exc.exception))

    def test_cap_experiment_default_explicit_symlink_period1_shown_period2_held_back(self):
        import rate
        self.write(self.session_log, self.rows())
        link = self.root / "symlink"
        link.symlink_to(self.session_log)
        original = rate.attach_items
        def check_attached(result, *args):
            self.assertEqual([s["session"] for s in result["sessions"]], ["period1"])
            return original(result, *args)
        for flags in ([], ["--session-log", str(self.session_log)], ["--session-log", str(link)]):
            with mock.patch.object(rate, "attach_items", side_effect=check_attached):
                payload = json.loads(self.invoke("--by-mode", "--sessions", "--json", *flags))
            self.assertEqual(payload["early_look"]["held_back"], 2)
            self.assertEqual(payload["early_look"]["until"], "2026-10-26T17:20:51Z")
            self.assertEqual(payload["arms"]["orchestrator"]["usd_list_price"], 2)
            self.assertNotIn("999", json.dumps(payload))
            text = self.invoke("--by-mode", "--sessions", *flags)
            self.assertIn("2 drawn sessions held back", text)
            self.assertNotIn("999", text)
            self.assertNotIn("confirma", text)
        payload = json.loads(self.invoke("--by-mode", "--json", "--until", "2026-10-26T17:20:51Z"))
        self.assertEqual(payload["early_look"]["held_back"], 0)
        payload = json.loads(self.invoke("--by-mode", "--json", "--until", "2026-10-26T17:20:50Z"))
        self.assertEqual(payload["sessions"], [])
        payload = json.loads(self.invoke("--by-mode", "--json", "--since", "2026-10-26T17:20:51Z",
                                         "--until", "2026-10-31T00:29:58Z"))
        self.assertEqual(payload["early_look"]["held_back"], 1)
        self.assertEqual(payload["sessions"], [])

    def test_no_cap_other_log_or_at_freeze_and_xdg_default_is_guarded(self):
        self.write(self.session_log, self.rows())
        other = self.root / "copy"
        other.write_bytes(self.session_log.read_bytes())
        # A byte-identical copy shares the first row and is capped before the freeze.
        payload = json.loads(self.invoke("--by-mode", "--session-log", str(other), "--json"))
        self.assertEqual(payload["early_look"]["held_back"], 2)
        self.assertEqual(len(payload["sessions"]), 1)
        different = self.root / "different"
        self.write(different, [{"event": "start", "session": "solo", "ts": "2026-11-02T00:00:00Z",
                                "mode": "orchestrator", "u": .1},
                               {"event": "summary", "session": "solo", "transcript": "/missing",
                                "calls": 1, "usd_list_price": 1}])
        payload = json.loads(self.invoke("--by-mode", "--session-log", str(different), "--json"))
        self.assertNotIn("early_look", payload)
        self.assertEqual(len(payload["sessions"]), 1)
        payload = json.loads(self.invoke("--by-mode", "--json", now=self.prereg.HARD_CLOSE + self.prereg.FREEZE_DELAY))
        self.assertNotIn("early_look", payload)
        self.assertEqual(len(payload["sessions"]), 3)
        cache_log = self.root / "claude-delegate/sessions.jsonl"
        cache_log.parent.mkdir()
        cache_log.write_bytes(self.session_log.read_bytes())
        with mock.patch.dict(os.environ, {"CLAUDE_SESSION_LOG": "", "XDG_CACHE_HOME": str(self.root)}):
            payload = json.loads(self.invoke("--by-mode", "--session-log", str(cache_log), "--json"))
        self.assertEqual(payload["early_look"]["held_back"], 2)



if __name__ == "__main__":
    unittest.main()
