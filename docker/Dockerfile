FROM python:3.12-slim

# ffmpeg/ffprobe: accurate audio probing (duration, codec, chapters)
RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

# Optional speech-to-text of audiobook intros (~200 MB extra):
#   docker compose -f docker-compose.build.yml build --build-arg WITH_WHISPER=1
ARG WITH_WHISPER=0
RUN if [ "$WITH_WHISPER" = "1" ]; then pip install --no-cache-dir faster-whisper; fi

COPY *.py ./
COPY templates ./templates

# /source = messy library (mount read-only), /dest = clean output,
# /config = decisions database, covers, settings (back this up!)
VOLUME ["/source", "/dest", "/config"]
EXPOSE 8765
ENV PYTHONUNBUFFERED=1 DATA_DIR=/config
CMD ["python", "app.py"]
