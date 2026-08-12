FROM python:3.12-slim

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY allegro_client.py auth.py main.py ./

# .env i state/ przychodzą z wolumenu (patrz README) — nie pakujemy ich do obrazu.
VOLUME ["/app/state"]

CMD ["python", "main.py"]
