import json
import tempfile
import tarfile
import threading
import time
import unittest
from pathlib import Path
from urllib.request import Request, urlopen

from dashboard import App, DEFAULT_PORT, make_server, validate_settings
from relayloop_runner import LiveAdapters
from relayloop_dashboard import ARCHIVE, _patch_extractor_source


class FakeAdapters:
    def __init__(self, session="ses_one"):
        self.session = session
        self.planner = {"id": "planner-old", "text": "historical", "role": "assistant", "completed": True}
        self.worker = {"id": "worker-old", "text": "old reply", "role": "assistant", "completed": True}
        self.sent_worker = []
        self.sent_planner = []
        self.planner_messages = []
        self.worker_messages = []
        self.ambiguous_worker = False
        self.worker_busy = False
        self.auto_ack = True
        self.delay_receipt_polls = 0
        self.delay_worker_receipt_polls = 0
        self.fail_after_submit = False
        self.pending_planner_receipt = None
        self.pending_worker_receipt = None
        self.planner_receipt_polls = 0
        self.worker_receipt_polls = 0
        self.n = 0
        self.verified = []

    def verify(self, url, session):
        self.verified.append((url, session))
        return {"ok": True, "planner_conversation_id": url.rsplit("/", 1)[-1],
                "worker_session": session}

    def planner_latest(self, url):
        value = dict(self.planner)
        if value.get("role") == "assistant" and "writing_blocks" not in value:
            value["writing_blocks"] = [value.get("text", "")]
        return value
    def worker_state(self, session):
        latest = None if self.worker_busy else dict(self.worker)
        return {"busy": self.worker_busy, "latest": latest, "latest_completed": dict(self.worker)}

    def worker_destination_messages(self, session):
        self.worker_receipt_polls += 1
        if (self.pending_worker_receipt and self.delay_worker_receipt_polls
                and self.worker_receipt_polls >= self.delay_worker_receipt_polls + 1):
            self.worker_messages.append(self.pending_worker_receipt)
            self.pending_worker_receipt = None
        return list(self.worker_messages)

    def verify_delivery(self, url, require_complete=True):
        self.planner_receipt_polls += 1
        if (self.pending_planner_receipt and self.delay_receipt_polls
                and self.planner_receipt_polls >= self.delay_receipt_polls + 1):
            self.planner_messages.append(self.pending_planner_receipt)
            self.pending_planner_receipt = None
        return list(self.planner_messages)
    match_planner_receipt = staticmethod(LiveAdapters.match_planner_receipt)

    def deliver_worker(self, session, text):
        self.sent_worker.append((session, text))
        if self.ambiguous_worker:
            raise RuntimeError("synthetic timeout after submit boundary")
        self.n += 1
        receipt = {"id": f"worker-user-{self.n}", "text": text, "role": "user"}
        self.pending_worker_receipt = receipt if self.delay_worker_receipt_polls else None
        if not self.delay_worker_receipt_polls:
            self.worker_messages.append(receipt)
        self.worker_receipt_polls = 0
        self.worker = {"id": f"worker-reply-{self.n}", "text": f"reply:{text}",
                       "role": "assistant", "completed": True}
        return {"confirmed": True, "destination_message_id": f"worker-user-{self.n}"}

    def submit_message(self, url, text):
        self.sent_planner.append((url, text))
        self.n += 1
        receipt = {"id": f"planner-user-{self.n}", "text": text}
        self.pending_planner_receipt = receipt
        if not self.delay_receipt_polls:
            self.planner_messages.append(receipt)
            self.pending_planner_receipt = None
        if self.auto_ack and self.n == 2:
            self.planner = {"id": "planner-ack", "text": "Acknowledged", "role": "assistant", "completed": True}
        if self.fail_after_submit:
            raise RuntimeError("synthetic provider timeout after submission")
        return {"submitted": True}


class DashboardTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.adapters = FakeAdapters()

    def tearDown(self): self.tmp.cleanup()

    def app(self): return App(self.tmp.name, adapters=self.adapters)

    def test_runtime_extractor_exposes_structured_writing_block_text(self):
        with tarfile.open(ARCHIVE, "r:gz") as archive:
            member = archive.extractfile("relay-beacon-cli/extract_chatgpt.js")
            source = member.read().decode("utf8")
        patched = _patch_extractor_source(source)
        self.assertIn("data-client-defined-widget=\"writing_block\"", patched)
        self.assertIn("data-markdown-copy-text", patched)
        self.assertIn("writing_blocks: await page.evaluate", patched)

    def test_settings_and_identity_validation(self):
        app = self.app()
        self.assertEqual(app.settings["worker_session"], "ses_e68a35de3ffep9JYScN5236MsG")
        self.assertEqual(app.save({"planner_url": "https://chatgpt.com/c/test", "worker_session": "ses_switch"}),
                         {"planner_url": "https://chatgpt.com/c/test", "worker_session": "ses_switch"})
        self.assertEqual(app.relay.worker_session, "ses_switch")
        self.assertEqual(validate_settings(app.settings)["worker_session"], "ses_switch")
        with self.assertRaises(ValueError): validate_settings({"planner_url": "nope", "worker_session": "x"})

    def test_legacy_migration_is_stable_private_and_profile_operations_validate(self):
        path = Path(self.tmp.name) / "settings.json"
        path.write_text(json.dumps({"planner_url":"https://chatgpt.com/c/legacy", "worker_session":"ses_legacy"}))
        app = self.app()
        profile = app.status()["pairs"][0]
        self.assertEqual(profile["name"], "Core plus plugin architecture")
        self.assertEqual((profile["planner_url"], profile["worker_session"]), ("https://chatgpt.com/c/legacy", "ses_legacy"))
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        self.assertEqual(App(self.tmp.name, adapters=self.adapters).pairs[0]["id"], profile["id"])
        added = app.save_pair({"name":"Second", "planner_url":"https://chatgpt.com/c/second", "worker_session":"ses_second"})["pair"]
        app.select_pair(added["id"])
        self.assertEqual(app.settings["worker_session"], "ses_second")
        with self.assertRaises(ValueError): app.save_pair({"name":"Third", "planner_url":"https://chatgpt.com/c/second", "worker_session":"ses_third"})
        app.save_pair({"id":added["id"], "name":"Second", "planner_url":"https://chatgpt.com/c/second-edited", "worker_session":"ses_second"})
        self.assertEqual(app.settings["planner_url"], "https://chatgpt.com/c/second-edited")
        app.relay.state["pending"] = {"stage":"submitting"}
        with self.assertRaises(ValueError): app.select_pair(profile["id"])
        with self.assertRaises(ValueError): app.save_pair({"name":"Third", "planner_url":"https://chatgpt.com/c/third", "worker_session":"ses_third"})
        self.assertEqual(json.loads(path.read_text())["planner_url"], "https://chatgpt.com/c/second-edited")

    def test_empty_runtime_seeds_stable_default_profile(self):
        app = self.app()
        first = app.status()["pairs"][0]
        self.assertEqual(first["name"], "Core plus plugin architecture")
        self.assertEqual(app.status()["selected_pair_id"], first["id"])
        self.assertEqual(App(self.tmp.name, adapters=self.adapters).pairs[0]["id"], first["id"])

    def test_pair_http_payload_field_errors_and_legacy_duplicate_check(self):
        app = self.app(); server = make_server(app, port=0)
        thread = threading.Thread(target=server.serve_forever, daemon=True); thread.start()
        try:
            base = f"http://127.0.0.1:{server.server_port}"
            csrf = json.loads(urlopen(base + "/api/csrf").read())["csrf"]
            def post(path, payload):
                request = Request(base + path, data=json.dumps(payload).encode(), method="POST", headers={
                    "Content-Type":"application/json", "Origin":base, "X-CSRF-Token":csrf})
                try: return json.loads(urlopen(request).read())
                except Exception as error: return json.loads(error.read())
            payload = {"name":"Second", "planner_url":"https://chatgpt.com/c/second?x=1", "worker_session":" ses_second "}
            created = post("/api/pairs/save", payload)
            self.assertTrue(created["ok"])
            self.assertEqual(created["pair"]["worker_session"], "ses_second")
            bad = post("/api/pairs/save", {"name":"Bad", "planner_url":"bad", "worker_session":""})
            self.assertEqual(set(bad["field_errors"]), {"planner_url", "worker_session"})
            before = app.selected_pair_id
            duplicate = post("/api/settings", {"planner_url":"https://CHATGPT.com/g/g-p-6ab749f725548191934d1e5c4da00f2a-bot/c/6ae4d33a-bee0-83ec-9308-7d48b781c915/?x=other#frag", "worker_session":"ses_legacy"})
            self.assertIn("planner_url", duplicate["field_errors"])
            self.assertEqual(app.selected_pair_id, before)
        finally:
            server.shutdown(); thread.join(); server.server_close()

    def test_pair_operations_fail_closed_for_busy_running_pending_and_unresolved(self):
        app = self.app(); original = app.relay; selected = app.selected_pair_id
        app.operation_busy = True
        with self.assertRaises(ValueError): app.select_pair(selected)
        app.operation_busy = False
        app.relay.state["unresolved"] = {"stage":"held"}
        with self.assertRaises(ValueError): app.save_pair({"name":"X", "planner_url":"https://chatgpt.com/c/x", "worker_session":"ses_x"})
        with self.assertRaises(ValueError): app.mutate("verify")
        self.assertIs(app.relay, original)
        self.assertEqual(app.selected_pair_id, selected)

    def test_save_pair_rejects_unknown_and_nonselected_ids_without_mutation(self):
        app = self.app()
        original_relay = app.relay
        original_selected = app.selected_pair_id
        original_pairs = json.loads(json.dumps(app.pairs))
        original_file = Path(self.tmp.name, "settings.json").read_bytes()
        payload = {"name":"Renamed", "planner_url":"https://chatgpt.com/c/changed", "worker_session":"ses_changed"}
        for pair_id in ("missing-id", "not-selected"):
            with self.subTest(pair_id=pair_id), self.assertRaises(ValueError):
                app.save_pair({**payload, "id":pair_id})
            self.assertIs(app.relay, original_relay)
            self.assertEqual(app.selected_pair_id, original_selected)
            self.assertEqual(app.pairs, original_pairs)
            self.assertEqual(Path(self.tmp.name, "settings.json").read_bytes(), original_file)

        added = app.save_pair({"name":"Second", "planner_url":"https://chatgpt.com/c/second", "worker_session":"ses_second"})["pair"]
        saved_pairs = json.loads(json.dumps(app.pairs))
        saved_file = Path(self.tmp.name, "settings.json").read_bytes()
        relay = app.relay
        with self.assertRaisesRegex(ValueError, "Only the selected"):
            app.save_pair({**payload, "id":original_selected})
        self.assertIs(app.relay, relay)
        self.assertEqual(app.pairs, saved_pairs)
        self.assertEqual(Path(self.tmp.name, "settings.json").read_bytes(), saved_file)
        self.assertEqual(app.selected_pair_id, added["id"])

    def test_start_verifies_the_current_connection_without_a_separate_ui_gate(self):
        app = self.app()
        app.save({"planner_url": "https://chatgpt.com/c/new", "worker_session": "ses_new"})
        result = app.mutate("start")
        self.assertTrue(result["ok"])
        self.assertTrue(any(row["kind"] == "start" for row in app.timeline()))
        app.mutate("stop")

    def test_standalone_verify_button_checks_form_connections_without_starting(self):
        app = self.app()
        result = app.mutate("verify", {"planner_url": "https://chatgpt.com/c/form", "worker_session": "ses_form"})
        self.assertTrue(result["ok"])
        self.assertEqual(self.adapters.verified[-1], ("https://chatgpt.com/c/form", "ses_form"))
        self.assertEqual(app.relay.status()["status"], "stopped")

    def test_status_exposes_exact_pending_turn_and_phase(self):
        app = self.app()
        app.relay.state.update({
            "phase": "delivering_to_planner",
            "expected_speaker": "planner",
            "pending": {"direction": "worker_to_planner", "source_id": "worker-7", "stage": "submitting"},
        })
        status = app.status()
        self.assertEqual(status["phase"], "delivering_to_planner")
        self.assertEqual(status["turn"]["pending_direction"], "worker_to_planner")
        self.assertEqual(status["turn"]["pending_source_id"], "worker-7")
        self.assertEqual(status["extraction"], "Worker response extracted")
        self.assertEqual(status["delivery"], "submitting")

    def test_status_exposes_live_worker_observation_and_unavailable_state(self):
        app = self.app()
        self.assertEqual(app.status()["worker_observation"], {
            "state": "idle", "busy": False, "latest_completed_id": "worker-old"})
        self.adapters.worker_busy = True
        self.assertEqual(app.status()["worker_observation"]["state"], "busy")
        self.adapters.worker_state = lambda _session: (_ for _ in ()).throw(RuntimeError("offline"))
        self.assertEqual(app.status()["worker_observation"]["state"], "unknown")

    def test_dashboard_restart_discards_session_error_and_hold(self):
        app = self.app()
        operation = {"direction": "worker_to_planner", "source_id": "worker-uncertain",
                     "stage": "submitting", "reason": "no unique receipt"}
        app.relay.state.update({"unresolved": operation, "phase": "stopped"})
        app.relay._save()
        app.last_error = "stale dashboard error"
        restarted = App(self.tmp.name, adapters=self.adapters)
        status = restarted.status()
        self.assertEqual(status["phase"], "stopped")
        self.assertIsNone(status["unresolved"])
        self.assertIsNone(status["error"])

    def test_dashboard_to_runner_integration_shows_real_fake_activity(self):
        app = self.app()
        app.relay.interval = 0.01
        self.assertTrue(app.verify()["ok"])
        self.assertEqual(DEFAULT_PORT, 5555)
        server = make_server(app, port=0)
        thread = threading.Thread(target=server.serve_forever, daemon=True); thread.start()
        try:
            base = f"http://127.0.0.1:{server.server_port}"
            csrf = json.loads(urlopen(base + "/api/csrf").read())["csrf"]
            request = Request(base + "/api/verify", data=json.dumps({
                "planner_url": "https://chatgpt.com/c/planner", "worker_session": "ses_one"
            }).encode(), method="POST", headers={
                "Content-Type": "application/json", "Origin": base, "X-CSRF-Token": csrf})
            verified = json.loads(urlopen(request).read())
            self.assertTrue(verified["ok"])
            self.assertEqual(app.relay.status()["status"], "stopped")
            request = Request(base + "/api/start", data=b"{}", method="POST", headers={
                "Content-Type": "application/json", "Origin": base, "X-CSRF-Token": csrf})
            self.assertTrue(json.loads(urlopen(request).read())["ok"])
            self.adapters.planner = {"id": "planner-1", "text": "hello", "role": "assistant", "completed": True}
            deadline = time.time() + 3
            while time.time() < deadline and not self.adapters.sent_planner: time.sleep(.01)
            state = json.loads(urlopen(base + "/api/status").read())
            self.assertEqual(state["status"], "running")
            self.assertTrue(any(x["kind"] == "delivery" and "Planner → Worker" in x["message"] for x in state["timeline"]))
            request = Request(base + "/api/stop", data=b"{}", method="POST", headers={
                "Content-Type": "application/json", "Origin": base, "X-CSRF-Token": csrf})
            self.assertTrue(json.loads(urlopen(request).read())["ok"])
        finally:
            server.shutdown(); thread.join(); server.server_close()


if __name__ == "__main__": unittest.main()
