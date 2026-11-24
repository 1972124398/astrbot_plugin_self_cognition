import tempfile
import time
import unittest
from pathlib import Path

from astrbot_plugin_self_cognition.memory import Memory
from astrbot_plugin_self_cognition.store import Store


class DailyMemoryCache(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.folder.name) / 'cognition.sqlite3')
        self.memory = Memory(self.store)
        await self.memory.call(self.store.ensure_scope, 'scope', 'test:FriendMessage:alice', 'friend')
        await self.memory.call(self.store.touch, 'scope', 'alice', 'Alice', time.time())
        self.payload = {'version': 1, 'context_enabled': True, 'records': [
            {'key': 'tea', 'text': '喜欢喝无糖茶', 'evidence': '我喜欢无糖茶', 'updated': time.time()}]}

    async def asyncTearDown(self):
        self.store.db.close()
        self.folder.cleanup()

    async def test_import_is_retrievable_without_becoming_a_native_memory_or_feeling(self):
        await self.memory.call(self.store.replace_daily, 'scope', 'alice', self.payload, 0)
        await self.memory.refresh('scope')
        rows = self.memory.recall('scope', 'alice', '无糖茶', conversation=True)['memories']
        self.assertEqual(rows[0]['source'], 'daily_routine')
        self.assertEqual(self.store.stats('scope')['memories'], 0)
        self.assertEqual(self.store.stats('scope')['feelings'], 0)
        self.assertFalse(self.memory.views['scope']['memories'])

    async def test_disabled_frontend_and_another_actor_do_not_receive_imported_knowledge(self):
        self.payload['context_enabled'] = False
        await self.memory.call(self.store.replace_daily, 'scope', 'alice', self.payload, 0)
        await self.memory.refresh('scope')
        self.assertFalse(self.memory.recall('scope', 'alice', '喝茶', conversation=True)['memories'])
        self.assertFalse(self.memory.recall('scope', 'bob', '喝茶')['memories'])
        self.assertTrue(self.memory.recall('scope', 'alice', '喝茶')['memories'])

    async def test_clear_blocks_old_records_but_accepts_new_source_records(self):
        await self.memory.call(self.store.replace_daily, 'scope', 'alice', self.payload, 0)
        await self.memory.call(self.store.clear, 'scope')
        self.payload['records'].append({'key': 'meal', 'text': '早餐喝牛奶', 'updated': time.time()})
        await self.memory.call(self.store.replace_daily, 'scope', 'alice', self.payload, 1)
        await self.memory.refresh('scope')
        rows = self.memory.recall('scope', 'alice')['memories']
        self.assertEqual([r['slot'] for r in rows], ['meal'])
