import unittest
from datetime import timedelta
from decimal import Decimal

from nft_tracker import valuation as v
from tests.helpers import NOW, ev, sales


class Valuation(unittest.TestCase):
    def test_exact_discount_30_signals(self):
        sig = v.evaluate(ev(price="70"), sales(["100", "100", "100"]), 30, NOW)
        self.assertIsNotNone(sig)
        self.assertEqual(sig.shown_discount, Decimal("30.0"))
        self.assertEqual(sig.estimate.tier, "exact")

    def test_just_below_threshold_no_signal(self):
        self.assertIsNone(v.evaluate(ev(price="70.01"), sales(["100", "100", "100"]), 30, NOW))

    def test_display_never_rounds_up_past_threshold(self):
        sig = v.evaluate(ev(price="69.96"), sales(["100", "100", "100"]), 30, NOW)
        self.assertEqual(sig.shown_discount, Decimal("30.0"))  # 30.04 -> 30.0, not 30.1

    def test_formula(self):
        self.assertEqual(v.discount_pct(Decimal("60"), Decimal("100")), Decimal("40"))

    def test_too_few_sales(self):
        self.assertIsNone(v.evaluate(ev(), sales(["100", "100"]), 30, NOW))

    def test_other_backdrop_not_comparable_for_exact(self):
        s = sales(["100"] * 3, backdrop="Purple")
        # exact tier fails; model tier needs 5 sales -> no signal
        self.assertIsNone(v.evaluate(ev(), s, 30, NOW))

    def test_model_tier_needs_five_and_extra_margin(self):
        s = [x for i, b in enumerate(["A", "B", "C", "D", "E"])
             for x in sales(["100"], backdrop=b, symbol=f"sym{i}") ]
        for i, x in enumerate(s):  # make gift ids unique
            s[i] = ev("purchase", "100", gift_id=f"m{i}", when=NOW - timedelta(hours=i + 1),
                      backdrop="ABCDE"[i], symbol=f"sym{i}")
        self.assertIsNone(v.evaluate(ev(price="70"), s, 30, NOW))       # 30% < 30+10
        sig = v.evaluate(ev(price="59"), s, 30, NOW)                     # 41%
        self.assertIsNotNone(sig)
        self.assertEqual(sig.estimate.tier, "model")

    def test_missing_attributes_never_match_each_other(self):
        s = sales(["100"] * 3, model="", backdrop="", symbol="")
        self.assertIsNone(v.evaluate(ev(model="", backdrop="", symbol=""), s, 30, NOW))

    def test_outlier_sale_is_trimmed(self):
        s = sales(["100", "101", "99", "100", "1000"])
        est = v.estimate(ev(), s, NOW)
        self.assertIsNotNone(est)
        self.assertEqual(est.value, Decimal("100"))
        self.assertEqual(est.sales, 4)

    def test_noisy_data_gives_no_signal(self):
        self.assertIsNone(v.estimate(ev(), sales(["10", "100", "250"]), NOW))

    def test_stale_data_gives_no_signal(self):
        old = [ev("purchase", "100", gift_id=f"o{i}", when=NOW - timedelta(days=5, hours=i)) for i in range(3)]
        self.assertIsNone(v.estimate(ev(), old, NOW))

    def test_recent_outlier_does_not_refresh_stale_comparables(self):
        stale = [ev("purchase", "100", gift_id=f"old{i}",
                    when=NOW - timedelta(days=5, hours=i)) for i in range(3)]
        anomalous = ev("purchase", "1000", gift_id="recent_outlier",
                       when=NOW - timedelta(hours=1))
        self.assertIsNone(v.estimate(ev(), stale + [anomalous], NOW))

    def test_sales_after_listing_or_too_old_ignored(self):
        future = [ev("purchase", "100", gift_id=f"f{i}", when=NOW + timedelta(hours=i + 1)) for i in range(3)]
        ancient = [ev("purchase", "100", gift_id=f"a{i}", when=NOW - timedelta(days=30)) for i in range(3)]
        self.assertIsNone(v.estimate(ev(), future + ancient, NOW))

    def test_own_previous_sale_excluded(self):
        s = sales(["100", "100"]) + [ev("purchase", "100", gift_id="g1", when=NOW - timedelta(hours=9))]
        self.assertIsNone(v.estimate(ev(), s, NOW))

    def test_purchase_events_never_signal(self):
        self.assertIsNone(v.evaluate(ev("purchase", "10"), sales(["100"] * 3), 30, NOW))

    def test_decimal_precision_no_float_error(self):
        sig = v.evaluate(ev(price="0.7"), sales(["1", "1", "1"]), 30, NOW)
        self.assertIsNotNone(sig)  # 0.7 vs 1 is exactly 30%


if __name__ == "__main__":
    unittest.main()
