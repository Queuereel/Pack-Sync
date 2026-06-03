// TrayHelper.cs — minimal WinForms tray icon for Pack Sync
// Launched as a child process by pack_sync.py.
// Protocol (all lines terminated with \n):
//   stdin  <- "QUIT"                        Python asks helper to exit
//   stdin  <- "RGBA:<n>:<b64>"              Python sends 32x32 RGBA icon bytes (base64)
//   stdin  <- "BALLOON:<title>|<text>|<ms>" Python requests a balloon tip
//   stdout -> "READY"                       icon is visible in the tray
//   stdout -> "OPEN"                        user chose Open / double-clicked
//   stdout -> "SYNC"                        user triggered Sync All
//   stdout -> "QUIT"                        user chose Quit (helper then exits)
//   stdout -> "BALLOON_CLICK"               user clicked the balloon tip

using System;
using System.Drawing;
using System.Drawing.Imaging;
using System.IO;
using System.Threading;
using System.Windows.Forms;

class Program {
    [STAThread]
    static void Main(string[] args) {
        Application.EnableVisualStyles();
        Application.SetCompatibleTextRenderingDefault(false);
        Application.Run(new TrayContext(args.Length > 0 ? args[0] : null));
    }
}

class TrayContext : ApplicationContext {
    private readonly NotifyIcon _icon;
    private bool _quitting = false;

    public TrayContext(string icoPath) {
        Icon ico = null;
        if (icoPath != null && File.Exists(icoPath)) {
            try { ico = new Icon(icoPath, 32, 32); } catch { }
        }
        ico = ico ?? SystemIcons.Application;

        // Context menu
        var menu = new ContextMenuStrip();

        var openItem = new ToolStripMenuItem("Open Pack Sync");
        openItem.Font = new Font(openItem.Font, openItem.Font.Style | FontStyle.Bold);
        openItem.Click += (s, e) => Send("OPEN");
        menu.Items.Add(openItem);

        var syncItem = new ToolStripMenuItem("Sync All");
        syncItem.Click += (s, e) => Send("SYNC");
        menu.Items.Add(syncItem);

        menu.Items.Add(new ToolStripSeparator());

        var quitItem = new ToolStripMenuItem("Quit");
        quitItem.Click += (s, e) => DoQuit();
        menu.Items.Add(quitItem);

        _icon = new NotifyIcon {
            Icon             = ico,
            Text             = "Pack Sync",
            ContextMenuStrip = menu,
            Visible          = true,
        };
        _icon.MouseDoubleClick += (s, e) => {
            if (e.Button == MouseButtons.Left) Send("OPEN");
        };
        _icon.BalloonTipClicked += (s, e) => Send("BALLOON_CLICK");

        // Tell Python the icon is live
        Send("READY");

        // Background thread: read commands from Python via stdin
        var t = new Thread(() => {
            try {
                string line;
                while ((line = Console.ReadLine()) != null) {
                    if (line.Trim().Equals("QUIT", StringComparison.OrdinalIgnoreCase)) {
                        DoQuit();
                        return;
                    }
                    if (line.StartsWith("RGBA:")) {
                        var newIco = TryLoadRgba(line);
                        if (newIco != null) _icon.Icon = newIco;
                        continue;
                    }
                    if (line.StartsWith("BALLOON:")) {
                        var data  = line.Substring(8);
                        var parts = data.Split(new char[]{'|'}, 3);
                        int ms = 5000;
                        if (parts.Length >= 3) int.TryParse(parts[2], out ms);
                        _icon.BalloonTipTitle = parts.Length >= 1 ? parts[0] : "Pack Sync";
                        _icon.BalloonTipText  = parts.Length >= 2 ? parts[1] : "";
                        _icon.BalloonTipIcon  = ToolTipIcon.Info;
                        _icon.ShowBalloonTip(ms);
                        continue;
                    }
                }
            } catch { }
            // stdin closed — parent process died, clean up
            DoQuit();
        }) { IsBackground = true, Name = "stdin-reader" };
        t.Start();
    }

    // Parse "RGBA:<size>:<base64_rgba>" and build an Icon.
    private static Icon TryLoadRgba(string line) {
        try {
            var parts = line.Split(new char[]{':'}, 3);
            if (parts.Length < 3) return null;
            int size;
            if (!int.TryParse(parts[1], out size) || size < 1 || size > 256) return null;
            byte[] rgba = Convert.FromBase64String(parts[2]);
            if (rgba.Length != size * size * 4) return null;

            var bmp = new Bitmap(size, size, PixelFormat.Format32bppArgb);
            for (int y = 0; y < size; y++)
                for (int x = 0; x < size; x++) {
                    int i = (y * size + x) * 4;
                    // PIL sends R,G,B,A — Color.FromArgb wants A,R,G,B
                    bmp.SetPixel(x, y, Color.FromArgb(rgba[i+3], rgba[i], rgba[i+1], rgba[i+2]));
                }
            return Icon.FromHandle(bmp.GetHicon());
        } catch { return null; }
    }

    private void Send(string msg) {
        Console.WriteLine(msg);
        Console.Out.Flush();
    }

    private void DoQuit() {
        if (_quitting) return;
        _quitting = true;
        Send("QUIT");
        _icon.Visible = false;
        Application.Exit();
    }

    protected override void Dispose(bool disposing) {
        if (disposing) {
            _icon.Visible = false;
            _icon.Dispose();
        }
        base.Dispose(disposing);
    }
}
