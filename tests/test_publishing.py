import copy
import datetime as dt
import json
import threading
import unittest
import urllib.error
import urllib.request
from unittest.mock import patch

import test_engine as fixture
from content_engine.documents import DocumentConflict
from content_engine.engine import CN, iso, now
from content_engine.server import make_server


class PublicationTests(unittest.TestCase):
    setUp = fixture.EngineTests.setUp
    tearDown = fixture.EngineTests.tearDown
    make_topic = fixture.EngineTests.make_topic
    result = fixture.EngineTests.result
    make_article = fixture.EngineTests.make_article
    approve = fixture.EngineTests.approve

    def export(self, aid):
        word, receipt = self.engine.publications.prepare_export(aid, {'revision':self.engine.article(aid)['revision']})
        self.assertTrue(word.startswith(b'PK'))
        self.engine.publications.complete_export(aid,receipt)
        return receipt

    def mark(self, aid, **kwargs):
        active=self.engine.publications.summary(aid)['publication']
        body={'action':'confirm','revision':self.engine.article(aid)['revision'],'expected_id':active['id'] if active else None,'expected_version':active['version'] if active else 0}
        return self.engine.publications.change(aid,{**body,**kwargs})

    def test_export_needs_receipt_completion_and_never_means_published(self):
        aid=self.make_article();self.approve(aid);before=self.engine.article(aid)
        tracker=self.engine.publications
        self.assertEqual(tracker.summary(aid)['state'],'draft')
        word,receipt=tracker.prepare_export(aid,{'revision':before['revision']})
        self.assertEqual(tracker.summary(aid)['state'],'draft')
        tracker.complete_export(aid,receipt);tracker.complete_export(aid,receipt)
        self.assertEqual(tracker.summary(aid)['state'],'pending')
        self.assertEqual(len(self.engine.store.all("SELECT * FROM audit WHERE action='article.exported'")),1)
        self.assertEqual(self.engine.article(aid),before)

    def test_failed_export_does_not_create_progress_or_record(self):
        aid=self.make_article()
        with patch.object(self.engine,'export_word',side_effect=ValueError('synthetic failure')):
            with self.assertRaises(ValueError):self.engine.publications.prepare_export(aid,{'revision':1})
        self.assertEqual(self.engine.publications.summary(aid)['state'],'draft')
        self.assertFalse(self.engine.store.all('SELECT * FROM article_exports'))
        with self.assertRaises(DocumentConflict):self.engine.publications.prepare_export(aid,{'revision':0})

    def test_changed_content_during_export_never_records_wrong_revision(self):
        aid=self.make_article()
        def changed(*args):
            self.engine.edit_article(aid,{'base_revision':1,'title':'导出期间发生修改'})
            return b'PK-synthetic-file'
        with patch.object(self.engine,'export_word',side_effect=changed):
            with self.assertRaises(DocumentConflict):self.engine.publications.prepare_export(aid,{'revision':1})
        self.assertFalse(self.engine.store.all('SELECT * FROM article_exports'))

    def test_delayed_old_receipt_never_replaces_a_newer_export_default(self):
        aid=self.make_article()
        _,first=self.engine.publications.prepare_export(aid,{'revision':1})
        self.engine.edit_article(aid,{'base_revision':1,'title':'较新的导出版本'})
        _,second=self.engine.publications.prepare_export(aid,{'revision':2})
        self.engine.publications.complete_export(aid,second)
        summary=self.engine.publications.complete_export(aid,first)
        self.assertEqual(summary['latest_export']['id'],second)
        self.assertEqual(summary['latest_export']['revision'],2)
        self.assertEqual(summary['latest_export']['title'],'较新的导出版本')

    def test_publication_transaction_rolls_back_on_storage_failure(self):
        aid=self.make_article();before=self.engine.article(aid)
        with patch.object(self.engine,'audit',side_effect=RuntimeError('synthetic write failure')):
            with self.assertRaises(RuntimeError):self.mark(aid)
        self.assertEqual(self.engine.publications.summary(aid)['state'],'draft')
        self.assertFalse(self.engine.store.all('SELECT * FROM article_publications'))
        self.assertEqual(self.engine.article(aid),before)

    def test_simultaneous_confirmations_from_two_engines_are_idempotent(self):
        from content_engine.engine import Engine
        aid=self.make_article();other=Engine(self.engine.data_dir,self.folder/'modes')
        barrier=threading.Barrier(2);errors=[]
        def confirm(tracker):
            try:
                barrier.wait(timeout=5)
                tracker.change(aid,{'action':'confirm','revision':1,'expected_id':None,'expected_version':0})
            except Exception as exc:errors.append(exc)
        threads=[threading.Thread(target=confirm,args=(tracker,)) for tracker in (self.engine.publications,other.publications)]
        for thread in threads:thread.start()
        for thread in threads:thread.join(15)
        self.assertFalse(any(thread.is_alive() for thread in threads))
        self.assertFalse(errors,errors)
        self.assertEqual(len(self.engine.store.all('SELECT * FROM article_publications')),1)

    def test_old_export_is_bound_to_snapshot_and_later_edits_stay_local(self):
        aid=self.make_article();receipt=self.export(aid);old=self.engine.article(aid)
        doc=self.engine.document(aid);doc['title']='导出后的手工修改'
        self.engine.save_document(aid,{'base_revision':old['revision'],'document':doc})
        before=self.engine.article(aid)
        summary=self.mark(aid,export_id=receipt)
        self.assertEqual(summary['publication']['revision'],old['revision'])
        self.assertEqual(summary['publication']['title'],old['data']['title'])
        self.assertTrue(summary['has_local_changes'])
        self.assertEqual(self.engine.article(aid),before)
        self.engine.edit_article(aid,{'base_revision':before['revision'],'title':'标记发布后仍可修改'})
        self.assertTrue(self.engine.export_word(aid).startswith(b'PK'))
        self.assertEqual(self.engine.article(aid)['status'],'needs_review')

    def test_manual_confirmation_has_no_required_link_or_actual_publish_time(self):
        aid=self.make_article();summary=self.mark(aid)
        self.assertEqual(summary['state'],'published')
        self.assertIsNone(summary['publication']['published_at'])
        self.assertTrue(summary['publication']['confirmed_at'])
        self.assertEqual(summary['publication']['source'],'manual')
        self.assertEqual(summary['publication']['remote_url'],'')

    def test_double_click_confirmation_is_idempotent(self):
        aid=self.make_article();body={'action':'confirm','revision':1,'expected_id':None,'expected_version':0}
        self.engine.publications.change(aid,body);self.engine.publications.change(aid,body)
        self.assertEqual(len(self.engine.store.all('SELECT * FROM article_publications')),1)
        self.assertEqual(len(self.engine.store.all("SELECT * FROM audit WHERE action='publication.confirm'")),1)

    def test_update_revoke_and_reconfirm_keep_history_without_editing_content(self):
        aid=self.make_article();receipt=self.export(aid);summary=self.mark(aid,export_id=receipt)
        before=self.engine.article(aid);active=summary['publication']
        body={'action':'update','target':'published','expected_id':active['id'],'expected_version':active['version'],'remote_url':'https://www.toutiao.com/article/123/','published_at':iso(now()-dt.timedelta(minutes=30))}
        updated=self.engine.publications.change(aid,body)['publication']
        self.assertEqual(updated['confirmed_at'],active['confirmed_at'])
        self.assertEqual(updated['version'],2)
        with self.assertRaises(DocumentConflict):self.engine.publications.change(aid,{**body,'remote_url':'https://www.toutiao.com/article/456/'})
        revoked=self.engine.publications.change(aid,{'action':'revoke','expected_id':updated['id'],'expected_version':updated['version']})
        self.assertEqual(revoked['state'],'pending')
        self.assertEqual(self.engine.article(aid),before)
        self.mark(aid,export_id=receipt)
        self.assertEqual(len(self.engine.store.all('SELECT * FROM article_publications')),2)

    def test_new_published_version_supersedes_but_does_not_delete_old_record(self):
        aid=self.make_article();first=self.export(aid);self.mark(aid,export_id=first)
        self.engine.edit_article(aid,{'base_revision':1,'title':'第二个已发布版本'})
        second=self.export(aid);summary=self.mark(aid,action='update',export_id=second)
        self.assertEqual(summary['publication']['revision'],2)
        self.assertFalse(summary['has_local_changes'])
        rows=self.engine.store.all('SELECT state FROM article_publications')
        self.assertEqual(sorted(r['state'] for r in rows),['active','superseded'])

    def test_cross_article_receipts_pending_receipts_and_invalid_details_are_rejected(self):
        aid=self.make_article();other=self.make_article('乙');receipt=self.export(aid)
        with self.assertRaises(ValueError):self.engine.publications.complete_export(other,receipt)
        with self.assertRaises(ValueError):self.mark(other,export_id=receipt)
        _,pending=self.engine.publications.prepare_export(aid,{'revision':1})
        with self.assertRaises(ValueError):self.mark(aid,export_id=pending)
        with self.assertRaises(ValueError):self.mark(aid,remote_url='javascript:alert(1)')
        with self.assertRaises(ValueError):self.mark(aid,published_at=iso(now()+dt.timedelta(days=1)))
        demo=self.engine.seed_demo()['article_id']
        with self.assertRaisesRegex(ValueError,'演示'):self.mark(demo)

    def test_legacy_live_record_is_preserved_and_revocation_is_not_resurrected(self):
        aid=self.make_article();article=self.engine.article(aid)
        with self.engine.store.db() as db:
            db.execute("INSERT INTO schedules VALUES('old',?,?,?,?,?,?,?,?,?)",(aid,'main',article['revision_hash'],iso(),'live','https://www.toutiao.com/article/123/','old user confirmation',iso(),iso()))
        summary=self.engine.publications.summary(aid)
        self.assertEqual(summary['publication']['source'],'legacy')
        active=summary['publication']
        summary=self.engine.publications.change(aid,{'action':'revoke','expected_id':active['id'],'expected_version':0})
        self.assertEqual(summary['state'],'draft')
        self.assertIsNone(summary['publication'])
        self.assertEqual(self.engine.store.one("SELECT state FROM schedules WHERE id='old'")['state'],'live')

    def test_http_download_and_metadata_updates_require_local_write_auth(self):
        aid=self.make_article();server=make_server(self.engine,0)
        worker=threading.Thread(target=server.serve_forever,daemon=True);worker.start()
        base='http://127.0.0.1:'+str(server.server_port);token=(self.engine.data_dir/'.local-token').read_text().strip()
        def post(action,body,auth=token):
            return urllib.request.urlopen(urllib.request.Request(base+'/api/articles/'+aid+'/'+action,data=json.dumps(body).encode(),headers={'Content-Type':'application/json','Origin':base,'X-App-Token':auth}))
        try:
            with self.assertRaises(urllib.error.HTTPError) as caught:post('publication',{'action':'confirm','revision':1},'bad')
            self.assertEqual(caught.exception.code,403)
            with post('export-download',{'revision':1}) as response:
                self.assertTrue(response.read().startswith(b'PK'));receipt=response.headers['X-Export-Id']
            self.assertEqual(self.engine.publications.summary(aid)['state'],'draft')
            with post('export-complete',{'receipt':receipt}) as response:self.assertEqual(json.load(response)['state'],'pending')
            with post('publication',{'action':'confirm','export_id':receipt}) as response:self.assertEqual(json.load(response)['state'],'published')
        finally:server.shutdown();server.server_close();worker.join(2)


if __name__=='__main__':unittest.main()
