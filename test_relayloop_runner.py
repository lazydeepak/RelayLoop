import json
import tempfile
import threading
import time
import unittest
from types import SimpleNamespace

from relayloop_runner import LiveAdapters, RelayRunner
from test_dashboard import FakeAdapters


class RelayRunnerTests(unittest.TestCase):
    def setUp(self): self.tmp = tempfile.TemporaryDirectory()
    def tearDown(self): self.tmp.cleanup()

    def wait_for(self, predicate, timeout=3):
        deadline = time.time() + timeout
        while time.time() < deadline:
            if predicate(): return True
            time.sleep(.01)
        return False

    def make(self, adapters, session="ses_one"):
        return RelayRunner(self.tmp.name, "https://chatgpt.com/c/planner", session, adapters, interval=.01)

    def test_complete_turn_fresh_baseline_and_ack_turn(self):
        adapters = FakeAdapters()
        runner = self.make(adapters)
        runner.start()
        adapters.planner = {"id": "planner-1", "text": "hello", "role": "assistant", "completed": True}
        self.assertTrue(self.wait_for(lambda: runner.state["last_confirmed_turn"] is not None
                                      and runner.state["expected_speaker"] == "planner"))
        self.assertEqual(adapters.sent_worker[0], ("ses_one", "hello"))
        self.assertEqual(adapters.sent_planner[0][1], "Worker is connected with RelayLoop, prompt please, automation started")
        self.assertEqual(adapters.sent_planner[1][1], "reply:hello")
        self.assertEqual(runner.state["expected_speaker"], "planner")
        self.assertEqual(runner.state["last_confirmed_turn"]["worker_id"], "worker-reply-2")
        adapters.planner = {"id": "planner-ack", "text": "Acknowledged", "role": "assistant", "completed": True}
        self.assertTrue(self.wait_for(lambda: len(adapters.sent_worker) == 2))
        self.assertEqual(adapters.sent_worker[1], ("ses_one", "Acknowledged"))
        runner.stop()

    def test_streaming_user_historical_and_duplicate_planner_turns_not_forwarded(self):
        adapters = FakeAdapters(); adapters.auto_ack = False
        adapters.planner = {"id": "user-old", "text": "old user turn", "role": "user", "completed": True}
        runner = self.make(adapters); runner.start()
        adapters.planner = {"id": "streaming", "text": "partial", "role": "assistant", "completed": False}
        time.sleep(.08); self.assertEqual(adapters.sent_worker, [])
        adapters.planner = {"id": "user-turn", "text": "operator prompt", "role": "user", "completed": True}
        time.sleep(.08); self.assertEqual(adapters.sent_worker, [])
        adapters.planner = {"id": "planner-live", "text": "fresh", "role": "assistant", "completed": True}
        self.assertTrue(self.wait_for(lambda: bool(adapters.sent_worker)))
        time.sleep(.08); self.assertEqual(len(adapters.sent_worker), 1)
        runner.stop()

    def test_planner_prompt_only_writing_block_is_forwarded_to_worker(self):
        adapters = FakeAdapters(); adapters.auto_ack = False
        runner = self.make(adapters)
        adapters.planner = {"id": "planner-prompt", "text": "analysis and commentary plus block",
                            "writing_blocks": ["exact Worker prompt only"],
                            "role": "assistant", "completed": True}
        runner._planner_turn()
        self.assertEqual(adapters.sent_worker, [("ses_one", "exact Worker prompt only")])

    def test_missing_prompt_requests_once_then_forwards_only_recovered_prompt(self):
        adapters = FakeAdapters(); adapters.auto_ack = False
        runner = self.make(adapters)
        adapters.planner = {"id": "planner-no-block", "text": "No structured prompt.",
                            "writing_blocks": [], "role": "assistant", "completed": True}
        runner._planner_turn()
        self.assertEqual(adapters.sent_planner[0][1],
                         "Worker prompt not found. Please generate the next Worker prompt.")
        self.assertTrue(runner.state["prompt_recovery_attempted"])
        self.assertEqual(adapters.sent_worker, [])

        adapters.planner = {"id": "planner-with-block", "text": "Prompt plus commentary",
                            "writing_blocks": ["recovered Worker prompt"],
                            "role": "assistant", "completed": True}
        runner._planner_turn()
        self.assertEqual(adapters.sent_worker, [("ses_one", "recovered Worker prompt")])
        self.assertFalse(runner.state["prompt_recovery_attempted"])

    def test_second_missing_prompt_pauses_relay_without_worker_delivery(self):
        adapters = FakeAdapters(); adapters.auto_ack = False
        runner = self.make(adapters)
        adapters.planner = {"id": "planner-no-block-1", "text": "No prompt one.",
                            "writing_blocks": [], "role": "assistant", "completed": True}
        runner._planner_turn()
        adapters.planner = {"id": "planner-no-block-2", "text": "Still no prompt.",
                            "writing_blocks": [], "role": "assistant", "completed": True}
        runner._planner_turn()
        self.assertEqual(runner.state["phase"], "worker_prompt_unavailable")
        self.assertTrue(runner.stop_event.is_set())
        self.assertEqual(len(adapters.sent_planner), 1)
        self.assertEqual(adapters.sent_worker, [])
        self.assertTrue(any(row["message"] == "Worker prompt not available"
                            for row in runner.state["activity"]))

    def test_hello10_post_baseline_opencode_prompt_receipt_confirms_once(self):
        adapters = FakeAdapters()
        payload = ("Hi, Worker! 👋\n\nPlanner here. Testing our simplified RelayLoop relay.\n\n"
                   "Test marker: RELAYLOOP-HELLO-10\n\nPlease reply:\n\n"
                   "“Hello Planner! I received RELAYLOOP-HELLO-10 successfully.”\n\n"
                   "No coding or project changes. Just testing Planner → Worker → Planner communication.")
        adapters.worker_messages = [
            {"id": "historical-hello10", "role": "user", "text": f'"{payload}"'},
            {"id": "msg_1a0ff2ca6001s06Agjwdj0D8Bh", "role": "user", "text": f'"{payload}"'},
        ]
        runner = self.make(adapters)
        operation = runner._new_operation("planner_to_worker", "6e3a9b58-1e4c-41f8-aceb-b6f4040f06f6", payload)
        operation["destination_baseline_ids"] = ["historical-hello10"]
        runner._begin_operation(operation)

        receipt = runner._worker_receipt(operation)
        self.assertEqual(receipt["destination_message_id"], "msg_1a0ff2ca6001s06Agjwdj0D8Bh")
        runner.confirm_delivery(operation, receipt)
        runner.confirm_delivery(operation, receipt)

        self.assertEqual(runner.state["last_confirmed_operation_id"], operation["operation_id"])
        self.assertEqual(runner.state["expected_speaker"], "worker")
        self.assertIsNone(runner.state["pending"])
        self.assertEqual(len([row for row in runner.state["activity"] if row["kind"] == "delivery"]), 1)

    def test_worker_receipt_decodes_json_transport_with_embedded_quotes(self):
        payload = 'Use only the guarded launcher:\n\n"/private/tmp/Launch Relay Beacon QA.command"'
        self.assertTrue(RelayRunner._same_payload(payload, json.dumps(payload, ensure_ascii=False)))
        relaxed = json.dumps(payload, ensure_ascii=False).replace("\\n", "\n")
        self.assertTrue(RelayRunner._same_payload(payload, relaxed))
        self.assertFalse(RelayRunner._same_payload(payload, json.dumps(payload + " changed", ensure_ascii=False)))

    def test_worker_receipt_verification_observes_delayed_destination_message(self):
        adapters = FakeAdapters()
        adapters.delay_worker_receipt_polls = 1
        runner = RelayRunner(self.tmp.name, "https://chatgpt.com/c/planner", "ses_one",
                             adapters, interval=.001, receipt_timeout=.2)
        operation = runner._new_operation("planner_to_worker", "planner-delayed", "delayed HELLO-10")
        operation["destination_baseline_ids"] = []
        runner._begin_operation(operation)
        runner._submit_and_confirm_worker(operation)
        self.assertEqual(adapters.sent_worker, [("ses_one", "delayed HELLO-10")])
        self.assertEqual(runner.state["last_confirmed_delivery"]["receipt"]["destination_message_id"], "worker-user-1")
        self.assertEqual(runner.state["expected_speaker"], "worker")
        self.assertIsNone(runner.state["unresolved"])

    def test_restart_clears_in_memory_hold_and_baselines_current_messages(self):
        adapters = FakeAdapters(); adapters.planner = {"id": "user-old", "text": "old", "role": "user", "completed": True}
        runner = self.make(adapters); runner.start(); runner.stop()
        restarted = self.make(adapters); restarted.start()
        self.assertEqual(adapters.sent_worker, [])
        restarted.stop()
        adapters = FakeAdapters(); adapters.ambiguous_worker = True
        adapters.planner = {"id": "user-old", "text": "old", "role": "user", "completed": True}
        runner = self.make(adapters); runner.start()
        adapters.planner = {"id": "planner-uncertain", "text": "hello", "role": "assistant", "completed": True}
        self.assertTrue(self.wait_for(lambda: runner.status()["unresolved"] is not None))
        self.assertEqual(runner.state["expected_speaker"], "planner")
        self.assertEqual(runner.state["last_planner_id"], "user-old")
        runner.stop()
        recovered = self.make(adapters)
        recovered.start()
        self.assertIsNone(recovered.state["unresolved"])
        recovered.stop()
        self.assertEqual(len(adapters.sent_worker), 1)

    def test_controller_state_is_never_written_to_disk(self):
        runner = self.make(FakeAdapters())
        runner.state["unresolved"] = {"source_id": "transient"}
        runner._save()
        restarted = self.make(FakeAdapters())
        self.assertIsNone(restarted.state["unresolved"])
        self.assertEqual(list(__import__("pathlib").Path(self.tmp.name).glob("**/*.json")), [])

    def test_new_session_drops_old_in_memory_uncertainty(self):
        adapters = FakeAdapters()
        first = self.make(adapters)
        first.state["unresolved"] = {"direction": "worker_to_planner", "source_id": "legacy-source",
            "stage": "submitting", "text": "uncertain payload", "reason": "old receipt failure"}
        first.state["phase"] = "unresolved"
        first._save()
        restarted = self.make(adapters)
        restarted.start()
        self.wait_for(lambda: len(adapters.sent_planner) >= 1)
        self.assertIsNone(restarted.state["unresolved"])
        restarted.stop()
        self.assertIn(len(adapters.sent_planner), (0, 1))

    def test_connection_switching_keeps_state_isolated_and_stop_is_graceful(self):
        first = FakeAdapters(); first.planner = {"id": "user-old", "text": "old", "role": "user", "completed": True}
        one = self.make(first, "ses_one"); one.start()
        self.assertTrue(one.stop()["ok"])
        second = FakeAdapters(); second.planner = {"id": "user-old", "text": "old", "role": "user", "completed": True}
        two = self.make(second, "ses_two"); two.start()
        self.assertEqual(first.sent_worker, []); self.assertEqual(second.sent_worker, [])
        self.assertIsNone(two.state["unresolved"])
        self.assertTrue(two.stop()["ok"])

    def test_only_one_runtime_loop_and_old_ambiguous_state_stays_with_original_pair(self):
        first_adapters = FakeAdapters(); first_adapters.planner = {"id": "user-old", "text": "old", "role": "user", "completed": True}
        one = self.make(first_adapters, "ses_original")
        one.start()
        two = self.make(FakeAdapters(), "ses_replacement")
        # Multiple distinct pairs may run concurrently (per-pair locks).
        two.start()
        self.assertTrue(one.stop()["ok"])
        self.assertTrue(two.stop()["ok"])
        # Same pair cannot run twice concurrently.
        one.start()
        duplicate = self.make(FakeAdapters(), "ses_original")
        with self.assertRaisesRegex(RuntimeError, "already owns"):
            duplicate.start()
        first_adapters.ambiguous_worker = True
        first_adapters.planner = {"id": "planner-risk", "text": "hold me", "role": "assistant", "completed": True}
        self.assertTrue(self.wait_for(lambda: one.status()["unresolved"] is not None))
        one.stop()
        self.assertEqual(one.state["unresolved"]["source_id"], "planner-risk")
        two.start()
        self.assertIsNone(two.status()["unresolved"])
        two.stop()

    def test_start_baselines_historical_planner_message_without_replay(self):
        adapters = FakeAdapters()
        adapters.planner = {"id": "planner-latest", "text": "resume this", "role": "assistant", "completed": True}
        runner = self.make(adapters)
        runner.start()
        time.sleep(.05)
        self.assertEqual(adapters.sent_worker, [])
        adapters.planner = {"id": "planner-new", "text": "new message", "role": "assistant", "completed": True}
        self.assertTrue(self.wait_for(lambda: len(adapters.sent_worker) == 1))
        self.assertEqual(adapters.sent_worker, [("ses_one", "new message")])
        runner.stop()

    def test_start_waits_for_active_worker_then_relays_its_response(self):
        adapters = FakeAdapters(); adapters.worker_busy = True
        adapters.planner = {"id": "planner-already-sent", "text": "job", "role": "assistant", "completed": True}
        runner = self.make(adapters)
        runner.start()
        self.assertEqual(runner.state["expected_speaker"], "worker")
        adapters.worker = {"id": "worker-finished", "text": "report", "role": "assistant", "completed": True}
        adapters.worker_busy = False
        self.assertTrue(self.wait_for(lambda: len(adapters.sent_planner) == 1))
        self.assertEqual(adapters.sent_planner[0][1], "report")
        runner.stop()

    def test_start_waits_for_active_planner_then_relays_it(self):
        adapters = FakeAdapters()
        adapters.planner = {"id": "planner-stream", "text": "partial", "role": "assistant", "completed": False,
                            "all_turns": [{"id": "planner-previous", "text": "old"}]}
        runner = self.make(adapters)
        runner.start()
        self.assertEqual(adapters.sent_worker, [])
        adapters.planner = {"id": "planner-stream", "text": "complete job", "role": "assistant", "completed": True}
        self.assertTrue(self.wait_for(lambda: len(adapters.sent_worker) == 1))
        self.assertEqual(adapters.sent_worker[0][1], "complete job")
        runner.stop()

    def test_idle_restart_preserves_watermark_without_replaying_same_planner_message(self):
        adapters = FakeAdapters(); adapters.auto_ack = False
        adapters.planner = {"id": "old", "text": "history", "role": "user", "completed": True}
        first = self.make(adapters); first.start()
        adapters.planner = {"id": "planner-once", "text": "one delivery", "role": "assistant", "completed": True}
        self.assertTrue(self.wait_for(lambda: len(adapters.sent_worker) == 1))
        self.assertTrue(self.wait_for(lambda: len(adapters.sent_planner) == 1))
        first.stop()
        restarted = self.make(adapters); restarted.start()
        time.sleep(.05)
        self.assertEqual(adapters.sent_worker, [("ses_one", "one delivery")])
        restarted.stop()

    def test_start_refuses_to_guess_when_both_sides_are_active(self):
        adapters = FakeAdapters(); adapters.worker_busy = True
        adapters.planner = {"id": "planner-stream", "text": "partial", "role": "assistant", "completed": False}
        runner = self.make(adapters)
        with self.assertRaisesRegex(RuntimeError, "both active"):
            runner.start()
        self.assertFalse(runner.running)
        self.assertEqual(adapters.sent_worker, [])
        self.assertEqual(adapters.sent_planner, [])

    def test_planner_wait_times_out_after_configured_window(self):
        adapters = FakeAdapters()
        adapters.planner = {"id": "user-old", "text": "old", "role": "user", "completed": True}
        runner = RelayRunner(self.tmp.name, "https://chatgpt.com/c/planner", "ses_one",
                             adapters, interval=.005, planner_wait_timeout=.05)
        runner.start()
        self.assertTrue(self.wait_for(lambda: runner.status()["phase"] == "planner_timeout"
                                      and not runner.running, timeout=1))
        self.assertFalse(runner.running)
        self.assertIsNone(runner.state["pending"])
        self.assertIsNone(runner.state["unresolved"])
        self.assertTrue(any(row["kind"] == "timeout" for row in runner.state["activity"]))
        adapters.planner = {"id": "planner-after-timeout", "text": "resume", "role": "assistant", "completed": True}
        resumed = self.make(adapters)
        resumed.start()
        time.sleep(.05)
        self.assertEqual(adapters.sent_worker, [])
        resumed.stop()

    def test_worker_delivery_has_no_arbitrary_execution_timeout(self):
        calls = []
        options = []
        def fake_process(command, **kwargs):
            calls.append(command)
            options.append(kwargs)
            return SimpleNamespace(returncode=0, stdout='{}', stderr='')
        adapter = LiveAdapters(self.tmp.name, runner=fake_process)
        receipt = adapter.deliver_worker("ses_selected", "Worker response payload")
        self.assertTrue(receipt["confirmed"])
        self.assertEqual(receipt["provenance"], "opencode.run.completed")
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0][-1], "Worker response payload")
        self.assertNotIn("timeout", options[0])

    def test_worker_busy_uses_session_metadata_and_caches_unchanged_export(self):
        calls = []
        updated = 10
        busy = 7
        idle = 8
        exported = {"messages": [{"type": "assistant", "info": {"id": "m1", "role": "assistant",
                    "time": {"completed": 7}}, "parts": [{"type": "text", "text": "done"}]}]}
        def fake_process(command, **kwargs):
            calls.append(command[1:3])
            if command[1] == "api":
                return SimpleNamespace(returncode=0, stdout=json.dumps({"data": {"id": "ses_x", "time": {
                    "updated": updated, "busy": busy, "idle": idle}}}), stderr="")
            kwargs["stdout"].write(json.dumps(exported))
            return SimpleNamespace(returncode=0, stderr="")
        adapter = LiveAdapters(self.tmp.name, runner=fake_process)
        state = adapter.worker_state("ses_x")
        self.assertFalse(state["busy"])
        self.assertEqual(state["latest"]["id"], "m1")
        adapter.worker_state("ses_x")
        self.assertEqual(calls.count(["session", "export"]), 1)
        busy = 12
        self.assertTrue(adapter.worker_state("ses_x")["busy"])
        self.assertEqual(calls.count(["session", "export"]), 1)

    def test_production_receipt_matcher_excludes_history_and_accepts_markdown_rendering(self):
        messages = [
            {"id": "history", "text": "Same `payload`"},
            {"id": "new", "text": "Same payload"},
        ]
        receipt = LiveAdapters.match_planner_receipt(messages, "Same `payload`", "planner", ["history"])
        self.assertEqual(receipt["destination_message_id"], "new")
        self.assertIsNone(LiveAdapters.match_planner_receipt(messages[:1], "Same `payload`", "planner", ["history"]))
        with self.assertRaisesRegex(RuntimeError, "multiple post-baseline"):
            LiveAdapters.match_planner_receipt(messages[1:] + [{"id": "newer", "text": "Same payload"}],
                                               "Same `payload`", "planner", [])

    def test_production_receipt_matcher_accepts_one_new_message_with_rendered_text_difference(self):
        expected = "## Verification\n\n" + ("A long complete Planner report with detailed findings. " * 18)
        rendered = "Planner received this report but DOM extraction omitted its final section."
        receipt = LiveAdapters.match_planner_receipt(
            [{"id": "new-rendered", "text": rendered}], expected, "planner", [])
        self.assertEqual(receipt["destination_message_id"], "new-rendered")
        self.assertEqual(receipt["match_method"], "unique_post_baseline_user_message")
        self.assertFalse(receipt["content_match"])

    def test_production_receipt_matcher_accepts_rendered_link_and_table_layout(self):
        expected = ("**Report**\n\n- **Run:** [42168685902](https://example.test/run)\n\n"
                    "| Assertion | Result |\n|---|---|\n" + "| Browser custody | **NOT VERIFIED** |\n" * 18)
        rendered = ("**Report**\n- **Run:** [42168685902]\\([https://example.test/run](https://example.test/run))\n"
                    "| Assertion | Result ||---|---|" + "| Browser custody | **NOT VERIFIED** |" * 18)
        receipt = LiveAdapters.match_planner_receipt(
            [{"id": "rendered-link", "text": rendered}], expected, "planner", [])
        self.assertEqual(receipt["destination_message_id"], "rendered-link")
        self.assertFalse(receipt["content_match"])

    def test_production_receipt_matcher_confirms_one_new_message_but_marks_content_mismatch(self):
        expected = "A long complete Planner report with detailed findings. " * 18
        different = "A different long Planner report with different findings. " * 18
        receipt = LiveAdapters.match_planner_receipt(
            [{"id": "new-different", "text": different}], expected, "planner", [])
        self.assertEqual(receipt["destination_message_id"], "new-different")
        self.assertFalse(receipt["content_match"])

    def test_confirm_receipt_state_is_discarded_on_new_session(self):
        adapters = FakeAdapters()
        runner = self.make(adapters)
        operation = runner._new_operation("worker_to_planner", "worker-source", "one response")
        operation.update({"stage": "submitting", "submission_attempted": True,
                          "destination_baseline_ids": ["old"]})
        runner._begin_operation(operation)
        runner._set_operation(operation, "submitting")
        adapters.planner_messages.append({"id": "receipt-once", "text": "one response"})
        save = runner._save
        def interrupted_save(): raise OSError("simulated crash before atomic state commit")
        runner._save = interrupted_save
        with self.assertRaisesRegex(OSError, "simulated crash"):
            runner.confirm_delivery(operation, runner._planner_receipt(operation))
        runner._save = save

        recovered = self.make(adapters)
        recovered.start()
        self.assertEqual(recovered.state["last_worker_id"], "worker-old")
        self.assertIsNone(recovered.state["last_confirmed_turn"])
        self.assertEqual(adapters.sent_planner, [])
        recovered.stop()

    def test_live_planner_adapter_separates_read_only_verification_from_single_submit(self):
        calls = []
        def fake_process(command, **kwargs):
            calls.append(command)
            if command[2] == "verify":
                return SimpleNamespace(returncode=0, stdout='{"ok":true,"conversation_id":"planner","messages":[]}', stderr="")
            return SimpleNamespace(returncode=0, stdout='{"submitted":true,"conversation_id":"planner"}', stderr="")
        adapter = LiveAdapters(self.tmp.name, runner=fake_process)
        self.assertEqual(adapter.verify_delivery("https://chatgpt.com/c/planner"), [])
        self.assertEqual(adapter.submit_message("https://chatgpt.com/c/planner", "one payload")["submitted"], True)
        self.assertEqual(calls[0][2], "verify")
        self.assertEqual(calls[1][2], "submit")
        self.assertEqual(calls[1][-1], "one payload")

    def test_live_planner_adapter_wait_path_tolerates_delayed_message_identity(self):
        def fake_process(command, **kwargs):
            return SimpleNamespace(returncode=0, stdout='{"ok":true,"conversation_id":"planner",'
                '"messages":[{"id":null,"text":"new message"}]}', stderr="")
        adapter = LiveAdapters(self.tmp.name, runner=fake_process)
        self.assertIsNone(adapter.verify_delivery("https://chatgpt.com/c/planner", require_complete=False)[0]["id"])
        with self.assertRaisesRegex(RuntimeError, "incomplete message identities"):
            adapter.verify_delivery("https://chatgpt.com/c/planner", require_complete=True)

    def test_production_planner_adapter_preserves_completion_and_conversation_identity(self):
        outputs = [
            '{"conversation_url":"https://chatgpt.com/c/planner","conversation_id":"planner",'
            '"message_identifier":"m1","extracted_text":"partial","latest_message_is_assistant":true,'
            '"completion_status":"streaming","diagnostics":{"rendered":true,"tab_state":{"authBlocked":false}}}',
            '{"conversation_url":"https://chatgpt.com/c/other","conversation_id":"other",'
            '"message_identifier":"m2","extracted_text":"wrong","latest_message_is_assistant":true,'
            '"completion_status":"completed","diagnostics":{"rendered":true,"tab_state":{"authBlocked":false}}}',
        ]
        def fake_process(command, **kwargs):
            return SimpleNamespace(returncode=0, stdout=outputs.pop(0), stderr="")
        adapter = LiveAdapters(self.tmp.name, runner=fake_process)
        self.assertFalse(adapter.planner_latest("https://chatgpt.com/c/planner")["completed"])
        with self.assertRaisesRegex(RuntimeError, "different conversation"):
            adapter.planner_latest("https://chatgpt.com/c/planner")

    def test_delayed_planner_receipt_is_verified_without_a_second_submit(self):
        adapters = FakeAdapters()
        adapters.auto_ack = False
        adapters.delay_receipt_polls = 2
        runner = RelayRunner(self.tmp.name, "https://chatgpt.com/c/planner", "ses_one", adapters,
                             interval=.005, receipt_timeout=.5)
        adapters.planner = {"id": "old-user", "text": "old", "role": "user", "completed": True}
        runner.start()
        adapters.planner = {"id": "new-planner", "text": "request", "role": "assistant", "completed": True}
        adapters.worker = {"id": "worker-new", "text": "response", "role": "assistant", "completed": True}
        self.assertTrue(self.wait_for(lambda: len(adapters.sent_planner) == 1))
        self.assertTrue(self.wait_for(lambda: runner.state["last_confirmed_turn"] is not None))
        self.assertEqual(len(adapters.sent_planner), 1)
        self.assertEqual(runner.state["last_confirmed_turn"]["worker_id"], "worker-reply-1")
        runner.stop()

    def test_worker_report_waits_for_planner_readiness_before_submission(self):
        class PlannerBusyFake(FakeAdapters):
            def __init__(self):
                super().__init__()
                self.readiness_checks = 0
            def planner_readiness(self, url, expected_text=None):
                self.readiness_checks += 1
                busy = self.readiness_checks < 3
                return {"ready": not busy, "busy": busy,
                        "reason": "Planner is generating" if busy else ""}

        adapters = PlannerBusyFake()
        runner = RelayRunner(self.tmp.name, "https://chatgpt.com/c/planner", "ses_one", adapters,
                             planner_ready_timeout=.5, planner_ready_poll_interval=.001,
                             receipt_timeout=.1)
        operation = runner._new_operation("worker_to_planner", "worker-ready-1", "worker report")
        runner._begin_operation(operation)
        operation["stage"] = "waiting_for_planner_ready"
        runner._set_operation(operation, "waiting_for_planner_ready")

        runner._submit_and_confirm_planner(operation)

        self.assertEqual(adapters.readiness_checks, 3)
        self.assertEqual(len(adapters.sent_planner), 1)
        self.assertEqual(runner.state["last_confirmed_delivery"]["direction"], "worker_to_planner")
        messages = [event["message"] for event in runner.state["activity"]]
        self.assertTrue(any("Planner is generating" in message and "not sent" in message for message in messages))

    def test_planner_receipt_wait_refreshes_once_then_fails_after_second_window(self):
        class RefreshFake(FakeAdapters):
            def __init__(self):
                super().__init__()
                self.refreshes = 0
            def refresh_planner(self, url):
                self.refreshes += 1
                return list(self.planner_messages)

        adapters = RefreshFake()
        runner = RelayRunner(self.tmp.name, "https://chatgpt.com/c/planner", "ses_one",
                             adapters, receipt_timeout=.02, receipt_poll_interval=.002)
        operation = runner._new_operation("worker_to_planner", "worker-1", "one response")
        operation["destination_baseline_ids"] = []
        with self.assertRaisesRegex(RuntimeError, "two verification windows"):
            runner._wait_for_planner_receipt(operation)
        self.assertEqual(adapters.refreshes, 1)
        self.assertEqual(len(adapters.sent_planner), 0)

    def test_submit_timeout_is_held_only_for_current_session(self):
        adapters = FakeAdapters()
        adapters.auto_ack = False
        adapters.fail_after_submit = True
        adapters.delay_receipt_polls = 100
        adapters.planner = {"id": "old-user", "text": "old", "role": "user", "completed": True}
        runner = RelayRunner(self.tmp.name, "https://chatgpt.com/c/planner", "ses_one", adapters,
                             interval=.005, receipt_timeout=.05)
        runner.start()
        adapters.planner = {"id": "new-planner", "text": "request", "role": "assistant", "completed": True}
        self.assertTrue(self.wait_for(lambda: runner.status()["unresolved"] is not None))
        operation = runner.state["unresolved"]
        self.assertEqual(operation["direction"], "worker_to_planner")
        self.assertTrue(operation["submission_attempted"])
        submits = len(adapters.sent_planner)
        runner.stop()
        # A fresh Start establishes a new baseline and drops the prior hold.
        adapters.planner_messages.append({"id": "late-receipt", "text": operation["text"]})
        recovered = RelayRunner(self.tmp.name, "https://chatgpt.com/c/planner", "ses_one", adapters,
                                interval=.005, receipt_timeout=.05)
        recovered.start()
        self.assertEqual(len(adapters.sent_planner), submits)
        self.assertIsNone(recovered.state["unresolved"])
        self.assertIsNone(recovered.state["last_confirmed_turn"])
        recovered.stop()


if __name__ == "__main__": unittest.main()
