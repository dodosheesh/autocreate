import pytest

from app.services.dialogue import (
    DialogueParseError,
    parse_tagged_script,
    render_for_prompt,
)

SCRIPT = """
[H] first line in deep voice
[beat]
[F] second line in kawaii voice
"""


def test_parse_ordre_et_tags():
    segments = parse_tagged_script(SCRIPT)
    assert [(s.tag, s.text) for s in segments] == [
        ("H", "first line in deep voice"),
        ("F", "second line in kawaii voice"),
    ]


def test_beat_ne_cree_pas_de_segment():
    assert len(parse_tagged_script("[H] a\n[beat]\n[H] b")) == 2


def test_ligne_sans_tag_rejettee():
    with pytest.raises(DialogueParseError, match="sans tag"):
        parse_tagged_script("[H] ok\nligne orpheline")


def test_tag_inconnu_rejette():
    with pytest.raises(DialogueParseError, match="tag inconnu"):
        parse_tagged_script("[X] hello")


def test_tag_sans_texte_rejette():
    with pytest.raises(DialogueParseError, match="sans texte"):
        parse_tagged_script("[H]")


def test_script_vide_rejette():
    with pytest.raises(DialogueParseError, match="vide"):
        parse_tagged_script("[beat]")


def test_render_pour_prompt():
    rendered = render_for_prompt(SCRIPT)
    assert rendered == (
        'She says in a deep masculine voice: "first line in deep voice", '
        'then after a brief pause, in a soft high-pitched feminine voice: '
        '"second line in kawaii voice".'
    )


def test_render_multi_locuteurs_nomme_qui_parle():
    # micro-trottoir : un homme (intervieweur) + la model → chaque réplique
    # est attribuée à son locuteur (pas tout mis sur « she »).
    rendered = render_for_prompt("[M] where are you from?\n[F] I am from Paris")
    assert rendered == (
        'A man off-camera says in a natural male voice: "where are you from?", '
        'then she says in a soft high-pitched feminine voice: "I am from Paris".'
    )


def test_tags_m_et_w_valides():
    segs = parse_tagged_script("[M] question\n[W] another woman replies")
    assert [s.tag for s in segs] == ["M", "W"]


def test_tags_a_la_suite_meme_ligne():
    # tags « à la suite » sur une seule ligne = même résultat qu'à la ligne
    segs = parse_tagged_script("[M] tu viens d'où ? [F] de Paris [beat] [F] et toi ?")
    assert [(s.tag, s.text) for s in segs] == [
        ("M", "tu viens d'où ?"),
        ("F", "de Paris"),
        ("F", "et toi ?"),
    ]
