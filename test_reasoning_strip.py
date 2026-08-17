"""local_host.strip_reasoning must not delete an answer that arrived after an unclosed tag.

This function runs on the RAW reply, before anything in news_digest sees it. Whatever it
removes is gone -- the pipeline cannot recover a verdict it never receives, and records the
call as having returned nothing.

Measured 2026-08-10 over every pipeline_run_*.log on the server: of 152 logged contract
failures, 81 were an EMPTY reply -- 34 of fragment_merge's 93 and 47 of entailment_check's
48. An emptied reply and a truncated one are indistinguishable downstream, which is why the
old rule ("an unclosed opening tag means the model never finished, so keep nothing") was
invisible for the whole project: it could only ever be observed as a model that said
nothing.
"""
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import local_host
import news_digest
from local_host import strip_reasoning


def test_verdict_after_unclosed_tag_reaches_the_parser():
    assert strip_reasoning("<think>A is tariffs, B is tariffs\nSAME") == "SAME"
    assert strip_reasoning("<think>checking the sources\nUNSUPPORTED") == "UNSUPPORTED"
    assert strip_reasoning("<think>reasoning\n\n\nDIFFERENT\n\n") == "DIFFERENT"


def test_truncated_thought_still_yields_no_answer():
    """The old rule's INTENT was right and must survive: a cut-off thought is not a verdict.

    It now falls out of the content rather than the tag -- the last line is reasoning prose,
    so the readers refuse it -- instead of being enforced by deleting the reply.
    """
    cut = "<think>Cluster A is about the tariff ruling, cluster B about"
    assert news_digest.read_same_different(strip_reasoning(cut)) is None
    assert news_digest.entail_verdict(strip_reasoning(cut)) is None


def test_text_on_the_tags_own_line_is_not_read_as_an_answer():
    """'<think>maybe SAME but the dates differ' is a thought, not a decision."""
    assert strip_reasoning("<think>maybe SAME but") == ""
    assert news_digest.read_same_different(strip_reasoning("<think>maybe SAME but")) is None


def test_only_the_conclusion_survives_not_the_whole_thought():
    """Keeping the entire body would let a verdict word MENTIONED mid-thought win."""
    reply = "<think>these could be the SAME, but the dates differ\nDIFFERENT"
    assert strip_reasoning(reply) == "DIFFERENT"
    assert "dates differ" not in strip_reasoning(reply)


def test_the_conclusion_is_the_LAST_line_not_the_second():
    """Reasoning usually runs several lines before the answer.

    Added after a mutation that read tail[1] instead of tail[-1] passed the suite: every
    case here had exactly one line after the tag, so "second line" and "last line" were
    the same string and the tests could not tell a correct implementation from one that
    grabs the first line of reasoning. Real replies think for several lines first.
    """
    reply = (
        "<think>Cluster A is about the tariff ruling in Ottawa.\n"
        "Cluster B is about a tariff ruling too, but in Washington.\n"
        "Different jurisdictions, so different events.\n"
        "DIFFERENT"
    )
    assert strip_reasoning(reply) == "DIFFERENT"
    assert news_digest.read_same_different(strip_reasoning(reply)) == "DIFFERENT"
    # A long thought that never reaches a verdict must still yield nothing.
    no_answer = (
        "<think>Cluster A is about the tariff ruling.\n"
        "Cluster B mentions tariffs as well.\n"
        "I need to check whether the dates line up"
    )
    assert news_digest.read_same_different(strip_reasoning(no_answer)) is None


def test_existing_behaviour_is_unchanged():
    assert strip_reasoning("<think>hidden</think>\nSAME") == "SAME"
    assert strip_reasoning("reasoning</think>\nDIFFERENT") == "DIFFERENT"   # closing tag only
    assert strip_reasoning('```json\n{"a": 1}\n```') == '{"a": 1}'
    assert strip_reasoning("") == ""
    assert strip_reasoning("SUPPORTED") == "SUPPORTED"
    assert strip_reasoning("SAME\n<think>because both") == "SAME"


def test_answer_before_an_unclosed_tag_is_kept():
    assert strip_reasoning("DIFFERENT\n<think>the dates are weeks apart") == "DIFFERENT"


def test_the_two_copies_of_this_rule_agree():
    """news_digest has its OWN strip_reasoning, and the pair has to stay in step.

    They are not identical functions -- this one runs on the raw reply and handles a
    stray closing tag, that one runs as a second pass -- so this pins the shared
    behaviour that matters: the same replies must yield the same VERDICT through either
    path. app.js and extension/digest.js drifted twice for exactly this reason, so the
    duplication is pinned rather than trusted.
    """
    shared = [
        "<think>hmm</think>\nSAME",
        "<think>A tariffs, B tariffs\nSAME",
        "<think>reasoning\nDIFFERENT",
        "<think>cut off mid thou",
        "SAME",
        "  DIFFERENT.",
        "ghiSAME",
        "",
        "SAME\n<think>because",
    ]
    for reply in shared:
        via_client = news_digest.read_same_different(strip_reasoning(reply))
        via_pipeline = news_digest.read_same_different(news_digest.strip_reasoning(reply))
        assert via_client == via_pipeline, (reply, via_client, via_pipeline)


def test_both_copies_are_documented_as_a_pair():
    """A future editor must be told there is a second copy before they change one."""
    client = Path(local_host.__file__).read_text(encoding="utf-8")
    assert "news_digest.strip_reasoning is a SECOND copy" in client
