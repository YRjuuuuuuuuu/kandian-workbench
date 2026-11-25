import copy
import io
import json
import unittest
import zipfile
import xml.etree.ElementTree as ET
import threading
import urllib.request

import test_agent as agent_fixture
import test_upgrade as media_fixture
from content_engine.content_depth import normalize, plan
from content_engine import batch_production
from content_engine.word_export import publication_caption


class WritingDepthTests(unittest.TestCase):
    setUp = agent_fixture.AgentTests.setUp
    tearDown = agent_fixture.AgentTests.tearDown
    discover = agent_fixture.AgentTests.discover

    def test_depth_choice_survives_batch_and_later_default_changes(self):
        found = self.discover()
        cid = found['result']['candidate_ids'][0]
        result = batch_production.submit(self.agent, {'account_id':'main','candidate_ids':[cid],'content_depth':'rich'})
        self.agent.preferences({'account_id':'main','content_depth':'compact'})
        run = self.agent.run(result['submitted'][0]['run_id'])
        self.assertEqual(run['payload']['config']['content_depth'],'rich')
        self.assertEqual(self.agent.config('main')['content_depth'],'compact')
        self.agent.tick(schedule=False)
        produced = self.agent.run(run['id'])
        self.assertEqual(produced['state'],'needs_review', produced['error'])
        article = self.engine.article(produced['result']['article_id'])
        self.assertEqual(article['data']['writing_policy']['level'],'rich')
        job = json.loads(self.engine.store.one('SELECT payload FROM jobs WHERE result=?',(article['id'],))['payload'])
        self.assertEqual(job['writing_policy']['level'],'rich')
        self.assertEqual(job['template']['min_chars'],article['data']['writing_policy']['target_min_chars'])
        writing = next(c['prompt'] for c in self.provider.calls if c['stem']=='writing')
        self.assertIn('writing_policy',writing)
        self.assertIn('丰富',writing)

    def test_manual_edit_preserves_depth_and_revision_can_change_it(self):
        found = self.discover()
        chosen = self.agent.decide(found['result']['candidate_ids'][0],{'decision':'select','content_depth':'rich'})
        self.agent.tick(schedule=False)
        aid = self.agent.run(chosen['id'])['result']['article_id']
        article = self.engine.article(aid)
        self.engine.edit_article(aid,{'base_revision':article['revision'],'title':'合成稿修改标题'})
        self.assertEqual(self.engine.article(aid)['data']['writing_policy']['level'],'rich')
        current = self.engine.article(aid)
        run = self.agent.create('revise',{'account_id':'main','article_id':aid,'base_revision':current['revision'],'feedback':'扩充已有事实的解释','content_depth':'standard'})
        self.agent.tick(schedule=False)
        revised = self.agent.run(run['id'])
        self.assertEqual(revised['state'],'needs_review', revised['error'])
        suggestion=self.engine.suggestions(aid)[0]
        self.engine.apply_suggestion(aid,suggestion['id'],{'base_revision':current['revision']})
        self.assertEqual(self.engine.article(aid)['data']['writing_policy']['level'],'standard')
        self.assertIn('writing_policy',next(c['prompt'] for c in self.provider.calls if c['stem']=='revision'))

    def test_overly_short_draft_gets_one_bounded_expansion(self):
        self.agent.preferences({'account_id':'main','production_mode':'fast'})
        found=self.discover()
        cid=found['result']['candidate_ids'][0]
        candidate=self.agent.candidate(cid)
        f=candidate['data']['facts'][0]
        candidate['data']['facts'] += [{**copy.deepcopy(f),'id':'f'+str(n),'text':f['text']+f'合成观察条目{n}。'} for n in range(2,6)]
        with self.engine.store.db() as db:
            db.execute('UPDATE agent_candidates SET data=? WHERE id=?',(json.dumps(candidate['data']),cid))
        created=self.agent.decide(cid,{'decision':'select','content_depth':'rich'})
        self.agent.tick(schedule=False)
        result=self.agent.run(created['id'])
        self.assertEqual(result['state'],'needs_review',result['error'])
        self.assertEqual(sum(c['stem']=='expansion' for c in self.provider.calls),1)
        self.assertIsNone(self.engine.article(result['result']['article_id'])['approval_hash'])

    def test_invalid_choice_is_rejected_before_creating_run(self):
        count=len(self.engine.store.all('SELECT id FROM agent_runs'))
        with self.assertRaises(ValueError):
            self.agent.discover({'account_id':'main','content_depth':'unbounded'})
        self.assertEqual(len(self.engine.store.all('SELECT id FROM agent_runs')),count)

    def test_direct_article_load_uses_account_model_and_depth_defaults(self):
        found=self.discover()
        chosen=self.agent.decide(found['result']['candidate_ids'][0],{'decision':'select'})
        self.agent.tick(schedule=False)
        aid=self.agent.run(chosen['id'])['result']['article_id']
        self.agent.preferences({'account_id':'main','content_depth':'rich','ai':{'production_provider':'deepseek'}})
        from content_engine.server import make_server
        server=make_server(self.engine,0)
        thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
        try:
            token=(self.engine.data_dir/'.local-token').read_text(encoding='ascii').strip()
            request=urllib.request.Request(f'http://127.0.0.1:{server.server_port}/api/articles/{aid}',headers={'X-App-Token':token})
            data=json.load(urllib.request.urlopen(request,timeout=5))
            self.assertEqual(data['production_defaults']['ai']['production_provider'],'deepseek')
            self.assertEqual(data['production_defaults']['content_depth'],'rich')
            # The already written draft keeps the level used when it was produced.
            self.assertEqual(data['article']['data']['writing_policy']['level'],'rich')
        finally:
            server.shutdown();server.server_close();thread.join(timeout=5)

    def test_lengths_follow_template_and_depth(self):
        template={'min_chars':350,'max_chars':800}
        levels=[plan(template,x,6) for x in ('compact','standard','rich','deep')]
        self.assertLess(levels[0]['target_min_chars'],levels[1]['target_min_chars'])
        self.assertLess(levels[1]['target_min_chars'],levels[2]['target_min_chars'])
        self.assertLess(levels[2]['target_min_chars'],levels[3]['target_min_chars'])
        self.assertEqual(normalize(),'rich')
        other=plan({'min_chars':1000,'max_chars':1400},'rich',8)
        self.assertGreater(other['target_min_chars'],levels[2]['target_min_chars'])


class CleanWordTests(unittest.TestCase):
    setUp = media_fixture.PhotoAndDayTests.setUp
    tearDown = media_fixture.PhotoAndDayTests.tearDown
    make_topic = media_fixture.PhotoAndDayTests.make_topic
    result = media_fixture.PhotoAndDayTests.result
    make_article = media_fixture.PhotoAndDayTests.make_article
    approve = media_fixture.PhotoAndDayTests.approve
    save_photo = media_fixture.PhotoAndDayTests.save_photo

    def test_word_hides_internal_notes_but_zip_keeps_photo_provenance(self):
        aid=self.make_article()
        asset=self.save_photo(aid)
        info=self.engine.photos.asset(asset)['data']
        info.update({'caption':'原报道配图（赛事、人物及拍摄时间请在终审核对）','credit':'来源：news.bjd.com.cn','license':'公开可见；转载依据未核实','photo_role':'source_report','reuse_verified':False})
        with self.engine.store.db() as db:
            db.execute('UPDATE photo_assets SET data=? WHERE id=?',(json.dumps(info),asset))
        self.approve(aid)
        with zipfile.ZipFile(io.BytesIO(self.engine.export_article(aid))) as bundle:
            credits=bundle.read('图片来源与署名.json').decode()
            self.assertIn('news.bjd.com.cn',credits)
            with zipfile.ZipFile(io.BytesIO(bundle.read('article.docx'))) as doc:
                raw=doc.read('word/document.xml').decode()
                for value in ('AI 辅助创作','发布时请按平台要求选择声明','终审核对','转载依据未核实','news.bjd.com.cn','图片出处：'):
                    self.assertNotIn(value,raw)
                self.assertEqual(len([n for n in doc.namelist() if n.startswith('word/media/')]),1)
                ET.fromstring(raw)

    def test_reader_caption_file_photo_and_required_credit_remain(self):
        self.assertEqual(publication_caption({'data':{'caption':'球员在比赛中完成投篮','photo_role':'source_report'}}),'球员在比赛中完成投篮')
        caption=publication_caption({'data':{'caption':'资料图','photo_role':'file_photo','reuse_verified':True,'credit':'Test Author','license':'CC BY 4.0'}})
        self.assertIn('资料图',caption)
        self.assertIn('Test Author',caption)
        self.assertIn('CC BY 4.0',caption)


if __name__=='__main__':
    unittest.main()
