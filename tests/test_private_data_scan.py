"""Nothing committed says where the owner shops or lives, or holds their key."""

import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).parent.parent / "scripts"))

import private_data_scan


def test_a_home_network_address_is_never_committed():
    # Built at run time, so this file does not itself hold what it forbids.
    for address in (".".join(p) for p in (("10", "0", "0", "7"), ("192", "168", "1", "50"))):
        assert private_data_scan.offences(f"bridge at http://{address}:8000", set()), address


def test_a_street_address_is_never_committed():
    street = "Storgata" + " 12"
    assert private_data_scan.offences(f"the shop in {street}", set())
    assert private_data_scan.offences("Kirke" + "veien 3B", set())


def test_coordinates_more_exact_than_two_decimals_are_never_committed():
    assert private_data_scan.offences("home = " + "59.91" + "38, 10.75" + "22", set())
    assert private_data_scan.offences("home = 59.91, 10.75", set()) == []


def test_a_word_from_the_private_list_is_never_committed():
    shop = "Meny " + "Sentrum Nord"
    assert private_data_scan.offences(f"cheapest at {shop.lower()}", {shop})


def test_the_key_is_never_committed(tmp_path):
    key = tmp_path / "key"
    key.write_text("made-up-key-0123456789\n")
    found = private_data_scan.private_values(tmp_path / "none.toml", key)
    assert private_data_scan.offences("Bearer made-up-key-0123456789", found)


def test_the_private_list_is_read_from_local_config(tmp_path):
    words = tmp_path / "private.toml"
    words.write_text('words = ["Meny Sentrum Nord", "ab"]\n')
    # A word too short to be told from ordinary text is left out.
    assert private_data_scan.private_values(words, tmp_path / "no-key") == {"Meny Sentrum Nord"}


def test_ordinary_text_and_documentation_addresses_are_allowed():
    text = "Meny and Joker, 192.0.2.10, version 1.2.3.4, 26.90 kroner, prices-bulk takes 100"
    assert private_data_scan.offences(text, set()) == []


def _clean_run(tmp_path, *flags):
    return private_data_scan.main(
        [*flags, "--words", str(tmp_path / "none.toml"), "--key-file", str(tmp_path / "no-key")]
    )


def test_the_tracked_tree_is_clean(tmp_path):
    assert _clean_run(tmp_path, "--tracked") == 0


def test_the_history_is_clean(tmp_path):
    assert _clean_run(tmp_path, "--history") == 0


def test_a_broken_private_list_refuses_the_commit_and_says_why(tmp_path, capsys):
    words = tmp_path / "private.toml"
    words.write_text('words = ["Meny Sentrum Nord"\n', encoding="utf-8")
    assert private_data_scan.main(["--tracked", "--words", str(words)]) == 1
    said = capsys.readouterr().err
    assert "private.toml can't be read" in said
    assert "Meny Sentrum" not in said


def test_a_private_list_without_a_list_of_words_refuses_the_commit(tmp_path, capsys):
    words = tmp_path / "private.toml"
    words.write_text('words = "Meny Sentrum Nord"\n', encoding="utf-8")
    assert private_data_scan.main(["--tracked", "--words", str(words)]) == 1
    assert "a list of words" in capsys.readouterr().err
