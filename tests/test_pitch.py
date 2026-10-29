import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pitch  # noqa: E402
from crustdata_api import Crustdata  # noqa: E402

SINCE = "2026-09-01"
TODAY = "2026-10-29"


def person(pid=1, left_end="2026-09-20T00:00:00", competitor="https://www.intel.com", current=None, title="Firmware Engineer"):
    if current is None:
        current = [{"name": "NewCo", "title": "Engineer", "company_website": "http://newco.io", "end_date": None}]
    return {
        "crustdata_person_id": pid,
        "basic_profile": {"name": f"P{pid}", "headline": "Embedded firmware", "location": {"country": "United States"}},
        "social_handles": {"professional_network_identifier": {"profile_url": f"https://example.org/in/p{pid}"}},
        "experience": {"employment_details": {
            "current": current,
            "past": [{"name": "Intel", "title": title, "company_website": competitor, "end_date": left_end,
                      "function_category": "Engineering", "seniority_level": "Director"}],
        }},
    }


ROLE_FW = {"title": "Senior Firmware Engineer", "keywords": ["firmware", "embedded"],
           "function": "Engineering", "country": "United States"}
ROLE_SALES = {"title": "Enterprise Account Executive", "keywords": ["sales"], "function": "Sales"}


class QualifyTests(unittest.TestCase):
    def test_passes_recent_leaver(self):
        rec = pitch.qualify(person(), "intel.com", SINCE, TODAY)
        self.assertEqual(rec["left_date"], "2026-09-20")
        self.assertEqual(rec["left_domain"], "intel.com")
        self.assertTrue(rec["available"] is False)  # already has a new role

    def test_available_when_no_current_role(self):
        self.assertTrue(pitch.qualify(person(current=[]), "intel.com", SINCE, TODAY)["available"])

    def test_rejects_still_employed_at_competitor(self):
        cur = [{"name": "Intel", "title": "Eng", "company_website": "http://intel.com"}]
        self.assertIsNone(pitch.qualify(person(current=cur), "intel.com", SINCE, TODAY))

    def test_rejects_other_employer(self):
        self.assertIsNone(pitch.qualify(person(competitor="http://amd.com"), "intel.com", SINCE, TODAY))

    def test_rejects_left_before_window(self):
        self.assertIsNone(pitch.qualify(person(left_end="2026-05-01T00:00:00"), "intel.com", SINCE, TODAY))

    def test_rejects_future_end_date(self):
        self.assertIsNone(pitch.qualify(person(left_end="2027-01-01T00:00:00"), "intel.com", SINCE, TODAY))


class MatchTests(unittest.TestCase):
    def setUp(self):
        self.rec = pitch.qualify(person(current=[]), "intel.com", SINCE, TODAY)

    def test_matches_best_role(self):
        out = pitch.match([self.rec], [ROLE_SALES, ROLE_FW])
        self.assertEqual(out[0]["role"], "Senior Firmware Engineer")
        self.assertGreaterEqual(out[0]["score"], 3)

    def test_drops_below_min_score(self):
        self.assertEqual(pitch.match([self.rec], [ROLE_SALES]), [])

    def test_sorted_best_first(self):
        weak = dict(self.rec, id=2, left_title="Hardware Engineer", headline="")
        out = pitch.match([weak, self.rec], [ROLE_FW])
        self.assertEqual(out[0]["id"], self.rec["id"])


class RunTests(unittest.TestCase):
    def _transport(self, calls):
        def transport(url, body, headers, timeout):
            calls.append((url, body, headers))
            return {"profiles": [person(1, current=[]), person(2), person(1, current=[])], "next_cursor": None}
        return transport

    def test_run_uses_documented_endpoint_and_dedupes(self):
        calls = []
        clients = [{"name": "Acme", "competitors": ["https://www.Intel.com/"], "open_roles": [ROLE_FW]}]
        # use a window that includes the fixture dates relative to today's real date
        res = pitch.run(Crustdata(api_key="test", transport=self._transport(calls)), clients, "2000-01-01", 10,
                        include_placed=True)
        self.assertTrue(calls[0][0].endswith("/person/search"))
        self.assertEqual(calls[0][2]["x-api-version"], "2025-11-01")
        conds = calls[0][1]["filters"]["conditions"]
        self.assertEqual(conds[0]["value"], "intel.com")
        self.assertEqual(res[0]["leavers"], 2)  # id 1 deduped
        self.assertEqual(len(res[0]["shortlist"]), 2)

    def test_placed_people_excluded_by_default(self):
        calls = []
        clients = [{"name": "Acme", "competitors": ["intel.com"], "open_roles": [ROLE_FW]}]
        res = pitch.run(Crustdata(api_key="test", transport=self._transport(calls)), clients, "2000-01-01", 10)
        self.assertEqual([c["id"] for c in res[0]["shortlist"]], [1])

    def test_only_domains_skips_other_competitors(self):
        calls = []
        clients = [{"name": "Acme", "competitors": ["intel.com", "amd.com"], "open_roles": [ROLE_FW]}]
        pitch.run(Crustdata(api_key="test", transport=self._transport(calls)), clients, "2000-01-01", 10,
                  only_domains=["amd.com"])
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0][1]["filters"]["conditions"][0]["value"], "amd.com")


class WatchTests(unittest.TestCase):
    def test_watch_body_and_endpoint(self):
        body = pitch.watch_body(["https://www.Intel.com", "amd.com", "intel.com"], "https://hooks.invalid/x")
        self.assertEqual(body["entities"]["domains"], ["amd.com", "intel.com"])
        self.assertEqual(body["track"], {"field": "headcount.total", "type": "decreased"})
        self.assertEqual(body["config"]["refresh_frequency_days"], 30)
        calls = []

        def transport(url, b, headers, timeout):
            calls.append((url, headers))
            return {"id": 7}

        Crustdata(api_key="test", transport=transport).watch_company(body)
        self.assertTrue(calls[0][0].endswith("/watch/company"))
        self.assertEqual(calls[0][1]["x-api-version"], "2025-11-01")

    def test_watch_without_webhook_is_pull_only(self):
        self.assertEqual(pitch.watch_body(["intel.com"])["notifications"], [])


class OutputTests(unittest.TestCase):
    def setUp(self):
        rec = pitch.qualify(person(current=[]), "intel.com", SINCE, TODAY)
        self.results = [{"client": "Acme", "competitors": ["intel.com"], "leavers": 3, "contact": "Jordan",
                         "shortlist": pitch.match([rec], [ROLE_FW])}]

    def test_summary(self):
        s = pitch.summary(self.results)
        self.assertEqual((s["clients"], s["leavers_found"], s["shortlisted"]), (1, 3, 1))
        self.assertEqual(s["by_open_role"], {"Senior Firmware Engineer": 1})

    def test_slack_text_has_no_names(self):
        text = pitch.slack_text(self.results)
        self.assertIn("1 candidates ready to pitch", text)
        self.assertIn("[Name]", text)
        self.assertNotIn("P1", text)

    def test_template_pitch_is_anonymised(self):
        text = pitch.draft_pitch({"name": "Acme", "contact": "Jordan"}, self.results[0]["shortlist"])
        self.assertIn("Hi Jordan", text)
        self.assertIn("Senior Firmware Engineer", text)
        self.assertNotIn("P1", text)

    def test_state_dedupe(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "seen.json")
            self.assertEqual(len(pitch.only_new(self.results, path)[0]["shortlist"]), 1)
            pitch.save_state(self.results, path)
            self.assertEqual(pitch.only_new(self.results, path)[0]["shortlist"], [])


if __name__ == "__main__":
    unittest.main()
