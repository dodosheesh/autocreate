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


def test_split_transcript_derniere_phrase_dominante_ne_vide_pas_le_clip2():
    # la 2e phrase pèse plus que la moitié des mots → sans filet, clip2 serait
    # vide (tout le speech tassé sur clip1). Le filet met la phrase longue en clip2.
    a, b = split_transcript_halves("[H] short. [F] this second sentence is clearly much much longer here.")
    assert a and b  # aucune moitié vide
    assert a.startswith("[H]") and b.startswith("[F]")
    assert "second sentence" in b


def test_tag_transcript_ecrasante_majorite_masculine_une_seule_feminine():
    # 6 phrases → 1 SEULE phrase féminine (le reste masculin), bloc contigu.
    out = tag_transcript_voices("one. two. three. four. five. six.")
    lines = out.splitlines()
    assert len(lines) == 6
    assert out.count("[F]") == 1  # une seule bascule féminine
    assert out.count("[H]") == 5  # écrasante majorité masculine
    assert lines[0].startswith("[H]")  # commence en masculin


def test_tag_transcript_long_max_deux_feminines_contigues():
    # transcript long (≥ 8 phrases) → au plus 2 féminines, CONTIGUËS (transitions min.)
    out = tag_transcript_voices(". ".join(f"s{i}" for i in range(10)) + ".")
    tags = [ln[:3] for ln in out.splitlines()]
    assert tags.count("[F]") == 2
    fem = [i for i, t in enumerate(tags) if t == "[F]"]
    assert fem[1] - fem[0] == 1  # les 2 féminines sont adjacentes (bloc contigu)


def test_tag_transcript_une_phrase_reste_masculine():
    # transcript court (1 phrase) → tout en voix masculine (aucune bascule)
    assert tag_transcript_voices("just one sentence here") == "[H] just one sentence here"


def test_tag_transcript_deux_phrases_une_seule_feminine():
    out = tag_transcript_voices("first one. second one.")
    assert out.count("[F]") == 1 and out.count("[H]") == 1


def test_tag_transcript_vide():
    assert tag_transcript_voices("") == ""


def test_count_model_voice_switches():
    from app.services.dialogue import count_model_voice_switches

    assert count_model_voice_switches("[H] a\n[F] b") == 1
    assert count_model_voice_switches("[H] a\n[F] b\n[H] c") == 2
    assert count_model_voice_switches("[H] a\n[H] b") == 0
    # [M]/[W] (autres personnes) ne comptent pas : SA voix ne change pas
    assert count_model_voice_switches("[M] q\n[F] a\n[M] q2\n[F] b") == 0


def test_tag_transcript_jamais_plus_d_une_bascule():
    # RÈGLE (tous formats) : au plus UNE bascule [H]↔[F] par vidéo.
    from app.services.dialogue import count_model_voice_switches

    for n in (2, 3, 6, 8, 10, 12):
        out = tag_transcript_voices(". ".join(f"s{i}" for i in range(n)) + ".")
        assert count_model_voice_switches(out) <= 1
    # le bloc féminin CLÔT le script → une seule transition H→F
    out = tag_transcript_voices("one. two. three. four. five. six.")
    assert out.splitlines()[-1].startswith("[F]")


def test_dialogue_banque_refuse_plus_d_une_bascule():
    from pydantic import ValidationError

    from app.api.schemas import DialogueLineCreate

    with pytest.raises(ValidationError, match="changement de voix"):
        DialogueLineCreate(category="skit", raw_text="[H] a\n[F] b\n[H] c")
    # une seule bascule → accepté
    DialogueLineCreate(category="skit", raw_text="[H] a\n[F] b")
    # l'interlocuteur [M] entre deux lignes de la model ne compte pas
    DialogueLineCreate(category="micro_trottoir", raw_text="[M] q\n[F] a\n[M] q2\n[F] b")


def test_tags_a_la_suite_meme_ligne():
    # tags « à la suite » sur une seule ligne = même résultat qu'à la ligne
    segs = parse_tagged_script("[M] tu viens d'où ? [F] de Paris [beat] [F] et toi ?")
    assert [(s.tag, s.text) for s in segs] == [
        ("M", "tu viens d'où ?"),
        ("F", "de Paris"),
        ("F", "et toi ?"),
    ]
