import math
import unittest

import pandas as pd

from app.services.analysis import StudyAnalysisService, constant_rater_ids, rating_token
from app.services.analysis_settings import default_analysis_settings, normalize_analysis_settings


def _frame(rows):
    return pd.DataFrame(rows)


class RatingTokenTests(unittest.TestCase):
    def test_blank_and_nan_are_missing(self):
        self.assertIsNone(rating_token(None))
        self.assertIsNone(rating_token(""))
        self.assertIsNone(rating_token("   "))
        self.assertIsNone(rating_token("nan"))
        self.assertIsNone(rating_token(float("nan")))
        self.assertIsNone(rating_token(math.nan))

    def test_numeric_spellings_match(self):
        self.assertEqual(rating_token(5), 5)
        self.assertEqual(rating_token(5.0), 5)
        self.assertEqual(rating_token("5"), 5)
        self.assertEqual(rating_token(" 5 "), 5)
        self.assertEqual(rating_token("5.0"), 5)

    def test_unparseable_text_stays_distinct(self):
        self.assertEqual(rating_token("five"), "five")
        self.assertEqual(rating_token(5.5), 5.5)


class ConstantRaterIdTests(unittest.TestCase):
    def test_all_fives_and_all_ones_are_dropped(self):
        ids = constant_rater_ids(
            ["a", "a", "b", "b"],
            [5, 5, 1, 1],
        )
        self.assertEqual(ids, {"a", "b"})

    def test_string_and_float_fives_are_the_same_score(self):
        ids = constant_rater_ids(["a", "a", "a"], ["5", 5, 5.0])
        self.assertEqual(ids, {"a"})

    def test_one_different_score_stays(self):
        ratings = [5] * 103 + [4]
        ids = constant_rater_ids(["a"] * len(ratings), ratings)
        self.assertEqual(ids, set())

    def test_single_score_and_all_blank_stay(self):
        ids = constant_rater_ids(
            ["one", "blank", "blank", "none"],
            [5, None, "", float("nan")],
        )
        self.assertEqual(ids, set())

    def test_blanks_between_matching_scores_still_drop(self):
        ids = constant_rater_ids(["a", "a", "a"], [5, "", 5])
        self.assertEqual(ids, {"a"})

    def test_number_and_unparseable_text_stay(self):
        ids = constant_rater_ids(["a", "a"], ["5", "five"])
        self.assertEqual(ids, set())

    def test_two_people_only_the_constant_one_drops(self):
        ids = constant_rater_ids(
            ["same", "same", "mixed", "mixed"],
            [5, "5", 5, 4],
        )
        self.assertEqual(ids, {"same"})


class ConstantRaterFrameTests(unittest.TestCase):
    def setUp(self):
        self.service = StudyAnalysisService()

    def _df(self):
        return _frame(
            [
                {"Panelist": "same", "Rating": 5, "Task": 1, "Gender": "Female", "Age": 30, "ResponseTime": 1.0, "Color_Red": 1},
                {"Panelist": "same", "Rating": "5", "Task": 2, "Gender": "Female", "Age": 30, "ResponseTime": 1.2, "Color_Red": 0},
                {"Panelist": "mixed", "Rating": 5, "Task": 1, "Gender": "Male", "Age": 40, "ResponseTime": 1.1, "Color_Red": 1},
                {"Panelist": "mixed", "Rating": 4, "Task": 2, "Gender": "Male", "Age": 40, "ResponseTime": 1.4, "Color_Red": 0},
                {"Panelist": "once", "Rating": 5, "Task": 1, "Gender": "Female", "Age": 22, "ResponseTime": 0.8, "Color_Red": 1},
            ]
        )

    def test_flag_off_keeps_everyone(self):
        kept = self.service._without_constant_raters(self._df(), {"exclude_constant_raters": False})
        self.assertEqual(set(kept["Panelist"]), {"same", "mixed", "once"})

    def test_flag_on_drops_only_the_constant_rater(self):
        kept = self.service._without_constant_raters(self._df(), {"exclude_constant_raters": True})
        self.assertEqual(set(kept["Panelist"]), {"mixed", "once"})

    def test_missing_rating_column_is_left_alone(self):
        df = _frame([{"Panelist": "a", "Task": 1}])
        kept = self.service._without_constant_raters(df, {"exclude_constant_raters": True})
        self.assertEqual(len(kept), 1)

    def test_json_report_counts_the_people_who_remain(self):
        study = {
            "title": "T",
            "categories": [{"id": "c", "name": "Color", "order": 0}],
            "elements": [{"name": "Red", "category": {"name": "Color", "order": 0}}],
        }
        options = default_analysis_settings()
        options["exclude_constant_raters"] = True
        report = self.service.generate_json_report(self._df(), study, include_raw_data=False, analysis_options=options)
        summary = report["dashboard_summary"]
        self.assertEqual(summary["uniquePanelists"], 2)
        self.assertEqual(summary["excludedConstantRaters"], 1)
        self.assertNotIn("filter_meta", report)

    def test_demographic_filter_then_constant_rater(self):
        study = {"title": "T", "categories": [], "elements": []}
        options = default_analysis_settings()
        options["exclude_constant_raters"] = True
        report = self.service.generate_json_report(
            self._df(),
            study,
            include_raw_data=True,
            analysis_options=options,
            filters={"genders": ["Female"]},
        )
        meta = report["filter_meta"]
        self.assertEqual(meta["panelists_after_filter"], 1)
        self.assertEqual(report["dashboard_summary"]["excludedConstantRaters"], 1)
        raw_people = {row["Panelist"] for row in report["RawData"]}
        self.assertEqual(raw_people, {"once"})

    def test_json_report_stays_empty_when_everyone_is_constant(self):
        df = _frame(
            [
                {"Panelist": "a", "Rating": 5, "Task": 1, "Gender": "Female", "Age": 30, "ResponseTime": 1, "Color_Red": 1},
                {"Panelist": "a", "Rating": 5, "Task": 2, "Gender": "Female", "Age": 30, "ResponseTime": 1, "Color_Red": 0},
            ]
        )
        options = default_analysis_settings()
        options["exclude_constant_raters"] = True
        report = self.service.generate_json_report(df, {"title": "T"}, include_raw_data=False, analysis_options=options)
        self.assertIn("every task the same", report["filter_meta"]["error"])
        self.assertEqual(report["dashboard_summary"]["total_respondents"] if isinstance(report.get("dashboard_summary"), dict) and "total_respondents" in report["dashboard_summary"] else report["(T) Overall"]["base_size"], 0)

    def test_filtered_regression_uses_the_same_rule(self):
        study = {
            "title": "T",
            "categories": [{"id": "c", "name": "Color", "order": 0}],
            "elements": [{"name": "Red", "category": {"name": "Color", "order": 0}}],
        }
        options = default_analysis_settings()
        options["exclude_constant_raters"] = True
        report = self.service.run_filtered_regression_report(
            study,
            self._df(),
            filters={},
            include_per_panelist=True,
            analysis_options=options,
        )
        people = {row["Panelist"] for row in report["per_panelist"]["top"]}
        self.assertNotIn("same", people)
        self.assertIn("mixed", people)
        self.assertEqual(report["meta"]["panelists_after_filter"], 2)

    def _two_group_study(self):
        question = "Do you use it?"
        rows = []

        def add(person, answer, ratings):
            for task, rating in enumerate(ratings, start=1):
                rows.append({
                    "Panelist": person,
                    "Rating": rating,
                    "Task": task,
                    "Gender": "Female" if answer == "Yes" else "Male",
                    "Age": 30,
                    "ResponseTime": 1.0 + task / 10,
                    "Color_Red": task % 2,
                    question: answer,
                })

        add("yes-all-5", "Yes", ["5", 5.0, 5, 5])
        add("yes-all-1", "Yes", [1, 1, 1, 1])
        add("yes-almost", "Yes", [5, 5, 5, 4])
        add("yes-mixed-a", "Yes", [1, 2, 3, 4])
        add("yes-mixed-b", "Yes", [5, 4, 5, 4])
        add("yes-once", "Yes", [5])
        add("no-all-3", "No", [3, 3, 3, 3])
        add("no-mixed-a", "No", [1, 5, 2, 4])
        add("no-mixed-b", "No", [2, 2, 2, 5])
        add("no-mixed-c", "No", [4, 3, 4, 3])
        add("no-mixed-d", "No", [1, 1, 1, 2])
        add("no-mixed-e", "No", [5, 1, 5, 1])
        study = {
            "title": "Two groups",
            "categories": [{"id": "c", "name": "Color", "order": 0}],
            "elements": [{"name": "Red", "category": {"name": "Color", "order": 0}}],
            "classification_questions": [{"question_text": question, "answer_options": ["Yes", "No"]}],
        }
        return _frame(rows), study, question

    def _segment_bases(self, report, question):
        section = report["(T) Classification Questions"]
        question_block = next(q for q in section["questions"] if q["question_text"] == question)
        return {ans: info["base_size"] for ans, info in question_block["segments"].items()}

    def test_two_groups_drop_their_own_straight_liners(self):
        df, study, question = self._two_group_study()
        off = default_analysis_settings()
        on = default_analysis_settings()
        on["exclude_constant_raters"] = True

        before = self.service.generate_json_report(df, study, include_raw_data=False, analysis_options=off)
        after = self.service.generate_json_report(df, study, include_raw_data=True, analysis_options=on)

        self.assertEqual(self._segment_bases(before, question), {"Yes": 6, "No": 6})
        self.assertEqual(self._segment_bases(after, question), {"Yes": 4, "No": 5})
        self.assertNotEqual(
            self._segment_bases(after, question)["Yes"],
            self._segment_bases(after, question)["No"],
        )
        self.assertEqual(after["dashboard_summary"]["excludedConstantRaters"], 3)

        kept = {row["Panelist"] for row in after["RawData"]}
        self.assertNotIn("yes-all-5", kept)
        self.assertNotIn("yes-all-1", kept)
        self.assertNotIn("no-all-3", kept)
        self.assertIn("yes-almost", kept)
        self.assertIn("yes-once", kept)
        self.assertIn("no-mixed-d", kept)

        yes_key = f"{question}::Yes"
        no_key = f"{question}::No"
        yes_value = after["(T) Classification Questions"]["categories"][0]["elements"][0]["values"][yes_key]["value"]
        no_value = after["(T) Classification Questions"]["categories"][0]["elements"][0]["values"][no_key]["value"]
        self.assertNotEqual(yes_value, no_value)

    def test_excel_raises_a_clear_error_when_nobody_remains(self):
        df = _frame(
            [
                {"Panelist": "a", "Rating": 1, "Task": 1, "Gender": "Female", "Age": 30, "ResponseTime": 1},
                {"Panelist": "a", "Rating": 1, "Task": 2, "Gender": "Female", "Age": 30, "ResponseTime": 1},
            ]
        )
        options = default_analysis_settings()
        options["exclude_constant_raters"] = True
        with self.assertRaises(ValueError) as caught:
            self.service.generate_report(df, {"title": "T"}, analysis_options=options)
        self.assertIn("every task the same", str(caught.exception))


class AnalysisSettingsNormalizeTests(unittest.TestCase):
    def test_missing_key_stays_off(self):
        settings = normalize_analysis_settings({"top": {"hundred": [4, 5], "zero": [1, 2, 3]}})
        self.assertIs(settings["exclude_constant_raters"], False)
        self.assertIs(default_analysis_settings()["exclude_constant_raters"], False)
        self.assertIs(default_analysis_settings(7)["exclude_constant_raters"], False)

    def test_explicit_true_survives(self):
        settings = normalize_analysis_settings({"exclude_constant_raters": True})
        self.assertIs(settings["exclude_constant_raters"], True)

    def test_null_stays_off(self):
        settings = normalize_analysis_settings({"exclude_constant_raters": None})
        self.assertIs(settings["exclude_constant_raters"], False)


if __name__ == "__main__":
    unittest.main()
