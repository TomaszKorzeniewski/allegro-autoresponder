#!/usr/bin/env python3
"""Autoresponder: jedna automatyczna odpowiedź na pierwszą wiadomość w nowym wątku.

Allegro nie udostępnia webhooków dla wiadomości (potwierdzone: allegro/allegro-api#11049),
więc jedyna droga to cykliczne odpytywanie /messaging/threads. Odpowiada dokładnie raz na
wątek i nigdy więcej — stan trzymany w state.json (wolumen Dockera, przetrwa restart).

Watki istniejące w chwili pierwszego uruchomienia są przy starcie oznaczane jako już
obsłużone (seed), żeby bot nie zaczął odpowiadać na starą, częściowo już obsłużoną
historię — reaguje wyłącznie na wątki, które pojawią się po starcie usługi.
"""
import json
import logging
import time
from pathlib import Path

from allegro_client import AllegroAPIError, AllegroClient

STATE_PATH = Path(__file__).parent / "state" / "state.json"
POLL_INTERVAL_S = 180
THREADS_PAGE_LIMIT = 20  # API zwraca 422 powyżej 20
MESSAGES_CHECK_LIMIT = 20

CANNED_REPLY = (
    "Dzień dobry, dziękujemy za wiadomość. Odpowiemy tak szybko, jak to możliwe."
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("autoresponder")

client = AllegroClient()


def load_state() -> dict:
    if STATE_PATH.exists():
        return json.loads(STATE_PATH.read_text(encoding="utf-8"))
    return {"seeded": False, "handled": []}


def save_state(state: dict) -> None:
    STATE_PATH.parent.mkdir(exist_ok=True)
    STATE_PATH.write_text(
        json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8"
    )


def fetch_thread_ids() -> list[str]:
    ids = []
    offset = 0
    while True:
        r = client.get(
            "/messaging/threads",
            params={"limit": THREADS_PAGE_LIMIT, "offset": offset},
        )
        threads = r.get("threads", [])
        ids += [t["id"] for t in threads]
        if len(threads) < THREADS_PAGE_LIMIT:
            break
        offset += THREADS_PAGE_LIMIT
    return ids


def seed(state: dict) -> None:
    """Pierwsze uruchomienie: istniejące wątki oznaczamy jako obsłużone, nie odpowiadamy."""
    ids = fetch_thread_ids()
    state["handled"] = ids
    state["seeded"] = True
    save_state(state)
    logger.info(
        "Seed: oznaczono %d istniejących wątków jako już obsłużone. "
        "Od teraz reaguję tylko na nowe.",
        len(ids),
    )


def is_unanswered_buyer_first_message(thread_id: str) -> bool:
    """Ostatnia wiadomość jest od kupującego I nikt (człowiek ani bot) jeszcze nie odpisał."""
    r = client.get(
        f"/messaging/threads/{thread_id}/messages",
        params={"limit": MESSAGES_CHECK_LIMIT},
    )
    messages = r.get("messages", [])
    if not messages:
        return False
    latest_is_buyer = bool(messages[0].get("author", {}).get("isInterlocutor"))
    if not latest_is_buyer:
        return False
    already_replied = any(
        not m.get("author", {}).get("isInterlocutor") for m in messages
    )
    return not already_replied


def reply(thread_id: str) -> None:
    client.post(
        f"/messaging/threads/{thread_id}/messages",
        json={"text": CANNED_REPLY, "attachments": []},
    )
    logger.info("Wysłano powitanie w wątku %s", thread_id)


def poll_once(state: dict) -> None:
    ids = fetch_thread_ids()
    new_ids = [i for i in ids if i not in state["handled"]]
    for thread_id in new_ids:
        try:
            if is_unanswered_buyer_first_message(thread_id):
                reply(thread_id)
        except AllegroAPIError as e:
            logger.error("Błąd API dla wątku %s: %s — pomijam na tym cyklu", thread_id, e)
            continue
        state["handled"].append(thread_id)
        save_state(state)


def main() -> None:
    state = load_state()
    if not state.get("seeded"):
        seed(state)
    logger.info("Start pętli, odpytywanie co %ds", POLL_INTERVAL_S)
    while True:
        try:
            poll_once(state)
        except Exception:
            logger.exception("Błąd w cyklu odpytywania, próbuję dalej za %ds", POLL_INTERVAL_S)
        time.sleep(POLL_INTERVAL_S)


if __name__ == "__main__":
    main()
