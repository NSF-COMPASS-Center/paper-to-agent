from paperskill.tabletx import (TableRead, check_rows, grown_box, rectangular, split_bands,
                                to_csv, to_markdown, verdict)

BOX = [0, 0, 100, 100]
WORDS = [(1, 1, 20, 9, w) for w in
         ["Parameter", "Raw", "water", "quality", "Treatment", "effect", "159", "n=90."]]
ROWS = [["Parameter", "Raw water quality"], ["Treatment effect", "159 n=90."]]


def read(rows, cut=()):
    return TableRead(rows=rows, cut_off=list(cut), uncertain=[], notes="")


def test_complete_read_is_accepted():
    chk = check_rows(ROWS, WORDS, BOX)
    assert chk["checked"] and chk["coverage"] == 1.0 and chk["precision"] == 1.0
    assert verdict(read(ROWS), chk) == "accepted"


def test_a_dropped_number_is_reported_and_rejected():
    rows = [["Parameter", "Raw water quality"], ["Treatment effect", "n=90."]]
    chk = check_rows(rows, WORDS, BOX)
    assert "159" in chk["missing"]
    assert verdict(read(rows), chk) == "rejected"


def test_a_number_matches_as_a_whole_token():
    rows = [["Parameter", "Raw water quality"], ["Treatment effect", "1159 n=90."]]
    assert "159" in check_rows(rows, WORDS, BOX)["missing"]


def test_an_invented_word_lowers_precision():
    rows = ROWS + [["Hallucinated", "Footnote"]]
    chk = check_rows(rows, WORDS, BOX)
    assert chk["precision"] < 1.0 and "hallucinated" in chk["invented"]
    assert verdict(read(rows), chk) == "rejected"


def test_words_outside_the_box_do_not_count():
    far = WORDS + [(500, 500, 520, 509, "elsewhere")]
    assert check_rows(ROWS, far, BOX)["coverage"] == 1.0


def test_no_text_layer_is_unchecked_not_accepted():
    chk = check_rows(ROWS, [], BOX)
    assert not chk["checked"]
    assert verdict(read(ROWS), chk) == "unchecked"


def test_a_cut_off_table_is_partial_when_its_words_match():
    chk = check_rows(ROWS, WORDS, BOX)
    assert verdict(read(ROWS, cut=["bottom"]), chk) == "partial"


def test_exponent_digits_match_inside_a_mixed_token():
    words = [(1, 1 + 10 * i, 20, 9 + 10 * i, w) for i, w in
             enumerate(["Organism", "coli", "2.4E+", "04", "influent", "water", "sample"])]
    rows = [["Organism", "coli", "2.4E+04", "influent", "water", "sample"]]
    assert check_rows(rows, words, BOX)["missing"] == []


def _page(lines):
    """One word per printed line, 10 pt apart."""
    return [(10, 10 * i, 60, 10 * i + 8, w) for i, w in enumerate(lines)]


def test_title_and_footnote_lines_are_set_aside_not_counted_missing():
    lines = ["Mean titre", "Alpha", "Bravo", "Charlie", "Delta", "Echo",
             "Foxtrot", "Golf", "Hotel", "India", "Juliet", "Kilo", "Lima", "Mike", "Novem",
             "Oscar", "Papa", "Quebec", "Romeo", "Sierra", "standard deviation"]
    ws = [(x0, y0, x1, y1, w) for (x0, y0, x1, y1, t) in _page(lines) for w in t.split()]
    rows = [[w] for w in lines[1:-1]]
    chk = check_rows(rows, ws, [0, -5, 200, 300])
    assert chk["coverage"] == 1.0 and len(chk["excluded_lines"]) == 2


def test_a_header_only_refusal_cannot_hide_behind_excluded_lines():
    lines = ["Header", "Alpha", "Bravo", "Charlie", "Delta", "Echo", "Foxtrot", "Golf",
             "Hotel", "India", "Juliet", "Kilo"]
    chk = check_rows([["Header"]], _page(lines), [0, -5, 200, 300])
    assert chk["coverage"] < 0.5
    assert verdict(read([["Header"]]), chk) == "rejected"


def test_split_bands_breaks_at_the_widest_gap_near_the_target():
    ys = [i * 10 for i in range(30)]
    ys = [y + (6 if y >= 150 else 0) for y in ys]        # a wider gap before the 16th line
    ws = [(10, y, 60, y + 8, "w") for y in ys]
    bands = split_bands(ws, [0, -1, 100, 320], max_lines=14)
    assert len(bands) == 3 and bands[0][3] == bands[1][1]
    assert split_bands(ws[:10], [0, -1, 100, 320], max_lines=14) == []


def test_empty_rows_are_rejected():
    assert verdict(read([["", ""]]), check_rows([["", ""]], WORDS, BOX)) == "rejected"


def test_ragged_rows_are_padded():
    rows, padded = rectangular([["a", "b", "c"], ["d"]])
    assert padded and rows == [["a", "b", "c"], ["d", "", ""]]


def test_grown_box_only_moves_reported_sides_and_clips_to_the_page():
    assert grown_box([10, 10, 50, 50], ["bottom", "right"], [0, 0, 55, 100], 14) == [10, 10, 55, 64]
    assert grown_box([10, 10, 50, 50], ["top"], [0, 0, 100, 100], 14) == [10, 0, 50, 50]


def test_csv_and_markdown_escape_cells():
    assert to_csv([["a,b", "c"]]) == '"a,b",c\n'
    assert to_markdown([["h|1", "h2"], ["x", "y"]]).splitlines()[0] == "| h\\|1 | h2 |"
