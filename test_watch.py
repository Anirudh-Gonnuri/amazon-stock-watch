"""Offline tests for watch.py (parsing and alerting). Run: python3 -m unittest -v"""

import argparse
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import watch

URL = "https://www.amazon.in/dp/B0DSKL9MQ8"


def respond(body: str):
    """Patch urlopen so fetch_once receives `body` without touching the network."""
    resp = mock.MagicMock()
    resp.__enter__.return_value.read.return_value = body.encode()
    return mock.patch("urllib.request.urlopen", return_value=resp)


class FetchClassification(unittest.TestCase):
    def test_normal_product_page_is_returned(self):
        with respond('<span id="productTitle">Galaxy</span>'):
            self.assertIn("Galaxy", watch.fetch_once(URL))

    def test_captcha_is_blocked_not_layout_change(self):
        with respond("<form>Type the characters you see: captcha</form>"):
            with self.assertRaises(watch.Blocked) as ctx:
                watch.fetch_once(URL)
        self.assertNotIsInstance(ctx.exception, watch.LayoutChanged)

    def test_bot_check_challenge_is_blocked_not_layout_change(self):
        body = "<meta http-equiv=\"refresh\" content=\"5; URL='/dp/x?bm-verify=AAQ'\">"
        with respond(body):
            with self.assertRaises(watch.Blocked) as ctx:
                watch.fetch_once(URL)
        self.assertNotIsInstance(ctx.exception, watch.LayoutChanged)

    def test_unrecognised_page_is_layout_change(self):
        with respond("<html><h1 class='new-title'>Galaxy</h1></html>"):
            with self.assertRaises(watch.LayoutChanged):
                watch.fetch_once(URL)

    def test_layout_change_is_not_retried(self):
        with mock.patch.object(watch, "fetch_once",
                               side_effect=watch.LayoutChanged("x")) as f:
            with self.assertRaises(watch.LayoutChanged):
                watch.fetch(URL)
        self.assertEqual(f.call_count, 1)


class VariantMatching(unittest.TestCase):
    VARIANTS = {
        "B0DSKL9MQ8": ["12GB + 256GB", "No cost EMI", "Titanium Whitesilver"],
        "B0DSBTKP5Q": ["12GB + 512GB", "No cost EMI", "Titanium Silverblue"],
    }

    def test_no_match_when_colour_only_exists_in_other_size(self):
        self.assertIsNone(watch.find_variant(self.VARIANTS, ["512GB", "Whitesilver"]))

    def test_match_ignores_case_and_spacing(self):
        self.assertEqual(watch.find_variant(self.VARIANTS, ["512 gb", "silver blue"]),
                         "B0DSBTKP5Q")

    def test_missing_variant_table_is_layout_change(self):
        with self.assertRaises(watch.LayoutChanged):
            watch.parse_variants('<span id="productTitle">x</span>')


class Alerts(unittest.TestCase):
    """run() should alert once per event, and at the right threshold."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        patcher = mock.patch.object(watch, "STATE_FILE", Path(tmp.name) / "state.json")
        patcher.start()
        self.addCleanup(patcher.stop)
        self.args = argparse.Namespace(host="www.amazon.in", listing="B0DSKL9MQ8",
                                       match=["512GB", "Whitesilver"], heartbeat=False)

    def run_with(self, outcome, times=1):
        """Run `times` checks that each return/raise `outcome`; collect alert titles."""
        kwargs = {"side_effect": outcome} if isinstance(outcome, Exception) \
            else {"return_value": outcome}
        with mock.patch.object(watch, "check", **kwargs), \
             mock.patch.object(watch, "notify") as notify:
            for _ in range(times):
                watch.run(self.args)
        return [c.args[0] for c in notify.call_args_list]

    def test_in_stock_alerts_once_not_every_run(self):
        result = watch.Result("in_stock", asin="B0X", price="₹1,09,999", title="S25")
        self.assertEqual(len(self.run_with(result, times=3)), 1)

    def test_ordinary_blocks_alert_once_at_threshold(self):
        alerts = self.run_with(watch.Blocked("CAPTCHA"), times=watch.BLOCKED_ALERT_AFTER + 3)
        self.assertEqual(alerts, ["Amazon watcher can't see the page"])

    def test_redesign_alerts_sooner_with_its_own_message(self):
        alerts = self.run_with(watch.LayoutChanged("no title"),
                               times=watch.LAYOUT_ALERT_AFTER)
        self.assertEqual(alerts, ["Amazon watcher needs updating"])

    def test_single_odd_page_does_not_alert(self):
        self.assertEqual(self.run_with(watch.LayoutChanged("no title")), [])


    def test_heartbeat_summarises_and_resets_counters(self):
        result = watch.Result("not_listed")
        self.run_with(result, times=2)
        self.run_with(watch.Blocked("CAPTCHA"))
        self.args.heartbeat = True
        with mock.patch.object(watch, "check", return_value=result), \
             mock.patch.object(watch, "notify_ntfy") as push:
            watch.run(self.args)
        body = push.call_args.args[1]
        self.assertIn("3 checks ok, 1 blocked", body)
        self.assertEqual(push.call_args.kwargs["priority"], "low")
        state = watch.load_state()
        self.assertEqual((state["ok_since_heartbeat"], state["blocked_since_heartbeat"]), (0, 0))


if __name__ == "__main__":
    unittest.main()
