"""What the board says about licences: the attribution footer, the demo strip, `/licences`.

The footer is built from the payloads a response was made of, never from a static
string. Each tool payload names its sources (`sources`, provenance.py); a response
carries the union as `attribution`, and the page shows the union of what is on
screen. So an ODbL source cannot be served without its attribution appearing, and a
source that served nothing is not credited.
"""

from __future__ import annotations

import html
from collections.abc import Iterable, Mapping
from typing import Any, Final

from flightops_mcp.edition import EditionPlan
from flightops_mcp.licences import CHECKED_ON, CLASS_MEANING, REGISTRY
from flightops_mcp.server import licence_notice

Payload = dict[str, Any]

# The board draws every airport from the bundled OurAirports reference, read directly
# rather than through a tool, so its record is added to every board response.
REFERENCE: Final[Payload] = {
    "name": REGISTRY["ourairports"].name,
    "licence_class": REGISTRY["ourairports"].licence_class,
    "text": REGISTRY["ourairports"].attribution,
}
STRIP_MARKER: Final = "<!--edition-strip-->"
DEMO_STRIP: Final = (
    '<aside class="edition-strip" aria-label="Edition" data-edition="demo"><p>'
    "<strong>Demo build: not licensed for commercial use.</strong> "
    "Its traffic comes from OpenSky Network, whose terms forbid commercial and operational "
    "use without a written licence. "
    '<a href="/licences">What this deployment may and may not do</a></p></aside>'
)


def attribution(*payloads: Mapping[str, Any], reference: bool = False) -> list[Payload]:
    """One line per source the payloads were built from, each once, in first-seen order."""
    lines: dict[str, Payload] = {}
    if reference:
        lines[REFERENCE["name"]] = dict(REFERENCE)
    for payload in payloads:
        if not payload.get("ok"):
            continue
        for record in payload.get("sources") or []:
            if isinstance(record, dict) and record.get("name") and record.get("attribution"):
                lines.setdefault(
                    record["name"],
                    {
                        "name": record["name"],
                        "licence_class": record.get("licence_class"),
                        "text": record["attribution"],
                    },
                )
    return list(lines.values())


def strip(plan: EditionPlan) -> str:
    """The persistent notice at the top of the page. The demo always has one."""
    return DEMO_STRIP if plan.edition == "demo" else ""


def index_page(template: str, plan: EditionPlan) -> str:
    """The board's page with the edition strip in place. A template without its marker
    is a page that lost its notice, and that is refused rather than served."""
    if STRIP_MARKER not in template:
        raise ValueError("index.html has lost its edition strip marker")
    return template.replace(STRIP_MARKER, strip(plan))


def _list(items: Iterable[str]) -> str:
    return "".join(f"<li>{html.escape(item)}</li>" for item in items)


def licences_page(plan: EditionPlan) -> str:
    """`/licences`: the edition, what it may and may not do, each source's terms, ODbL's notice."""
    notice = licence_notice(plan)
    rows = []
    for item in notice["sources"]:
        terms = plan.licence(item["source"])
        link = f'<a href="{html.escape(terms.terms_url)}">terms</a>' if terms.terms_url else "—"
        rows.append(
            "<tr>"
            f'<th scope="row">{html.escape(terms.name)}</th>'
            f"<td>{terms.licence_class} ({html.escape(CLASS_MEANING[terms.licence_class])})</td>"
            f"<td>{html.escape(terms.licence_name)}</td>"
            f"<td><ul>{_list(terms.obligations)}</ul></td>"
            f"<td>{link}</td>"
            "</tr>"
        )
    odbl = notice["derived_database_notice"]
    derived = (
        f'<h2 id="odbl">Derived database notice (ODbL 1.0)</h2><p>{html.escape(odbl)}</p>'
        if odbl
        else ""
    )
    strip_html = strip(plan)
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Licences: airport board</title><link rel="stylesheet" href="/static/board.css"></head>
<body class="licences">{strip_html}<main>
<h1>Licences and what this deployment may do</h1>
<p>Edition: <strong>{html.escape(plan.edition)}</strong>. Commercial use:
<strong>{html.escape(plan.commercial_use)}</strong>. Licence classes served:
{", ".join(sorted(plan.allowed_classes))}. Terms checked on {CHECKED_ON}.</p>
<h2>This deployment may</h2><ul>{_list(notice["may"])}</ul>
<h2>This deployment may not</h2><ul>{_list(notice["may_not"])}</ul>
<h2>Sources in use</h2>
<table><thead><tr><th scope="col">Source</th><th scope="col">Class</th>
<th scope="col">Licence</th><th scope="col">Obligations</th><th scope="col">Terms</th></tr>
</thead><tbody>{"".join(rows)}</tbody></table>
{derived}
<p><a href="/">Back to the board</a></p>
</main></body></html>"""
