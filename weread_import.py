#!/usr/bin/env python3
"""WeRead official read-only API -> Logseq 2.0.2 native DB properties.

No direct SQLite writes. Preview is the default; --apply makes a graph backup
and appends missing source records. Existing prose/metadata is never replaced.
"""
import argparse
import collections
import datetime as dt
import fcntl
import json
import os
import shutil
from pathlib import Path
import subprocess
import sys
import time
import urllib.error
import urllib.request
import uuid

VERSION = '1.0.5'  # Tencent/WeChatReading skills/SKILL.md
GATEWAY = 'https://i.weread.qq.com/api/agent/gateway'
CLI = shutil.which('logseq') or str(Path.home() / '.local/bin/logseq')
KEY_FILE = Path.home() / '.config/logseq-import/weread-api-key'
ROOT = Path(__file__).resolve().parent
TZ = dt.timezone(dt.timedelta(hours=8))
NAMESPACE = uuid.uuid5(uuid.NAMESPACE_URL, 'https://weread.qq.com/logseq-db-import')

# Reuse migrated properties by display name. New graphs get the same small
# schema. IDs are required for de-duplication and remain hidden native values.
SCHEMA = {
    '书籍id': ('default', 'one', True), '版本': ('number', 'one', True),
    '部分': ('default', 'one', True), '章节id': ('number', 'one', True),
    '划线id': ('default', 'one', True), '想法id': ('default', 'one', True),
    '起始': ('number', 'one', True), '结束': ('default', 'one', True),
    '创建日期': ('date', 'many', False), '作者': ('default', 'one', False),
    '译者': ('default', 'one', False), '分类': ('default', 'one', False),
    '出版社': ('default', 'one', False), '出版日期': ('default', 'one', False),
    'isbn': ('default', 'one', False), '来源': ('node', 'many', False),
    '已读完': ('default', 'one', False), '阅读链接': ('url', 'one', False),
}
CORE = '[:find (pull ?b [:db/id :block/uuid :block/title :block/name :block/parent :block/page :block/order :block/journal-day :logseq.property/value :logseq.property/created-from-property]) :where [?b :block/uuid]]'


def dump(path, data):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, 'w') as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
        f.write('\n')


def text(value):
    if value is None:
        return ''
    return str(value).strip()


def stable_id(kind, *parts):
    return str(uuid.uuid5(NAMESPACE, json.dumps([kind, *map(str, parts)], ensure_ascii=False)))


class Keyword(str):
    pass


class UUID(str):
    pass


def transit(value):
    if isinstance(value, Keyword):
        return '~:' + value
    if isinstance(value, UUID):
        return '~u' + value
    if isinstance(value, dict):
        result = ['^ ']
        for k, v in value.items():
            result += ['~:' + k, transit(v)]
        return result
    if isinstance(value, list):
        return [transit(v) for v in value]
    if isinstance(value, str) and value.startswith(('~', '^', '`')):
        return '~' + value
    return value


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise RuntimeError('微信读书接口发生重定向，已停止，未向其他地址发送密钥。')


class WeRead:
    def __init__(self, key=None):
        self.key = key or os.environ.get('WEREAD_API_KEY') or (KEY_FILE.read_text().strip() if KEY_FILE.exists() else '')
        if not self.key.startswith('wrk-'):
            raise RuntimeError('请先运行「设置微信读书密钥.command」，或设置 WEREAD_API_KEY 环境变量。')
        self.opener = urllib.request.build_opener(NoRedirect())

    def call(self, api, **params):
        payload = {'api_name': api, 'skill_version': VERSION, **params}
        for attempt in range(3):
            req = urllib.request.Request(GATEWAY, json.dumps(payload).encode(), {
                'Content-Type': 'application/json', 'Authorization': 'Bearer ' + self.key,
            }, method='POST')
            try:
                with self.opener.open(req, timeout=40) as response:
                    data = json.load(response)
                break
            except urllib.error.HTTPError as e:
                if (e.code == 429 or e.code >= 500) and attempt < 2:
                    time.sleep(2 ** attempt)
                    continue
                raise RuntimeError(f'微信读书 {api} 返回 HTTP {e.code}；请检查密钥是否有效。') from None
        if data.get('upgrade_info'):
            raise RuntimeError('官方接口要求更新说明版本，已停止导入。请更新 Tencent/WeChatReading 接口适配后再运行。')
        if data.get('errcode', 0) not in (0, '0', None):
            raise RuntimeError(f'微信读书 {api} 调用失败（错误码 {data.get("errcode")}）。')
        data = data.get('data', data)
        if not isinstance(data, dict):
            raise RuntimeError(f'微信读书 {api} 返回格式异常。')
        return data

    def notebooks(self):
        result, seen, cursor = {}, set(), None
        while True:
            params = {'count': 100}
            if cursor is not None:
                params['lastSort'] = cursor
            page = self.call('/user/notebooks', **params)
            books = page.get('books')
            if not isinstance(books, list):
                raise RuntimeError('笔记本概览缺少 books，不能当作空书架继续。')
            for book in books:
                result[text(book['bookId'])] = book
            if not page.get('hasMore'):
                return list(result.values())
            cursor = books[-1].get('sort') if books else None
            if cursor is None or cursor in seen:
                raise RuntimeError('笔记本分页游标没有前进，已停止，避免重复导入。')
            seen.add(cursor)

    def book_notes(self, book_id):
        book_id = text(book_id)
        info = self.call('/book/info', bookId=book_id)
        marks = self.call('/book/bookmarklist', bookId=book_id)
        if not isinstance(marks.get('updated'), list):
            raise RuntimeError('划线接口缺少 updated，不能当作没有划线继续。')
        chapters = {text(c['chapterUid']): c.get('title', '') for c in marks.get('chapters', [])}
        reviews, cursor, seen = {}, 0, set()
        while True:
            page = self.call('/review/list/mine', bookid=book_id, count=100, synckey=cursor)
            if not isinstance(page.get('reviews'), list):
                raise RuntimeError('想法接口缺少 reviews，不能当作没有想法继续。')
            for row in page['reviews']:
                review = row.get('review', row)
                if not review.get('reviewId'):
                    raise RuntimeError('想法缺少唯一 ID；已停止，避免产生无法去重的笔记。')
                reviews[text(review['reviewId'])] = {**review, 'bookId': book_id}
            if not page.get('hasMore'):
                break
            cursor = page.get('synckey')
            if cursor is None or cursor in seen:
                raise RuntimeError('想法分页游标没有前进，已停止。')
            seen.add(cursor)
        bookmarks = []
        for mark in marks['updated']:
            if mark.get('type') == 0:
                continue
            if not mark.get('bookmarkId') or not text(mark.get('markText')):
                raise RuntimeError('划线缺少唯一 ID 或原文；已停止。')
            bookmarks.append({**mark, 'bookId': book_id,
                              'chapterName': chapters.get(text(mark.get('chapterUid')), mark.get('chapterName', ''))})
        for review in reviews.values():
            review.setdefault('chapterName', chapters.get(text(review.get('chapterUid')), ''))
        return {'book': {**marks.get('book', {}), **info, 'bookId': book_id},
                'bookmarks': bookmarks, 'reviews': list(reviews.values())}


class Graph:
    def __init__(self, graph, root=None, cli=CLI):
        self.graph, self.root, self.executable = graph, root, cli
        self.server = next(s for s in self.cli('server', 'list')['servers'] if s['graph'] == graph and s['status'] == 'ready')
        if self.server['revision'] != '533ac43':
            raise RuntimeError('此适配器验证版本为 Logseq 2.0.2 (533ac43)，请先验证新版本 API 再导入。')
        if not self.server['base-url'].startswith('http://127.0.0.1:'):
            raise RuntimeError('仅允许调用本机 Logseq worker。')
        self.refresh()

    def cli(self, *args):
        cmd = [self.executable, *args, '-g', self.graph, '-o', 'json', '--timeout-ms', '120000']
        if self.root:
            cmd += ['--root-dir', str(self.root)]
        p = subprocess.run(cmd, text=True, capture_output=True)
        if p.returncode:
            raise RuntimeError((p.stderr + p.stdout)[:1500])
        result = json.loads(p.stdout)
        if result.get('status') != 'ok':
            raise RuntimeError(str(result.get('error')))
        return result['data']

    def query(self, query):
        return self.cli('query', '--query', query)['result']

    def ops(self, operations):
        body = {'method': 'thread-api/apply-outliner-ops',
                'argsTransit': json.dumps(transit([self.server['repo'], [[Keyword(name), args] for name, args in operations], {}]), ensure_ascii=False)}
        req = urllib.request.Request(self.server['base-url'] + '/v1/invoke', json.dumps(body).encode(), {'Content-Type': 'application/json'}, method='POST')
        with urllib.request.urlopen(req, timeout=120) as response:
            result = json.load(response)
        if result.get('ok') is not True:
            raise RuntimeError(str(result.get('error'))[:1500])

    def get(self, uid):
        rows = self.query('[:find (pull ?b [*]) :where [?b :block/uuid #uuid ' + json.dumps(uid) + ']]')
        return rows[0][0] if rows else None

    def refresh(self):
        self.entities = {b['db/id']: b for b, in self.query(CORE)}
        self.by_uuid = {b['block/uuid']: b for b in self.entities.values()}
        self.properties = {}
        for p, in self.query('[:find (pull ?p [*]) :where [?p :block/tags :logseq.class/Property]]'):
            if p['block/title'] in SCHEMA:
                name = p['block/title']
                if name in self.properties:
                    raise RuntimeError(f'有同名属性「{name}」，需要先确认使用哪一个。')
                self.properties[name] = p
        self.records = {}
        if self.properties:
            clauses = ' '.join('[?b :' + p['db/ident'] + ']' for p in self.properties.values())
            self.records = {b['db/id']: b for b, in self.query('[:find (pull ?b [*]) :where (or ' + clauses + ')]')}
        self.books, self.highlights, self.reviews, self.sections, self.chapters = {}, {}, {}, {}, {}
        for b in self.records.values():
            book_id = self.value(b, '书籍id')
            if book_id is not None and not b.get('block/page'):
                self.unique(self.books, text(book_id), b)
        by_page = {b['db/id']: key for key, b in self.books.items()}
        for b in self.records.values():
            book = by_page.get(b.get('block/page', {}).get('db/id'))
            if book is None:
                continue
            for name, index in [('划线id', self.highlights), ('想法id', self.reviews), ('部分', self.sections), ('章节id', self.chapters)]:
                value = self.value(b, name)
                if value is not None:
                    self.unique(index, (book, text(value)), b)

    @staticmethod
    def unique(index, key, block):
        if key in index and index[key]['block/uuid'] != block['block/uuid']:
            raise RuntimeError(f'发现重复来源 ID {key}，已停止自动导入，避免覆盖。')
        index[key] = block

    def resolve(self, value):
        if isinstance(value, list):
            return [self.resolve(x) for x in value]
        if isinstance(value, dict):
            b = self.entities.get(value['db/id'])
            if b is None:
                rows = self.query(f'[:find (pull ?b [*]) :where [?b :db/id {value["db/id"]}]]')
                b = rows[0][0] if rows else {}
                self.entities[value['db/id']] = b
            return b.get('logseq.property/value', b.get('block/title'))
        return value

    def value(self, block, name):
        prop = self.properties.get(name)
        if not prop:
            return None
        return self.resolve(block.get(prop['db/ident']))

    def ensure_schema(self):
        for name, (typ, cardinality, hide) in SCHEMA.items():
            existing = self.properties.get(name)
            if existing:
                if hide and not existing.get('logseq.property/hide?'):
                    self.cli('upsert', 'property', '--id', str(existing['db/id']), '--hide', 'true')
                continue
            self.cli('upsert', 'property', '--name', name, '--type', typ, '--cardinality', cardinality, '--hide', str(hide).lower())
        self.refresh()

    def property_ops(self, uid, values):
        result = []
        for name, value in values.items():
            if value is None or value == '':
                continue
            prop = self.properties[name]
            typ = prop['logseq.property/type']
            if typ == 'number':
                value = float(value)
                if value.is_integer():
                    value = int(value)
                result.append(['batch-set-property', [[UUID(uid)], Keyword(prop['db/ident']), value, {'entity-id?': False}]])
            else:
                if typ in ('default', 'string', 'url'):
                    value = str(value)
                for val in value if isinstance(value, list) else [value]:
                    result.append(['set-block-property', [UUID(uid), Keyword(prop['db/ident']), val]])
        return result

    def insert(self, parent, title, kind, parts, values=None, heading=False):
        uid = stable_id(kind, *parts)
        existing = self.get(uid)
        if existing:
            return existing
        block = {'block/uuid': UUID(uid), 'block/title': str(title)}
        if heading:
            block['logseq.property/heading'] = 2
        self.ops([['insert-blocks', [[block], UUID(parent['block/uuid']), {'sibling?': False, 'bottom?': True, 'keep-uuid?': True}]],
                  *self.property_ops(uid, values or {})])
        result = self.get(uid)
        if not result or result.get('block/title') != str(title):
            raise RuntimeError('内容写入后校验失败，已停止。')
        self.entities[result['db/id']] = result
        self.by_uuid[uid] = result
        return result

    def page(self, title, kind, parts):
        uid = stable_id(kind, *parts)
        existing = self.get(uid)
        if existing:
            return existing
        matches = [b for b in self.entities.values() if b.get('block/name') and b.get('block/title', '').casefold() == title.casefold()]
        if matches:
            title += '（微信读书 ' + text(parts[0]) + '）'
        self.ops([['create-page', [title, {'uuid': UUID(uid)}]]])
        result = self.get(uid)
        if not result:
            raise RuntimeError('创建书籍页面失败，未继续写入。')
        self.entities[result['db/id']] = result
        return result

    def journal_id(self, timestamp):
        date = dt.datetime.fromtimestamp(float(timestamp), TZ).date()
        day = int(date.strftime('%Y%m%d'))
        rows = self.query(f'[:find ?b :where [?b :block/journal-day {day}]]')
        if not rows:
            self.ops([['create-page', [date.isoformat(), {'journal?': True}]]])
            rows = self.query(f'[:find ?b :where [?b :block/journal-day {day}]]')
        if not rows:
            raise RuntimeError('创建日期节点失败。')
        return rows[0][0]


def category_names(book):
    value = book.get('categories', book.get('category', []))
    if isinstance(value, str):
        return value  # Commas within a label are not separators.
    if isinstance(value, dict):
        return text(value.get('title', value.get('name')))
    return '；'.join(dict.fromkeys(text(x.get('title', x.get('name'))) if isinstance(x, dict) else text(x) for x in value or []))


def note_body(note, review=False):
    if not review:
        return str(note['markText'])
    content, abstract = text(note.get('content')), text(note.get('abstract'))
    if not content:
        raise RuntimeError('想法正文为空，已停止，避免导入空记录。')
    return content + ('\n\n' + '\n'.join('> ' + line for line in abstract.splitlines()) if abstract else '')


def position(note):
    raw = text(note.get('range'))
    parts = raw.split('-')
    if parts and parts[0].isdigit():
        start = int(parts[0])
        return {'起始': start, '结束': parts[1] if len(parts) > 1 and parts[1].isdigit() else str(start)}
    return {}


def preview(graph, bundles):
    counts = collections.Counter(books=0, new_books=0, highlights=0, new_highlights=0, reviews=0, new_reviews=0)
    for bundle in bundles:
        book_id = text(bundle['book']['bookId'])
        counts['books'] += 1
        counts['new_books'] += book_id not in graph.books
        for kind, id_field, index in [('highlights', 'bookmarkId', graph.highlights), ('reviews', 'reviewId', graph.reviews)]:
            notes = bundle['bookmarks' if kind == 'highlights' else 'reviews']
            unique = {text(n[id_field]): n for n in notes if n.get('type') != 0 or kind == 'reviews'}
            for nid, note in unique.items():
                if not nid:
                    raise RuntimeError('缺少来源 ID')
                note_body(note, kind == 'reviews')
                counts[kind] += 1
                counts['new_' + kind] += (book_id, nid) not in index
    return dict(counts)


def apply(graph, bundles):
    graph.ensure_schema()
    result = collections.Counter()
    if any(text(bundle['book']['bookId']) not in graph.books for bundle in bundles):
        graph.cli('upsert', 'tag', '--name', '书')
    # A source node provides provenance, without a noisy link in every excerpt.
    source = next((b for b in graph.entities.values() if b.get('block/name') and b.get('block/title') == '微信读书'), None)
    if not source:
        graph.cli('upsert', 'page', '--page', '微信读书')
        source = graph.query('[:find (pull ?b [*]) :where [?b :block/title "微信读书"] [?b :block/name]]')[0][0]
    for bundle in bundles:
        book = bundle['book']; bid = text(book['bookId'])
        page = graph.books.get(bid)
        if page is None:
            if not text(book.get('title')):
                raise RuntimeError('书名为空，已停止。')
            page = graph.page(text(book['title']), 'book', [bid])
            graph.ops(graph.property_ops(page['block/uuid'], {'书籍id': bid, '来源': source['db/id']}))
            graph.cli('upsert', 'page', '--id', str(page['db/id']), '--update-tags', '["书"]')
            page = graph.get(page['block/uuid']); graph.books[bid] = page
            result['new_books'] += 1
        metadata = {'作者': book.get('author'), '译者': book.get('translator'), '分类': category_names(book),
                    '出版社': book.get('publisher'), '出版日期': book.get('publishTime'), 'isbn': book.get('isbn'),
                    '阅读链接': book.get('readerLink') or book.get('deepLink')}
        metadata = {name: value for name, value in metadata.items() if value and graph.value(page, name) in (None, '', '未知')}
        if metadata:
            graph.ops(graph.property_ops(page['block/uuid'], metadata)); result['metadata_filled'] += len(metadata)

        def section(name):
            key = (bid, name)
            if key not in graph.sections:
                graph.sections[key] = graph.insert(page, name, 'section', [bid, name], {'部分': name}, heading=True)
            return graph.sections[key]

        if text(book.get('intro')) and (bid, '简介') not in graph.sections:
            graph.insert(section('简介'), str(book['intro']), 'intro', [bid])

        for is_review, notes, index, key in [(False, bundle['bookmarks'], graph.highlights, '划线id'), (True, bundle['reviews'], graph.reviews, '想法id')]:
            notes = sorted(notes, key=lambda n: (int(n.get('chapterUid') or 0), position(n).get('起始', 0), text(n.get('bookmarkId', n.get('reviewId')))))
            for note in notes:
                if not is_review and note.get('type') == 0:
                    continue
                nid = text(note.get('reviewId' if is_review else 'bookmarkId'))
                if (bid, nid) in index:
                    result['unchanged_reviews' if is_review else 'unchanged_highlights'] += 1
                    continue
                chapter_id = note.get('chapterUid')
                if chapter_id is None or (is_review and not chapter_id and not text(note.get('chapterName'))):
                    parent = section('想法与书评')
                else:
                    chapter_key = (bid, text(chapter_id))
                    if chapter_key not in graph.chapters:
                        graph.chapters[chapter_key] = graph.insert(section('笔记'), text(note.get('chapterName')) or '章节 ' + text(chapter_id),
                            'chapter', [bid, chapter_id], {'章节id': chapter_id}, heading=True)
                    parent = graph.chapters[chapter_key]
                metadata = {key: nid, **position(note)}
                if note.get('createTime'):
                    metadata['创建日期'] = graph.journal_id(note['createTime'])
                inserted = graph.insert(parent, note_body(note, is_review), 'review' if is_review else 'highlight', [bid, nid], metadata)
                index[(bid, nid)] = inserted
                result['new_reviews' if is_review else 'new_highlights'] += 1
        print('已核对：' + text(book.get('title', bid)), flush=True)
    return dict(result)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--graph', default='logseq_base_v2')
    parser.add_argument('--root-dir', type=Path)
    parser.add_argument('--book-id', action='append', help='Only import this source book; repeatable')
    parser.add_argument('--input', type=Path, help='Previously fetched bundles JSON; no network')
    parser.add_argument('--output-dir', type=Path, default=ROOT / 'runs' / dt.datetime.now(TZ).strftime('%Y%m%d-%H%M%S'))
    parser.add_argument('--apply', action='store_true', help='Backup and append missing records; default is preview')
    args = parser.parse_args()
    lock_dir = args.root_dir or (Path.home() / '.config/logseq-import')
    lock_dir.mkdir(parents=True, exist_ok=True)
    with (lock_dir / 'weread-import.lock').open('w') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise RuntimeError('另一个微信读书导入正在运行。')
        graph = Graph(args.graph, args.root_dir)
        if args.input:
            bundles = json.loads(args.input.read_text())
        else:
            api = WeRead()
            ids = args.book_id or [text(b['bookId']) for b in api.notebooks()]
            bundles = []
            for i, bid in enumerate(ids):
                bundles.append(api.book_notes(bid))
                print(f'已读取 {i + 1}/{len(ids)} 本的划线和个人想法', flush=True)
        dump(args.output_dir / 'source.json', bundles)
        plan = preview(graph, bundles)
        dump(args.output_dir / 'preview.json', plan)
        print(json.dumps(plan, ensure_ascii=False), flush=True)
        if not args.apply:
            print('预览完成，未修改笔记。加 --apply 或运行「同步微信读书.command」后写入。')
            return
        backup = graph.cli('graph', 'backup', 'create', '--name', 'before-weread-import-' + dt.datetime.now(TZ).strftime('%Y%m%d-%H%M%S'))
        dump(args.output_dir / 'backup.json', backup)
        result = apply(graph, bundles)
        graph.refresh()
        remaining = preview(graph, bundles)
        if any(remaining[k] for k in ['new_books', 'new_highlights', 'new_reviews']):
            raise RuntimeError('导入后仍有缺失记录，请检查本次日志。')
        dump(args.output_dir / 'result.json', {'applied': result, 'verified': remaining, 'backup': backup})
        print('完成。正文与技术字段分开存储；再次运行不会重复创建已导入记录。')


if __name__ == '__main__':
    try:
        main()
    except KeyboardInterrupt:
        print('已停止。已写入的记录保留，再次运行会按来源 ID 继续。', file=sys.stderr)
        sys.exit(130)
    except Exception as error:
        print('导入未完成：' + str(error), file=sys.stderr)
        sys.exit(1)
