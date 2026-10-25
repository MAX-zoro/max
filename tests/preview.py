"""Disposable browser-test fixture; only synthetic messages, no Discord network access."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from aiohttp import web
from app import STATE, create_app
from test_tool import OfflineMonitor, message


if __name__ == '__main__':
    app = create_app('outputs/browser-test-data', 'http://127.0.0.1:8766',
                     'offline-browser-test-only', OfflineMonitor, False)
    store = app[STATE]['store']
    store.select_channels([{'id': '10', 'guild_id': '20', 'guild_name': '币风港（离线测试）', 'name': '公告'}], '100')
    store.set('running', True)
    store.save_message(message(101, content='离线测试消息：用于验证页面显示，不来自真实 Discord 频道。'))
    store.save_message(message(102, content='<script>window.__unsafe = true</script>\n这段内容必须按文字显示，不能执行。',
                               embeds=[{'title': '离线嵌入内容测试', 'description': '图片、附件、消息正文分别保存。', 'url': 'javascript:alert(1)'}]))
    web.run_app(app, host='127.0.0.1', port=8766, access_log=None)
