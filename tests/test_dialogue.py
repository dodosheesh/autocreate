import pytest

from app.services.dialogue import (
    DialogueParseError,
    parse_tagged_script,
    render_for_prompt,
    split_script_halves,
    split_transcript_halves,
    tag_transcript_voices,
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


def test_split_script_halves_equilibre():
    # 4 répliques de longueur égale → 2 + 2 (moitié / moitié)
    a, b = split_script_halves("[F] one two\n[H] three four\n[F] five six\n[H] seven eight")
    assert a == "[F] one two\n[H] three four"
    assert b == "[F] five six\n[H] seven eight"


def test_split_script_halves_ne_coupe_pas_une_replique():
    # découpe entre répliques, jamais au milieu ; les 2 moitiés couvrent tout
    a, b = split_script_halves("[F] a b c\n[H] d\n[F] e f g h")
    assert a and b
    assert (a + "\n" + b).split() == "[F] a b c [H] d [F] e f g h".split()


def test_action_rendue_comme_action_pas_parlee():
    # [action] = ce qu'elle FAIT (jamais entre guillemets), [F] = ce qu'elle DIT.
    rendered = render_for_prompt(
        "[action] sniffs her fingers one by one\n[F] my own personal pheromone\n"
        "[action] rolls her eyes back\n[H] like a drug"
    )
    assert rendered == (
        'She sniffs her fingers one by one, '
        'then she says in a soft high-pitched feminine voice: "my own personal pheromone", '
        'then she rolls her eyes back, '
        'then she says in a deep masculine voice: "like a drug".'
    )


def test_action_ne_cree_pas_de_segment_audio():
    # [action] n'est PAS une ligne parlée → pas de segment (pas de voice-swap dessus)
    segs = parse_tagged_script("[action] winks\n[F] hi")
    assert [(s.tag, s.text) for s in segs] == [("F", "hi")]


def test_split_transcript_plusieurs_phrases_coupe_entre_phrases():
    # transcript monologue [F] de 4 phrases (mêmes longueurs) → 2 phrases / 2 phrases
    a, b = split_transcript_halves("[F] one two. three four. five six. seven eight.")
    assert a == "[F] one two.\n[F] three four."
    assert b == "[F] five six.\n[F] seven eight."


def test_split_transcript_une_seule_phrase_coupe_les_mots():
    # une seule phrase, pas de ponctuation → on coupe les MOTS en deux (sinon le
    # clip 2 serait muet). Les 2 moitiés couvrent tout, sous le même tag.
    a, b = split_transcript_halves("[F] alpha beta gamma delta")
    assert a == "[F] alpha beta"
    assert b == "[F] gamma delta"


def test_split_transcript_un_seul_mot_pas_de_deuxieme_moitie():
    a, b = split_transcript_halves("[F] hey")
    assert a == "[F] hey"
    assert b == ""


def test_split_transcript_vide():
    assert split_transcript_halves("") == ("", "")


def test_tag_transcript_majorite_masculine_occasionnelle_feminine():
    # 6 phrases → voix masculine [H] majoritaire, [F] une fois sur 3 (3e, 6e).
    out = tag_transcript_voices("one. two. three. four. five. six.")
    lines = out.splitlines()
    assert len(lines) == 6
    assert lines[0].startswith("[H]") and lines[1].startswith("[H]")
    assert lines[2].startswith("[F]")  # 3e phrase = voix féminine
    assert lines[5].startswith("[F]")  # 6e phrase = voix féminine
    assert out.count("[H]") == 4 and out.count("[F]") == 2  # majorité masculine


def test_tag_transcript_une_phrase_reste_masculine():
    # transcript court (1 phrase) → tout en voix masculine (majoritaire)
    assert tag_transcript_voices("just one sentence here") == "[H] just one sentence here"


def test_tag_transcript_vide():
    assert tag_transcript_voices("") == ""


def test_tags_a_la_suite_meme_ligne():
    # tags « à la suite » sur une seule ligne = même résultat qu'à la ligne
    segs = parse_tagged_script("[M] tu viens d'où ? [F] de Paris [beat] [F] et toi ?")
    assert [(s.tag, s.text) for s in segs] == [
        ("M", "tu viens d'où ?"),
        ("F", "de Paris"),
        ("F", "et toi ?"),
    ]
