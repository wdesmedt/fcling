import io

from rich.console import Console

from fcli import render
from fcli.checks import Finding
from fcli.report import Column, Report


def test_a_cell_is_printed_as_it_is() -> None:
    # Rich would show an ESI's ":ab:" as an emoji, and "[...]" as markup.
    esi = "01:ab:cd:00:00:00:00:00:00:01"
    report = Report(
        name="t",
        help="t",
        reads=(),
        record=Finding,
        build=lambda node: [],
        columns=(Column("subject", "subject"), Column("detail", "detail")),
    )
    row = Finding(node="leaf1", check="c", severity="error", subject=esi, detail="in [mh-1]")
    out = io.StringIO()
    render.table(report, [row], Console(file=out, width=200))
    assert esi in out.getvalue() and "in [mh-1]" in out.getvalue()
