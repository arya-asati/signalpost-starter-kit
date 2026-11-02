from scripts.run_nav_jobs_connector import _candidate_names, employer_name_similarity


def test_candidate_names_excludes_vacancy_title():
    item = {
        "_feed_entry": {"businessName": "Nordlys Elektro AS", "title": "Senior Software Engineer"},
        "employer_name": "Nordlys Elektro AS",
        "title": "Senior Software Engineer",
    }
    assert _candidate_names(item) == ["Nordlys Elektro AS"]


def test_similarity_is_symmetric_for_short_and_long_names():
    result = employer_name_similarity("North Star", "North Star Logistics AS")
    assert result["token_dice"] < 1.0
    assert result["score"] < 1.0


def test_exact_legal_name_scores_one():
    result = employer_name_similarity("Nordlys Elektro AS", "Nordlys Elektro AS")
    assert result["score"] == 1.0
    assert result["token_dice"] == 1.0


def test_unrelated_employer_names_score_low():
    result = employer_name_similarity("Nordlys Elektro AS", "Fjordland Bakeri AS")
    assert result["score"] < 0.5
