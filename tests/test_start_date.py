"""Start dates: May 2027 or the earliest 2027 option, never 2026 or earlier; in-person preference."""
import unittest
from pathlib import Path

import yaml

from autofill import engine

HERE = Path(__file__).resolve().parent.parent
P = yaml.safe_load(((HERE / "profile.yaml") if (HERE / "profile.yaml").exists()
                    else (HERE / "profile.example.yaml")).read_text(encoding="utf-8"))


def ans(label, options=(), kind=None):
    q = {"key": "k", "label": label, "options": list(options), "kind": kind or ("select" if options else "text")}
    return engine.form_answer(q, P)


class StartDate(unittest.TestCase):
    def test_floor_is_2027(self):
        self.assertGreaterEqual(engine.start_floor(P), (2027, 1))

    def test_text_field(self):
        self.assertEqual(ans("What is your earliest start date?")[0], "May 2027")

    def test_only_2026_options_asks(self):
        opts = ["March 2026", "April 2026", "May 2026", "June 2026"]
        self.assertIsNone(ans("If selected, when would you be able to start?", opts)[0])

    def test_may_2027_preferred(self):
        r = ans("When could you start?", ["December 2026", "January 2027", "May 2027", "June 2027"])
        self.assertEqual(r[:2], ("May 2027", True))

    def test_earliest_2027_when_no_may(self):
        r = ans("Available to start", ["Fall 2026", "Summer 2027", "Spring 2027"])
        self.assertEqual(r[:2], ("Spring 2027", False))

    def test_select_all_drops_2026(self):
        r = ans("Which terms are you available?", ["Summer 2026", "Fall 2026", "Summer 2027"], kind="multi")
        self.assertEqual(r[0], ["Summer 2027"])

    def test_education_start_not_touched(self):
        self.assertIsNone(ans("Education - Start date"))

    def test_too_early(self):
        floor = engine.start_floor(P)
        for v in ("May 2026", "05/2026", "2026-06-01", "Immediately"):
            self.assertTrue(engine.too_early(v, floor), v)
        for v in ("May 2027", "Yes", "Summer 2027"):
            self.assertFalse(engine.too_early(v, floor), v)

    def test_in_person(self):
        self.assertEqual(ans("What is your work arrangement preference?", ["Remote", "Hybrid", "On-site"])[0], "On-site")
        self.assertEqual(ans("Work arrangement preference")[0], "In-person")


if __name__ == "__main__":
    unittest.main()
