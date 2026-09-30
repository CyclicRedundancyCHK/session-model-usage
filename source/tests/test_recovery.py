import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from session_model_usage import controller


class RecoveryTests(unittest.TestCase):
    def process(self, port=None):
        process = Mock(pid=101)
        process.cmdline.return_value = [] if port is None else [f"--remote-debugging-port={port}"]
        process.create_time.return_value = 123.0
        return process

    def test_waits_for_manual_exit_of_normal_codex(self):
        self.assertFalse(controller.recovery_ready([self.process()]))

    def test_exit_allows_recovery(self):
        self.assertTrue(controller.recovery_ready([]))

    def test_one_debug_instance_can_reattach(self):
        self.assertTrue(controller.recovery_ready([self.process(1234)]))

    def test_invalid_debug_port_does_not_reattach(self):
        self.assertFalse(controller.recovery_ready([self.process(0)]))

    def test_multiple_instances_never_guess(self):
        self.assertFalse(controller.recovery_ready([self.process(1234), self.process(5678)]))

    def test_dead_companion_clears_old_view_and_message(self):
        with patch.object(controller, "read_state", return_value={"thread_id": "old",
                "message": "old minimized state", "overlay_visible": True, "badge_rect": [1,2,3,4]}), \
                patch.object(controller, "alive", return_value=False):
            state = controller.status()
        self.assertEqual(state["status"], "stopped")
        self.assertIsNone(state["thread_id"])
        self.assertIsNone(state["badge_rect"])
        self.assertFalse(state["overlay_visible"])
        self.assertNotEqual(state["message"], "old minimized state")

    def test_recovery_uses_gui_binary(self):
        with patch.object(controller.sys, "frozen", True, create=True), \
                patch.object(controller.sys, "executable", str(Path("runtime/session-usage.exe"))):
            command = controller.own_command("recovery", "--run-id", "test")
        self.assertEqual(Path(command[0]).name, "CodexSessionUsage.exe")
        self.assertEqual(command[1:], ["recovery", "--run-id", "test"])

    @unittest.skipUnless(os.name == "nt", "Windows launcher lock")
    def test_normal_launch_arms_one_waiter_without_stopping_codex(self):
        states = []
        app = self.process()
        with tempfile.TemporaryDirectory() as folder, \
                patch.object(controller, "state_directory", return_value=Path(folder)), \
                patch.object(controller, "status", return_value={"running": False}), \
                patch.object(controller, "running_apps", return_value=[app]), \
                patch.object(controller, "write_state", side_effect=lambda s: states.append(dict(s))), \
                patch.object(controller.subprocess, "Popen", return_value=Mock(pid=202)) as popen, \
                patch.object(controller.psutil, "Process", return_value=self.process()) as process, \
                patch.object(controller, "start_app") as start:
            result = controller.launch()
        self.assertEqual(result["status"], "waiting_for_restart")
        self.assertEqual(result["mode"], "recovery")
        self.assertFalse(result["overlay_visible"])
        self.assertIsNone(result["thread_id"])
        self.assertEqual(states[-1]["overlay_pid"], 202)
        self.assertIn("recovery", popen.call_args.args[0])
        app.terminate.assert_not_called()
        app.kill.assert_not_called()
        start.assert_not_called()
        process.assert_called_once_with(202)

    def test_repeated_launch_does_not_spawn_duplicate_waiter(self):
        state = {"running": True, "mode": "recovery", "run_id": "test", "overlay_pid": 202}
        with patch.object(controller, "status", return_value=state), \
                patch.object(controller.subprocess, "Popen") as popen:
            self.assertEqual(controller.launch(), state)
        popen.assert_not_called()

    @unittest.skipUnless(os.name == "nt", "Windows launcher lock")
    def test_another_process_cannot_resume_waiter(self):
        state = {"running": True, "mode": "recovery", "run_id": "test", "overlay_pid": -1}
        with patch.object(controller, "status", return_value=state), \
                patch.object(controller.subprocess, "Popen") as popen:
            self.assertEqual(controller.launch(recovery_run_id="test"), state)
        popen.assert_not_called()


if __name__ == "__main__":
    unittest.main()
