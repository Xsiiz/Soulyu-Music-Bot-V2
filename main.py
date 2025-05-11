# -*- coding: utf-8 -*-
# main_bot_updated.py

import discord
from discord.ext import commands, tasks
import yt_dlp
import asyncio
import logging
import os

logging.basicConfig(level=logging.INFO)

# !!! สำคัญมาก: โปรดเปลี่ยน BOT_TOKEN นี้ และเก็บรักษาเป็นความลับ !!!
# BOT_TOKEN = os.environ.get('DISCORD_BOT_TOKEN') # วิธีอ่านจาก Environment Variable
BOT_TOKEN = 'MTI5MzE0NDgwMjQ4NTA3NjAzOQ.GdVIjQ.f6c5I-mw3GdQmAHHtgA86A-MJJPBeegQTwolnQ' # <--- ใส่ Token ที่ปลอดภัยของคุณที่นี่

intents = discord.Intents.default()
intents.message_content = True
intents.voice_states = True

bot = commands.Bot(command_prefix="%", intents=intents)

YDL_OPTIONS_BASE = {
    'format': 'bestaudio/best',
    'outtmpl': '%(extractor)s-%(id)s-%(title)s.%(ext)s',
    'restrictfilenames': True,
    'nocheckcertificate': True,
    'ignoreerrors': True,
    'logtostderr': False,
    'quiet': True,
    'no_warnings': True,
    'default_search': 'auto',
    'source_address': '0.0.0.0',
}

FFMPEG_OPTIONS = {
    'before_options': '-reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5',
    'options': '-vn'
}

queues = {} # {guild_id: [song_info_dict, ...]}

class MusicCog(commands.Cog):
    def __init__(self, bot_instance):
        self.bot = bot_instance
        self.bot.idle_timers = {}
        self.bot.current_song_info = {} # {guild_id: song_info_dict}
        self.bot.background_playlist_loaders = {} # {guild_id: asyncio.Task}

    async def start_idle_timer(self, ctx):
        guild_id = ctx.guild.id
        if guild_id in self.bot.idle_timers and self.bot.idle_timers[guild_id]:
            self.bot.idle_timers[guild_id].cancel()
        self.bot.idle_timers[guild_id] = self.bot.loop.create_task(self.auto_disconnect(ctx))

    async def cancel_idle_timer(self, guild_id):
        if guild_id in self.bot.idle_timers and self.bot.idle_timers[guild_id]:
            self.bot.idle_timers[guild_id].cancel()
            self.bot.idle_timers[guild_id] = None

    async def auto_disconnect(self, ctx):
        await asyncio.sleep(15)
        guild_id = ctx.guild.id
        voice_client = ctx.guild.voice_client
        if voice_client and voice_client.is_connected():
            is_playing_or_paused = voice_client.is_playing() or voice_client.is_paused()
            is_queue_empty = not (guild_id in queues and queues[guild_id])
            
            # Check if background loader is active for this guild
            is_loading = guild_id in self.bot.background_playlist_loaders and \
                         not self.bot.background_playlist_loaders[guild_id].done()

            if not is_playing_or_paused and is_queue_empty and not is_loading:
                logging.info(f"Auto-disconnecting from {ctx.guild.name} due to inactivity.")
                await voice_client.disconnect()
                await ctx.send("👋 ไม่มีเพลงเล่นหรือโหลดเข้าคิวเป็นเวลา 15 วินาที บอทขอตัวก่อนนะ!")
        if guild_id in self.bot.idle_timers:
            self.bot.idle_timers[guild_id] = None

    async def _background_load_remaining_playlist(self, ctx, playlist_entries, playlist_title):
        guild_id = ctx.guild.id
        if guild_id not in queues: # Should not happen if called after first song is added
            queues[guild_id] = []

        initial_queue_size = len(queues[guild_id])
        songs_added_count = 0

        for i, entry in enumerate(playlist_entries): # Start from the second song (index 0 of this list)
            if not entry:
                logging.warning(f"Background load: Skipping None entry in playlist '{playlist_title}'")
                continue
            
            webpage_url = entry.get('webpage_url') or entry.get('url')
            if not webpage_url:
                logging.warning(f"Background load: Skipping entry without webpage_url in '{playlist_title}' (Title: {entry.get('title', 'N/A')})")
                continue

            song_metadata = {
                'webpage_url': webpage_url,
                'title': entry.get('title', f'เพลงที่ {initial_queue_size + songs_added_count + 1} จาก {playlist_title}'),
                'thumbnail': entry.get('thumbnail'),
                'duration': entry.get('duration'), # yt-dlp with extract_flat=True might provide this
                'requester': ctx.author.mention, # Or a generic "Playlist Loader"
                'playlist_title_source': playlist_title
            }
            queues[guild_id].append(song_metadata)
            songs_added_count += 1
            # logging.info(f"Guild {guild_id}: BG loaded '{song_metadata['title']}' to queue.")
            
            # Send update periodically, e.g., every 10 songs or so, or not at all to reduce spam
            # if songs_added_count % 10 == 0:
            #    await ctx.send(f"ℹ️ เพิ่มอีก {songs_added_count} เพลงจาก '{playlist_title}' เข้าคิวแล้ว...", delete_after=10)
            
            await asyncio.sleep(0.1) # Yield control, makes it feel "one by one"

        if songs_added_count > 0:
            await ctx.send(f"✅ โหลดเพลงที่เหลืออีก {songs_added_count} เพลงจากเพลย์ลิสต์ **'{playlist_title}'** เข้าคิวเรียบร้อยแล้ว", delete_after=30)
        else:
            logging.info(f"Background load: No more songs to load for playlist '{playlist_title}' for guild {guild_id}.")
        
        if guild_id in self.bot.background_playlist_loaders:
            del self.bot.background_playlist_loaders[guild_id]


    async def play_next_song(self, ctx):
        guild_id = ctx.guild.id
        await self.cancel_idle_timer(guild_id) # Stop idle timer when a song is about to play

        if guild_id in queues and queues[guild_id]:
            voice_client = ctx.guild.voice_client
            if not (voice_client and voice_client.is_connected()):
                logging.warning(f"Voice client not connected in {ctx.guild.name}, clearing queue for this guild.")
                if guild_id in queues: queues[guild_id].clear()
                if guild_id in self.bot.current_song_info: del self.bot.current_song_info[guild_id]
                # Cancel background loader if voice client disappears
                if guild_id in self.bot.background_playlist_loaders:
                    self.bot.background_playlist_loaders[guild_id].cancel()
                    del self.bot.background_playlist_loaders[guild_id]
                return

            if voice_client.is_playing() or voice_client.is_paused():
                return # Already playing or paused, don't start another

            song_to_prepare = queues[guild_id][0] # Peek at the first song
            title_to_prepare = song_to_prepare.get('title', 'เพลงที่กำลังจะเล่น')
            webpage_url_to_fetch = song_to_prepare.get('webpage_url')

            if not webpage_url_to_fetch:
                logging.error(f"Missing webpage_url for '{title_to_prepare}' in guild {guild_id}. Skipping.")
                await ctx.send(f"😥 ข้อมูลเพลง `{title_to_prepare}` ไม่สมบูรณ์ (ไม่มี URL หน้าเว็บ) ขอข้ามเพลงนี้ค่ะ")
                queues[guild_id].pop(0) # Remove problematic song
                asyncio.create_task(self.play_next_song(ctx)) # Try next
                return

            prepare_msg = await ctx.send(f"🎧 กำลังเตรียม: [{title_to_prepare}]({webpage_url_to_fetch})...")
            
            source_url = None
            actual_song_data = None
            try:
                ydl_opts_single = YDL_OPTIONS_BASE.copy()
                ydl_opts_single['noplaylist'] = True 
                ydl_opts_single['extract_flat'] = False # We need the actual stream URL

                with yt_dlp.YoutubeDL(ydl_opts_single) as ydl:
                    track_info = await self.bot.loop.run_in_executor(
                        None,
                        lambda: ydl.extract_info(webpage_url_to_fetch, download=False)
                    )
                
                if 'entries' in track_info and track_info['entries']:
                    actual_song_data = track_info['entries'][0]
                else:
                    actual_song_data = track_info
                
                source_url = actual_song_data.get('url')
                if not source_url:
                    raise ValueError("Could not extract stream URL from track_info.")

            except Exception as e:
                logging.error(f"JIT fetch error for '{title_to_prepare}': {e}")
                await prepare_msg.edit(content=f"😥 ไม่สามารถดึง URL สตรีมสำหรับ `{title_to_prepare}` ได้: {str(e)[:1000]}. กำลังข้าม...")
                queues[guild_id].pop(0) # Remove problematic song
                await asyncio.sleep(2)
                asyncio.create_task(self.play_next_song(ctx)) # Try next
                return
            
            song_info = queues[guild_id].pop(0) # Now that fetch is successful, pop it
            
            song_info['source'] = source_url
            song_info['title'] = actual_song_data.get('title', song_info['title']) # Update with freshest title
            song_info['thumbnail'] = actual_song_data.get('thumbnail', song_info.get('thumbnail'))
            song_info['duration'] = actual_song_data.get('duration', song_info.get('duration'))

            try:
                player = discord.FFmpegPCMAudio(song_info['source'], **FFMPEG_OPTIONS)

                def after_playing_song(error):
                    if error:
                        logging.error(f"Error during playback of '{song_info['title']}': {error}")
                    
                    # Ensure bot is still in a guild and connected before trying to play next
                    if ctx.guild and ctx.guild.voice_client and ctx.guild.voice_client.is_connected():
                         asyncio.create_task(self.play_next_song(ctx))
                    else:
                        logging.info(f"Voice client no longer connected in guild {guild_id} after song '{song_info['title']}'. Not playing next.")
                        if guild_id in queues: queues[guild_id].clear()
                        if guild_id in self.bot.current_song_info: del self.bot.current_song_info[guild_id]
                        if guild_id in self.bot.background_playlist_loaders: # Ensure loader is stopped if VC dies
                            self.bot.background_playlist_loaders[guild_id].cancel()
                            del self.bot.background_playlist_loaders[guild_id]

                voice_client.play(player, after=after_playing_song)
                self.bot.current_song_info[guild_id] = song_info

                embed = discord.Embed(
                    title="🎧 กำลังเล่นเพลง",
                    description=f"[{song_info['title']}]({song_info['webpage_url']})",
                    color=discord.Color.blue()
                )
                embed.add_field(name="ขอโดย", value=song_info['requester'], inline=False)
                if song_info.get('thumbnail'):
                    embed.set_thumbnail(url=song_info['thumbnail'])
                if song_info.get('duration'):
                    m, s = divmod(song_info['duration'], 60); h, m = divmod(m, 60)
                    duration_str = f"{int(h):02d}:{int(m):02d}:{int(s):02d}" if h else f"{int(m):02d}:{int(s):02d}"
                    embed.add_field(name="ความยาว", value=duration_str, inline=True)
                
                await prepare_msg.delete()
                await ctx.send(embed=embed)

            except Exception as e:
                logging.error(f"Failed to play '{song_info['title']}': {e}")
                await ctx.send(f"😥 ไม่สามารถเล่นเพลง `{song_info['title']}` ได้: {str(e)[:1000]}")
                asyncio.create_task(self.play_next_song(ctx)) # Try next

        else: # Queue is empty
            if guild_id in self.bot.current_song_info:
                del self.bot.current_song_info[guild_id]
            
            is_loading = guild_id in self.bot.background_playlist_loaders and \
                         not self.bot.background_playlist_loaders[guild_id].done()

            if is_loading:
                await ctx.send("🎵 คิวเพลงปัจจุบันหมดแล้ว แต่กำลังโหลดเพลงเพิ่มเติมในพื้นหลัง...")
            else:
                await ctx.send("🎵 คิวเพลงหมดแล้วจ้า")

            if ctx.guild.voice_client and ctx.guild.voice_client.is_connected():
                await ctx.send(f"บอทจะออกจากช่องเสียงหากไม่มีการใช้งานใน 15 วินาที (พิมพ์ `{self.bot.command_prefix}play <ชื่อเพลง>` เพื่อเล่นต่อ)")
                await self.start_idle_timer(ctx)


    @commands.command(name="play", aliases=["p", "เล่น"], help="เล่นเพลง/เพลย์ลิสต์จาก YouTube")
    async def play(self, ctx, *, search_query: str):
        guild_id = ctx.guild.id
        await self.cancel_idle_timer(guild_id) # Stop idle timer if user plays something

        # Cancel any existing background loader for this guild before starting a new one
        if guild_id in self.bot.background_playlist_loaders:
            logging.info(f"Cancelling previous background loader for guild {guild_id} due to new play command.")
            self.bot.background_playlist_loaders[guild_id].cancel()
            # We don't delete it from the dict here; let the task clean itself up or be overwritten.

        if not ctx.author.voice:
            await ctx.send("🤔 คุณต้องอยู่ในช่องเสียงก่อนถึงจะใช้คำสั่งนี้ได้นะ")
            return

        voice_channel = ctx.author.voice.channel
        voice_client = ctx.guild.voice_client

        if not voice_client or not voice_client.is_connected():
            try:
                voice_client = await voice_channel.connect()
                await ctx.send(f"🔊 เข้าร่วมช่อง `{voice_channel.name}` แล้วจ้า")
            except Exception as e:
                await ctx.send(f"😥 ไม่สามารถเข้าร่วมช่องเสียงได้: {e}")
                return
        elif voice_client.channel != voice_channel:
            try:
                await voice_client.move_to(voice_channel)
                await ctx.send(f"🔄 ย้ายไปที่ช่อง `{voice_channel.name}` แล้วจ้า")
            except Exception as e:
                await ctx.send(f"😥 ไม่สามารถย้ายช่องเสียงได้: {e}")
                return

        processing_msg = await ctx.send(f"🔎 กำลังค้นหาและประมวลผล `{search_query}`...")
        
        playlist_entries_meta = []
        playlist_title = None
        is_playlist = False
        
        try:
            ydl_opts_playlist_check = YDL_OPTIONS_BASE.copy()
            ydl_opts_playlist_check['noplaylist'] = False # Allow playlist detection
            ydl_opts_playlist_check['extract_flat'] = True # Get flat list of entries quickly
            # For extract_flat: True, 'playlist_items' can limit the number of items yt-dlp processes internally for the flat list,
            # but it will still list all if it's a playlist. We want all titles/webpage_urls.

            with yt_dlp.YoutubeDL(ydl_opts_playlist_check) as ydl:
                info = await self.bot.loop.run_in_executor(
                    None,
                    lambda: ydl.extract_info(search_query, download=False)
                )

            if not info:
                await processing_msg.edit(content=f"😭 ไม่พบข้อมูลสำหรับ `{search_query}`")
                return

            if 'entries' in info and info['entries']: # It's a playlist or mix
                is_playlist = True
                playlist_title = info.get('title', search_query)
                # Filter out None entries that yt-dlp might sometimes produce with extract_flat
                playlist_entries_meta = [entry for entry in info['entries'] if entry and (entry.get('webpage_url') or entry.get('url'))]
                if not playlist_entries_meta:
                    await processing_msg.edit(content=f"😥 ไม่พบรายการเพลงที่สามารถเล่นได้ในเพลย์ลิสต์ `{playlist_title}`")
                    return
                await processing_msg.edit(content=f"🎶 ตรวจพบเพลย์ลิสต์: **{playlist_title}** ({len(playlist_entries_meta)} เพลง)")
            
            else: # Single video
                webpage_url = info.get('webpage_url') or info.get('url')
                if not webpage_url:
                    await processing_msg.edit(content=f"😥 ไม่สามารถดึง URL หน้าเว็บสำหรับ `{info.get('title', search_query)}` ได้")
                    return
                # For a single song, playlist_entries_meta will contain just this one song's metadata
                playlist_entries_meta.append({
                    'webpage_url': webpage_url,
                    'title': info.get('title', 'ไม่ทราบชื่อเพลง'),
                    'thumbnail': info.get('thumbnail'),
                    'duration': info.get('duration'),
                    'requester': ctx.author.mention # Requester for the initial command
                })
                playlist_title = info.get('title', 'เพลงเดี่ยว') # For consistency in messaging

        except Exception as e:
            logging.error(f"Error with yt-dlp or processing: {e}")
            await processing_msg.edit(content=f"😥 อ๊ะ! มีบางอย่างผิดพลาดขณะประมวลผล `{search_query}`: {str(e)[:1000]}")
            return

        if not playlist_entries_meta: # Should be caught above, but as a safeguard
            await processing_msg.edit(content=f"😥 ไม่พบข้อมูลเพลงที่สามารถเพิ่มได้จาก `{search_query}`")
            return

        # --- Add first song and start background loading for playlists ---
        if guild_id not in queues:
            queues[guild_id] = []

        # Determine if the system is currently idle (no song playing/paused, queue empty)
        # This helps decide whether to immediately start play_next_song
        is_system_idle = not (voice_client.is_playing() or voice_client.is_paused()) and not queues[guild_id]

        first_song_meta = playlist_entries_meta[0]
        # Ensure requester is set for the first song if it wasn't (e.g. from flat playlist entries)
        if 'requester' not in first_song_meta:
            first_song_meta['requester'] = ctx.author.mention


        # Add first song to the front of the queue if the queue is being cleared, or to the end if appending
        # For this new "play first fast" logic, if a playlist is played, we clear the existing queue.
        # If it's a single song, we append. This behavior might need user feedback.
        # For now, let's assume playing a playlist means focusing on that playlist.
        
        current_queue_was_cleared = False
        if is_playlist:
            if queues[guild_id]: # If there's an existing queue and we're playing a new playlist
                queues[guild_id].clear()
                current_queue_was_cleared = True
                logging.info(f"Guild {guild_id}: Cleared existing queue for new playlist '{playlist_title}'.")
                if guild_id in self.bot.current_song_info: # Stop current song if new playlist overrides
                    if voice_client.is_playing() or voice_client.is_paused():
                        voice_client.stop() # This will trigger after_playing, which then checks empty queue
                    del self.bot.current_song_info[guild_id]
                is_system_idle = True # Since we cleared and stopped, it's effectively idle for starting new playlist
            
            queues[guild_id].append(first_song_meta)
            
            if len(playlist_entries_meta) > 1:
                # Start background loading for the rest of the songs
                loader_task = asyncio.create_task(
                    self._background_load_remaining_playlist(ctx, playlist_entries_meta[1:], playlist_title)
                )
                self.bot.background_playlist_loaders[guild_id] = loader_task
                await processing_msg.edit(
                    content=f"▶️ กำลังจะเล่นเพลงแรก: **{first_song_meta['title']}** จากเพลย์ลิสต์ **'{playlist_title}'**.\n"
                            f"⏳ กำลังโหลดเพลงที่เหลือ ({len(playlist_entries_meta) - 1} เพลง) เข้าคิวในเบื้องหลัง..."
                )
            else: # Playlist with only one song
                await processing_msg.edit(content=f"▶️ เพิ่มเพลง **{first_song_meta['title']}** เข้าคิวแล้ว")

        else: # Single song
            queues[guild_id].append(first_song_meta)
            await processing_msg.edit(content=f"▶️ เพิ่มเพลง **{first_song_meta['title']}** เข้าคิวแล้ว")


        if is_system_idle or current_queue_was_cleared: # If nothing was playing OR we just cleared the queue for a new playlist
            asyncio.create_task(self.play_next_song(ctx))
        else: # If something is already playing and this is just adding to queue
            if not is_playlist: # For single song added to an existing queue
                embed = discord.Embed(title="🎶 เพิ่มเข้าคิวแล้ว", description=f"[{first_song_meta['title']}]({first_song_meta['webpage_url']})", color=discord.Color.green())
                embed.add_field(name="ขอโดย", value=first_song_meta['requester'])
                if first_song_meta.get('thumbnail'): embed.set_thumbnail(url=first_song_meta['thumbnail'])
                # No need to show processing_msg if we send this embed
                try: await processing_msg.delete() 
                except discord.NotFound: pass
                await ctx.send(embed=embed)


    @commands.command(name="skip", aliases=["s", "ข้าม"], help="ข้ามเพลงปัจจุบัน")
    async def skip(self, ctx):
        guild_id = ctx.guild.id
        voice_client = ctx.guild.voice_client
        if not voice_client or not (voice_client.is_playing() or voice_client.is_paused()):
            await ctx.send("🤔 ไม่มีเพลงกำลังเล่นอยู่นะ")
            return

        current_song_title = "เพลงปัจจุบัน"
        if guild_id in self.bot.current_song_info and self.bot.current_song_info[guild_id]:
            current_song_title = self.bot.current_song_info[guild_id]['title']
        
        await self.cancel_idle_timer(guild_id) # Cancel idle timer as an action is being taken
        await ctx.send(f"⏭️ ข้ามเพลง `{current_song_title}` แล้วจ้า")
        voice_client.stop() # Triggers after_playing_song -> play_next_song


    @commands.command(name="queue", aliases=["q", "คิว"], help="แสดงคิวเพลงปัจจุบัน")
    async def queue_command(self, ctx):
        guild_id = ctx.guild.id
        embed = discord.Embed(title="รายการคิวเพลง 📜", color=discord.Color.orange())
        
        current_song_data = self.bot.current_song_info.get(guild_id)
        active_queue_songs = queues.get(guild_id, [])
        
        is_loading = guild_id in self.bot.background_playlist_loaders and \
                     not self.bot.background_playlist_loaders[guild_id].done()

        if not current_song_data and not active_queue_songs and not is_loading:
            await ctx.send(f"썰 คิวเพลงว่างเปล่าจ้า ลองใช้ `{self.bot.command_prefix}play` เพื่อเพิ่มเพลงดูสิ")
            return

        if current_song_data:
            embed.add_field(
                name="กำลังเล่นอยู่ 🎧",
                value=f"[{current_song_data['title']}]({current_song_data['webpage_url']}) (ขอโดย: {current_song_data['requester']})",
                inline=False
            )

        queue_list_str = ""
        if active_queue_songs:
            for i, song in enumerate(active_queue_songs[:15]): # Show up to 15 upcoming songs
                duration_str = ""
                if song.get('duration'): # Assuming duration might be available from initial flat extract
                    m_q, s_q = divmod(song['duration'], 60); h_q, m_q = divmod(m_q, 60)
                    duration_str = f" ({int(h_q):02d}:{int(m_q):02d}:{int(s_q):02d})" if h_q else f" ({int(m_q):02d}:{int(s_q):02d})"
                
                queue_list_str += f"{i+1}. [{song['title']}]({song['webpage_url']}){duration_str} (ขอโดย: {song['requester']})\n"
        
        if not queue_list_str and active_queue_songs:
             queue_list_str = "มีเพลงในคิวแต่ยังไม่แสดง" # Should not happen with above logic
        elif not active_queue_songs and not is_loading and current_song_data:
            queue_list_str = "ไม่มีเพลงในคิวถัดไป"
        elif not active_queue_songs and is_loading:
            queue_list_str = "กำลังโหลดเพลงเข้าคิวในเบื้องหลัง..."
        
        if queue_list_str:
            embed.add_field(name=f"เพลงถัดไปในคิว ({len(active_queue_songs)} เพลง) ⏳", value=queue_list_str, inline=False)
        elif not current_song_data and is_loading : # No current song, queue empty, but loading
             embed.add_field(name="คิวเพลง ⏳", value="กำลังโหลดเพลงเข้าคิวในเบื้องหลัง...", inline=False)


        footer_parts = []
        if len(active_queue_songs) > 15:
            footer_parts.append(f"และอีก {len(active_queue_songs) - 15} เพลงในคิว...")
        if is_loading:
            footer_parts.append("🔄 กำลังโหลดเพลงเพิ่มเติมในเบื้องหลัง...")
        
        if footer_parts:
            embed.set_footer(text="\n".join(footer_parts))
        elif not active_queue_songs and not is_loading and current_song_data:
            embed.set_footer(text="ไม่มีเพลงรอในคิวแล้ว")
        
        await ctx.send(embed=embed)


    @commands.command(name="stop", aliases=["หยุด"], help="หยุดเล่นเพลงและล้างคิวทั้งหมด")
    async def stop(self, ctx):
        guild_id = ctx.guild.id
        voice_client = ctx.guild.voice_client

        if voice_client and voice_client.is_connected():
            # Cancel background loader first
            if guild_id in self.bot.background_playlist_loaders:
                self.bot.background_playlist_loaders[guild_id].cancel()
                del self.bot.background_playlist_loaders[guild_id] # Remove task reference
                logging.info(f"Guild {guild_id}: Background loader cancelled by stop command.")

            if voice_client.is_playing() or voice_client.is_paused():
                voice_client.stop() 
            
            if guild_id in queues: queues[guild_id].clear()
            if guild_id in self.bot.current_song_info: del self.bot.current_song_info[guild_id]
            
            await self.cancel_idle_timer(guild_id)
            await ctx.send("🛑 หยุดเล่นเพลงและล้างคิวเรียบร้อยแล้ว")
            await ctx.send(f"บอทจะออกจากช่องเสียงหากไม่มีการใช้งานใน 15 วินาที (พิมพ์ `{self.bot.command_prefix}play <ชื่อเพลง>` เพื่อเล่นต่อ)")
            await self.start_idle_timer(ctx)
        else:
            await ctx.send("🤔 บอทไม่ได้เชื่อมต่อกับช่องเสียงใดๆ หรือไม่ได้กำลังเล่นเพลงอยู่")


    @commands.command(name="leave", aliases=["dc", "ออก"], help="ให้บอทออกจากช่องเสียง")
    async def leave(self, ctx):
        guild_id = ctx.guild.id
        voice_client = ctx.guild.voice_client
        await self.cancel_idle_timer(guild_id)

        if guild_id in self.bot.background_playlist_loaders: # Cancel loader on leave
            self.bot.background_playlist_loaders[guild_id].cancel()
            # Task will be removed from dict when it finishes/is_cancelled

        if voice_client and voice_client.is_connected():
            await voice_client.disconnect() # on_voice_state_update will handle full cleanup
            await ctx.send("👋 แล้วเจอกันใหม่นะ!")
        else:
            await ctx.send("🤔 บอทไม่ได้อยู่ในช่องเสียงใดๆ เลยนะ")


    @commands.command(name="nowplaying", aliases=["np", "กำลังเล่น"], help="แสดงเพลงที่กำลังเล่นอยู่")
    async def nowplaying(self, ctx):
        guild_id = ctx.guild.id
        if not ctx.guild.voice_client or not (ctx.guild.voice_client.is_playing() or ctx.guild.voice_client.is_paused()):
            await ctx.send("🤔 ไม่มีเพลงกำลังเล่นอยู่นะ")
            return

        current = self.bot.current_song_info.get(guild_id)
        if current:
            embed = discord.Embed(title="🎧 เพลงที่กำลังเล่น", description=f"[{current['title']}]({current['webpage_url']})", color=discord.Color.purple())
            embed.add_field(name="ขอโดย", value=current['requester'], inline=True)
            if current.get('duration'):
                m, s = divmod(current['duration'], 60); h, m = divmod(m, 60)
                duration_str = f"{int(h):02d}:{int(m):02d}:{int(s):02d}" if h else f"{int(m):02d}:{int(s):02d}"
                embed.add_field(name="ความยาว", value=duration_str, inline=True)
            if current.get('thumbnail'): embed.set_thumbnail(url=current['thumbnail'])
            await ctx.send(embed=embed)
        else:
            await ctx.send("🤔 ไม่สามารถดึงข้อมูลเพลงที่กำลังเล่นได้ (อาจจะเพิ่งเริ่มเล่น หรือมีข้อผิดพลาด)")

    async def cog_check(self, ctx): 
        if not ctx.guild:
            await ctx.send("คำสั่งนี้ใช้ได้เฉพาะในเซิร์ฟเวอร์เท่านั้น")
            return False
        return True

    @play.before_invoke
    async def ensure_user_in_voice_for_play(self, ctx):
        if not ctx.author.voice:
            await ctx.send("🤔 คุณต้องอยู่ในช่องเสียงก่อนถึงจะใช้คำสั่งนี้ได้นะ")
            raise commands.CommandError("User not in a voice channel for play command.")

    @skip.before_invoke
    @stop.before_invoke
    @nowplaying.before_invoke
    async def ensure_bot_and_user_in_voice_for_control(self, ctx):
        if not ctx.guild.voice_client:
            await ctx.send(f"⚠️ บอทไม่ได้เชื่อมต่อกับช่องเสียงใดๆ เลยนะ ลองใช้ `{self.bot.command_prefix}play` เพื่อให้บอทเข้ามา")
            raise commands.CommandError("Bot is not connected to a voice channel.")
        if not ctx.author.voice or ctx.author.voice.channel != ctx.guild.voice_client.channel:
            await ctx.send("⚠️ คุณต้องอยู่ในช่องเสียงเดียวกับบอทเพื่อใช้คำสั่งนี้")
            raise commands.CommandError("User is not in the same voice channel as the bot.")


@bot.event
async def on_ready():
    print(f'🤖 {bot.user.name} พร้อมให้บริการแล้วจ้า!')
    print(f'🆔 ID ของบอท: {bot.user.id}')
    print('------')
    await bot.add_cog(MusicCog(bot))

@bot.event
async def on_voice_state_update(member, before, after):
    if member.id == bot.user.id: 
        guild_id = member.guild.id
        if not after.channel: # Bot disconnected or was moved from voice channel
            logging.info(f"Bot disconnected/moved from voice in {member.guild.name}. Cleaning up guild {guild_id}.")
            
            if guild_id in bot.idle_timers and bot.idle_timers[guild_id]:
                bot.idle_timers[guild_id].cancel()
                bot.idle_timers[guild_id] = None
            
            if hasattr(bot, 'background_playlist_loaders') and guild_id in bot.background_playlist_loaders:
                if not bot.background_playlist_loaders[guild_id].done():
                    bot.background_playlist_loaders[guild_id].cancel()
                # We can remove it here or let the cancelled task handle its removal from the dict
                # For safety, let's remove it.
                del bot.background_playlist_loaders[guild_id]
                logging.info(f"Cancelled and removed background loader for guild {guild_id} due to voice disconnect.")

            if guild_id in queues:
                queues[guild_id].clear()
            if hasattr(bot, 'current_song_info') and guild_id in bot.current_song_info:
                del bot.current_song_info[guild_id]
            
            # Ensure voice client is fully cleaned up by discord.py
            vc = discord.utils.get(bot.voice_clients, guild=member.guild)
            if vc and vc.is_connected(): # Should ideally not happen if after.channel is None
                logging.warning(f"Bot voice state indicates disconnect, but voice_client for {guild_id} still exists. Forcing stop.")
                if vc.is_playing() or vc.is_paused():
                    vc.stop()
                # await vc.disconnect(force=True) # Might be too aggressive, usually discord.py handles this
            logging.info(f"Cleanup for guild {guild_id} completed after voice disconnect.")


# Token Check
if BOT_TOKEN == 'YOUR_ACTUAL_BOT_TOKEN':
    print("🚨 คำเตือน: คุณกำลังใช้ BOT_TOKEN ตัวอย่าง!")
    print("   โปรดตั้งค่า BOT_TOKEN ที่ถูกต้องในโค้ด หรือผ่าน Environment Variable")
    print("   บอทจะไม่สามารถเริ่มทำงานได้จนกว่าจะใส่ Token ที่ถูกต้อง")
    exit() 
#elif len(BOT_TOKEN) < 50: # Basic sanity check for token format
    # print(f"🚨 คำเตือน: BOT_TOKEN ('{BOT_TOKEN[:10]}...') ดูเหมือนจะไม่ถูกต้อง โปรดตรวจสอบ")
    # exit() # Optionally exit if token looks very wrong

try:
    bot.run(BOT_TOKEN)
except discord.errors.LoginFailure:
    print("🚨 ไม่สามารถล็อกอินได้ โปรดตรวจสอบ BOT_TOKEN อีกครั้ง")
except Exception as e:
    print(f"🚨 เกิดข้อผิดพลาดในการรันบอท: {e}")