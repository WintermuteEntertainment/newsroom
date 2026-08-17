"""The refresh banners must not quote a run time.

Why: local run time moves with the model, the llama-swap --parallel setting and
top_n. A figure baked into a banner goes stale silently, and the failure mode is
bad: a user who is told "30+ minutes" and sees 90 concludes the run has hung and
kills it. So the banners say WHICH choice is slower and never how long.

These tests read the shipped files rather than importing anything, because the
strings live in front-end source that has no Python entry point.
"""

import re
import pathlib

ROOT = pathlib.Path(__file__).parent

# Every file that renders a run-in-progress message to a human.
FRONT_ENDS = [
    "app.js",
    "extension/digest.js",
    "extension/dist/chrome/digest.js",
    "extension/dist/firefox/digest.js",
]

# "30 minutes", "30+ min", "about an hour", "2 hrs", "~55 min" -- any duration
# with a number attached. Bare "a while" / "slower" are what we want instead.
DURATION = re.compile(r"[~\d][\d\s+.-]*\s*(?:min|minute|hour|hr|sec|second)s?\b", re.I)

# Spelled-out durations are just as stale-prone as digits, and the quantifier can
# be vague ("a few minutes") while still being a claim about how long it takes.
# "a while" is deliberately NOT matched: it carries no figure to go stale.
SPELLED = re.compile(
    r"\b(?:half|one|two|three|four|five|ten|several|a\s+few|a\s+couple(?:\s+of)?|an?)"
    r"\s+(?:an?\s+)?(?:hour|minute|min|sec|second)s?\b",
    re.I,
)


def _code_lines(rel):
    """Yield (lineno, text) for lines that are not pure comments.

    Comments are allowed to discuss timings -- the rule is about what a user
    SEES. A comment explaining why there is no number is desirable.
    """
    for i, line in enumerate((ROOT / rel).read_text(encoding="utf-8").splitlines(), 1):
        stripped = line.strip()
        if stripped.startswith("//") or stripped.startswith("*") or stripped.startswith("/*"):
            continue
        yield i, line


def test_no_front_end_quotes_a_duration():
    offenders = []
    for rel in FRONT_ENDS:
        for lineno, line in _code_lines(rel):
            if DURATION.search(line) or SPELLED.search(line):
                offenders.append(f"{rel}:{lineno}: {line.strip()[:90]}")
    assert not offenders, (
        "a run banner quotes a duration; say which option is slower instead:\n"
        + "\n".join(offenders)
    )


def test_local_option_is_still_marked_as_the_slower_one():
    """Dropping the number must not drop the warning.

    A user choosing between a metered run and a local one needs to know the
    local one costs nothing but takes much longer. If we strip the figure and
    say nothing, the local button looks like the obvious free win.
    """
    app = (ROOT / "app.js").read_text(encoding="utf-8")
    lines = [l for l in app.splitlines() if not l.strip().startswith("//")]

    # Two kinds of message mention the local GPU, and they have different jobs.
    # The run banners ("Running ... on the local GPU") appear once a run has
    # started and must say it is both free and slow. The profile-dropdown
    # tooltip describes cost; it should mention the tradeoff but is not a banner.
    banners = [l for l in lines if "local GPU" in l and "Running" in l]
    assert banners, "no local-run banner found in app.js"
    for msg in banners:
        assert re.search(r"slower|a while|longer", msg, re.I), (
            "local-run banner no longer says it is the slower choice: " + msg.strip()
        )
        assert "no API spend" in msg, (
            "local-run banner no longer says it is free: " + msg.strip()
        )

    tooltips = [l for l in lines if "local GPU" in l and "Running" not in l]
    for msg in tooltips:
        assert re.search(r"slower|a while|longer", msg, re.I), (
            "profile tooltip no longer hints that local is slower: " + msg.strip()
        )


def test_metered_option_is_marked_as_the_quicker_one():
    """The metered banner must say it is the faster choice.

    Pinned to the banner assignments themselves, not to the word appearing
    anywhere in the file -- otherwise an unrelated comment satisfies the test.
    """
    app = (ROOT / "app.js").read_text(encoding="utf-8")
    lines = app.splitlines()

    # The two `const eta = ...` conditionals: the line after each is the metered
    # branch (`? ...`), the line after that is the local branch (`: ...`).
    metered_branches = []
    for i, line in enumerate(lines):
        if "const eta" in line and not line.strip().startswith("//"):
            for follow in lines[i + 1:i + 3]:
                if follow.strip().startswith("?"):
                    metered_branches.append(follow)

    assert len(metered_branches) == 2, (
        f"expected 2 metered banner branches, found {len(metered_branches)}"
    )
    for msg in metered_branches:
        assert re.search(r"quicker|faster", msg, re.I), (
            "metered banner no longer identifies itself as the faster choice: "
            + msg.strip()
        )


def test_extension_note_still_tells_the_user_they_can_leave():
    """The point of the extension note is that the popup can be closed.

    That is the reassurance that replaces the number: not "it takes an hour"
    but "this is long, you do not have to watch it".
    """
    for rel in ["extension/digest.js",
                "extension/dist/chrome/digest.js",
                "extension/dist/firefox/digest.js"]:
        text = (ROOT / rel).read_text(encoding="utf-8")
        assert "you can close this" in text, f"{rel} lost the close-the-popup reassurance"
        assert re.search(r"take a while|takes a while", text, re.I), (
            f"{rel} no longer signals that a local run is long"
        )


def test_both_front_ends_agree_the_run_is_long():
    """app.js and extension/digest.js are separate copies of this logic and
    have drifted twice before. Neither may quietly keep a number."""
    app = (ROOT / "app.js").read_text(encoding="utf-8")
    ext = (ROOT / "extension/digest.js").read_text(encoding="utf-8")
    for name, text in (("app.js", app), ("extension/digest.js", ext)):
        user_lines = [l for l in text.splitlines() if not l.strip().startswith("//")]
        joined = "\n".join(user_lines)
        assert not DURATION.search(joined), f"{name} still quotes a duration"
