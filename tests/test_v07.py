import copy
import io
import json
import threading
import unittest
import zipfile
import xml.etree.ElementTree as ET
from pathlib import Path
from unittest.mock import patch

import test_upgrade as fixture
from content_engine.documents import DocumentConflict, cover_id, reader_blocks
from content_engine.ai import SecretKey


class DocumentTests(unittest.TestCase):
    setUp = fixture.PhotoAndDayTests.setUp
    tearDown = fixture.PhotoAndDayTests.tearDown
    make_article = fixture.PhotoAndDayTests.make_article
    make_topic = fixture.PhotoAndDayTests.make_topic
    result = fixture.PhotoAndDayTests.result
    save_photo = fixture.PhotoAndDayTests.save_photo

    def prepared(self):
        aid = self.make_article()
        first = self.save_photo(aid)
        original = self.engine.store.one('SELECT * FROM photo_assets WHERE id=?',(first,))
        import uuid
        second = uuid.uuid4().hex
        original.update(id=second,image_url='https://example.org/second.png')
        with self.engine.store.db() as db:
            db.execute('INSERT INTO photo_assets VALUES('+','.join('?' for _ in original)+')',tuple(original.values()))
        return aid, first, second, self.engine.document(aid)

    def save(self, aid, document, revision=None):
        return self.engine.save_document(aid, {'base_revision': revision or self.engine.article(aid)['revision'], 'document': document})

    def test_legacy_projection_is_stable_and_never_writes_until_save(self):
        aid, first, second, doc = self.prepared()
        before = self.engine.article(aid)
        self.assertEqual(doc, self.engine.document(aid))
        self.assertNotIn('document', before['data'])
        self.save(aid, doc)
        self.assertEqual(self.engine.document(aid), doc)
        self.assertEqual(self.engine.article(aid)['data']['paragraphs'], before['data']['paragraphs'])
        self.assertEqual(len(self.engine.photos.list(aid)), 2)

    def test_order_cover_captions_and_removal_are_one_revision(self):
        aid, first, second, doc = self.prepared()
        images = [b for b in doc['blocks'] if b['type']=='image']
        texts = [b for b in doc['blocks'] if b['type']=='text']
        doc['blocks'] = [images[1], texts[0], images[0], *texts[1:]]
        images[1]['caption'] = '新的正常图注'
        doc['cover'] = {'mode':'auto','asset_id':None}
        self.save(aid,doc)
        self.assertEqual(cover_id(self.engine.document(aid)), second)
        self.assertEqual(self.engine.photos.cover_asset(aid)['id'], second)
        doc['cover']={'mode':'manual','asset_id':first}
        self.save(aid,doc)
        doc['blocks'].reverse()
        self.save(aid,doc)
        self.assertEqual(cover_id(self.engine.document(aid)), first)
        doc['blocks']=[b for b in doc['blocks'] if b.get('asset_id')!=first]
        result=self.save(aid,doc)
        self.assertIn('恢复首图',result['warning'])
        self.assertEqual(self.engine.document(aid)['cover']['mode'],'auto')
        self.assertEqual(self.engine.photos.asset(first)['selected'],0)
        self.assertEqual(len(self.engine.photos.list(aid)),2)
        # Restore removed material without a photo-specific save call.
        doc=result['document'];doc['blocks'].append(images[0]);self.save(aid,doc)
        self.assertEqual(len(self.engine.photos.selected(aid)),2)

    def test_invalid_foreign_duplicate_or_stale_save_is_atomic(self):
        aid, first, second, doc=self.prepared()
        current=self.engine.article(aid)
        invalid=copy.deepcopy(doc);invalid['blocks'].append(copy.deepcopy(invalid['blocks'][0]))
        with self.assertRaises(ValueError):self.save(aid,invalid)
        self.assertEqual(self.engine.article(aid),current)
        self.save(aid,doc)
        stale=copy.deepcopy(doc);stale['title']='另一个窗口的编辑不能覆盖'
        with self.assertRaises(DocumentConflict):self.save(aid,stale,current['revision'])
        self.assertEqual(self.engine.document(aid)['title'],doc['title'])
        self.assertEqual(len(self.engine.store.all('SELECT * FROM revisions WHERE article_id=?',(aid,))),2)

    def test_html_docx_reader_share_order_original_ratio_and_clean_captions(self):
        aid, first, second, doc=self.prepared()
        images=[b for b in doc['blocks'] if b['type']=='image'];texts=[b for b in doc['blocks'] if b['type']=='text']
        texts[0]['text']='开篇检查顺序';texts[1]['text']='末段检查顺序'
        images[0]['caption']='原报道配图（赛事、人物及拍摄时间请在终审核对）'
        images[1]['caption']='第二张正常图注'
        doc['blocks']=[images[1],texts[0],images[0],texts[1]]
        result=self.save(aid,doc)
        with zipfile.ZipFile(io.BytesIO(self.engine.export_article(aid))) as bundle:
            html=bundle.read('article.html').decode();word=bundle.read('article.docx')
            self.assertLess(html.index('第二张正常图注'),html.index('开篇检查顺序'))
            self.assertNotIn('终审核对',html);self.assertNotIn('转载依据',html);self.assertNotIn('AI 辅助创作',html)
            self.assertIn('source_url',bundle.read('图片来源与署名.json').decode())
        with zipfile.ZipFile(io.BytesIO(word)) as bundle:
            root=ET.fromstring(bundle.read('word/document.xml'));ns={'w':'http://schemas.openxmlformats.org/wordprocessingml/2006/main','wp':'http://schemas.openxmlformats.org/drawingml/2006/wordprocessingDrawing'}
            text=''.join(root.itertext());self.assertLess(text.index('第二张正常图注'),text.index('开篇检查顺序'))
            self.assertNotIn('终审核对',text);self.assertEqual(len([n for n in bundle.namelist() if n.startswith('word/media/')]),2)
            for extent in root.findall('.//wp:extent',ns):self.assertAlmostEqual(int(extent.attrib['cx'])/int(extent.attrib['cy']),640/360,places=5)
            for paragraph in root.findall('.//w:p',ns):
                if paragraph.find('.//w:drawing',ns) is not None:
                    spacing=paragraph.find('./w:pPr/w:spacing',ns)
                    self.assertIsNotNone(spacing)
                    self.assertEqual(spacing.attrib['{'+ns['w']+'}lineRule'],'auto')
        with self.assertRaises(DocumentConflict):self.engine.export_word(aid, result['revision']-1)

    def test_missing_image_requires_explicit_skip_but_never_approval(self):
        aid,first,second,doc=self.prepared();self.save(aid,doc)
        asset=self.engine.photos.asset(first);(self.engine.data_dir/'photos'/asset['local_name']).write_bytes(b'bad')
        with self.assertRaisesRegex(ValueError,'跳过'):self.engine.export_word(aid)
        self.assertTrue(self.engine.export_word(aid,skip_missing=True).startswith(b'PK'))

    def test_ai_suggestion_cannot_overwrite_concurrent_human_edit(self):
        aid,first,second,doc=self.prepared();old=self.engine.article(aid)
        suggestion=self.engine.suggest_revision(aid,'synthetic-run',old['revision'],{**old['data'],'title':'AI 提议的标题'}, {'level':'standard'})
        self.assertEqual(self.engine.article(aid),old)
        doc['title']='我的手动标题';self.save(aid,doc)
        with self.assertRaises(DocumentConflict):self.engine.apply_suggestion(aid,suggestion,{'base_revision':old['revision']+1})
        self.assertEqual(self.engine.document(aid)['title'],'我的手动标题')
        self.assertEqual(len(self.engine.suggestions(aid)),1)

    def test_40_threshold_is_readable_advisory_and_exports_without_review(self):
        aid=self.make_article();article=self.engine.article(aid);tid=article['topic_id'];topic=self.engine.topic(tid)
        topic['data']['facts'][0]['text']='第一场42分，第二场45分'
        with self.engine.store.db() as db:db.execute('UPDATE topics SET data=? WHERE id=?',(json.dumps(topic['data']),tid))
        doc=self.engine.document(aid);doc['title']='两场得分40以上';doc['blocks'][0]['text']='两场得分突破40，但不是每场都得到相同的分数。'
        self.save(aid,doc)
        quality=self.engine.quality(aid)
        self.assertFalse(quality['errors']);self.assertNotIn('4E+1',json.dumps(quality))
        self.assertFalse(any('数字' in item and '40' in item for item in quality['warnings']))
        self.assertTrue(self.engine.export_word(aid).startswith(b'PK'))

    def test_permission_failure_status_does_not_break_existing_export(self):
        aid=self.make_article()
        with patch.object(self.engine.models.secret,'get',side_effect=PermissionError('synthetic lock')):
            self.assertEqual(self.engine.models.secret.status()['error_code'],'credential_permission')
            self.assertIn('deepseek',self.engine.models.status('main'))
            self.assertTrue(self.engine.export_word(aid).startswith(b'PK'))

    def test_transaction_failure_rolls_back_document_photo_state_and_revision(self):
        aid,first,second,doc=self.prepared()
        before=self.engine.article(aid);photos=self.engine.photos.list(aid)
        doc['blocks']=[b for b in doc['blocks'] if b.get('asset_id')!=first]
        with patch.object(self.engine,'audit',side_effect=RuntimeError('synthetic database failure')):
            with self.assertRaises(RuntimeError):self.save(aid,doc)
        self.assertEqual(self.engine.article(aid),before)
        self.assertEqual(self.engine.photos.list(aid),photos)
        self.assertEqual(len(self.engine.store.all('SELECT * FROM revisions WHERE article_id=?',(aid,))),1)

    def test_http_whole_document_save_conflict_is_409(self):
        import urllib.request,urllib.error
        from content_engine.server import make_server
        aid=self.make_article();doc=self.engine.document(aid)
        server=make_server(self.engine,0);thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
        url='http://127.0.0.1:'+str(server.server_port)
        token=(self.engine.data_dir/'.local-token').read_text().strip()
        def post(body):
            req=urllib.request.Request(url+'/api/articles/'+aid+'/document',data=json.dumps(body).encode(),headers={'X-App-Token':token,'Content-Type':'application/json','Origin':url})
            return urllib.request.urlopen(req)
        try:
            with post({'base_revision':1,'document':doc}) as res:self.assertEqual(json.load(res)['revision'],2)
            with self.assertRaises(urllib.error.HTTPError) as caught:post({'base_revision':1,'document':doc})
            self.assertEqual(caught.exception.code,409)
            self.assertEqual(json.load(caught.exception)['code'],'revision_conflict')
        finally:server.shutdown();server.server_close();thread.join(2)

    def test_transient_credential_lock_retries_and_permanent_lock_is_clear(self):
        secret=SecretKey(self.folder);secret.set('synthetic-test-key-for-v07')
        original=Path.read_bytes;attempts=[]
        def read(path):
            if path==secret.path:
                attempts.append(1)
                if len(attempts)<3:raise PermissionError('synthetic sharing violation')
            return original(path)
        with patch.object(Path,'read_bytes',read):self.assertEqual(secret.get(),'synthetic-test-key-for-v07')
        self.assertEqual(len(attempts),3)
        with patch.object(Path,'read_bytes',side_effect=PermissionError('lock')):
            self.assertEqual(secret.status()['error_code'],'credential_permission')


if __name__=='__main__':unittest.main()
