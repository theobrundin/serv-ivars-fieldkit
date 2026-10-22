"""Watchdog must respect maintenance, absent cameras and restart limits."""
from datetime import datetime, timedelta, timezone
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from tools.reolink_bench.watchdog import advance, camera_healthy


class WatchdogTests(unittest.TestCase):
    def step(self, state, now, healthy=False, ready=True, active=True):
        return advance(state, now=now, healthy=healthy,
                       camera_ready=ready, service_active=active)

    def test_startup_grace(self):
        state, restart = self.step({}, 100)
        self.assertFalse(restart)
        self.assertFalse(self.step(state, 180)[1])
        self.assertTrue(self.step(state, 191)[1])

    def test_reachable_stall_restarts_only_after_grace(self):
        state, _ = self.step({'seen_healthy': True}, 100)
        self.assertFalse(self.step(state, 129)[1])
        self.assertTrue(self.step(state, 131)[1])

    def test_unreachable_camera_does_not_churn(self):
        state, _ = self.step({'seen_healthy': True}, 100, ready=False)
        self.assertFalse(self.step(state, 300, ready=False)[1])
        self.assertTrue(self.step(state, 301, ready=True)[1])

    def test_restart_rate_is_bounded(self):
        state, _ = self.step({'seen_healthy': True, 'bad_since': 100}, 131)
        state, _ = self.step(state, 132)
        self.assertFalse(self.step(state, 310)[1])
        self.assertTrue(self.step(state, 311)[1])

    def test_transient_running_does_not_erase_outage(self):
        state = {'seen_healthy': True, 'bad_since': 100}
        state, _ = self.step(state, 125, healthy=True)
        self.assertEqual(state['bad_since'], 100)
        self.assertTrue(self.step(state, 131)[1])
        state, _ = self.step(state, 141, healthy=True)
        self.assertNotIn('bad_since', state)

    def test_intentional_stop_never_started(self):
        state, restart = self.step({'seen_healthy': True, 'bad_since': 100}, 500, active=False)
        self.assertFalse(restart)
        self.assertNotIn('bad_since', state)

    def test_stale_and_zero_health_running_status_rejected(self):
        now = datetime.now(timezone.utc)
        camera = {'status': 'running', 'health': 80, 'received_fps': 25,
                  'last_checked': (now - timedelta(seconds=5)).isoformat()}
        data = {'cameras': {'reolink-bench': camera}}
        self.assertTrue(camera_healthy(data, now))
        camera['health'] = 0
        self.assertFalse(camera_healthy(data, now))
        camera['health'] = 80
        camera['last_checked'] = (now - timedelta(seconds=30)).isoformat()
        self.assertFalse(camera_healthy(data, now))


if __name__ == '__main__':
    unittest.main()
