"""Local archive. Discord snowflakes stay strings at the JSON boundary."""
import hashlib
import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path


def now():
    return datetime.now(timezone.utc).isoformat()


class Store:
    def __init__(self, directory):
        self.directory = Path(directory)
        self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.media_dir = self.directory / "media"
        self.media_dir.mkdir(exist_ok=True, mode=0o700)
        self.db = sqlite3.connect(self.directory / "archive.sqlite3")
        self.db.row_factory = sqlite3.Row
        self.db.executescript("""
            PRAGMA journal_mode=WAL;
            CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS channels (
                id TEXT PRIMARY KEY, guild_id TEXT, guild_name TEXT, name TEXT,
                since_id TEXT, cursor TEXT, enabled INTEGER DEFAULT 1, error TEXT DEFAULT '', checked TEXT
            );
            CREATE TABLE IF NOT EXISTS messages (
                id TEXT PRIMARY KEY, channel_id TEXT NOT NULL, body TEXT NOT NULL, saved TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS messages_channel ON messages(channel_id, id);
            CREATE TABLE IF NOT EXISTS media (
                id TEXT PRIMARY KEY, message_id TEXT, url TEXT, filename TEXT, mime TEXT,
                path TEXT DEFAULT '', error TEXT DEFAULT '', attempts INTEGER DEFAULT 0,
                retry_at REAL DEFAULT 0
            );
        """)

    def get(self, key, default=None):
        row = self.db.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
        return json.loads(row[0]) if row else default

    def set(self, key, value):
        with self.db:
            self.db.execute("INSERT OR REPLACE INTO settings VALUES (?,?)", (key, json.dumps(value)))

    def channels(self, enabled=False):
        return [dict(r) for r in self.db.execute("SELECT * FROM channels" + (" WHERE enabled=1" if enabled else ""))]

    def select_channels(self, channels, since_id):
        with self.db:
            self.db.execute("UPDATE channels SET enabled=0")
            for c in channels:
                # Re-enabling an old channel preserves its cursor so paused messages can be recovered.
                self.db.execute("""INSERT INTO channels (id,guild_id,guild_name,name,since_id,cursor)
                    VALUES (?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET
                    enabled=1,name=excluded.name,guild_name=excluded.guild_name""",
                    (c['id'], c['guild_id'], c['guild_name'], c['name'], since_id, since_id))

    def save_message(self, body):
        cid, mid = body['channel_id'], body['id']
        channel = self.db.execute("SELECT * FROM channels WHERE id=? AND enabled=1", (cid,)).fetchone()
        if not channel or int(mid) <= int(channel['since_id']):
            return False
        old = self.db.execute("SELECT body FROM messages WHERE id=?", (mid,)).fetchone()
        # A delayed history response must not replace a more recent edit.
        if old and (json.loads(old[0]).get('edited_at') or '') > (body.get('edited_at') or ''):
            return False
        with self.db:
            self.db.execute("INSERT OR REPLACE INTO messages VALUES (?,?,?,?)", (mid, cid, json.dumps(body, ensure_ascii=False), now()))
            for item in body.get('media', []):
                key = hashlib.sha256(f"{mid}:{item['key']}".encode()).hexdigest()
                self.db.execute("""INSERT INTO media (id,message_id,url,filename,mime) VALUES (?,?,?,?,?)
                    ON CONFLICT(id) DO UPDATE SET url=excluded.url""",
                    (key, mid, item['url'], item['filename'], item.get('mime', 'application/octet-stream')))
        return not bool(old)

    def progress(self, cid, cursor=None, error=''):
        with self.db:
            self.db.execute("UPDATE channels SET cursor=COALESCE(?,cursor), error=?, checked=? WHERE id=?",
                            (cursor, error, now(), cid))

    def messages(self, channel='', before='', limit=50):
        conditions, args = [], []
        if channel:
            conditions.append('channel_id=?')
            args.append(channel)
        if before:
            conditions.append('CAST(id AS INTEGER) < CAST(? AS INTEGER)')
            args.append(before)
        where = ' WHERE ' + ' AND '.join(conditions) if conditions else ''
        rows = self.db.execute('SELECT * FROM messages' + where + ' ORDER BY CAST(id AS INTEGER) DESC LIMIT ?', (*args, limit)).fetchall()
        result = []
        for r in rows:
            body = json.loads(r['body'])
            body['files'] = [dict(x) for x in self.db.execute('SELECT id,filename,mime,path,error FROM media WHERE message_id=?', (r['id'],))]
            result.append(body)
        return result

    def stats(self):
        return {
            'messages': self.db.execute('SELECT COUNT(*) FROM messages').fetchone()[0],
            'files': self.db.execute("SELECT COUNT(*) FROM media WHERE path!=''").fetchone()[0],
            'pending_files': self.db.execute("SELECT COUNT(*) FROM media WHERE path='' AND error='' ").fetchone()[0],
            'failed_files': self.db.execute("SELECT COUNT(*) FROM media WHERE path='' AND error!='' ").fetchone()[0],
        }

    def close(self):
        self.db.close()
