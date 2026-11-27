package bot.persnol.musicbot;

import club.minnced.discord.jdave.interop.JDaveSessionFactory;
import com.fasterxml.jackson.databind.JsonNode;
import com.fasterxml.jackson.databind.ObjectMapper;
import com.sedmelluq.discord.lavaplayer.player.AudioPlayer;
import com.sedmelluq.discord.lavaplayer.player.AudioPlayerManager;
import com.sedmelluq.discord.lavaplayer.player.DefaultAudioPlayerManager;
import com.sedmelluq.discord.lavaplayer.player.event.AudioEventAdapter;
import com.sedmelluq.discord.lavaplayer.source.AudioSourceManagers;
import com.sedmelluq.discord.lavaplayer.track.AudioItem;
import com.sedmelluq.discord.lavaplayer.track.AudioPlaylist;
import com.sedmelluq.discord.lavaplayer.track.AudioTrack;
import com.sedmelluq.discord.lavaplayer.track.AudioTrackEndReason;
import com.sedmelluq.discord.lavaplayer.track.playback.AudioFrame;
import dev.lavalink.youtube.YoutubeAudioSourceManager;
import net.dv8tion.jda.api.JDA;
import net.dv8tion.jda.api.JDABuilder;
import net.dv8tion.jda.api.audio.AudioModuleConfig;
import net.dv8tion.jda.api.audio.AudioSendHandler;
import net.dv8tion.jda.api.entities.Guild;
import net.dv8tion.jda.api.entities.channel.middleman.AudioChannel;
import net.dv8tion.jda.api.entities.channel.concrete.TextChannel;
import net.dv8tion.jda.api.events.interaction.command.SlashCommandInteractionEvent;
import net.dv8tion.jda.api.hooks.ListenerAdapter;
import net.dv8tion.jda.api.interactions.commands.OptionType;
import net.dv8tion.jda.api.interactions.commands.build.Commands;
import net.dv8tion.jda.api.interactions.commands.build.SlashCommandData;
import net.dv8tion.jda.api.interactions.commands.build.OptionData;
import net.dv8tion.jda.api.interactions.InteractionContextType;
import net.dv8tion.jda.api.entities.Member;
import net.dv8tion.jda.api.Permission;
import net.dv8tion.jda.api.managers.AudioManager;
import net.dv8tion.jda.api.utils.MemberCachePolicy;

import java.nio.ByteBuffer;
import java.net.URI;
import java.net.URLEncoder;
import java.net.http.HttpClient;
import java.net.http.HttpRequest;
import java.net.http.HttpResponse;
import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.nio.file.Path;
import java.time.Duration;
import java.util.ArrayDeque;
import java.util.ArrayList;
import java.util.Collections;
import java.util.Deque;
import java.util.List;
import java.util.Map;
import java.util.concurrent.ConcurrentHashMap;
import java.util.concurrent.CompletableFuture;
import java.util.concurrent.TimeUnit;
import java.util.concurrent.atomic.AtomicLong;

public final class MusicBot extends ListenerAdapter {
    private static final Map<Long, MusicController> CONTROLLERS = new ConcurrentHashMap<>();
    private static final Path LOCAL_ROOT = Path.of(System.getenv().getOrDefault("AUDIO_LOCAL_DIR", "/data/music")).toAbsolutePath().normalize();
    private static final AudioPlayerManager PLAYERS = createPlayerManager();
    private static final List<SlashCommandData> COMMANDS = List.of(
        Commands.slash("play", "Play a search, URL, playlist, stream, or local file").addOption(OptionType.STRING,"query","Song name or URL",true),
        Commands.slash("pause","Pause the current track"), Commands.slash("resume","Resume playback"),
        Commands.slash("skip","Skip the current track"), Commands.slash("stop","Stop and clear the queue"),
        Commands.slash("leave","Leave the voice channel"), Commands.slash("queue","Show queued tracks"),
        Commands.slash("nowplaying","Show the track that is playing"), Commands.slash("shuffle","Shuffle the queue"),
        Commands.slash("clear","Clear the upcoming queue"),
        Commands.slash("volume","Set volume from 0 to 100").addOption(OptionType.INTEGER,"value","Volume",true),
        Commands.slash("remove","Remove a queued track by position").addOption(OptionType.INTEGER,"position","Queue position",true),
        Commands.slash("seek","Seek within the current track in seconds").addOption(OptionType.INTEGER,"seconds","Position in seconds",true),
        Commands.slash("loop","Set loop mode: off, track, or queue").addOptions(new OptionData(OptionType.STRING,"mode","Loop mode",true).addChoice("off","off").addChoice("track","track").addChoice("queue","queue")),
        Commands.slash("musicdiag","Show voice and player status")
    ).stream().map(c -> c.setContexts(InteractionContextType.GUILD)).toList();

    public static void main(String[] args) throws Exception {
        String token = System.getenv("DISCORD_TOKEN");
        if (token == null || token.isBlank()) throw new IllegalStateException("DISCORD_TOKEN is required");
        Files.createDirectories(LOCAL_ROOT);
        Runtime.getRuntime().addShutdownHook(new Thread(() -> {
            CONTROLLERS.values().forEach(MusicController::close);
            PLAYERS.shutdown();
        }, "music-shutdown"));
        JDA jda = JDABuilder.createDefault(token)
            .enableIntents(net.dv8tion.jda.api.requests.GatewayIntent.GUILD_VOICE_STATES)
            .setMemberCachePolicy(MemberCachePolicy.VOICE)
            .setAudioModuleConfig(new AudioModuleConfig().withDaveSessionFactory(new JDaveSessionFactory()))
            .addEventListeners(new MusicBot()).build();
        jda.awaitReady();
        String guildId = System.getenv("DISCORD_GUILD_ID");
        if (guildId != null && !guildId.isBlank()) {
            Guild guild = jda.getGuildById(guildId);
            if (guild == null) throw new IllegalStateException("DISCORD_GUILD_ID is not a server this bot has joined");
            guild.updateCommands().addCommands(COMMANDS).queue();
            System.out.printf("[commands] synced %d music commands to guild %s%n", COMMANDS.size(), guildId);
        } else {
            jda.updateCommands().addCommands(COMMANDS).queue();
            System.out.printf("[commands] synced %d global music commands%n", COMMANDS.size());
        }
        System.out.printf("[startup] Java music bot ready as %s; local files: %s%n", jda.getSelfUser().getName(), LOCAL_ROOT);
    }

    private static AudioPlayerManager createPlayerManager() {
        AudioPlayerManager manager = new DefaultAudioPlayerManager();
        manager.registerSourceManager(new YoutubeAudioSourceManager());
        AudioSourceManagers.registerRemoteSources(manager, com.sedmelluq.discord.lavaplayer.source.youtube.YoutubeAudioSourceManager.class);
        AudioSourceManagers.registerLocalSource(manager);
        return manager;
    }

    @Override public void onSlashCommandInteraction(SlashCommandInteractionEvent event) {
        if (event.getGuild() == null || event.getMember() == null) return;
        try {
            switch (event.getName()) {
                case "play" -> play(event);
                case "pause" -> { controller(event).player.setPaused(true); reply(event,"⏸ Paused."); }
                case "resume" -> { controller(event).player.setPaused(false); reply(event,"▶ Resumed."); }
                case "skip" -> { controller(event).skip(); reply(event,"⏭ Skipped."); }
                case "stop" -> { controller(event).stop(); reply(event,"⏹ Stopped and cleared the queue."); }
                case "leave" -> leave(event);
                case "queue" -> reply(event,controller(event).queueText());
                case "nowplaying" -> reply(event,controller(event).nowPlaying());
                case "shuffle" -> { controller(event).shuffle(); reply(event,"🔀 Queue shuffled."); }
                case "clear" -> { controller(event).queue.clear(); reply(event,"🧹 Queue cleared."); }
                case "volume" -> volume(event);
                case "remove" -> remove(event);
                case "seek" -> seek(event);
                case "loop" -> loop(event);
                case "musicdiag" -> reply(event,controller(event).diagnostic());
                default -> { }
            }
        } catch (IllegalArgumentException | IllegalStateException error) {
            reply(event,"⚠ " + error.getMessage());
        } catch (Exception error) {
            System.err.printf("[command] /%s failed guild=%s: %s: %s%n", event.getName(), event.getGuild().getId(), error.getClass().getSimpleName(), error.getMessage());
            reply(event,"❌ Music command failed: " + safeMessage(error));
        }
    }

    private static void play(SlashCommandInteractionEvent event) {
        event.deferReply().queue();
        Guild guild = event.getGuild();
        Member member = event.getMember();
        AudioChannel channel = member.getVoiceState() == null ? null : member.getVoiceState().getChannel();
        if (channel == null) { event.getHook().editOriginal("Join a voice channel first.").queue(); return; }
        if (!guild.getSelfMember().hasPermission(channel, Permission.VOICE_CONNECT, Permission.VOICE_SPEAK)) {
            event.getHook().editOriginal("I need Connect and Speak permissions in that voice channel.").queue(); return;
        }
        String query = event.getOption("query").getAsString().trim();
        if (query.isBlank()) { event.getHook().editOriginal("Enter a song name, URL, playlist, or local file.").queue(); return; }
        if (query.startsWith("local:")) {
            try {
                Path root = LOCAL_ROOT.toRealPath();
                Path file = root.resolve(query.substring(6)).normalize().toRealPath();
                if (!file.startsWith(root) || !Files.isRegularFile(file)) throw new IllegalArgumentException("Local file must exist inside " + root);
                query = file.toString();
            } catch (Exception error) { event.getHook().editOriginal("Local file unavailable: " + safeMessage(error)).queue(); return; }
        } else if (!query.startsWith("http://") && !query.startsWith("https://") && !query.startsWith("www.")) {
            query = "ytsearch:" + query;
        }
        String request = query;
        MusicController music = controller(guild);
        music.textChannel = event.getChannel().asTextChannel();
        if (request.matches("https?://open\\.spotify\\.com/(?:intl-[a-z]{2}/)?track/[A-Za-z0-9]+(?:\\?.*)?")) {
            String spotifyTrack = request;
            CompletableFuture.supplyAsync(() -> spotifySearch(spotifyTrack)).whenComplete((search, error) -> {
                if (error != null) event.getHook().editOriginal("Could not read that Spotify track: " + safeMessage(error)).queue();
                else loadResolved(event, guild, music, channel, search, "Spotify track match");
            });
            return;
        }
        if (request.matches("https?://open\\.spotify\\.com/.*")) {
            event.getHook().editOriginal("Spotify track links work without an API key. For albums or playlists, use a direct playlist URL from a supported source.").queue();
            return;
        }
        loadResolved(event, guild, music, channel, request, request.startsWith("ytsearch:") ? "YouTube search" : request.startsWith("http") ? "URL" : "local file");
    }

    private static String spotifySearch(String url) {
        try {
            HttpRequest request = HttpRequest.newBuilder(URI.create("https://open.spotify.com/oembed?url=" + URLEncoder.encode(url, StandardCharsets.UTF_8)))
                .timeout(Duration.ofSeconds(8)).header("User-Agent", "PersonalBot/1.0").GET().build();
            HttpResponse<String> response = HttpClient.newBuilder().connectTimeout(Duration.ofSeconds(5)).followRedirects(HttpClient.Redirect.NORMAL).build()
                .send(request, HttpResponse.BodyHandlers.ofString());
            if (response.statusCode() != 200) throw new IllegalArgumentException("Spotify returned HTTP " + response.statusCode());
            JsonNode metadata = new ObjectMapper().readTree(response.body());
            String title = metadata.path("title").asText("").trim();
            String artist = metadata.path("author_name").asText("").trim();
            if (title.isBlank()) throw new IllegalArgumentException("Spotify did not return track metadata");
            String search = "ytsearch:" + title + (artist.isBlank() ? "" : " " + artist);
            System.out.printf("[play] Spotify metadata resolved to source search title=%s artist=%s%n", title, artist);
            return search;
        } catch (InterruptedException error) {
            Thread.currentThread().interrupt();
            throw new IllegalStateException("Spotify metadata request was interrupted", error);
        } catch (Exception error) {
            throw new IllegalStateException(safeMessage(error), error);
        }
    }

    private static void loadResolved(SlashCommandInteractionEvent event, Guild guild, MusicController music, AudioChannel channel, String request, String source) {
        System.out.printf("[play] resolving source=%s guild=%s query=%s%n", source, guild.getId(), request.length() > 150 ? request.substring(0,150) : request);
        PLAYERS.loadItem(request, new com.sedmelluq.discord.lavaplayer.player.AudioLoadResultHandler() {
            @Override public void trackLoaded(AudioTrack track) { loaded(List.of(track)); }
            @Override public void playlistLoaded(AudioPlaylist playlist) { loaded(playlist.getTracks()); }
            @Override public void noMatches() { event.getHook().editOriginal("No playable track matched that query.").queue(); }
            @Override public void loadFailed(com.sedmelluq.discord.lavaplayer.tools.FriendlyException error) {
                System.err.printf("[load] failed guild=%s severity=%s source=%s message=%s%n", guild.getId(), error.severity, source, error.getMessage());
                event.getHook().editOriginal("Could not load that source: " + safeMessage(error)).queue();
            }
            private void loaded(List<AudioTrack> tracks) {
                if (tracks.isEmpty()) { event.getHook().editOriginal("That playlist has no playable tracks.").queue(); return; }
                try {
                    music.connect(channel);
                    int added = music.enqueue(tracks);
                    event.getHook().editOriginal("✅ Added **" + added + "** track" + (added == 1 ? "" : "s") + ".").queue();
                } catch (Exception error) {
                    System.err.printf("[play] failed after resolve guild=%s: %s%n", guild.getId(), error.getMessage());
                    event.getHook().editOriginal("Could not start playback: " + safeMessage(error)).queue();
                }
            }
        });
    }

    private static MusicController controller(SlashCommandInteractionEvent event) {
        AudioChannel requested = event.getMember().getVoiceState() == null ? null : event.getMember().getVoiceState().getChannel();
        MusicController music = controller(event.getGuild());
        if (requested != null && music.audio.isConnected() && !music.audio.getConnectedChannel().equals(requested))
            throw new IllegalStateException("Join my current voice channel to control this queue.");
        return music;
    }
    private static MusicController controller(Guild guild) { return CONTROLLERS.computeIfAbsent(guild.getIdLong(), ignored -> new MusicController(guild)); }
    private static void reply(SlashCommandInteractionEvent event, String message) { if (event.isAcknowledged()) event.getHook().editOriginal(message).queue(); else event.reply(message).queue(); }
    private static void volume(SlashCommandInteractionEvent event) { int value = event.getOption("value").getAsInt(); if (value < 0 || value > 100) throw new IllegalArgumentException("Volume must be between 0 and 100."); controller(event).player.setVolume(value); reply(event,"🔊 Volume set to " + value + "%."); }
    private static void remove(SlashCommandInteractionEvent event) { int position = event.getOption("position").getAsInt(); if (position < 1) throw new IllegalArgumentException("Queue position starts at 1."); reply(event,controller(event).remove(position)); }
    private static void seek(SlashCommandInteractionEvent event) { long seconds = event.getOption("seconds").getAsLong(); if (seconds < 0) throw new IllegalArgumentException("Position must be zero or higher."); MusicController music = controller(event); AudioTrack track = music.player.getPlayingTrack(); if (track == null) throw new IllegalStateException("Nothing is playing."); if (!track.getInfo().isStream && seconds * 1000L >= track.getDuration()) throw new IllegalArgumentException("Position is past the end of the track."); track.setPosition(seconds * 1000L); reply(event,"⏩ Seeked to " + seconds + "s."); }
    private static void loop(SlashCommandInteractionEvent event) { String mode = event.getOption("mode").getAsString(); controller(event).loop = mode; reply(event,"🔁 Loop mode: " + mode + "."); }
    private static void leave(SlashCommandInteractionEvent event) { MusicController music = controller(event); music.stop(); music.audio.closeAudioConnection(); reply(event,"👋 Left the voice channel."); }
    private static String safeMessage(Throwable error) { return error.getMessage() == null || error.getMessage().isBlank() ? error.getClass().getSimpleName() : error.getMessage(); }

    private static final class MusicController extends AudioEventAdapter {
        private final Guild guild;
        private final AudioPlayer player = PLAYERS.createPlayer();
        private final AudioManager audio;
        private final Deque<AudioTrack> queue = new ArrayDeque<>();
        private volatile TextChannel textChannel;
        private volatile AudioTrack current;
        private volatile AudioTrack announcedTrack;
        private final AtomicLong framesSent = new AtomicLong();
        private volatile String loop = "off";
        private MusicController(Guild guild) { this.guild = guild; audio = guild.getAudioManager(); player.addListener(this); audio.setSendingHandler(new PlayerSendHandler(this)); }
        private synchronized int enqueue(List<AudioTrack> tracks) {
            int added = 0;
            for (AudioTrack track : tracks) { track.setUserData(textChannel == null ? null : textChannel.getId()); if (player.getPlayingTrack() == null && current == null) current = track; else queue.addLast(track); added++; }
            if (player.getPlayingTrack() == null && current != null) player.playTrack(current);
            return added;
        }
        private void connect(AudioChannel channel) throws InterruptedException {
            if (!audio.isConnected() || !channel.equals(audio.getConnectedChannel())) {
                audio.openAudioConnection(channel);
                long deadline = System.nanoTime() + TimeUnit.SECONDS.toNanos(20);
                while (!audio.isConnected() && System.nanoTime() < deadline) Thread.sleep(100);
                if (!audio.isConnected()) throw new IllegalStateException("Discord voice connection did not become ready within 20 seconds.");
            }
            System.out.printf("[voice] connected guild=%s channel=%s status=connected dave=enabled%n", guild.getId(), channel.getId());
        }
        @Override public synchronized void onTrackStart(AudioPlayer player, AudioTrack track) {
            current = track;
            announcedTrack = null;
            System.out.printf("[playback] started guild=%s title=%s duration_ms=%d stream=%s; waiting for first Opus frame%n", guild.getId(), track.getInfo().title, track.getDuration(), track.getInfo().isStream);
        }
        @Override public synchronized void onTrackEnd(AudioPlayer player, AudioTrack track, AudioTrackEndReason reason) {
            System.out.printf("[playback] ended guild=%s title=%s reason=%s%n", guild.getId(), track.getInfo().title, reason);
            if (reason == AudioTrackEndReason.REPLACED || reason == AudioTrackEndReason.STOPPED || reason == AudioTrackEndReason.CLEANUP) return;
            if ("track".equals(loop) && reason == AudioTrackEndReason.FINISHED) { current = track.makeClone(); player.playTrack(current); return; }
            if (reason.mayStartNext) advance(track);
        }
        @Override public synchronized void onTrackException(AudioPlayer player, AudioTrack track, com.sedmelluq.discord.lavaplayer.tools.FriendlyException error) {
            System.err.printf("[playback] exception guild=%s title=%s severity=%s cause=%s%n", guild.getId(), track.getInfo().title, error.severity, safeMessage(error));
            if (textChannel != null) textChannel.sendMessage("⚠ Could not play **" + track.getInfo().title + "**. Skipping to the next track.").queue();
            advance(track);
        }
        @Override public synchronized void onTrackStuck(AudioPlayer player, AudioTrack track, long thresholdMs) {
            System.err.printf("[playback] stuck guild=%s title=%s threshold_ms=%d; skipping%n", guild.getId(), track.getInfo().title, thresholdMs);
            advance(track);
        }
        private synchronized void advance(AudioTrack ended) {
            if (ended != null && current != ended) return;
            if ("queue".equals(loop) && ended != null) queue.addLast(ended.makeClone());
            current = queue.pollFirst();
            if (current == null) { player.stopTrack(); return; }
            player.playTrack(current);
        }
        private synchronized void skip() { if (player.getPlayingTrack() == null) throw new IllegalStateException("Nothing is playing."); advance(current); }
        private synchronized void stop() { queue.clear(); loop = "off"; current = null; player.stopTrack(); }
        private synchronized void shuffle() { List<AudioTrack> shuffled = new ArrayList<>(queue); Collections.shuffle(shuffled); queue.clear(); queue.addAll(shuffled); }
        private synchronized String remove(int position) { if (position > queue.size()) throw new IllegalArgumentException("That queue position does not exist."); List<AudioTrack> entries = new ArrayList<>(queue); AudioTrack removed = entries.remove(position - 1); queue.clear(); queue.addAll(entries); return "Removed **" + removed.getInfo().title + "**."; }
        private synchronized String queueText() { if (queue.isEmpty()) return player.getPlayingTrack() == null ? "Queue is empty." : "**Now:** " + player.getPlayingTrack().getInfo().title + "\nQueue is empty."; StringBuilder out = new StringBuilder(player.getPlayingTrack() == null ? "" : "**Now:** " + player.getPlayingTrack().getInfo().title + "\n"); int i = 1; for (AudioTrack track : queue) { if (i > 15) { out.append("…and ").append(queue.size() - 15).append(" more"); break; } out.append(i++).append(". ").append(track.getInfo().title).append('\n'); } return out.toString(); }
        private String nowPlaying() { AudioTrack track = player.getPlayingTrack(); if (track == null) return "Nothing is playing."; long seconds = track.getPosition() / 1000; return "▶ **" + track.getInfo().title + "** — " + seconds + "s / " + (track.getInfo().isStream ? "live" : track.getDuration() / 1000 + "s"); }
        private String diagnostic() { AudioTrack track = player.getPlayingTrack(); return "Voice: " + (audio.isConnected() ? "connected to " + audio.getConnectedChannel().getName() : "disconnected") + " | Player: " + (player.isPaused() ? "paused" : track == null ? "idle" : "playing") + " | Queue: " + queue.size() + " | Opus frames sent: " + framesSent.get() + " | DAVE: configured"; }
        private void audioFrameSent() {
            framesSent.incrementAndGet();
            AudioTrack track = player.getPlayingTrack();
            if (track == null || announcedTrack == track) return;
            announcedTrack = track;
            String id = track.getUserData() instanceof String value ? value : null;
            TextChannel channel = id == null ? textChannel : guild.getTextChannelById(id);
            System.out.printf("[playback] first Opus frame guild=%s title=%s frame_count=%d%n", guild.getId(), track.getInfo().title, framesSent.get());
            if (channel != null) channel.sendMessage("▶ Now playing: **" + track.getInfo().title + "**" + (track.getInfo().uri == null ? "" : "\n" + track.getInfo().uri)).queue();
        }
        private void close() { player.destroy(); }
    }

    private static final class PlayerSendHandler implements AudioSendHandler {
        private final MusicController music;
        private final AudioPlayer player;
        private AudioFrame frame;
        private PlayerSendHandler(MusicController music) { this.music = music; player = music.player; }
        @Override public boolean canProvide() { frame = player.provide(); return frame != null; }
        @Override public ByteBuffer provide20MsAudio() {
            ByteBuffer audio = ByteBuffer.wrap(frame.getData());
            music.audioFrameSent();
            return audio;
        }
        @Override public boolean isOpus() { return true; }
    }
}
