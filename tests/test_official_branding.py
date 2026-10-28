"""HACS only: the integration says it is not made or supported by Nighp, and ships its icon.

The app maker allowed the name Baby Tracker and the official icon on the condition that the
documentation says the integration isn't theirs. The README and the home-assistant.io page draft
both carry that disclaimer, and the brand folder holds the icon at the two sizes Home Assistant
reads.
"""

import re

import pytest
from PIL import Image, ImageChops

from .helpers import INTEGRATION, ROOT

README = ROOT / "README.md"
PAGE = ROOT / "docs" / "home-assistant-io" / "baby_tracker.markdown"
BRAND = INTEGRATION / "brand"


def _live_text(path) -> str:
    """The file's text without HTML comments, flattened so a wrapped line still matches."""
    text = re.sub(r"<!--.*?-->", "", path.read_text(), flags=re.DOTALL)
    return " ".join(text.split())


def _top_of_readme() -> str:
    lines = README.read_text().splitlines()
    return " ".join(" ".join(lines[:15]).split())


def test_the_readme_says_it_is_not_made_or_supported_by_nighp_near_the_top() -> None:
    assert "not made or supported by Nighp" in _top_of_readme()


def test_the_readme_disclaimer_names_the_app_maker_and_the_app() -> None:
    top = _top_of_readme()
    assert "the maker of Baby Tracker" in top


def test_the_readme_disclaimer_sends_help_requests_to_this_repository() -> None:
    top = _top_of_readme()
    assert "open an issue in this repository" in top
    assert re.search(r"(don't|do not) contact Nighp", top)


def test_the_docs_page_says_it_is_not_made_or_supported_by_nighp() -> None:
    assert "not made or supported by Nighp" in _live_text(PAGE)


def test_the_docs_page_disclaimer_names_the_app_maker_and_sends_help_to_the_repository() -> None:
    live = _live_text(PAGE)
    assert "the maker of Baby Tracker" in live
    assert "open an issue" in live
    assert re.search(r"(don't|do not) contact Nighp", live)


def test_the_docs_page_disclaimer_comes_before_the_supported_devices_section() -> None:
    live = _live_text(PAGE)
    assert live.index("not made or supported by Nighp") < live.index("## Supported devices")


def test_the_disclaimer_is_one_or_two_sentences_in_the_readme() -> None:
    top = _top_of_readme()
    start = top.index("This integration is not made or supported by Nighp")
    sentences = re.findall(r"[^.]+\.", top[start:])
    disclaimer = []
    for sentence in sentences:
        disclaimer.append(sentence)
        if re.search(r"(don't|do not) contact Nighp", sentence):
            break
    assert 1 <= len(disclaimer) <= 2


@pytest.mark.parametrize(("name", "size"), [("icon.png", 256), ("icon@2x.png", 512)])
def test_the_brand_icons_are_square_pngs_at_the_sizes_home_assistant_reads(
    name: str, size: int
) -> None:
    with Image.open(BRAND / name) as icon:
        assert icon.format == "PNG"
        assert icon.size == (size, size)


@pytest.mark.parametrize("name", ["icon.png", "icon@2x.png"])
def test_the_brand_icons_are_fully_opaque_like_the_official_icon(name: str) -> None:
    with Image.open(BRAND / name) as icon:
        if icon.mode == "RGBA":
            assert icon.getchannel("A").getextrema() == (255, 255)
        else:
            assert icon.mode == "RGB"


def test_the_two_icons_are_the_same_picture_at_two_sizes() -> None:
    with Image.open(BRAND / "icon.png") as small, Image.open(BRAND / "icon@2x.png") as large:
        shrunk = large.convert("RGB").resize(small.size, Image.LANCZOS)
        diff = ImageChops.difference(small.convert("RGB"), shrunk)
        mean = sum(diff.convert("L").getdata()) / (small.width * small.height)
    assert mean < 3
