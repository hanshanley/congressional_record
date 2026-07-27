"""Reproducibility guarantees for the lexicon scorers.

The compiled phrase alternations are built from Python ``set`` objects, whose
iteration order changes with per-process hash randomisation. Python alternation
is leftmost-first rather than longest-match, so an unordered alternation makes
the published counts depend on which process scored them -- two identical runs
disagreed by several hits per Congress before this was pinned down.

These tests run the scorer in *separate interpreters with different hash seeds*,
which is the only way to observe the failure; an in-process check always passes.
"""

from __future__ import annotations

import subprocess
import sys
import textwrap
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from analysis.score.scorers import _phrase_regex, _word_regex  # noqa: E402


def _run_with_seed(seed: str, body: str) -> str:
    script = textwrap.dedent(
        f"""
        import sys
        sys.path.insert(0, {str(ROOT)!r})
        {textwrap.indent(textwrap.dedent(body), '        ').strip()}
        """
    )
    result = subprocess.run(
        [sys.executable, "-c", script],
        env={"PYTHONHASHSEED": seed, "PATH": "/usr/bin:/bin"},
        capture_output=True,
        text=True,
        check=True,
    )
    return result.stdout.strip()


def test_compiled_lexicons_are_identical_across_hash_seeds():
    body = """
        import hashlib
        from analysis.score.scorers import Scorers
        s = Scorers()
        h = hashlib.sha256()
        for name in ("formal_courtesy", "gratitude_praise", "cooperation",
                     "hostility", "misconduct", "ideological_labels", "outgroup_idiom"):
            lex = getattr(s, name)
            h.update((lex.phrase_re.pattern if lex.phrase_re else "").encode())
            h.update(repr(sorted(lex.singles)).encode())
        print(h.hexdigest())
    """
    digests = {_run_with_seed(seed, body) for seed in ("0", "1", "12345")}
    assert len(digests) == 1, f"lexicon compilation is not reproducible: {digests}"


def test_scores_are_identical_across_hash_seeds():
    body = """
        from analysis.score.scorers import Scorers
        s = Scorers()
        text = (
            "I thank my friend and distinguished colleague. We must reach across "
            "the aisle and work across party lines with our Republican colleagues, "
            "who have shown willingness to work together in good faith."
        )
        got = s.score_turn(text, "D")
        print(sorted((k, round(float(v), 6)) for k, v in got.items()))
    """
    results = {_run_with_seed(seed, body) for seed in ("0", "1", "12345")}
    assert len(results) == 1, f"scoring is not reproducible across processes: {results}"


def test_phrase_alternation_is_ordered_longest_first():
    # Both phrases can match at the same position; leftmost-first alternation means
    # the longer, more specific phrase must be offered first or it can never win.
    pattern = _phrase_regex([("work", "together"), ("work", "together", "in", "good", "faith")])
    match = pattern.search("we will work together in good faith today")
    assert match is not None
    assert match.group() == "work together in good faith"


def test_phrase_alternation_order_is_stable_regardless_of_input_order():
    forward = _phrase_regex([("reach", "across", "the", "aisle"), ("common", "ground")])
    reverse = _phrase_regex([("common", "ground"), ("reach", "across", "the", "aisle")])
    assert forward.pattern == reverse.pattern


def test_duplicate_phrases_do_not_change_the_pattern():
    once = _phrase_regex([("common", "ground")])
    twice = _phrase_regex([("common", "ground"), ("common", "ground")])
    assert once.pattern == twice.pattern


def test_word_regex_orders_variants_longest_first_deterministically():
    pattern = _word_regex("colleague")
    forms = pattern[3:-1].split("|")
    assert forms == sorted(forms, key=lambda f: (-len(f), f))
    # Stable across repeated construction within a process too.
    assert _word_regex("colleague") == pattern
