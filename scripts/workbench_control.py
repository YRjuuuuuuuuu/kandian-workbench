"""Windows launcher backend. Own-service control only; never reads AI secrets."""
import argparse
import contextlib
import ctypes
from ctypes import wintypes
import json
import os
from pathlib import Path
import socket
import sqlite3
import struct
import subprocess
import sys
import time
import urllib.request

ROOT = Path(__file__).resolve().parents[1]
HIDDEN = getattr(subprocess, 'CREATE_NO_WINDOW', 0)


def command_args(command):
    count=ctypes.c_int()
    function=ctypes.windll.shell32.CommandLineToArgvW
    function.argtypes=[wintypes.LPCWSTR,ctypes.POINTER(ctypes.c_int)]
    function.restype=ctypes.POINTER(wintypes.LPWSTR)
    pointer=function(command,ctypes.byref(count))
    if not pointer:raise OSError('无法读取服务启动参数')
    try:return [pointer[i] for i in range(count.value)]
    finally:
        free=ctypes.windll.kernel32.LocalFree;free.argtypes=[wintypes.HLOCAL];free.restype=wintypes.HLOCAL
        free(ctypes.cast(pointer,wintypes.HLOCAL))


def owns_command(args, executable, data_dir, port):
    if not args or not executable or not Path(executable).stem.lower().startswith(('python','pypy')):return False
    try:
        module=args.index('-m')
        if args[module+1:module+3]!=['content_engine','serve']:return False
        index=args.index('--data-dir');folder=Path(args[index+1]).resolve()
        actual=int(args[args.index('--port')+1]) if '--port' in args else 8765
        return folder==Path(data_dir).resolve() and actual==port
    except (ValueError,IndexError,OSError):return False


def process_info(pid):
    # The only interpolation is a validated integer PID, never source/user text.
    command='[Console]::OutputEncoding=[System.Text.UTF8Encoding]::new($false); Get-CimInstance Win32_Process -Filter "ProcessId = '+str(int(pid))+'" | Select-Object CommandLine,ExecutablePath | ConvertTo-Json -Compress'
    result=subprocess.run([str(Path(os.environ.get('SystemRoot','C:/Windows'))/'System32/WindowsPowerShell/v1.0/powershell.exe'),'-NoLogo','-NoProfile','-NonInteractive','-Command',command],capture_output=True,encoding='utf-8',errors='replace',timeout=12,creationflags=HIDDEN)
    if result.returncode:raise OSError('无法核对服务进程，请在当前 Windows 用户下打开控制台')
    return json.loads(result.stdout.strip()) if result.stdout.strip() else None


def port_owner(port):
    class Row(ctypes.Structure):
        _fields_=[(n,wintypes.DWORD) for n in ('state','local_address','local_port','remote_address','remote_port','pid')]
    size=wintypes.DWORD()
    query=ctypes.windll.iphlpapi.GetExtendedTcpTable
    query.argtypes=[ctypes.c_void_p,ctypes.POINTER(wintypes.DWORD),wintypes.BOOL,wintypes.ULONG,ctypes.c_int,wintypes.ULONG]
    query.restype=wintypes.DWORD
    query(None,ctypes.byref(size),False,2,3,0)
    for _ in range(3):
        buffer=ctypes.create_string_buffer(size.value)
        error=query(buffer,ctypes.byref(size),False,2,3,0)
        if error==122:continue
        if error:raise OSError('无法读取本机服务端口：'+str(error))
        count=wintypes.DWORD.from_buffer(buffer).value
        owners=set()
        for i in range(count):
            row=Row.from_buffer(buffer,ctypes.sizeof(wintypes.DWORD)+i*ctypes.sizeof(Row))
            address=socket.inet_ntoa(struct.pack('<I',row.local_address))
            if socket.ntohs(row.local_port & 65535)==port and address in ('127.0.0.1','0.0.0.0'):owners.add(row.pid)
        if len(owners)>1:raise OSError('端口有多个所属进程，已保留原服务')
        return next(iter(owners),None)
    raise OSError('端口状态正在变化，请重试')


@contextlib.contextmanager
def action_lock(folder):
    import msvcrt
    folder.mkdir(parents=True,exist_ok=True)
    with (folder/'.launcher-control.lock').open('a+b') as handle:
        handle.seek(0);handle.write(b'0');handle.flush();handle.seek(0)
        try:msvcrt.locking(handle.fileno(),msvcrt.LK_NBLCK,1)
        except OSError:raise ValueError('另一个启动／停止操作正在处理，请稍候')
        try:yield
        finally:handle.seek(0);msvcrt.locking(handle.fileno(),msvcrt.LK_UNLCK,1)


class Controller:
    def __init__(self, data_dir, port=8765):
        self.data=Path(data_dir).resolve();self.port=port;self.url='http://127.0.0.1:'+str(port)

    def owns(self,pid):
        info=process_info(pid)
        return bool(info and owns_command(command_args(info['CommandLine'] or ''),info['ExecutablePath'],self.data,self.port))

    def counts(self,connection=None):
        if connection is not None:
            return dict(connection.execute("SELECT state,COUNT(*) FROM agent_runs WHERE state IN ('running','queued','waiting_external') GROUP BY state"))
        database=self.data/'workbench.sqlite3'
        if not database.is_file():raise OSError('还无法读取任务状态，暂不停止服务')
        with contextlib.closing(sqlite3.connect(database.as_uri()+'?mode=ro',uri=True,timeout=3)) as db:return self.counts(db)

    def health(self):
        opener=urllib.request.build_opener(urllib.request.ProxyHandler({}))
        with opener.open(self.url+'/health',timeout=2) as response:
            value=json.loads(response.read(4096))
        return value if value.get('ok') is True and isinstance(value.get('version'),str) else None

    def status(self):
        pid=port_owner(self.port)
        if pid is None:return {'state':'stopped','message':'工作台已停止','url':self.url,'active':0,'queued':0}
        if not self.owns(pid):return {'state':'conflict','message':'端口被其他程序或项目占用，未操作该程序','url':self.url,'pid':pid}
        try:health=self.health()
        except (OSError,ValueError):health=None
        result={'state':'running' if health else 'starting','message':'工作台运行中' if health else '服务正在启动，请稍候','url':self.url,'pid':pid,'version':health.get('version') if health else None}
        try:
            counts=self.counts();result.update(active=counts.get('running',0),queued=counts.get('queued',0),external=counts.get('waiting_external',0))
        except (OSError,sqlite3.Error):result['active']=None
        return result

    def start(self):
        with action_lock(self.data):
            current=self.status()
            if current['state']=='running':return current
            if current['state']=='conflict':raise ValueError(current['message'])
            if current['state']=='starting':return current
            if sys.version_info<(3,11):raise ValueError('需要 Python 3.11 或以上，请设置 CONTENT_ENGINE_PYTHON')
            with (self.data/'server.log').open('ab') as out,(self.data/'server-error.log').open('ab') as error:
                process=subprocess.Popen([sys.executable,'-X','utf8','-m','content_engine','serve','--port',str(self.port),'--data-dir',str(self.data)],cwd=ROOT,stdin=subprocess.DEVNULL,stdout=out,stderr=error,creationflags=HIDDEN|subprocess.CREATE_NEW_PROCESS_GROUP,close_fds=True)
            deadline=time.monotonic()+25
            while time.monotonic()<deadline:
                if process.poll() is not None:raise OSError('服务未能启动，请查看 data/server-error.log')
                try:
                    health=self.health()
                    if health:return {**self.status(),'message':'工作台已启动'}
                except (OSError,ValueError):pass
                time.sleep(.25)
            return {'state':'starting','message':'服务仍在启动，控制台会继续检查','url':self.url,'pid':process.pid}

    def terminate(self,pid,check_idle):
        kernel=ctypes.windll.kernel32
        kernel.OpenProcess.argtypes=[wintypes.DWORD,wintypes.BOOL,wintypes.DWORD];kernel.OpenProcess.restype=wintypes.HANDLE
        kernel.CloseHandle.argtypes=[wintypes.HANDLE]
        kernel.TerminateProcess.argtypes=[wintypes.HANDLE,wintypes.UINT]
        kernel.WaitForSingleObject.argtypes=[wintypes.HANDLE,wintypes.DWORD]
        handle=kernel.OpenProcess(0x100000|0x1000|1,False,pid)
        if not handle:raise OSError('无法停止当前服务，请使用启动工作台的同一 Windows 用户')
        try:
            # Holding a process handle prevents PID reuse while checking ownership.
            if not self.owns(pid):raise ValueError('进程归属已变化，未停止其他程序')
            if not check_idle():return False
            if not kernel.TerminateProcess(handle,0):raise OSError('停止服务失败，原进程已保留')
            if kernel.WaitForSingleObject(handle,7000)!=0:raise OSError('服务尚未退出，请稍后重试')
            return True
        finally:kernel.CloseHandle(handle)

    def stop(self):
        with action_lock(self.data):
            current=self.status()
            if current['state']=='stopped':return current
            if current['state']=='conflict':raise ValueError(current['message'])
            if not (self.data/'workbench.sqlite3').is_file():raise ValueError('服务正在初始化，暂不停止，请稍后重试')
            with contextlib.closing(sqlite3.connect(self.data/'workbench.sqlite3',timeout=4)) as db,db:
                # Prevent an idle-to-running race: no new task admission/claim can
                # commit between this check and termination. Queued tasks persist.
                db.execute('BEGIN IMMEDIATE')
                stopped=self.terminate(current['pid'],lambda:self.counts(db).get('running',0)==0)
                if not stopped:
                    counts=self.counts(db)
                    return {**current,'state':'waiting','message':'等待正在执行的任务完成后停止','active':counts.get('running',0),'queued':counts.get('queued',0)}
            return {'state':'stopped','message':'工作台已停止；稿件和设置已保留','url':self.url,'active':0,'queued':current.get('queued',0)}


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('action',choices=('status','start','stop'))
    parser.add_argument('--port',type=int,default=8765)
    parser.add_argument('--data-dir',default=str(ROOT/'data'))
    args=parser.parse_args()
    try:
        if os.name!='nt':raise ValueError('此控制台用于 Windows')
        folder=Path(args.data_dir).resolve()
        if not folder.is_relative_to(ROOT) or folder==ROOT:raise ValueError('数据目录必须位于本项目中')
        if not 1024<=args.port<=65535:raise ValueError('端口无效')
        result=getattr(Controller(folder,args.port),args.action)()
    except Exception as exc:
        result={'state':'error','message':str(exc),'url':'http://127.0.0.1:'+str(args.port)}
    print(json.dumps(result,ensure_ascii=False),flush=True)
    return 1 if result['state']=='error' else 0


if __name__=='__main__':raise SystemExit(main())
