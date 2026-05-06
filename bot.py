import os
import asyncio
import discord
import edge_tts
import tempfile
import pytchat
import logging
import re
from discord.ext import commands
from discord import app_commands
from dotenv import load_dotenv

# Configure Logging
logging.basicConfig(
    level=logging.INFO, # Change to logging.DEBUG for more verbosity
    format='%(asctime)s - %(levelname)s - %(name)s - %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S'
)
logger = logging.getLogger('VoiceOverBot')

def extract_video_id(url_or_id):
    """Extracts the YouTube Video ID from a URL or returns the input if it's already an ID."""
    # Regex for various YouTube URL formats
    patterns = [
        r"(?:v=|\/)([0-9A-Za-z_-]{11}).*", # matches ?v=ID or /ID
        r"youtu\.be\/([0-9A-Za-z_-]{11})",  # matches youtu.be/ID
    ]
    
    for pattern in patterns:
        match = re.search(pattern, url_or_id)
        if match:
            return match.group(1)
    
    # If no match, assume it's already a raw ID (11 chars)
    return url_or_id.strip()

# Load environment variables
load_dotenv()
TOKEN = os.getenv("DISCORD_TOKEN")

# Bot Configuration
VOICE = "en-IN-PrabhatNeural"
BOT_NAMES = ["nightbot", "streamelements", "streamlabs", "moobot"]
COMMAND_PREFIXES = ("!", "/", "$", "#")

class GuildState:
    """Stores the state for a specific guild (server)."""
    def __init__(self, guild_id):
        self.guild_id = guild_id
        self.youtube_task = None
        self.tts_task = None
        self.message_queue = asyncio.Queue()
        self.current_video_id = None
        self.is_running = False
        self.starter_id = None # Track who started the session
        self.ignore_bots = True # Default to ignoring bots
        self.voice = VOICE # Default voice

    def stop(self):
        self.is_running = False
        self.starter_id = None
        if self.youtube_task:
            self.youtube_task.cancel()
        if self.tts_task:
            self.tts_task.cancel()
        # Clear the queue
        while not self.message_queue.empty():
            try:
                self.message_queue.get_nowait()
            except asyncio.QueueEmpty:
                break

class VoiceOverBot(commands.Bot):
    def __init__(self):
        intents = discord.Intents.default()
        intents.message_content = True
        intents.members = True
        intents.voice_states = True
        super().__init__(command_prefix="!", intents=intents)
        self.guild_states = {}

    def get_state(self, guild_id):
        if guild_id not in self.guild_states:
            self.guild_states[guild_id] = GuildState(guild_id)
        return self.guild_states[guild_id]

    async def setup_hook(self):
        await self.tree.sync()
        logger.info(f"Slash commands synced for {self.user}")

bot = VoiceOverBot()

async def fetch_youtube_chat(guild_id, video_id):
    """Polls YouTube Chat and feeds the message queue."""
    state = bot.get_state(guild_id)
    try:
        chat = pytchat.create(video_id=video_id)
        logger.info(f"Started listening to YouTube video: {video_id} for Guild: {guild_id}")
        
        while state.is_running and chat.is_alive():
            for c in chat.get().sync_items():
                if not state.is_running:
                    break
                
                author_name = c.author.name
                message = c.message.strip()
                logger.debug(f"New chat item: {author_name}: {message}")

                # Filter Logic from reference.py
                author_clean = author_name.replace("@", "").strip().lower()
                msg_lower = message.lower()

                # Filter Logic from reference.py
                if state.ignore_bots and any(bot_name in author_clean for bot_name in BOT_NAMES):
                    logger.debug(f"Skipping bot message from {author_name}")
                    continue
                
                if msg_lower.startswith(COMMAND_PREFIXES):
                    continue
                if "http" in msg_lower or "www." in msg_lower:
                    continue

                display_name = author_name.replace("@", "")
                full_text = f"{display_name} says {message}"
                
                logger.info(f"[READING] {full_text}")
                await state.message_queue.put(full_text)
            
            await asyncio.sleep(1)
    except Exception as e:
        logger.error(f"YouTube Chat Error in guild {guild_id}: {e}", exc_info=True)
    finally:
        logger.info(f"Stopped listening to YouTube video: {video_id} for Guild: {guild_id}")

async def tts_worker(guild_id):
    """Processes the queue and speaks messages in Discord."""
    state = bot.get_state(guild_id)
    guild = bot.get_guild(guild_id)
    
    while state.is_running:
        try:
            text = await state.message_queue.get()
            logger.debug(f"Processing message from queue: {text}")
            voice_client = guild.voice_client
            
            if not voice_client or not voice_client.is_connected():
                state.message_queue.task_done()
                continue

            # Generate TTS using the guild's selected voice
            with tempfile.NamedTemporaryFile(delete=False, suffix=".mp3") as tmp:
                temp_path = tmp.name

            communicate = edge_tts.Communicate(text, state.voice)
            await communicate.save(temp_path)

            # Wait for current audio to finish if any
            while voice_client.is_playing():
                await asyncio.sleep(0.1)

            # Play in Discord
            source = discord.FFmpegPCMAudio(executable="/usr/bin/ffmpeg", source=temp_path)
            
            def after_playing(error):
                if error:
                    logger.error(f"Playback error in guild {guild_id}: {error}")
                if os.path.exists(temp_path):
                    try:
                        os.remove(temp_path)
                        logger.debug(f"Cleaned up temp file: {temp_path}")
                    except Exception as e:
                        logger.warning(f"Failed to remove temp file {temp_path}: {e}")

            voice_client.play(source, after=after_playing)
            
            # Wait for this specific message to finish playing before moving to the next
            while voice_client.is_playing():
                await asyncio.sleep(0.1)
                
            state.message_queue.task_done()
        except asyncio.CancelledError:
            break
        except Exception as e:
            logger.error(f"TTS Worker Error in guild {guild_id}: {e}", exc_info=True)
            await asyncio.sleep(1)

@bot.event
async def on_ready():
    logger.info(f"Logged in as {bot.user} (ID: {bot.user.id})")
    logger.info("------")

@bot.event
async def on_voice_state_update(member, before, after):
    """Auto-leave if the starter leaves or the bot is left alone."""
    guild_id = member.guild.id
    state = bot.get_state(guild_id)
    voice_client = member.guild.voice_client

    if not voice_client:
        return

    # 1. If the person who started the bot leaves the channel
    if member.id == state.starter_id and before.channel == voice_client.channel and after.channel != voice_client.channel:
        logger.info(f"Starter {member.name} left the channel. Shutting down in guild {guild_id}.")
        state.stop()
        await voice_client.disconnect()
        return

    # 2. If the bot is left alone in the channel
    if voice_client.channel and len(voice_client.channel.members) == 1: # Just the bot
        logger.info(f"Bot left alone in channel. Shutting down in guild {guild_id}.")
        state.stop()
        await voice_client.disconnect()

@bot.tree.command(name="join", description="Join the voice channel you are currently in")
async def join(interaction: discord.Interaction):
    if interaction.user.voice:
        channel = interaction.user.voice.channel
        state = bot.get_state(interaction.guild_id)
        state.starter_id = interaction.user.id # Set the starter
        
        if interaction.guild.voice_client:
            await interaction.guild.voice_client.move_to(channel)
        else:
            await channel.connect()
        await interaction.response.send_message(f"Joined {channel.name}! (Session started by {interaction.user.display_name})")
    else:
        await interaction.response.send_message("You are not in a voice channel!", ephemeral=True)

@bot.tree.command(name="leave", description="Leave the voice channel and stop any active voice-over")
async def leave(interaction: discord.Interaction):
    state = bot.get_state(interaction.guild_id)
    state.stop()
    
    if interaction.guild.voice_client:
        await interaction.guild.voice_client.disconnect()
        await interaction.response.send_message("Disconnected and stopped YouTube voice-over.")
    else:
        await interaction.response.send_message("I'm not in a voice channel!", ephemeral=True)

@bot.tree.command(name="read_ytchat", description="Start voicing over a YouTube live chat")
@app_commands.describe(video_id="The Video ID or full YouTube URL")
async def read_ytchat(interaction: discord.Interaction, video_id: str):
    # Get state first
    state = bot.get_state(interaction.guild_id)
    
    # Check if bot is in a voice channel
    voice_client = interaction.guild.voice_client
    
    if not voice_client:
        # Bot is not in a channel, try to join the user
        if interaction.user.voice:
            channel = interaction.user.voice.channel
            voice_client = await channel.connect()
            state.starter_id = interaction.user.id # Set the starter
        else:
            await interaction.response.send_message("You need to be in a voice channel for me to join you!", ephemeral=True)
            return

    # Defer immediately to avoid "Unknown Interaction" (3-second timeout)
    await interaction.response.defer()

    # Parse the ID if a URL was provided
    actual_id = extract_video_id(video_id)
    
    if len(actual_id) != 11:
        await interaction.followup.send(f"Invalid YouTube Video ID or URL: `{video_id}`. Please check and try again.")
        return

    if state.is_running:
        await interaction.followup.send(f"Already running voice-over for video: {state.current_video_id}. Use `/stop_ytchat` first.")
        return

    try:
        state.is_running = True
        state.current_video_id = actual_id
        state.youtube_task = asyncio.create_task(fetch_youtube_chat(interaction.guild_id, actual_id))
        state.tts_task = asyncio.create_task(tts_worker(interaction.guild_id))

        await interaction.followup.send(f"Starting YouTube voice-over for video: `https://youtu.be/{actual_id}`")
    except Exception as e:
        state.stop()
        await interaction.followup.send(f"Failed to start: {e}")

@bot.tree.command(name="stop_ytchat", description="Stop the YouTube live chat voice-over")
async def stop_ytchat(interaction: discord.Interaction):
    state = bot.get_state(interaction.guild_id)
    if not state.is_running:
        await interaction.response.send_message("No YouTube voice-over is currently running.", ephemeral=True)
        return

    state.stop()
    await interaction.response.send_message("Stopped YouTube voice-over.")

@bot.tree.command(name="toggle_bots", description="Toggle whether to ignore or read messages from common YouTube bots")
async def toggle_bots(interaction: discord.Interaction):
    state = bot.get_state(interaction.guild_id)
    state.ignore_bots = not state.ignore_bots
    
    status = "now ignoring" if state.ignore_bots else "now reading"
    await interaction.response.send_message(f"Bot filtering updated: I am {status} YouTube bots (Nightbot, etc.).")

@bot.tree.command(name="set_voice", description="Change the voice used for text-to-speech")
@app_commands.describe(voice="Choose a voice")
@app_commands.choices(voice=[
    app_commands.Choice(name="Neerja (Indian Female)", value="en-IN-NeerjaNeural"),
    app_commands.Choice(name="Prabhat (Indian Male)", value="en-IN-PrabhatNeural"),
    app_commands.Choice(name="Jenny (US Female)", value="en-US-JennyNeural"),
    app_commands.Choice(name="Guy (US Male)", value="en-US-GuyNeural"),
    app_commands.Choice(name="Sonia (UK Female)", value="en-GB-SoniaNeural"),
    app_commands.Choice(name="Ryan (UK Male)", value="en-GB-RyanNeural"),
])
async def set_voice(interaction: discord.Interaction, voice: app_commands.Choice[str]):
    state = bot.get_state(interaction.guild_id)
    state.voice = voice.value
    await interaction.response.send_message(f"Voice updated to: **{voice.name}**")

if __name__ == "__main__":
    if not TOKEN:
        logger.critical("DISCORD_TOKEN not found in environment variables.")
    else:
        try:
            # Increase verbosity of discord logs if needed
            # logging.getLogger('discord').setLevel(logging.DEBUG)
            bot.run(TOKEN, log_handler=None)
        except KeyboardInterrupt:
            logger.info("Shutdown signal received (Ctrl+C). Cleaning up...")
        finally:
            # Ensure the bot is closed gracefully
            if not bot.is_closed():
                asyncio.run(bot.close())
            logger.info("Bot has been shut down.")
