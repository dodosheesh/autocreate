"""Gestion d'équipe (owner-only) : création/suspension/suppression de membres."""

from fastapi.testclient import TestClient

from app.db.base import Base, SessionLocal, engine
from app.db.models import User
from app.main import app
from app.services.security import hash_password

OWNER = "owner@example.com"
OWNER_PW = "owner-password-1"


def _fresh():
    Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)
    with SessionLocal() as db:
        db.add(User(email=OWNER, password_hash=hash_password(OWNER_PW),
                    role="owner", tenant_id="tnt-a"))
        db.commit()


def _login(email, pw):
    c = TestClient(app)
    r = c.post("/api/auth/login", json={"email": email, "password": pw})
    assert r.status_code == 200, r.text
    return c


def test_owner_cree_un_membre_avec_mdp_genere():
    _fresh()
    owner = _login(OWNER, OWNER_PW)
    r = owner.post("/api/admin/members", json={"email": "Team@Example.com"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["member"]["email"] == "team@example.com"  # normalisé
    assert body["member"]["role"] == "member"
    pw = body["generated_password"]
    assert pw  # mot de passe généré renvoyé une fois
    # le membre peut se connecter avec ce mot de passe
    assert _login("team@example.com", pw).get("/api/auth/me").json()["email"] == "team@example.com"


def test_membre_partage_le_tenant_de_lowner():
    _fresh()
    owner = _login(OWNER, OWNER_PW)
    mid = owner.post("/api/admin/members",
                     json={"email": "t@example.com", "password": "member-pass-1"}).json()["member"]["id"]
    with SessionLocal() as db:
        import uuid
        assert db.get(User, uuid.UUID(mid)).tenant_id == "tnt-a"


def test_membre_ne_peut_pas_gerer_lequipe():
    _fresh()
    owner = _login(OWNER, OWNER_PW)
    owner.post("/api/admin/members", json={"email": "m@example.com", "password": "member-pass-1"})
    member = _login("m@example.com", "member-pass-1")
    assert member.get("/api/admin/members").status_code == 403
    assert member.post("/api/admin/members", json={"email": "x@example.com"}).status_code == 403


def test_email_en_double_rejete():
    _fresh()
    owner = _login(OWNER, OWNER_PW)
    owner.post("/api/admin/members", json={"email": "dup@example.com", "password": "member-pass-1"})
    r = owner.post("/api/admin/members", json={"email": "dup@example.com", "password": "member-pass-2"})
    assert r.status_code == 409


def test_suspension_coupe_lacces():
    _fresh()
    owner = _login(OWNER, OWNER_PW)
    mid = owner.post("/api/admin/members",
                     json={"email": "s@example.com", "password": "member-pass-1"}).json()["member"]["id"]
    # membre connecté OK avant suspension
    member = _login("s@example.com", "member-pass-1")
    assert member.get("/api/auth/me").status_code == 200
    # suspension → sessions révoquées + login refusé
    assert owner.post(f"/api/admin/members/{mid}/deactivate").status_code == 200
    assert member.get("/api/auth/me").status_code == 401
    assert TestClient(app).post("/api/auth/login",
                                json={"email": "s@example.com", "password": "member-pass-1"}).status_code == 401
    # réactivation → login de nouveau possible
    owner.post(f"/api/admin/members/{mid}/activate")
    assert _login("s@example.com", "member-pass-1").get("/api/auth/me").status_code == 200


def test_reset_password_revoque_les_sessions():
    _fresh()
    owner = _login(OWNER, OWNER_PW)
    mid = owner.post("/api/admin/members",
                     json={"email": "r@example.com", "password": "member-pass-1"}).json()["member"]["id"]
    member = _login("r@example.com", "member-pass-1")
    r = owner.post(f"/api/admin/members/{mid}/reset-password")
    newpw = r.json()["generated_password"]
    assert newpw and newpw != "member-pass-1"
    assert member.get("/api/auth/me").status_code == 401  # ancienne session tuée
    assert _login("r@example.com", newpw).get("/api/auth/me").status_code == 200


def test_owner_ne_peut_pas_se_supprimer():
    _fresh()
    owner = _login(OWNER, OWNER_PW)
    me_id = owner.get("/api/admin/members").json()[0]["id"]
    assert owner.delete(f"/api/admin/members/{me_id}").status_code == 400
    assert owner.post(f"/api/admin/members/{me_id}/deactivate").status_code == 400


def test_suppression_membre():
    _fresh()
    owner = _login(OWNER, OWNER_PW)
    mid = owner.post("/api/admin/members",
                     json={"email": "d@example.com", "password": "member-pass-1"}).json()["member"]["id"]
    assert owner.delete(f"/api/admin/members/{mid}").status_code == 200
    emails = [m["email"] for m in owner.get("/api/admin/members").json()]
    assert "d@example.com" not in emails
