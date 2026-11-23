"""Run: python -m unittest discover tests"""
import unittest

from autofill.guard import GuardError, check


class GuardTests(unittest.TestCase):
    def test_refuses_submit_like_buttons(self):
        for text in ("Submit application", "Submit Application", "SUBMIT", "Send application", "Finish",
                     "Complete", "Confirm", "Review and Submit", "Done", "submit"):
            for purpose, chosen in (("nav", None), ("option", text)):
                with self.assertRaises(GuardError, msg=f"{text} / {purpose}"):
                    check(text, purpose, chosen)

    def test_allows_navigation(self):
        for text in ("Next", "Continue", "Save and Continue", "Apply Manually", "Continue with Google",
                     "Sign in with Google", "Add Another", "Apply"):
            check(text, "nav")

    def test_nav_must_be_on_list(self):
        for text in ("Delete", "Withdraw", "Autofill with Resume", "Yes", ""):
            with self.assertRaises(GuardError):
                check(text, "nav")

    def test_option_must_match_choice(self):
        check("Yes", "option", "Yes")
        check(" No ", "option", "no")
        with self.assertRaises(GuardError):
            check("Yes", "option", "No")
        with self.assertRaises(GuardError):
            check("Yes", "option", None)


if __name__ == "__main__":
    unittest.main()
