# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

"""
Quality guardrails for LLM-generated social posts.

Small local models (4B-12B) are enthusiastic liars. They will cheerfully invent
a giveaway, promise a VOD, hallucinate a start time, spam eleven hashtags, and
repost your title verbatim while calling it "engaging". These are the rules that
govern the rule-following machine. Meta as fuck.

Every function here is pure and side-effect free, so they're trivial to test and
safe to call from anywhere. Stateful checks (deduplication) live on the LLM
client itself, since they need a cache with a lifetime.
"""

import logging
import re
from typing import List, Optional, Set, Tuple

from hypeman.llm.profiles import ContentProfile, GENERIC_PROFILE

logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────────────────────
# Pure text helpers (shared verbatim by every daemon)
# ─────────────────────────────────────────────────────────────────────────────
def tokenize_username(username: str) -> Set[str]:
    """
    Tokenize username into parts that should not appear in hashtags.

    Handles various username formats:
    - CamelCase: CoolStreamer99 -> ['cool', 'streamer', '99', 'coolstreamer99']
    - Underscores: Cool_Streamer_99 -> ['cool', 'streamer', 'cool_streamer_99']
    - Numbers: Gamer123 -> ['gamer', '123', 'gamer123']
    - Prefixes: @username, #username -> removes prefix

    Because apparently teaching a computer "don't use the person's name as a hashtag"
    is like explaining to your uncle why his username isn't interesting content.

    Args:
        username: The content creator's username

    Returns:
        Set of lowercase username parts (min 3 chars to avoid false positives)
    """
    if not username:
        return set()

    # Remove common prefixes (@, #)
    clean_username = username.lstrip('@#').strip()

    # Add the full username (lowercased) to the set
    parts = {clean_username.lower()}

    # Split on underscores, hyphens, dots
    for separator in ['_', '-', '.']:
        if separator in clean_username:
            parts.update(p.lower() for p in clean_username.split(separator) if len(p) >= 3)

    # Split CamelCase: CoolStreamer99 -> Cool, Streamer, 99
    camel_parts = re.findall(r'[A-Z]*[a-z]+|[A-Z]+(?=[A-Z]|$)|[0-9]+', clean_username)
    parts.update(p.lower() for p in camel_parts if len(p) >= 3)

    # Also add consecutive parts for partial matches
    for i in range(len(camel_parts)):
        for j in range(i + 1, min(i + 4, len(camel_parts) + 1)):
            combined = ''.join(camel_parts[i:j]).lower()
            if len(combined) >= 3:
                parts.add(combined)

    return parts

def extract_hashtags(message: str) -> List[str]:
    """
    Extract all hashtags from a message.

    Because we need to teach a computer to recognize words that start with #.
    In the history of human civilization, this is where we ended up.

    Args:
        message: The generated message

    Returns:
        List of hashtags (without # prefix, lowercase)
    """
    # Match hashtags: # followed by a letter, then any alphanumeric characters
    hashtags = re.findall(r'#([a-zA-Z]\w*)', message)
    return [tag.lower() for tag in hashtags]

def remove_hashtag_from_message(message: str, hashtag: str) -> str:
    """
    Remove a specific hashtag from the message.

    Args:
        message: The message to modify
        hashtag: The hashtag to remove (without #)

    Returns:
        Message with the hashtag removed, cleaned up
    """
    # Remove the hashtag (case insensitive)
    pattern = r'#' + re.escape(hashtag) + r'\b'
    message = re.sub(pattern, '', message, flags=re.IGNORECASE)

    # Clean up extra spaces
    message = re.sub(r'\s+', ' ', message).strip()

    return message

def safe_trim(message: str, limit: int) -> str:
    """
    Safely trim message to character limit without cutting words mid-token.

    Args:
        message: The message to trim
        limit: Maximum character limit

    Returns:
        Trimmed message at word boundary
    """
    message = message.strip()
    if len(message) <= limit:
        return message

    # Try to trim at a word boundary
    trimmed = message[:limit].rsplit(' ', 1)[0].rstrip()

    # If we trimmed too aggressively, hard cut
    return trimmed if trimmed else message[:limit]

def count_emojis(message: str) -> int:
    """
    Count emoji characters in a message.

    Because apparently some people think more emojis = more engagement.
    Spoiler alert: It doesn't. You just look like you're 12.

    Args:
        message: The message to check

    Returns:
        Number of emoji characters
    """
    emoji_pattern = re.compile(
        "["
        "\U0001F600-\U0001F64F"  # emoticons
        "\U0001F300-\U0001F5FF"  # symbols & pictographs
        "\U0001F680-\U0001F6FF"  # transport & map symbols
        "\U0001F1E0-\U0001F1FF"  # flags (iOS)
        "\U00002702-\U000027B0"  # dingbats
        "\U000024C2-\U0001F251"  # enclosed characters
        "]", flags=re.UNICODE
    )
    return len(emoji_pattern.findall(message))

def contains_forbidden_words(message: str) -> Tuple[bool, List[str]]:
    """
    Check if message contains clickbait/forbidden words.

    We built a machine that can process language at superhuman levels, and we use it
    to generate social media posts. Then we have to check if the machine used the words
    "INSANE" or "EPIC" because apparently we can't trust it to follow basic instructions.

    It's like hiring a PhD to write greeting cards, then having to make sure they didn't
    write anything too smart. Welcome to the goddamn future.

    Args:
        message: The generated message

    Returns:
        tuple: (has_forbidden_words, list_of_found_words)
    """
    # Forbidden words we explicitly tell the AI not to use
    forbidden_words = [
        'insane', 'epic', 'crazy', 'smash', 'unmissable', 
        'incredible', 'amazing', 'lit', 'fire', 'legendary',
        'mind-blowing', 'jaw-dropping', 'unbelievable'
    ]

    message_lower = message.lower()
    found_words = []

    for word in forbidden_words:
        # Use word boundaries to avoid false positives
        if re.search(rf'\b{word}\b', message_lower):
            found_words.append(word)

    return (len(found_words) > 0, found_words)

def contains_profanity(message: str, severity: str = 'moderate') -> Tuple[bool, List[str]]:
    """
    Check if message contains profanity.

    Ironic, considering this entire codebase is written with Carlin-style commentary.
    But apparently we draw the line at the AI dropping F-bombs about your cat videos.

    Args:
        message: The message to check
        severity: 'mild', 'moderate', or 'severe'

    Returns:
        tuple: (has_profanity, list_of_found_words)
    """
    # Profanity lists by severity
    mild_words = ['damn', 'hell', 'crap', 'suck', 'sucks', 'piss', 'pissed']
    moderate_words = ['ass', 'bastard', 'bitch', 'dick', 'cock', 'pussy', 'slut', 'whore']
    severe_words = ['fuck', 'fucking', 'shit', 'shitty', 'motherfucker', 'asshole', 'cunt']

    if severity == 'severe':
        check_words = mild_words + moderate_words + severe_words
    elif severity == 'moderate':
        check_words = mild_words + moderate_words
    else:  # mild
        check_words = mild_words

    message_lower = message.lower()
    found_words = []

    for word in check_words:
        if re.search(rf'\b{word}\b', message_lower):
            found_words.append(word)

    return (len(found_words) > 0, found_words)

def validate_hashtags_against_username(message: str, username: str) -> str:
    """
    Remove hashtags that are derived from the username.

    This is a post-generation guardrail to filter out username-derived hashtags
    that the LLM may have incorrectly generated despite prompt instructions.

    Because apparently teaching a computer "don't use the person's name as a hashtag"
    is like explaining to your uncle why #MAGA isn't a personality trait.

    Args:
        message: The generated message
        username: The content creator's username

    Returns:
        Message with username-derived hashtags removed
    """
    # Get username parts
    username_parts = tokenize_username(username)

    if not username_parts:
        return message

    # Extract hashtags from message
    hashtags = extract_hashtags(message)

    # Check each hashtag against username parts
    for hashtag in hashtags:
        should_remove = False

        # Direct match (exact match with any username part)
        if hashtag in username_parts:
            should_remove = True
            logger.debug(f"Removing hashtag #{hashtag} - direct match with username part")
        else:
            # Check if hashtag contains username parts (substring matching)
            for part in username_parts:
                if len(part) >= 4 and part in hashtag:
                    should_remove = True
                    logger.debug(f"Removing hashtag #{hashtag} - contains username part '{part}'")
                    break

        if should_remove:
            message = remove_hashtag_from_message(message, hashtag)
            logger.warning(f"⚠ Removed username-derived hashtag: #{hashtag}")

    return message


# ─────────────────────────────────────────────────────────────────────────────
# Profile-driven validators
#
# These behave the same everywhere; only their vocabulary changes. Pass the
# ContentProfile for whatever you're announcing. See hypeman.llm.profiles.
# ─────────────────────────────────────────────────────────────────────────────


def score_message_quality(
    message: str,
    title: str,
    profile: ContentProfile = GENERIC_PROFILE,
) -> Tuple[int, List[str]]:
    """
    Score a generated message from 10 (great) down to 1 (unpostable).

    Penalises what small models do when they run out of ideas: generic filler,
    lazy length, repeated words, ignoring the source title, or parroting the
    title back with a hashtag stapled on.

    Args:
        message: The generated message.
        title: The source title. May be None or empty — an end-of-stream event
            does not always carry one.
        profile: Supplies the generic-phrase list and the content noun.

    Returns:
        (score, issues), score clamped to 1-10.
    """
    score = 10
    issues = []
    message_lower = message.lower()

    # Filler phrases. Only penalised past a threshold — one stock phrase is
    # human, three is a model padding for length.
    generic_count = sum(1 for phrase in profile.generic_phrases if phrase in message_lower)
    if generic_count > 2:
        score -= 2
        issues.append(f"Too many generic phrases ({generic_count})")

    # Length: too short reads as lazy, too long as rambling.
    content_without_hashtags = re.sub(r'#\w+', '', message).strip()
    word_count = len(content_without_hashtags.split())
    if word_count < 5:
        score -= 3
        issues.append("Too short (feels lazy)")
    elif word_count > 25:
        score -= 2
        issues.append("Too long (rambling)")

    # Repeated words are a sign of poor generation.
    words = content_without_hashtags.lower().split()
    unique_ratio = len(set(words)) / len(words) if words else 0
    if unique_ratio < 0.7:
        score -= 2
        issues.append("Too many repeated words")

    # A message that ignores the title entirely is generic by definition.
    title_words = set(title.lower().split()) if title else set()
    message_words = set(message_lower.split())
    if len(title_words & message_words) == 0:
        score -= 3
        issues.append(f"Doesn't reference {profile.content_noun} title/content")

    # Parroting the title adds nothing the reader can't already see.
    content_before_hashtags = re.split(r'\s+#', message)[0].strip()
    title_clean = title.strip() if title else ''
    content_no_punct = re.sub(r'[^\w\s]', '', content_before_hashtags.lower()).strip()
    title_no_punct = re.sub(r'[^\w\s]', '', title_clean.lower()).strip()

    if content_no_punct == title_no_punct:
        score -= 4
        issues.append(
            "Message just reposts the title verbatim - should encourage engagement instead"
        )
    elif (
        len(title_no_punct) > 0
        and len(content_no_punct) >= len(title_no_punct)
        and len(title_no_punct) / len(content_no_punct) > 0.7
    ):
        score -= 3
        issues.append("Message too similar to title - should add value and encourage viewers")

    # Some spark of personality. Not much to ask.
    has_personality = bool(
        re.search(r'[!?]', message)
        or re.search(r'[\U0001F600-\U0001F64F]', message)
    )
    if not has_personality:
        score -= 1
        issues.append("Lacks personality (no punctuation variety or emoji)")

    return max(1, min(10, score)), issues


def validate_message_quality(
    message: str,
    expected_hashtag_count: int,
    title: str,
    username: str,
    profile: ContentProfile = GENERIC_PROFILE,
) -> Tuple[bool, List[str]]:
    """
    Hard pass/fail validation for a generated message.

    Unlike scoring, a failure here means "do not post this" — the message
    contains a fabricated detail, the wrong number of hashtags, or a URL that
    should have been attached separately as a link facet.

    Args:
        message: The generated message.
        expected_hashtag_count: Exactly how many hashtags there should be.
        title: The source title, for context.
        username: The account name, used to strip self-referential hashtags.
        profile: Content profile supplying the hallucination patterns.

    Returns:
        (is_valid, issues). is_valid is True only when issues is empty.
    """
    issues = []

    hashtags = extract_hashtags(message)
    if len(hashtags) != expected_hashtag_count:
        issues.append(f'Wrong hashtag count: {len(hashtags)} (expected {expected_hashtag_count})')

    has_forbidden, found_words = contains_forbidden_words(message)
    if has_forbidden:
        issues.append(f"Contains forbidden words: {', '.join(found_words)}")

    if re.search(r'https?://', message):
        issues.append('Message contains URL (should be added separately)')

    # The important one: details the model invented that were never in the
    # input. One is enough to reject the message, so stop at the first.
    for pattern in profile.hallucination_patterns:
        if re.search(pattern, message, re.IGNORECASE):
            issues.append(f"Possible hallucination detected: '{pattern}'")
            break

    return (len(issues) == 0), issues


def validate_platform_specific(message: str, platform: str) -> List[str]:
    """
    Check a message against one social platform's formatting rules.

    Each network breaks in its own special way: Discord renders unmatched
    markdown as literal asterisks, Bluesky needs URLs attached as facets rather
    than inline text, and an accidental @everyone is a good way to get muted.

    Args:
        message: The generated message.
        platform: Target platform — 'discord', 'bluesky', 'mastodon', 'matrix'.

    Returns:
        A list of issues. Empty means the message is fine for that platform.
    """
    issues = []
    platform_lower = (platform or '').lower()

    if platform_lower == 'discord':
        if '@everyone' in message or '@here' in message:
            issues.append("Contains @everyone or @here mention")
        if re.search(r'@\d+>', message):
            issues.append("Malformed Discord mention detected")
        if message.count('**') % 2 != 0 or message.count('__') % 2 != 0:
            issues.append("Unmatched markdown formatting")

    elif platform_lower == 'bluesky':
        if re.search(r'https?://', message):
            issues.append("URL found in content (should be added separately for facets)")
        # A bare @handle without a domain won't resolve on Bluesky.
        if '@' in message and not re.search(r'@[a-zA-Z0-9][a-zA-Z0-9-]*\.', message):
            issues.append("Malformed Bluesky handle (needs .domain)")

    elif platform_lower == 'mastodon':
        # Mastodon renders plain text; stray HTML entities show up literally.
        if re.search(r'&[a-z]+;', message):
            issues.append("HTML entities detected (should be plain text)")

    return issues


def extract_from_thinking(thinking: str, max_chars: int = 300) -> Optional[str]:
    """
    Salvage a usable post from a reasoning model's thinking output.

    Qwen3 and friends sometimes spend their whole token budget reasoning and
    return an empty content field. The post is usually in there, and these are
    the shapes it tends to take, in descending order of confidence:

      1. Quoted with a leading '>', where the model "shows" its answer.
      2. After an explicit marker: "Final post:", "Here's the post:", etc.
      3. On a line carrying hashtags and roughly post-length.

    Args:
        thinking: Raw contents of the model's thinking field.
        max_chars: Longest acceptable result.

    Returns:
        The extracted message, or None if nothing usable was found — which
        usually means the model ran out of tokens mid-thought.
    """
    if not thinking:
        return None

    lines = thinking.split('\n')

    # 1. Quoted lines. The model is showing its work.
    quoted = [line.strip()[1:].strip() for line in lines if line.strip().startswith('>')]
    if quoted:
        result = ' '.join(quoted).strip()
        if len(result) >= 20:
            return result

    # 2. Explicit hand-off markers.
    markers = (
        'final post:', "here's the post:", 'the post:', 'my post:',
        'announcement:', 'here it is:', 'result:', 'output:',
    )
    lowered = thinking.lower()
    for marker in markers:
        if marker in lowered:
            after = thinking[lowered.find(marker) + len(marker):].strip()
            first_line = after.split('\n')[0].strip()
            if first_line and len(first_line) >= 20:
                return first_line.strip('"\'')

    # 3. A line with hashtags, at roughly post length.
    for line in lines:
        stripped = line.strip()
        if '#' in stripped and 30 <= len(stripped) <= max_chars:
            cleaned = re.sub(r'^[-*\u2022]\s*', '', stripped).strip('"\'')
            if cleaned:
                return cleaned

    logger.debug("Could not extract content from thinking — model may have run out of tokens")
    return None
