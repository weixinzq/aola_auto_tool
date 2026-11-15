from __future__ import annotations

import io
from dataclasses import dataclass
from functools import lru_cache
from importlib.resources import files
from pathlib import Path
from typing import Union

import numpy as np
from PIL import Image


WORD_WIDTH = 80
WORD_HEIGHT = 32
DIGIT_COUNT = 4
DIGIT_WIDTH = WORD_WIDTH // DIGIT_COUNT
COLOR_MARGIN = 30

# These conservative limits accept every verified sample in this repository.
# Images outside the known colour/font distribution are sent to the fallback
# OCR by aola_accounts/ocr.py instead of returning a low-confidence guess.
MAX_DISTANCE = 130
MIN_MARGIN = 12

_MODEL_RESOURCE = files("aola_accounts").joinpath("data", "fast_templates.npz")


@dataclass(frozen=True)
class Recognition:
    value: str
    distances: tuple[int, ...]
    margins: tuple[int, ...]

    @property
    def confident(self) -> bool:
        return (
            max(self.distances, default=MAX_DISTANCE + 1) <= MAX_DISTANCE
            and min(self.margins, default=MIN_MARGIN - 1) >= MIN_MARGIN
        )


def _open_rgb(source: Union[str, Path, bytes, bytearray]) -> np.ndarray:
    if isinstance(source, (bytes, bytearray)):
        stream = io.BytesIO(bytes(source))
        with Image.open(stream) as image:
            return np.asarray(image.convert("RGB"), dtype=np.int16)
    with Image.open(source) as image:
        return np.asarray(image.convert("RGB"), dtype=np.int16)


def extract_digit_features(
    source: Union[str, Path, bytes, bytearray],
) -> np.ndarray:
    """Return four normalized binary digit images with black lines removed."""
    rgb = _open_rgb(source)
    red = rgb[:, :, 0]
    green = rgb[:, :, 1]
    blue = rgb[:, :, 2]

    # The site draws digits in blue and interference lines in black. Comparing
    # channels is more stable than a fixed brightness threshold and removes the
    # lines before recognition.
    mask = (blue > red + COLOR_MARGIN) & (blue > green + COLOR_MARGIN)
    rows, columns = np.where(mask)
    if len(columns) < 100:
        raise ValueError("image does not contain enough blue digit pixels")

    x0, x1 = int(columns.min()), int(columns.max()) + 1
    y0, y1 = int(rows.min()), int(rows.max()) + 1
    if x1 - x0 < 40 or y1 - y0 < 10:
        raise ValueError("detected digit area is unexpectedly small")

    cropped = Image.fromarray(mask[y0:y1, x0:x1])
    normalized = cropped.resize(
        (WORD_WIDTH, WORD_HEIGHT),
        Image.Resampling.NEAREST,
    )
    word = np.asarray(normalized, dtype=np.bool_)
    return np.stack(
        [
            word[:, position * DIGIT_WIDTH : (position + 1) * DIGIT_WIDTH]
            .reshape(-1)
            for position in range(DIGIT_COUNT)
        ]
    )


@lru_cache(maxsize=1)
def _load_templates() -> tuple[np.ndarray, np.ndarray]:
    if not _MODEL_RESOURCE.is_file():
        raise FileNotFoundError(
            f"fast OCR templates not found: {_MODEL_RESOURCE}. "
            "Run image/build_fast_templates.py to generate them."
        )

    with _MODEL_RESOURCE.open("rb") as stream, np.load(stream, allow_pickle=False) as model:
        templates = model["templates"].astype(np.bool_, copy=False)
        labels = model["labels"].astype(np.uint8, copy=False)

    expected_size = WORD_HEIGHT * DIGIT_WIDTH
    if templates.ndim != 2 or templates.shape[1] != expected_size:
        raise RuntimeError(f"invalid fast OCR template shape: {templates.shape}")
    if labels.ndim != 1 or len(labels) != len(templates):
        raise RuntimeError("fast OCR labels do not match the templates")
    return templates, labels


def recognize(source: Union[str, Path, bytes, bytearray]) -> Recognition:
    """Recognize four digits and expose confidence data for fallback routing."""
    digits = extract_digit_features(source)
    templates, labels = _load_templates()

    # Shape: digit position x template. Hamming distance is sufficient for the
    # normalized binary glyphs and avoids a neural-network runtime entirely.
    distances = np.count_nonzero(
        templates[np.newaxis, :, :] != digits[:, np.newaxis, :],
        axis=2,
    )

    classes = np.unique(labels)
    class_distances = np.full((DIGIT_COUNT, 10), digits.shape[1] + 1, dtype=np.int32)
    for label in classes:
        class_distances[:, int(label)] = distances[:, labels == label].min(axis=1)

    order = np.argsort(class_distances, axis=1)
    positions = np.arange(DIGIT_COUNT)
    best_labels = order[:, 0]
    best_distances = class_distances[positions, best_labels]
    second_distances = class_distances[positions, order[:, 1]]

    return Recognition(
        value="".join(str(int(label)) for label in best_labels),
        distances=tuple(int(value) for value in best_distances),
        margins=tuple(int(value) for value in second_distances - best_distances),
    )


def get_num(image_path: Path) -> str:
    """Recognize a known-style image without using the fallback OCR."""
    path = Path(image_path)
    if not path.is_file():
        raise FileNotFoundError(f"captcha image does not exist: {path}")

    result = recognize(path)
    if not result.confident:
        raise ValueError(
            "fast OCR result is uncertain: "
            f"value={result.value}, distances={result.distances}, "
            f"margins={result.margins}"
        )
    return result.value

