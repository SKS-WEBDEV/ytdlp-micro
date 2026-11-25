FROM python:3.11-slim

# ffmpeg is required for mp3/flac/wav conversion, ID3 tagging and artwork.
RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# Audio output directory. Mount a volume here so files survive restarts.
RUN mkdir -p /data
VOLUME ["/data"]

CMD ["sh", "start.sh"]
