using System;
using System.Drawing;
using System.Drawing.Imaging;
using System.IO;
using System.Windows.Forms;
using CodexQuotaTray;

internal static class CombinedPreview
{
    [STAThread]
    private static int Main(string[] args)
    {
        if (args.Length != 1 && !(args.Length == 2 && (args[1] == "--interactive" || args[1] == "--countdown" || args[1] == "--context"))) return 1;
        Application.EnableVisualStyles();
        Application.SetCompatibleTextRenderingDefault(false);
        Directory.CreateDirectory(args[0]);
        if (args.Length == 2 && args[1] == "--context")
        {
            foreach (var mode in new[] { "dark", "light" })
            foreach (var theme in new[] { "mono", "glacier", "rose", "champagne" })
            foreach (var language in new[] { "zh", "en" })
            foreach (var font in new[] { 100, 120 })
            foreach (var dpi in new[] { 1f, 1.5f })
            using (var menu = new ThemedContextMenu(new AppSettings { AppearanceMode = mode, ThemeId = theme,
                Language = language, FontScalePercent = font, GlassEnabled = false }, true))
            {
                var labels = language == "en" ? new[] { "Account quota", "Current session usage", "Reconnect session", "Refresh now", "Quit" }
                    : new[] { "账号额度", "当前会话用量", "恢复会话连接", "立即刷新", "退出" };
                for (var i = 0; i < labels.Length; i++)
                {
                    if (i == 4) menu.Items.Add(new ToolStripSeparator());
                    menu.Items.Add(new ToolStripMenuItem(labels[i]) { Tag = i == 4 ? "danger" : null });
                }
                using (var bitmap = menu.RenderPreview(dpi))
                    bitmap.Save(Path.Combine(args[0], "context-" + mode + "-" + theme + "-" + language + "-" + font + "-" +
                        dpi.ToString(System.Globalization.CultureInfo.InvariantCulture) + ".png"), ImageFormat.Png);
            }
            return 0;
        }
        var recent = new RecentThreadSnapshot { Available = true };
        var chatNames = new[] { "添加最近会话弹出与打开", "声音克隆模型与默认音色", "人工智能发现与演示", "鼠标切换会话用量修复", "学习平台管理功能完善", "跨平台 UI 与无障碍检查" };
        for (var i = 0; i < chatNames.Length; i++)
        {
            var id = "00000000-0000-0000-0000-" + (i + 1).ToString("D12");
            recent.Targets.Add(new TaskNavigationTarget { ThreadId = id });
            recent.Titles[id] = chatNames[i];
        }
        foreach (var mode in new[] { "dark", "light" })
        foreach (var dpi in new[] { 1f, 1.5f })
        foreach (var font in new[] { 100, 120 })
        using (var menu = CodexThreadNavigator.RecentMenu(recent, false, target => { }, null,
            new AppSettings { AppearanceMode = mode, ThemeId = "mono", FontScalePercent = font, GlassEnabled = false }))
        using (var bitmap = menu.RenderPreview(dpi))
            bitmap.Save(Path.Combine(args[0], "recent-chats-" + mode + "-" + dpi.ToString(System.Globalization.CultureInfo.InvariantCulture) + "-" + font + ".png"), ImageFormat.Png);
        var now = DateTimeOffset.Now;
        var quota = new QuotaSnapshot { CapturedAt = now, PlanType = "plus" };
        quota.Windows.Add(new QuotaWindow { Kind = QuotaWindowKind.FiveHour, UsedPercent = 23, WindowMinutes = 300, ResetsAt = now.AddHours(3.6), Key = "codex:300" });
        quota.Windows.Add(new QuotaWindow { Kind = QuotaWindowKind.Weekly, UsedPercent = 51, WindowMinutes = 10080, ResetsAt = now.AddDays(4.5), Key = "codex:10080" });
        var result = new QuotaReadResult { Snapshot = quota, IsCodexRunning = true, IsLiveQuota = true, ActivityIsRequestBased = true, ActivityStatus = "complete" };
        if (args.Length == 2 && args[1] == "--countdown")
        {
            quota.Windows[0].ResetsAt = now.AddHours(3).AddMinutes(26);
            quota.Windows[1].UsedPercent = 37;
            foreach (var mode in new[] { "dark", "light" })
            foreach (var five in new[] { true, false })
            foreach (var status in new[] { true, false })
            foreach (var font in new[] { 80, 100, 120 })
            foreach (var dpi in new[] { 1f, 1.5f })
            using (var widget = new TaskbarWidgetForm())
            using (var bitmap = widget.RenderPreview(result, new AppSettings { AppearanceMode = mode, Language = "zh",
                ShowFiveHourQuota = five, TaskStatusEnabled = status, FontScalePercent = font }, dpi))
                bitmap.Save(Path.Combine(args[0], "countdown-" + mode + "-" + five + "-" + status + "-" + font + "-" +
                    dpi.ToString(System.Globalization.CultureInfo.InvariantCulture) + ".png"), ImageFormat.Png);
            return 0;
        }
        foreach (var mode in new[] { "dark", "light" })
        foreach (var dpi in new[] { 1f, 1.5f })
        foreach (var font in new[] { 100, 120 })
        using (var hover = new QuotaHoverCard())
        using (var bitmap = hover.RenderPreview("账号额度剩余 · 每周 63%\n长周期 91.9H\nCodex · Working 0 · Ask 0 · Err 0 · Finish 0\n点击 Codex 状态区域查看最近 6 个会话；点击余额查看额度",
            new AppSettings { AppearanceMode = mode, ThemeId = "mono", FontScalePercent = font, GlassEnabled = false }, dpi))
            bitmap.Save(Path.Combine(args[0], "quota-hover-" + mode + "-" + dpi.ToString(System.Globalization.CultureInfo.InvariantCulture) + "-" + font + ".png"), ImageFormat.Png);
        foreach (var state in new[] { CodexTaskState.Finish, CodexTaskState.Ask, CodexTaskState.Err, CodexTaskState.Working })
        foreach (var mode in new[] { "dark", "light" })
        foreach (var five in new[] { true, false })
        foreach (var phase in new[] { 0d, 1d })
            using (var widget = new TaskbarWidgetForm())
            using (var bitmap = widget.RenderStatusPreview(result, new AppSettings { Language = "zh", AppearanceMode = mode, ShowFiveHourQuota = five },
                new TaskStatusSnapshot { State = state, WorkingCount = state == CodexTaskState.Working ? 1 : 0 }, phase))
                bitmap.Save(Path.Combine(args[0], "status-" + state + "-" + mode + "-" + five + "-" + phase + ".png"), ImageFormat.Png);
        for (var i = 0; i < 24; i++) result.Activity.Add(new TokenActivitySample { CapturedAt = now.AddHours(-i), Tokens = 8000 + (i % 5) * 16000 });
        result.ActivityTimeline = result.Activity;
        var session = new SessionUsageSnapshot { Total = 164000, ThreadId = "demo-session", Status = "complete" };
        session.Models.Add(new SessionUsageRow { Name = "gpt-6-sol", Configuration = "max · Fast", Total = 126000, Input = 116000, Cached = 86000, Output = 10000, Reasoning = 4000, Main = 98000, Children = 28000 });
        session.Models.Add(new SessionUsageRow { Name = "gpt-6-luna", Configuration = "high · 普通", Total = 38000, Input = 34000, Cached = 24000, Output = 4000, Reasoning = 2000, Children = 38000 });
        session.Threads.Add(new SessionUsageRow { Name = "主会话", Configuration = "complete", Total = 98000 });
        session.Threads.Add(new SessionUsageRow { Name = "/root/review", Configuration = "complete", Total = 66000 });
        if (args.Length == 2) return ShowInteractive(result, session);
        using (var form = new QuotaPopupForm())
        {
            foreach (var color in new[] { "mono", "glacier", "rose", "champagne", "custom" })
            foreach (var mode in new[] { "dark", "light" })
            foreach (var page in new[] { 0, 1, 2 })
            {
                form.ApplySettings(new AppSettings { ThemeId = color, AppearanceMode = mode, Language = "zh", GlassEnabled = false,
                    CustomPrimary = Color.FromArgb(160, 90, 180), CustomSecondary = Color.FromArgb(65, 156, 167), FontScalePercent = 120 });
                form.ApplyResult(result); form.ShowPreviewPage(page);
                using (var bitmap = new Bitmap(form.Width, form.Height))
                { form.DrawToBitmap(bitmap, new Rectangle(Point.Empty, form.Size)); bitmap.Save(Path.Combine(args[0], "theme-" + color + "-" + mode + "-" + page + ".png"), ImageFormat.Png); }
            }
            foreach (var language in new[] { "zh", "en" })
            foreach (var theme in new[] { "light", "dark" })
            {
                form.ApplySettings(new AppSettings { AppearanceMode = theme, Language = language, GlassEnabled = false, ActivityRange = ActivityRange.Hours24 });
                form.ApplyResult(result);
                form.ApplySession(session);
                form.CreateControl();
                for (var page = 0; page < 4; page++)
                {
                    form.ShowPreviewPage(page);
                    using (var bitmap = new Bitmap(form.Width, form.Height))
                    {
                        form.DrawToBitmap(bitmap, new Rectangle(Point.Empty, form.Size));
                        bitmap.Save(Path.Combine(args[0], "combined-" + language + "-" + theme + "-" + page + ".png"), ImageFormat.Png);
                    }
                }
                form.ApplySettings(new AppSettings { AppearanceMode = theme, Language = language, GlassEnabled = false, ActivityRange = ActivityRange.Hours24, QuotaPrimaryEmphasis = false, FontScalePercent = 120 });
                form.ShowPreviewPage(0);
                using (var bitmap = new Bitmap(form.Width, form.Height))
                {
                    form.DrawToBitmap(bitmap, new Rectangle(Point.Empty, form.Size));
                    bitmap.Save(Path.Combine(args[0], "equal-large-" + language + "-" + theme + ".png"), ImageFormat.Png);
                }
                using (var widget = new TaskbarWidgetForm())
                using (var bitmap = widget.RenderPreview(result, new AppSettings { AppearanceMode = theme, Language = language, WidgetScalePercent = 100, FontScalePercent = 120 }))
                    bitmap.Save(Path.Combine(args[0], "widget-" + language + "-" + theme + ".png"), ImageFormat.Png);
                var hiddenSettings = new AppSettings { AppearanceMode = theme, Language = language, GlassEnabled = false,
                    ActivityRange = ActivityRange.Hours5, ShowFiveHourQuota = false, FontScalePercent = 120 };
                form.ApplySettings(hiddenSettings);
                for (var page = 0; page < 3; page++)
                {
                    form.ShowPreviewPage(page);
                    using (var bitmap = new Bitmap(form.Width, form.Height))
                    {
                        form.DrawToBitmap(bitmap, new Rectangle(Point.Empty, form.Size));
                        bitmap.Save(Path.Combine(args[0], "hidden-" + language + "-" + theme + "-" + page + ".png"), ImageFormat.Png);
                    }
                }
                using (var widget = new TaskbarWidgetForm())
                using (var bitmap = widget.RenderPreview(result, hiddenSettings))
                    bitmap.Save(Path.Combine(args[0], "widget-hidden-" + language + "-" + theme + ".png"), ImageFormat.Png);
            }
            quota.Windows[0].UsedPercent = 100;
            quota.Windows[1].UsedPercent = 0;
            form.ApplySettings(new AppSettings { AppearanceMode = "dark", Language = "zh", GlassEnabled = false, ActivityRange = ActivityRange.Hours24, FontScalePercent = 120 });
            form.ApplyResult(result);
            form.ShowPreviewPage(0);
            using (var bitmap = new Bitmap(form.Width, form.Height))
            {
                form.DrawToBitmap(bitmap, new Rectangle(Point.Empty, form.Size));
                bitmap.Save(Path.Combine(args[0], "quota-edge-values.png"), ImageFormat.Png);
            }
            using (var widget = new TaskbarWidgetForm())
            using (var bitmap = widget.RenderPreview(result, new AppSettings { AppearanceMode = "dark", Language = "zh", FontScalePercent = 120 }))
                bitmap.Save(Path.Combine(args[0], "widget-edge-values.png"), ImageFormat.Png);
            quota.ReserveWindow = new QuotaWindow { Name = "gpt-reserve · 备用额度", Key = "gpt-reserve:10080", Kind = QuotaWindowKind.Weekly, WindowMinutes = 10080, UsedPercent = 70 };
            quota.OrdinaryUsageAllowed = false;
            form.ApplyResult(result);
            using (var bitmap = new Bitmap(form.Width, form.Height))
            {
                form.DrawToBitmap(bitmap, new Rectangle(Point.Empty, form.Size));
                bitmap.Save(Path.Combine(args[0], "quota-reserve.png"), ImageFormat.Png);
            }
            using (var widget = new TaskbarWidgetForm())
            using (var bitmap = widget.RenderPreview(result, new AppSettings { AppearanceMode = "dark", Language = "zh" }))
                bitmap.Save(Path.Combine(args[0], "widget-reserve.png"), ImageFormat.Png);
            quota.ReserveWindow = null;
            quota.OrdinaryUsageAllowed = true;
            quota.Windows.RemoveAt(1);
            using (var widget = new TaskbarWidgetForm())
            using (var bitmap = widget.RenderPreview(result, new AppSettings { AppearanceMode = "dark", Language = "zh" }))
                bitmap.Save(Path.Combine(args[0], "widget-single.png"), ImageFormat.Png);
            result.Snapshot = null;
            result.IsCodexRunning = false;
            result.ActivityStatus = "partial";
            form.ApplyResult(result);
            form.ShowPreviewPage(0);
            using (var bitmap = new Bitmap(form.Width, form.Height))
            {
                form.DrawToBitmap(bitmap, new Rectangle(Point.Empty, form.Size));
                bitmap.Save(Path.Combine(args[0], "quota-unavailable.png"), ImageFormat.Png);
            }
            using (var widget = new TaskbarWidgetForm())
            using (var bitmap = widget.RenderPreview(result, new AppSettings { AppearanceMode = "dark", Language = "zh" }))
                bitmap.Save(Path.Combine(args[0], "widget-unavailable.png"), ImageFormat.Png);
        }
        return 0;
    }

    private static int ShowInteractive(QuotaReadResult result, SessionUsageSnapshot session)
    {
        using (var host = new Form { Text = "Quota redesign fixture", ClientSize = new Size(560, 650), StartPosition = FormStartPosition.CenterScreen, TopMost = true })
        using (var form = new QuotaPopupForm())
        {
            // Expose an independent preview window so UI tools can target it without activating its backdrop.
            form.ShowInTaskbar = true;
            form.KeepPreviewOpen = true;
            var reopen = new Button { Text = "查看额度", Location = new Point(450, 575), Size = new Size(100, 34) };
            host.Controls.Add(reopen);
            reopen.Click += delegate { form.Show(); form.Activate(); };
            host.Paint += delegate(object sender, PaintEventArgs e)
            {
                using (var blue = new SolidBrush(Color.FromArgb(42, 102, 145))) e.Graphics.FillRectangle(blue, 0, 0, 560, 220);
                using (var green = new SolidBrush(Color.FromArgb(46, 121, 105))) e.Graphics.FillRectangle(green, 0, 220, 560, 220);
                using (var orange = new SolidBrush(Color.FromArgb(150, 90, 49))) e.Graphics.FillRectangle(orange, 0, 440, 560, 210);
                using (var font = new Font("Segoe UI", 44, FontStyle.Bold))
                    e.Graphics.DrawString("BACKGROUND", font, Brushes.White, 0, 120);
            };
            form.ApplySettings(new AppSettings { AppearanceMode = "dark", Language = "zh", GlassEnabled = true, GlassOpacityPercent = 5, ActivityRange = ActivityRange.Hours24 });
            form.ApplyResult(result);
            form.ApplySession(session);
            form.RefreshRequested += delegate { host.Text = "Quota redesign fixture · refreshed"; };
            form.ExitRequested += delegate { host.Close(); };
            using (var startup = new Timer { Interval = 350 })
            {
                startup.Tick += delegate
                {
                    startup.Stop();
                    // Shell launchers can suppress the first ShowWindow call; this fixture is intentionally visible.
                    host.Hide(); host.Show(); host.Activate();
                    form.Location = new Point(host.Left + 32, host.Top + 35); form.Show(); form.Activate();
                };
                startup.Start();
                Application.Run(host);
            }
        }
        return 0;
    }
}
