# -*- coding: utf-8 -*-
# main_bot_updated.py

import discord
from discord.ext import commands, tasks
import yt_dlp # ใช้ yt-dlp แทน youtube_dl เพราะมีการอัปเดตสม่ำเสมอ
import asyncio
import logging
import os

# ตั้งค่า logging เพื่อดู error (ถ้ามี)
logging.basicConfig(level=logging.INFO)

# Token ของบอทคุณ (สำคัญมาก: ให้เก็บเป็นความลับ)
# แนะนำให้ใช้ environment variable หรือ config file ในการเก็บ token จริง
BOT_TOKEN = os.environ.get('DISCORD_BOT_TOKEN', "YOUR_BOT_TOKEN_HERE") # ใส่ Token ของคุณที่นี่ถ้าไม่ได้ใช้ env var

# ตั้งค่า Intents (สิทธิ์ที่บอทต้องการ)
intents = discord.Intents.default()
intents.message_content = True # จำเป็นสำหรับการอ่านเนื้อหาข้อความ (คำสั่ง)
intents.voice_states = True    # จำเป็นสำหรับการจัดการสถานะเสียง

# Prefix ของคำสั่ง
bot = commands.Bot(command_prefix="%", intents=intents)

# การตั้งค่าสำหรับ yt-dlp และ ffmpeg
YDL_OPTIONS = {
    'format': 'bestaudio/best',
    'outtmpl': '%(extractor)s-%(id)s-%(title)s.%(ext)s',
    'restrictfilenames': True,
    'noplaylist': False,       # ตั้งค่าเป็น False เพื่อให้ yt-dlp ประมวลผล URL ของเพลย์ลิสต์
    'nocheckcertificate': True,
    'ignoreerrors': True,      # สำคัญสำหรับเพลย์ลิสต์: ให้ yt-dlp ข้ามวิดีโอที่ประมวลผลไม่ได้และทำงานต่อ
    'logtostderr': False,
    'quiet': True,
    'no_warnings': True,
    'default_search': 'ytsearch',
    'source_address': '0.0.0.0', # ipv6 addresses cause issues sometimes
    'extract_flat': False,     # ดึงข้อมูล metadata ทั้งหมดของแต่ละรายการในเพลย์ลิสต์
    # หากคุณได้ตั้งค่า cookiefile ไว้จากคำแนะนำก่อนหน้า ให้คงไว้อย่างเดิม
    'cookiefile': '/app/cookies.txt', # หรือ path ที่ถูกต้อง
}

FFMPEG_OPTIONS = {
    'before_options': '-reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5', # ช่วยให้การเชื่อมต่อเสถียรขึ้น
    'options': '-vn' # ไม่เอาส่วนวิดีโอ เอาแต่เสียง
}

# Dictionary สำหรับเก็บคิวเพลงของแต่ละเซิร์ฟเวอร์ (guild)
queues = {} # {guild_id: [song_info, song_info, ...]}

class MusicCog(commands.Cog):
    def __init__(self, bot_instance):
        self.bot = bot_instance
        self.bot.idle_timers = {} # {guild_id: asyncio.Task}
        self.bot.current_song = {} # {guild_id: song_info}

    async def start_idle_timer(self, ctx):
        """
        เริ่มนับเวลาถอยหลังเพื่อออกจากห้องหากไม่มีการใช้งาน
        """
        guild_id = ctx.guild.id
        if guild_id in self.bot.idle_timers and self.bot.idle_timers[guild_id]:
            self.bot.idle_timers[guild_id].cancel() # ยกเลิก timer เก่าถ้ามี

        self.bot.idle_timers[guild_id] = self.bot.loop.create_task(self.auto_disconnect(ctx))

    async def cancel_idle_timer(self, guild_id):
        """
        ยกเลิก timer การออกจากห้องอัตโนมัติ
        """
        if guild_id in self.bot.idle_timers and self.bot.idle_timers[guild_id]:
            self.bot.idle_timers[guild_id].cancel()
            self.bot.idle_timers[guild_id] = None

    async def auto_disconnect(self, ctx):
        """
        ตรวจสอบและออกจากห้องเสียงหากไม่มีการใช้งานเป็นเวลา 15 วินาที
        """
        await asyncio.sleep(15) # รอ 15 วินาที
        guild_id = ctx.guild.id
        voice_client = ctx.guild.voice_client

        if voice_client and voice_client.is_connected():
            # ตรวจสอบว่ายังไม่มีเพลงเล่น และคิวว่างอยู่
            is_playing_or_paused = voice_client.is_playing() or voice_client.is_paused()
            is_queue_empty = not (guild_id in queues and queues[guild_id])

            if not is_playing_or_paused and is_queue_empty:
                logging.info(f"Auto-disconnecting from {ctx.guild.name} due to inactivity.")
                await voice_client.disconnect()
                await ctx.send("👋 ไม่มีเพลงเล่นเป็นเวลา 15 วินาที บอทขอตัวก่อนนะ!")
                # ล้างข้อมูลที่เกี่ยวข้องกับ guild นี้
                if guild_id in queues:
                    queues[guild_id].clear()
                if guild_id in self.bot.current_song:
                    del self.bot.current_song[guild_id]
        
        if guild_id in self.bot.idle_timers: # ล้าง task ออกจาก dict
             self.bot.idle_timers[guild_id] = None


    async def play_next_song(self, ctx):
        """
        เล่นเพลงถัดไปในคิวของเซิร์ฟเวอร์ (guild) นั้นๆ
        """
        guild_id = ctx.guild.id
        await self.cancel_idle_timer(guild_id) # ยกเลิก idle timer เมื่อเริ่มเล่นเพลงใหม่

        if guild_id in queues and queues[guild_id]:
            voice_client = ctx.guild.voice_client
            if voice_client and voice_client.is_connected():
                if voice_client.is_playing() or voice_client.is_paused(): # ป้องกันการเล่นซ้อน
                    return

                song_info = queues[guild_id].pop(0)
                source_url = song_info['source']
                title = song_info['title']
                webpage_url = song_info['webpage_url']
                requester = song_info['requester']

                try:
                    player = discord.FFmpegPCMAudio(source_url, **FFMPEG_OPTIONS)
                    
                    def after_playing(error):
                        if error:
                            logging.error(f"เกิดข้อผิดพลาดระหว่างเล่นเพลง: {error}")
                        
                        # ตรวจสอบว่าบอทถูกตัดการเชื่อมต่อด้วยตนเองหรือไม่ก่อนที่จะพยายามเล่นเพลงถัดไป
                        if ctx.guild.voice_client and ctx.guild.voice_client.is_connected():
                            fut = asyncio.run_coroutine_threadsafe(self.play_next_song(ctx), self.bot.loop)
                            try:
                                fut.result(timeout=5) # เพิ่ม timeout เพื่อป้องกันการ block นานเกินไป
                            except asyncio.TimeoutError:
                                logging.warning(f"play_next_song task timed out for guild {guild_id}")
                            except Exception as e_fut:
                                logging.error(f"เกิดข้อผิดพลาดในการเรียก play_next_song: {e_fut}")
                        else:
                            logging.info(f"Voice client not connected in guild {guild_id} after song, not playing next.")
                            # ถ้า voice client ไม่ได้เชื่อมต่อแล้ว อาจจะเคลียร์คิวที่เหลือ
                            if guild_id in queues:
                                queues[guild_id].clear()
                            if guild_id in self.bot.current_song:
                                del self.bot.current_song[guild_id]


                    voice_client.play(player, after=after_playing)
                    
                    embed = discord.Embed(
                        title="🎧 กำลังเล่นเพลง",
                        description=f"[{title}]({webpage_url})",
                        color=discord.Color.blue()
                    )
                    embed.add_field(name="ขอโดย", value=requester, inline=False)
                    embed.set_thumbnail(url=song_info.get('thumbnail', ''))
                    await ctx.send(embed=embed)
                    
                    self.bot.current_song[guild_id] = song_info

                except Exception as e:
                    logging.error(f"ไม่สามารถเล่นเพลงได้: {e}")
                    await ctx.send(f"😥 ไม่สามารถเล่นเพลง `{title}` ได้: {e}")
                    await self.play_next_song(ctx) # ลองเล่นเพลงถัดไป
            else: # voice_client ไม่ได้เชื่อมต่อ
                if guild_id in queues: queues[guild_id].clear()
                if guild_id in self.bot.current_song: del self.bot.current_song[guild_id]
        else: # คิวหมด
            if guild_id in self.bot.current_song:
                 del self.bot.current_song[guild_id]
            await ctx.send("🎵 คิวเพลงหมดแล้วจ้า")
            if ctx.guild.voice_client and ctx.guild.voice_client.is_connected():
                await ctx.send(f"บอทจะออกจากช่องเสียงหากไม่มีการใช้งานใน 15 วินาที (พิมพ์ `{self.bot.command_prefix}play <ชื่อเพลง>` เพื่อเล่นต่อ)")
                await self.start_idle_timer(ctx)

    @commands.command(name="play", aliases=["p", "เล่น"], help="เล่นเพลงจาก YouTube หรือ URL (รองรับเพลย์ลิสต์)")
    async def play(self, ctx, *, search_query: str):
        guild_id = ctx.guild.id
        await self.cancel_idle_timer(guild_id) # ยกเลิก idle timer เมื่อมีคำสั่ง play

        if not ctx.author.voice:
            await ctx.send("🤔 คุณต้องอยู่ในช่องเสียงก่อนถึงจะใช้คำสั่งนี้ได้นะ")
            return

        voice_channel = ctx.author.voice.channel
        voice_client = ctx.guild.voice_client

        if not voice_client:
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
        
        processing_msg = await ctx.send(f"🔎 กำลังค้นหา/ประมวลผล `{search_query}`...")
        songs_to_add = []
        playlist_title = None # สำหรับเก็บชื่อเพลย์ลิสต์

        try:
            with yt_dlp.YoutubeDL(YDL_OPTIONS) as ydl:
                info = ydl.extract_info(search_query, download=False)

            if not info:
                await processing_msg.edit(content=f"😭 ไม่พบข้อมูลสำหรับ `{search_query}`")
                return

            if 'entries' in info: # Playlist
                playlist_title = info.get('title', search_query)
                await processing_msg.edit(content=f"🎶 กำลังเพิ่มเพลงจากเพลย์ลิสต์: **{playlist_title}** ({len(info['entries'])} เพลง)...")
                
                for i, entry in enumerate(info['entries']):
                    if not entry:
                        logging.warning(f"Skipping None entry in playlist: {playlist_title} at index {i}")
                        continue
                    
                    stream_url = entry.get('url')
                    if not stream_url: # yt-dlp อาจจะดึง URL ไม่ได้สำหรับบางรายการ
                        title_entry = entry.get('title', f'รายการที่ {i+1} (ไม่ทราบชื่อ)')
                        logging.warning(f"Skipping entry '{title_entry}' in playlist '{playlist_title}' due to missing 'url'.")
                        # อาจจะแจ้งผู้ใช้ แต่ถ้าเพลย์ลิสต์ใหญ่จะ spam chat
                        # await ctx.send(f"⚠️ ข้าม '{title_entry}' เนื่องจากไม่พบ URL สำหรับเล่น")
                        continue

                    songs_to_add.append({
                        'source': stream_url,
                        'title': entry.get('title', 'ไม่ทราบชื่อเพลง'),
                        'webpage_url': entry.get('webpage_url', '#'),
                        'thumbnail': entry.get('thumbnail', ''),
                        'duration': entry.get('duration', 0),
                        'requester': ctx.author.mention
                    })
                if not songs_to_add:
                    await processing_msg.edit(content=f"😥 ไม่สามารถดึงข้อมูลเพลงที่เล่นได้จากเพลย์ลิสต์ `{playlist_title}` เลย")
                    return
            else: # Single video
                stream_url = info.get('url')
                if not stream_url:
                    await processing_msg.edit(content=f"😥 ไม่สามารถดึง URL สำหรับเล่นเพลง `{info.get('title', search_query)}` ได้")
                    return

                songs_to_add.append({
                    'source': stream_url,
                    'title': info.get('title', 'ไม่ทราบชื่อเพลง'),
                    'webpage_url': info.get('webpage_url', '#'),
                    'thumbnail': info.get('thumbnail', ''),
                    'duration': info.get('duration', 0),
                    'requester': ctx.author.mention
                })
        except Exception as e:
            logging.error(f"เกิดข้อผิดพลาดกับ yt-dlp หรือการประมวลผล: {e}")
            await processing_msg.edit(content=f"😥 อ๊ะ! มีบางอย่างผิดพลาด: {e}")
            return

        if not songs_to_add:
            await processing_msg.edit(content=f"😥 ไม่พบข้อมูลเพลงที่สามารถเล่นได้สำหรับ `{search_query}`")
            return

        if guild_id not in queues:
            queues[guild_id] = []
        
        is_first_song_in_empty_queue = not queues[guild_id] and not (voice_client.is_playing() or voice_client.is_paused())

        for song_info in songs_to_add:
            queues[guild_id].append(song_info)

        await processing_msg.delete() # ลบข้อความ "กำลังค้นหา..."

        if len(songs_to_add) == 1:
            song = songs_to_add[0]
            embed = discord.Embed(
                title="🎶 เพิ่มเข้าคิวแล้ว",
                description=f"[{song['title']}]({song['webpage_url']})",
                color=discord.Color.green()
            )
            embed.add_field(name="ขอโดย", value=song['requester'])
            if song['duration']:
                m, s = divmod(song['duration'], 60); h, m = divmod(m, 60)
                duration_str = f"{int(h):02d}:{int(m):02d}:{int(s):02d}" if h else f"{int(m):02d}:{int(s):02d}"
                embed.add_field(name="ความยาว", value=duration_str)
            embed.set_thumbnail(url=song.get('thumbnail', ''))
            await ctx.send(embed=embed)
        else:
            embed = discord.Embed(
                title="🎶 เพิ่มเพลย์ลิสต์เข้าคิวแล้ว",
                description=f"เพิ่ม **{len(songs_to_add)}** เพลงจาก **{playlist_title or 'เพลย์ลิสต์นี้'}**",
                color=discord.Color.green()
            )
            embed.add_field(name="ขอโดย", value=ctx.author.mention)
            await ctx.send(embed=embed)

        if is_first_song_in_empty_queue or not (voice_client.is_playing() or voice_client.is_paused()):
            await self.play_next_song(ctx)


    @commands.command(name="skip", aliases=["s", "ข้าม"], help="ข้ามเพลงปัจจุบัน")
    async def skip(self, ctx):
        voice_client = ctx.guild.voice_client
        if not voice_client or not (voice_client.is_playing() or voice_client.is_paused()):
            await ctx.send("🤔 ไม่มีเพลงกำลังเล่นอยู่นะ")
            return

        await self.cancel_idle_timer(ctx.guild.id) # ยกเลิก idle timer ถ้ามีการ skip
        voice_client.stop() # การ stop จะ trigger 'after' callback ซึ่งจะเรียก play_next_song
        await ctx.send("⏭️ ข้ามเพลงปัจจุบันแล้วจ้า")
        # play_next_song จะถูกเรียกโดย after_playing, หรือถ้าคิวหมดก็จะเริ่ม idle timer

    @commands.command(name="queue", aliases=["q", "คิว"], help="แสดงคิวเพลงปัจจุบัน")
    async def queue_command(self, ctx):
        guild_id = ctx.guild.id
        
        if not (guild_id in queues and queues[guild_id]) and not (guild_id in self.bot.current_song and self.bot.current_song[guild_id]):
            await ctx.send(f"썰 คิวเพลงว่างเปล่าจ้า ลองใช้ `{self.bot.command_prefix}play` เพื่อเพิ่มเพลงดูสิ")
            return

        embed = discord.Embed(title="รายการคิวเพลง 📜", color=discord.Color.orange())
        
        if guild_id in self.bot.current_song and self.bot.current_song[guild_id]:
            current = self.bot.current_song[guild_id]
            embed.add_field(
                name="กำลังเล่นอยู่ 🎧", 
                value=f"[{current['title']}]({current['webpage_url']}) (ขอโดย: {current['requester']})", 
                inline=False
            )
        
        queue_list_str = ""
        if guild_id in queues and queues[guild_id]:
            for i, song in enumerate(queues[guild_id][:10]):
                queue_list_str += f"{i+1}. [{song['title']}]({song['webpage_url']}) (ขอโดย: {song['requester']})\n"
        
        if not queue_list_str:
            queue_list_str = "ไม่มีเพลงในคิวถัดไป"

        embed.add_field(name="เพลงถัดไป ⏳", value=queue_list_str, inline=False)
        
        if guild_id in queues and len(queues[guild_id]) > 10:
            embed.set_footer(text=f"และอีก {len(queues[guild_id]) - 10} เพลงในคิว...")
            
        await ctx.send(embed=embed)

    @commands.command(name="stop", aliases=["หยุด"], help="หยุดเล่นเพลงและล้างคิว")
    async def stop(self, ctx):
        guild_id = ctx.guild.id
        voice_client = ctx.guild.voice_client

        if voice_client and voice_client.is_connected():
            voice_client.stop()
            if guild_id in queues:
                queues[guild_id].clear()
            if guild_id in self.bot.current_song:
                del self.bot.current_song[guild_id]
            await ctx.send("🛑 หยุดเล่นเพลงและล้างคิวเรียบร้อยแล้ว")
            # เริ่ม idle timer หลังจากหยุดเพลงและล้างคิว
            await ctx.send(f"บอทจะออกจากช่องเสียงหากไม่มีการใช้งานใน 15 วินาที (พิมพ์ `{self.bot.command_prefix}play <ชื่อเพลง>` เพื่อเล่นต่อ)")
            await self.start_idle_timer(ctx)
        else:
            await ctx.send("🤔 บอทไม่ได้เชื่อมต่อกับช่องเสียงใดๆ หรือไม่ได้กำลังเล่นเพลงอยู่")

    @commands.command(name="leave", aliases=["dc", "ออก"], help="ให้บอทออกจากช่องเสียง")
    async def leave(self, ctx):
        guild_id = ctx.guild.id
        voice_client = ctx.guild.voice_client

        await self.cancel_idle_timer(guild_id) # ยกเลิก idle timer ก่อนออก

        if voice_client and voice_client.is_connected():
            await voice_client.disconnect()
            if guild_id in queues: queues[guild_id].clear()
            if guild_id in self.bot.current_song: del self.bot.current_song[guild_id]
            await ctx.send("👋 แล้วเจอกันใหม่นะ!")
        else:
            await ctx.send("🤔 บอทไม่ได้อยู่ในช่องเสียงใดๆ เลยนะ")
            
    @commands.command(name="nowplaying", aliases=["np", "กำลังเล่น"], help="แสดงเพลงที่กำลังเล่นอยู่")
    async def nowplaying(self, ctx):
        guild_id = ctx.guild.id
        voice_client = ctx.guild.voice_client

        if not voice_client or not (voice_client.is_playing() or voice_client.is_paused()): # Check if playing or paused
            await ctx.send("🤔 ไม่มีเพลงกำลังเล่นอยู่นะ")
            return

        if guild_id in self.bot.current_song and self.bot.current_song[guild_id]:
            current = self.bot.current_song[guild_id]
            embed = discord.Embed(
                title="🎧 กำลังเล่นเพลง",
                description=f"[{current['title']}]({current['webpage_url']})",
                color=discord.Color.purple()
            )
            embed.add_field(name="ขอโดย", value=current['requester'], inline=True)
            if current.get('duration'):
                m, s = divmod(current['duration'], 60); h, m = divmod(m, 60)
                duration_str = f"{int(h):02d}:{int(m):02d}:{int(s):02d}" if h else f"{int(m):02d}:{int(s):02d}"
                embed.add_field(name="ความยาว", value=duration_str, inline=True)
            embed.set_thumbnail(url=current.get('thumbnail', ''))
            await ctx.send(embed=embed)
        else:
            await ctx.send("🤔 ไม่สามารถดึงข้อมูลเพลงที่กำลังเล่นได้ (อาจจะเพิ่งเริ่มเล่น หรือมีข้อผิดพลาด)")

    @play.before_invoke
    @skip.before_invoke
    @stop.before_invoke
    @nowplaying.before_invoke
    # @queue_command.before_invoke # queue can be checked without being in channel
    async def ensure_voice_state(self, ctx):
        """
        ตรวจสอบว่าผู้ใช้หรือบอทอยู่ในสถานะ voice ที่เหมาะสมสำหรับคำสั่ง
        """
        if ctx.command.name == 'play': # play command handles its own voice channel joining logic
             # For play, ensure user is in a voice channel
            if not ctx.author.voice:
                await ctx.send("🤔 คุณต้องอยู่ในช่องเสียงก่อนถึงจะใช้คำสั่งนี้ได้นะ")
                raise commands.CommandError("User not in a voice channel for play command.")
            return # Play command handles its own connection logic

        # For other commands (skip, stop, nowplaying)
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
    """
    จัดการเมื่อสถานะเสียงของสมาชิก (รวมถึงบอท) เปลี่ยนแปลง
    เช่น บอทถูกเตะออกจากห้อง หรือย้ายห้อง
    """
    if member == bot.user: # ถ้าเป็นบอทเอง
        voice_client = member.guild.voice_client
        guild_id = member.guild.id

        if not after.channel and voice_client: # ถ้าบอทออกจากช่องเสียง (ถูกเตะ หรือ disconnect เอง)
            logging.info(f"Bot disconnected from voice channel in {member.guild.name}.")
            # ยกเลิก idle timer ถ้ามี
            if guild_id in bot.idle_timers and bot.idle_timers[guild_id]:
                bot.idle_timers[guild_id].cancel()
                bot.idle_timers[guild_id] = None
            
            # ล้างคิวและเพลงปัจจุบันของ guild นั้นๆ
            if guild_id in queues:
                queues[guild_id].clear()
                logging.info(f"Cleared queue for guild {guild_id} after disconnect.")
            if guild_id in bot.current_song:
                del bot.current_song[guild_id]
                logging.info(f"Cleared current song for guild {guild_id} after disconnect.")
            
            # ไม่จำเป็นต้องเรียก voice_client.cleanup() หรือ player.cleanup() โดยตรง
            # discord.py และ FFMPEGPCMAudio จัดการส่วนนี้เมื่อ voice_client.disconnect() หรือ stop()

# ตรวจสอบว่า token ถูกใส่หรือยัง
if BOT_TOKEN == "YOUR_BOT_TOKEN_HERE":
    print("🚨 โปรดใส่ BOT_TOKEN ของคุณในโค้ดก่อนรันบอท!")
    print("   คุณสามารถตั้งค่าผ่าน Environment Variable ชื่อ DISCORD_BOT_TOKEN หรือแก้ไขในโค้ดโดยตรง")
else:
    try:
        bot.run(BOT_TOKEN)
    except discord.errors.LoginFailure:
        print("🚨 ไม่สามารถล็อกอินได้ โปรดตรวจสอบ BOT_TOKEN อีกครั้ง")
    except Exception as e:
        print(f"🚨 เกิดข้อผิดพลาดในการรันบอท: {e}")

