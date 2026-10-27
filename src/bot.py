#!/usr/bin/env python3
"""
DiscordSpeaker - PipeWire Audio Transmitter Bot
Captures audio from PipeWire null-sink ('DiscordSpeaker.monitor') and streams it to Discord Voice.
"""

import asyncio
import json
import logging
import os
import signal
import subprocess
import sys
from typing import Optional

import discord
from discord import app_commands
from discord.ext import commands
from dotenv import load_dotenv

# Set up logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("DiscordSpeaker")

# Load environment variables
load_dotenv()

TOKEN = os.getenv("DISCORD_BOT_TOKEN")
SINK_NAME = os.getenv("SINK_NAME", "DiscordSpeaker")
AUTO_CREATE_SINK = os.getenv("AUTO_CREATE_SINK", "true").lower() in ("true", "1", "yes")
COMMAND_PREFIX = os.getenv("COMMAND_PREFIX", "!")

if not TOKEN or TOKEN.startswith("YOUR_"):
    logger.error("DISCORD_BOT_TOKEN is missing or not configured in .env!")
    sys.exit(1)


# Ensure libopus is loaded
if not discord.opus.is_loaded():
    try:
        discord.opus._load_default()
        logger.info("Loaded libopus library successfully.")
    except Exception as e:
        logger.warning(f"Could not load default libopus: {e}")


def pulse_list(kind: str) -> list[dict]:
    """Read structured PulseAudio data, independent of locale and numeric sink IDs."""
    res = subprocess.run(
        ["pactl", "--format=json", "list", kind],
        capture_output=True, text=True, check=True,
    )
    return json.loads(res.stdout)


def is_sink_active(sink_name: str = SINK_NAME) -> bool:
    """Check for an exact sink name on the host audio server."""
    try:
        return any(sink["name"] == sink_name for sink in pulse_list("sinks"))
    except Exception as e:
        logger.error(f"Error checking sink status: {e}")
        return False


def ensure_sink_active(sink_name: str = SINK_NAME) -> bool:
    """Ensure the null sink is created."""
    if is_sink_active(sink_name):
        return True

    script_path = os.path.join(os.path.dirname(__file__), "audio_ctl.sh")
    if os.path.isfile(script_path):
        logger.info(f"Running '{script_path} setup' to activate audio sink...")
        res = subprocess.run([script_path, "setup"], capture_output=True, text=True)
        if res.returncode == 0 and is_sink_active(sink_name):
            logger.info(f"Successfully activated sink '{sink_name}'.")
            return True

    # Fallback to direct pactl
    try:
        subprocess.run(
            [
                "pactl",
                "load-module",
                "module-null-sink",
                f"sink_name={sink_name}",
                'sink_properties=device.description="Discord Speaker"',
            ],
            check=True,
            capture_output=True,
        )
        return is_sink_active(sink_name)
    except Exception as e:
        logger.error(f"Failed to create sink: {e}")
        return False


def get_routed_apps(sink_name: str = SINK_NAME) -> list[str]:
    """Resolve numeric sink IDs to show all apps routed to this speaker."""
    try:
        sink_ids = {sink["index"] for sink in pulse_list("sinks") if sink["name"] == sink_name}
        apps = []
        for stream in pulse_list("sink-inputs"):
            if stream["sink"] in sink_ids:
                props = stream.get("properties", {})
                name = props.get("application.name") or props.get("application.process.binary") or "Unknown app"
                if name not in apps:
                    apps.append(name)
        return apps
    except Exception as e:
        logger.error(f"Error querying sink inputs: {e}")
        return []


def route_app_stream(application: str, sink_name: str = SINK_NAME) -> tuple[bool, str]:
    """Move all active streams matching an app name or process name to the speaker."""
    target = application.strip().casefold()
    if not target:
        return False, "Provide an application name, such as spotify, firefox, or vlc."
    try:
        if not is_sink_active(sink_name) and not ensure_sink_active(sink_name):
            return False, f"Could not create audio sink '{sink_name}'. Check the host audio connection."
        matches = []
        for stream in pulse_list("sink-inputs"):
            props = stream.get("properties", {})
            names = (props.get("application.name", ""), props.get("application.process.binary", ""))
            if any(target in name.casefold() for name in names):
                matches.append(stream["index"])
        if not matches:
            return False, f"No active playback found for '{application}'. Start playback first, or route it in pavucontrol."
        moved = 0
        for stream_id in matches:
            subprocess.run(["pactl", "move-sink-input", str(stream_id), sink_name], check=True, capture_output=True)
            moved += 1
        return True, f"Routed {moved} playback stream(s) for '{application}' to Discord Speaker ({sink_name})."
    except Exception as e:
        logger.error(f"Failed to route application: {e}")
        return False, f"Could not route all streams for '{application}'. Check the bot logs and host audio server."


def route_spotify_stream(sink_name: str = SINK_NAME) -> tuple[bool, str]:
    """Convenience shortcut; audio from every routed application is supported."""
    return route_app_stream("spotify", sink_name)


class AudioBot(commands.Bot):
    def __init__(self):
        intents = discord.Intents.default()
        intents.guilds = True
        intents.voice_states = True
        intents.message_content = True

        super().__init__(
            command_prefix=COMMAND_PREFIX,
            intents=intents,
            help_command=None,
        )
        self.current_volume: float = 1.0
        self.volume_transformer: Optional[discord.PCMVolumeTransformer] = None

    async def setup_hook(self):
        # Sync slash commands on startup
        try:
            synced = await self.tree.sync()
            logger.info(f"Synced {len(synced)} application slash commands.")
        except Exception as e:
            logger.error(f"Failed to sync application commands: {e}")

    async def on_ready(self):
        logger.info(f"Bot logged in as {self.user} (ID: {self.user.id})")
        logger.info(f"Connected to {len(self.guilds)} guild(s).")

        if AUTO_CREATE_SINK:
            if ensure_sink_active(SINK_NAME):
                logger.info(f"Audio sink '{SINK_NAME}' is ready.")
            else:
                logger.warning(f"Could not verify audio sink '{SINK_NAME}'. Check PipeWire / pactl.")

        activity = discord.Activity(
            type=discord.ActivityType.listening,
            name=f"PipeWire ({SINK_NAME})",
        )
        await self.change_presence(activity=activity)

    def start_streaming(self, voice_client: discord.VoiceClient):
        """Start or restart reading audio from PipeWire into Discord voice."""
        if voice_client.is_playing():
            voice_client.stop()

        monitor_source = f"{SINK_NAME}.monitor"
        logger.info(f"Starting audio stream from '{monitor_source}'...")

        # Native 48kHz stereo 16-bit PCM input matching Discord Opus requirements
        raw_source = discord.FFmpegPCMAudio(
            monitor_source,
            before_options="-f pulse",
            options="-vn",
        )
        self.volume_transformer = discord.PCMVolumeTransformer(
            raw_source,
            volume=self.current_volume,
        )

        def after_playback(err):
            if err:
                logger.error(f"Stream playback error: {err}")
            else:
                logger.info("Stream playback stopped.")

        voice_client.play(self.volume_transformer, after=after_playback)
        logger.info(f"Audio stream active in channel: {voice_client.channel.name}")


bot = AudioBot()


@bot.tree.command(name="join", description="Join your voice channel and begin streaming audio")
@app_commands.describe(channel="Optional voice channel to join (defaults to your current channel)")
async def slash_join(
    interaction: discord.Interaction,
    channel: Optional[discord.VoiceChannel] = None,
):
    await interaction.response.defer()

    target_channel = channel
    if not target_channel:
        if interaction.user.voice and interaction.user.voice.channel:
            target_channel = interaction.user.voice.channel
        else:
            await interaction.followup.send(
                "❌ You must either be in a voice channel or specify one in the command.",
                ephemeral=True,
            )
            return

    guild = interaction.guild
    me = guild.me or await guild.fetch_member(bot.user.id)
    perms = target_channel.permissions_for(me)

    if not perms.connect:
        await interaction.followup.send(
            f"❌ I don't have permission to **Connect** to `{target_channel.name}`.\n"
            f"Please grant the bot the **Connect** permission (or appropriate role) for this channel, or test in another channel.",
            ephemeral=True,
        )
        return

    if not perms.speak:
        await interaction.followup.send(
            f"❌ I don't have permission to **Speak** in `{target_channel.name}`.\n"
            f"Please grant the bot the **Speak** permission for this channel.",
            ephemeral=True,
        )
        return

    # Check / activate sink
    if AUTO_CREATE_SINK and not is_sink_active(SINK_NAME):
        ensure_sink_active(SINK_NAME)

    voice_client: Optional[discord.VoiceClient] = guild.voice_client

    if voice_client:
        if voice_client.is_connected():
            if voice_client.channel.id != target_channel.id:
                await voice_client.move_to(target_channel)
        else:
            await voice_client.disconnect(force=True)
            voice_client = None

    if not voice_client:
        try:
            voice_client = await asyncio.wait_for(
                target_channel.connect(self_deaf=True, timeout=10.0),
                timeout=12.0,
            )
        except asyncio.TimeoutError:
            await interaction.followup.send(
                f"❌ Timed out connecting to `{target_channel.name}`.\n"
                f"Discord did not complete the voice handshake. Please verify channel permissions or try another voice channel.",
                ephemeral=True,
            )
            return
        except Exception as e:
            await interaction.followup.send(
                f"❌ Failed to connect to `{target_channel.name}`: {e}",
                ephemeral=True,
            )
            return

    if not voice_client or not voice_client.is_connected():
        await interaction.followup.send(
            f"❌ Failed to establish connection with `{target_channel.name}`.",
            ephemeral=True,
        )
        return

    bot.start_streaming(voice_client)

    # Check currently routed apps
    apps = get_routed_apps(SINK_NAME)
    apps_text = ", ".join(apps) if apps else "None detected yet"

    embed = discord.Embed(
        title="🔊 Audio Stream Active",
        description=f"Streaming audio from **{SINK_NAME}** into **{target_channel.name}**",
        color=0x5865F2,
    )
    embed.add_field(name="Connected Channel", value=target_channel.name, inline=True)
    embed.add_field(name="Volume", value=f"{int(bot.current_volume * 100)}%", inline=True)
    embed.add_field(name="Active App(s)", value=apps_text, inline=False)
    embed.set_footer(text="Route any app to Discord Speaker in pavucontrol, or use /route_app")

    await interaction.followup.send(embed=embed)


@bot.tree.command(name="leave", description="Stop streaming and leave the voice channel")
async def slash_leave(interaction: discord.Interaction):
    guild = interaction.guild
    voice_client: Optional[discord.VoiceClient] = guild.voice_client

    if not voice_client:
        await interaction.response.send_message("❌ Bot is not currently in a voice channel.", ephemeral=True)
        return

    channel_name = voice_client.channel.name
    if voice_client.is_playing():
        voice_client.stop()
    await voice_client.disconnect(force=True)

    await interaction.response.send_message(f"👋 Disconnected from **{channel_name}**.")


@bot.tree.command(name="status", description="Show stream and PipeWire audio status")
async def slash_status(interaction: discord.Interaction):
    guild = interaction.guild
    voice_client: Optional[discord.VoiceClient] = guild.voice_client

    sink_ok = is_sink_active(SINK_NAME)
    apps = get_routed_apps(SINK_NAME) if sink_ok else []
    apps_text = "\n".join([f"• {a}" for a in apps]) if apps else "None"

    embed = discord.Embed(
        title="📊 Discord Speaker Status",
        color=0x3498DB,
    )
    embed.add_field(
        name="PipeWire Sink",
        value=f"✅ `{SINK_NAME}` (Active)" if sink_ok else f"❌ `{SINK_NAME}` (Inactive)",
        inline=True,
    )
    embed.add_field(
        name="Voice Connection",
        value=f"Connected to **{voice_client.channel.name}**" if voice_client else "Disconnected",
        inline=True,
    )
    embed.add_field(
        name="Streaming",
        value="▶️ Playing" if (voice_client and voice_client.is_playing()) else "⏹️ Idle",
        inline=True,
    )
    embed.add_field(name="Volume", value=f"{int(bot.current_volume * 100)}%", inline=True)
    embed.add_field(name="Gateway Latency", value=f"{round(bot.latency * 1000)} ms", inline=True)
    embed.add_field(name="Apps Routed to Sink", value=apps_text, inline=False)

    await interaction.response.send_message(embed=embed)


@bot.tree.command(name="route_app", description="Route an active app's playback to Discord Speaker")
@app_commands.describe(application="App or process name, e.g. spotify, firefox, or vlc")
async def slash_route_app(interaction: discord.Interaction, application: str):
    await interaction.response.defer()
    success, msg = route_app_stream(application)
    await interaction.followup.send(f"{'✅' if success else '⚠️'} {msg}")


@bot.tree.command(name="route_spotify", description="Automatically move active Spotify playback to DiscordSpeaker")
async def slash_route_spotify(interaction: discord.Interaction):
    await interaction.response.defer()
    success, msg = route_spotify_stream(SINK_NAME)
    if success:
        await interaction.followup.send(f"✅ {msg}")
    else:
        await interaction.followup.send(f"⚠️ {msg}")


@bot.tree.command(name="volume", description="Set streaming output volume (1% - 200%)")
@app_commands.describe(level="Volume level percentage (e.g. 100 for default, 50 for half)")
async def slash_volume(interaction: discord.Interaction, level: app_commands.Range[int, 1, 200]):
    new_vol = level / 100.0
    bot.current_volume = new_vol
    if bot.volume_transformer:
        bot.volume_transformer.volume = new_vol
    await interaction.response.send_message(f"🔊 Bot stream volume set to **{level}%**.")


# Legacy prefix commands for convenience (!join, !leave, !status, etc.)
@bot.command(name="join")
async def prefix_join(ctx: commands.Context, *, channel: Optional[discord.VoiceChannel] = None):
    target_channel = channel
    if not target_channel:
        if ctx.author.voice and ctx.author.voice.channel:
            target_channel = ctx.author.voice.channel
        else:
            await ctx.send("❌ You must be in a voice channel or specify one.")
            return

    guild = ctx.guild
    me = guild.me or await guild.fetch_member(bot.user.id)
    perms = target_channel.permissions_for(me)

    if not perms.connect:
        await ctx.send(
            f"❌ I don't have permission to **Connect** to `{target_channel.name}`.\n"
            f"Please grant the bot the **Connect** permission for this channel."
        )
        return

    if not perms.speak:
        await ctx.send(
            f"❌ I don't have permission to **Speak** in `{target_channel.name}`.\n"
            f"Please grant the bot the **Speak** permission for this channel."
        )
        return

    if AUTO_CREATE_SINK and not is_sink_active(SINK_NAME):
        ensure_sink_active(SINK_NAME)

    voice_client = ctx.guild.voice_client
    if voice_client:
        if voice_client.is_connected():
            if voice_client.channel.id != target_channel.id:
                await voice_client.move_to(target_channel)
        else:
            await voice_client.disconnect(force=True)
            voice_client = None

    if not voice_client:
        try:
            voice_client = await asyncio.wait_for(
                target_channel.connect(self_deaf=True, timeout=10.0),
                timeout=12.0,
            )
        except asyncio.TimeoutError:
            await ctx.send(
                f"❌ Timed out connecting to `{target_channel.name}`. Discord didn't complete the voice handshake."
            )
            return
        except Exception as e:
            await ctx.send(f"❌ Failed to connect to `{target_channel.name}`: {e}")
            return

    if not voice_client or not voice_client.is_connected():
        await ctx.send(f"❌ Failed to establish connection with `{target_channel.name}`.")
        return

    bot.start_streaming(voice_client)
    await ctx.send(f"🔊 Streaming **{SINK_NAME}** in **{target_channel.name}**.")


@bot.command(name="leave")
async def prefix_leave(ctx: commands.Context):
    voice_client = ctx.guild.voice_client
    if not voice_client:
        await ctx.send("❌ Bot is not connected to a voice channel.")
        return
    if voice_client.is_playing():
        voice_client.stop()
    await voice_client.disconnect(force=True)
    await ctx.send("👋 Disconnected.")


@bot.command(name="status")
async def prefix_status(ctx: commands.Context):
    sink_ok = is_sink_active(SINK_NAME)
    vc = ctx.guild.voice_client
    status_str = f"PipeWire Sink: {'✅ Active' if sink_ok else '❌ Inactive'}\n"
    status_str += f"Voice: {'Connected to ' + vc.channel.name if vc else 'Disconnected'}\n"
    status_str += f"Volume: {int(bot.current_volume * 100)}%"
    await ctx.send(f"```{status_str}```")


@bot.command(name="route_app")
async def prefix_route_app(ctx: commands.Context, *, application: str):
    success, msg = route_app_stream(application)
    await ctx.send(f"{'✅' if success else '⚠️'} {msg}")


@bot.command(name="route")
async def prefix_route(ctx: commands.Context):
    success, msg = route_spotify_stream(SINK_NAME)
    prefix = "✅" if success else "⚠️"
    await ctx.send(f"{prefix} {msg}")


@bot.command(name="volume")
async def prefix_volume(ctx: commands.Context, level: int):
    if not 1 <= level <= 200:
        await ctx.send("❌ Volume must be between 1 and 200.")
        return
    bot.current_volume = level / 100.0
    if bot.volume_transformer:
        bot.volume_transformer.volume = bot.current_volume
    await ctx.send(f"🔊 Volume set to **{level}%**.")


def handle_shutdown(signum, frame):
    logger.info("Received termination signal. Shutting down...")
    sys.exit(0)


signal.signal(signal.SIGINT, handle_shutdown)
signal.signal(signal.SIGTERM, handle_shutdown)


if __name__ == "__main__":
    bot.run(TOKEN)
