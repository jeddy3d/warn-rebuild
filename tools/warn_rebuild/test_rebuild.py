"""Run in the rebuild env: pip install -r requirements.txt pytest; pytest tools/warn_rebuild."""

import csv
import io

from rebuild import normalize_wi, wi_previous_names

HEADER = "Company,City,Affected Workers,Notice Received\n"


def rows(body):
    return list(csv.DictReader(io.StringIO(body.decode(), newline="")))


def test_update_prefix_and_plain_note_are_dropped():
    body = (
        HEADER + "UPDATE 2 - Synthetic Foods,Town,71 (+2),9/30/2026\n"
        '"Synthetic Works* The company clarified the count.",Town,75,8/12/2026\n'
        "Plain Notice,Town,10,11/1/2026\n"
    ).encode()
    out, n = normalize_wi(body)
    assert [r["Company"] for r in rows(out)] == [
        "Synthetic Foods",
        "Synthetic Works",
        "Plain Notice",
    ]
    assert n == 2
    assert (
        rows(out)[0]["Affected Workers"] == "71 (+2)"
    )  # jobs tolerance happens in the transformer


def test_republished_notice_keeps_the_previous_verbatim_name():
    previous = [
        {"postal_code": "WI", "company": 'Synthetic Works<br/></a><a><em style="x">*note</em>'}
    ]
    body = (
        HEADER + '"Synthetic Works* The company clarified the count.",Town,75,8/12/2026\n'
    ).encode()
    out, _ = normalize_wi(body, wi_previous_names(previous))
    assert rows(out)[0]["Company"] == previous[0]["company"]


def test_names_with_page_html_before_the_asterisk_are_untouched():
    name = 'Old Notice<br/></a><a><em style="x">* old note</em>'
    body = (HEADER + f'"{name.replace(chr(34), chr(34) * 2)}",Town,5,1/2/2025\n').encode()
    out, n = normalize_wi(body)
    assert rows(out)[0]["Company"] == name and n == 0
