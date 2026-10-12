"""Testy deterministického overovania – bežia bez modelu aj bez internetu."""

from radar.market import combine, to_base, unit_kind
from radar.util import chunk_text, strip_thinking
from radar.verify import number_candidates, number_present, numbers_missing_in_source, quote_score

SOURCE = (
    "Celkový dlh štátnych nemocníc vzrástol na 1,444 mld. €. "
    "Obec tvrdí, že „nemá kapacity“ na verejné obstarávanie – projekty preto meškajú. "
    "Na Slovensku je 2 890 obcí a nedoplatky tvoria 15,9 % predpisu."
)


def test_exact_quote_is_found():
    assert quote_score("Celkový dlh štátnych nemocníc vzrástol na 1,444 mld. €.", SOURCE) == 100


def test_whitespace_and_typographic_quotes_are_normalized():
    quote = 'obec tvrdí, že "nemá kapacity" na verejné   obstarávanie - projekty preto meškajú'
    assert quote_score(quote, SOURCE) == 100


def test_quote_shortened_with_ellipsis_is_checked_by_parts():
    quote = "Celkový dlh štátnych nemocníc … na Slovensku je 2 890 obcí"
    assert quote_score(quote, SOURCE) == 100


def test_fabricated_quote_is_rejected():
    assert quote_score("Ministerstvo plánuje zrušiť všetky malé obce do roku 2027.", SOURCE) < 80


def test_changed_number_is_caught_even_when_text_matches():
    quote = "Celkový dlh štátnych nemocníc vzrástol na 1,5 mld. €."
    assert quote_score(quote, SOURCE) >= 90  # fuzzy zhoda by citát prepustila…
    assert numbers_missing_in_source(quote, SOURCE) == ["1,5"]  # …kontrola čísel nie
    assert numbers_missing_in_source("Na Slovensku je 2890 obcí", SOURCE) == []


def test_numbers_in_slovak_and_english_formats():
    assert number_present(1.444, "dlh vzrástol na 1,444 mld. €")
    assert number_present(2890, "na Slovensku je 2 890 obcí")
    assert number_present(15.9, "nedoplatky 15,9 %")
    assert number_present(2781, "hodnota 2.781 mil. €")  # bodka ako oddeľovač tisícov
    assert number_present(2.781, "hodnota 2.781 mld. €")
    assert number_present(1444.5, "spolu 1 444,5 tis. €")
    assert not number_present(5000, "nedoplatky 15,9 %")
    assert not number_present(1202, "v roku 1 2024")  # "1 202" sa nesmie vyrobiť z "1 2024"


def test_number_candidates_ambiguous_comma():
    assert number_candidates("1,444") == {1.444, 1444.0}


def test_chunking_covers_whole_text():
    text = "".join(f"Veta číslo {i} o probléme obcí.\n" for i in range(3000))
    chunks = chunk_text(text, 6000, 400)
    assert len(chunks) > 1
    assert all(len(c) <= 6000 for c in chunks)
    assert text.startswith(chunks[0][:50]) and text.endswith(chunks[-1][-50:])
    assert chunk_text(text, 6000, 400) == chunks  # deterministické delenie


def test_strip_thinking():
    assert strip_thinking("<think>hmm</think>{\"a\": 1}") == '{"a": 1}'
    assert strip_thinking("{\"a\": 1}<think>neuzavreté") == '{"a": 1}'


def test_user_agent_is_ascii():
    from radar.config import FetchConfig

    assert FetchConfig(user_agent="Radar (kontakt: doplň)").user_agent == "Radar (kontakt: dopln)"


def test_units_and_product():
    assert unit_kind("%") == "share" and unit_kind("mil. €") == "money" and unit_kind("obcí") == "count"
    assert to_base(25, "%") == 0.25
    assert to_base(1.5, "mil. €") == 1_500_000
    assert to_base(2, "miliárd €") == 2_000_000_000
    low, base, high = combine([(2890, 2890), (300, 1200)])
    assert (low, high) == (2890 * 300, 2890 * 1200)
    assert low < base < high
