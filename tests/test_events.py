# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

"""The shared chat event shape and receiver contract."""

import dataclasses

import pytest

from hypeman_social.social.events import ROLE_MODERATOR, ChatEvent, ChatReceiver


def _event(**over):
    base = dict(
        platform='twitch', channel='somechannel', message_id='m1',
        author_id='42', author_name='Someone', text='hello',
    )
    base.update(over)
    return ChatEvent(**base)


def test_event_defaults_are_conservative():
    event = _event()
    assert event.is_self is False
    assert event.bridged is False
    assert event.roles == frozenset()
    assert event.reply_to_id is None


def test_event_is_immutable():
    event = _event()
    with pytest.raises(dataclasses.FrozenInstanceError):
        setattr(event, 'text', 'changed')


def test_roles_are_a_set_of_known_names():
    assert ROLE_MODERATOR in _event(roles=frozenset({ROLE_MODERATOR})).roles


def test_receiver_is_abstract():
    with pytest.raises(TypeError):
        ChatReceiver()
