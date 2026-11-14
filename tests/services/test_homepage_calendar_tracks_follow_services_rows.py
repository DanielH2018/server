"""The calendar column's track count follows the Services group's row count.

The two Top Row columns are separate grids, and `custom.css` squares their bottom edges by
cutting the calendar column into two tracks per Services row: the calendar spans all but the
last four, the Deploy/Artifacts/Docs row takes two of those and the two link rows one each. A Services tile added without re-deriving the CSS
leaves an orphan on the last row and the calendar column a row short. Nothing logs it, and the
pod stays 1/1, so only this check or a look at the page catches it.

Paired, per the repo's red-proof rule.

Run: uv run pytest ansible/tests/services/test_homepage_calendar_tracks_follow_services_rows.py
"""

import re

from _homepage_config import custom_css, services_yaml, settings_yaml

TRACKS = re.compile(
    r"ul\.services-list:has\(> #my-calendar\)\s*\{[^}]*?grid-template-rows:\s*repeat\((\d+),"
)
SPAN = re.compile(r"#my-calendar\s*\{[^}]*?grid-row:\s*1\s*/\s*span\s*(\d+);")


def layout_problems(tiles: int, columns: int, css: str) -> list[str]:
    """What is wrong with a Services group of `tiles` tiles at `columns` columns beside `css`."""
    out = []
    if tiles % columns:
        out.append(
            f"{tiles} Services tiles at {columns} columns leaves an orphan on the last row"
        )
    rows = -(-tiles // columns)
    tracks, span = TRACKS.search(css), SPAN.search(css)
    if not tracks or not span:
        return out + [
            "custom.css no longer carries the calendar's track count and span"
        ]
    if int(tracks.group(1)) != 2 * rows:
        out.append(
            f"calendar has {tracks.group(1)} tracks; {rows} Services rows need {2 * rows}"
        )
    if int(span.group(1)) != 2 * rows - 4:
        out.append(
            f"calendar spans {span.group(1)} tracks; {rows} rows need {2 * rows - 4}"
        )
    return out


def _services_tiles() -> int:
    [top_row] = [entry["Top Row"] for entry in services_yaml() if "Top Row" in entry]
    [services] = [group["Services"] for group in top_row if "Services" in group]
    return len(services)


def test_the_deployed_layout_squares_the_two_columns():
    columns = settings_yaml()["layout"]["Top Row"]["Services"]["columns"]
    assert layout_problems(_services_tiles(), columns, custom_css()) == []


def test_the_tool_tiles_sit_between_the_calendar_and_the_links():
    """The CSS spans the tool tiles over the row the calendar gives up, matched by id prefix.

    A tool tile without the prefix renders icon-only in a link cell, and one placed after a link
    leaves the tool row short.
    """
    [top_row] = [entry["Top Row"] for entry in services_yaml() if "Top Row" in entry]
    [calendar] = [group["Calendar"] for group in top_row if "Calendar" in group]
    ids = [next(iter(tile.values())).get("id", "") for tile in calendar]
    assert ids[:4] == [
        "my-calendar",
        "calendar-tool-deploy",
        "calendar-tool-artifacts",
        "calendar-tool-docs",
    ]
    assert not [i for i in ids[4:] if i.startswith("calendar-tool-")]


def test_a_tile_added_without_rederiving_the_css_is_flagged():
    problems = layout_problems(_services_tiles() + 1, 3, custom_css())
    assert any("orphan" in p for p in problems), problems
    assert any("tracks;" in p for p in problems), problems
