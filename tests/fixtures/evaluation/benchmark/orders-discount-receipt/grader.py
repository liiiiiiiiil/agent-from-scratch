import contextlib
import io
import json
from pathlib import Path
import sys
import unittest

WORKSPACE = Path(sys.argv[1]).resolve()
sys.path.insert(0, str(WORKSPACE / "src"))


class OrderReceiptBehavior(unittest.TestCase):
    def test_discount_rounds_half_up_to_cents(self):
        from pricing import calculate_discount_cents, amount_due_cents
        self.assertEqual(calculate_discount_cents(130, 15), 20)
        self.assertEqual(amount_due_cents(130, 15), 110)

    def test_discount_and_receipt_are_consistent(self):
        from receipt import render_receipt
        self.assertEqual(
            render_receipt(1235, 15),
            "Subtotal: $12.35\nDiscount (15%): -$1.85\nAmount due: $10.50\n",
        )

    def test_receipt_uses_integer_cents(self):
        from receipt import render_receipt
        self.assertIn("Amount due: $1.10\n", render_receipt(130, 15))

    def test_invalid_inputs_are_rejected(self):
        from pricing import calculate_discount_cents
        for subtotal, percent in ((-1, 10), (100, -1), (100, 101)):
            with self.subTest(subtotal=subtotal, percent=percent):
                with self.assertRaises(ValueError):
                    calculate_discount_cents(subtotal, percent)


stream = io.StringIO()
with contextlib.redirect_stdout(stream):
    result = unittest.TextTestRunner(stream=stream, verbosity=1).run(
        unittest.defaultTestLoader.loadTestsFromTestCase(OrderReceiptBehavior)
    )
print(json.dumps({"passed": result.wasSuccessful(), "detail": stream.getvalue()[-4000:]}, ensure_ascii=False))
