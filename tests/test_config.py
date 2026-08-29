# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

"""Config lookup: key precedence, type coercion, and username parsing."""

import pytest

from hypeman_social.config import (
    get_bool_config,
    get_config,
    get_float_config,
    get_int_config,
    get_usernames,
)


@pytest.fixture(autouse=True)
def no_doppler(monkeypatch):
    """These tests exercise env lookups; Doppler must stay out of the way."""
    monkeypatch.delenv('DOPPLER_TOKEN', raising=False)


class TestGetConfig:
    def test_simple_key(self, monkeypatch):
        monkeypatch.setenv('CHECK_INTERVAL', '30')
        assert get_config('Settings', 'check_interval') == '30'

    def test_sectioned_key(self, monkeypatch):
        monkeypatch.delenv('CHECK_INTERVAL', raising=False)
        monkeypatch.setenv('SETTINGS_CHECK_INTERVAL', '60')
        assert get_config('Settings', 'check_interval') == '60'

    def test_simple_key_wins_over_sectioned(self, monkeypatch):
        monkeypatch.setenv('CHECK_INTERVAL', 'simple')
        monkeypatch.setenv('SETTINGS_CHECK_INTERVAL', 'sectioned')
        assert get_config('Settings', 'check_interval') == 'simple'

    def test_default_when_unset(self, monkeypatch):
        monkeypatch.delenv('NOPE_MISSING', raising=False)
        assert get_config('Nope', 'missing', default='fallback') == 'fallback'

    def test_empty_string_falls_through_to_default(self, monkeypatch):
        monkeypatch.setenv('NOPE_MISSING', '')
        assert get_config('Nope', 'missing', default='fallback') == 'fallback'


class TestTypedConfig:
    @pytest.mark.parametrize('raw,expected', [
        ('true', True), ('1', True), ('yes', True), ('on', True),
        ('ENABLED', True), ('false', False), ('0', False), ('junk', False),
    ])
    def test_bool_parsing(self, monkeypatch, raw, expected):
        monkeypatch.setenv('T_FLAG', raw)
        assert get_bool_config('T', 'flag') is expected

    def test_bool_default(self, monkeypatch):
        monkeypatch.delenv('T_FLAG', raising=False)
        assert get_bool_config('T', 'flag', default=True) is True

    def test_int_parsing_and_fallback(self, monkeypatch):
        monkeypatch.setenv('T_NUM', '42')
        assert get_int_config('T', 'num') == 42
        monkeypatch.setenv('T_NUM', 'not-a-number')
        assert get_int_config('T', 'num', default=7) == 7

    def test_float_parsing_and_fallback(self, monkeypatch):
        monkeypatch.setenv('T_RATIO', '0.5')
        assert get_float_config('T', 'ratio') == 0.5
        monkeypatch.setenv('T_RATIO', 'nan-sense')
        assert get_float_config('T', 'ratio', default=1.5) == 1.5


class TestGetUsernames:
    def test_singular(self, monkeypatch):
        monkeypatch.delenv('TWITCH_USERNAMES', raising=False)
        monkeypatch.setenv('TWITCH_USERNAME', 'chief')
        assert get_usernames('Twitch') == ['chief']

    def test_plural_comma_separated(self, monkeypatch):
        monkeypatch.setenv('TWITCH_USERNAMES', 'one, two ,three')
        assert get_usernames('Twitch') == ['one', 'two', 'three']

    def test_plural_wins_over_singular(self, monkeypatch):
        monkeypatch.setenv('TWITCH_USERNAMES', 'a,b')
        monkeypatch.setenv('TWITCH_USERNAME', 'c')
        assert get_usernames('Twitch') == ['a', 'b']

    def test_default_string_and_list(self, monkeypatch):
        monkeypatch.delenv('KICK_USERNAMES', raising=False)
        monkeypatch.delenv('KICK_USERNAME', raising=False)
        assert get_usernames('Kick') == []
        assert get_usernames('Kick', default='solo') == ['solo']
        assert get_usernames('Kick', default=['x', 'y']) == ['x', 'y']
