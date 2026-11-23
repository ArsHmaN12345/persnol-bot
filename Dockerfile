FROM python:3.12-slim

# ffmpeg + libopus = audio engine
RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg libopus0 curl unzip ca-certificates \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .

CMD ["python", "main.py"]
