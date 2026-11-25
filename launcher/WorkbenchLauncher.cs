using System;
using System.Collections.Generic;
using System.Diagnostics;
using System.Drawing;
using System.IO;
using System.Runtime.InteropServices;
using System.Security.Cryptography;
using System.Text;
using System.Threading;
using System.Threading.Tasks;
using System.Web.Script.Serialization;
using System.Windows.Forms;

internal static class Program {
    [DllImport("user32.dll", CharSet=CharSet.Unicode)] static extern IntPtr FindWindow(string cls,string title);
    [DllImport("user32.dll")] static extern bool PostMessage(IntPtr hwnd,int message,IntPtr w,IntPtr l);
    internal const int OpenMessage=0x8001, StopMessage=0x8002;
    [STAThread] static void Main(string[] args) {
        Application.EnableVisualStyles(); Application.SetCompatibleTextRenderingDefault(false);
        string root=AppDomain.CurrentDomain.BaseDirectory, data=Path.Combine(root,"data"); int port=8765; string action="start"; bool browser=true;
        for(int i=0;i<args.Length;i++) {
            if(args[i]=="--stop") action="stop";
            else if(args[i]=="--panel") action="status";
            else if(args[i]=="--no-browser") browser=false;
            else if(args[i]=="--port" && i+1<args.Length) int.TryParse(args[++i],out port);
            else if(args[i]=="--data-dir" && i+1<args.Length) data=Path.GetFullPath(args[++i]);
        }
        string title="看点工场 · 工作台控制台"+(port==8765?"":" · 验收 "+port);
        string hash; using(var sha=SHA256.Create())hash=BitConverter.ToString(sha.ComputeHash(Encoding.UTF8.GetBytes((root+data+port).ToLowerInvariant()))).Replace("-","").Substring(0,24);
        bool created; using(var mutex=new Mutex(true,"Local\\ContentWorkbench-"+hash,out created)) {
            if(!created) {IntPtr window=FindWindow(null,title);if(window!=IntPtr.Zero)PostMessage(window,action=="stop"?StopMessage:OpenMessage,IntPtr.Zero,IntPtr.Zero);return;}
            Application.Run(new ControlWindow(root,data,port,title,action,browser));
        }
    }
}

internal sealed class ControlWindow:Form {
    readonly string root,data,url,initialAction; readonly int port; readonly bool allowBrowser;
    readonly Label status=new Label(), detail=new Label(), footer=new Label();
    readonly Button start=new Button(), open=new Button(), stop=new Button();
    readonly ProgressBar progress=new ProgressBar(); readonly System.Windows.Forms.Timer timer=new System.Windows.Forms.Timer();
    bool busy=false, waiting=false, openWhenReady=false, ready=false;
    string pendingAction=null;bool pendingOpen=false;
    [DllImport("user32.dll")] static extern bool SetForegroundWindow(IntPtr hwnd);
    internal ControlWindow(string root,string data,int port,string title,string action,bool browser) {
        this.root=root;this.data=data;this.port=port;url="http://127.0.0.1:"+port;initialAction=action;allowBrowser=browser;
        Text=title;ClientSize=new Size(650,390);MinimumSize=Size;MaximumSize=Size;MaximizeBox=false;
        StartPosition=FormStartPosition.CenterScreen;BackColor=Color.FromArgb(244,247,242);Font=new Font("Microsoft YaHei UI",10);
        AutoScaleMode=AutoScaleMode.Dpi;
        try{Icon=Icon.ExtractAssociatedIcon(Application.ExecutablePath);}catch{}
        var heading=new Label{Text="看点工场",Font=new Font("Microsoft YaHei UI",25,FontStyle.Bold),ForeColor=Color.FromArgb(25,60,49),AutoSize=true,Location=new Point(30,24)};Controls.Add(heading);
        Controls.Add(new Label{Text="你的图文工作台，一键启动与停止",AutoSize=true,ForeColor=Color.FromArgb(107,124,113),Location=new Point(34,82)});
        var card=new Panel{BackColor=Color.White,Location=new Point(30,122),Size=new Size(590,130),BorderStyle=BorderStyle.FixedSingle};Controls.Add(card);
        status.SetBounds(22,18,540,31);status.Font=new Font(Font.FontFamily,16,FontStyle.Bold);status.Text="正在检查工作台…";card.Controls.Add(status);
        detail.SetBounds(24,61,540,48);detail.ForeColor=Color.FromArgb(106,121,112);detail.Text=url;card.Controls.Add(detail);
        Configure(start,"启动并打开",30,272,190,Color.FromArgb(21,117,94),Color.White);
        Configure(open,"打开网页",232,272,180,Color.White,Color.FromArgb(30,70,55));
        Configure(stop,"停止工作台",424,272,196,Color.White,Color.FromArgb(30,70,55));
        start.Click+=async (s,e)=>{waiting=false;openWhenReady=true;await Work("start");};
        open.Click+=async (s,e)=>{if(ready)OpenBrowser();else{openWhenReady=true;await Work("start");}};
        stop.Click+=async (s,e)=>{if(waiting){waiting=false;PaintStatus("running","已取消等待停止；工作台继续运行",url);return;}waiting=true;await Work("stop");};
        progress.SetBounds(30,331,590,4);progress.Style=ProgressBarStyle.Marquee;progress.Visible=false;Controls.Add(progress);
        footer.SetBounds(32,346,590,40);footer.Font=new Font(Font.FontFamily,8.5f);footer.ForeColor=Color.FromArgb(112,125,115);
        footer.Text="关闭此面板不停止服务。停止前请确认网页显示“已保存”。\n等待任务完成后停止时，请保持此面板打开。";Controls.Add(footer);
        timer.Interval=4000;timer.Tick+=async (s,e)=>{if(!busy)await Work(waiting?"stop":"status");};
        Shown+=async (s,e)=>{openWhenReady=initialAction=="start";waiting=initialAction=="stop";await Work(initialAction);timer.Start();};
        FormClosing+=(s,e)=>{if(busy){e.Cancel=true;detail.Text="操作处理中，请稍后再关闭面板。";}else timer.Stop();};
    }
    void Configure(Button button,string text,int x,int y,int width,Color back,Color fore) {
        button.Text=text;button.SetBounds(x,y,width,48);button.BackColor=back;button.ForeColor=fore;button.FlatStyle=FlatStyle.Flat;button.FlatAppearance.BorderColor=Color.FromArgb(218,228,219);button.Cursor=Cursors.Hand;button.Enabled=false;Controls.Add(button);
    }
    static string Quote(string value){return "\""+value.Replace("\"","\\\"")+"\"";}
    string Python() {
        string custom=Environment.GetEnvironmentVariable("CONTENT_ENGINE_PYTHON");if(!String.IsNullOrWhiteSpace(custom))return custom;
        string bundled=Path.Combine(root,"runtime","python.exe");
        return File.Exists(bundled)?bundled:"python.exe";
    }
    Dictionary<string,object> Request(string action) {
        string script=Path.Combine(root,"scripts","workbench_control.py");
        if(!File.Exists(script))throw new IOException("请将控制台放在项目目录中，与 scripts、content_engine 文件夹放在一起。");
        var info=new ProcessStartInfo(Python(),"-X utf8 "+Quote(script)+" "+action+" --port "+port+" --data-dir "+Quote(data));
        info.WorkingDirectory=root;info.UseShellExecute=false;info.CreateNoWindow=true;info.WindowStyle=ProcessWindowStyle.Hidden;
        info.RedirectStandardOutput=true;info.RedirectStandardError=true;info.StandardOutputEncoding=Encoding.UTF8;info.StandardErrorEncoding=Encoding.UTF8;
        using(var process=Process.Start(info)) {
            var stdout=process.StandardOutput.ReadToEndAsync();var stderr=process.StandardError.ReadToEndAsync();
            if(!process.WaitForExit(45000)){try{process.Kill();}catch{}throw new IOException("控制操作超时，请稍后重试；未强制停止工作台。");}
            Task.WaitAll(stdout,stderr);
            string output=stdout.Result.Trim();if(String.IsNullOrEmpty(output))throw new IOException("无法读取控制结果，请确认 Python 3.11+ 可用。");
            return new JavaScriptSerializer().Deserialize<Dictionary<string,object>>(output);
        }
    }
    string Value(Dictionary<string,object> result,string key,string fallback="") {object value;return result.TryGetValue(key,out value)&&value!=null?Convert.ToString(value):fallback;}
    async Task Work(string action) {
        if(busy)return;busy=true;progress.Visible=true;start.Enabled=open.Enabled=stop.Enabled=false;
        if(action!="status"){status.Text=action=="start"?"正在启动工作台…":"正在安全停止…";detail.Text="请稍候，正在检查服务与任务状态。";}
        try {
            var result=await Task.Run(()=>Request(action));string state=Value(result,"state"),message=Value(result,"message");
            ready=state=="running" || state=="waiting";if(state=="stopped" || state=="error" || state=="conflict")waiting=false;
            string summary=url;string version=Value(result,"version");if(version!="")summary+="  ·  v"+version;
            if(ready)summary+="\n执行中 "+Value(result,"active","未知")+" 项  ·  排队 "+Value(result,"queued","0")+" 项";
            PaintStatus(state,message,summary);
            if(ready && openWhenReady){openWhenReady=false;OpenBrowser();}
        } catch(Exception error) {ready=false;waiting=false;PaintStatus("error","操作未完成",error.Message);}
        finally {
            busy=false;progress.Visible=false;start.Enabled=true;open.Enabled=ready;stop.Enabled=ready||waiting;stop.Text=waiting?"取消等待停止":"停止工作台";
            if(pendingAction!=null){string next=pendingAction;bool show=pendingOpen;pendingAction=null;BeginInvoke(new Action(()=>{waiting=next=="stop";openWhenReady=show;var task=Work(next);}));}
        }
    }
    void PaintStatus(string state,string message,string summary) {
        status.Text=message;status.ForeColor=state=="error"||state=="conflict"?Color.FromArgb(166,72,51):state=="stopped"?Color.FromArgb(109,121,111):Color.FromArgb(21,117,94);detail.Text=summary;
    }
    void OpenBrowser() {
        if(!allowBrowser)return;
        try{Process.Start(new ProcessStartInfo(url){UseShellExecute=true});}
        catch{detail.Text="工作台已运行；请在浏览器打开："+url;}
    }
    protected override void WndProc(ref Message message) {
        if(message.Msg==Program.OpenMessage || message.Msg==Program.StopMessage) {
            if(WindowState==FormWindowState.Minimized)WindowState=FormWindowState.Normal;Show();SetForegroundWindow(Handle);
            string action=message.Msg==Program.StopMessage?"stop":"start";
            if(busy){pendingAction=action;pendingOpen=action=="start";}
            else{waiting=action=="stop";openWhenReady=!waiting;var task=Work(action);}
            return;
        }
        base.WndProc(ref message);
    }
}
