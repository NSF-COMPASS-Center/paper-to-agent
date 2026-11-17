from paperskill.tablesnap import Frame, edit_distance, snap_rows, snap_token

BOX = [0, 0, 400, 400]


def words(*texts, step=12):
    return [(10 + 60 * (i % 5), step * (i // 5), 60 + 60 * (i % 5), step * (i // 5) + 9, t)
            for i, t in enumerate(texts)]


def test_edit_distance_and_cap():
    assert edit_distance("kitten", "sitting", cap=4) == 3
    assert edit_distance("abc", "abcdefgh", cap=2) == 3


def test_a_token_the_pdf_carries_is_never_touched():
    native = {"cgytggatg": {"CGYTGGATG"}}
    assert snap_token("CGYTGGATG", native) == (None, "exact")


def test_a_one_letter_misread_snaps_to_the_pdf_spelling():
    native = {"gtctccachgacacyaaytggac": {"GTCTCCACHGACACYAAYTGGAC"}}
    assert snap_token("GTCTCCACHGACAYAAYTGGAC", native) == ("GTCTCCACHGACACYAAYTGGAC", "snapped")


def test_two_equally_close_words_are_ambiguous_and_left_alone():
    native = {"sample12": {"Sample12"}, "sample13": {"Sample13"}}
    assert snap_token("Sample14", native) == (None, "ambiguous")


def test_short_tokens_are_not_snapped():
    assert snap_token("1.2", {"1.3": {"1.3"}}) == (None, "short")


def test_a_footnote_marker_glued_on_is_kept():
    native = {"scm2": {"s/cm2"}}
    assert snap_token("s/cm2a", native)[0] is None


def test_snap_rows_reports_every_change_and_keeps_structure():
    ws = words("Name", "Sequence", "COG1F", "CGYTGGATGCGNTTYCATGA", "COG1R",
               "CTTAGACGCCATCATCATTYAC")
    rows = [["Name", "Sequence"], ["COG1F", "CGYTGGATGCGNTTYCATGA"],
            ["COG1R", "CTTAGACGCCATCATCATTYAT"]]
    out = snap_rows(rows, ws, BOX)
    assert out["rows"][2] == ["COG1R", "CTTAGACGCCATCATCATTYAC"]
    assert out["snaps"] == [{"row": 2, "col": 1, "from": "CTTAGACGCCATCATCATTYAT",
                             "to": "CTTAGACGCCATCATCATTYAC"}]
    assert out["rows"][:2] == rows[:2]


def test_far_off_token_is_paired_with_the_next_leftover_in_reading_order():
    ws = words("Name", "Sequence", "A1", "ACCGGATGGCCAATCCAA", "A2", "TTGTCGGTTGCAATGCAAGT")
    rows = [["Name", "Sequence"], ["A1", "ACCCGTTGCCCAATCAA"], ["A2", "TTGTCGCTGCTAACTGGTTGA"]]
    out = snap_rows(rows, ws, BOX)
    assert [r[1] for r in out["rows"][1:]] == ["ACCGGATGGCCAATCCAA", "TTGTCGGTTGCAATGCAAGT"]
    assert len(out["paired"]) == 2


def test_a_pdf_cell_stored_as_two_words_is_rejoined():
    ws = words("Name", "Probe", "P1", "FAM-CCGACTACTTTGGGTGTCCGTGTTTC", "-TAMRA", "Reverse")
    rows = [["Name", "Probe"], ["P1", "FAM-CCGACTACTTTGGTGTTCCGTTTC-TAMRA"]]
    out = snap_rows(rows, ws, BOX)
    assert out["rows"][1][1] == "FAM-CCGACTACTTTGGGTGTCCGTGTTTC-TAMRA"


def test_missing_layer_is_skipped():
    assert snap_rows([["abcdef"]], [], BOX)["skipped"] == "no text layer"


def test_a_value_the_pdf_stores_as_separate_words_is_left_alone():
    # "2.6E+03" is stored as "2.6E", a corrupted plus, "03"
    ws = words("Organism", "coli", "2.6E", "\u00fe", "03", "influent", "water")
    rows = [["Organism", "coli", "2.6E+03", "influent", "water"]]
    out = snap_rows(rows, ws, BOX)
    assert out["rows"] == rows and out["snaps"] == [] and out["paired"] == []


def test_numeric_tokens_are_never_paired_by_order():
    ws = words("Name", "Value", "r1", "1.7E", "06", "9.9E", "Alpha")
    rows = [["Name", "Value"], ["r1", "5.5E+08"]]
    out = snap_rows(rows, ws, BOX)
    assert out["rows"] == rows and out["paired"] == []


def test_adjacent_unmatched_tokens_are_joined_before_pairing():
    ws = words("Name", "Seq", "A1", "GCAGCGTCAGTGCTGTCAA", "Alpha", "Bravo")
    rows = [["Name", "Seq"], ["A1", "GCAGCGTCA GTGCTGTCAA"]]
    out = snap_rows(rows, ws, BOX)
    assert out["rows"][1][1] == "GCAGCGTCAGTGCTGTCAA"


def test_frame_turns_bottom_to_top_text_upright():
    # a 2-word line running up the page at x=100: "alpha" is lower, "beta" higher
    f = Frame((0, -1), 600, 800)
    ws = [(95, 500, 105, 540, "alpha"), (95, 440, 105, 490, "beta")]
    a, b = f.words(ws)
    assert a[0] < b[0] and abs(a[1] - b[1]) < 1e-9          # reads left to right, one line
    assert (a[2] - a[0]) > (a[3] - a[1])                    # wider than tall
    assert f.box([90, 430, 110, 545]) == [255, 90, 370, 110]


def test_top_to_bottom_and_upside_down_frames_are_inverses_of_the_page_turn():
    w, h = 600, 800
    f = Frame((0, 1), w, h)
    first, second = f.words([(95, 100, 105, 140, "alpha"), (95, 150, 105, 190, "beta")])
    assert first[0] < second[0]
    g = Frame((-1, 0), w, h)
    x, y = g.words([(10, 10, 60, 20, "up"), (100, 10, 150, 20, "side")])
    assert x[0] > y[0]                                     # reading reversed -> restored order flips
