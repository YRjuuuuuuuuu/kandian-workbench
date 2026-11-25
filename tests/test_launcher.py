import contextlib
from pathlib import Path
import shutil
import sqlite3
import unittest
import uuid
from unittest.mock import Mock,patch

from scripts.workbench_control import Controller,owns_command


class LauncherTests(unittest.TestCase):
    def setUp(self):
        self.base=(Path(__file__).resolve().parent/'.tmp').resolve();self.base.mkdir(exist_ok=True)
        self.folder=self.base/('launcher-'+uuid.uuid4().hex);self.folder.mkdir()
        with contextlib.closing(sqlite3.connect(self.folder/'workbench.sqlite3')) as db,db:
            db.execute('CREATE TABLE agent_runs(id TEXT,state TEXT)')
            db.execute('CREATE TABLE manuscripts(id TEXT,body TEXT)')
            db.execute("INSERT INTO manuscripts VALUES('mine','人工编辑正文')")
        self.controller=Controller(self.folder,8768)
        self.lock=patch('scripts.workbench_control.action_lock',return_value=contextlib.nullcontext());self.lock.start()
    def tearDown(self):
        self.lock.stop()
        if self.folder.resolve().parent!=self.base:raise RuntimeError('Unexpected test path')
        shutil.rmtree(self.folder)
    def tasks(self,states):
        with contextlib.closing(sqlite3.connect(self.folder/'workbench.sqlite3')) as db,db:
            db.executemany('INSERT INTO agent_runs VALUES(?,?)',[(str(i),s) for i,s in enumerate(states)])

    def test_identity_requires_exact_module_data_directory_and_port(self):
        args=['python.exe','-X','utf8','-m','content_engine','serve','--data-dir',str(self.folder),'--port','8768']
        self.assertTrue(owns_command(args,'C:/Python/python.exe',self.folder,8768))
        self.assertFalse(owns_command(args,'C:/Windows/cmd.exe',self.folder,8768))
        self.assertFalse(owns_command(args,'C:/Python/python.exe',self.folder,8765))
        self.assertFalse(owns_command(args,'C:/Python/python.exe',self.folder/'neighbor',8768))
        self.assertFalse(owns_command(['python.exe','-m','other_engine','serve']+args[5:],'C:/Python/python.exe',self.folder,8768))

    def test_foreign_port_never_calls_health_or_kills_process(self):
        with patch('scripts.workbench_control.port_owner',return_value=123),patch.object(self.controller,'owns',return_value=False),patch.object(self.controller,'health') as health,patch.object(self.controller,'terminate') as terminate:
            self.assertEqual(self.controller.status()['state'],'conflict')
            with self.assertRaises(ValueError):self.controller.stop()
            health.assert_not_called();terminate.assert_not_called()

    def test_existing_running_service_is_reused_without_second_process(self):
        running={'state':'running','url':self.controller.url,'pid':123}
        with patch.object(self.controller,'status',return_value=running),patch('scripts.workbench_control.subprocess.Popen') as spawn:
            self.assertEqual(self.controller.start(),running);spawn.assert_not_called()

    def test_running_task_waits_without_changing_tasks_or_manuscripts(self):
        self.tasks(['running','queued','waiting_external']);calls=[]
        def terminate(pid,check):calls.append(check());return calls[-1]
        with patch.object(self.controller,'status',return_value={'state':'running','pid':123}),patch.object(self.controller,'terminate',side_effect=terminate):
            result=self.controller.stop()
        self.assertEqual(result['state'],'waiting');self.assertEqual(result['active'],1);self.assertEqual(calls,[False])
        with contextlib.closing(sqlite3.connect(self.folder/'workbench.sqlite3')) as db,db:
            self.assertEqual(db.execute('SELECT state FROM agent_runs ORDER BY id').fetchall(),[('running',),('queued',),('waiting_external',)])
            self.assertEqual(db.execute('SELECT body FROM manuscripts').fetchone()[0],'人工编辑正文')

    def test_idle_stop_holds_write_lock_and_preserves_queued_work(self):
        self.tasks(['queued','waiting_external'])
        def terminate(pid,check):
            self.assertTrue(check())
            with contextlib.closing(sqlite3.connect(self.folder/'workbench.sqlite3',timeout=.01)) as second,second:
                with self.assertRaises(sqlite3.OperationalError):second.execute("INSERT INTO agent_runs VALUES('race','running')")
            return True
        with patch.object(self.controller,'status',return_value={'state':'running','pid':123,'queued':1}),patch.object(self.controller,'terminate',side_effect=terminate):
            result=self.controller.stop()
        self.assertEqual(result['state'],'stopped');self.assertEqual(self.controller.counts(),{'queued':1,'waiting_external':1})

    def test_stopping_already_stopped_does_nothing(self):
        with patch.object(self.controller,'status',return_value={'state':'stopped'}),patch.object(self.controller,'terminate') as terminate:
            self.assertEqual(self.controller.stop()['state'],'stopped');terminate.assert_not_called()

    def test_native_handle_is_closed_without_kill_when_identity_changed(self):
        kernel=Mock();kernel.OpenProcess.return_value=55
        with patch('scripts.workbench_control.ctypes.windll',create=True) as library,patch.object(self.controller,'owns',return_value=False):
            library.kernel32=kernel
            with self.assertRaises(ValueError):self.controller.terminate(123,lambda:True)
        kernel.TerminateProcess.assert_not_called();kernel.CloseHandle.assert_called_once_with(55)

    def test_native_handle_is_closed_without_kill_when_work_is_active(self):
        kernel=Mock();kernel.OpenProcess.return_value=55
        with patch('scripts.workbench_control.ctypes.windll',create=True) as library,patch.object(self.controller,'owns',return_value=True):
            library.kernel32=kernel
            self.assertFalse(self.controller.terminate(123,lambda:False))
        kernel.TerminateProcess.assert_not_called();kernel.CloseHandle.assert_called_once_with(55)


if __name__=='__main__':unittest.main()
