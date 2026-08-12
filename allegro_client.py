"""Klient HTTP do Allegro REST API (PRODUKCJA: https://api.allegro.pl).

Dokłada nagłówek Authorization, odświeża token przy 401 i ponawia zapytanie,
gdy API każe zwolnić (429) albo chwilowo padnie (5xx).

Świadome ograniczenie ponawiania
--------------------------------
Przy 5xx ponawiamy TYLKO metody idempotentne (GET, PUT, DELETE). POST i PATCH
zostawiamy w spokoju, bo Allegro potrafi zwrócić błąd po tym, jak operacja
faktycznie przeszła: ponowiony POST tworzy wtedy drugą ofertę. Na tym koncie
duplikaty już raz powstały, więc wolimy zgłosić błąd niż zdublować ofertę.
Kod 429 ponawiamy dla wszystkich metod, bo oznacza „nie przyjąłem zapytania".
"""

import logging
import time
from typing import Any, Iterator

import requests

from auth import get_valid_token, refresh_access_token

logger = logging.getLogger(__name__)

BASE_URL = "https://api.allegro.pl"
CONTENT_TYPE = "application/vnd.allegro.public.v1+json"
BETA_CONTENT_TYPE = "application/vnd.allegro.beta.v1+json"

# Kody, po których warto spróbować jeszcze raz.
KODY_PONAWIALNE = (429, 500, 502, 503, 504)
METODY_IDEMPOTENTNE = ("GET", "PUT", "DELETE", "HEAD")
MAX_PONOWIEN = 4
MAX_ODCZEKANIE = 60  # sekundy, górna granica pojedynczej przerwy

# Allegro kolejkuje zmiany statusu publikacji i odrzuca kolejne polecenie,
# dopóki poprzednie się nie przemieli. To znaczy „czekaj i ponów", nie „popsute".
BLAD_KOLEJKI = "InProgressTaskLimitReachedException"


class AllegroAPIError(Exception):
    """Błąd zwrócony przez Allegro API."""

    def __init__(self, status_code: int, message: str):
        self.status_code = status_code
        self.message = message
        super().__init__(f"Allegro API error {status_code}: {message}")

    @property
    def to_kolejka(self) -> bool:
        """Czy to jest „poprzednia zmiana statusu jeszcze trwa"?

        Taki błąd nie oznacza awarii: ofertę trzeba po prostu dopchnąć później
        (wzorzec z `activate_pending.py`).
        """
        return BLAD_KOLEJKI in self.message

    @property
    def to_konflikt_katalogu(self) -> bool:
        """Czy 422 dotyczy karty produktu w katalogu Allegro (EAN, produkt)?

        Vault mówi jasno: przy takim błędzie zatrzymujemy się i pytamy Tomka
        o decyzję, zamiast iterować kolejnymi wariantami danych.
        """
        if self.status_code != 422:
            return False
        tresc = self.message.lower()
        return any(s in tresc for s in ("catalog", "product", "ean", "gtin"))


class AllegroClient:
    def __init__(self, base_url: str = BASE_URL):
        self.base_url = base_url.rstrip("/")
        self.session = requests.Session()

    def _headers(self, token: str, accept: str = CONTENT_TYPE) -> dict:
        return {
            "Authorization": f"Bearer {token}",
            "Accept": accept,
            "Content-Type": CONTENT_TYPE,
        }

    @staticmethod
    def _ile_czekac(resp: requests.Response, proba: int) -> float:
        """Ile odczekać przed ponowieniem: najpierw to, co każe API."""
        naglowek = resp.headers.get("Retry-After", "")
        if naglowek.strip().isdigit():
            return min(int(naglowek), MAX_ODCZEKANIE)
        # Bez wskazówki: wykładniczo, 1s, 2s, 4s, 8s.
        return min(2 ** (proba - 1), MAX_ODCZEKANIE)

    def request(
        self,
        method: str,
        path: str,
        params: dict | None = None,
        json: dict | None = None,
        accept: str = CONTENT_TYPE,
        raw: bool = False,
    ):
        """Wykonuje zapytanie do API, odświeżając token i ponawiając w razie potrzeby.

        raw=True zwraca surowe bajty (na przykład etykiety PDF) zamiast JSON-a.
        """
        url = f"{self.base_url}{path}"
        token = get_valid_token()
        metoda = method.upper()

        proba = 0
        odswiezono = False
        while True:
            resp = self.session.request(
                metoda,
                url,
                headers=self._headers(token, accept),
                params=params,
                json=json,
                timeout=60,
            )

            # 401 nie zużywa puli ponowień: to jednorazowe odświeżenie tokenu.
            if resp.status_code == 401 and not odswiezono:
                logger.warning("401 z API, odświeżam token i ponawiam zapytanie.")
                token = refresh_access_token(uzyty_token=token)
                odswiezono = True
                continue

            ponawialny = resp.status_code in KODY_PONAWIALNE and (
                resp.status_code == 429 or metoda in METODY_IDEMPOTENTNE
            )
            if ponawialny and proba < MAX_PONOWIEN:
                proba += 1
                czekaj = self._ile_czekac(resp, proba)
                logger.warning(
                    "%s z API na %s %s, czekam %.0fs (próba %d/%d).",
                    resp.status_code, metoda, path, czekaj, proba, MAX_PONOWIEN,
                )
                time.sleep(czekaj)
                continue

            break

        if resp.status_code >= 400:
            try:
                errors = resp.json().get("errors", [])
                message = "; ".join(
                    e.get("userMessage") or e.get("message", "") for e in errors
                ) or resp.text
            except (ValueError, AttributeError):
                message = resp.text
            logger.error("%s %s -> %s: %s", metoda, path, resp.status_code, message)
            raise AllegroAPIError(resp.status_code, message)

        if raw:
            return resp.content
        if resp.status_code == 204 or not resp.content:
            return {"status": "ok", "http_status": resp.status_code}
        return resp.json()

    # Skróty
    def get(self, path: str, params: dict | None = None, **kwargs):
        return self.request("GET", path, params=params, **kwargs)

    def post(self, path: str, json: dict | None = None, **kwargs):
        return self.request("POST", path, json=json, **kwargs)

    def put(self, path: str, json: dict | None = None, **kwargs):
        return self.request("PUT", path, json=json, **kwargs)

    def patch(self, path: str, json: dict | None = None, **kwargs):
        return self.request("PATCH", path, json=json, **kwargs)

    def delete(self, path: str, **kwargs):
        return self.request("DELETE", path, **kwargs)

    # --- Paginacja ---------------------------------------------------------

    def paginate(
        self,
        path: str,
        klucz: str,
        params: dict | None = None,
        limit: int = 100,
        max_pozycji: int | None = None,
    ) -> Iterator[dict]:
        """Przechodzi po wszystkich stronach wyniku, oddając pozycje pojedynczo.

        `klucz` to nazwa listy w odpowiedzi (`offers`, `checkoutForms`, ...).
        Do tej pory ta sama pętla limit/offset była napisana osobno w trzech
        skryptach, za każdym razem odrobinę inaczej.
        """
        params = dict(params or {})
        offset = 0
        oddane = 0
        while True:
            # Świeży słownik na każdą stronę: współdzielony obiekt byłby
            # widoczny dla wywołującego i zmieniałby się pod nim w trakcie.
            dane = self.get(path, params={**params, "limit": limit, "offset": offset})
            pozycje = dane.get(klucz, [])
            if not pozycje:
                return
            for p in pozycje:
                yield p
                oddane += 1
                if max_pozycji is not None and oddane >= max_pozycji:
                    return
            offset += len(pozycje)
            total = dane.get("totalCount") or dane.get("count")
            if total is not None and offset >= total:
                return
            if len(pozycje) < limit:
                return

    def pobierz_wszystkie(
        self, path: str, klucz: str, params: dict | None = None, **kwargs
    ) -> list[dict]:
        """`paginate` zebrane do listy, gdy wygodniej mieć całość naraz."""
        return list(self.paginate(path, klucz, params=params, **kwargs))


def czekaj_na_kolejke(
    fn, prob: int = 16, odstep: float = 180.0, loguj=logger.info
) -> Any:
    """Ponawia operację, dopóki Allegro trzyma poprzednią zmianę statusu w kolejce.

    Wzorzec przeniesiony z `activate_pending.py`: publikacja oferty potrafi wisieć
    kilkadziesiąt minut, a każde kolejne polecenie odbija się `InProgressTask...`.
    """
    for numer in range(1, prob + 1):
        try:
            return fn()
        except AllegroAPIError as e:
            if not e.to_kolejka or numer == prob:
                raise
            loguj("Kolejka publikacji zajęta (próba %d/%d), czekam %.0fs.",
                  numer, prob, odstep)
            time.sleep(odstep)
