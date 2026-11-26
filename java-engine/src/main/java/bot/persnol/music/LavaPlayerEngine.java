package bot.persnol.music;

import com.google.gson.Gson;
import com.google.gson.JsonArray;
import com.google.gson.JsonObject;
import com.sedmelluq.discord.lavaplayer.player.AudioPlayer;
import com.sedmelluq.discord.lavaplayer.player.AudioPlayerManager;
import com.sedmelluq.discord.lavaplayer.player.DefaultAudioPlayerManager;
import com.sedmelluq.discord.lavaplayer.source.AudioSourceManagers;
import com.sedmelluq.discord.lavaplayer.track.AudioItem;
import com.sedmelluq.discord.lavaplayer.track.AudioPlaylist;
import com.sedmelluq.discord.lavaplayer.track.AudioTrack;
import com.sedmelluq.discord.lavaplayer.track.playback.MutableAudioFrame;
import dev.lavalink.youtube.YoutubeAudioSourceManager;

import java.io.BufferedReader;
import java.io.BufferedWriter;
import java.io.IOException;
import java.io.InputStreamReader;
import java.io.OutputStreamWriter;
import java.net.InetAddress;
import java.net.ServerSocket;
import java.net.Socket;
import java.nio.ByteBuffer;
import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.nio.file.Path;
import java.util.ArrayList;
import java.util.Base64;
import java.util.List;
import java.util.Map;
import java.util.UUID;
import java.util.concurrent.ConcurrentHashMap;
import java.util.concurrent.ExecutorService;
import java.util.concurrent.Executors;
import java.util.concurrent.TimeUnit;
import java.util.concurrent.atomic.AtomicBoolean;

/** Local Lavaplayer process. It never connects to Discord; Python owns the gateway and voice session. */
public final class LavaPlayerEngine {
    private static final Gson GSON = new Gson();
    private static final byte[] OPUS_SILENCE = {(byte) 0xF8, (byte) 0xFF, (byte) 0xFE};

    private final AudioPlayerManager manager = new DefaultAudioPlayerManager();
    private final Map<Long, AudioPlayer> players = new ConcurrentHashMap<>();
    private final Map<Long, Long> startingAt = new ConcurrentHashMap<>();
    private final Map<String, AudioTrack> tracks = new ConcurrentHashMap<>();
    private final AtomicBoolean running = new AtomicBoolean(true);
    private final AtomicBoolean closed = new AtomicBoolean(false);
    private final ExecutorService clients = Executors.newCachedThreadPool(r -> {
        Thread thread = new Thread(r, "lavaplayer-ipc-client");
        thread.setDaemon(true);
        return thread;
    });
    private final Path localRoot;
    private final int port;
    private volatile ServerSocket server;

    private LavaPlayerEngine() throws IOException {
        this.port = Integer.parseInt(System.getenv().getOrDefault("LAVAPLAYER_ENGINE_PORT", "8765"));
        this.localRoot = Path.of(System.getenv().getOrDefault("AUDIO_LOCAL_DIR", "storage/music"))
                .toAbsolutePath().normalize();
        Files.createDirectories(localRoot);

        // The maintained YouTube source replaces Lavaplayer's deprecated built-in YouTube manager.
        AudioSourceManagers.registerRemoteSources(
                manager,
                com.sedmelluq.discord.lavaplayer.source.youtube.YoutubeAudioSourceManager.class);
        manager.registerSourceManager(new YoutubeAudioSourceManager());
        AudioSourceManagers.registerLocalSource(manager);
        System.err.printf("[lavaplayer] ready; local media root: %s%n", localRoot);
    }

    public static void main(String[] args) throws Exception {
        LavaPlayerEngine engine = new LavaPlayerEngine();
        Runtime.getRuntime().addShutdownHook(new Thread(engine::close, "lavaplayer-shutdown"));
        InetAddress loopback = InetAddress.getByName("127.0.0.1");
        try (ServerSocket server = new ServerSocket(engine.port, 32, loopback)) {
            engine.server = server;
            server.setReuseAddress(true);
            System.err.printf("[lavaplayer] IPC listening on 127.0.0.1:%d%n", engine.port);
            while (engine.running.get()) {
                try {
                    Socket socket = server.accept();
                    socket.setTcpNoDelay(true);
                    engine.clients.submit(() -> engine.serve(socket));
                } catch (IOException error) {
                    if (engine.running.get()) {
                        System.err.printf("[lavaplayer] IPC accept failed: %s%n", error.getMessage());
                    }
                }
            }
        } finally {
            engine.close();
        }
    }

    private void serve(Socket socket) {
        try (socket;
             BufferedReader input = new BufferedReader(new InputStreamReader(socket.getInputStream(), StandardCharsets.UTF_8));
             BufferedWriter output = new BufferedWriter(new OutputStreamWriter(socket.getOutputStream(), StandardCharsets.UTF_8))) {
            String line;
            while ((line = input.readLine()) != null) {
                String reply;
                try {
                    reply = handle(line);
                } catch (Exception error) {
                    System.err.printf("[lavaplayer] request failed: %s%n", error.getMessage());
                    reply = "ERR\t" + encode(error.getMessage() == null ? "Lavaplayer request failed." : error.getMessage());
                }
                output.write(reply);
                output.newLine();
                output.flush();
                if ("SHUTDOWN".equals(line)) {
                    break;
                }
            }
        } catch (IOException ignored) {
            // The Python side reconnects after a dropped local socket.
        }
    }

    private String handle(String line) throws Exception {
        String[] parts = line.split("\\t", -1);
        String op = parts[0];
        return switch (op) {
            case "PING" -> "OK\tLavaPlayer 2.2.7";
            case "SEARCH" -> tracks(decode(part(parts, 1)), decode(part(parts, 2)), false);
            case "RESOLVE" -> tracks(decode(part(parts, 1)), "", true);
            case "PLAY" -> play(Long.parseLong(parts[1]), parts[2], Integer.parseInt(parts[3]));
            case "STOP" -> stop(Long.parseLong(parts[1]));
            case "PAUSE" -> pause(Long.parseLong(parts[1]), Boolean.parseBoolean(parts[2]));
            case "VOLUME" -> volume(Long.parseLong(parts[1]), Integer.parseInt(parts[2]));
            case "FRAME" -> frame(Long.parseLong(parts[1]));
            case "DESTROY" -> destroy(Long.parseLong(parts[1]));
            case "STATUS" -> "OK\t" + players.size();
            case "SHUTDOWN" -> {
                running.set(false);
                if (server != null) server.close();
                yield "OK";
            }
            default -> throw new IllegalArgumentException("Unknown music-engine operation.");
        };
    }

    private String tracks(String query, String prefix, boolean direct) throws Exception {
        if (query.isBlank()) {
            throw new IllegalArgumentException("Enter a song name, link, or local path.");
        }
        String identifier;
        if (query.startsWith("local:")) {
            Path file = checkedLocalFile(query.substring("local:".length()));
            identifier = file.toString();
        } else if (query.startsWith("http://") || query.startsWith("https://")) {
            identifier = query;
        } else if (direct) {
            throw new IllegalArgumentException("Direct links must start with http://, https://, or local:.");
        } else {
            String safePrefix = prefix.matches("[a-zA-Z0-9_-]+") ? prefix : "ytsearch";
            identifier = safePrefix + ":" + query;
        }

        AudioItem item = manager.loadItemSync(identifier);
        List<AudioTrack> loaded = new ArrayList<>();
        if (item instanceof AudioTrack track) {
            loaded.add(track);
        } else if (item instanceof AudioPlaylist playlist) {
            loaded.addAll(playlist.getTracks());
        }
        JsonArray array = new JsonArray();
        int max = 300;
        for (AudioTrack track : loaded) {
            if (array.size() >= max) break;
            String id = UUID.randomUUID().toString();
            tracks.put(id, track);
            var info = track.getInfo();
            JsonObject json = new JsonObject();
            json.addProperty("encoded", id);
            json.addProperty("title", info.title == null ? "Unknown" : info.title);
            json.addProperty("author", info.author == null ? "" : info.author);
            json.addProperty("uri", info.uri == null ? "" : info.uri);
            json.addProperty("length", info.isStream ? 0 : info.length / 1000.0);
            json.addProperty("stream", info.isStream);
            array.add(json);
        }
        return "TRACKS\t" + encode(GSON.toJson(array));
    }

    private Path checkedLocalFile(String requested) throws IOException {
        Path root = localRoot.toRealPath();
        Path supplied = Path.of(requested);
        Path file = (supplied.isAbsolute() ? supplied : root.resolve(supplied)).normalize();
        Path realFile = file.toRealPath();
        if (!realFile.startsWith(root) || !Files.isRegularFile(realFile)) {
            throw new IllegalArgumentException("Local files must be regular files inside " + root);
        }
        return realFile;
    }

    private String play(long guildId, String id, int volume) {
        AudioTrack source = tracks.get(id);
        if (source == null) throw new IllegalArgumentException("That queued track expired. Search for it again.");
        AudioPlayer player = players.computeIfAbsent(guildId, ignored -> manager.createPlayer());
        player.setVolume(Math.max(0, Math.min(1000, volume)));
        startingAt.put(guildId, System.nanoTime());
        player.playTrack(source.makeClone());
        return "OK";
    }

    private String stop(long guildId) {
        startingAt.remove(guildId);
        AudioPlayer player = players.get(guildId);
        if (player != null) player.stopTrack();
        return "OK";
    }

    private String pause(long guildId, boolean paused) {
        AudioPlayer player = players.get(guildId);
        if (player != null) player.setPaused(paused);
        return "OK";
    }

    private String volume(long guildId, int value) {
        AudioPlayer player = players.get(guildId);
        if (player != null) player.setVolume(Math.max(0, Math.min(1000, value)));
        return "OK";
    }

    private String frame(long guildId) {
        AudioPlayer player = players.get(guildId);
        if (player == null) return "IDLE";
        if (player.getPlayingTrack() == null) {
            Long started = startingAt.get(guildId);
            if (started == null) return "IDLE";
            if (System.nanoTime() - started < TimeUnit.SECONDS.toNanos(15)) return "WAIT";
            startingAt.remove(guildId, started);
            System.err.printf("[lavaplayer] guild %d produced no track within 15 seconds%n", guildId);
            return "IDLE";
        }
        startingAt.remove(guildId);
        ByteBuffer buffer = ByteBuffer.allocate(4096);
        MutableAudioFrame frame = new MutableAudioFrame();
        frame.setBuffer(buffer);
        if (!player.provide(frame)) return "WAIT";
        buffer.flip();
        byte[] bytes = new byte[buffer.remaining()];
        buffer.get(bytes);
        return "FRAME\t" + Base64.getEncoder().encodeToString(bytes);
    }

    private String destroy(long guildId) {
        startingAt.remove(guildId);
        AudioPlayer player = players.remove(guildId);
        if (player != null) {
            player.destroy();
        }
        return "OK";
    }

    private static String part(String[] parts, int index) {
        if (index >= parts.length) throw new IllegalArgumentException("Missing request argument.");
        return parts[index];
    }

    private static String encode(String value) {
        return Base64.getEncoder().encodeToString(value.getBytes(StandardCharsets.UTF_8));
    }

    private static String decode(String value) {
        return new String(Base64.getDecoder().decode(value), StandardCharsets.UTF_8);
    }

    private void close() {
        running.set(false);
        if (!closed.compareAndSet(false, true)) return;
        if (server != null && !server.isClosed()) {
            try { server.close(); } catch (IOException ignored) { }
        }
        players.values().forEach(AudioPlayer::destroy);
        players.clear();
        startingAt.clear();
        manager.shutdown();
        clients.shutdown();
        try {
            clients.awaitTermination(2, TimeUnit.SECONDS);
        } catch (InterruptedException ignored) {
            Thread.currentThread().interrupt();
        }
    }
}

