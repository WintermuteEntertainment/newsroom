"""Verdict readers must survive the reply shapes local models actually produce.

Both guards here were written from a survey of every pipeline_run_*.log on the deployment host
(2026-08-10). fragment_merge was the worst stage on the output contract at 263/372 = 70.7%
parsed, against 99.5-99.9% for every stage except entailment_check. That gap was not the
model being wrong: 93 failures were logged with the reply text, and two distinct shapes
account for them.

WHY the stage matters despite its low volume: a lost fragment_merge verdict defaults to
no-merge, so one event stays split across two rows and its prevalence -- the thing the
whole digest ranks by -- is understated. It fails silently and in the direction that looks
like normal output.
"""
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from news_digest import entail_verdict, read_same_different, strip_reasoning

# Verbatim from the run logs. Each is a reply the model produced and the parser discarded.
GLUED_REAL = {
    "ghiSAME": "SAME",
    "ghiDIFFERENT": "DIFFERENT",
    "ffinDIFFERENT": "DIFFERENT",
    "ffinDifferent": "DIFFERENT",
    "UniMOREDifferent": "DIFFERENT",
    "SbSAME": "SAME",
    "clusteringSAME": "SAME",
    "\u5916\u5411DIFFERENT": "DIFFERENT",
    "uentuacuteDIFFERENT": "DIFFERENT",
    "specializationDIFFERENT": "DIFFERENT",
    "sbbsbDIFFERENT": "DIFFERENT",
    "ghiemplu-1234DIFFERENT": "DIFFERENT",
}

# Also verbatim from the logs, but these carry NO decision. Recovering a verdict from any
# of them would be inventing one.
NO_VERDICT_REAL = [
    "",
    "Uni",
    "UniMER",
    "ghi",
    "Sherlock",
    "Shopkeeper",
    "Salesforce",
    "Bearish",
    "Painting",
    "Appropriate",
    "dekameters",
    "aplaude",
    "\u5916\u5411",
    "ghiExploitation\u2026",
    "Shopping for a new car? We can help!",
    "Sheriff, I'm going to need you to step out of the car.",
]


def test_glued_prefix_verdicts_are_recovered():
    for reply, expected in GLUED_REAL.items():
        assert read_same_different(reply) == expected, reply


def test_replies_with_no_decision_stay_none():
    for reply in NO_VERDICT_REAL:
        assert read_same_different(reply) is None, reply


def test_differential_is_not_read_as_a_truncated_verdict():
    """'pskohrvatskiDifferential' appears in the logs.

    DIFFERENT is a prefix of DIFFERENTIAL, so a naive substring search calls this a
    verdict. It is a word that happens to start the same way, not a decision, and the
    recovery requires the verdict to end cleanly for exactly this case.
    """
    assert read_same_different("pskohrvatskiDifferential") is None
    assert read_same_different("DIFFERENTIAL") is None
    assert read_same_different("SAMENESS") is None


def test_echoed_prompt_is_not_a_verdict():
    """The merge prompt ends 'Same event?', and 'UniSame event?' is in the logs.

    Reading a parroted question as SAME would merge two unrelated stories on the strength
    of an echo -- the failure this stage makes worst, since a wrong merge is invisible in
    the output.
    """
    assert read_same_different("UniSame event?") is None
    assert read_same_different("xxSame event?") is None


def test_conflicting_glued_words_stay_none():
    """Two different verdicts glued in one reply is not a decision to pick from."""
    assert read_same_different("xxSAME yyDIFFERENT") is None


def test_clean_replies_are_unaffected():
    """The recovery must not change what already worked."""
    assert read_same_different("SAME") == "SAME"
    assert read_same_different("  DIFFERENT.") == "DIFFERENT"
    assert read_same_different("These are NOT THE SAME event") == "DIFFERENT"
    assert read_same_different("<think>weighing it</think>\nSAME") == "SAME"
    # reasons aloud, then concludes -- scored on the conclusion
    assert read_same_different("They look SAME at first but are DIFFERENT") == "DIFFERENT"


# --- unclosed reasoning tags -----------------------------------------------------------
# 34 of the 93 fragment_merge failures and 47 of the 48 entailment_check failures were
# logged as an empty reply. Some are genuinely empty. But strip_reasoning used to delete
# everything after an unclosed <think>, so a model that emitted its verdict without ever
# closing the tag was recorded as having said nothing at all.


def test_verdict_after_an_unclosed_tag_survives():
    assert read_same_different("<think>A is tariffs, B is tariffs\nSAME") == "SAME"
    assert read_same_different("<think>reasoning\n\n\nDIFFERENT\n\n") == "DIFFERENT"
    assert entail_verdict(strip_reasoning("<think>sources agree\nSUPPORTED")) == "supported"
    assert entail_verdict(strip_reasoning("<think>they do not\nUNSUPPORTED")) == "unsupported"


def test_a_truncated_reply_still_yields_no_verdict():
    """The reason the old code blanked the reply was sound -- keep that outcome.

    A reply cut off mid-thought must not be half-read as an answer. It ends on reasoning
    prose, which carries no verdict word, so it is refused on content rather than by
    deleting it wholesale.
    """
    assert read_same_different("<think>Cluster A is about the ruling, cluster B about") is None
    assert read_same_different("<think>These both mention Ottawa but") is None
    assert entail_verdict(strip_reasoning("<think>Checking whether the sources say")) is None


def test_closed_tags_and_fences_still_strip():
    assert strip_reasoning("<think>hidden</think>SAME") == "SAME"
    assert strip_reasoning('```json\n{"a": 1}\n```').strip() == '{"a": 1}'
    assert "hidden" not in strip_reasoning("<think>hidden</think>\nDIFFERENT")


def test_text_before_an_unclosed_tag_is_kept():
    """A verdict emitted BEFORE the model started thinking must not be lost either."""
    assert read_same_different("SAME\n<think>because both cover") == "SAME"


def test_recovery_is_bounded_to_the_last_line():
    """Only the final non-blank line survives an unclosed tag.

    Keeping the whole reasoning body would let a verdict word MENTIONED mid-thought
    ('these could be the SAME, but...') be read as the conclusion.
    """
    reply = "<think>these could be the SAME, but the dates differ\nDIFFERENT"
    assert read_same_different(reply) == "DIFFERENT"
    assert "dates differ" not in strip_reasoning(reply)
