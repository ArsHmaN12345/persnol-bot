FROM ghcr.io/lavalink-devs/lavalink:4.2.2
COPY application.yml /opt/Lavalink/application.yml
# keep memory modest on small hosting plans (raise it if you have more RAM)
ENV _JAVA_OPTIONS="-Xmx512m"
# Railway's private network is IPv6: listen on "::" (docker-compose overrides this with 0.0.0.0)
ENV LAVALINK_BIND_ADDRESS="::"
EXPOSE 2333
