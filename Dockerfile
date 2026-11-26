FROM maven:3.9.9-eclipse-temurin-17 AS java-builder
WORKDIR /build
COPY java-engine/pom.xml ./pom.xml
COPY java-engine/src ./src
RUN mvn -B -DskipTests package

# Bookworm includes the OpenJDK 17 runtime package used by the music engine.
FROM python:3.12-slim-bookworm

# FFmpeg decodes audio attachments for the AI voice-command feature.
RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg ca-certificates openjdk-17-jre-headless \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .
COPY --from=java-builder /build/target/lavaplayer-engine-1.0.0.jar /app/java-engine.jar

ENV LAVAPLAYER_ENGINE_JAR=/app/java-engine.jar
ENV LAVAPLAYER_ENGINE_PORT=8765
ENV AUDIO_LOCAL_DIR=/data/music
ENV JAVA_TOOL_OPTIONS="-Xms64m -Xmx384m"

CMD ["python", "main.py"]

