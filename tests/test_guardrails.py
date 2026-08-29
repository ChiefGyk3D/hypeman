# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

"""
Tests for the shared guardrails.

The point of the content profile is that the same checks apply everywhere but
the vocabulary changes: a stream announcement promising "VOD coming soon" is a
hallucination, and so is a video announcement claiming you're "live now". These
tests pin that behaviour so the two daemons can't drift apart again.
"""

import pytest

from hypeman_social.llm import guardrails
from hypeman_social.llm.profiles import STREAM_PROFILE, VIDEO_PROFILE
from hypeman_social.social.base import is_url_for_domain

# ─────────────────────────────────────────────────────────────────────────────
# Profile-specific hallucination detection
# ─────────────────────────────────────────────────────────────────────────────

def test_stream_profile_catches_stream_specific_fabrications():
    valid, issues = guardrails.validate_message_quality(
        "Playing some Doom, VOD coming soon", 0, "Doom night", "chiefgyk3d", STREAM_PROFILE
    )
    assert valid is False
    assert any('hallucination' in i for i in issues)


def test_video_profile_catches_video_specific_fabrications():
    valid, issues = guardrails.validate_message_quality(
        "New breakdown of the exploit, live now", 0, "Exploit breakdown", "chiefgyk3d", VIDEO_PROFILE
    )
    assert valid is False
    assert any('hallucination' in i for i in issues)


def test_profiles_do_not_cross_contaminate():
    """'new video' is fabricated for a stream, normal for an upload."""
    stream_valid, _ = guardrails.validate_message_quality(
        "new video is up", 0, "Some title", "user", STREAM_PROFILE
    )
    video_valid, _ = guardrails.validate_message_quality(
        "new video is up", 0, "Some title", "user", VIDEO_PROFILE
    )
    assert stream_valid is False
    assert video_valid is True


@pytest.mark.parametrize("fabrication", [
    "Giveaway at the end!",
    "Starting at 8pm",
    "drops enabled tonight",
])
def test_universal_fabrications_are_caught(fabrication):
    valid, _ = guardrails.validate_message_quality(
        fabrication, 0, "A title", "user", STREAM_PROFILE
    )
    assert valid is False


def test_clean_message_passes():
    valid, issues = guardrails.validate_message_quality(
        "Digging into the new firmware today", 0, "Firmware teardown", "chiefgyk3d", STREAM_PROFILE
    )
    assert valid is True, issues


def test_hashtag_count_is_enforced():
    valid, issues = guardrails.validate_message_quality(
        "Some post #one #two #three", 1, "A title", "user", VIDEO_PROFILE
    )
    assert valid is False
    assert any('hashtag count' in i for i in issues)


def test_inline_urls_are_rejected():
    """Bluesky needs URLs attached as facets, not pasted into the text."""
    valid, issues = guardrails.validate_message_quality(
        "Watch at https://example.com", 0, "A title", "user", VIDEO_PROFILE
    )
    assert valid is False
    assert any('URL' in i for i in issues)


# ─────────────────────────────────────────────────────────────────────────────
# Quality scoring
# ─────────────────────────────────────────────────────────────────────────────

def test_verbatim_title_repost_scores_badly():
    title = "Building a Raspberry Pi cluster"
    score, issues = guardrails.score_message_quality(title, title, VIDEO_PROFILE)
    assert score < 7
    assert any('verbatim' in i for i in issues)


def test_none_title_does_not_crash():
    """End-of-stream events don't always carry a title. This used to be a landmine."""
    score, _ = guardrails.score_message_quality("That's a wrap, thanks all!", None, STREAM_PROFILE)
    assert 0 <= score <= 10


def test_empty_title_does_not_crash():
    score, _ = guardrails.score_message_quality("Stream over!", "", STREAM_PROFILE)
    assert 0 <= score <= 10


def test_generic_filler_is_penalised_past_a_threshold():
    """One stock phrase is human. Three is a model padding for length."""
    heavy = "come hang out, let's go, join me, going live, stream time"
    score, issues = guardrails.score_message_quality(heavy, "Doom night", STREAM_PROFILE)
    assert any('generic phrases' in i for i in issues)
    assert score < 10


def test_a_single_generic_phrase_is_tolerated():
    _, issues = guardrails.score_message_quality(
        "come hang out while I fight the firmware", "Firmware night", STREAM_PROFILE
    )
    assert not any('generic phrases' in i for i in issues)


def test_short_messages_are_penalised_as_lazy():
    score, issues = guardrails.score_message_quality("live now", "Doom night", STREAM_PROFILE)
    assert any('Too short' in i for i in issues)
    assert score < 10


def test_rambling_messages_are_penalised():
    rambling = " ".join(f"word{i}" for i in range(30))
    _, issues = guardrails.score_message_quality(rambling, "A title", STREAM_PROFILE)
    assert any('Too long' in i for i in issues)


def test_score_never_drops_below_one():
    """A floor of 1 keeps the scale meaningful; 0 would imply 'no signal'."""
    awful = "come hang out let's go join me going live stream time"
    score, _ = guardrails.score_message_quality(awful, awful, STREAM_PROFILE)
    assert score >= 1


# ─────────────────────────────────────────────────────────────────────────────
# Platform rules
# ─────────────────────────────────────────────────────────────────────────────

def test_discord_everyone_mention_is_flagged():
    issues = guardrails.validate_platform_specific("@everyone new video!", "discord")
    assert any('everyone' in i for i in issues)


def test_discord_unmatched_markdown_is_flagged():
    issues = guardrails.validate_platform_specific("**bold but unclosed", "discord")
    assert any('markdown' in i for i in issues)


def test_bluesky_inline_url_is_flagged():
    issues = guardrails.validate_platform_specific("see https://example.com", "bluesky")
    assert any('facets' in i for i in issues)


def test_clean_message_passes_platform_rules():
    assert guardrails.validate_platform_specific("A perfectly normal post", "mastodon") == []


# ─────────────────────────────────────────────────────────────────────────────
# Text helpers
# ─────────────────────────────────────────────────────────────────────────────

def test_username_derived_hashtags_are_stripped():
    """Nobody needs #ChiefGyk3D on a post from ChiefGyk3D."""
    result = guardrails.validate_hashtags_against_username(
        "Great stream today #ChiefGyk3D #Doom", "chiefgyk3d"
    )
    assert '#ChiefGyk3D' not in result
    assert '#Doom' in result


def test_safe_trim_respects_limit():
    long_message = "word " * 100
    assert len(guardrails.safe_trim(long_message, 50)) <= 50


def test_safe_trim_leaves_short_messages_alone():
    assert guardrails.safe_trim("short", 300) == "short"


def test_extract_from_thinking_finds_a_quoted_answer():
    """A '>' quoted line is the model showing its final answer."""
    thinking = (
        "Let me think about this.\n"
        "> Tearing into the new firmware today, and it is not going quietly. #Firmware\n"
        "That captures the tone I want."
    )
    result = guardrails.extract_from_thinking(thinking)
    assert result is not None
    assert "Tearing into the new firmware" in result
    assert ">" not in result


def test_extract_from_thinking_finds_a_marked_answer():
    """An explicit 'Final post:' marker is the clearest signal there is."""
    thinking = (
        "Steps:\n1. Keep it short\n2. Add a hashtag\n"
        "Final post: Firmware teardown continues tonight, come watch. #Hardware"
    )
    result = guardrails.extract_from_thinking(thinking)
    assert result is not None
    assert "Firmware teardown continues tonight" in result


def test_extract_from_thinking_finds_a_hashtag_line():
    """Failing the clearer signals, a post-length line with hashtags will do."""
    thinking = (
        "I need something catchy...\nThe user wants:\n- short\n"
        "- Firmware teardown night, come join me on stream. #Hardware #Linux\n"
        "That should work."
    )
    result = guardrails.extract_from_thinking(thinking)
    assert result is not None
    assert "#Hardware" in result
    # Leading list markers are stripped.
    assert not result.startswith('-')


def test_extract_from_thinking_respects_max_chars():
    """A wall of text is not a social post, however many hashtags it has."""
    long_line = "word " * 200 + "#Tag"
    assert guardrails.extract_from_thinking(long_line, max_chars=300) is None


def test_extract_from_thinking_gives_up_gracefully():
    assert guardrails.extract_from_thinking("", 300) is None
    assert guardrails.extract_from_thinking("short", 300) is None


# ─────────────────────────────────────────────────────────────────────────────
# URL domain matching
# ─────────────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("url,domain,expected", [
    ("https://youtube.com/watch?v=abc", "youtube.com", True),
    ("https://www.youtube.com/watch?v=abc", "youtube.com", True),
    ("https://twitch.tv/chiefgyk3d", "twitch.tv", True),
    # The substring bug this replaced: a lookalike host must not match.
    ("https://evil-youtube.com.attacker.net/x", "youtube.com", False),
    ("https://notyoutube.com/x", "youtube.com", False),
    ("", "youtube.com", False),
    ("not a url", "youtube.com", False),
])
def test_url_domain_matching(url, domain, expected):
    assert is_url_for_domain(url, domain) is expected


# ─────────────────────────────────────────────────────────────────────────────
# Star profile (for Star-Daemon, whenever it gains an LLM)
# ─────────────────────────────────────────────────────────────────────────────

def test_star_profile_catches_invented_repo_stats():
    """A model that cannot see the repo will cheerfully invent its numbers."""
    from hypeman_social.llm.profiles import STAR_PROFILE

    for fabrication in ["This one has 12k stars already",
                        "Currently trending on GitHub",
                        "Just released v2.1.0"]:
        valid, issues = guardrails.validate_message_quality(
            fabrication, 0, "some-repo", "chiefgyk3d", STAR_PROFILE)
        assert valid is False, fabrication


def test_star_profile_allows_an_honest_description():
    from hypeman_social.llm.profiles import STAR_PROFILE

    valid, issues = guardrails.validate_message_quality(
        "Starred a neat little tool for wrangling Kubernetes secrets",
        0, "kubesec", "chiefgyk3d", STAR_PROFILE)
    assert valid is True, issues
