import 'dotenv/config';
import { spawn } from 'node:child_process';
import { readdir } from 'node:fs/promises';
import path from 'node:path';
import { Client, GatewayIntentBits, SlashCommandBuilder, EmbedBuilder } from 'discord.js';
import { AudioPlayerStatus, NoSubscriberBehavior, StreamType, createAudioPlayer, createAudioResource, entersState, joinVoiceChannel, VoiceConnectionStatus } from '@discordjs/voice';

const token = process.env.DISCORD_TOKEN;
if (!token) throw new Error('DISCORD_TOKEN is required');
const client = new Client({ intents: [GatewayIntentBits.Guilds, GatewayIntentBits.GuildVoiceStates] });
const sessions = new Map();
const root = path.resolve(process.env.AUDIO_LOCAL_DIR || 'storage/music');
const idleMs = Number(process.env.IDLE_TIMEOUT || 300) * 1000;
const playerFor = guild => {
  if (!sessions.has(guild.id)) {
    const session = { guild, queue: [], current: null, connection: null, audio: createAudioPlayer({ behaviors: { noSubscriber: NoSubscriberBehavior.Pause } }), volume: Number(process.env.DEFAULT_VOLUME || 0.5), loop: 'off', timer: null };
    session.audio.on(AudioPlayerStatus.Idle, () => {
      if (session.current && session.loop === 'track') session.queue.unshift(session.current);
      else if (session.current && session.loop === 'queue') session.queue.push(session.current);
      session.current = null;
      if (session.queue.length) void start(guild, session);
      else session.timer = setTimeout(() => { if (!session.queue.length && !session.current) { session.connection?.destroy(); session.connection = null; } }, idleMs);
    });
    session.audio.on('error', error => console.error(`[music] audio player error guild=${guild.id}: ${error.message}`));
    sessions.set(guild.id, session);
  }
  return sessions.get(guild.id);
};

function runYtdlp(args) {
  return new Promise((resolve, reject) => {
    const proc = spawn('yt-dlp', args, { stdio: ['ignore', 'pipe', 'pipe'] });
    let stdout = '', stderr = '';
    proc.stdout.setEncoding('utf8').on('data', chunk => stdout += chunk);
    proc.stderr.setEncoding('utf8').on('data', chunk => stderr += chunk);
    proc.on('error', reject);
    proc.on('close', code => code === 0 ? resolve(stdout.trim()) : reject(new Error(stderr.trim().slice(-800) || `yt-dlp exit ${code}`)));
  });
}
async function resolveTrack(query, requester) {
  const local = query.startsWith('local:');
  const target = local ? path.resolve(root, query.slice(6)) : query.startsWith('http') ? query : `ytsearch1:${query}`;
  if (local && !target.startsWith(root + path.sep)) throw new Error('Local files must be inside the configured music folder.');
  const json = await runYtdlp(['--no-warnings', '--dump-single-json', '--no-playlist', target]);
  const item = JSON.parse(json);
  if (item._type === 'playlist' || item.entries) return (item.entries || []).filter(Boolean).map(x => ({ id: x.id || x.url, title: x.title || 'Unknown', url: x.webpage_url || x.url, duration: x.duration, thumb: x.thumbnail, requester }));
  if (!item.title) throw new Error('The source returned no playable track.');
  return [{ id: item.id || item.webpage_url, title: item.title, url: item.webpage_url || item.url, duration: item.duration, thumb: item.thumbnail, requester }];
}
async function start(guild, session) {
  if (session.current || !session.queue.length) return;
  const track = session.queue.shift();
  session.current = track;
  let ffmpeg;
  try {
    const input = await runYtdlp(['--no-warnings', '--get-url', '--format', 'bestaudio/best', '--no-playlist', track.url]);
    const mediaUrl = input.split(/\r?\n/)[0];
    if (!/^https?:\/\//.test(mediaUrl)) throw new Error('Extractor did not return a valid HTTP audio stream.');
    ffmpeg = spawn('ffmpeg', ['-nostdin', '-hide_banner', '-loglevel', 'warning', '-i', mediaUrl, '-vn', '-c:a', 'libopus', '-application', 'audio', '-frame_duration', '20', '-ar', '48000', '-ac', '2', '-f', 'ogg', 'pipe:1'], { stdio: ['ignore', 'pipe', 'pipe'] });
    let diagnostic = '';
    ffmpeg.stderr.setEncoding('utf8').on('data', part => diagnostic += part);
    const resource = createAudioResource(ffmpeg.stdout, { inputType: StreamType.OggOpus, metadata: track });
    ffmpeg.on('error', error => console.error(`[music] ffmpeg spawn failed: ${error.message}`));
    ffmpeg.on('close', code => { if (code && session.current === track) console.error(`[music] ffmpeg exited ${code}: ${diagnostic.slice(-500)}`); });
    session.audio.play(resource);
    await entersState(session.audio, AudioPlayerStatus.Playing, 15000);
    console.log(`[music] PLAYING guild=${guild.id} title=${track.title}`);
    const channel = guild.channels.cache.find(c => c.isTextBased() && c.permissionsFor(guild.members.me)?.has('SendMessages'));
    if (channel) await channel.send({ embeds: [new EmbedBuilder().setColor(0x5865f2).setTitle('🎶 Now Playing').setDescription(`[${track.title}](${track.url})`).setThumbnail(track.thumb || null).addFields({ name: 'Requested by', value: track.requester }, { name: 'Duration', value: track.duration ? `${Math.floor(track.duration / 60)}:${String(Math.floor(track.duration % 60)).padStart(2, '0')}` : 'Live' })] }).catch(() => {});
  } catch (error) {
    console.error(`[music] playback startup failed guild=${guild.id} track=${track.title}: ${error.stack || error}`);
    ffmpeg?.kill('SIGTERM');
    session.current = null;
    await start(guild, session);
  }
}
function connect(interaction, session) {
  const channel = interaction.member.voice?.channel;
  if (!channel) throw new Error('Join a voice channel first.');
  if (!channel.permissionsFor(interaction.guild.members.me)?.has(['Connect', 'Speak'])) throw new Error('I need Connect and Speak permissions in your voice channel.');
  if (session.connection?.joinConfig.channelId !== channel.id) {
    session.connection?.destroy();
    session.connection = joinVoiceChannel({ channelId: channel.id, guildId: interaction.guildId, adapterCreator: interaction.guild.voiceAdapterCreator, selfDeaf: true, debug: true });
    session.connection.on('stateChange', (oldState, newState) => console.log(`[music] voice ${oldState.status} -> ${newState.status} guild=${interaction.guildId}`));
    session.connection.on('error', error => console.error(`[music] voice error guild=${interaction.guildId}: ${error.message}`));
    session.connection.subscribe(session.audio);
  }
  return entersState(session.connection, VoiceConnectionStatus.Ready, 45000);
}
const choices = [
  new SlashCommandBuilder().setName('play').setDescription('Search or play a URL/local track').addStringOption(o => o.setName('query').setDescription('Song, supported URL, or local:filename').setRequired(true)),
  new SlashCommandBuilder().setName('pause').setDescription('Pause playback'), new SlashCommandBuilder().setName('resume').setDescription('Resume playback'),
  new SlashCommandBuilder().setName('skip').setDescription('Skip current track'), new SlashCommandBuilder().setName('stop').setDescription('Stop and clear queue'),
  new SlashCommandBuilder().setName('leave').setDescription('Stop and leave voice'), new SlashCommandBuilder().setName('queue').setDescription('Show queued tracks'),
  new SlashCommandBuilder().setName('nowplaying').setDescription('Show current track'), new SlashCommandBuilder().setName('shuffle').setDescription('Shuffle queue'),
  new SlashCommandBuilder().setName('clear').setDescription('Clear queued tracks'), new SlashCommandBuilder().setName('volume').setDescription('Set volume 0-200').addIntegerOption(o => o.setName('percent').setDescription('Volume percent').setRequired(true).setMinValue(0).setMaxValue(200)),
  new SlashCommandBuilder().setName('loop').setDescription('Set loop mode').addStringOption(o => o.setName('mode').setDescription('Loop mode').setRequired(true).addChoices({name:'off',value:'off'},{name:'track',value:'track'},{name:'queue',value:'queue'})),
  new SlashCommandBuilder().setName('remove').setDescription('Remove queue position').addIntegerOption(o => o.setName('position').setDescription('Queue position').setRequired(true).setMinValue(1)),
  new SlashCommandBuilder().setName('musicdiag').setDescription('Inspect music and voice state'),
];
client.once('ready', async bot => {
  console.log(`[bot] logged in as ${bot.user.tag}`);
  console.log(`[music] GuildVoiceStates intent=${client.options.intents.has(GatewayIntentBits.GuildVoiceStates) ? 'enabled' : 'MISSING'}`);
  // Preserve other registered command definitions while replacing this player's commands.
  for (const guild of bot.guilds.cache.values()) {
    const existing = await guild.commands.fetch().catch(() => null);
    const musicNames = new Set(choices.map(command => command.name));
    const retained = existing ? [...existing.values()].filter(command => !musicNames.has(command.name)).map(command => command.toJSON()) : [];
    await guild.commands.set([...retained, ...choices.map(command => command.toJSON())])
      .then(() => console.log(`[commands] registered ${choices.length} music commands in ${guild.id}`))
      .catch(error => console.error(`[commands] couldn't register music commands in ${guild.id}: ${error.message}`));
  }
});
client.on('voiceStateUpdate', (oldState, newState) => {
  if (newState.id === client.user?.id) console.log(`[music] gateway voice state guild=${newState.guild.id} channel=${newState.channelId || 'left'} session=${newState.sessionId ? 'present' : 'missing'}`);
});
client.on('voiceServerUpdate', data => {
  console.log(`[music] gateway voice server guild=${data.guild.id} endpoint=${data.endpoint || 'missing'} token=${data.token ? 'present' : 'missing'}`);
});
client.on('interactionCreate', async interaction => {
  if (!interaction.isChatInputCommand()) return;
  const session = playerFor(interaction.guild);
  try {
    if (interaction.commandName === 'play') {
      await interaction.deferReply();
      await connect(interaction, session);
      const tracks = await resolveTrack(interaction.options.getString('query'), interaction.user.tag);
      session.queue.push(...tracks);
      if (session.timer) clearTimeout(session.timer);
      await interaction.editReply(`Added **${tracks.length}** track(s).`);
      void start(interaction.guild, session);
    } else if (interaction.commandName === 'musicdiag') await interaction.reply({ ephemeral: true, content: `Voice: ${session.connection?.state.status || 'disconnected'} | Audio: ${session.audio.state.status} | Current: ${session.current?.title || 'none'} | Queue: ${session.queue.length} | FFmpeg: ${await runYtdlp(['--version']).then(()=>'yt-dlp available').catch(()=>'yt-dlp missing')}` });
    else if (interaction.commandName === 'pause') { session.audio.pause(); await interaction.reply('Paused.'); }
    else if (interaction.commandName === 'resume') { session.audio.unpause(); await interaction.reply('Resumed.'); }
    else if (interaction.commandName === 'skip') { session.audio.stop(true); await interaction.reply('Skipped.'); }
    else if (interaction.commandName === 'stop') { session.queue.length = 0; session.audio.stop(true); await interaction.reply('Stopped and cleared the queue.'); }
    else if (interaction.commandName === 'leave') { session.queue.length = 0; session.audio.stop(true); session.connection?.destroy(); session.connection = null; await interaction.reply('Left voice.'); }
    else if (interaction.commandName === 'queue') await interaction.reply(session.queue.slice(0, 15).map((t,i)=>`${i+1}. ${t.title}`).join('\n') || 'Queue is empty.');
    else if (interaction.commandName === 'nowplaying') await interaction.reply(session.current?.title || 'Nothing is playing.');
    else if (interaction.commandName === 'shuffle') { session.queue.sort(() => Math.random() - 0.5); await interaction.reply('Shuffled.'); }
    else if (interaction.commandName === 'clear') { session.queue.length=0; await interaction.reply('Queue cleared.'); }
    else if (interaction.commandName === 'volume') { session.volume=interaction.options.getInteger('percent')/100; await interaction.reply(`Volume set to ${interaction.options.getInteger('percent')}%.`); }
    else if (interaction.commandName === 'loop') { session.loop=interaction.options.getString('mode'); await interaction.reply(`Loop: ${session.loop}.`); }
    else if (interaction.commandName === 'remove') { const removed=session.queue.splice(interaction.options.getInteger('position')-1,1); await interaction.reply(removed[0] ? `Removed ${removed[0].title}.` : 'No track at that position.'); }
  } catch (error) {
    console.error(`[music] /${interaction.commandName}: ${error.stack || error}`);
    let message = `❌ ${error.message || 'Music command failed.'}`;
    if (error.name === 'AbortError' || error.code === 'VOICE_CONNECTION_TIMEOUT') {
      if (session.connection?.state.status !== VoiceConnectionStatus.Ready) {
        session.connection?.destroy();
        session.connection = null;
      }
      message = '❌ Discord voice did not finish connecting. Check the bot has Connect and Speak permissions, then retry.';
    }
    if (interaction.deferred || interaction.replied) await interaction.editReply(message).catch(()=>{});
    else await interaction.reply({content:message,ephemeral:true}).catch(()=>{});
  }
});
client.on('voiceStateUpdate', (oldState, newState) => { if (newState.id === client.user?.id && !newState.channelId) { const s=sessions.get(newState.guild.id); if(s) { s.connection=null; s.audio.stop(true); s.current=null; } } });
client.on('error', error => console.error(`[discord] ${error.stack || error}`));
process.on('SIGTERM', () => { for (const s of sessions.values()) { s.audio.stop(true); s.connection?.destroy(); } client.destroy(); process.exit(0); });
client.login(token);
