import pytest
from fastapi.testclient import TestClient

from whospeaks.__main__ import web_server_config
from whospeaks.config import Config, parse_networks
from whospeaks.web import client_ip, create_app

TRUSTED = parse_networks("172.18.0.0/16, 10.0.0.1")
PROXY = "172.18.0.2"
PUBLIC = "8.8.8.8"
LAN = "192.168.0.50"


# --- Résolution de l'adresse --------------------------------------------------
@pytest.mark.parametrize("peer, headers, expected", [
    # Proxy non fiable : les en-têtes sont ignorés.
    (PUBLIC, {"x-forwarded-for": LAN}, PUBLIC),
    (LAN, {"x-forwarded-for": PUBLIC}, LAN),
    (PUBLIC, {"x-real-ip": LAN}, PUBLIC),
    # Proxy de confiance.
    (PROXY, {"x-forwarded-for": PUBLIC}, PUBLIC),
    (PROXY, {"x-forwarded-for": LAN}, LAN),
    # En-tête usurpé : le client a ajouté une IP locale, le proxy a ajouté la vraie.
    (PROXY, {"x-forwarded-for": f"{LAN}, {PUBLIC}"}, PUBLIC),
    # Chaîne de proxys de confiance : on les saute.
    (PROXY, {"x-forwarded-for": f"{PUBLIC}, 172.18.0.3, 10.0.0.1"}, PUBLIC),
    (PROXY, {"x-forwarded-for": f"{LAN}, 172.18.0.3"}, LAN),
    # Toute la chaîne est de confiance : la plus à gauche.
    (PROXY, {"x-forwarded-for": "172.18.0.5, 10.0.0.1"}, "172.18.0.5"),
    # X-Real-IP seulement sans X-Forwarded-For.
    (PROXY, {"x-real-ip": PUBLIC}, PUBLIC),
    (PROXY, {"x-forwarded-for": PUBLIC, "x-real-ip": LAN}, PUBLIC),
    # Sans en-tête : le proxy lui-même.
    (PROXY, {}, PROXY),
    # Formes avec port.
    (PROXY, {"x-forwarded-for": f"{PUBLIC}:4711"}, f"{PUBLIC}:4711"),
    (PROXY, {"x-forwarded-for": f"{PUBLIC}, 172.18.0.3:8080"}, PUBLIC),
    # IPv4 mappée en IPv6 pour le proxy.
    ("::ffff:172.18.0.2", {"x-forwarded-for": PUBLIC}, PUBLIC),
    # Entrée illisible : renvoyée telle quelle (donc refusée par le filtre local).
    (PROXY, {"x-forwarded-for": "n'importe quoi"}, "n'importe quoi"),
])
def test_client_ip(peer, headers, expected):
    assert client_ip(peer, headers, TRUSTED) == expected


def test_no_trusted_proxies_ignores_headers():
    assert client_ip(PROXY, {"x-forwarded-for": PUBLIC}, []) == PROXY


def test_parse_networks():
    nets = parse_networks("10.0.0.1, 172.18.0.0/16 fd00::/8")
    assert [str(n) for n in nets] == ["10.0.0.1/32", "172.18.0.0/16", "fd00::/8"]
    assert parse_networks("") == []
    with pytest.raises(ValueError, match="TRUSTED_PROXIES"):
        parse_networks("10.0.0.300")


def test_env(monkeypatch):
    monkeypatch.setenv("TRUSTED_PROXIES", "172.18.0.0/16")
    assert [str(n) for n in Config.from_env().trusted_proxies] == ["172.18.0.0/16"]
    monkeypatch.delenv("TRUSTED_PROXIES")
    assert Config.from_env().trusted_proxies == []


def test_uvicorn_does_not_read_proxy_headers(service):
    cfg = web_server_config(create_app(service, service.config), service.config)
    assert cfg.proxy_headers is False


# --- Application complète -----------------------------------------------------
@pytest.fixture
def make_client(service):
    service.config.ui_token = ""
    service.config.ui_local_only = True

    def make(peer, trusted=TRUSTED):
        service.config.trusted_proxies = trusted
        return TestClient(create_app(service, service.config), client=(peer, 50000))
    return make


def status(client, **headers):
    return client.get("/healthz", headers=headers).status_code


def test_direct_clients(make_client):
    assert status(make_client(LAN)) == 200
    assert status(make_client(PUBLIC)) == 403
    # Un client public ne peut pas se faire passer pour local s'il n'est pas un proxy de confiance.
    assert status(make_client(PUBLIC), **{"X-Forwarded-For": LAN}) == 403
    assert status(make_client(PUBLIC), **{"X-Real-IP": LAN}) == 403


def test_behind_trusted_proxy(make_client):
    c = make_client(PROXY)
    assert status(c, **{"X-Forwarded-For": LAN}) == 200
    assert status(c, **{"X-Forwarded-For": PUBLIC}) == 403  # contournement corrigé
    assert status(c, **{"X-Forwarded-For": f"{LAN}, {PUBLIC}"}) == 403  # usurpation
    assert status(c, **{"X-Real-IP": PUBLIC}) == 403
    assert status(c, **{"X-Real-IP": LAN}) == 200
    assert status(c, **{"X-Forwarded-For": "garbage"}) == 403


def test_proxy_not_trusted_by_default(make_client):
    # Sans TRUSTED_PROXIES, c'est l'IP du proxy qui compte (comportement d'avant).
    c = make_client(PROXY, trusted=[])
    assert status(c, **{"X-Forwarded-For": PUBLIC}) == 200


def test_hsts(make_client):
    trusted = make_client(PROXY)
    r = trusted.get("/healthz", headers={"X-Forwarded-For": LAN, "X-Forwarded-Proto": "https"})
    assert r.headers["strict-transport-security"] == "max-age=31536000"
    r = trusted.get("/healthz", headers={"X-Forwarded-For": LAN, "X-Forwarded-Proto": "http"})
    assert "strict-transport-security" not in r.headers
    assert "strict-transport-security" not in trusted.get("/healthz").headers
    # X-Forwarded-Proto d'un client non fiable : ignoré.
    r = make_client(LAN).get("/healthz", headers={"X-Forwarded-Proto": "https"})
    assert "strict-transport-security" not in r.headers


def test_hsts_direct_https(service):
    service.config.ui_token = ""
    c = TestClient(create_app(service, service.config), base_url="https://whospeaks.lan", client=(LAN, 1))
    assert c.get("/healthz").headers["strict-transport-security"] == "max-age=31536000"
