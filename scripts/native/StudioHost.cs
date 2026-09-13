// ============================================================
//  GI Agent Studio · 原生窗口宿主（真正的桌面程序）
//
//  以前 Studio 是用 `msedge --app=...` 打开一个"看起来像应用"的浏览器窗口，
//  本质还是网页；这个宿主把同一套界面塞进自己的 WinForms 窗口里的 WebView2 控件：
//    · 独立进程 / 独立任务栏图标与标题，没有地址栏、标签页、Edge 菜单；
//    · 由它负责拉起 Python 后端（app_web.py --no-window），窗口关掉就一起收摊；
//    · WebView2 的托管程序集与原生加载器都以资源形式嵌在本 exe 里，运行时自解压，
//      所以对外就是一个单文件 exe。
//
//  界面本身（HTML/CSS/JS）与后端接口完全没变 —— 换的只是"外壳"。
//
//  编译：scripts\build_studio_exe.ps1（Roslyn csc，.NET Framework 4.8）
// ============================================================

using System;
using System.Diagnostics;
using System.Drawing;
using System.IO;
using System.Net;
using System.Net.Sockets;
using System.Reflection;
using System.Runtime.InteropServices;
using System.Text;
using System.Threading;
using System.Windows.Forms;
using Microsoft.Web.WebView2.Core;
using Microsoft.Web.WebView2.WinForms;

namespace GiAgentStudio
{
    internal static class Program
    {
        private const string Title = "GI Agent Studio";
        private static Mutex singleInstance;

        [STAThread]
        private static void Main(string[] args)
        {
            bool createdNew;
            singleInstance = new Mutex(true, "GI_Agent_Studio_Native_Host", out createdNew);
            if (!createdNew)
            {
                MessageBox.Show("GI Agent Studio 已经在运行了。", Title,
                    MessageBoxButtons.OK, MessageBoxIcon.Information);
                return;
            }

            AppDomain.CurrentDomain.AssemblyResolve += EmbeddedAssemblies.Resolve;
            EmbeddedAssemblies.PrepareNativeLoader();
            HostLog.Write("=== 启动宿主（原生窗口）===");

            try
            {
                Dpi.TryEnablePerMonitorV2();
            }
            catch (Exception)
            {
                // 老系统上没有这个 API，忽略即可
            }

            Application.EnableVisualStyles();
            Application.SetCompatibleTextRenderingDefault(false);
            Application.Run(new StudioForm(args));
        }
    }

    /// <summary>把嵌在 exe 里的 WebView2 程序集/原生库取出来用。</summary>
    internal static class EmbeddedAssemblies
    {
        private const string CoreName = "Microsoft.Web.WebView2.Core.dll";
        private const string WinFormsName = "Microsoft.Web.WebView2.WinForms.dll";
        private const string LoaderName = "WebView2Loader.dll";

        private static string resolvedDir;

        /// <summary>
        /// 选一个**可写**的目录来放解压出来的依赖。优先级：
        /// 1) exe 旁边的 .studio-lib（搬走整个文件夹也不会丢，最直观）
        /// 2) %LOCALAPPDATA%\GI_Agent
        /// 3) %TEMP%\GI_Agent
        /// 实测踩坑：只写 LOCALAPPDATA 的话，在受限环境（沙箱/只读漫游配置）里会
        /// "访问被拒绝"，WebView2 程序集加载失败、窗口就只剩启动画面。
        /// </summary>
        public static string ResolveDir()
        {
            if (resolvedDir != null)
            {
                return resolvedDir;
            }

            string[] candidates = new string[]
            {
                Path.Combine(Program2.Root, ".studio-lib"),
                Path.Combine(
                    Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData),
                    "GI_Agent"),
                Path.Combine(Path.GetTempPath(), "GI_Agent")
            };

            foreach (string candidate in candidates)
            {
                if (IsUsable(candidate))
                {
                    resolvedDir = candidate;
                    HostLog.Write("依赖解压目录：" + candidate);
                    return resolvedDir;
                }
            }

            // 都写不了就退回 exe 目录，至少报错信息清楚
            resolvedDir = Program2.Root;
            return resolvedDir;
        }

        private static bool IsUsable(string directory)
        {
            try
            {
                Directory.CreateDirectory(directory);
                string probe = Path.Combine(directory, ".write-probe");
                File.WriteAllText(probe, "ok");
                File.Delete(probe);
                return true;
            }
            catch (Exception)
            {
                return false;
            }
        }

        /// <summary>把原生加载器放到磁盘上（原生 DLL 不能从内存加载）。</summary>
        public static void PrepareNativeLoader()
        {
            string directory = ResolveDir();
            try
            {
                string target = Path.Combine(directory, LoaderName);
                if (!File.Exists(target))
                {
                    Extract(LoaderName, target);
                }
                Native.SetDllDirectory(directory);
            }
            catch (Exception exc)
            {
                HostLog.Write("提取 WebView2Loader 失败：" + exc.Message);
            }
        }

        public static Assembly Resolve(object sender, ResolveEventArgs args)
        {
            string simpleName = new AssemblyName(args.Name).Name;
            if (string.Equals(simpleName, "Microsoft.Web.WebView2.Core", StringComparison.OrdinalIgnoreCase))
            {
                return Load(CoreName);
            }
            if (string.Equals(simpleName, "Microsoft.Web.WebView2.WinForms", StringComparison.OrdinalIgnoreCase))
            {
                return Load(WinFormsName);
            }
            return null;
        }

        private static Assembly Load(string fileName)
        {
            try
            {
                string directory = ResolveDir();
                string target = Path.Combine(directory, fileName);
                if (!File.Exists(target))
                {
                    Extract(fileName, target);
                }
                return Assembly.LoadFrom(target);
            }
            catch (Exception exc)
            {
                HostLog.Write("加载 " + fileName + " 失败：" + exc.Message);
                return null;
            }
        }

        private static void Extract(string fileName, string target)
        {
            using (Stream source = Assembly.GetExecutingAssembly().GetManifestResourceStream(fileName))
            {
                if (source == null)
                {
                    throw new FileNotFoundException("exe 里没有嵌入资源：" + fileName);
                }
                using (FileStream output = File.Create(target))
                {
                    source.CopyTo(output);
                }
            }
            HostLog.Write("已解压 " + fileName + " -> " + target);
        }
    }

    /// <summary>最小化的项目路径/日志工具（窗口还没出来时也要能记日志）。</summary>
    internal static class Program2
    {
        private static string root;

        public static string Root
        {
            get
            {
                if (root == null)
                {
                    root = AppDomain.CurrentDomain.BaseDirectory.TrimEnd('\\');
                }
                return root;
            }
        }
    }

    internal static class HostLog
    {
        /// <summary>运行期日志统一放 logs\（以前散在根目录，和项目文件混在一起）。</summary>
        internal static string LogDir
        {
            get { return Path.Combine(Program2.Root, "logs"); }
        }

        internal static string LogPath
        {
            get { return Path.Combine(LogDir, "studio-host.log"); }
        }

        public static void Write(string message)
        {
            try
            {
                string line = DateTime.Now.ToString("yyyy-MM-dd HH:mm:ss") + " | [host] " + message
                              + Environment.NewLine;
                Directory.CreateDirectory(LogDir);
                File.AppendAllText(LogPath, line, Encoding.UTF8);
            }
            catch (Exception)
            {
                // 日志失败不能影响启动
            }
        }
    }

    internal static class Native
    {
        [DllImport("kernel32.dll", CharSet = CharSet.Unicode, SetLastError = true)]
        public static extern bool SetDllDirectory(string path);

        [DllImport("dwmapi.dll")]
        public static extern int DwmSetWindowAttribute(IntPtr hwnd, int attribute, ref int value, int size);

        [DllImport("user32.dll")]
        public static extern IntPtr MonitorFromWindow(IntPtr hwnd, int flags);

        [DllImport("user32.dll", CharSet = CharSet.Unicode)]
        public static extern bool GetMonitorInfo(IntPtr monitor, ref MONITORINFO info);

        [StructLayout(LayoutKind.Sequential)]
        public struct RECT
        {
            public int Left;
            public int Top;
            public int Right;
            public int Bottom;
        }

        [StructLayout(LayoutKind.Sequential)]
        public struct MONITORINFO
        {
            public int cbSize;
            public RECT rcMonitor;
            public RECT rcWork;
            public int dwFlags;
        }

        [StructLayout(LayoutKind.Sequential)]
        public struct POINT
        {
            public int X;
            public int Y;
        }

        [StructLayout(LayoutKind.Sequential)]
        public struct MINMAXINFO
        {
            public POINT ptReserved;
            public POINT ptMaxSize;
            public POINT ptMaxPosition;
            public POINT ptMinTrackSize;
            public POINT ptMaxTrackSize;
        }

        public const int DWMWA_USE_IMMERSIVE_DARK_MODE = 20;
        public const int DWMWA_WINDOW_CORNER_PREFERENCE = 33;
        public const int DWMWCP_ROUND = 2;
        public const int WM_NCLBUTTONDOWN = 0x00A1;
        public const int HTCAPTION = 2;

        [DllImport("user32.dll")]
        public static extern bool ReleaseCapture();

        [DllImport("user32.dll", CharSet = CharSet.Unicode)]
        public static extern IntPtr SendMessage(IntPtr hwnd, int message, IntPtr wParam, IntPtr lParam);

        [DllImport("user32.dll")]
        public static extern bool SetWindowPos(IntPtr hwnd, IntPtr insertAfter,
            int x, int y, int cx, int cy, uint flags);

        public const uint SWP_NOSIZE = 0x0001;
        public const uint SWP_NOZORDER = 0x0004;
        public const uint SWP_NOACTIVATE = 0x0010;

        /// <summary>只挪位置、不动大小与层级。</summary>
        public static void MoveWindow(IntPtr handle, int x, int y)
        {
            SetWindowPos(handle, IntPtr.Zero, x, y, 0, 0, SWP_NOSIZE | SWP_NOZORDER | SWP_NOACTIVATE);
        }

        [DllImport("user32.dll")]
        public static extern bool GetWindowRect(IntPtr hwnd, ref RECT rect);

        [DllImport("user32.dll")]
        public static extern bool SetForegroundWindow(IntPtr hwnd);

        [DllImport("user32.dll")]
        public static extern IntPtr GetForegroundWindow();

        [DllImport("user32.dll")]
        public static extern bool GetCursorPos(out POINT point);

        [DllImport("user32.dll")]
        public static extern IntPtr SetCapture(IntPtr hwnd);

        [DllImport("user32.dll")]
        public static extern bool SetCursorPos(int x, int y);

        [DllImport("user32.dll")]
        public static extern void mouse_event(uint flags, int dx, int dy, uint data, IntPtr extra);

        public const uint MOUSEEVENTF_LEFTDOWN = 0x0002;
        public const uint MOUSEEVENTF_LEFTUP = 0x0004;

        public static RECT WindowRect(IntPtr handle)
        {
            RECT rect = new RECT();
            GetWindowRect(handle, ref rect);
            return rect;
        }

        public static void MouseLeftDown()
        {
            mouse_event(MOUSEEVENTF_LEFTDOWN, 0, 0, 0, IntPtr.Zero);
        }

        public static void MouseLeftUp()
        {
            mouse_event(MOUSEEVENTF_LEFTUP, 0, 0, 0, IntPtr.Zero);
        }

        /// <summary>
        /// 无边框窗口的收尾：Win11 上给圆角 + 深色边框（不然四角是直角、还可能有浅色描边）。
        /// Win10 上这些调用会失败，忽略即可。
        /// </summary>
        public static void ApplyBorderlessChrome(IntPtr handle)
        {
            try
            {
                int round = DWMWCP_ROUND;
                DwmSetWindowAttribute(handle, DWMWA_WINDOW_CORNER_PREFERENCE, ref round, sizeof(int));
                int dark = 1;
                DwmSetWindowAttribute(handle, DWMWA_USE_IMMERSIVE_DARK_MODE, ref dark, sizeof(int));
            }
            catch (Exception)
            {
            }
        }

        public static RECT WorkArea(IntPtr handle)
        {
            MONITORINFO info = new MONITORINFO();
            info.cbSize = Marshal.SizeOf(typeof(MONITORINFO));
            IntPtr monitor = MonitorFromWindow(handle, 2); // MONITOR_DEFAULTTONEAREST
            if (monitor != IntPtr.Zero && GetMonitorInfo(monitor, ref info))
            {
                return info.rcWork;
            }
            return new RECT { Left = 0, Top = 0, Right = 1920, Bottom = 1080 };
        }
    }

    internal static class Dpi
    {
        [DllImport("user32.dll")]
        private static extern bool SetProcessDpiAwarenessContext(IntPtr value);

        public static void TryEnablePerMonitorV2()
        {
            // DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2 = -4
            SetProcessDpiAwarenessContext(new IntPtr(-4));
        }
    }

    /// <summary>主窗口：先显示"正在启动"，后端就绪后再把 WebView2 铺满。</summary>
    internal sealed class StudioForm : Form
    {
        private readonly string[] extraArgs;
        private Process backend;
        private WebView2 web;
        private Label splash;
        private System.Windows.Forms.Timer pollTimer;
        private int port;
        private int attempts;
        private bool webReady;
        private bool closingByBackend;
        private bool browserOnly;
        private bool selfTest;
        private bool resizeLoopSeen;
        private int nativeResizeHit;
        private int diagHitTests;
        private int diagMouseMoves;
        private int diagNcDowns;
        private int diagClientDowns;
        private Native.RECT nativeResizeRect;
        private Native.POINT nativeResizeCursor;
        private PointF? dragCursor;
        private double dragScale = 1;

        public StudioForm(string[] args)
        {
            extraArgs = args ?? new string[0];

            Text = "GI Agent Studio";
            // 🌟 无边框：默认的 Windows 标题栏是浅色的，跟这套深色界面拼在一起很割裂，
            //    所以整条标题栏去掉，改由界面自己的顶栏承担（拖拽见 HandleDragMessage，
            //    最小化/最大化/关闭由界面按钮通过 postMessage 通知宿主）。
            //    ⚠️ 但**不能**直接 FormBorderStyle.None —— 那会连 WS_THICKFRAME 一起去掉，
            //    窗口就再也拉不动大小了（实测：能点、能拖，就是拉不大）。
            //    正确做法是留着可拉伸边框、只去掉标题栏，见下面的 CreateParams。
            FormBorderStyle = FormBorderStyle.None;
            ClientSize = new Size(1480, 960);
            MinimumSize = new Size(980, 640);
            StartPosition = FormStartPosition.CenterScreen;
            BackColor = Color.FromArgb(20, 22, 27);
            ForeColor = Color.FromArgb(220, 224, 232);
            Font = new Font("Microsoft YaHei UI", 9.5f);
            try
            {
                Icon = Icon.ExtractAssociatedIcon(Application.ExecutablePath);
            }
            catch (Exception)
            {
                // 没图标也能跑
            }

            splash = new Label();
            splash.Dock = DockStyle.Fill;
            splash.TextAlign = ContentAlignment.MiddleCenter;
            splash.ForeColor = Color.FromArgb(200, 206, 216);
            splash.Text = "正在启动 GI Agent Studio…" + Environment.NewLine + Environment.NewLine
                          + "首次启动需要几秒预热（加载项目模块），请稍等。";
            Controls.Add(splash);

            Load += OnLoaded;
            FormClosing += OnClosing;
        }

        /// <summary>
        /// 去掉**标题栏**但保留**可拉伸边框**：只有 WS_CAPTION 被清掉，
        /// WS_THICKFRAME / WS_MINIMIZEBOX / WS_MAXIMIZEBOX 补回来。
        /// 这样窗口照样能拖边缘改大小（系统原生行为，含边缘平滑与最小尺寸约束），
        /// 而且不再显示那条浅色标题栏。
        /// </summary>
        protected override CreateParams CreateParams
        {
            get
            {
                const int WS_CAPTION = 0x00C00000;      // WS_BORDER | WS_DLGFRAME
                const int WS_THICKFRAME = 0x00040000;   // 可拉伸边框
                const int WS_MINIMIZEBOX = 0x00020000;
                const int WS_MAXIMIZEBOX = 0x00010000;

                CreateParams parameters = base.CreateParams;
                parameters.Style |= WS_THICKFRAME | WS_MINIMIZEBOX | WS_MAXIMIZEBOX;
                parameters.Style &= ~WS_CAPTION;
                return parameters;
            }
        }

        private void OnLoaded(object sender, EventArgs e)
        {
            Native.ApplyBorderlessChrome(Handle);
            try
            {
                LaunchBackend();
            }
            catch (Exception exc)
            {
                HostLog.Write("启动后端失败：" + exc);
                Fail("启动后端失败：" + exc.Message);
                return;
            }

            pollTimer = new System.Windows.Forms.Timer();
            pollTimer.Interval = 700;
            pollTimer.Tick += OnPoll;
            pollTimer.Start();
        }

        private void LaunchBackend()
        {
            string root = Program2.Root;
            string python = Path.Combine(root, @"venv\Scripts\pythonw.exe");
            if (!File.Exists(python))
            {
                python = Path.Combine(root, @"venv\Scripts\python.exe");
            }
            if (!File.Exists(python))
            {
                throw new FileNotFoundException(
                    "没找到 venv\\Scripts\\pythonw.exe。请先在项目根目录建好虚拟环境：\r\n"
                    + "    python -m venv venv\r\n"
                    + "    venv\\Scripts\\python.exe -m pip install -r requirements.txt\r\n"
                    + "（当前目录：" + root + "）");
            }
            string script = Path.Combine(root, "app_web.py");
            if (!File.Exists(script))
            {
                throw new FileNotFoundException(
                    "没找到 app_web.py。请把 GI-Agent-Studio.exe 放在项目根目录再运行。\r\n（当前目录：" + root + "）");
            }

            // --browser：跳过 WebView2，直接用浏览器应用窗口（排查问题时用）
            // --selftest：加载完界面后自检"拖动窗口"链路（写进 studio-host.log）
            // --selftest：加载完界面后自检"拖动窗口"链路（写进 studio-host.log）
            // 注意 explorer 不会转发附加参数，所以也支持"在旁边放一个 studio-selftest.flag"
            // 来开启自检（调试用，正式使用不会出现这个文件）。
            selfTest = File.Exists(Path.Combine(Program2.Root, "studio-selftest.flag"));
            foreach (string argument in extraArgs)
            {
                if (string.Equals(argument, "--browser", StringComparison.OrdinalIgnoreCase))
                {
                    browserOnly = true;
                }
                if (string.Equals(argument, "--selftest", StringComparison.OrdinalIgnoreCase))
                {
                    selfTest = true;
                }
            }

            port = FreePort();
            string arguments = "\"" + script + "\" --no-window --port " + port;
            foreach (string argument in extraArgs)
            {
                string lower = argument.ToLowerInvariant();
                if (lower != "--browser" && lower != "--selftest")
                {
                    arguments += " " + argument;
                }
            }

            ProcessStartInfo info = new ProcessStartInfo(python, arguments);
            info.WorkingDirectory = root;
            info.UseShellExecute = false;
            info.CreateNoWindow = true;
            info.RedirectStandardOutput = true;
            info.RedirectStandardError = true;

            backend = new Process();
            backend.StartInfo = info;
            backend.EnableRaisingEvents = true;
            backend.Exited += OnBackendExited;
            backend.Start();

            // 两个流都要读，否则管道写满会把后端卡死
            backend.BeginOutputReadLine();
            backend.BeginErrorReadLine();
            backend.OutputDataReceived += (s, a) => HostLog.Write("out> " + a.Data);
            backend.ErrorDataReceived += (s, a) => HostLog.Write("err> " + a.Data);

            HostLog.Write("后端已启动：PID " + backend.Id + "，端口 " + port);
        }

        private static int FreePort()
        {
            TcpListener listener = new TcpListener(IPAddress.Loopback, 0);
            listener.Start();
            int value = ((IPEndPoint)listener.LocalEndpoint).Port;
            listener.Stop();
            return value;
        }

        private void OnPoll(object sender, EventArgs e)
        {
            if (backend != null && backend.HasExited && !webReady)
            {
                pollTimer.Stop();
                Fail("后端进程启动后立刻退出了。\r\n\r\n最近日志（studio.log / studio-host.log）：\r\n"
                     + Tail());
                return;
            }

            attempts++;
            if (IsBackendReady())
            {
                pollTimer.Stop();
                HostLog.Write("后端就绪（第 " + attempts + " 次探测）");
                InitialiseWebView();
                return;
            }

            if (attempts == 4)
            {
                splash.Text = "正在启动 GI Agent Studio…" + Environment.NewLine + Environment.NewLine
                              + "预热中（首次启动要加载项目模块，可能要十几秒）";
            }
            if (attempts > 120)
            {
                pollTimer.Stop();
                Fail("等了 80 秒后端还没就绪。\r\n\r\n最近日志：\r\n" + Tail());
            }
        }

        private bool IsBackendReady()
        {
            try
            {
                HttpWebRequest request = (HttpWebRequest)WebRequest.Create(
                    "http://127.0.0.1:" + port + "/api/state");
                request.Timeout = 2500;
                request.ReadWriteTimeout = 2500;
                using (HttpWebResponse response = (HttpWebResponse)request.GetResponse())
                {
                    return response.StatusCode == HttpStatusCode.OK;
                }
            }
            catch (Exception)
            {
                return false;
            }
        }

        private async void InitialiseWebView()
        {
            string url = "http://127.0.0.1:" + port + "/";
            string version = DescribeWebViewRuntime();
            Exception last = null;

            if (browserOnly)
            {
                HostLog.Write("按 --browser 要求，直接用浏览器应用窗口。");
                OpenInBrowserWindow(url, version, new Exception("指定了 --browser"));
                return;
            }

            foreach (string[] attempt in WebViewAttempts())
            {
                string folder = attempt[0];
                string argument = attempt[1];
                string label = "目录=" + folder + " 参数=" + (argument ?? "默认");
                WebView2 candidate = null;
                try
                {
                    Directory.CreateDirectory(folder);

                    candidate = new WebView2();
                    candidate.Dock = DockStyle.Fill;
                    candidate.Visible = false;
                    candidate.DefaultBackgroundColor = Color.FromArgb(20, 22, 27);
                    Controls.Add(candidate);
                    candidate.BringToFront();

                    CoreWebView2EnvironmentOptions options =
                        argument == null ? null : new CoreWebView2EnvironmentOptions(argument);
                    CoreWebView2Environment environment =
                        await CoreWebView2Environment.CreateAsync(null, folder, options);
                    await candidate.EnsureCoreWebView2Async(environment);

                    web = candidate;
                    ApplySettings();
                    HostLog.Write("WebView2 初始化成功：" + label + "｜运行时 " + version);
                    web.CoreWebView2.Navigate(url);
                    return;
                }
                catch (Exception exc)
                {
                    last = exc;
                    HostLog.Write("WebView2 初始化失败（" + label + "）：" + exc.Message);
                    try
                    {
                        if (candidate != null)
                        {
                            Controls.Remove(candidate);
                            candidate.Dispose();
                        }
                    }
                    catch (Exception)
                    {
                    }
                }
            }

            HostLog.Write("WebView2 全部尝试都失败：" + (last == null ? "未知" : last.ToString()));
            if (!OpenInBrowserWindow(url, version, last))
            {
                Fail("WebView2 起不来：" + (last == null ? "未知错误" : last.Message)
                     + "\r\n\r\n请安装 WebView2 运行时后重试：\r\n"
                     + "https://developer.microsoft.com/microsoft-edge/webview2/"
                     + "\r\n\r\n（检测到的运行时：" + version + "）");
            }
        }

        /// <summary>
        /// 依次尝试"用户数据目录 × 浏览器参数"的组合。
        /// 受限环境（沙箱 / 只读漫游配置 / 老驱动 / OneDrive 占位文件）里，
        /// Chromium 的沙箱或 GPU 进程常常起不来，报 0x8000FFFF(E_UNEXPECTED)，
        /// 这时换个目录或加 --disable-gpu / --no-sandbox 往往就好了。
        /// </summary>
        private static System.Collections.Generic.List<string[]> WebViewAttempts()
        {
            string projectProfile = Path.Combine(Program2.Root, ".studio-profile");
            string projectProfileSub = Path.Combine(projectProfile, "wv2");
            string tempProfile = Path.Combine(Path.GetTempPath(), "GI_Agent-wv2");

            System.Collections.Generic.List<string[]> attempts =
                new System.Collections.Generic.List<string[]>();
            attempts.Add(new string[] { projectProfileSub, null });
            attempts.Add(new string[] { projectProfileSub, "--disable-gpu" });
            attempts.Add(new string[] { tempProfile, null });
            attempts.Add(new string[] { tempProfile, "--disable-gpu" });
            attempts.Add(new string[] { tempProfile, "--disable-gpu --no-sandbox" });
            return attempts;
        }

        private void ApplySettings()
        {
            CoreWebView2Settings settings = web.CoreWebView2.Settings;
            settings.AreDefaultContextMenusEnabled = false;
            settings.AreDevToolsEnabled = false;
            settings.IsStatusBarEnabled = false;
            settings.IsZoomControlEnabled = false;
            settings.AreBrowserAcceleratorKeysEnabled = false;
            settings.IsPasswordAutosaveEnabled = false;
            settings.IsGeneralAutofillEnabled = false;
            settings.IsSwipeNavigationEnabled = false;
            try
            {
                // 让界面里的 `-webkit-app-region: drag` 生效：顶栏可以拖动窗口。
                // 不开这个开关的话，无边框窗口就没有任何办法移动了。
                settings.IsNonClientRegionSupportEnabled = true;
            }
            catch (Exception exc)
            {
                HostLog.Write("开启非客户区拖拽失败：" + exc.Message);
            }

            web.CoreWebView2.WebMessageReceived += OnWebMessage;
            web.CoreWebView2.DocumentTitleChanged += (s, a) =>
            {
                string title = web.CoreWebView2.DocumentTitle;
                Text = string.IsNullOrEmpty(title) ? "GI Agent Studio" : title;
            };
            web.CoreWebView2.NavigationCompleted += (s, a) =>
            {
                webReady = true;
                web.Visible = true;
                splash.Visible = false;
                HostLog.Write("界面已加载：" + web.Source);
                if (selfTest)
                {
                    RunDragSelfTest();
                }
            };
        }

        /// <summary>
        /// `--selftest`：让界面走一遍"拖动顶栏"的消息流程，检查窗口是否真的移动。
        /// 真实鼠标注入在受限环境（沙箱/UIPI）里进不来，所以用这条可编程链路自检。
        /// </summary>
        private async void RunDragSelfTest()
        {
            try
            {
                await System.Threading.Tasks.Task.Delay(500);
                int beforeX = Left;
                int beforeY = Top;
                string result = await web.CoreWebView2.ExecuteScriptAsync("window.__giDragProbe(120, 60)");
                await System.Threading.Tasks.Task.Delay(800);
                HostLog.Write("自检：窗口 " + beforeX + "," + beforeY + " -> " + Left + "," + Top
                              + "（位移 " + (Left - beforeX) + "," + (Top - beforeY) + "），脚本返回 " + result);
                HostLog.Write("自检结论：" + ((Left - beforeX) == 120 && (Top - beforeY) == 60
                    ? "✅ 拖动链路正常（界面 → 宿主 → 移动窗口）"
                    : "❌ 拖动链路异常"));

                // 顺带验证窗口按钮那条消息通路（最大化 → 还原）
                await web.CoreWebView2.ExecuteScriptAsync("window.__giWindowProbe('maximize')");
                await System.Threading.Tasks.Task.Delay(600);
                HostLog.Write("自检：最大化后 WindowState=" + WindowState);
                await web.CoreWebView2.ExecuteScriptAsync("window.__giWindowProbe('restore')");
                await System.Threading.Tasks.Task.Delay(600);
                HostLog.Write("自检：还原后 WindowState=" + WindowState);

                // 真·拉伸自检：在**本进程内**注入真实鼠标输入拖右下角，
                // 看窗口尺寸是否真的变了（外面注入会被 UIPI 拦掉，进程内注入不会）。
                await System.Threading.Tasks.Task.Delay(400);
                RunResizeSelfTest();
            }
            catch (Exception exc)
            {
                HostLog.Write("自检失败：" + exc);
            }
        }

        private void RunResizeSelfTest()
        {
            // 先把窗口摆到一个"边角一定在屏幕内"的位置与尺寸，
            // 否则注入的鼠标点跑到屏幕外，拉伸自然无效（第一次自检就是这么误判的）。
            Rectangle work = Screen.PrimaryScreen.WorkingArea;
            int probeWidth = Math.Min(900, Math.Max(600, work.Width - 240));
            int probeHeight = Math.Min(620, Math.Max(420, work.Height - 240));
            Native.SetWindowPos(Handle, IntPtr.Zero, work.Left + 80, work.Top + 80,
                probeWidth, probeHeight, Native.SWP_NOZORDER | Native.SWP_NOACTIVATE);
            System.Threading.Thread.Sleep(500);

            Native.RECT before = Native.WindowRect(Handle);
            int width = before.Right - before.Left;
            int height = before.Bottom - before.Top;
            HostLog.Write("自检：屏幕工作区 " + work.Width + "x" + work.Height
                          + "；拉伸前窗口 " + before.Left + "," + before.Top + " "
                          + width + "x" + height);

            try
            {
                // 注入的鼠标会落在"光标底下那个窗口"上，所以先把自己顶到最前面
                TopMost = true;
                Activate();
                Native.SetForegroundWindow(Handle);
                System.Threading.Thread.Sleep(500);
                HostLog.Write("自检：前台窗口是本窗口 = "
                              + (Native.GetForegroundWindow() == Handle));

                resizeLoopSeen = false;
                int startX = before.Right - 3;
                int startY = before.Bottom - 3;
                Native.SetCursorPos(startX, startY);
                System.Threading.Thread.Sleep(220);
                Native.MouseLeftDown();
                for (int step = 1; step <= 14; step++)
                {
                    Native.SetCursorPos(startX + step * 9, startY + step * 6);
                    System.Threading.Thread.Sleep(35);
                }
                System.Threading.Thread.Sleep(180);
                Native.MouseLeftUp();
                System.Threading.Thread.Sleep(600);
                HostLog.Write("自检：WM_ENTERSIZEMOVE=" + resizeLoopSeen
                              + "｜命中测试=" + diagHitTests
                              + "｜鼠标移动=" + diagMouseMoves
                              + "｜非客户区按下=" + diagNcDowns
                              + "｜客户区按下=" + diagClientDowns);
                diagHitTests = diagMouseMoves = diagNcDowns = diagClientDowns = 0;
                TopMost = false;
            }
            catch (Exception exc)
            {
                HostLog.Write("自检：注入鼠标失败：" + exc.Message);
            }

            Native.RECT after = Native.WindowRect(Handle);
            int widthAfter = after.Right - after.Left;
            int heightAfter = after.Bottom - after.Top;
            HostLog.Write("自检：拉伸后 " + widthAfter + "x" + heightAfter
                          + "（变化 " + (widthAfter - width) + "," + (heightAfter - height) + "）");
            if (diagHitTests == 0 && diagMouseMoves == 0 && diagNcDowns == 0 && diagClientDowns == 0)
            {
                // 一条鼠标消息都没收到 = 注入根本没进来（受限环境常见），这次自检不作数，
                // 免得误报成"拉伸无效"。
                HostLog.Write("自检结论：⚠️ 注入的鼠标输入没有送达本窗口（消息计数全 0），"
                              + "本环境无法自动验证拉伸；请用真实鼠标拖窗口边缘确认。");
            }
            else
            {
                HostLog.Write("自检结论：" + ((widthAfter - width) > 40
                    ? "✅ 窗口可以自由拉伸"
                    : "❌ 拉伸无效（检查 WS_THICKFRAME 与边缘命中测试）"));
            }
        }

        /// <summary>界面顶栏的自绘窗口按钮通过 postMessage 通知宿主。</summary>
        private void OnWebMessage(object sender, CoreWebView2WebMessageReceivedEventArgs e)
        {
            string payload;
            try
            {
                payload = e.TryGetWebMessageAsString();
            }
            catch (Exception)
            {
                return;
            }
            if (string.IsNullOrEmpty(payload))
            {
                return;
            }

            if (payload.IndexOf("\"drag\"", StringComparison.OrdinalIgnoreCase) >= 0)
            {
                HandleDragMessage(payload);
                return;
            }

            HostLog.Write("界面消息：" + payload);
            if (payload.IndexOf("\"minimize\"", StringComparison.OrdinalIgnoreCase) >= 0)
            {
                WindowState = FormWindowState.Minimized;
            }
            else if (payload.IndexOf("\"restore\"", StringComparison.OrdinalIgnoreCase) >= 0)
            {
                WindowState = FormWindowState.Normal;
            }
            else if (payload.IndexOf("\"maximize\"", StringComparison.OrdinalIgnoreCase) >= 0)
            {
                WindowState = WindowState == FormWindowState.Maximized
                    ? FormWindowState.Normal
                    : FormWindowState.Maximized;
            }
            else if (payload.IndexOf("\"close\"", StringComparison.OrdinalIgnoreCase) >= 0)
            {
                Close();
            }
        }

        /// <summary>
        /// 界面顶栏拖拽。
        /// WebView2 虽然支持 `app-region: drag`，但在"宿主自绘窗口 + WinForms 版控件"里
        /// 实测命中测试仍然返回 HTCLIENT（拖不动），所以这里走界面上报的屏幕坐标自己搬窗口：
        /// JS 只发屏幕坐标与 devicePixelRatio，宿主按增量移动，缩放屏也不会错位。
        /// </summary>
        private void HandleDragMessage(string payload)
        {
            try
            {
                string phase = Match(payload, "\"phase\"\\s*:\\s*\"(\\w+)\"");
                double x = ParseDouble(Match(payload, "\"x\"\\s*:\\s*(-?[0-9.]+)"), 0);
                double y = ParseDouble(Match(payload, "\"y\"\\s*:\\s*(-?[0-9.]+)"), 0);
                double scale = ParseDouble(Match(payload, "\"scale\"\\s*:\\s*(-?[0-9.]+)"), 1);
                if (scale <= 0)
                {
                    scale = 1;
                }

                if (phase == "start")
                {
                    if (WindowState == FormWindowState.Maximized)
                    {
                        WindowState = FormWindowState.Normal;
                    }
                    dragCursor = new PointF((float)x, (float)y);
                    dragScale = scale;
                    return;
                }
                if (phase == "end")
                {
                    dragCursor = null;
                    return;
                }
                if (phase != "move" || dragCursor == null)
                {
                    return;
                }

                PointF previous = dragCursor.Value;
                dragCursor = new PointF((float)x, (float)y);
                int dx = (int)Math.Round(((float)x - previous.X) * dragScale);
                int dy = (int)Math.Round(((float)y - previous.Y) * dragScale);
                if (dx == 0 && dy == 0)
                {
                    return;
                }
                Native.MoveWindow(Handle, Left + dx, Top + dy);
            }
            catch (Exception exc)
            {
                HostLog.Write("拖拽处理失败：" + exc.Message);
            }
        }

        /// <summary>把命中代码算成的 8 个方向，按光标增量改写窗口矩形（受 MinimumSize 约束）。</summary>
        private static bool IsResizeHit(int hit)
        {
            return hit == 10 || hit == 11 || hit == 12 || hit == 13
                   || hit == 14 || hit == 15 || hit == 16 || hit == 17;
        }

        private void ApplyNativeResize()
        {
            Native.POINT cursor;
            Native.GetCursorPos(out cursor);
            int dx = cursor.X - nativeResizeCursor.X;
            int dy = cursor.Y - nativeResizeCursor.Y;

            int left = nativeResizeRect.Left;
            int top = nativeResizeRect.Top;
            int right = nativeResizeRect.Right;
            int bottom = nativeResizeRect.Bottom;

            int minWidth = Math.Max(480, MinimumSize.Width);
            int minHeight = Math.Max(360, MinimumSize.Height);

            bool leftEdge = nativeResizeHit == 10 || nativeResizeHit == 13 || nativeResizeHit == 16;
            bool rightEdge = nativeResizeHit == 11 || nativeResizeHit == 14 || nativeResizeHit == 17;
            bool topEdge = nativeResizeHit == 12 || nativeResizeHit == 13 || nativeResizeHit == 14;
            bool bottomEdge = nativeResizeHit == 15 || nativeResizeHit == 16 || nativeResizeHit == 17;

            if (leftEdge)
            {
                left = Math.Min(nativeResizeRect.Left + dx, right - minWidth);
            }
            if (rightEdge)
            {
                right = Math.Max(nativeResizeRect.Right + dx, left + minWidth);
            }
            if (topEdge)
            {
                top = Math.Min(nativeResizeRect.Top + dy, bottom - minHeight);
            }
            if (bottomEdge)
            {
                bottom = Math.Max(nativeResizeRect.Bottom + dy, top + minHeight);
            }

            Native.SetWindowPos(Handle, IntPtr.Zero, left, top, right - left, bottom - top,
                Native.SWP_NOZORDER | Native.SWP_NOACTIVATE);
        }

        private static string Match(string text, string pattern)
        {
            System.Text.RegularExpressions.Match match =
                System.Text.RegularExpressions.Regex.Match(text, pattern,
                    System.Text.RegularExpressions.RegexOptions.IgnoreCase);
            return match.Success ? match.Groups[1].Value : null;
        }

        private static double ParseDouble(string text, double fallback)
        {
            double value;
            return double.TryParse(text, System.Globalization.NumberStyles.Float,
                System.Globalization.CultureInfo.InvariantCulture, out value) ? value : fallback;
        }

        protected override void WndProc(ref Message m)
        {
            const int WM_NCHITTEST = 0x0084;
            const int WM_GETMINMAXINFO = 0x0024;
            const int WM_ENTERSIZEMOVE = 0x0231;
            const int WM_NCLBUTTONDOWN = 0x00A1;
            const int WM_MOUSEMOVE = 0x0200;
            const int WM_LBUTTONDOWN_MSG = 0x0201;
            const int WM_LBUTTONUP = 0x0202;
            const int WM_NCLBUTTONUP = 0x00A2;

            if (m.Msg == WM_ENTERSIZEMOVE)
            {
                resizeLoopSeen = true;   // 自检用：系统真的开始拖大小了
            }
            if (selfTest)
            {
                if (m.Msg == WM_NCHITTEST) { diagHitTests++; }
                else if (m.Msg == WM_MOUSEMOVE) { diagMouseMoves++; }
                else if (m.Msg == WM_NCLBUTTONDOWN) { diagNcDowns++; }
                else if (m.Msg == WM_LBUTTONDOWN_MSG) { diagClientDowns++; }
            }

            // 🌟 自己实现边缘拉伸。
            // 为什么不用系统的：`FormBorderStyle.None` 会连 WS_THICKFRAME 一起拿掉，
            // 窗口就再也拉不动；把 WS_THICKFRAME 加回来之后，命中测试能返回 HTLEFT 之类，
            // 但实测系统**不会**因此进入自己的尺寸调整循环（收不到 WM_ENTERSIZEMOVE），
            // 所以这里干脆接管：按下边缘时自己 SetCapture，移动时按光标增量改窗口矩形。
            if (m.Msg == WM_NCLBUTTONDOWN)
            {
                int hit = m.WParam.ToInt32();
                if (IsResizeHit(hit))
                {
                    nativeResizeHit = hit;
                    nativeResizeRect = Native.WindowRect(Handle);
                    Native.POINT cursor;
                    Native.GetCursorPos(out cursor);
                    nativeResizeCursor = cursor;
                    Native.SetCapture(Handle);
                    m.Result = IntPtr.Zero;
                    return;
                }
            }
            else if (m.Msg == WM_MOUSEMOVE && nativeResizeHit != 0)
            {
                ApplyNativeResize();
                return;
            }
            else if ((m.Msg == WM_LBUTTONUP || m.Msg == WM_NCLBUTTONUP) && nativeResizeHit != 0)
            {
                nativeResizeHit = 0;
                Native.ReleaseCapture();
                m.Result = IntPtr.Zero;
                return;
            }

            if (m.Msg == WM_GETMINMAXINFO && WindowState == FormWindowState.Maximized)
            {
                // 无边框窗口最大化时会盖住任务栏；按显示器工作区收一下
                Native.RECT work = Native.WorkArea(Handle);
                Native.MINMAXINFO info = (Native.MINMAXINFO)Marshal.PtrToStructure(
                    m.LParam, typeof(Native.MINMAXINFO));
                info.ptMaxPosition.X = work.Left;
                info.ptMaxPosition.Y = work.Top;
                info.ptMaxSize.X = work.Right - work.Left;
                info.ptMaxSize.Y = work.Bottom - work.Top;
                info.ptMaxTrackSize.X = info.ptMaxSize.X;
                info.ptMaxTrackSize.Y = info.ptMaxSize.Y;
                Marshal.StructureToPtr(info, m.LParam, false);
                m.Result = IntPtr.Zero;
                return;
            }

            if (m.Msg == WM_NCHITTEST && WindowState == FormWindowState.Normal && webReady)
            {
                // 无边框窗口没有系统边框，得自己把"边缘几像素"报成可拉伸区域。
                // 顶栏的拖拽交给 WebView2 的 app-region: drag 处理（见 ApplySettings），
                // 所以这里**不能**把整条顶栏都当成标题栏，否则会抢掉界面上的按钮点击。
                int screenX = unchecked((short)(long)m.LParam);
                int screenY = unchecked((short)((long)m.LParam >> 16));
                Point client = PointToClient(new Point(screenX, screenY));
                const int grip = 6;
                bool left = client.X <= grip;
                bool right = client.X >= ClientSize.Width - grip;
                bool top = client.Y <= grip;
                bool bottom = client.Y >= ClientSize.Height - grip;

                int hit = 0;
                if (top && left) hit = 13;        // HTTOPLEFT
                else if (top && right) hit = 14;  // HTTOPRIGHT
                else if (bottom && left) hit = 16;   // HTBOTTOMLEFT
                else if (bottom && right) hit = 17;  // HTBOTTOMRIGHT
                else if (left) hit = 10;          // HTLEFT
                else if (right) hit = 11;         // HTRIGHT
                else if (top) hit = 12;           // HTTOP
                else if (bottom) hit = 15;        // HTBOTTOM

                if (hit != 0)
                {
                    m.Result = (IntPtr)hit;
                    return;
                }
            }

            base.WndProc(ref m);
        }

        private static string DescribeWebViewRuntime()
        {
            try
            {
                string value = CoreWebView2Environment.GetAvailableBrowserVersionString();
                return string.IsNullOrEmpty(value) ? "未检测到" : value;
            }
            catch (Exception exc)
            {
                return "检测失败（" + exc.Message + "）";
            }
        }

        /// <summary>
        /// 依次尝试几种创建方式：默认配置 → 换独立用户数据目录 → 加 --disable-gpu。
        /// 受限环境（沙箱、只读漫游配置、老显卡驱动）里经常只有某一种能用。
        /// </summary>
        private async System.Threading.Tasks.Task<CoreWebView2Environment> CreateEnvironmentWithFallbacks()
        {
            string profileRoot = Path.Combine(Program2.Root, ".studio-profile");
            string[] folders = new string[]
            {
                profileRoot,
                Path.Combine(profileRoot, "wv2"),
                Path.Combine(Path.GetTempPath(), "GI_Agent-wv2")
            };
            string[] arguments = new string[] { null, "--disable-gpu" };

            Exception last = null;
            foreach (string folder in folders)
            {
                foreach (string argument in arguments)
                {
                    try
                    {
                        Directory.CreateDirectory(folder);
                        CoreWebView2EnvironmentOptions options = null;
                        if (argument != null)
                        {
                            options = new CoreWebView2EnvironmentOptions(argument);
                        }
                        CoreWebView2Environment environment =
                            await CoreWebView2Environment.CreateAsync(null, folder, options);
                        HostLog.Write("WebView2 环境创建成功：目录=" + folder
                                      + " 参数=" + (argument ?? "（默认）"));
                        return environment;
                    }
                    catch (Exception exc)
                    {
                        last = exc;
                        HostLog.Write("WebView2 环境创建失败（目录=" + folder + " 参数="
                                      + (argument ?? "默认") + "）：" + exc.Message);
                    }
                }
            }
            throw last ?? new InvalidOperationException("无法创建 WebView2 环境");
        }

        /// <summary>兜底：用 Edge/Chrome 的 --app 窗口打开界面（和旧版 Studio 一样）。</summary>
        private bool OpenInBrowserWindow(string url, string version, Exception reason)
        {
            string[] candidates = new string[]
            {
                @"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
                @"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
                @"C:\Program Files\Google\Chrome\Application\chrome.exe"
            };
            string browser = null;
            foreach (string candidate in candidates)
            {
                if (File.Exists(candidate))
                {
                    browser = candidate;
                    break;
                }
            }
            if (browser == null)
            {
                return false;
            }

            try
            {
                string profile = Path.Combine(Program2.Root, ".studio-profile");
                Directory.CreateDirectory(profile);
                Process.Start(new ProcessStartInfo(browser,
                    "--app=" + url + " --window-size=1400,920 --user-data-dir=\"" + profile
                    + "\" --no-first-run --no-default-browser-check")
                {
                    UseShellExecute = false,
                    CreateNoWindow = true
                });
                HostLog.Write("WebView2 不可用，已改用浏览器窗口打开：" + browser);
                MessageBox.Show(
                    "这个环境里 WebView2 控件起不来（" + reason.Message + "），\r\n"
                    + "已经改用浏览器窗口打开界面，功能完全一样。\r\n\r\n"
                    + "想用原生窗口就装一下 WebView2 运行时：\r\n"
                    + "https://developer.microsoft.com/microsoft-edge/webview2/\r\n\r\n"
                    + "（检测到的运行时：" + version + "）",
                    "GI Agent Studio", MessageBoxButtons.OK, MessageBoxIcon.Warning);
                Close();
                return true;
            }
            catch (Exception exc)
            {
                HostLog.Write("浏览器兜底也失败：" + exc.Message);
                return false;
            }
        }

        private void OnBackendExited(object sender, EventArgs e)
        {
            try
            {
                BeginInvoke(new Action(() =>
                {
                    if (!closingByBackend && !Disposing)
                    {
                        HostLog.Write("后端退出，关闭窗口");
                        closingByBackend = true;
                        Close();
                    }
                }));
            }
            catch (Exception)
            {
                // 窗口正在销毁时忽略
            }
        }

        private void OnClosing(object sender, FormClosingEventArgs e)
        {
            try
            {
                if (pollTimer != null)
                {
                    pollTimer.Stop();
                }
            }
            catch (Exception)
            {
            }

            if (backend != null)
            {
                try
                {
                    if (!backend.HasExited)
                    {
                        HostLog.Write("窗口关闭，结束后端 PID " + backend.Id);
                        Process.Start(new ProcessStartInfo("taskkill",
                            "/PID " + backend.Id + " /T /F")
                        {
                            UseShellExecute = false,
                            CreateNoWindow = true
                        });
                        backend.WaitForExit(4000);
                    }
                }
                catch (Exception exc)
                {
                    HostLog.Write("结束后端失败：" + exc.Message);
                }
                backend.Dispose();
                backend = null;
            }
        }

        private string Tail()
        {
            StringBuilder builder = new StringBuilder();
            foreach (string name in new string[] { "studio-host.log", "studio.log" })
            {
                try
                {
                    string path = Path.Combine(HostLog.LogDir, name);
                    if (!File.Exists(path))
                    {
                        continue;
                    }
                    string[] lines = File.ReadAllLines(path);
                    int start = Math.Max(0, lines.Length - 12);
                    builder.AppendLine("--- " + name);
                    for (int i = start; i < lines.Length; i++)
                    {
                        builder.AppendLine(lines[i]);
                    }
                }
                catch (Exception)
                {
                }
            }
            return builder.Length == 0 ? "（没有日志）" : builder.ToString();
        }

        private void Fail(string message)
        {
            HostLog.Write("失败：" + message.Replace(Environment.NewLine, " "));
            MessageBox.Show(message, "GI Agent Studio", MessageBoxButtons.OK, MessageBoxIcon.Error);
            Close();
        }
    }
}
