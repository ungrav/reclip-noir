FROM python:3.12-slim

RUN apt-get update && \
    apt-get install -y --no-install-recommends ffmpeg curl unzip && \
    rm -rf /var/lib/apt/lists/*

# Instalar Deno (requerido por bgutil-ytdlp-pot-provider para evaluar JS)
ENV DENO_INSTALL=/usr/local
RUN curl -fsSL https://deno.land/install.sh | sh

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# Directorios para auth y descargas
RUN mkdir -p /app/auth /app/downloads

EXPOSE 8899
ENV HOST=0.0.0.0
CMD ["python", "app.py"]
