"""OAuth 2.0 Device Flow dla Allegro API (PRODUKCJA).

Uruchom bezpośrednio, żeby się autoryzować:
    python auth.py

Moduł udostępnia też:
    get_valid_token()      - zwraca ważny access token (odświeża, gdy trzeba)
    refresh_access_token() - odświeża token, bezpiecznie wobec innych procesów

Dlaczego to jest bardziej skomplikowane, niż wygląda
----------------------------------------------------
Refresh token Allegro jest JEDNORAZOWY: użycie go zwraca nową parę tokenów
i unieważnia starą. Jeśli dwa procesy (na przykład serwer MCP w Claude Code
i skrypt w Terminalu) odświeżą token w tej samej chwili, jeden dostanie 400
i konto traci autoryzację aż do ręcznego `python auth.py`.

Dlatego całe odświeżanie idzie pod blokadą pliku (`.env.lock`), a po jej
zdobyciu ponownie czytamy `.env`: jeśli w międzyczasie odświeżył go inny
proces, korzystamy z jego wyniku, zamiast palić kolejny refresh token.
"""

import base64
import fcntl
import logging
import os
import sys
import time
import webbrowser
from contextlib import contextmanager
from pathlib import Path

import requests
from dotenv import load_dotenv, set_key

logger = logging.getLogger(__name__)

ENV_PATH = Path(__file__).parent / ".env"
LOCK_PATH = Path(__file__).parent / ".env.lock"
AUTH_BASE = "https://allegro.pl/auth/oauth"
DEVICE_URL = f"{AUTH_BASE}/device"
TOKEN_URL = f"{AUTH_BASE}/token"

# Odświeżamy z wyprzedzeniem, żeby token nie wygasł w trakcie długiej operacji
# (na przykład masowej aktualizacji stanów na 114 ofertach).
MARGINES_WYGASNIECIA = 300  # sekundy
# Ile czekamy na blokadę, zanim uznamy, że drugi proces zawiesił się na dobre.
TIMEOUT_BLOKADY = 60  # sekundy

load_dotenv(ENV_PATH)


def _wartosc(klucz: str) -> str:
    """Czyta zmienną i zdejmuje apostrofy.

    `dotenv.set_key` zapisuje wartości w apostrofach, a Allegro odrzuca token
    z apostrofem błędem 401. To był realny, powtarzalny błąd na tym koncie.
    """
    return os.getenv(klucz, "").strip().strip("'\"")


def _przeladuj_env() -> None:
    """Wczytuje `.env` z dysku, nadpisując to, co siedzi w pamięci procesu."""
    load_dotenv(ENV_PATH, override=True)


@contextmanager
def _blokada():
    """Wyłączny dostęp do `.env` między procesami (flock na osobnym pliku).

    Blokujemy `.env.lock`, nie `.env`, bo `set_key` podmienia plik przez
    zapis i rename, co zerwałoby blokadę założoną na oryginale.
    """
    LOCK_PATH.touch(exist_ok=True)
    uchwyt = LOCK_PATH.open("r+")
    deadline = time.time() + TIMEOUT_BLOKADY
    try:
        while True:
            try:
                fcntl.flock(uchwyt, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.time() >= deadline:
                    raise RuntimeError(
                        f"Nie udało się zdobyć blokady {LOCK_PATH} w {TIMEOUT_BLOKADY}s. "
                        "Prawdopodobnie inny proces odświeża tokeny i się zawiesił."
                    )
                time.sleep(0.1)
        yield
    finally:
        try:
            fcntl.flock(uchwyt, fcntl.LOCK_UN)
        finally:
            uchwyt.close()


def _client_credentials() -> tuple[str, str]:
    client_id = _wartosc("ALLEGRO_CLIENT_ID")
    client_secret = _wartosc("ALLEGRO_CLIENT_SECRET")
    if not client_id or not client_secret:
        raise RuntimeError(
            "Brak ALLEGRO_CLIENT_ID lub ALLEGRO_CLIENT_SECRET w pliku .env"
        )
    return client_id, client_secret


def _basic_auth_header() -> dict:
    client_id, client_secret = _client_credentials()
    creds = base64.b64encode(f"{client_id}:{client_secret}".encode()).decode()
    return {"Authorization": f"Basic {creds}"}


def _zapisz_tokeny(tokens: dict) -> str:
    """Zapisuje nową parę tokenów wraz z momentem wygaśnięcia.

    Zwraca nowy access token. Wywoływać wyłącznie spod blokady.
    """
    access = tokens["access_token"]
    refresh = tokens["refresh_token"]
    wygasa = int(time.time()) + int(tokens.get("expires_in", 43200))

    set_key(ENV_PATH, "ALLEGRO_ACCESS_TOKEN", access)
    set_key(ENV_PATH, "ALLEGRO_REFRESH_TOKEN", refresh)
    set_key(ENV_PATH, "ALLEGRO_TOKEN_EXPIRES_AT", str(wygasa))

    os.environ["ALLEGRO_ACCESS_TOKEN"] = access
    os.environ["ALLEGRO_REFRESH_TOKEN"] = refresh
    os.environ["ALLEGRO_TOKEN_EXPIRES_AT"] = str(wygasa)
    return access


def _token_wazny() -> bool:
    """Czy zapisany access token jest jeszcze ważny (z marginesem)?

    Brak znacznika wygaśnięcia (`.env` sprzed tej zmiany) traktujemy jako
    „nie wiadomo": token idzie do użycia, a ewentualne 401 uruchomi odświeżenie.
    Dzięki temu wdrożenie tej zmiany nie pali refresh tokenu bez potrzeby.
    """
    wygasa = _wartosc("ALLEGRO_TOKEN_EXPIRES_AT")
    if not wygasa:
        return bool(_wartosc("ALLEGRO_ACCESS_TOKEN"))
    try:
        return int(wygasa) - MARGINES_WYGASNIECIA > time.time()
    except ValueError:
        return False


def device_flow_authorize() -> str:
    """Pełny device flow: pokazuje kod, czeka na autoryzację, zapisuje tokeny."""
    client_id, _ = _client_credentials()

    resp = requests.post(
        DEVICE_URL,
        headers={
            **_basic_auth_header(),
            "Content-Type": "application/x-www-form-urlencoded",
        },
        data={"client_id": client_id},
        timeout=30,
    )
    if resp.status_code != 200:
        raise RuntimeError(
            f"Nie udało się rozpocząć device flow ({resp.status_code}): {resp.text}"
        )
    data = resp.json()

    verification_uri = data.get("verification_uri_complete") or data.get(
        "verification_uri"
    )
    user_code = data.get("user_code", "")
    device_code = data["device_code"]
    interval = int(data.get("interval", 5))
    expires_in = int(data.get("expires_in", 600))

    print("\n=== Autoryzacja Allegro ===")
    print(f"Otwórz w przeglądarce: {verification_uri}")
    if user_code:
        print(f"Kod użytkownika: {user_code}")
    print(f"(kod wygasa za {expires_in // 60} min)\n")
    try:
        webbrowser.open(verification_uri)
    except Exception:
        pass

    deadline = time.time() + expires_in
    while time.time() < deadline:
        time.sleep(interval)
        token_resp = requests.post(
            TOKEN_URL,
            headers={**_basic_auth_header()},
            params={
                "grant_type": "urn:ietf:params:oauth:grant-type:device_code",
                "device_code": device_code,
            },
            timeout=30,
        )
        if token_resp.status_code == 200:
            with _blokada():
                access = _zapisz_tokeny(token_resp.json())
            print("Autoryzacja zakończona — tokeny zapisane w .env")
            return access

        error = token_resp.json().get("error", "")
        if error == "authorization_pending":
            continue
        if error == "slow_down":
            interval += 5
            continue
        raise RuntimeError(f"Autoryzacja nieudana: {error} — {token_resp.text}")

    raise RuntimeError("Kod urządzenia wygasł — uruchom autoryzację ponownie.")


def refresh_access_token(uzyty_token: str | None = None) -> str:
    """Odświeża access token, nie depcząc innym procesom po refresh tokenie.

    `uzyty_token` to access token, który właśnie dostał 401. Jeśli po zdobyciu
    blokady okaże się, że na dysku leży już inny token, znaczy to, że równoległy
    proces zdążył odświeżyć, i po prostu korzystamy z jego wyniku.
    """
    with _blokada():
        _przeladuj_env()

        biezacy = _wartosc("ALLEGRO_ACCESS_TOKEN")
        if uzyty_token and biezacy and biezacy != uzyty_token:
            logger.info("Token odświeżył równoległy proces, korzystam z jego wyniku.")
            return biezacy
        if uzyty_token is None and _token_wazny():
            return biezacy

        refresh_token = _wartosc("ALLEGRO_REFRESH_TOKEN")
        if not refresh_token:
            raise RuntimeError(
                "Brak refresh tokenu — uruchom `python auth.py`, żeby się autoryzować."
            )

        resp = requests.post(
            TOKEN_URL,
            headers={**_basic_auth_header()},
            params={"grant_type": "refresh_token", "refresh_token": refresh_token},
            timeout=30,
        )
        if resp.status_code != 200:
            raise RuntimeError(
                f"Odświeżenie tokenu nieudane ({resp.status_code}): {resp.text}. "
                "Uruchom `python auth.py`, żeby autoryzować ponownie."
            )

        access = _zapisz_tokeny(resp.json())
        logger.info("Access token odświeżony.")
        return access


def get_valid_token() -> str:
    """Zwraca ważny access token, odświeżając go zawczasu, gdy dobiega końca."""
    _przeladuj_env()
    if _token_wazny():
        return _wartosc("ALLEGRO_ACCESS_TOKEN")
    return refresh_access_token()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    try:
        device_flow_authorize()
    except RuntimeError as e:
        print(f"BŁĄD: {e}", file=sys.stderr)
        sys.exit(1)
