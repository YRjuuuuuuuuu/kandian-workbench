import copy
import io
import json
import random
import unittest
import urllib.parse
import zipfile
import contextlib
from types import SimpleNamespace
from unittest.mock import Mock, patch

import test_agent as agent_fixture
import test_upgrade as media_fixture
from content_engine import content_depth
from content_engine.ai import normalize
from content_engine.photos import OpenGraphImages
from content_engine.image_pipeline import similar, visual_hash
from content_engine.image_subjects import title_matches
from content_engine.sports_charts import extracted, supported_row
from content_engine.writer_factory import prepare


def samples():
    rows=[]
    for i in range(8):
        rng=random.Random(i)
        text='样本作者'+str(i)+'\n\n'+''.join(chr(0x4e00+rng.randrange(3500)) for _ in range(650))
        rows.append({'title':'合成样本'+str(i),'author':'合成作者'+str(i),'url':'https://www.toutiao.com/article/'+str(100+i),'text':text,'group':['basketball','football','tennis'][i%3]})
    return prepare(rows)


def rules(rows):
    train=[s['id'] for s in rows if s['split']=='train']
    return {'persona':'面向普通体育读者，先交代具体结果，再解释过程与可观察的变化。','patterns':[{'section':['opening','body','analysis','title'][i%4],'rule':'根据可核实的比赛细节解释具体变化，同时把事实与作者解释区分清楚。'+str(i),'sample_ids':train[:2]} for i in range(8)],'avoid':['不编造事实或采访。'],'rubric':[{'name':'具体性','check':'每段是否提供独立的信息或解释。'}],'templates':[{'id':'analysis','outline':['结果与看点','关键变化','另一种解释']}]}


class AssetLogicTests(unittest.TestCase):
    def test_proxy_transport_validates_dns_before_any_network_request(self):
        from content_engine.public_fetch import fetch
        opener,context=Mock(),Mock()
        response=Mock();response.__enter__=Mock(return_value=response);response.__exit__=Mock(return_value=False)
        response.getheader.side_effect=lambda k,d=None: {'Content-Type':'image/png'}.get(k,d);response.read.return_value=b'public-image'
        opener.open.return_value=response
        with patch('content_engine.public_fetch._native_curl',return_value=None),patch('content_engine.public_fetch.public_addresses',return_value=(urllib.parse.urlsplit('https://public.example/a.png'),'public.example',['93.184.216.34'])) as validate,patch('content_engine.public_fetch.urllib.request.getproxies',return_value={'https':'http://127.0.0.1:7890'}),patch('content_engine.public_fetch.urllib.request.build_opener',return_value=opener),patch('content_engine.public_fetch.tls_context',return_value=context):
            self.assertEqual(fetch('https://public.example/a.png')[0],b'public-image')
        validate.assert_called_once();self.assertEqual(opener.open.call_args.args[0].full_url,'https://public.example/a.png')

    def test_rich_body_lazy_image_excludes_recommendation_and_carries_alt(self):
        parser=OpenGraphImages();parser.feed('<div class="article-content"><img data-src="https://cdn.example/a.jpg" alt="Luka Doncic shoots"><div class="related"><img src="https://cdn.example/wrong.jpg"></div><img srcset="https://cdn.example/s.jpg 400w, https://cdn.example/large.jpg 1200w"></div>')
        self.assertEqual(parser.images,['https://cdn.example/a.jpg','https://cdn.example/large.jpg']);self.assertIn('Doncic',parser.details[parser.images[0]]['alt'])

    def test_roster_mention_cannot_identify_another_player_photo(self):
        self.assertFalse(title_matches('File:Terrance Ferguson watches rim.jpg',{'name':'Bam Adebayo'}))
        self.assertTrue(title_matches('File:Luka Dončić 2021.jpg',{'name':'Luka Doncic'}))

    def test_same_photo_in_different_encodings_is_deduplicated(self):
        from PIL import Image
        rng=random.Random(4);im=Image.frombytes('RGB',(500,320),bytes(rng.randrange(256) for _ in range(500*320*3)))
        first,second=io.BytesIO(),io.BytesIO();im.save(first,'PNG');im.save(second,'JPEG',quality=90)
        self.assertTrue(similar(visual_hash(first.getvalue()),visual_hash(second.getvalue())))

    def test_commons_crop_name_maps_to_original_without_merging_different_dates(self):
        from content_engine.image_pipeline import commons_original_key
        def key(title):return commons_original_key({'source_kind':'wikimedia_commons','source_title':title})
        self.assertEqual(key('File:Austin Reaves (cropped).jpg'),key('File:Austin Reaves.jpg'))
        self.assertNotEqual(key('File:Austin Reaves 2021.jpg'),key('File:Austin Reaves 2022.jpg'))

    def test_wrong_subject_is_rejected_even_when_sport_scene_has_high_score(self):
        from content_engine.image_pipeline import rejected_assessment
        self.assertTrue(rejected_assessment({'score':88,'scene':'sport_action','subject_match':'mismatch','duplicate_of':''}))

    def test_visual_review_uses_official_reference_and_caps_uncertain_identity(self):
        import shutil
        import uuid
        from pathlib import Path
        from PIL import Image
        from content_engine.image_pipeline import review
        buffer=io.BytesIO();Image.new('RGB',(100,100),'white').save(buffer,'PNG')
        reference={'id':'ref','article_id':'a','data':{'source_kind':'official_portrait','matched_subject':'Luka Doncic'}}
        candidate={'id':'candidate','article_id':'a','data':{'caption':'待核对'}}
        output={'choices':[{'finish_reason':'stop','message':{'content':json.dumps({'images':[{'id':'candidate','score':99,'subject_match':'unclear','scene':'sport_action','reason':'身份未确认','duplicate_of':''}]})}}]}
        scratch=(Path(__file__).resolve().parent/'.tmp').resolve();scratch.mkdir(exist_ok=True)
        folder=scratch/('visual-'+uuid.uuid4().hex);folder.mkdir()
        def cleanup():
            if folder.resolve().parent!=scratch:raise RuntimeError('Unexpected test scratch path')
            shutil.rmtree(folder)
        self.addCleanup(cleanup)
        with contextlib.nullcontext(folder):
            engine=SimpleNamespace(data_dir=Path(folder),models=SimpleNamespace(secret=SimpleNamespace(get=lambda:'unused-test-key')),agent=SimpleNamespace(stopped=lambda _:False))
            manager=SimpleNamespace(engine=engine,list=lambda _:[reference],content=lambda _:(buffer.getvalue(),'image/png'))
            run={'id':'r','payload':{'config':{'ai':normalize({'production_provider':'deepseek'})}}}
            with patch('content_engine.ai.deepseek_request',return_value=output) as request:
                result=review(manager,[candidate],[{'name':'Luka Doncic','aliases':['Luka Doncic']}],run,lambda *a:None)
            parts=request.call_args.args[2]['messages'][1]['content']
            self.assertEqual(len([x for x in parts if x['type']=='image_url']),2)
            self.assertEqual(result['candidate']['score'],65)

    def test_chart_rejects_wrong_subject_value_and_wrong_units(self):
        fact={'id':'f1','text':'东契奇40分，詹姆斯30分。','url':'https://www.nba.com/news/test'};by_id={'f1':fact}
        good={'label':'东契奇','value':40,'fact_id':'f1','quote':'东契奇40分'}
        self.assertIsNotNone(supported_row(good,'得分','分',by_id))
        self.assertIsNone(supported_row({**good,'value':30,'quote':'东契奇40分，詹姆斯30分'},'得分','分',by_id))
        self.assertIsNone(supported_row(good,'得分','次',by_id));self.assertIsNone(supported_row({**good,'value':45},'得分','分',by_id))

    def test_stat_card_does_not_mix_next_player_or_invent_missing_assists(self):
        data=extracted([{'id':'f','text':'文班亚马得到52分24个篮板8次盖帽，队友30分10助攻。','url':'https://www.nba.com/test'}],[{'name':'Victor Wembanyama','aliases':['文班亚马']}])
        self.assertEqual([(r['label'],r['value']) for r in data[0]['rows']],[('得分',52),('篮板',24),('盖帽',8)])

    def test_football_score_keeps_the_explicit_team_order(self):
        from content_engine.sports_charts import score_comparison
        data=score_comparison([{'id':'f','text':'曼城以3比2击败莱比锡。','url':'https://example.org/report'}],[{'aliases':['曼城']},{'aliases':['莱比锡']}],'football')
        self.assertEqual([(r['label'],r['value']) for r in data[0]['rows']],[('曼城',3),('莱比锡',2)])
        self.assertEqual(data[0]['unit'],'球')
        self.assertEqual(score_comparison([{'id':'f','text':'2026-11-05尚未取得比分。','url':'https://example.org/report'}],[{'aliases':['曼城']},{'aliases':['莱比锡']}],'football'),[])

    def test_rich_default_deep_is_larger_and_old_choices_remain_valid(self):
        self.assertEqual(content_depth.normalize(),'rich')
        template={'min_chars':600,'max_chars':1100}
        self.assertGreater(content_depth.plan(template,'deep')['target_min_chars'],content_depth.plan(template,'rich')['target_min_chars'])
        self.assertEqual(content_depth.normalize('standard'),'standard')

    def test_editorial_check_targets_repetition_and_unsupported_stat_causality(self):
        from content_engine.editorial_quality import diagnostics
        repeated='同一组统计可以提示新的问题，但不能自动解释球员的全部作用，应回到具体比赛进程中观察这些变化。'
        issues=diagnostics({'paragraphs':[{'kind':'analysis','text':repeated},{'kind':'analysis','text':repeated},{'kind':'analysis','text':'东契奇的篮板降到7个，说明内线保护交给队友，因此他的任务已经改变。'}]})
        self.assertEqual(len(issues),2)

    def test_palette_transparency_exports_white_and_caption_is_not_duplicated(self):
        from PIL import Image
        from content_engine.word_export import native_image,publication_caption
        picture=Image.new('P',(10,10));picture.putpalette([0,0,0]*256);picture.info['transparency']=bytes([0]*256)
        buffer=io.BytesIO();picture.save(buffer,'PNG')
        output,_,_=native_image(buffer.getvalue())
        with Image.open(io.BytesIO(output)) as rendered:self.assertEqual(rendered.convert('RGB').getpixel((0,0)),(255,255,255))
        self.assertEqual(publication_caption({'data':{'caption':'球员（资料照）','photo_role':'file_photo'}}),'球员（资料照）')


class WriterFactoryTests(unittest.TestCase):
    setUp=agent_fixture.AgentTests.setUp
    tearDown=agent_fixture.AgentTests.tearDown

    def test_native_redirect_to_private_target_stops_before_second_request(self):
        from content_engine.public_fetch import _curl_fetch
        folder=self.folder/'native';folder.mkdir()
        def validate(url):
            if '127.0.0.1' in url:raise ValueError('来源不能指向本机或私有网络')
            return urllib.parse.urlsplit(url),'public.example',['93.184.216.34']
        def execute(command,**kwargs):
            (folder/'headers').write_text('HTTP/1.1 302 Found\nLocation: https://127.0.0.1/private\n\n',encoding='utf-8')
            (folder/'body').write_bytes(b'')
            self.assertIn('--max-filesize',command);self.assertNotIn('--insecure',command);self.assertNotIn('--location',command)
            return SimpleNamespace(returncode=0,stdout=b'302\ntext/html')
        with patch('content_engine.public_fetch._scratch',return_value=contextlib.nullcontext(folder)),patch('content_engine.public_fetch.public_addresses',side_effect=validate),patch('content_engine.public_fetch.subprocess.run',side_effect=execute) as call,self.assertRaisesRegex(ValueError,'私有网络'):
            _curl_fetch('curl.exe','https://public.example/start',100,1,'text/html')
        self.assertEqual(call.call_count,1)

    def test_native_body_cap_remains_enforced_without_content_length(self):
        from content_engine.public_fetch import _curl_fetch
        folder=self.folder/'native';folder.mkdir()
        def execute(*args,**kwargs):
            (folder/'body').write_bytes(b'x'*101)
            return SimpleNamespace(returncode=0,stdout=b'200\nimage/png')
        with patch('content_engine.public_fetch._scratch',return_value=contextlib.nullcontext(folder)),patch('content_engine.public_fetch.public_addresses'),patch('content_engine.public_fetch.subprocess.run',side_effect=execute),self.assertRaisesRegex(ValueError,'读取上限'):
            _curl_fetch('curl.exe','https://public.example/a.png',100,1,'image/png')

    def test_split_is_stable_and_each_group_retains_training_material(self):
        rows=samples();self.assertEqual(sum(s['split']=='holdout' for s in rows),2)
        self.assertEqual({s['group'] for s in rows},{s['group'] for s in rows if s['split']=='train'})
        duplicated=[{**s,'text':s['text']} for s in rows]+[rows[0]]
        self.assertEqual(len(prepare(duplicated)),8)

    def test_holdout_cannot_be_used_as_rule_evidence(self):
        rows=samples();body=rules(rows);held=next(s['id'] for s in rows if s['split']=='holdout')
        for pattern in body['patterns']:pattern['sample_ids']=[held]
        before=self.engine.writer_factory.current('sports')
        with self.assertRaisesRegex(ValueError,'证据或覆盖不足'):self.engine.writer_factory.save(self.engine.modes.get('sports'),rows,body)
        self.assertEqual(self.engine.writer_factory.current('sports'),before)

    def test_activation_conflict_rollback_and_task_skill_snapshot(self):
        factory=self.engine.writer_factory;mode=self.engine.modes.get('sports');rows=samples();first=factory.save(mode,rows,rules(rows));old=factory.current('sports')
        factory.activate('sports',first['id'],old['id'] if old else None)
        run=self.agent.discover({'account_id':'main'});frozen=self.agent.run(run['id'])['payload']['writer_skill']
        second=factory.save(mode,rows,rules(rows));factory.activate('sports',second['id'],first['id'])
        from content_engine.documents import DocumentConflict
        with self.assertRaises(DocumentConflict):factory.activate('sports',first['id'],first['id'])
        self.assertEqual(self.agent.run(run['id'])['payload']['writer_skill'],frozen)
        self.assertEqual(factory.current('sports')['id'],second['id'])

    def test_sample_text_is_private_and_wrong_mode_cannot_activate(self):
        rows=samples();profile=self.engine.writer_factory.save(self.engine.modes.get('sports'),rows,rules(rows))
        self.assertNotIn(rows[0]['text'][:300],profile['data']['skill_markdown'])
        self.assertNotIn('text',profile['data']['samples'][0])
        with self.assertRaises(ValueError):self.engine.writer_factory.activate('unknown',profile['id'],None)


class ChartExportTests(unittest.TestCase):
    setUp=media_fixture.PhotoAndDayTests.setUp
    tearDown=media_fixture.PhotoAndDayTests.tearDown
    make_topic=media_fixture.PhotoAndDayTests.make_topic
    result=media_fixture.PhotoAndDayTests.result
    make_article=media_fixture.PhotoAndDayTests.make_article

    def test_chart_is_ordered_embedded_and_retains_numeric_provenance(self):
        aid=self.make_article();from content_engine.sports_charts import render
        chart={'title':'合成图表验收','metric':'得分','unit':'分','rows':[{'label':'甲队','value':98,'fact_id':'f1','quote':'合成98分','source_url':'https://example.org/report'},{'label':'乙队','value':94,'fact_id':'f1','quote':'合成94分','source_url':'https://example.org/report'}]}
        content=render(chart,'合成验收');asset=self.engine.photos.save_generated(aid,content,{'caption':'合成数据对比','source_url':'https://example.org/report','chart_data':chart})
        doc=self.engine.document(aid);doc['blocks'].insert(1,{'id':'chart-test','type':'image','asset_id':asset,'caption':'合成数据对比'})
        self.engine.save_document(aid,{'base_revision':self.engine.article(aid)['revision'],'document':doc})
        self.assertEqual(self.engine.document(aid)['blocks'][1]['asset_id'],asset)
        with zipfile.ZipFile(io.BytesIO(self.engine.export_word(aid))) as word:
            self.assertTrue(any(name.startswith('word/media/') for name in word.namelist()))
            self.assertIn('合成数据对比',word.read('word/document.xml').decode())
        self.assertEqual(self.engine.photos.asset(asset)['data']['chart_data']['rows'][0]['value'],98)


if __name__=='__main__':unittest.main()
