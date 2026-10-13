from fcli.render import TABLE_MAX_ROWS, truncate_rows


def test_truncate_rows_caps_large_tables() -> None:
    rows = [["a"] for _ in range(TABLE_MAX_ROWS + 10)]
    kept, note = truncate_rows(rows)
    assert len(kept) == TABLE_MAX_ROWS
    assert note and "25,000" in note
