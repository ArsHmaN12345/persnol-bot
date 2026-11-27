FROM maven:3.9.11-eclipse-temurin-25 AS build

WORKDIR /build
COPY java-bot/pom.xml /build/pom.xml
RUN mvn -q -DskipTests dependency:go-offline
COPY java-bot/src /build/src
RUN mvn -q -DskipTests package

FROM eclipse-temurin:25-jre-noble

LABEL org.opencontainers.image.source="https://github.com/ArsHmaN12345/persnol-bot"
LABEL org.opencontainers.image.description="Persnol Discord music bot using JDA and Lavaplayer"

RUN apt-get update && apt-get install -y --no-install-recommends ca-certificates \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY --from=build /build/target/personal-music-bot.jar /app/personal-music-bot.jar
ENV AUDIO_LOCAL_DIR=/data/music
ENV JAVA_TOOL_OPTIONS="--enable-native-access=ALL-UNNAMED"
CMD ["java", "-jar", "/app/personal-music-bot.jar"]

