# allegro-buypack-autoresponder

Jedna automatyczna odpowiedź na pierwszą wiadomość kupującego w nowym wątku Allegro
("Dzień dobry, dziękujemy za wiadomość..."). Nigdy więcej nic automatycznego w tym
samym wątku. Wyłącznie do spełnienia zasad pierwszego kontaktu, nie do obsługi
merytorycznej.

**Osobna aplikacja Allegro, osobny token.** Celowo NIE współdzieli `.env` z głównym
projektem `allegro-buypack` — dwa niezależne procesy odświeżające ten sam refresh
token unieważniają sobie nawzajem autoryzację (udokumentowane w CLAUDE.md głównego
projektu). Zarejestruj w https://apps.developer.allegro.pl/ osobną aplikację typu
"device", z jednym uprawnieniem: `allegro:api:messaging`.

## Dlaczego odpytywanie, nie webhook

Allegro nie udostępnia webhooków dla wiadomości (potwierdzone przez support:
allegro/allegro-api#11049). Usługa odpytuje `/messaging/threads` co 3 minuty.

## Instalacja lokalna

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
cp .env.example .env
# wklej ALLEGRO_CLIENT_ID / ALLEGRO_CLIENT_SECRET z Developer Portal
.venv/bin/python auth.py     # jednorazowa autoryzacja device flow
.venv/bin/python main.py     # start pętli
```

Przy pierwszym starcie usługa **nie wysyła nic** — oznacza wszystkie istniejące
wątki jako już obsłużone (`state/state.json`) i reaguje wyłącznie na wątki
powstałe po tym momencie.

## Docker / Dokploy

```bash
docker build -t allegro-buypack-autoresponder .
docker run -d --name allegro-buypack-autoresponder \
  --restart unless-stopped \
  --env-file .env \
  -v allegro-autoresponder-state:/app/state \
  allegro-buypack-autoresponder
```

`state/` musi siedzieć na trwałym wolumenie — bez tego każdy restart kontenera
zresetowałby pamięć "co już obsłużone" i mogłoby dojść do powtórnego seedu
(nieszkodliwe: seed nigdy nie wysyła, tylko na nowo oznacza obecne wątki jako
obsłużone — ale traci historię tego, co bot faktycznie zdążył wysłać).

## Zmiana treści odpowiedzi

Stała `CANNED_REPLY` w `main.py`. Zmiana wymaga restartu kontenera.
