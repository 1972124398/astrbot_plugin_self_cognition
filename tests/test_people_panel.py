import copy
import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from astrbot_plugin_self_cognition.cognition import digest, living_context, parse_reflection
from astrbot_plugin_self_cognition.memory import Memory
from astrbot_plugin_self_cognition.store import Store


class PeoplePanelTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.path = Path(self.directory.name) / 'people.sqlite3'
        self.store = Store(self.path)
        self.store.ensure_scope('a', 'test:FriendMessage:a', 'friend')
        self.store.ensure_scope('b', 'test:FriendMessage:b', 'friend')

    def tearDown(self):
        self.store.close()
        self.directory.cleanup()

    def remember(self, key, human, output, scope='a', kind='dialogue'):
        job = dict(id=key, scope=scope, generation=0, actor='user:alice', name='wxid_alice',
                   origin='test:FriendMessage:'+scope, source_kind=kind, human=human, reply='')
        self.store.capture(job)
        known = self.store.context(scope)['entities']
        result = parse_reflection(json.dumps(output, ensure_ascii=False), human,
                                  companion=kind == 'companion', known_entities=known)
        self.assertTrue(self.store.apply(job, result))

    def test_names_titles_and_preferred_name_survive_restart_without_changing_actor(self):
        words = '我叫林夏，昵称小夏，大家叫我林工，以后叫我小夏就好。'
        self.remember('name', words, {'identities': [{'entity': '$speaker', 'name': '林夏',
            'nickname': '小夏', 'titles': ['林工'], 'preferred_name': '小夏', 'evidence': words}]})
        self.store.close()
        self.store = Store(self.path)
        panel = self.store.dashboard('a')
        person = next(p for p in panel['entities'] if p['id'] == 'user:alice')
        self.assertEqual(person['name'], 'wxid_alice')
        self.assertEqual(person['display_name'], '小夏')
        self.assertEqual(person['identity']['name'], '林夏')
        self.assertEqual(person['identity']['titles'], ['林工'])
        self.assertEqual(set(person['aliases']), {'林夏', '小夏', '林工'})
        self.assertEqual(person['identity']['evidence']['name']['quote'], words)
        self.assertEqual(self.store.dashboard('b')['entities'], [])
        self.remember('relation', '小夏和陈然是同事。', {'relations': [{'source': '小夏',
            'target': '陈然', 'label': '同事', 'evidence': '小夏和陈然是同事'}]})
        self.assertEqual(self.store.dashboard('a')['relations'][0]['source'], 'user:alice')

    def test_identity_requires_human_evidence_and_does_not_accept_invented_names(self):
        payload = {'identities': [{'entity': '$speaker', 'name': '王老板', 'nickname': '虚构',
                    'titles': ['不存在'], 'evidence': '我爱喝茶'}]}
        self.remember('invalid', '我爱喝茶', payload)
        self.assertEqual(self.store.dashboard('a')['portraits'], {})
        payload['identities'][0].update(name='小夏', evidence='我叫小夏')
        self.remember('companion', '我叫小夏', payload, kind='companion')
        self.assertFalse(any(p.get('identity') for p in self.store.dashboard('a')['entities']))

    def test_nickname_correction_keeps_old_alias_and_does_not_overwrite_impression(self):
        self.remember('first', '我叫林夏，叫我小林。', {'portrait': {'impression': '很有耐心'},
            'identities': [{'entity': '$speaker', 'name': '林夏', 'preferred_name': '小林',
                            'evidence': '我叫林夏，叫我小林'}]})
        self.remember('correction', '现在叫我小夏。', {'identities': [{'entity': '$speaker',
            'preferred_name': '小夏', 'evidence': '现在叫我小夏'}]})
        panel = self.store.dashboard('a')
        person = next(p for p in panel['entities'] if p['id'] == 'user:alice')
        self.assertEqual(person['display_name'], '小夏')
        self.assertEqual(person['identity']['name'], '林夏')
        self.assertIn('小林', person['aliases'])
        self.assertEqual(panel['portraits']['user:alice']['impression'], '很有耐心')

    def test_panel_returns_all_edges_and_includes_people_mentioned_by_others(self):
        words = '陈然和周宁是朋友。'
        self.remember('named', words, {'relations': [{'source': '陈然', 'target': '周宁',
            'label': '朋友', 'evidence': words}], 'identities': [{'entity': '陈然',
                'nickname': '阿然', 'evidence': '陈然和周宁是朋友，陈然昵称阿然'}]})
        with self.store.db:
            for i in range(65):
                self.store.db.execute('INSERT INTO relations VALUES(?,?,?,?,?,?,?,?,?,?)',
                    ('a', 'named:'+digest('陈然')[:24], 'user:alice', str(i), words, .8, i, 'current', '', 'user:alice'))
        panel = self.store.dashboard('a')
        self.assertEqual(len(panel['relations']), 66)
        self.assertTrue(any(p['name'] == '陈然' and p['kind'] == 'character' for p in panel['entities']))
        self.assertNotIn('阿然', json.dumps(panel, ensure_ascii=False))

    def test_ambiguous_shared_title_does_not_merge_people(self):
        self.remember('one', '陈然和周宁都被叫作老师。', {'identities': [
            {'entity': name, 'titles': ['老师'], 'evidence': '陈然和周宁都被叫作老师'} for name in ('陈然', '周宁')]})
        self.remember('two', '老师和小李是同事。', {'relations': [{'source': '老师', 'target': '小李',
            'label': '同事', 'evidence': '老师和小李是同事'}]})
        relation = self.store.dashboard('a')['relations'][0]
        self.assertNotIn(relation['source'], {'named:'+digest(n)[:24] for n in ('陈然', '周宁')})

    def test_panel_is_read_only_and_identity_is_not_a_dialogue_instruction(self):
        words = '我叫小夏。'
        self.remember('identity', words, {'identities': [{'entity': '$speaker', 'name': '小夏', 'evidence': words}]})
        _, view = self.store.read_view('a')
        cache = Memory(self.store)
        cache.views['a'] = view
        before = copy.deepcopy(view)
        note_before = living_context(cache.recall('a', 'user:alice', '你好', conversation=True), '平台昵称')
        self.store.dashboard('a', '小夏')
        note_after = living_context(cache.recall('a', 'user:alice', '你好', conversation=True), '平台昵称')
        self.assertEqual(view, before)
        self.assertEqual(note_before, note_after)
        self.assertFalse(any('称呼' in line or '小夏' in line for line in note_after))
        self.store.clear('a')
        self.assertEqual(self.store.dashboard('a')['portraits'], {})
        self.assertEqual(self.store.dashboard('a')['aliases'], [])

    def test_mentioned_person_receives_own_portrait_and_alias(self):
        words = '陈然昵称阿然，做事很细心。'
        self.remember('third-party', words, {
            'identities': [{'entity': '陈然', 'nickname': '阿然', 'evidence': words}],
            'people': [{'actor': '陈然', 'portrait': {'impression': '做事很细心'}}]})
        panel = self.store.dashboard('a')
        person = next(p for p in panel['entities'] if p['id'].startswith('named:'))
        self.assertEqual(person['display_name'], '阿然')
        self.assertEqual(panel['portraits'][person['id']]['impression'], '做事很细心')
        self.assertNotIn('identity', panel['portraits'].get('user:alice', {}))

    def test_panel_does_not_truncate_people_at_chat_candidate_limit(self):
        with self.store.db:
            self.store.db.executemany('INSERT INTO entities(scope,id,name,kind) VALUES(?,?,?,?)',
                [('a', 'p'+str(i), '人物'+str(i), 'character') for i in range(225)])
        self.assertEqual(len(self.store.dashboard('a')['entities']), 225)
        self.assertEqual(len(self.store.context('a')['entities']), 200)

    def test_title_is_used_when_no_personal_name_is_known(self):
        self.remember('title-only', '大家都叫我陈老师。', {'identities': [
            {'entity': '$speaker', 'titles': ['陈老师'], 'evidence': '大家都叫我陈老师'}]})
        person = next(p for p in self.store.dashboard('a')['entities'] if p['id'] == 'user:alice')
        self.assertEqual(person['display_name'], '陈老师')

    def test_each_title_keeps_its_original_evidence_when_another_is_added(self):
        self.remember('first-title', '大家叫我林工。', {'identities': [
            {'entity': '$speaker', 'titles': ['林工'], 'evidence': '大家叫我林工'}]})
        self.remember('second-title', '同事也叫我夏老师。', {'identities': [
            {'entity': '$speaker', 'titles': ['夏老师'], 'evidence': '同事也叫我夏老师'}]})
        identity = self.store.dashboard('a')['portraits']['user:alice']['identity']
        self.assertEqual(identity['titles'], ['林工', '夏老师'])
        self.assertEqual(identity['title_evidence']['林工']['quote'], '大家叫我林工')
        self.assertEqual(identity['title_evidence']['夏老师']['quote'], '同事也叫我夏老师')
        self.assertEqual(identity['title_evidence']['林工']['source'], 'first-title')

    def test_forget_does_not_leave_aliases_from_cleared_portraits(self):
        words = '我叫小夏，我一直不吃香菜。'
        result = {'identities': [{'entity': '$speaker', 'name': '小夏', 'evidence': words}],
            'memories': [{'subject': '$speaker', 'key': 'food', 'kind': 'preference',
                          'content': '不吃香菜', 'importance': .8, 'evidence': '我一直不吃香菜'}]}
        self.remember('private-a', words, result)
        self.remember('private-b', words, result, scope='b')
        memory = self.store.dashboard('a')['memories'][0]
        self.assertTrue(self.store.forget_memory('a', memory['id']))
        self.assertEqual(self.store.dashboard('a')['aliases'], [])
        self.assertEqual(self.store.dashboard('a')['portraits'], {})
        self.assertTrue(self.store.dashboard('b')['aliases'])

    def test_legacy_title_evidence_is_preserved_without_inventing_a_source(self):
        self.remember('old', '大家叫我林工。', {'identities': [
            {'entity': '$speaker', 'titles': ['林工'], 'evidence': '大家叫我林工'}]})
        portrait = self.store.dashboard('a')['portraits']['user:alice']
        portrait['identity'].pop('title_evidence')
        portrait['identity']['titles'].append('没有依据的旧称号')
        portrait['identity']['evidence']['titles'] = {'quote': '大家叫我林工', 'source': 'old', 'updated': 1}
        with self.store.db:
            self.store.save_portrait('a', 'user:alice', portrait, 1)
        self.remember('new', '他们也叫我夏老师。', {'identities': [
            {'entity': '$speaker', 'titles': ['夏老师'], 'evidence': '他们也叫我夏老师'}]})
        identity = self.store.dashboard('a')['portraits']['user:alice']['identity']
        self.assertEqual(identity['title_evidence']['林工']['source'], 'old')
        self.assertEqual(identity['title_evidence']['夏老师']['source'], 'new')
        self.assertNotIn('没有依据的旧称号', identity['title_evidence'])

    def test_legacy_aliases_cannot_invent_names_absent_from_human_words(self):
        self.remember('alias', '我叫小夏。', {'aliases': [
            {'entity': '$speaker', 'names': ['小夏', '从未说过的名字']}]})
        self.assertEqual([item['alias'] for item in self.store.dashboard('a')['aliases']], ['小夏'])
        self.remember('invalid-evidence', '还有人叫我夏工。', {'aliases': [
            {'entity': '$speaker', 'names': ['夏工'], 'evidence': '不存在的原话'}]})
        self.assertEqual([item['alias'] for item in self.store.dashboard('a')['aliases']], ['小夏'])
