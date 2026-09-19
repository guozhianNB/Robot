"""Static readiness contract checks that do not require a ROS installation."""

from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[1]
INTERFACES = ROOT / "src" / "robot_interfaces"
NAVIGATION = ROOT / "src" / "robot_navigation" / "robot_navigation" / "robot_actions.py"


def _source() -> str:
    return NAVIGATION.read_text(encoding="utf-8")


class ReadinessContractTests(unittest.TestCase):
    def test_readiness_service_has_expected_response_fields(self):
        srv_path = INTERFACES / "srv" / "RobotReadiness.srv"
        self.assertTrue(srv_path.is_file(), "RobotReadiness.srv must exist")
        if not srv_path.is_file():
            return
        srv = srv_path.read_text(encoding="utf-8")
        request, response = srv.split("---", maxsplit=1)
        self.assertFalse(request.strip())
        self.assertEqual(
            [line.strip() for line in response.splitlines() if line.strip()],
            [
                "bool ready",
                "string exec_state",
                "bool nav_available",
                "string message",
            ],
        )

    def test_readiness_service_is_registered_in_interface_generation(self):
        cmake = (INTERFACES / "CMakeLists.txt").read_text(encoding="utf-8")
        self.assertIn('"srv/RobotReadiness.srv"', cmake)

    def test_robot_actions_persists_state_before_publishing_and_heartbeats(self):
        source = _source()
        self.assertIn('self._exec_state = "idle"', source)
        self.assertIn("self._exec_state = state", source)
        self.assertIn("self._exec_state_pub.publish(String(data=self._exec_state))", source)
        self.assertRegex(source, r"self\.create_timer\(\s*1\.0\s*,")

    def test_readiness_uses_its_own_callback_group_and_nonblocking_nav_probe(self):
        source = _source()
        self.assertIn("self._readiness_group = MutuallyExclusiveCallbackGroup()", source)
        self.assertIn("RobotReadiness, \"robot/readiness\"", source)
        self.assertIn("callback_group=self._readiness_group", source)
        self.assertIn("self._nav_client.server_is_ready()", source)
        self.assertIn('response.ready = self._exec_state == "idle"', source)
        self.assertIn("response.nav_available = nav_available", source)
        readiness_callback = source.split("def _on_readiness", 1)[-1].split("def ", 1)[0]
        self.assertNotIn("wait_for_server", readiness_callback)


if __name__ == "__main__":
    unittest.main()
