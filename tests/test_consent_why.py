"""Consent rules and the free honesty checks on 'why this company' answers."""
import unittest
from pathlib import Path

import yaml

from autofill import engine, why

HERE = Path(__file__).resolve().parent.parent
P = yaml.safe_load(((HERE / "profile.yaml") if (HERE / "profile.yaml").exists()
                    else (HERE / "profile.example.yaml")).read_text(encoding="utf-8"))


def consent(label, options=(), kind=None, description=""):
    q = {"key": "k", "label": label, "options": list(options), "description": description,
         "kind": kind or ("radio" if options else "checkbox")}
    r = engine.consent_answer(q, P)
    return r and r[0]


class Consent(unittest.TestCase):
    def test_privacy_yes(self):
        self.assertEqual(consent("I consent to the processing of my personal data as described in the privacy notice"),
                         "Yes")

    def test_ai_notetaker_yes(self):
        self.assertEqual(consent("Do you consent to an AI notetaker recording your interview?", ["Yes", "No"]), "Yes")

    def test_sms_yes(self):
        self.assertEqual(consent("I agree to receive text messages about my application"), "Yes")

    def test_marketing_no(self):
        self.assertEqual(consent("Would you like to receive marketing text messages?", ["Yes", "No"]), "No")
        self.assertEqual(consent("Join our talent community newsletter", ["I consent", "I do not consent"]),
                         "I do not consent")

    def test_certification_pauses(self):
        self.assertIsNone(consent("I certify that the information provided is true and accurate and I consent to "
                                  "processing of my data"))
        q = {"key": "k", "label": "I confirm that I am legally authorized to work in the US", "kind": "checkbox",
             "options": []}
        self.assertEqual(engine.ask_ali_topic(q), "legal attestation")

    def test_criminal_not_consent(self):
        self.assertIsNone(consent("Do you consent to a review of your criminal record?", ["Yes", "No"]))

    def test_not_a_consent(self):
        self.assertIsNone(consent("Do you have experience with recording studios?", ["Yes", "No"]))


class WhyChecks(unittest.TestCase):
    RESUME = "Projects: SixSense, a sensor glasses project in Python and C++ on a Raspberry Pi."
    ALLOWED = why._words(RESUME)
    URLS = {"https://example.com/robots"}

    def r(self, **kw):
        base = {"honest_match": True, "company_project": "warehouse robots", "source_url": "https://example.com/robots",
                "my_project": "SixSense", "my_tools": ["Python", "C++"],
                "answer": "I really like your warehouse robots. I built something similar (SixSense) using Python "
                          "and C++. I enjoy this work and would like to continue it at Example."}
        base.update(kw)
        return base

    def test_ok(self):
        self.assertEqual(why.problems(self.r(), self.ALLOWED, self.RESUME, self.URLS), [])

    def test_tool_not_on_resume(self):
        p = why.problems(self.r(my_tools=["Python", "Kubernetes"]), self.ALLOWED, self.RESUME, self.URLS)
        self.assertTrue(any("Kubernetes" in x for x in p))

    def test_unsourced_fact(self):
        p = why.problems(self.r(source_url="https://made-up.example"), self.ALLOWED, self.RESUME, self.URLS)
        self.assertTrue(any("source" in x for x in p))

    def test_invented_project(self):
        p = why.problems(self.r(my_project="Autonomous drone swarm"), self.ALLOWED, self.RESUME, self.URLS)
        self.assertTrue(any("project not found" in x for x in p))

    def test_pasted_resume(self):
        p = why.problems(self.r(answer="Projects: SixSense, a sensor glasses project in Python and C++ on a "
                                       "Raspberry Pi."), self.ALLOWED, self.RESUME, self.URLS)
        self.assertTrue(any("copies the resume" in x for x in p))

    def test_too_long(self):
        p = why.problems(self.r(answer="word " * 120), self.ALLOWED, self.RESUME, self.URLS)
        self.assertTrue(any("too long" in x for x in p))


if __name__ == "__main__":
    unittest.main()
