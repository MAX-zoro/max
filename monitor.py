"""Read-only Discord collection: no Discord message-send or membership APIs."""
import asyncio
import contextlib
import logging
import os
import time
from datetime import datetime, timezone
from urllib.parse import urlsplit

import aiohttp
import discord

from store import now


MAX_FILE_BYTES = 100 * 1024 * 1024
CDN_HOSTS = {'cdn.discordapp.com', 'media.discordapp.net', 'cdn.discordapp.net'}


def allowed_media_url(url):
    parsed = urlsplit(url)
    return parsed.scheme == 'https' and parsed.hostname in CDN_HOSTS and parsed.port in (None, 443) and not parsed.username and not parsed.password


def private_write(path, text):
    temporary = path.with_suffix('.tmp')
    with open(temporary, 'w', encoding='utf-8') as f:
        os.chmod(temporary, 0o600)
        f.write(text)
    temporary.replace(path)


def message_data(message):
    media = []
    for att in message.attachments:
        media.append({'key': str(att.id), 'url': att.url, 'filename': att.filename,
                      'mime': att.content_type or 'application/octet-stream'})
    embeds = [embed.to_dict() for embed in message.embeds]
    for index, embed in enumerate(embeds):
        for kind in ('image', 'thumbnail', 'video'):
            value = embed.get(kind, {})
            url = value.get('proxy_url') or value.get('url')
            if url:
                media.append({'key': f'embed-{index}-{kind}', 'url': url,
                              'filename': f'embed-{index}-{kind}', 'mime': 'application/octet-stream'})
    for sticker in message.stickers:
        media.append({'key': f'sticker-{sticker.id}', 'url': str(sticker.url),
                      'filename': sticker.name, 'mime': 'application/octet-stream'})
    return {
        'id': str(message.id), 'channel_id': str(message.channel.id),
        'channel_name': message.channel.name, 'guild_id': str(message.guild.id),
        'guild_name': message.guild.name, 'author': message.author.display_name,
        'author_id': str(message.author.id), 'content': message.clean_content,
        'raw_content': message.content, 'created_at': message.created_at.isoformat(),
        'edited_at': message.edited_at.isoformat() if message.edited_at else None,
        'embeds': embeds, 'media': media, 'url': message.jump_url,
        'reply_to': str(message.reference.message_id) if message.reference and message.reference.message_id else None,
    }


class Reader(discord.Client):
    def __init__(self, monitor):
        super().__init__(chunk_guilds_at_startup=False, guild_subscriptions=False, max_messages=200)
        self.monitor = monitor

    async def on_ready(self):
        m = self.monitor
        previous = m.store.get('owner')
        if previous and previous != str(self.user.id):
            m.store.select_channels([], '0')
            m.store.set('running', False)
        m.store.set('owner', str(self.user.id))
        m.state, m.error = 'connected', ''
        m.account = {'name': self.user.name, 'id': str(self.user.id)}
        await m.subscribe_selected()
        m.wake.set()

    async def on_disconnect(self):
        if self.monitor.state == 'connected':
            self.monitor.state = 'reconnecting'

    async def on_resumed(self):
        self.monitor.state = 'connected'
        self.monitor.error = ''
        self.monitor.wake.set()

    async def on_message(self, message):
        self.monitor.ingest(message)

    async def on_raw_message_edit(self, payload):
        m = self.monitor
        if not m.is_collecting(payload.channel_id):
            return
        # Only update an archived message; do not import unrelated older history.
        if not m.store.db.execute('SELECT 1 FROM messages WHERE id=?', (str(payload.message_id),)).fetchone():
            return
        try:
            channel = self.get_channel(payload.channel_id)
            if channel:
                m.ingest(await channel.fetch_message(payload.message_id))
        except discord.HTTPException:
            m.error = '有消息编辑未能同步，将继续接收新消息。'

    async def on_error(self, event, *args, **kwargs):
        self.monitor.error = '处理 Discord 事件失败；后台会继续补查新消息。'


class Monitor:
    def __init__(self, store):
        self.store = store
        self.client = None
        self.connection_task = None
        self.workers = []
        self.state = 'disconnected'
        self.error = ''
        self.account = None
        self.last_message_at = None
        self.wake = asyncio.Event()
        self.control_lock = asyncio.Lock()
        # Library exception logs can contain request details; UI exposes only fixed error messages.
        logger = logging.getLogger('discord')
        logger.handlers = [logging.NullHandler()]
        logger.propagate = False

    @property
    def token_path(self):
        return self.store.directory / 'discord-token.txt'

    async def start(self):
        self.workers = [asyncio.create_task(self.reconcile_loop()), asyncio.create_task(self.media_loop())]
        if self.token_path.exists():
            token = self.token_path.read_text(encoding='utf-8').strip()
            if token:
                await self.connect(token)

    async def connect(self, token):
        async with self.control_lock:
            await self.stop_connection()
            private_write(self.token_path, '')
            self.state, self.error = 'connecting', ''
            self.client = Reader(self)
            self.connection_task = asyncio.create_task(self.run_connection(self.client, token))

    async def run_connection(self, client, token):
        delay = 5
        try:
            while True:
                try:
                    await asyncio.wait_for(client.login(token), 45)
                    break
                except (aiohttp.ClientError, OSError, asyncio.TimeoutError):
                    self.state, self.error = 'reconnecting', '服务器暂时无法连接 Discord，正在重试。'
                    await asyncio.sleep(delay)
                    delay = min(delay * 2, 120)
            private_write(self.token_path, token)
            while True:
                try:
                    await client.connect(reconnect=True)
                    if client.is_closed():
                        break
                except (discord.GatewayNotFound, discord.ConnectionClosed, aiohttp.ClientError, OSError):
                    self.state = 'reconnecting'
                    self.error = '连接中断，正在重连。'
                    await asyncio.sleep(delay)
                    delay = min(delay * 2, 120)
        except discord.LoginFailure:
            self.state, self.error = 'error', 'Token 无效或已过期，请重新连接。'
        except asyncio.CancelledError:
            raise
        except Exception:
            self.state, self.error = 'error', '连接失败，请检查服务器网络、账号状态和依赖版本，然后重新连接。'
        finally:
            await client.close()
            if self.state not in ('error', 'disconnected'):
                self.state = 'disconnected'

    async def stop_connection(self):
        if self.connection_task:
            self.connection_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self.connection_task
        self.connection_task = None
        self.client = None
        self.account = None
        self.state = 'disconnected'

    async def disconnect(self):
        async with self.control_lock:
            self.store.set('running', False)
            await self.stop_connection()
            private_write(self.token_path, '')
            self.error = ''

    async def close(self):
        await self.stop_connection()
        for task in self.workers:
            task.cancel()
        await asyncio.gather(*self.workers, return_exceptions=True)

    def ready(self):
        return bool(self.client and self.client.is_ready() and self.state == 'connected')

    def is_collecting(self, channel_id):
        return self.store.get('running', False) and any(c['id'] == str(channel_id) for c in self.store.channels(True))

    def ingest(self, message):
        if not message.guild or not self.is_collecting(message.channel.id):
            return
        if self.store.save_message(message_data(message)):
            self.last_message_at = now()

    async def subscribe_selected(self):
        if not self.ready():
            return
        for gid in {c['guild_id'] for c in self.store.channels(True)}:
            guild = self.client.get_guild(int(gid))
            if guild:
                await guild.subscribe(typing=True, threads=True)

    def available_channels(self, guild):
        # Forum posts and threads are selectable individually once Discord has loaded them.
        return [c for c in [*guild.text_channels, *guild.threads]
                if guild.me and c.permissions_for(guild.me).view_channel
                and c.permissions_for(guild.me).read_message_history]

    async def select(self, ids):
        if not self.ready():
            raise ValueError('请先连接 Discord 账号。')
        allowed = {str(c.id): c for g in self.client.guilds for c in self.available_channels(g)}
        if not ids or any(cid not in allowed for cid in ids):
            raise ValueError('请选择当前账号有权限读取的文字频道或已加载的帖子。')
        selected = [{'id': cid, 'guild_id': str(allowed[cid].guild.id),
                     'guild_name': allowed[cid].guild.name, 'name': allowed[cid].name} for cid in dict.fromkeys(ids)]
        baseline = str(discord.utils.time_snowflake(datetime.now(timezone.utc)))
        self.store.select_channels(selected, baseline)
        self.store.set('running', True)
        await self.subscribe_selected()
        self.wake.set()

    async def reconcile_once(self):
        if not self.ready() or not self.store.get('running', False):
            return
        for saved in self.store.channels(True):
            cid = saved['id']
            channel = self.client.get_channel(int(cid))
            try:
                if channel is None:
                    channel = await self.client.fetch_channel(int(cid))
                async for message in channel.history(limit=None, after=discord.Object(id=int(saved['cursor'])), oldest_first=True):
                    if not self.is_collecting(cid):
                        break
                    self.ingest(message)
                    # Only ordered history advances the cursor; live events may arrive out of order.
                    self.store.progress(cid, str(message.id))
                self.store.progress(cid)
            except (discord.Forbidden, discord.NotFound):
                self.store.progress(cid, error='频道不可访问或没有读取历史权限。')
            except Exception:
                self.store.progress(cid, error='补查失败，将在下一轮重试。')

    async def reconcile_loop(self):
        while True:
            try:
                self.wake.clear()
                await self.reconcile_once()
                try:
                    await asyncio.wait_for(self.wake.wait(), 60)
                except asyncio.TimeoutError:
                    pass
            except asyncio.CancelledError:
                raise
            except Exception:
                self.error = '补查任务暂时失败，将自动重试。'
                await asyncio.sleep(10)

    async def download(self, session, row):
        error = ''
        path = self.store.media_dir / row['id']
        temporary = self.store.media_dir / (row['id'] + '.part')
        try:
            if not allowed_media_url(row['url']):
                raise ValueError('外部嵌入媒体不自动下载，原链接已保留。')
            async with session.get(row['url'], allow_redirects=False) as response:
                if response.status != 200:
                    raise ValueError(f'媒体下载返回 HTTP {response.status}，请查看原消息。')
                if response.content_length and response.content_length > MAX_FILE_BYTES:
                    raise ValueError('文件超过 100 MB，原链接已保留。')
                size = 0
                with open(temporary, 'wb') as f:
                    async for chunk in response.content.iter_chunked(65536):
                        size += len(chunk)
                        if size > MAX_FILE_BYTES:
                            raise ValueError('文件超过 100 MB，原链接已保留。')
                        f.write(chunk)
                temporary.replace(path)
                mime = response.headers.get('Content-Type', row['mime']).split(';')[0]
                with self.store.db:
                    self.store.db.execute('UPDATE media SET path=?,mime=?,error=? WHERE id=?', (row['id'], mime, '', row['id']))
                return
        except ValueError as exc:
            error = str(exc)
        except (aiohttp.ClientError, OSError, asyncio.TimeoutError):
            error = '媒体下载失败，后台稍后重试；原链接已保留。'
        with self.store.db:
            self.store.db.execute('UPDATE media SET error=?,attempts=attempts+1,retry_at=? WHERE id=?',
                                  (error, time.time() + min(3600, 60 * 2 ** min(row['attempts'], 6)), row['id']))

    async def media_loop(self):
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=180)) as session:
            while True:
                try:
                    rows = self.store.db.execute("SELECT * FROM media WHERE path='' AND retry_at<=? LIMIT 10", (time.time(),)).fetchall()
                    for row in rows:
                        await self.download(session, row)
                    await asyncio.sleep(3)
                except asyncio.CancelledError:
                    raise
                except Exception:
                    self.error = '媒体保存任务暂时失败，请检查磁盘空间。'
                    await asyncio.sleep(10)
