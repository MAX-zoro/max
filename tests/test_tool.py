import asyncio
import tempfile
import unittest
from datetime import datetime, timezone
from types import SimpleNamespace

from aiohttp.test_utils import TestClient, TestServer

from app import STATE, create_app
from monitor import MAX_FILE_BYTES, Monitor, Reader, allowed_media_url
from store import Store


def message(mid, channel='10', **extra):
    return {'id': str(mid), 'channel_id': channel, 'content': '测试消息 <script>alert(1)</script>',
            'guild_id': '20', 'guild_name': '币风港（测试）', 'channel_name': '公告',
            'author': '测试用户', 'created_at': '2026-10-25T00:00:00+00:00',
            'edited_at': None, 'media': [], 'embeds': [], **extra}


def discord_message(mid):
    return SimpleNamespace(id=mid, guild=SimpleNamespace(id=20, name='测试服务器'),
                           channel=SimpleNamespace(id=10, name='测试频道'),
                           author=SimpleNamespace(id=30, display_name='测试用户'),
                           content='测试消息', clean_content='测试消息', attachments=[], stickers=[], embeds=[],
                           created_at=datetime.now(timezone.utc), edited_at=None, reference=None,
                           jump_url=f'https://discord.com/channels/20/10/{mid}')


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(self.tmp.name)
        self.store.select_channels([{'id': '10', 'guild_id': '20', 'guild_name': '测试', 'name': '公告'}], '100')

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def test_channel_boundary_and_deduplication(self):
        self.assertFalse(self.store.save_message(message(99)))
        self.assertFalse(self.store.save_message(message(101, '11')))
        self.assertTrue(self.store.save_message(message(101)))
        self.assertFalse(self.store.save_message(message(101)))
        self.assertEqual(self.store.stats()['messages'], 1)

    def test_cursor_survives_restart_and_reselection(self):
        self.store.progress('10', '105')
        self.store.select_channels([], '200')
        self.store.select_channels([{'id': '10', 'guild_id': '20', 'guild_name': '测试', 'name': '公告'}], '300')
        self.store.close()
        self.store = Store(self.tmp.name)
        self.assertEqual(self.store.channels(True)[0]['cursor'], '105')
        self.assertEqual(self.store.channels(True)[0]['since_id'], '100')

    def test_old_history_cannot_overwrite_new_edit(self):
        self.store.save_message(message(101, content='新版本', edited_at='2026-10-25T02:00:00+00:00'))
        self.store.save_message(message(101, content='旧版本'))
        self.assertEqual(self.store.messages()[0]['content'], '新版本')

    def test_pagination_has_no_duplicates_or_missing_rows(self):
        for mid in range(101, 226):
            self.store.save_message(message(mid))
        first = self.store.messages()
        second = self.store.messages(before=first[-1]['id'])
        third = self.store.messages(before=second[-1]['id'])
        self.assertEqual([m['id'] for m in first + second + third], [str(n) for n in range(225, 100, -1)])

    def test_refresh_signed_media_url_does_not_duplicate_file(self):
        item = {'key': 'attachment-1', 'url': 'https://cdn.discordapp.com/attachments/a?old', 'filename': '../../test.png'}
        self.store.save_message(message(101, media=[item]))
        item['url'] = 'https://cdn.discordapp.com/attachments/a?new'
        self.store.save_message(message(101, media=[item]))
        rows = self.store.db.execute('SELECT * FROM media').fetchall()
        self.assertEqual(len(rows), 1)
        self.assertTrue(rows[0]['url'].endswith('?new'))
        self.assertEqual(len(rows[0]['id']), 64)


class Channel:
    id = 10
    name = '公告'
    guild = None

    def permissions_for(self, member):
        return SimpleNamespace(view_channel=True, read_message_history=True)

    async def history(self, *, limit, after, oldest_first):
        assert oldest_first and limit is None
        for mid in (101, 102, 103):
            if mid > after.id:
                yield discord_message(mid)


class Client:
    def __init__(self):
        channel = Channel()
        self.guild = SimpleNamespace(id=20, name='币风港（离线测试）', me=object(), text_channels=[channel], threads=[])
        self.guild.subscribe = self.subscribe
        channel.guild = self.guild
        self.channel = channel
        self.guilds = [self.guild]

    async def subscribe(self, **kwargs):
        pass

    def get_guild(self, gid):
        return self.guild if gid == 20 else None

    def get_channel(self, cid):
        return self.channel if cid == 10 else None

    def is_ready(self):
        return True


class MonitorTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(self.tmp.name)
        self.store.select_channels([{'id': '10', 'guild_id': '20', 'guild_name': '测试', 'name': '公告'}], '100')
        self.store.set('running', True)
        self.monitor = Monitor(self.store)
        self.monitor.client = Client()
        self.monitor.state = 'connected'

    async def asyncTearDown(self):
        self.store.close()
        self.tmp.cleanup()

    async def test_live_event_does_not_skip_earlier_gap(self):
        self.monitor.ingest(discord_message(103))
        self.assertEqual(self.store.channels(True)[0]['cursor'], '100')
        await self.monitor.reconcile_once()
        self.assertEqual(self.store.stats()['messages'], 3)
        self.assertEqual(self.store.channels(True)[0]['cursor'], '103')
        await self.monitor.reconcile_once()
        self.assertEqual(self.store.stats()['messages'], 3)

    async def test_pause_and_recovery(self):
        self.store.set('running', False)
        self.monitor.ingest(discord_message(101))
        await self.monitor.reconcile_once()
        self.assertEqual(self.store.stats()['messages'], 0)
        self.store.set('running', True)
        await self.monitor.reconcile_once()
        self.assertEqual(self.store.stats()['messages'], 3)

    async def test_permission_failure_does_not_advance_cursor(self):
        async def unavailable(cid):
            raise RuntimeError('sensitive third-party details')
        self.monitor.client.get_channel = lambda cid: None
        self.monitor.client.fetch_channel = unavailable
        await self.monitor.reconcile_once()
        self.assertEqual(self.store.channels(True)[0]['cursor'], '100')
        self.assertNotIn('sensitive', self.store.channels(True)[0]['error'])

    async def test_selection_rejects_unavailable_channels(self):
        with self.assertRaises(ValueError):
            await self.monitor.select(['999'])
        self.assertEqual(self.store.channels(True)[0]['id'], '10')

    async def test_disconnect_event_preserves_login_error(self):
        reader = Reader(self.monitor)
        self.monitor.state = 'error'
        await reader.on_disconnect()
        self.assertEqual(self.monitor.state, 'error')
        self.monitor.state = 'connected'
        await reader.on_disconnect()
        self.assertEqual(self.monitor.state, 'reconnecting')
        await reader.close()

    async def test_media_ssrf_guard_and_failure_state(self):
        for url in ('http://127.0.0.1/a', 'https://cdn.discordapp.com.evil.test/a', 'https://user:pass@cdn.discordapp.com/a', 'file:///etc/passwd'):
            self.assertFalse(allowed_media_url(url))
        self.assertTrue(allowed_media_url('https://cdn.discordapp.com/attachments/1/a.png?ex=123'))
        self.store.save_message(message(101, media=[{'key': '1', 'url': 'http://127.0.0.1/private', 'filename': 'a'}]))
        row = self.store.db.execute('SELECT * FROM media').fetchone()
        class NoNetwork:
            def get(self, *args, **kwargs):
                raise AssertionError('must not fetch private endpoints')
        await self.monitor.download(NoNetwork(), row)
        self.assertEqual(self.store.stats()['failed_files'], 1)
        self.assertEqual(self.store.stats()['files'], 0)

    async def test_media_success_and_oversized_response(self):
        self.store.save_message(message(101, media=[{'key': '1', 'url': 'https://cdn.discordapp.com/a', 'filename': 'a.png', 'mime': 'image/png'}]))
        row = self.store.db.execute('SELECT * FROM media').fetchone()
        class Content:
            async def iter_chunked(self, size):
                yield b'example-image'
        class Response:
            status = 200
            content_length = 13
            headers = {'Content-Type': 'image/png'}
            content = Content()
            async def __aenter__(self):
                return self
            async def __aexit__(self, *args):
                pass
        class Session:
            def get(self, url, *, allow_redirects):
                assert allow_redirects is False
                return Response()
        await self.monitor.download(Session(), row)
        self.assertEqual((self.store.media_dir / row['id']).read_bytes(), b'example-image')
        self.assertEqual(self.store.stats()['files'], 1)
        self.store.save_message(message(102, media=[{'key': '2', 'url': 'https://cdn.discordapp.com/b', 'filename': 'b.png'}]))
        row2 = self.store.db.execute("SELECT * FROM media WHERE message_id='102'").fetchone()
        Response.content_length = MAX_FILE_BYTES + 1
        await self.monitor.download(Session(), row2)
        self.assertEqual(self.store.stats()['failed_files'], 1)


class OfflineMonitor(Monitor):
    """Only used by tests. Never connects to Discord."""
    def __init__(self, store):
        super().__init__(store)
        self.client = Client()
        self.account = {'id': '30', 'name': '离线测试账号'}
        self.state = 'connected'

    async def connect(self, token):
        self.state = 'connecting'


class ApiTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.app = create_app(self.tmp.name, 'http://127.0.0.1:8765', 'test-password-123456', OfflineMonitor, False)
        self.server = TestServer(self.app)
        self.client = TestClient(self.server)
        await self.client.start_server()
        self.origin = str(self.server.make_url('')).rstrip('/')
        self.app[STATE]['origin'] = self.origin
        self.headers = {'Origin': self.origin}

    async def asyncTearDown(self):
        await self.client.close()
        self.tmp.cleanup()

    async def login(self):
        response = await self.client.post('/api/login', json={'password': 'test-password-123456'}, headers=self.headers)
        self.assertEqual(response.status, 200)
        cookie = response.cookies['session']
        self.assertTrue(cookie['httponly'])
        self.assertEqual(cookie['samesite'], 'Strict')

    async def test_login_required_and_csrf_blocked(self):
        self.assertEqual((await self.client.get('/api/messages')).status, 401)
        response = await self.client.post('/api/login', json={'password': 'test-password-123456'})
        self.assertEqual(response.status, 403)
        await self.login()
        self.assertEqual((await self.client.get('/api/status')).status, 200)
        response = await self.client.post('/api/running', json={'running': True}, headers={'Origin': 'https://evil.test'})
        self.assertEqual(response.status, 403)
        self.assertEqual((await self.client.get('/api/status', headers={'Host': 'evil.test'})).status, 403)

    async def test_secret_paths_and_response_redaction(self):
        await self.login()
        for path in ('/data/discord-token.txt', '/data/admin-password.txt', '/.env', '/store.py'):
            self.assertEqual((await self.client.get(path)).status, 404)
        token = 'not-a-real-token-for-offline-tests'
        response = await self.client.post('/api/connect', json={'token': token}, headers=self.headers)
        self.assertEqual(response.status, 202)
        self.assertNotIn(token, await response.text())
        self.assertNotIn(token, await (await self.client.get('/api/status')).text())

    async def test_guild_channel_selection_and_json(self):
        await self.login()
        guilds = await (await self.client.get('/api/guilds')).json()
        self.assertIn('币风港', guilds[0]['name'])
        channels = await (await self.client.get('/api/guilds/20/channels')).json()
        self.assertEqual(channels[0]['id'], '10')
        response = await self.client.post('/api/selection', json={'channels': ['10']}, headers=self.headers)
        self.assertEqual(response.status, 200)
        status = await (await self.client.get('/api/status')).json()
        self.assertTrue(status['running'])
        self.assertEqual(status['channels'][0]['id'], '10')
        self.assertEqual((await self.client.get('/api/messages?before=bad')).status, 400)

    async def test_media_auth_and_active_content_download(self):
        store = self.app[STATE]['store']
        store.select_channels([{'id': '10', 'guild_id': '20', 'guild_name': '测试', 'name': '公告'}], '100')
        store.save_message(message(101, media=[{'key': '1', 'url': 'https://cdn.discordapp.com/a', 'filename': 'test.svg', 'mime': 'image/svg+xml'}]))
        row = store.db.execute('SELECT * FROM media').fetchone()
        (store.media_dir / row['id']).write_text('<svg/>')
        with store.db:
            store.db.execute('UPDATE media SET path=? WHERE id=?', (row['id'], row['id']))
        self.assertEqual((await self.client.get('/media/' + row['id'])).status, 401)
        await self.login()
        response = await self.client.get('/media/' + row['id'])
        self.assertEqual(response.status, 200)
        self.assertTrue(response.headers['Content-Disposition'].startswith('attachment'))
        self.assertEqual(response.content_type, 'application/octet-stream')
        self.assertEqual(response.headers['X-Content-Type-Options'], 'nosniff')

    async def test_login_rate_limit_and_logout(self):
        for _ in range(10):
            self.assertEqual((await self.client.post('/api/login', json={'password': 'bad'}, headers=self.headers)).status, 401)
        self.assertEqual((await self.client.post('/api/login', json={'password': 'bad'}, headers=self.headers)).status, 429)
        self.app[STATE]['failures'].clear()
        await self.login()
        self.assertEqual((await self.client.post('/api/logout', json={}, headers=self.headers)).status, 200)
        self.assertEqual((await self.client.get('/api/status')).status, 401)


if __name__ == '__main__':
    unittest.main()
