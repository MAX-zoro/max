import hashlib
import hmac
import json
import os
import re
import secrets
import time
from pathlib import Path
from urllib.parse import quote, urlsplit

from aiohttp import web

from monitor import Monitor, private_write
from store import Store


ROOT = Path(__file__).resolve().parent
STATE = web.AppKey('state', dict)
ID_PATTERN = re.compile(r'^\d{1,20}$')


def fail(message, status=400):
    return web.json_response({'error': message}, status=status)


@web.middleware
async def guard(request, handler):
    state = request.app[STATE]
    origin = state['origin']
    if request.host != urlsplit(origin).netloc:
        return fail('访问地址与 PUBLIC_ORIGIN 配置不一致。', 403)
    if request.method not in ('GET', 'HEAD') and request.headers.get('Origin') != origin:
        return fail('请求来源无效，请从工具网页操作。', 403)
    public = request.path in ('/', '/app.js', '/style.css', '/api/login', '/healthz')
    session = state['sessions'].get(request.cookies.get('session', ''), 0)
    if not public and session < time.time():
        return fail('请先登录工具后台。', 401)
    try:
        response = await handler(request)
    except web.HTTPException as exc:
        response = fail('未找到资源或请求不被允许。', exc.status)
    except (ValueError, TypeError, KeyError, json.JSONDecodeError):
        response = fail('请求格式无效。')
    except Exception:
        response = fail('操作失败，请检查连接状态或服务器磁盘空间。', 500)
    response.headers.update({
        'X-Content-Type-Options': 'nosniff', 'Referrer-Policy': 'no-referrer',
        'X-Frame-Options': 'DENY', 'Cache-Control': 'no-store',
        'Content-Security-Policy': "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self'; media-src 'self'; connect-src 'self'; object-src 'none'; base-uri 'none'; frame-ancestors 'none'; form-action 'self'",
    })
    return response


async def login(request):
    state = request.app[STATE]
    current = time.time()
    # A global cap also works behind a reverse proxy without trusting spoofable client-IP headers.
    state['failures'] = [t for t in state['failures'] if t > current - 60]
    if len(state['failures']) >= 10:
        return fail('登录尝试过多，请一分钟后重试。', 429)
    payload = await request.json()
    password = payload.get('password', '')
    if not isinstance(password, str) or not hmac.compare_digest(hashlib.sha256(password.encode()).digest(), state['password_hash']):
        state['failures'].append(current)
        return fail('后台密码不正确。', 401)
    state['sessions'] = {key: expiry for key, expiry in state['sessions'].items() if expiry > current}
    if len(state['sessions']) >= 100:
        state['sessions'].clear()
    session = secrets.token_urlsafe(32)
    state['sessions'][session] = current + 12 * 3600
    response = web.json_response({'ok': True})
    response.set_cookie('session', session, max_age=12 * 3600, httponly=True,
                        secure=state['origin'].startswith('https://'), samesite='Strict', path='/')
    return response


async def logout(request):
    request.app[STATE]['sessions'].pop(request.cookies.get('session', ''), None)
    response = web.json_response({'ok': True})
    response.del_cookie('session')
    return response


async def status(request):
    state = request.app[STATE]
    monitor, store = state['monitor'], state['store']
    return web.json_response({'state': monitor.state, 'error': monitor.error,
                              'account': monitor.account, 'running': store.get('running', False),
                              'channels': store.channels(True), 'last_message_at': monitor.last_message_at,
                              **store.stats()})


async def connect(request):
    payload = await request.json()
    token = payload.get('token')
    if not isinstance(token, str) or not 20 <= len(token.strip()) <= 2048 or any(ch.isspace() for ch in token.strip()):
        return fail('请填写有效的个人账号 token。')
    await request.app[STATE]['monitor'].connect(token.strip())
    return web.json_response({'ok': True}, status=202)


async def disconnect(request):
    await request.app[STATE]['monitor'].disconnect()
    return web.json_response({'ok': True})


async def guilds(request):
    monitor = request.app[STATE]['monitor']
    if not monitor.ready():
        return fail('Discord 尚未连接，请稍后重试。', 409)
    return web.json_response([{'id': str(g.id), 'name': g.name} for g in monitor.client.guilds])


async def channels(request):
    monitor = request.app[STATE]['monitor']
    gid = request.match_info['guild']
    if not ID_PATTERN.fullmatch(gid):
        return fail('服务器 ID 无效。')
    if not monitor.ready():
        return fail('请先连接 Discord。', 409)
    guild = monitor.client.get_guild(int(gid))
    if not guild:
        return fail('当前账号无法访问该服务器。', 404)
    await guild.subscribe(typing=True, threads=True)
    result = [{'id': str(c.id), 'name': c.name, 'category': getattr(getattr(c, 'category', None), 'name', '') or '',
               'thread': hasattr(c, 'parent_id')} for c in monitor.available_channels(guild)]
    return web.json_response(result)


async def selection(request):
    payload = await request.json()
    ids = payload.get('channels')
    if not isinstance(ids, list) or len(ids) > 200 or any(not isinstance(cid, str) or not ID_PATTERN.fullmatch(cid) for cid in ids):
        return fail('频道选择无效，最多选择 200 个频道。')
    try:
        await request.app[STATE]['monitor'].select(ids)
    except ValueError as exc:
        return fail(str(exc))
    return web.json_response({'ok': True})


async def pause(request):
    payload = await request.json()
    running = payload.get('running')
    if not isinstance(running, bool):
        return fail('同步状态无效。')
    state = request.app[STATE]
    if running and not state['store'].channels(True):
        return fail('请先选择频道。')
    state['store'].set('running', running)
    state['monitor'].wake.set()
    return web.json_response({'ok': True})


async def messages(request):
    channel, before = request.query.get('channel', ''), request.query.get('before', '')
    if any(value and not ID_PATTERN.fullmatch(value) for value in (channel, before)):
        return fail('分页参数无效。')
    items = request.app[STATE]['store'].messages(channel, before)
    return web.json_response({'items': items, 'next_before': items[-1]['id'] if len(items) == 50 else None})


async def media(request):
    key = request.match_info['key']
    if not re.fullmatch(r'[0-9a-f]{64}', key):
        raise web.HTTPNotFound()
    store = request.app[STATE]['store']
    row = store.db.execute('SELECT * FROM media WHERE id=?', (key,)).fetchone()
    if not row or not row['path'] or not (store.media_dir / key).is_file():
        raise web.HTTPNotFound()
    response = web.FileResponse(store.media_dir / key)
    inline_types = {'image/png', 'image/jpeg', 'image/gif', 'image/webp', 'video/mp4', 'video/webm', 'audio/mpeg', 'audio/ogg'}
    response.content_type = row['mime'] if row['mime'] in inline_types else 'application/octet-stream'
    disposition = 'inline' if row['mime'] in inline_types and 'download' not in request.query else 'attachment'
    response.headers['Content-Disposition'] = f"{disposition}; filename*=UTF-8''{quote(row['filename'], safe='')}"
    return response


async def assets(request):
    filename = {'/': 'index.html', '/app.js': 'app.js', '/style.css': 'style.css'}[request.path]
    return web.FileResponse(ROOT / 'static' / filename)


async def health(request):
    return web.json_response({'ok': True})


def create_app(directory, origin, password=None, monitor_factory=Monitor, start_monitor=True):
    origin = origin.rstrip('/')
    parsed = urlsplit(origin)
    if parsed.scheme not in ('http', 'https') or not parsed.netloc or parsed.path or parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ValueError('PUBLIC_ORIGIN 必须是完整网站地址，例如 https://dc.example.com。')
    if parsed.scheme != 'https' and parsed.hostname not in ('127.0.0.1', 'localhost', '::1'):
        raise ValueError('远程访问需要 HTTPS；请通过反向代理配置 PUBLIC_ORIGIN。')
    store = Store(directory)
    password_path = store.directory / 'admin-password.txt'
    if not password:
        if password_path.exists():
            password = password_path.read_text(encoding='utf-8').strip()
        else:
            password = secrets.token_urlsafe(24)
            private_write(password_path, password)
        print(f'后台密码保存在：{password_path.resolve()}', flush=True)
    if len(password) < 12:
        store.close()
        raise ValueError('后台密码至少需要 12 个字符。')
    app = web.Application(middlewares=[guard], client_max_size=8192)
    monitor = monitor_factory(store)
    app[STATE] = {'store': store, 'monitor': monitor, 'origin': origin,
                  'password_hash': hashlib.sha256(password.encode()).digest(), 'sessions': {}, 'failures': []}
    app.add_routes([
        web.get('/', assets), web.get('/app.js', assets), web.get('/style.css', assets),
        web.get('/healthz', health), web.post('/api/login', login), web.post('/api/logout', logout),
        web.get('/api/status', status), web.post('/api/connect', connect), web.post('/api/disconnect', disconnect),
        web.get('/api/guilds', guilds), web.get('/api/guilds/{guild}/channels', channels),
        web.post('/api/selection', selection), web.post('/api/running', pause),
        web.get('/api/messages', messages), web.get('/media/{key}', media),
    ])

    async def lifecycle(app):
        if start_monitor:
            await monitor.start()
        yield
        await monitor.close()
        store.close()

    app.cleanup_ctx.append(lifecycle)
    return app


if __name__ == '__main__':
    port = int(os.environ.get('PORT', '8765'))
    host = os.environ.get('HOST', '127.0.0.1')
    origin = os.environ.get('PUBLIC_ORIGIN', f'http://127.0.0.1:{port}')
    app = create_app(os.environ.get('DATA_DIR', str(ROOT / 'data')), origin, os.environ.get('ADMIN_PASSWORD'))
    print(f'三季 · Discord 信息台：{origin}', flush=True)
    web.run_app(app, host=host, port=port, access_log=None)
