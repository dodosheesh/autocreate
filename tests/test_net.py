import pytest

from app.net import UnsafeUrlError, validate_public_url


def test_rejette_schema_non_http():
    for url in ["file:///etc/passwd", "ftp://x/y", "gopher://x", "data:text/plain,x"]:
        with pytest.raises(UnsafeUrlError):
            validate_public_url(url)


def test_rejette_loopback():
    with pytest.raises(UnsafeUrlError):
        validate_public_url("http://127.0.0.1/x")
    with pytest.raises(UnsafeUrlError):
        validate_public_url("http://localhost/x")


def test_rejette_metadata_cloud_et_prive():
    for host in ["169.254.169.254", "10.0.0.5", "192.168.1.1", "172.16.0.1"]:
        with pytest.raises(UnsafeUrlError):
            validate_public_url(f"http://{host}/latest/meta-data/")


def test_rejette_ipv6_loopback():
    with pytest.raises(UnsafeUrlError):
        validate_public_url("http://[::1]/x")


def test_accepte_ip_publique():
    # 8.8.8.8 est public ; pas de requête réseau, juste la validation
    validate_public_url("https://8.8.8.8/x")
