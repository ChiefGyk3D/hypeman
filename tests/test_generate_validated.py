# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

"""
LLMManager.generate_validated(): the generate → guardrails → strict-retry
flow the daemons used to each reimplement.
"""


from hypeman_social.llm.manager import LLMManager
from tests.test_availability import FakeLLM


class ScriptedLLM(FakeLLM):
    """A provider that returns queued responses in order."""

    def __init__(self, responses, **kwargs):
        super().__init__(server_up=True, **kwargs)
        self.responses = list(responses)
        self.prompts = []

    def _raw_generate(self, prompt, max_tokens):
        self.prompts.append(prompt)
        if not self.responses:
            return None
        return self.responses.pop(0)


def make_manager(responses):
    m = LLMManager()
    provider = ScriptedLLM(responses)
    provider.authenticate()
    # Deterministic guardrail config, independent of ambient env.
    provider.enable_profanity_filter = False
    provider.enable_quality_scoring = False
    provider.enable_platform_validation = False
    provider.max_emoji_count = 10
    m.primary = provider
    m.enabled = True
    m.enable_deduplication = False
    return m


def test_valid_first_attempt_ships(monkeypatch):
    m = make_manager(['Great new video from the stream! #gaming #fun'])
    result = m.generate_validated(
        'write a post', title='Speedrun', username='chief',
        char_limit=300, expected_hashtags=2)
    assert result == 'Great new video from the stream! #gaming #fun'
    assert len(m.primary.prompts) == 1


def test_retry_with_stricter_prompt_on_issues():
    # First message has the wrong hashtag count; the strict retry fixes it.
    m = make_manager([
        'No hashtags here at all',
        'Fixed message with tags #gaming #fun',
    ])
    prompts = []

    def build_prompt(strict):
        prompts.append(strict)
        return 'STRICT PROMPT' if strict else 'NORMAL PROMPT'

    result = m.generate_validated(
        build_prompt, title='Speedrun', username='chief',
        char_limit=300, expected_hashtags=2)

    assert result == 'Fixed message with tags #gaming #fun'
    assert prompts == [False, True]
    assert m.primary.prompts == ['NORMAL PROMPT', 'STRICT PROMPT']


def test_lenient_fallback_ships_original_despite_issues():
    # Both attempts have the wrong hashtag count — the original ships anyway,
    # because minor style problems beat silence.
    m = make_manager([
        '"Original message, zero hashtags"',
        'Retry also has zero hashtags',
    ])
    result = m.generate_validated(
        lambda strict: 'p', title='t', username='chief',
        char_limit=300, expected_hashtags=2)
    # Lenient acceptance also strips the wrapping quotes.
    assert result == 'Original message, zero hashtags'


def test_profanity_hard_veto_in_lenient_path():
    m = make_manager([
        'this stream is bad ass fun',   # fails hashtag count AND has profanity
        'retry still has no hashtags',
    ])
    m.primary.enable_profanity_filter = True
    m.primary.profanity_severity = 'moderate'
    result = m.generate_validated(
        lambda strict: 'p', title='t', username='chief',
        char_limit=300, expected_hashtags=2)
    assert result is None


def test_duplicate_hard_veto_in_lenient_path():
    m = make_manager(['same message again', 'retry without hashtags'])
    m.enable_deduplication = True
    m.add_to_message_cache('same message again')
    result = m.generate_validated(
        lambda strict: 'p', title='t', username='chief',
        char_limit=300, expected_hashtags=2)
    assert result is None


def test_generation_failure_returns_none():
    m = make_manager([])
    assert m.generate_validated('prompt', char_limit=300) is None


def test_disabled_manager_returns_none():
    m = make_manager(['whatever'])
    m.enabled = False
    assert m.generate_validated('prompt') is None


def test_plain_string_prompt_accepted():
    m = make_manager(['A perfectly fine message #one #two'])
    result = m.generate_validated(
        'plain prompt', title='t', username='chief',
        char_limit=300, expected_hashtags=2)
    assert result == 'A perfectly fine message #one #two'


def test_lenient_result_respects_char_limit():
    long_message = 'word ' * 100  # 500 chars, no hashtags -> fails validation twice
    m = make_manager([long_message, long_message])
    result = m.generate_validated(
        lambda strict: 'p', title='t', username='chief',
        char_limit=120, expected_hashtags=2)
    assert result is not None
    assert len(result) <= 120
