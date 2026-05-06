import os
import asyncio
import discord
import edge_tts
import tempfile
import pytchat
from discord.ext import commands
from discord import app_commands
from dotenv import load_dotenv

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

    def stop(self):
        self.is_running = False
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
        print(f"Slash commands synced for {self.user}")

bot = VoiceOverBot()

async def fetch_youtube_chat(guild_id, video_id):
    """Polls YouTube Chat and feeds the message queue."""
    state = bot.get_state(guild_id)
    try:
        chat = pytchat.create(video_id=video_id)
        print(f"Started listening to YouTube video: {video_id} for Guild: {guild_id}")
        
        while state.is_running and chat.is_alive():
            for c in chat.get().sync_items():
                if not state.is_running:
                    break
                
                author_name = c.author.name
                message = c.message.strip()

                # Filter Logic from reference.py
                author_clean = author_name.replace("@", "").strip().lower()
                msg_lower = message.lower()

                if any(bot_name in author_clean for bot_name in BOT_NAMES):
                    continue
                if msg_lower.startswith(COMMAND_PREFIXES):
                    continue
                if "http" in msg_lower or "www." in msg_lower:
                    continue

                display_name = author_name.replace("@", "")
                full_text = f"{display_name} says {message}"
                
                await state.message_queue.put(full_text)
            
            await asyncio.sleep(1)
    except Exception as e:
        print(f"YouTube Chat Error in guild {guild_id}: {e}")
    finally:
        print(f"Stopped listening to YouTube video: {video_id} for Guild: {guild_id}")

async def tts_worker(guild_id):
    """Processes the queue and speaks messages in Discord."""
    state = bot.get_state(guild_id)
    guild = bot.get_guild(guild_id)
    
    while state.is_running:
        try:
            text = await state.message_queue.get()
            voice_client = guild.voice_client
            
            if not voice_client or not voice_client.is_connected():
                state.message_queue.task_done()
                continue

            # Generate TTS
            with tempfile.NamedTemporaryFile(delete=False, suffix=".mp3") as tmp:
                temp_path = tmp.name

            communicate = edge_tts.Communicate(text, VOICE)
            await communicate.save(temp_path)

            # Wait for current audio to finish if any
            while voice_client.is_playing():
                await asyncio.sleep(0.1)

            # Play in Discord
            source = discord.FFmpegPCMAudio(executable="/usr/bin/ffmpeg", source=temp_path)
            
            def after_playing(error):
                if error:
                    print(f"Playback error in guild {guild_id}: {error}")
                if os.path.exists(temp_path):
                    try:
                        os.remove(temp_path)
                    except:
                        pass

            voice_client.play(source, after=after_playing)
            
            # Wait for this specific message to finish playing before moving to the next
            while voice_client.is_playing():
                await asyncio.sleep(0.1)
                
            state.message_queue.task_done()
        except asyncio.CancelledError:
            break
        except Exception as e:
            print(f"TTS Worker Error in guild {guild_id}: {e}")
            await asyncio.sleep(1)

@bot.event
async def on_ready():
    print(f"Logged in as {bot.user} (ID: {bot.user.id})")
    print("------")

@bot.tree.command(name="join", description="Join the voice channel you are currently in")
async def join(interaction: discord.Interaction):
    if interaction.user.voice:
        channel = interaction.user.voice.channel
        if interaction.guild.voice_client:
            await interaction.guild.voice_client.move_to(channel)
        else:
            await channel.connect()
        await interaction.response.send_message(f"Joined {channel.name}!")
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

@bot.tree.command(name="speak", description="Speak text in the voice channel")
@app_commands.describe(text="The text you want the bot to say")
async def speak(interaction: discord.Interaction, text: str):
    if not interaction.guild.voice_client:
        await interaction.response.send_message("I need to be in a voice channel first! Use `/join`", ephemeral=True)
        return

    await interaction.response.defer()
    
    # For manual speak, we just put it in the queue if a worker is running, 
    # otherwise we play it directly.
    state = bot.get_state(interaction.guild_id)
    if state.is_running:
        await state.message_queue.put(text)
        await interaction.followup.send(f"Added to queue: {text}")
    else:
        # One-off playback (same logic as before)
        try:
            with tempfile.NamedTemporaryFile(delete=False, suffix=".mp3") as tmp:
                temp_path = tmp.name
            communicate = edge_tts.Communicate(text, VOICE)
            await communicate.save(temp_path)
            
            voice_client = interaction.guild.voice_client
            if voice_client.is_playing():
                voice_client.stop()

            source = discord.FFmpegPCMAudio(executable="/usr/bin/ffmpeg", source=temp_path)
            def after_playing(e):
                if os.path.exists(temp_path): os.remove(temp_path)
            voice_client.play(source, after=after_playing)
            await interaction.followup.send(f"Speaking: {text}")
        except Exception as e:
            await interaction.followup.send(f"Error: {e}")

@bot.tree.command(name="youtube_start", description="Start voicing over a YouTube live chat")
@app_commands.describe(video_id="The ID of the YouTube video (e.g., SEnXZzGu4w0)")
async def youtube_start(interaction: discord.Interaction, video_id: str):
    if not interaction.guild.voice_client:
        await interaction.response.send_message("I need to be in a voice channel first! Use `/join`", ephemeral=True)
        return

    state = bot.get_state(interaction.guild_id)
    if state.is_running:
        await interaction.response.send_message(f"Already running voice-over for video: {state.current_video_id}. Use `/youtube_stop` first.", ephemeral=True)
        return

    state.is_running = True
    state.current_video_id = video_id
    state.youtube_task = asyncio.create_task(fetch_youtube_chat(interaction.guild_id, video_id))
    state.tts_task = asyncio.create_task(tts_worker(interaction.guild_id))

    await interaction.response.send_message(f"Starting YouTube voice-over for video ID: `{video_id}`")

@bot.tree.command(name="youtube_stop", description="Stop the YouTube live chat voice-over")
async def youtube_stop(interaction: discord.Interaction):
    state = bot.get_state(interaction.guild_id)
    if not state.is_running:
        await interaction.response.send_message("No YouTube voice-over is currently running.", ephemeral=True)
        return

    state.stop()
    await interaction.response.send_message("Stopped YouTube voice-over.")

if __name__ == "__main__":
    if not TOKEN:
        print("Error: DISCORD_TOKEN not found in environment variables.")
    else:
        bot.run(TOKEN)
