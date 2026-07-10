import time

from app.services.security import (
    hash_password,
    issue_session,
    read_session,
    verify_password,
)


def test_hash_verifie_le_bon_mot_de_passe():
    h = hash_password("s3cret-pass")
    assert verify_password("s3cret-pass", h)
    assert not verify_password("mauvais", h)


def test_hash_sale_deux_hash_differents():
    assert hash_password("x") != hash_password("x")  # salt aléatoire


def test_verify_hash_corrompu_renvoie_false():
    assert not verify_password("x", "pas-un-hash")
    assert not verify_password("x", "")


def test_session_roundtrip():
    token = issue_session("user-123")
    assert read_session(token) == "user-123"


def test_session_signature_invalide():
    token = issue_session("user-123")
    payload, _sig = token.rsplit(".", 1)
    assert read_session(payload + ".falsifie") is None


def test_session_expiree():
    past = int(time.time()) - 10_000_000
    token = issue_session("user-123", now=past)
    assert read_session(token) is None


def test_session_malformee():
    assert read_session("") is None
    assert read_session("pas-de-point") is None
