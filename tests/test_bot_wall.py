"""Bot-wall detection must look at visible text, not <script> configuration."""

from __future__ import annotations

from evidence_runtime.extract import _looks_like_bot_wall

WIKI_LIKE = """
<html><head><title>Python (programming language) - Wikipedia</title></head>
<body>
<script>RLQ.push({"wgConfirmEditCaptchaNeededForGenericEdit":"hcaptcha",
"wgConfirmEditForceShowCaptcha":false});</script>
<h1>Python (programming language)</h1>
<p>Python is a high-level, general-purpose programming language.</p>
</body></html>
"""

CHALLENGE = """
<html><head><title>Just a moment...</title></head>
<body><script>window.x=1;</script>
<noscript>Please enable JavaScript and cookies to continue</noscript>
</body></html>
"""


def test_script_config_captcha_is_not_a_bot_wall():
    assert _looks_like_bot_wall(WIKI_LIKE) is False


def test_visible_challenge_copy_is_a_bot_wall():
    assert _looks_like_bot_wall(CHALLENGE) is True


def test_challenge_title_is_a_bot_wall():
    assert _looks_like_bot_wall("<html><body>hi</body></html>", title_guess="Attention Required!") is True
