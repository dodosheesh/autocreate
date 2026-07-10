"""Téléchargement HTTP durci contre le SSRF.

Toutes les URLs traitées par l'app (assets fournis par l'utilisateur,
sorties renvoyées par kie.ai via webhook) passent par ici. On refuse tout
ce qui n'est pas http/https public :
- schéma limité à http/https ;
- résolution DNS + rejet des IP privées / loopback / link-local
  (notamment 169.254.169.254, endpoint metadata cloud) ;
- redirections suivies manuellement, chaque saut re-validé (une URL externe
  ne peut pas rebondir vers un service interne) ;
- taille maximale de téléchargement.
"""

import ipaddress
import socket
from pathlib import Path
from urllib.parse import urlparse

import httpx

MAX_DOWNLOAD_BYTES = 512 * 1024 * 1024  # 512 Mo
MAX_REDIRECTS = 5


class UnsafeUrlError(ValueError):
    """URL rejetée avant toute requête réseau (SSRF probable)."""


def _resolved_ips(host: str) -> list[ipaddress._BaseAddress]:
    try:
        infos = socket.getaddrinfo(host, None)
    except socket.gaierror as exc:
        raise UnsafeUrlError(f"Résolution DNS impossible pour {host!r}") from exc
    ips = []
    for info in infos:
        addr = info[4][0]
        # Retire le scope IPv6 éventuel (fe80::1%eth0)
        ips.append(ipaddress.ip_address(addr.split("%")[0]))
    return ips


def validate_public_url(url: str) -> None:
    """Lève UnsafeUrlError si l'URL n'est pas un http(s) public."""
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https"):
        raise UnsafeUrlError(f"Schéma interdit : {parsed.scheme!r} (http/https requis)")
    if not parsed.hostname:
        raise UnsafeUrlError("Hôte manquant dans l'URL")
    for ip in _resolved_ips(parsed.hostname):
        if (
            ip.is_private
            or ip.is_loopback
            or ip.is_link_local
            or ip.is_reserved
            or ip.is_multicast
            or ip.is_unspecified
        ):
            raise UnsafeUrlError(
                f"L'hôte {parsed.hostname!r} résout vers une IP non publique ({ip})"
            )


def safe_download(url: str, dest: Path, max_bytes: int = MAX_DOWNLOAD_BYTES) -> None:
    """Télécharge `url` vers `dest` en re-validant chaque redirection.

    follow_redirects reste désactivé : on suit à la main pour re-valider
    l'hôte à chaque saut (une allowlist sur l'URL initiale ne suffit pas)."""
    current = url
    for _ in range(MAX_REDIRECTS + 1):
        validate_public_url(current)
        with httpx.stream("GET", current, timeout=300, follow_redirects=False) as resp:
            if resp.is_redirect:
                location = resp.headers.get("location")
                if not location:
                    raise UnsafeUrlError("Redirection sans en-tête Location")
                current = str(resp.url.join(location))
                continue
            resp.raise_for_status()
            written = 0
            with open(dest, "wb") as f:
                for chunk in resp.iter_bytes(1024 * 1024):
                    written += len(chunk)
                    if written > max_bytes:
                        raise UnsafeUrlError(
                            f"Téléchargement > {max_bytes} octets — interrompu"
                        )
                    f.write(chunk)
            return
    raise UnsafeUrlError(f"Trop de redirections (> {MAX_REDIRECTS})")
