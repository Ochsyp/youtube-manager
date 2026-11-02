FROM python:3.13-slim

RUN apt-get update && \
    apt-get install -y ffmpeg curl unzip gosu && \
    curl -fsSL https://deno.land/install.sh | sh && \
    mv /root/.deno/bin/deno /usr/local/bin/deno && \
    rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .

RUN pip install --no-cache-dir -r requirements.txt
RUN pip install --no-cache-dir yt-dlp
# Client half only: fetches PO tokens over HTTP from the bgutil provider at
# POT_PROVIDER_URL and mints none itself.
RUN pip install --no-cache-dir bgutil-ytdlp-pot-provider

COPY . .
RUN chmod +x entrypoint.sh

# The web UI and the worker share this image, differing only in the command the host passes
# (see compose.example.yaml); the entrypoint execs that command as PUID/PGID, not root.
ENTRYPOINT ["./entrypoint.sh"]
CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8080"]
