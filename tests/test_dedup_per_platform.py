# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

"""
Deduplication is per platform: the same announcement going to Discord,
Matrix, Bluesky and Mastodon is one announcement, not three repeats, but the
same text twice to ONE platform is still a repeat.
"""

from tests.test_generate_validated import make_manager

TEXT = 'Brand new video is up on the channel, go and watch it now'


def _manager():
    m = make_manager([])
    m.enable_deduplication = True
    m.primary.enable_deduplication = True
    return m


def _post(m, platform, text=TEXT):
    return m.generate_validated(
        text, title='t', username='chief', platform=platform, char_limit=300)


def test_same_text_accepted_on_two_platforms():
    m = _manager()
    m.primary.responses = [TEXT, TEXT, TEXT, TEXT]
    assert _post(m, 'discord') == TEXT
    assert _post(m, 'mastodon') == TEXT
    assert _post(m, 'bluesky') == TEXT


def test_near_identical_text_accepted_on_other_platform():
    m = _manager()
    variant = TEXT + ' friends'
    m.primary.responses = [TEXT, variant]
    assert _post(m, 'discord') == TEXT
    assert _post(m, 'matrix') == variant


def test_same_text_twice_on_one_platform_rejected():
    m = _manager()
    m.primary.responses = [TEXT, TEXT, TEXT]
    assert _post(m, 'mastodon') == TEXT
    assert _post(m, 'mastodon') is None


def test_generic_bucket_still_dedups():
    m = _manager()
    m.primary.responses = [TEXT, TEXT, TEXT]
    assert _post(m, 'generic') == TEXT
    assert _post(m, 'generic') is None


def test_generic_bucket_is_default_for_cache_calls():
    m = _manager()
    m.add_to_message_cache(TEXT)
    assert m.is_duplicate_message(TEXT)
    assert m.is_duplicate_message(TEXT, 'generic')
    assert not m.is_duplicate_message(TEXT, 'discord')


def test_platform_name_case_is_not_a_new_bucket():
    m = _manager()
    m.add_to_message_cache(TEXT, 'Mastodon')
    assert m.is_duplicate_message(TEXT, 'mastodon')


def test_per_platform_history_is_bounded():
    m = _manager()
    m.dedup_cache_size = 2
    for word in ('alpha one', 'bravo two', 'charlie three'):
        m.add_to_message_cache(word, 'discord')
    assert not m.is_duplicate_message('alpha one', 'discord')
    assert m.is_duplicate_message('charlie three', 'discord')


def test_dedup_disabled_still_allows_everything():
    m = _manager()
    m.enable_deduplication = False
    m.primary.enable_deduplication = False
    m.primary.responses = [TEXT, TEXT]
    assert _post(m, 'discord') == TEXT
    assert _post(m, 'discord') == TEXT


def test_provider_cache_is_per_platform_too():
    m = _manager()
    p = m.primary
    p.add_to_message_cache(TEXT, 'discord')
    assert p.is_duplicate_message(TEXT, 'discord')
    assert not p.is_duplicate_message(TEXT, 'bluesky')
    assert p.is_duplicate_message(TEXT) is False
