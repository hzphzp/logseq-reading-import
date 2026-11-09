import copy
import json
from pathlib import Path
import subprocess
import tempfile
import unittest

from weread_import import Graph, WeRead, apply, category_names, note_body, preview, CLI


FIXTURE = [{'book': {'bookId': 'fixture-001', 'title': '导入集成测试', 'author': 'Test Author',
                     'intro': '保留简介正文。', 'category': 'Computer Science - Distributed, Parallel, and Cluster Computing'},
            'bookmarks': [{'bookId': 'fixture-001', 'bookmarkId': 'mark-001', 'type': 1,
                           'chapterUid': 1, 'chapterName': '第一章', 'markText': '保留完整划线，含 [[用户主动引用]]。',
                           'createTime': 1793750400, 'range': '15-32'}],
            'reviews': [{'bookId': 'fixture-001', 'reviewId': 'review-001', 'content': '整本书的个人感想。',
                         'createTime': 1793750400},
                        {'bookId': 'fixture-001', 'reviewId': 'review-002', 'content': '带原文的想法。',
                         'abstract': '原文第一行\n原文第二行', 'chapterUid': 1, 'chapterName': '第一章', 'range': '15-32'}]}]


class GatewayTests(unittest.TestCase):
    def test_notebook_cursor_and_deduplication(self):
        class Fake:
            notebooks = WeRead.notebooks
            def __init__(self): self.calls = []
            def call(self, api, **params):
                self.calls.append(params)
                return {'books': [{'bookId': '1', 'sort': 8}], 'hasMore': 1} if len(self.calls) == 1 else {'books': [{'bookId': '1', 'sort': 8}, {'bookId': '2', 'sort': 7}], 'hasMore': 0}
        api = Fake()
        self.assertEqual([x['bookId'] for x in api.notebooks()], ['1', '2'])
        self.assertEqual(api.calls[1], {'count': 100, 'lastSort': 8})

    def test_stuck_cursor_fails(self):
        class Fake:
            notebooks = WeRead.notebooks
            def call(self, api, **params): return {'books': [{'bookId': '1', 'sort': 8}], 'hasMore': 1}
        with self.assertRaises(RuntimeError): Fake().notebooks()

    def test_both_highlights_and_all_personal_reviews(self):
        class Fake:
            book_notes = WeRead.book_notes
            def call(self, api, **params):
                if api == '/book/info': return {'title': '书'}
                if api == '/book/bookmarklist':
                    return {'updated': [{'bookmarkId': 'm', 'markText': '摘录', 'type': 1, 'chapterUid': 2},
                                        {'bookmarkId': 'bookmark', 'type': 0}], 'chapters': [{'chapterUid': 2, 'title': '二'}]}
                return {'reviews': [{'review': {'reviewId': str(params['synckey']), 'content': '感想'}}],
                        'hasMore': int(params['synckey'] == 0), 'synckey': 4}
        result = Fake().book_notes('b')
        self.assertEqual(len(result['bookmarks']), 1)
        self.assertEqual(len(result['reviews']), 2)
        self.assertEqual(result['bookmarks'][0]['chapterName'], '二')

    def test_plain_template_and_taxonomy(self):
        self.assertEqual(category_names(FIXTURE[0]['book']), FIXTURE[0]['book']['category'])
        self.assertEqual(note_body(FIXTURE[0]['reviews'][0], True), '整本书的个人感想。')
        self.assertNotIn('None', note_body(FIXTURE[0]['reviews'][0], True))
        self.assertEqual(note_body(FIXTURE[0]['reviews'][1], True), '带原文的想法。\n\n> 原文第一行\n> 原文第二行')


class IntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.root = Path(tempfile.mkdtemp(prefix='logseq-reading-test-'))
        def cli(*args):
            return json.loads(subprocess.check_output([CLI, *args, '--root-dir', str(cls.root), '-g', 'reading-test', '-o', 'json', '--timeout-ms', '120000'], text=True))['data']
        cls.cli = staticmethod(cli)
        cli('graph', 'create'); cli('upsert', 'page', '--page', 'Independent test graph')
        cls.graph = Graph('reading-test', cls.root)

    @classmethod
    def tearDownClass(cls):
        cls.cli('server', 'stop')
        print('Isolated graph:', cls.root)

    def test_native_import_repeat_and_user_edits(self):
        graph = self.graph
        self.assertEqual(preview(graph, FIXTURE)['new_highlights'], 1)
        result = apply(graph, copy.deepcopy(FIXTURE))
        self.assertEqual(result['new_highlights'], 1)
        self.assertEqual(result['new_reviews'], 2)
        graph.refresh()
        mark = graph.highlights[('fixture-001', 'mark-001')]
        self.assertEqual(mark['block/title'], FIXTURE[0]['bookmarks'][0]['markText'])
        self.assertEqual(graph.value(mark, '划线id'), 'mark-001')
        self.assertEqual(graph.value(mark, '起始'), 15)
        self.assertEqual(graph.value(mark, '结束'), '32')
        self.assertTrue(graph.properties['划线id']['logseq.property/hide?'])
        self.assertEqual(graph.value(graph.books['fixture-001'], '分类'), FIXTURE[0]['book']['category'])
        self.assertTrue(graph.value(mark, '创建日期'))
        self.assertEqual(graph.sections[('fixture-001', '简介')]['block/title'], '简介')
        before_ids = set(graph.by_uuid)
        graph.cli('upsert', 'block', '--uuid', mark['block/uuid'], '--content', '我在 Logseq 追加了自己的理解。')
        graph.refresh()
        again = apply(graph, copy.deepcopy(FIXTURE))
        graph.refresh()
        self.assertEqual(again.get('new_highlights', 0), 0)
        self.assertEqual(set(graph.by_uuid), before_ids)
        self.assertEqual(graph.get(mark['block/uuid'])['block/title'], '我在 Logseq 追加了自己的理解。')
        self.assertEqual(preview(graph, FIXTURE)['new_reviews'], 0)
        validation = graph.cli('graph', 'validate')
        self.assertFalse(validation.get('errors'))
        self.assertFalse(validation.get('invalid-entity-ids'))


if __name__ == '__main__': unittest.main(verbosity=2)
