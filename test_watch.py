"""Offline tests for watch.py (parsing and alerting). Run: python3 -m unittest -v"""

import argparse
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import watch

URL = "https://www.amazon.in/dp/B0DSKL9MQ8"
MARKER = watch.AMAZON_MARKER


def respond(body: str):
    """Patch urlopen so fetch_once receives `body` without touching the network."""
    resp = mock.MagicMock()
    resp.__enter__.return_value.read.return_value = body.encode()
    return mock.patch("urllib.request.urlopen", return_value=resp)


class FetchClassification(unittest.TestCase):
    def test_normal_page_is_returned(self):
        with respond('<span id="productTitle">Galaxy</span>'):
            self.assertIn("Galaxy", watch.fetch_once(URL, MARKER))

    def test_captcha_is_blocked_not_layout_change(self):
        with respond("<form>Type the characters you see: captcha</form>"):
            with self.assertRaises(watch.Blocked) as ctx:
                watch.fetch_once(URL, MARKER)
        self.assertNotIsInstance(ctx.exception, watch.LayoutChanged)

    def test_bot_check_challenge_is_blocked_not_layout_change(self):
        body = "<meta http-equiv=\"refresh\" content=\"5; URL='/dp/x?bm-verify=AAQ'\">"
        with respond(body):
            with self.assertRaises(watch.Blocked) as ctx:
                watch.fetch_once(URL, MARKER)
        self.assertNotIsInstance(ctx.exception, watch.LayoutChanged)

    def test_unrecognised_page_is_layout_change(self):
        with respond("<html><h1 class='new-title'>Galaxy</h1></html>"):
            with self.assertRaises(watch.LayoutChanged):
                watch.fetch_once(URL, MARKER)

    def test_layout_change_is_not_retried(self):
        with mock.patch.object(watch, "fetch_once",
                               side_effect=watch.LayoutChanged("x")) as f:
            with self.assertRaises(watch.LayoutChanged):
                watch.fetch(URL, MARKER)
        self.assertEqual(f.call_count, 1)


class AmazonParsing(unittest.TestCase):
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

    def test_price_parsed_as_whole_rupees(self):
        page = ('<span id="productTitle"> S25 </span><span class="a-price-whole">1,09,999'
                '</span><div id="add-to-cart-button"></div>')
        self.assertEqual(watch.parse_amazon_product(page)[1], 109999)


def flipkart_page(price, availability="InStock", name="Galaxy S25 Ultra (Whitesilver, 512 GB)"):
    product = {"@context": "https://schema.org", "@type": "Product", "name": name,
               "offers": {"price": price, "priceCurrency": "INR",
                          "availability": f"https://schema.org/{availability}"}}
    return ('<script type="application/ld+json">{"@type":"BreadcrumbList"}</script>'
            f'<script type="application/ld+json">{json.dumps(product)}</script>')


class FlipkartParsing(unittest.TestCase):
    URL = "https://www.flipkart.com/x/p/itm1"

    def check(self, page, max_price=109999):
        with mock.patch.object(watch, "fetch", return_value=page):
            return watch.check_flipkart(self.URL, max_price)

    def test_reads_product_block_among_other_json_ld(self):
        name, price, in_stock = watch.parse_flipkart_product(flipkart_page(139999))
        self.assertEqual((price, in_stock), (139999, True))
        self.assertIn("Whitesilver", name)

    def test_above_target_is_not_good(self):
        r = self.check(flipkart_page(139999))
        self.assertEqual((r.status, r.good), ("above_target", False))

    def test_exactly_at_target_is_good(self):
        r = self.check(flipkart_page(109999))
        self.assertEqual((r.status, r.good), ("at_target", True))
        self.assertEqual(r.status_text, "at ₹1,09,999 (target ₹1,09,999)")

    def test_cheap_but_sold_out_is_not_good(self):
        r = self.check(flipkart_page(99999, availability="OutOfStock"))
        self.assertEqual((r.status, r.good), ("out_of_stock", False))

    def test_decimal_and_string_prices(self):
        self.assertEqual(watch.parse_flipkart_product(flipkart_page("109999.00"))[1], 109999)

    def test_missing_product_block_is_layout_change(self):
        with self.assertRaises(watch.LayoutChanged):
            watch.parse_flipkart_product('<script type="application/ld+json">{}</script>')

    def test_missing_price_is_layout_change(self):
        page = flipkart_page(1).replace('"price": 1, ', "")
        with self.assertRaises(watch.LayoutChanged):
            watch.parse_flipkart_product(page)


class Formatting(unittest.TestCase):
    def test_indian_digit_grouping(self):
        cases = {999: "₹999", 1000: "₹1,000", 109999: "₹1,09,999", 12345678: "₹1,23,45,678"}
        for rupees, text in cases.items():
            self.assertEqual(watch.format_inr(rupees), text)


class Config(unittest.TestCase):
    def test_shipped_targets_json_loads(self):
        targets = {t.key: t for t in watch.load_targets()}
        self.assertIn(watch.LEGACY_TARGET_KEY, targets)
        self.assertEqual(targets["flipkart-s25u-whitesilver-512"].site, "Flipkart")

    def test_unknown_site_rejected(self):
        with self.assertRaises(ValueError):
            watch.build_target({"key": "a", "site": "ebay", "label": "x"})

    def test_duplicate_keys_rejected(self):
        entry = {"key": "a", "site": "amazon", "label": "x", "host": "h",
                 "listing": "B0", "match": ["y"]}
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "targets.json"
            path.write_text(json.dumps({"targets": [entry, entry]}))
            with self.assertRaises(ValueError):
                watch.load_targets(path)


class StateMigration(unittest.TestCase):
    def test_single_product_state_moves_under_its_key(self):
        old = {"status": "in_stock", "asin": "B0X", "price": "₹1,09,999", "blocked_streak": 1}
        ts = watch.migrate_state(old)["targets"][watch.LEGACY_TARGET_KEY]
        self.assertEqual((ts["status"], ts["blocked_streak"], ts["good"]),
                         ("in_stock", 1, True))
        self.assertNotIn("price", ts)  # old string price would break formatting

    def test_empty_state(self):
        self.assertEqual(watch.migrate_state({}), {"targets": {}})

    def test_current_format_untouched(self):
        state = {"targets": {"a": {"status": "x"}}}
        self.assertIs(watch.migrate_state(state), state)


def make_target(outcome, key="t", site="Amazon"):
    """A target whose check returns (or raises) `outcome`."""
    def check():
        if isinstance(outcome, Exception):
            raise outcome
        return outcome
    return watch.Target(key=key, site=site, label="S25 Ultra Whitesilver 512GB", check=check)


IN_STOCK = watch.Result("in_stock", good=True, status_text="in stock",
                        title="S25", price=109999, url=URL)
NOT_LISTED = watch.Result("not_listed", good=False, status_text="not listed yet")


class Alerts(unittest.TestCase):
    """run() should alert once per event, and at the right threshold."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        patcher = mock.patch.object(watch, "STATE_FILE", Path(tmp.name) / "state.json")
        patcher.start()
        self.addCleanup(patcher.stop)
        self.args = argparse.Namespace(only=None, heartbeat=False)

    def run_with(self, *targets, times=1):
        """Run `times` checks over `targets`; collect alert titles."""
        with mock.patch.object(watch, "notify") as notify:
            self.codes = [watch.run(self.args, list(targets)) for _ in range(times)]
        return [c.args[0] for c in notify.call_args_list]

    def test_good_status_alerts_once_not_every_run(self):
        alerts = self.run_with(make_target(IN_STOCK), times=3)
        self.assertEqual(alerts, ["Amazon: S25 Ultra Whitesilver 512GB is in stock!"])

    def test_losing_good_status_alerts(self):
        self.run_with(make_target(IN_STOCK))
        sold_out = watch.Result("out_of_stock", good=False, status_text="sold out")
        self.assertEqual(self.run_with(make_target(sold_out)),
                         ["Amazon: S25 Ultra Whitesilver 512GB is now sold out"])

    def test_first_appearance_alerts_even_if_not_buyable(self):
        self.run_with(make_target(NOT_LISTED))
        listed = watch.Result("out_of_stock", good=False,
                              status_text="listed, not buyable yet")
        self.assertEqual(len(self.run_with(make_target(listed))), 1)

    def test_price_drop_to_target_alerts_once_and_rise_alerts_again(self):
        above = watch.Result("above_target", good=False, status_text="above your target",
                             price=139999)
        hit = watch.Result("at_target", good=True, status_text="at ₹1,05,999",
                           price=105999)
        self.assertEqual(self.run_with(make_target(above, site="Flipkart"), times=2), [])
        self.assertEqual(self.run_with(make_target(hit, site="Flipkart"), times=2),
                         ["Flipkart: S25 Ultra Whitesilver 512GB is at ₹1,05,999!"])
        self.assertEqual(self.run_with(make_target(above, site="Flipkart")),
                         ["Flipkart: S25 Ultra Whitesilver 512GB is now above your target"])

    def test_ordinary_blocks_alert_once_at_threshold(self):
        alerts = self.run_with(make_target(watch.Blocked("CAPTCHA")),
                               times=watch.BLOCKED_ALERT_AFTER + 3)
        self.assertEqual(alerts, ["Amazon watcher can't see the page"])
        self.assertEqual(set(self.codes), {2})

    def test_redesign_alerts_sooner_with_its_own_message(self):
        alerts = self.run_with(make_target(watch.LayoutChanged("no title")),
                               times=watch.LAYOUT_ALERT_AFTER)
        self.assertEqual(alerts, ["Amazon watcher needs updating"])

    def test_single_odd_page_does_not_alert(self):
        self.assertEqual(self.run_with(make_target(watch.LayoutChanged("no title"))), [])

    def test_targets_keep_separate_state(self):
        # One target being blocked must not affect the other.
        alerts = self.run_with(make_target(watch.Blocked("CAPTCHA"), key="a"),
                               make_target(IN_STOCK, key="b", site="Flipkart"), times=2)
        self.assertEqual(alerts, ["Flipkart: S25 Ultra Whitesilver 512GB is in stock!"])
        state = watch.load_state()["targets"]
        self.assertEqual((state["a"]["blocked_streak"], state["b"]["blocked_streak"]), (2, 0))

    def test_only_filters_targets(self):
        self.args.only = ["b"]
        alerts = self.run_with(make_target(IN_STOCK, key="a"),
                               make_target(IN_STOCK, key="b", site="Flipkart"))
        self.assertEqual(alerts, ["Flipkart: S25 Ultra Whitesilver 512GB is in stock!"])

    def test_heartbeat_summarises_each_target_and_resets_counters(self):
        target = make_target(NOT_LISTED)
        self.run_with(target, times=2)
        self.run_with(make_target(watch.Blocked("CAPTCHA")))
        self.args.heartbeat = True
        with mock.patch.object(watch, "notify_ntfy") as push:
            watch.run(self.args, [target])
        body = push.call_args.args[1]
        self.assertIn("not listed yet. 3 checks ok, 1 blocked", body)
        self.assertEqual(push.call_args.kwargs["priority"], "low")
        ts = watch.load_state()["targets"]["t"]
        self.assertEqual((ts["ok_since_heartbeat"], ts["blocked_since_heartbeat"]), (0, 0))


if __name__ == "__main__":
    unittest.main()
