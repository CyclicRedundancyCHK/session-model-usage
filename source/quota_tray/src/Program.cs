using System;
using System.Diagnostics;
using System.Globalization;
using System.Security.Cryptography;
using System.Text;
using System.Threading;
using System.Windows.Forms;

namespace CodexQuotaTray
{
    internal static class Program
    {
        [STAThread]
        private static void Main(string[] args)
        {
            int parentId;
            double parentCreated;
            if (args.Length != 6 || args[0] != "--manager-pid" || args[2] != "--manager-created" || args[4] != "--run-id" ||
                !int.TryParse(args[1], out parentId) || !double.TryParse(args[3], NumberStyles.Float, CultureInfo.InvariantCulture, out parentCreated)) return;
            if (double.IsNaN(parentCreated) || double.IsInfinity(parentCreated) ||
                !System.Text.RegularExpressions.Regex.IsMatch(args[5], "^[a-f0-9]{32}$")) return;
            Process parent;
            try
            {
                parent = Process.GetProcessById(parentId);
                var epoch = (parent.StartTime.ToUniversalTime() - new DateTime(1970, 1, 1)).TotalSeconds;
                if (Math.Abs(epoch - parentCreated) > 0.1) { parent.Dispose(); return; }
            }
            catch { return; }
            string key;
            using (var hash = SHA256.Create()) key = BitConverter.ToString(hash.ComputeHash(Encoding.UTF8.GetBytes(CompanionPaths.Root.ToLowerInvariant()))).Replace("-", "");
            bool createdNew;
            using (parent)
            using (var mutex = new Mutex(true, "CodexSessionUsage.Tray." + key, out createdNew))
            {
                if (!createdNew)
                {
                    return;
                }

                Application.EnableVisualStyles();
                Application.SetCompatibleTextRenderingDefault(false);
                using (var context = new TrayApplicationContext(args[5]))
                using (var monitor = new System.Windows.Forms.Timer { Interval = 1000 })
                {
                    monitor.Tick += delegate { try { if (parent.HasExited) context.ExitThread(); } catch { context.ExitThread(); } };
                    monitor.Start();
                    Application.Run(context);
                }
                GC.KeepAlive(mutex);
            }
        }

        private static void WaitForPreviousVersion(string[] args)
        {
            if (args == null || args.Length != 2 || !string.Equals(args[0], "--wait-for-pid", StringComparison.OrdinalIgnoreCase)) return;
            int processId;
            if (!int.TryParse(args[1], out processId) || processId <= 0) return;
            try
            {
                using (var process = Process.GetProcessById(processId)) process.WaitForExit(30000);
            }
            catch
            {
            }
        }
    }
}
