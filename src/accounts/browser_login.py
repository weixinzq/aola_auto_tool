"""Recognize login challenge responses without opening a browser."""


def is_captcha_page(text: str) -> bool:
    return any(marker in text.lower() for marker in (
        "tcaptcha.js", "tencentcaptcha", "/wafcaptcha",
    ))
