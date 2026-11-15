from __future__ import annotations

import argparse
import math
import re
from functools import lru_cache
from pathlib import Path
from typing import Any

import ddddocr


from .fast_ocr import recognize as _recognize_fast


_NEGATIVE_INFINITY = -1.0e100
_DIGITS = "0123456789"
_ALPHANUMERIC = _DIGITS + "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ"


def _log_add(left: float, right: float) -> float:
    """Add probabilities represented in log space."""
    if left <= _NEGATIVE_INFINITY:
        return right
    if right <= _NEGATIVE_INFINITY:
        return left
    if left < right:
        left, right = right, left
    return left + math.log1p(math.exp(right - left))


@lru_cache(maxsize=4)
def _get_ocr(mode: str = "digits", beta: bool = False) -> Any:
    """Load the relatively expensive OCR model only once per process."""
    ocr = ddddocr.DdddOcr(show_ad=False, beta=beta)
    ocr.set_ranges(_DIGITS if mode == "digits" else _ALPHANUMERIC)
    return ocr


def _decode_exactly_four_digits(result: dict[str, Any], beam_width: int = 10) -> str:
    """Decode the OCR probabilities with a four-digit CTC constraint."""
    charset = list(result["charset"])
    probabilities = result["probabilities"]

    blank_index = charset.index("")
    digit_indexes = [(digit, charset.index(digit)) for digit in _DIGITS]

    # Each prefix stores (probability ending in blank, ending in non-blank).
    beam: dict[str, tuple[float, float]] = {
        "": (0.0, _NEGATIVE_INFINITY)
    }

    for time_step in probabilities:
        row = time_step[0]
        next_beam: dict[str, tuple[float, float]] = {}
        blank_log_probability = math.log(max(float(row[blank_index]), 1.0e-300))

        for prefix, (blank_score, non_blank_score) in beam.items():
            total_score = _log_add(blank_score, non_blank_score)

            next_blank, next_non_blank = next_beam.get(
                prefix,
                (_NEGATIVE_INFINITY, _NEGATIVE_INFINITY),
            )
            next_blank = _log_add(
                next_blank,
                total_score + blank_log_probability,
            )
            next_beam[prefix] = (next_blank, next_non_blank)

            for digit, digit_index in digit_indexes:
                digit_log_probability = math.log(
                    max(float(row[digit_index]), 1.0e-300)
                )

                if prefix.endswith(digit):
                    # A repeated CTC character without a blank keeps the same prefix.
                    same_blank, same_non_blank = next_beam.get(
                        prefix,
                        (_NEGATIVE_INFINITY, _NEGATIVE_INFINITY),
                    )
                    same_non_blank = _log_add(
                        same_non_blank,
                        non_blank_score + digit_log_probability,
                    )
                    next_beam[prefix] = (same_blank, same_non_blank)

                    # A blank between equal characters creates a new digit.
                    if len(prefix) < 4:
                        extended = prefix + digit
                        extended_blank, extended_non_blank = next_beam.get(
                            extended,
                            (_NEGATIVE_INFINITY, _NEGATIVE_INFINITY),
                        )
                        extended_non_blank = _log_add(
                            extended_non_blank,
                            blank_score + digit_log_probability,
                        )
                        next_beam[extended] = (
                            extended_blank,
                            extended_non_blank,
                        )
                elif len(prefix) < 4:
                    extended = prefix + digit
                    extended_blank, extended_non_blank = next_beam.get(
                        extended,
                        (_NEGATIVE_INFINITY, _NEGATIVE_INFINITY),
                    )
                    extended_non_blank = _log_add(
                        extended_non_blank,
                        total_score + digit_log_probability,
                    )
                    next_beam[extended] = (
                        extended_blank,
                        extended_non_blank,
                    )

        beam = dict(
            sorted(
                next_beam.items(),
                key=lambda item: _log_add(*item[1]),
                reverse=True,
            )[:beam_width]
        )

    candidates = [
        (_log_add(blank_score, non_blank_score), prefix)
        for prefix, (blank_score, non_blank_score) in beam.items()
        if len(prefix) == 4
    ]
    if not candidates:
        raise ValueError("OCR could not produce a four-digit candidate")
    return max(candidates)[1]


def recognize(image_path: str | Path, *, mode: str = "digits", beta: bool = False) -> str:
    """Recognize four digits, or letters and digits without a fixed length."""
    if mode not in ("digits", "alphanumeric"):
        raise ValueError(f"unknown OCR mode: {mode!r}")
    path = Path(image_path)
    if not path.is_file():
        raise FileNotFoundError(f"captcha image does not exist: {path}")

    return recognize_bytes(path.read_bytes(), mode=mode, beta=beta)


def recognize_bytes(image_bytes: bytes, *, mode: str = "digits", beta: bool = False) -> str:
    """Recognize encoded image bytes; beta=True selects the ddddocr beta model."""
    if mode not in ("digits", "alphanumeric"):
        raise ValueError(f"unknown OCR mode: {mode!r}")
    if not isinstance(image_bytes, bytes):
        raise TypeError("image_bytes must be bytes")
    if not image_bytes:
        raise ValueError("image_bytes must not be empty")

    if mode == "alphanumeric":
        value = str(_get_ocr(mode, beta).classification(image_bytes)).strip()
        if re.fullmatch(r"[A-Za-z0-9]+", value) is None:
            raise ValueError(f"OCR result is not letters and digits: {value!r}")
        return value

    if not beta:
        try:
            fast_result = _recognize_fast(image_bytes)
        except (OSError, RuntimeError, ValueError):
            fast_result = None
        if fast_result is not None and fast_result.confident:
            return fast_result.value

    raw_value = str(_get_ocr(mode, beta).classification(image_bytes)).strip()
    if re.fullmatch(r"\d{4}", raw_value) is not None:
        return raw_value

    # Most images are already decoded as four digits. Requesting the complete
    # probability matrix and running constrained CTC decoding is reserved for
    # ambiguous results, such as an otherwise plausible five-digit output.
    result = _get_ocr(mode, beta).classification(image_bytes, probability=True)
    if not isinstance(result, dict):
        raise RuntimeError(f"unexpected OCR result type: {type(result).__name__}")

    value = _decode_exactly_four_digits(result)
    if re.fullmatch(r"\d{4}", value) is None:
        raise ValueError(f"OCR result is not four digits: {value!r}")
    return value


def main() -> int:
    parser = argparse.ArgumentParser(description="Recognize one CAPTCHA image")
    parser.add_argument("image", type=Path)
    parser.add_argument(
        "--mode", choices=("digits", "alphanumeric"), default="digits",
        help="digits: exactly four digits; alphanumeric: letters and digits",
    )
    args = parser.parse_args()
    print(recognize(args.image, mode=args.mode))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
