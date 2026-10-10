using System;
using System.Collections;
using System.Collections.Generic;
using System.Diagnostics;
using System.Drawing;
using System.Drawing.Imaging;
using System.Globalization;
using System.IO;
using System.Linq;
using System.Reflection;
using System.Text;
using System.Windows.Forms;
using System.Web.Script.Serialization;
using CodexQuotaTray;

internal static class IntegrationTests
{
    private static int _count;
    [System.Runtime.InteropServices.DllImport("user32.dll")] private static extern IntPtr GetForegroundWindow();
    private static void Assert(bool condition, string name) { _count++; if (!condition) throw new Exception(name); }
    [STAThread]
    private static int Main(string[] args)
    {
        if (args.Length > 0 && args[0] == "app-server") return FakeQuotaServer(args);
        Application.SetUnhandledExceptionMode(UnhandledExceptionMode.ThrowException);
        try { return Run(args); }
        catch (Exception error)
        {
            Console.Error.WriteLine(error.GetType().Name + ": " + error.Message + "\n" + error.StackTrace);
            return 1;
        }
    }

    private static int Run(string[] args)
    {
        var state = CompanionBridge.Parse("{\"run_id\":\"test\",\"attachment_id\":\"stub\",\"heartbeat\":100,\"thread_id\":\"demo-main\"}");
        const string fixture = "{\"run_id\":\"test\",\"attachment_id\":\"stub\",\"captured_at\":100,\"usage\":{\"thread_id\":\"demo-main\",\"status\":\"complete\",\"totals\":{\"total_tokens\":150},\"models\":[{\"model\":\"gpt-future\",\"configurations\":[{\"reasoning_effort\":\"ultra\",\"fast_mode\":true,\"totals\":{\"total_tokens\":150,\"input_tokens\":120,\"cached_input_tokens\":80,\"output_tokens\":30,\"reasoning_output_tokens\":20},\"main\":{\"total_tokens\":100},\"subagents\":{\"total_tokens\":50}}]}],\"threads\":[{\"role\":\"main\",\"totals\":{\"total_tokens\":100}}]}}";
        var envelope = CompanionBridge.Parse(fixture);
        var snapshot = CompanionBridge.ParseSession(state, envelope, "test", 101);
        Assert(snapshot.Total == 150 && snapshot.Models.Count == 1, "model totals and configuration bridge");
        Assert(snapshot.Models[0].Configuration == "ultra · Fast", "future configuration metadata");
        Assert(snapshot.Models[0].Main == 100 && snapshot.Models[0].Children == 50, "agent attribution");
        Assert(snapshot.Models[0].Cached == 80 && snapshot.Models[0].Reasoning == 20, "nested details without double counting");
        Assert(snapshot.Threads.Count == 1 && snapshot.Threads[0].Total == 100, "agent list");
        Assert(!CompanionBridge.ParseSession(state, envelope, "other", 101).Total.HasValue, "run isolation");
        Assert(!CompanionBridge.ParseSession(state, envelope, "test", 120).Total.HasValue, "stale usage hidden");
        Assert(!CompanionBridge.ParseSession(state, envelope, "test", 97).Total.HasValue, "future usage hidden");
        state["thread_id"] = "changed";
        Assert(!CompanionBridge.ParseSession(state, envelope, "test", 101).Total.HasValue, "switch clears previous totals");
        state["thread_id"] = "demo-main";
        state["attachment_id"] = "different";
        Assert(!CompanionBridge.ParseSession(state, envelope, "test", 101).Total.HasValue, "attachment isolation");
        Assert(CompanionBridge.Parse("bad") == null, "malformed IPC");
        var update = "{\"tag_name\":\"v0.2.4\",\"html_url\":\"https://github.com/CyclicRedundancyCHK/session-model-usage/releases/tag/v0.2.4\",\"assets\":[{\"name\":\"session-model-usage-v0.2.4-windows-x64.zip\",\"browser_download_url\":\"https://github.com/CyclicRedundancyCHK/session-model-usage/releases/download/v0.2.4/session-model-usage-v0.2.4-windows-x64.zip\"}]}";
        Assert(CombinedUpdateService.Parse(update).Newer, "complete combined update accepted");
        Assert(!CombinedUpdateService.Parse(update.Replace("v0.2.4", "v0.2.3")).Newer, "current stable version is up to date");
        Assert(!CombinedUpdateService.Parse(update.Replace("CyclicRedundancyCHK/session-model-usage", "SYD-Official/CodexQuotaTray")).Newer, "upstream quota-only update rejected");
        Assert(!CombinedUpdateService.Parse(update.Replace(".zip", ".exe")).Newer, "quota-only executable rejected");
        Assert(!CombinedUpdateService.Parse(update.Replace("\"tag_name\"", "\"draft\":true,\"tag_name\"")).Newer, "draft rejected");
        Assert(AppSettings.Load() != null, "shared settings load");
        Assert(CompanionBridge.Tokens(CompanionBridge.Parse("{\"value\":9223372036854775807}"), "value") == long.MaxValue, "exact integer token counts");
        Assert(CompanionBridge.Tokens(CompanionBridge.Parse("{\"value\":-1}"), "value") == 0, "invalid negative count rejected");
        VerifyFiveHourActivity();
        VerifyQuotaVisibilitySetting();
        VerifyWeeklyActivityConsumption();
        VerifyTaskStates();
        VerifyTaskNavigation();
        VerifyRecentNavigation();
        VerifyFinishEviction();
        VerifyPopupDismissalAndHover();
        VerifyStatusSettingsAndPalette();
        VerifyResetCountdown();
        VerifyGlobalTaskPriority();
        VerifyContextMenuTheme();
        VerifyQuotaConnectionIsolation();
        Console.WriteLine("Combined bridge: {0} assertions passed", _count);
        if (args.Length > 0 && args[0] == "--live-task")
        {
            var runtime = CompanionBridge.Read(CompanionPaths.FilePath("runtime.json"));
            var started = new DateTimeOffset(1970, 1, 1, 0, 0, 0, TimeSpan.Zero).AddSeconds(CompanionBridge.Number(CompanionBridge.Get(runtime, "app_created")));
            var monitor = new CodexTaskMonitor();
            var observed = monitor.Poll(CompanionBridge.DesktopRunning() == true, (int)CompanionBridge.Number(CompanionBridge.Get(runtime, "app_pid")), started);
            if (args.Length > 1)
            {
                var tasks = (IDictionary)typeof(CodexTaskMonitor).GetField("_tasks", BindingFlags.NonPublic | BindingFlags.Instance).GetValue(monitor);
                var rows = new List<object>();
                foreach (DictionaryEntry entry in tasks)
                {
                    var type = entry.Value.GetType();
                    rows.Add(new { id = entry.Key, state = type.GetField("State").GetValue(entry.Value).ToString(),
                        updated = ((DateTimeOffset)type.GetField("Updated").GetValue(entry.Value)).ToString("o") });
                }
                File.WriteAllText(args[1], new JavaScriptSerializer().Serialize(rows), Encoding.UTF8);
            }
            Console.WriteLine(new JavaScriptSerializer().Serialize(new { task_status = observed.Label, working = observed.WorkingCount,
                ask = observed.AskCount, error = observed.ErrorCount, finish = observed.FinishCount, source_available = observed.SourceAvailable }));
            return observed.SourceAvailable ? 0 : 2;
        }
        if (args.Length >= 1 && args[0] == "--live-quota")
        {
            var result = new SessionQuotaReader().ReadLatest();
            CompanionBridge.ApplyActivity(result);
            using (var form = new QuotaPopupForm())
            {
                form.ApplySettings(new AppSettings { AppearanceMode = "dark", Language = "zh", GlassEnabled = false,
                    ActivityRange = args.Contains("--hours24") ? ActivityRange.Hours24 : ActivityRange.Hours5, ShowFiveHourQuota = !args.Contains("--hide-five-hour") });
                form.ApplyResult(result);
                var buckets = ActivityBuckets(form, DateTimeOffset.Now);
                var data = new { running = result.IsCodexRunning, live = result.IsLiveQuota,
                    five_hour_remaining = result.Snapshot == null || result.Snapshot.FiveHourWindow == null ? (int?)null : result.Snapshot.FiveHourWindow.RemainingPercent,
                    weekly_remaining = result.Snapshot == null || result.Snapshot.WeeklyWindow == null ? (int?)null : result.Snapshot.WeeklyWindow.RemainingPercent,
                    plan = result.Snapshot == null ? null : result.Snapshot.PlanType, error = result.Error,
                    five_hour_title = CardTitle(form, result.Snapshot == null ? null : result.Snapshot.FiveHourWindow, false),
                    weekly_title = CardTitle(form, result.Snapshot == null ? null : result.Snapshot.DisplayWeeklyWindow, true),
                    activity_status = result.ActivityStatus, activity_tokens = BucketTokens(buckets), bucket_count = buckets.Count,
                    range = args.Contains("--hours24") ? "24h" : "5h", quota_summary = QuotaSummary(form, DateTimeOffset.Now) };
                Console.WriteLine(new JavaScriptSerializer().Serialize(data));
                if (args.Length >= 2 && args[1] != "--hide-five-hour")
                {
                    form.CreateControl();
                    using (var bitmap = new Bitmap(form.Width, form.Height))
                    {
                        form.DrawToBitmap(bitmap, new Rectangle(Point.Empty, form.Size));
                        bitmap.Save(args[1], ImageFormat.Png);
                    }
                }
            }
            return result.IsLiveQuota ? 0 : 2;
        }
        return 0;
    }

    private static int FakeQuotaServer(string[] args)
    {
        var transcript = Environment.GetEnvironmentVariable("SESSION_USAGE_TEST_QUOTA_TRANSCRIPT");
        var messages = new List<string>();
        var serializer = new JavaScriptSerializer();
        try
        {
            string line;
            while ((line = Console.ReadLine()) != null)
            {
                messages.Add(line);
                var request = CompanionBridge.Parse(line);
                var method = CompanionBridge.Text(CompanionBridge.Get(request, "method"));
                var id = CompanionBridge.Get(request, "id");
                if (method == "initialize")
                {
                    // Server and client request IDs occupy separate namespaces.
                    Console.WriteLine(serializer.Serialize(new { id = id, method = "item/tool/requestUserInput", @params = new { } }));
                    Console.WriteLine(serializer.Serialize(new { id = id, result = new { } }));
                }
                else if (method == "account/rateLimits/read")
                {
                    Console.WriteLine(serializer.Serialize(new { id = id, method = "item/tool/requestUserInput", @params = new { } }));
                    Console.Out.Flush();
                    // Account setup in an isolated server can exceed the old 4s limit.
                    System.Threading.Thread.Sleep(4200);
                    Console.WriteLine(serializer.Serialize(new { id = id, result = new { rateLimits = new {
                        limitId = "codex", primary = new { usedPercent = 12, windowDurationMins = 300 },
                        secondary = new { usedPercent = 34, windowDurationMins = 10080 } } } }));
                }
                Console.Out.Flush();
            }
            return 0;
        }
        finally
        {
            File.WriteAllText(transcript, serializer.Serialize(new { arguments = args, messages = messages }), Encoding.UTF8);
        }
    }

    private static void VerifyQuotaConnectionIsolation()
    {
        Assert(SessionQuotaReader.IsAppServerResponse("{\"id\":2,\"result\":null}", 2), "null RPC results remain valid responses");
        Assert(SessionQuotaReader.IsAppServerResponse("{\"id\":2,\"error\":{\"code\":-32602}}", 2), "quota parameter errors remain available for the read-only fallback");
        Assert(!SessionQuotaReader.IsAppServerResponse("{\"id\":2,\"method\":\"item/tool/requestUserInput\",\"result\":{}}", 2), "request envelopes cannot masquerade as quota replies");
        Assert(!SessionQuotaReader.IsAppServerResponse("{\"id\":2}", 2) &&
            !SessionQuotaReader.IsAppServerResponse("{\"id\":2,\"result\":{},\"error\":{}}", 2), "incomplete or conflicting response envelopes are ignored");
        Assert(!SessionQuotaReader.IsAppServerResponse("{\"id\":3,\"result\":{}}", 2) &&
            !SessionQuotaReader.IsAppServerResponse("not json", 2), "unrelated and malformed replies cannot finish the pending quota query");
        var directory = Path.Combine(Path.GetTempPath(), "quota-protocol-" + Guid.NewGuid().ToString("N"));
        Directory.CreateDirectory(directory);
        var transcript = Path.Combine(directory, "transcript.json");
        var previousCli = Environment.GetEnvironmentVariable("SESSION_USAGE_QUOTA_CLI");
        var previousTranscript = Environment.GetEnvironmentVariable("SESSION_USAGE_TEST_QUOTA_TRANSCRIPT");
        var previousInputEncoding = Console.InputEncoding;
        try
        {
            // UTF-8 consoles on CI must not inject a BOM into JSON-RPC stdin.
            Console.InputEncoding = new UTF8Encoding(true);
            Environment.SetEnvironmentVariable("SESSION_USAGE_QUOTA_CLI", Process.GetCurrentProcess().MainModule.FileName);
            Environment.SetEnvironmentVariable("SESSION_USAGE_TEST_QUOTA_TRANSCRIPT", transcript);
            var result = new SessionQuotaReader(directory, () => true).ReadLatest();
            if (!result.IsLiveQuota)
                Console.Error.WriteLine("Quota fixture: {0}; transcript={1}", result.Error,
                    File.Exists(transcript) ? File.ReadAllText(transcript) : "missing");
            Assert(result.IsLiveQuota && result.Snapshot.FiveHourWindow.RemainingPercent == 88 &&
                result.Snapshot.WeeklyWindow.RemainingPercent == 66,
                "colliding server question IDs must not finish the quota read or suppress the real response");
            Assert(Console.InputEncoding.CodePage == 65001 && Console.InputEncoding.GetPreamble().Length == 3,
                "quota process launch restores the caller's console encoding");
            var data = CompanionBridge.Read(transcript);
            var arguments = ((object[])data["arguments"]).Select(item => item.ToString()).ToArray();
            var disabled = Array.IndexOf(arguments, "--disable");
            Assert(disabled >= 0 && disabled + 1 < arguments.Length && arguments[disabled + 1] == "daemon_auto_start",
                "quota subprocess explicitly disables shared daemon auto-start before connecting");
            var messages = ((object[])data["messages"]).Select(item => CompanionBridge.Parse(item.ToString())).ToArray();
            Assert(messages.Select(item => CompanionBridge.Text(CompanionBridge.Get(item, "method")))
                .SequenceEqual(new[] { "initialize", "initialized", "account/rateLimits/read" }),
                "quota client only initializes and reads limits; it never answers or cancels incoming questions");
        }
        finally
        {
            Environment.SetEnvironmentVariable("SESSION_USAGE_QUOTA_CLI", previousCli);
            Environment.SetEnvironmentVariable("SESSION_USAGE_TEST_QUOTA_TRANSCRIPT", previousTranscript);
            Console.InputEncoding = previousInputEncoding;
            var fullDirectory = Path.GetFullPath(directory);
            var temporaryRoot = Path.GetFullPath(Path.GetTempPath()).TrimEnd(Path.DirectorySeparatorChar) + Path.DirectorySeparatorChar;
            if (!fullDirectory.StartsWith(temporaryRoot, StringComparison.OrdinalIgnoreCase))
                throw new InvalidOperationException("Quota fixture cleanup escaped its temporary directory");
            Directory.Delete(fullDirectory, true);
        }
    }

    private static string EventLine(DateTimeOffset stamp, string type, string extra)
    { return "{\"timestamp\":\"" + stamp.ToString("o") + "\",\"type\":\"event_msg\",\"payload\":{\"type\":\"" + type + "\"" + extra + "}}"; }

    private static void VerifyTaskNavigation()
    {
        const string first = "00000000-0000-0000-0000-000000000001";
        const string second = "00000000-0000-0000-0000-000000000002";
        var now = DateTimeOffset.UtcNow;
        var monitor = new CodexTaskMonitor(null, null);
        monitor.ProcessSessionLine(first, EventLine(now, "task_started", ",\"turn_id\":\"one\""));
        var targets = CodexThreadNavigator.Targets(monitor.Snapshot());
        Assert(targets.Length == 1 && targets[0].ThreadId == first, "Working selects its exact conversation");
        string launched = null;
        Assert(CodexThreadNavigator.Open(targets[0], uri => { launched = uri; return true; }) &&
            launched == "codex://threads/" + first + "?hostId=local", "navigation dispatches the verified local Codex deeplink");
        monitor.Acknowledge(targets[0]);
        Assert(monitor.Snapshot().WorkingCount == 1, "navigation cannot consume Working");
        monitor.ProcessSessionLine(second, EventLine(now.AddSeconds(1), "task_started", ",\"turn_id\":\"two\""));
        targets = CodexThreadNavigator.Targets(monitor.Snapshot());
        Assert(targets.Length == 2 && targets[0].ThreadId == second && targets[1].ThreadId == first, "multiple matching conversations remain individually selectable");
        monitor.ProcessDesktopLine(now.AddSeconds(2).ToString("o") + " info [desktop-notifications] received question conversationId=" + first + " requestId=question");
        targets = CodexThreadNavigator.Targets(monitor.Snapshot());
        Assert(targets.Length == 1 && targets[0].ThreadId == first && targets[0].State == CodexTaskState.Ask, "Ask routes to the question instead of other running work");
        monitor.Acknowledge(targets[0]);
        Assert(monitor.Snapshot().AskCount == 1, "opening Ask never answers the question");
        monitor.ProcessSessionLine(first, EventLine(now.AddSeconds(3), "task_complete", ",\"turn_id\":\"one\""));
        monitor.ProcessSessionLine(second, EventLine(now.AddSeconds(4), "task_complete", ",\"turn_id\":\"two\""));
        targets = CodexThreadNavigator.Targets(monitor.Snapshot());
        Assert(targets.Length == 2 && targets.All(t => t.State == CodexTaskState.Finish), "Finish retains both target identities");
        Assert(!CodexThreadNavigator.Open(targets[0], uri => false) && monitor.Snapshot().FinishCount == 2, "failed dispatch leaves completion reminders intact");
        monitor.Acknowledge(targets[0]);
        Assert(monitor.Snapshot().FinishCount == 1 && CodexThreadNavigator.Targets(monitor.Snapshot())[0].ThreadId == first, "opening one Finish preserves the other completion");
        var old = CodexThreadNavigator.Targets(monitor.Snapshot())[0];
        monitor.ProcessSessionLine(first, EventLine(now.AddSeconds(5), "task_started", ",\"turn_id\":\"new\""));
        monitor.ProcessSessionLine(first, EventLine(now.AddSeconds(6), "task_complete", ",\"turn_id\":\"new\""));
        monitor.Acknowledge(old);
        Assert(monitor.Snapshot().FinishCount == 1, "a stale menu cannot clear a newer completion");
        monitor.ProcessSessionLine(second, EventLine(now.AddSeconds(7), "task_started", ",\"turn_id\":\"failed\""));
        monitor.ProcessSessionLine(second, EventLine(now.AddSeconds(8), "error", ",\"will_retry\":false"));
        Assert(CodexThreadNavigator.Targets(monitor.Snapshot()).Single().ThreadId == second, "Err follows the same exact target routing");
        Assert(CodexThreadNavigator.ThreadUri("../settings") == null && CodexThreadNavigator.ThreadUri(first + "?prompt=wrong") == null &&
            !CodexThreadNavigator.Open(new TaskNavigationTarget { ThreadId = "bad" }, uri => { throw new Exception("must not launch"); }), "invalid identities cannot dispatch another route or inject a prompt");
        Assert(!CodexThreadNavigator.Open(targets[0], uri => { throw new System.ComponentModel.Win32Exception(); }), "missing protocol handler reports dispatch failure");
        Assert(CodexThreadNavigator.Targets(new TaskStatusSnapshot()).Length == 0, "neutral status has no guessed destination");
        var titles = new Dictionary<string, object> { { first, "标题 & 第二行\n内容" } };
        Assert(CodexThreadNavigator.MenuLabel(old, titles) == "标题 & 第二行 内容" &&
            CodexThreadNavigator.MenuLabel(old, null) == "未命名会话", "menu names preserve literal text and never display identities");
        var titleFile = CompanionPaths.FilePath("task-titles.json");
        Directory.CreateDirectory(CompanionPaths.Root);
        try
        {
            File.WriteAllText(titleFile, new JavaScriptSerializer().Serialize(new { run_id = "navigation-test", captured_at = CompanionBridge.Now, titles = titles }));
            Assert(CodexThreadNavigator.ReadTitles("navigation-test") != null && CodexThreadNavigator.ReadTitles("other-run") == null,
                "title cache is isolated to the current frontend run");
            File.WriteAllText(titleFile, new JavaScriptSerializer().Serialize(new { run_id = "navigation-test", captured_at = CompanionBridge.Now - 60, titles = titles }));
            Assert(CodexThreadNavigator.ReadTitles("navigation-test") == null, "expired title cache is never reused");
        }
        finally { File.Delete(titleFile); }

        using (var widget = new TaskbarWidgetForm())
        {
            var settings = new AppSettings { ShowFiveHourQuota = false, TaskStatusEnabled = true, WidgetScalePercent = 130 };
            widget.ApplySettings(settings);
            typeof(TaskbarWidgetForm).GetField("_dpiScale", BindingFlags.Instance | BindingFlags.NonPublic).SetValue(widget, 1.5f);
            widget.ApplyTaskStatus(monitor.Snapshot());
            var statusClicks = 0; var quotaClicks = 0;
            widget.TaskStatusClicked += delegate { statusClicks++; };
            widget.QuotaClicked += delegate { quotaClicks++; };
            var click = typeof(TaskbarWidgetForm).GetMethod("OnMouseClick", BindingFlags.Instance | BindingFlags.NonPublic);
            var bounds = widget.TaskStatusBounds;
            click.Invoke(widget, new object[] { new MouseEventArgs(MouseButtons.Left, 1, bounds.Left + bounds.Width / 2, bounds.Top + bounds.Height / 2, 0) });
            Assert(statusClicks == 1 && quotaClicks == 0, "scaled status click dispatches only navigation");
            click.Invoke(widget, new object[] { new MouseEventArgs(MouseButtons.Left, 1, 10, 10, 0) });
            Assert(statusClicks == 1 && quotaClicks == 1, "balance click still opens quota");
            click.Invoke(widget, new object[] { new MouseEventArgs(MouseButtons.Right, 1, bounds.Left + 2, bounds.Top + 2, 0) });
            Assert(statusClicks == 1 && quotaClicks == 1, "right click does not navigate or acknowledge");
            settings.TaskStatusEnabled = false; widget.ApplySettings(settings);
            click.Invoke(widget, new object[] { new MouseEventArgs(MouseButtons.Left, 1, 50, 10, 0) });
            widget.ApplyTaskStatus(new TaskStatusSnapshot()); settings.TaskStatusEnabled = true; widget.ApplySettings(settings);
            bounds = widget.TaskStatusBounds;
            click.Invoke(widget, new object[] { new MouseEventArgs(MouseButtons.Left, 1, bounds.Left + 2, bounds.Top + 2, 0) });
            Assert(statusClicks == 2 && quotaClicks == 2, "neutral Codex opens chats while disabled status preserves quota access");
        }
    }

    private static void VerifyFinishEviction()
    {
        var now = DateTimeOffset.UtcNow.AddSeconds(-1);
        const string first = "00000000-0000-0000-0000-000000000001";
        const string seventh = "00000000-0000-0000-0000-000000000007";
        const string error = "00000000-0000-0000-0000-000000000008";
        const string ask = "00000000-0000-0000-0000-000000000009";
        const string running = "00000000-0000-0000-0000-000000000010";
        var monitor = new CodexTaskMonitor(null, null);
        Func<string, string, int, string> line = (id, value, ms) => now.AddMilliseconds(ms).ToString("O") +
            " info [desktop-notifications] " + value + " conversationId=" + id + " turnId=turn-1";
        monitor.ProcessDesktopLine(line(first, "received turn-complete", 0));
        monitor.ProcessDesktopLine(line(seventh, "received turn-complete", 0));
        monitor.ProcessDesktopLine(line(ask, "received question requestId=question-1", 0));
        monitor.ProcessDesktopLine(line(running, "Received turn/started", 0));
        monitor.ProcessSessionLine(error, new JavaScriptSerializer().Serialize(new {
            timestamp = now.ToString("O"), type = "event_msg", payload = new { type = "task_complete", status = "failed", turn_id = "turn-1" }
        }));
        var recent = new RecentThreadSnapshot { Available = true, CapturedAt = now.AddMilliseconds(100) };
        for (var i = 1; i <= 6; i++) recent.Targets.Add(new TaskNavigationTarget {
            ThreadId = "00000000-0000-0000-0000-" + i.ToString("D12") });
        monitor.ReconcileRecent(recent);
        var result = monitor.Snapshot();
        Assert(result.FinishCount == 1 && result.Targets.Any(t => t.ThreadId == first) &&
            !result.Targets.Any(t => t.ThreadId == seventh), "Finish evicted from newest six no longer sticks in the status strip");
        Assert(result.ErrorCount == 1 && result.AskCount == 1 && result.WorkingCount == 1,
            "recent-list eviction preserves errors, pending questions and running work outside the list");
        monitor.ProcessDesktopLine(line(seventh, "received turn-complete", 0));
        monitor.ReconcileRecent(recent);
        Assert(monitor.Snapshot().FinishCount == 1, "duplicate completion does not restore an evicted Finish");
        monitor.ProcessDesktopLine(line(seventh, "Received turn/started", 200).Replace("turn-1", "turn-2"));
        monitor.ProcessDesktopLine(line(seventh, "received turn-complete", 300).Replace("turn-1", "turn-2"));
        monitor.ReconcileRecent(recent);
        Assert(monitor.Snapshot().FinishCount == 2, "older recent cache cannot clear a newer completion");
        recent.Available = false; recent.CapturedAt = now.AddMilliseconds(400);
        monitor.ReconcileRecent(recent);
        Assert(monitor.Snapshot().FinishCount == 2, "unavailable recent list does not clear reminders");
        recent.Available = true; recent.CapturedAt = DateTimeOffset.UtcNow.AddMinutes(-1);
        monitor.ReconcileRecent(recent);
        Assert(monitor.Snapshot().FinishCount == 2, "stale recent list does not clear reminders");
        recent.CapturedAt = DateTimeOffset.UtcNow.AddMinutes(1);
        monitor.ReconcileRecent(recent);
        Assert(monitor.Snapshot().FinishCount == 2, "future recent list does not clear reminders");
        recent.CapturedAt = now.AddMilliseconds(400);
        monitor.ReconcileRecent(recent);
        Assert(monitor.Snapshot().FinishCount == 1, "a fresh confirmed list consumes only the newly evicted completion");
        recent.Targets.Clear();
        monitor.ReconcileRecent(recent);
        Assert(monitor.Snapshot().FinishCount == 0 && monitor.Snapshot().ErrorCount == 1,
            "confirmed empty list clears remaining Finish while retaining errors");
    }

    private static void VerifyRecentNavigation()
    {
        var recentFile = CompanionPaths.FilePath("recent-threads.json");
        var rows = new List<object>();
        rows.Add(new { thread_id = "../settings", title = "invalid" });
        for (var i = 1; i <= 8; i++)
        {
            var id = "00000000-0000-0000-0000-" + i.ToString("D12");
            rows.Add(new { thread_id = id, title = i == 2 ? "" : "会话 & " + i + "\n第二行" });
            if (i == 1) rows.Add(new { thread_id = id, title = "duplicate" });
        }
        var serializer = new JavaScriptSerializer();
        Action<string, double, string, object> write = (run, age, status, chats) => File.WriteAllText(recentFile,
            serializer.Serialize(new { run_id = run, captured_at = CompanionBridge.Now - age, status = status, threads = chats }));
        try
        {
            write("recent-test", 0, "complete", rows);
            var recent = CodexThreadNavigator.ReadRecent("recent-test");
            Assert(recent.Available && recent.Targets.Count == 6 && recent.Targets[5].ThreadId.EndsWith("000006"),
                "recent menu keeps the newest six valid distinct exact IDs in index order");
            Assert(recent.Targets.All(t => t.State == CodexTaskState.None), "recent chats do not invent task status");
            var dispatched = new List<string>();
            using (var menu = CodexThreadNavigator.RecentMenu(recent, false, target =>
                CodexThreadNavigator.Open(target, uri => { dispatched.Add(uri); return true; })))
            {
                Assert(menu.Items.Count == 8 && menu.Items[0].Text == "最近会话", "recent popup includes a heading and six chat rows");
                Assert(menu.Items[2].Text == "会话 & 1 第二行" && menu.Items[2].ToolTipText == "会话 & 1 第二行",
                    "menu names and tooltips contain only literal display titles");
                Assert(menu.Items[3].Text == "未命名会话", "untitled recent chat uses a readable placeholder without its ID");
                Assert(menu.Items.Cast<ToolStripItem>().All(item => !(item.Text ?? "").Contains("00000000-") && !(item.ToolTipText ?? "").Contains("00000000-")),
                    "session IDs remain absent from every visible label and tooltip");
                foreach (var item in menu.Items.OfType<ToolStripMenuItem>().Where(item => item.Enabled)) item.PerformClick();
                Assert(dispatched.SequenceEqual(recent.Targets.Select(t => CodexThreadNavigator.ThreadUri(t.ThreadId))),
                    "all six rows dispatch their own local conversation instead of a captured last row");
            }
            Assert(!CodexThreadNavigator.ReadRecent("other-run").Available, "recent cache cannot cross frontend runs");
            var now = DateTimeOffset.UtcNow;
            var monitor = new CodexTaskMonitor(null, null);
            var first = recent.Targets[0].ThreadId;
            var second = recent.Targets[1].ThreadId;
            monitor.ProcessSessionLine(first, EventLine(now, "task_started", ",\"turn_id\":\"one\""));
            monitor.ProcessSessionLine(first, EventLine(now.AddSeconds(1), "task_complete", ",\"turn_id\":\"one\""));
            monitor.ProcessSessionLine(second, EventLine(now, "task_started", ",\"turn_id\":\"two\""));
            using (var menu = CodexThreadNavigator.RecentMenu(recent, true, target => monitor.Acknowledge(target), monitor.Snapshot()))
            {
                Assert(menu.Items.Count == 8 && ((TaskNavigationTarget)menu.Items[2].Tag).State == CodexTaskState.Finish &&
                    ((TaskNavigationTarget)menu.Items[3].Tag).State == CodexTaskState.Working && !menu.Items[2].Text.StartsWith("Finish"),
                    "per-chat states drive indicators and selection without polluting display names");
                Assert(monitor.Snapshot().FinishCount == 1, "opening or cancelling the recent menu does not acknowledge tasks");
                ((ToolStripMenuItem)menu.Items[2]).PerformClick();
                Assert(monitor.Snapshot().FinishCount == 0 && monitor.Snapshot().WorkingCount == 1,
                    "recent selection acknowledges only its matching completion and preserves running work");
            }
            write("recent-test", 60, "complete", rows);
            Assert(!CodexThreadNavigator.ReadRecent("recent-test").Available, "expired recent cache cannot show old chats");
            write("recent-test", -60, "complete", rows);
            Assert(!CodexThreadNavigator.ReadRecent("recent-test").Available, "future recent cache is rejected");
            write("recent-test", 0, "unavailable", rows);
            using (var menu = CodexThreadNavigator.RecentMenu(CodexThreadNavigator.ReadRecent("recent-test"), true, target => { throw new Exception(); }))
                Assert(menu.Items.Count == 3 && !menu.Items[2].Enabled && menu.Items[2].Text.Contains("unavailable"), "failed lookup shows a non-clickable unavailable row");
            write("recent-test", 0, "complete", new object[0]);
            using (var menu = CodexThreadNavigator.RecentMenu(CodexThreadNavigator.ReadRecent("recent-test"), false, target => { throw new Exception(); }))
                Assert(menu.Items.Count == 3 && menu.Items[2].Text == "暂无最近会话", "empty index is distinct from lookup failure");
            File.WriteAllText(recentFile, "bad");
            Assert(!CodexThreadNavigator.ReadRecent("recent-test").Available, "partial recent bridge is unavailable");
            write("recent-test", 0, "complete", new { thread_id = first });
            Assert(!CodexThreadNavigator.ReadRecent("recent-test").Available, "non-array recent data cannot clear Finish reminders");
            write("recent-test", 0, "complete", new[] { new { thread_id = "invalid", title = "invalid" } });
            Assert(!CodexThreadNavigator.ReadRecent("recent-test").Available, "invalid-only recent data is unavailable rather than empty");
            recent.Titles[first] = "添加最近会话弹出与打开";
            Assert(CodexThreadNavigator.MenuLabel(recent.Targets[0], recent.Titles) == "添加最近会话弹出与打开", "Codex display name stays unchanged within the limit");
            recent.Titles[first] = "一二三四五六七八九十一二";
            Assert(CodexThreadNavigator.MenuLabel(recent.Targets[0], recent.Titles) == "一二三四五六七八九十一二", "exactly twelve characters stay intact");
            recent.Titles[first] = "一二三四五六七八九十一二三四";
            Assert(CodexThreadNavigator.MenuLabel(recent.Targets[0], recent.Titles) == "一二三四五六七八九十一…", "ellipsis occupies the twelfth visible character");
            recent.Titles[first] = "一二三四五六七八九十😀二三";
            Assert(CodexThreadNavigator.MenuLabel(recent.Targets[0], recent.Titles) == "一二三四五六七八九十😀…", "truncation preserves complete supplementary Unicode characters");
            foreach (var mode in new[] { "dark", "light" })
            using (var menu = CodexThreadNavigator.RecentMenu(recent, false, target => { }, null,
                new AppSettings { AppearanceMode = mode, ThemeId = "glacier", FontScalePercent = 120, GlassEnabled = false }))
            using (var bitmap = menu.RenderPreview(1.5f))
            {
                var palette = ThemePalette.FromSettings(new AppSettings { AppearanceMode = mode, ThemeId = "glacier" });
                Assert(menu.BackColor == palette.Background && menu.ForeColor == palette.Text && menu.Width < 400,
                    "recent panel inherits palette and stays compact at high DPI and font scale in " + mode);
                Assert(menu.Items[7].Bounds.Bottom <= menu.Height - menu.Padding.Bottom && menu.Items[7].Bounds.Height >= 50,
                    "all six selectable rows fit at high DPI in " + mode);
                var pixel = bitmap.GetPixel(bitmap.Width / 2, 2);
                Assert(pixel.R == palette.Background.R && pixel.G == palette.Background.G && pixel.B == palette.Background.B,
                    "rendered panel uses the matching theme background in " + mode);
                var card = bitmap.GetPixel(bitmap.Width / 2, menu.Items[2].Bounds.Top + 6);
                Assert(card.R == palette.Surface.R && card.G == palette.Surface.G && card.B == palette.Surface.B,
                    "rendered menu includes its session cards in " + mode);
                menu.ApplyTheme();
                using (var repeat = menu.RenderPreview(1.5f))
                    Assert(repeat.Size == bitmap.Size, "unchanged theme can repaint without disposing the live menu font in " + mode);
            }
        }
        finally { File.Delete(recentFile); }
    }

    private static void VerifyPopupDismissalAndHover()
    {
        var closed = 0;
        using (var watcher = new PopupClickWatcher((point, root) => root == new IntPtr(7) ||
            (root == new IntPtr(8) && new Rectangle(10, 10, 80, 24).Contains(point)), () => closed++))
        {
            watcher.Sample(1, new Point(5, 5), new IntPtr(7));
            Assert(closed == 0, "a session row press stays open until its click action");
            watcher.Sample(0, Point.Empty, IntPtr.Zero);
            watcher.Sample(1, new Point(15, 15), new IntPtr(8));
            Assert(closed == 0, "original badge handles its own toggle without outside-close reopening");
            watcher.Sample(0, Point.Empty, IntPtr.Zero);
            watcher.Sample(1, new Point(15, 15), new IntPtr(9));
            Assert(closed == 1, "an overlapping app at badge coordinates is still an outside click");
            watcher.Sample(1, new Point(100, 100), IntPtr.Zero);
            Assert(closed == 1, "holding a mouse button does not repeatedly dismiss");
            watcher.Sample(0, Point.Empty, IntPtr.Zero);
            watcher.Sample(2, new Point(100, 100), IntPtr.Zero);
            Assert(closed == 2, "right-click outside also dismisses");
            watcher.Stop();
        }
        using (var menu = new ThemedContextMenu(new AppSettings()))
            Assert(!menu.AutoClose, "native modal close cannot consume the widget toggle");
        using (var anchor = new Form())
        using (var menu = new ThemedContextMenu(new AppSettings { GlassEnabled = false }))
        {
            anchor.Bounds = new Rectangle(-20000, -20000, 200, 40);
            var handle = anchor.Handle;
            menu.Items.Add(new ToolStripMenuItem("示例会话") { Tag = new TaskNavigationTarget() });
            menu.ApplyTheme();
            menu.WatchOutsideClicks(anchor, () => new Rectangle(20, 4, 84, 24));
            menu.Location = new Point(-20000, -20200);
            var foreground = GetForegroundWindow();
            menu.Visible = true;
            Assert(menu.Visible && GetForegroundWindow() == foreground, "recent popup opens without taking foreground focus");
            var dismissal = (PopupClickWatcher)typeof(ThemedContextMenu).GetField("_dismissal", BindingFlags.NonPublic | BindingFlags.Instance).GetValue(menu);
            dismissal.Sample(0, Point.Empty, IntPtr.Zero);
            dismissal.Sample(1, menu.Location, menu.Handle);
            Assert(menu.Visible, "native panel stays visible for a press inside its own window");
            dismissal.Sample(0, Point.Empty, IntPtr.Zero);
            dismissal.Sample(1, new Point(50, 50), IntPtr.Zero);
            Assert(!menu.Visible, "outside watcher actually hides the native popup");
            menu.Visible = true;
            typeof(ThemedContextMenu).GetMethod("OnItemClicked", BindingFlags.NonPublic | BindingFlags.Instance).Invoke(menu,
                new object[] { new ToolStripItemClickedEventArgs(menu.Items[0]) });
            Assert(!menu.Visible, "selecting a chat also collapses the non-modal recent popup");
        }
        const string description = "账号额度剩余 · 每周 63%\n长周期 91.9H\nCodex · Working 0 · Ask 0 · Err 0 · Finish 0\n点击 Codex 状态区域查看最近 6 个会话；点击余额查看额度";
        foreach (var mode in new[] { "dark", "light" })
        using (var hover = new QuotaHoverCard())
        {
            var settings = new AppSettings { AppearanceMode = mode, ThemeId = "glacier", GlassEnabled = false, FontScalePercent = 120 };
            using (var bitmap = hover.RenderPreview(description, settings, 1.5f))
            {
                Assert(bitmap.Width == 480 && bitmap.Height > 200 && bitmap.Height < 450, "hover content wraps into a compact scaled panel in " + mode);
                Assert(hover.AccessibleDescription == description && hover.AccessibleName == "账号额度剩余", "full hover description stays accessible in " + mode);
                var palette = ThemePalette.FromSettings(settings); var pixel = bitmap.GetPixel(bitmap.Width / 2, 4);
                Assert(pixel.ToArgb() == palette.Background.ToArgb(), "hover uses the shared theme rather than the system tooltip in " + mode);
                Assert(bitmap.GetPixel(0, 0).A == 0, "hover corners remain rounded in " + mode);
            }
        }
    }

    private static void VerifyTaskStates()
    {
        var now = DateTimeOffset.UtcNow;
        var monitor = new CodexTaskMonitor(null, null);
        monitor.ProcessSessionLine("first", EventLine(now, "task_started", ",\"turn_id\":\"one\""));
        Assert(monitor.Snapshot().State == CodexTaskState.Working, "real task start shows Working");
        monitor.ProcessDesktopLine(now.AddSeconds(1).ToString("o") + " info [desktop-notifications] received question conversationId=first requestId=question-one");
        Assert(monitor.Snapshot().State == CodexTaskState.Ask, "received question takes priority over running work");
        monitor.ProcessDesktopLine(now.AddSeconds(2).ToString("o") + " info thread_stream_view_activity_changed active=true conversationId=first resumeState=resumed");
        monitor.Acknowledge();
        Assert(monitor.Snapshot().State == CodexTaskState.Ask, "opening a chat and clicking the meter cannot consume Ask");
        monitor.ProcessSessionLine("first", "{\"timestamp\":\"" + now.AddSeconds(3).ToString("o") + "\",\"type\":\"response_item\",\"payload\":{\"type\":\"function_call_output\",\"call_id\":\"question-one\",\"output\":\"{\\\"accepted\\\":true}\"}}");
        Assert(monitor.Snapshot().State == CodexTaskState.Ask, "async tool acceptance does not mean a user answered");
        monitor.ProcessSessionLine("first", "{\"timestamp\":\"" + now.AddSeconds(4).ToString("o") + "\",\"type\":\"response_item\",\"payload\":{\"type\":\"message\",\"role\":\"user\",\"content\":[]}}");
        Assert(monitor.Snapshot().State == CodexTaskState.Working, "a user reply resumes its running conversation");
        monitor.ProcessSessionLine("first", EventLine(now.AddSeconds(5), "task_complete", ",\"turn_id\":\"one\""));
        Assert(monitor.Snapshot().State == CodexTaskState.Finish && monitor.Snapshot().FinishCount == 1, "completion shows green Finish once");
        monitor.Acknowledge();
        monitor.ProcessDesktopLine(now.AddSeconds(6).ToString("o") + " info [desktop-notifications] received turn-complete conversationId=first turnId=one");
        Assert(monitor.Snapshot().State == CodexTaskState.None, "duplicate completion cannot restore an acknowledged reminder");
        monitor.ProcessSessionLine("second", EventLine(now.AddSeconds(7), "task_started", ",\"turn_id\":\"two\""));
        monitor.ProcessSessionLine("third", EventLine(now.AddSeconds(7), "task_started", ",\"turn_id\":\"three\""));
        monitor.ProcessSessionLine("third", EventLine(now.AddSeconds(8), "task_complete", ",\"turn_id\":\"three\""));
        Assert(monitor.Snapshot().State == CodexTaskState.Finish && monitor.Snapshot().WorkingCount == 1, "an unread completion takes priority over other running work");
        monitor.ProcessSessionLine("second", EventLine(now.AddSeconds(9), "error", ",\"will_retry\":true"));
        monitor.ProcessDesktopLine(now.AddSeconds(9).ToString("o") + " error [global-error] ResizeObserver loop completed with undelivered notifications conversationId=second");
        monitor.ProcessSessionLine("second", EventLine(now.AddSeconds(9), "item_completed", ",\"item\":{\"type\":\"CommandExecution\",\"status\":\"failed\"}"));
        Assert(monitor.Snapshot().State == CodexTaskState.Finish && monitor.Snapshot().ErrorCount == 0, "retryable, UI and command errors do not replace the unread completion with Err");
        monitor.ProcessSessionLine("second", EventLine(now.AddSeconds(10), "error", ",\"will_retry\":false"));
        Assert(monitor.Snapshot().State == CodexTaskState.Err && monitor.Snapshot().ErrorCount == 1, "terminal task failure shows Err");
        monitor.ProcessSessionLine("second", EventLine(now.AddSeconds(11), "task_started", ",\"turn_id\":\"retry\""));
        Assert(monitor.Snapshot().State == CodexTaskState.Finish && monitor.Snapshot().ErrorCount == 0 && monitor.Snapshot().WorkingCount == 1, "a new task clears only its own failure and preserves another unread completion");
        monitor.ProcessSessionLine("second", EventLine(now.AddSeconds(11), "task_complete", ",\"turn_id\":\"two\""));
        Assert(monitor.Snapshot().State == CodexTaskState.Finish && monitor.Snapshot().WorkingCount == 1, "a delayed completion cannot finish the new turn or consume another unread completion");
        monitor.ProcessSessionLine("second", EventLine(now.AddSeconds(12), "turn_aborted", ",\"turn_id\":\"retry\""));
        Assert(monitor.Snapshot().WorkingCount == 0 && monitor.Snapshot().ErrorCount == 0, "user interruption neither completes nor fails a task");
        monitor.Acknowledge();
        monitor.ProcessSessionLine("first", EventLine(now.AddSeconds(13), "task_started", ",\"turn_id\":\"four\""));
        monitor.ProcessSessionLine("first", "{\"timestamp\":\"" + now.AddSeconds(14).ToString("o") + "\",\"type\":\"response_item\",\"payload\":{\"type\":\"function_call\",\"name\":\"request_user_input_async\",\"call_id\":\"pending\"}}");
        Assert(monitor.Snapshot().State == CodexTaskState.Ask, "an input request in an active turn still shows Ask");
        monitor.ProcessSessionLine("first", "{\"timestamp\":\"" + now.AddSeconds(14).AddMilliseconds(100).ToString("o") + "\",\"type\":\"response_item\",\"payload\":{\"type\":\"function_call_output\",\"call_id\":\"pending\",\"output\":\"{\\\"accepted\\\":true}\"}}");
        monitor.ProcessSessionLine("first", "{\"timestamp\":\"" + now.AddSeconds(14).AddMilliseconds(200).ToString("o") + "\",\"type\":\"response_item\",\"payload\":{\"type\":\"function_call_output\",\"call_id\":\"unrelated\",\"output\":\"{\\\"answers\\\":{}}\"}}");
        Assert(monitor.Snapshot().State == CodexTaskState.Ask, "unrelated output cannot consume an active question");
        monitor.ProcessSessionLine("first", EventLine(now.AddSeconds(15), "task_complete", ",\"turn_id\":\"four\""));
        Assert(monitor.Snapshot().State == CodexTaskState.Finish && monitor.Snapshot().AskCount == 0, "a completed turn closes its accepted async question without requiring a user reply");
        monitor.ProcessSessionLine("first", "{\"timestamp\":\"" + now.AddSeconds(16).ToString("o") + "\",\"type\":\"response_item\",\"payload\":{\"type\":\"function_call_output\",\"call_id\":\"unrelated\",\"output\":\"{\\\"answers\\\":{}}\"}}");
        Assert(monitor.Snapshot().State == CodexTaskState.Finish && monitor.Snapshot().AskCount == 0, "unrelated late output does not restore a closed question");
        monitor.ProcessSessionLine("first", "{\"timestamp\":\"" + now.AddSeconds(17).ToString("o") + "\",\"type\":\"response_item\",\"payload\":{\"type\":\"function_call_output\",\"call_id\":\"pending\",\"output\":\"{\\\"answers\\\":{\\\"choice\\\":\\\"yes\\\"}}\"}}");
        Assert(monitor.Snapshot().State == CodexTaskState.Finish && monitor.Snapshot().AskCount == 0, "a late answer leaves the completed turn in Finish");
        monitor.ProcessSessionLine("input", EventLine(now.AddSeconds(18), "task_started", ",\"turn_id\":\"input-turn\""));
        monitor.ProcessSessionLine("input", "{\"timestamp\":\"" + now.AddSeconds(19).ToString("o") + "\",\"type\":\"response_item\",\"payload\":{\"type\":\"function_call\",\"name\":\"request_user_input\",\"call_id\":\"input-closed\"}}");
        monitor.ProcessSessionLine("input", "{\"timestamp\":\"" + now.AddSeconds(20).ToString("o") + "\",\"type\":\"response_item\",\"payload\":{\"type\":\"function_call_output\",\"call_id\":\"input-closed\",\"output\":\"{\\\"answers\\\":{}}\"}}");
        Assert(monitor.Snapshot().AskCount == 0 && monitor.Snapshot().WorkingCount == 1, "a completed synchronous input request with no choices does not leave Ask behind");

        var folder = Path.Combine(CompanionPaths.Root, "task-monitor-" + Guid.NewGuid().ToString("N"));
        Directory.CreateDirectory(folder);
        var file = Path.Combine(folder, "desktop-123-task.log");
        try
        {
            var oldComplete = now.AddSeconds(-10).ToString("o") + " info [desktop-notifications] received turn-complete conversationId=old turnId=old\n";
            var working = now.AddSeconds(-5).ToString("o") + " info [AppServerConnection] response_routed conversationId=live method=turn/start errorCode=null\n";
            File.WriteAllText(file, oldComplete + working, Encoding.UTF8);
            monitor = new CodexTaskMonitor(folder, null);
            var status = monitor.Poll(true, 123, now.AddHours(-1));
            Assert(status.State == CodexTaskState.Working && status.FinishCount == 0 && status.SourceAvailable, "startup recovers running work without historical Finish alerts");
            var complete = now.AddSeconds(20).ToString("o") + " info [desktop-notifications] received turn-complete conversationId=live turnId=live-end";
            File.AppendAllText(file, complete.Substring(0, complete.Length / 2), Encoding.UTF8);
            Assert(monitor.Poll(true, 123, now.AddHours(-1)).State == CodexTaskState.Working, "incomplete log writes retain the last confirmed task state");
            File.AppendAllText(file, complete.Substring(complete.Length / 2) + "\n", Encoding.UTF8);
            Assert(monitor.Poll(true, 123, now.AddHours(-1)).State == CodexTaskState.Finish, "a completed appended line triggers Finish");
            Assert(monitor.Poll(false, 123, now.AddHours(-1)).State == CodexTaskState.None, "Codex exit clears running and unread task states");
            Assert(monitor.Poll(true, 124, now).State == CodexTaskState.None, "another host process never reuses the old host's log states");
        }
        finally { File.Delete(file); Directory.Delete(folder); }
        folder = Path.Combine(CompanionPaths.Root, "task-identity-" + Guid.NewGuid().ToString("N"));
        Directory.CreateDirectory(folder);
        const string localId = "00000000-0000-0000-0000-000000000123";
        var localFile = Path.Combine(folder, "rollout-fixture-" + localId + ".jsonl");
        file = Path.Combine(folder, "desktop-321-task.log");
        try
        {
            var header = "{\"type\":\"session_meta\",\"payload\":{\"id\":\"" + localId + "\"}}\n";
            File.WriteAllText(localFile, header + EventLine(now.AddSeconds(-2), "task_complete", ",\"turn_id\":\"done\"") + "\n", Encoding.UTF8);
            File.SetLastWriteTimeUtc(localFile, DateTime.UtcNow.AddDays(-3));
            var log = now.AddSeconds(-5).ToString("o") + " info [AppServerConnection] response_routed conversationId=" + localId + " method=turn/start errorCode=null\n" +
                now.AddSeconds(-4).ToString("o") + " info [AppServerConnection] response_routed conversationId=internal-generation method=turn/start errorCode=null\n";
            File.WriteAllText(file, log, Encoding.UTF8);
            monitor = new CodexTaskMonitor(folder, folder);
            var status = monitor.Poll(true, 321, now.AddHours(-1));
            Assert(status.WorkingCount == 0 && status.FinishCount == 0, "internal generation is excluded and older confirmed session history closes stale log work");
            var reply = "{\"timestamp\":\"" + now.AddSeconds(-6).ToString("o") + "\",\"type\":\"response_item\",\"payload\":{\"type\":\"message\",\"role\":\"user\",\"content\":[]}}\n";
            File.WriteAllText(localFile, header + EventLine(now.AddSeconds(-8), "task_started", ",\"turn_id\":\"done\"") + "\n" + reply +
                EventLine(now.AddSeconds(-2), "task_complete", ",\"turn_id\":\"done\"") + "\n", Encoding.UTF8);
            File.WriteAllText(file, now.AddSeconds(-7).ToString("o") + " info [desktop-notifications] received question conversationId=" + localId + " requestId=answered\n" +
                now.AddSeconds(-1).ToString("o") + " info [desktop-notifications] received turn-complete conversationId=" + localId + " turnId=done\n", Encoding.UTF8);
            monitor = new CodexTaskMonitor(folder, folder);
            status = monitor.Poll(true, 321, now.AddHours(-1));
            Assert(status.AskCount == 0 && status.WorkingCount == 0 && status.FinishCount == 0, "cross-source startup replay processes user reply before the later desktop completion");
            var question = "{\"timestamp\":\"" + now.AddSeconds(-7).ToString("o") + "\",\"type\":\"response_item\",\"payload\":{\"type\":\"function_call\",\"name\":\"request_user_input_async\",\"call_id\":\"historical-question\"}}\n";
            var accepted = "{\"timestamp\":\"" + now.AddSeconds(-6).ToString("o") + "\",\"type\":\"response_item\",\"payload\":{\"type\":\"function_call_output\",\"call_id\":\"historical-question\",\"output\":\"{\\\"accepted\\\":true}\"}}\n";
            File.WriteAllText(localFile, header + EventLine(now.AddSeconds(-8), "task_started", ",\"turn_id\":\"done\"") + "\n" + question + accepted +
                EventLine(now.AddSeconds(-2), "task_complete", ",\"turn_id\":\"done\"") + "\n", Encoding.UTF8);
            monitor = new CodexTaskMonitor(folder, folder);
            status = monitor.Poll(true, 321, now.AddHours(-1));
            Assert(status.AskCount == 0 && status.WorkingCount == 0 && status.FinishCount == 0, "startup never reopens a completed async question with no user reply");
            File.AppendAllText(file, now.AddSeconds(1).ToString("o") + " info [desktop-notifications] received question conversationId=" + localId + " requestId=current-question\n", Encoding.UTF8);
            Assert(monitor.Poll(true, 321, now.AddHours(-1)).State == CodexTaskState.Ask, "a newly observed question still shows Ask after startup");
            File.AppendAllText(file, now.AddSeconds(2).ToString("o") + " info [desktop-notifications] received turn-complete conversationId=" + localId + " turnId=current-turn\n", Encoding.UTF8);
            status = monitor.Poll(true, 321, now.AddHours(-1));
            Assert(status.AskCount == 0 && status.State == CodexTaskState.Finish, "a live desktop completion clears its question without a reply record");
            File.WriteAllText(localFile, header.Replace(localId, "unrelated-session"), Encoding.UTF8);
            monitor = new CodexTaskMonitor(folder, folder);
            Assert(monitor.Poll(true, 321, now.AddHours(-1)).WorkingCount == 0, "filename identity alone never confirms a desktop task");
        }
        finally { File.Delete(file); File.Delete(localFile); Directory.Delete(folder); }
    }

    private static void VerifyStatusSettingsAndPalette()
    {
        var settings = new AppSettings { AppearanceMode = "dark", Language = "zh", GlassEnabled = false };
        Assert(settings.TaskStatusEnabled, "task status defaults on for existing installations");
        var cyan = ThemePalette.FromSettings(settings); settings.ThemeId = "aurora";
        var purple = ThemePalette.FromSettings(settings);
        Assert(cyan.Background != purple.Background && cyan.Surface != purple.Surface, "theme changes backgrounds and cards, not only accent swatches");
        settings.AppearanceMode = "light"; var light = ThemePalette.FromSettings(settings);
        Assert(light.IsLight && light.Background != purple.Background && light.Primary != purple.Primary, "light appearance adjusts surfaces and accent contrast");
        var result = new QuotaReadResult { Snapshot = new QuotaSnapshot(), IsCodexRunning = true };
        result.Snapshot.Windows.Add(new QuotaWindow { Kind = QuotaWindowKind.Weekly, WindowMinutes = 10080, UsedPercent = 0 });
        settings.ShowFiveHourQuota = false;
        using (var widget = new TaskbarWidgetForm())
        using (var form = new QuotaPopupForm())
        {
            var status = new TaskStatusSnapshot { State = CodexTaskState.Working, WorkingCount = 2 };
            using (var high = widget.RenderStatusPreview(result, settings, status, 1))
            using (var low = widget.RenderStatusPreview(result, settings, status, 0))
            {
                Assert(high.Width == 200 && high.Height == 38, "single quota reserves a centered status area without shrinking its meter");
                Assert(high.GetPixel(69, 15) != low.GetPixel(69, 15), "Working breathing changes the status dot between phases");
            }
            typeof(TaskbarWidgetForm).GetMethod("UpdateQuotaDescription", BindingFlags.NonPublic | BindingFlags.Instance).Invoke(widget, null);
            Assert(widget.AccessibleDescription.Contains("Working 2") && widget.AccessibleDescription.Contains("100%"), "status counts and full balance remain accessible");
            form.ApplySettings(settings); form.ShowPreviewPage(1);
            var bounds = (Rectangle)typeof(QuotaPopupForm).GetField("_taskStatusToggleBounds", BindingFlags.NonPublic | BindingFlags.Instance).GetValue(form);
            typeof(QuotaPopupForm).GetMethod("OnMouseUp", BindingFlags.NonPublic | BindingFlags.Instance).Invoke(form,
                new object[] { new MouseEventArgs(MouseButtons.Left, 1, bounds.Left + 5, bounds.Top + 5, 0) });
            Assert(!settings.TaskStatusEnabled && !settings.ShowFiveHourQuota, "status switch is independent of the five-hour quota switch");
            using (var bitmap = widget.RenderPreview(result, settings)) Assert(bitmap.Width == 100, "disabling status restores the compact quota strip");
        }
    }

    private static void VerifyGlobalTaskPriority()
    {
        var now = DateTimeOffset.UtcNow;
        var monitor = new CodexTaskMonitor(null, null);
        monitor.ProcessSessionLine("older-error", EventLine(now, "task_failed", ",\"turn_id\":\"err\""));
        monitor.ProcessSessionLine("older-question", EventLine(now.AddSeconds(1), "request_user_input", ",\"call_id\":\"ask\""));
        monitor.ProcessSessionLine("older-finish", EventLine(now.AddSeconds(2), "task_complete", ",\"turn_id\":\"finish\""));
        monitor.ProcessSessionLine("newest-working", EventLine(now.AddSeconds(3), "task_started", ",\"turn_id\":\"working\""));
        var snapshot = monitor.Snapshot();
        Assert(snapshot.State == CodexTaskState.Err && snapshot.ErrorCount == 1 && snapshot.AskCount == 1 &&
            snapshot.FinishCount == 1 && snapshot.WorkingCount == 1, "older Err wins across all chats even when Working is the newest event");
        Assert(snapshot.Targets.Select(t => t.State).SequenceEqual(new[] { CodexTaskState.Err, CodexTaskState.Ask,
            CodexTaskState.Finish, CodexTaskState.Working }), "task targets follow Err Ask Finish Working priority before recency");
        monitor.Acknowledge(snapshot.Targets[0]);
        Assert(monitor.Snapshot().State == CodexTaskState.Ask, "acknowledging one Err falls back to a different chat's Ask");
        monitor.Acknowledge(monitor.Snapshot().Targets[0]);
        Assert(monitor.Snapshot().State == CodexTaskState.Ask, "opening a pending question cannot clear Ask");
        monitor.ProcessSessionLine("older-question", "{\"timestamp\":\"" + now.AddSeconds(4).ToString("o") + "\",\"type\":\"response_item\",\"payload\":{\"type\":\"message\",\"role\":\"user\",\"content\":[]}}");
        Assert(monitor.Snapshot().State == CodexTaskState.Finish, "after a question is answered the older unread Finish wins over newest Working");
        monitor.Acknowledge(monitor.Snapshot().Targets[0]);
        Assert(monitor.Snapshot().State == CodexTaskState.Working, "Working appears only after higher-priority reminders are handled");
        monitor.ProcessSessionLine("newest-working", EventLine(now.AddSeconds(5), "turn_aborted", ",\"turn_id\":\"working\""));
        Assert(monitor.Snapshot().State == CodexTaskState.None, "no remaining task state falls back to neutral Codex");
        Assert(CodexTaskMonitor.Priority(CodexTaskState.Err) == 0 && CodexTaskMonitor.Priority(CodexTaskState.Ask) == 1 &&
            CodexTaskMonitor.Priority(CodexTaskState.Finish) == 2 && CodexTaskMonitor.Priority(CodexTaskState.Working) == 4,
            "configured status priorities preserve the user's P0 P1 P2 P4 ordering");
    }

    private static void VerifyContextMenuTheme()
    {
        foreach (var mode in new[] { "dark", "light" })
        using (var menu = new ThemedContextMenu(new AppSettings { AppearanceMode = mode, ThemeId = "rose", GlassEnabled = false }, true))
        {
            var item = new ToolStripMenuItem("当前会话用量 & literal");
            menu.Items.Add(item); menu.Items.Add(new ToolStripSeparator());
            menu.Items.Add(new ToolStripMenuItem("退出") { Tag = "danger" });
            using (var bitmap = menu.RenderPreview(1.5f))
            {
                Assert(bitmap.GetPixel(0, 0).A == 0, "action menu has rounded transparent corners in " + mode);
                Assert(bitmap.GetPixel(bitmap.Width / 2, 4).ToArgb() == menu.Palette.Background.ToArgb(), "action menu uses the shared themed background in " + mode);
                Assert(bitmap.GetPixel(bitmap.Width / 2, item.Bounds.Top + 6).ToArgb() == menu.Palette.Surface.ToArgb(), "action rows use the same cards as the recent menu in " + mode);
            }
            var previous = menu.Palette.Background;
            menu.ApplySettings(new AppSettings { AppearanceMode = mode == "dark" ? "light" : "dark", GlassEnabled = false, FontScalePercent = 120 });
            Assert(menu.Palette.Background != previous && menu.Items[0].Text.Contains("& literal"), "theme updates keep the action label and click identity in " + mode);
            menu.Location = new Point(-20000, -20200);
            var foreground = GetForegroundWindow(); menu.Visible = true;
            Assert(menu.Visible && GetForegroundWindow() == foreground, "action popup opens without taking Codex focus in " + mode);
            var clicks = 0; item.Click += delegate { clicks++; };
            item.PerformClick();
            Assert(clicks == 1 && !menu.Visible, "an action fires exactly once and closes the themed menu in " + mode);
            menu.Visible = true;
            var dismissal = (PopupClickWatcher)typeof(ThemedContextMenu).GetField("_dismissal", BindingFlags.NonPublic | BindingFlags.Instance).GetValue(menu);
            dismissal.Sample(0, Point.Empty, IntPtr.Zero); dismissal.Sample(1, new Point(100, 100), IntPtr.Zero);
            Assert(!menu.Visible, "outside clicks dismiss the non-modal action menu in " + mode);
        }
    }

    private static void VerifyResetCountdown()
    {
        var now = DateTimeOffset.Parse("2026-10-10T08:00:00Z");
        Assert(TaskbarWidgetForm.FormatResetCountdown(null, now, false) == "--", "missing reset time stays unknown");
        Assert(TaskbarWidgetForm.FormatResetCountdown(now.AddDays(4).AddHours(12), now, false) == "4d12h", "weekly countdown uses compact days and hours");
        Assert(TaskbarWidgetForm.FormatResetCountdown(now.AddHours(3).AddMinutes(26), now, true) == "3h26m", "short countdown retains minutes");
        Assert(TaskbarWidgetForm.FormatResetCountdown(now.AddSeconds(1), now, false) == "1m", "positive sub-minute remainder never looks expired");
        Assert(TaskbarWidgetForm.FormatResetCountdown(now.AddMinutes(59).AddSeconds(59), now, true) == "1h0m", "minute rounding carries into hours");
        Assert(TaskbarWidgetForm.FormatResetCountdown(now.AddHours(23).AddMinutes(59).AddSeconds(59), now, true) == "1d0h", "hour rounding carries into days");
        Assert(TaskbarWidgetForm.FormatResetCountdown(now, now, false) == "待刷新" &&
            TaskbarWidgetForm.FormatResetCountdown(now.AddHours(-1), now, true) == "due", "elapsed resets wait for fresh account data");
        var quota = new QuotaSnapshot();
        var five = new QuotaWindow { Kind = QuotaWindowKind.FiveHour, WindowMinutes = 300, UsedPercent = 23, ResetsAt = DateTimeOffset.Now.AddHours(3) };
        var weekly = new QuotaWindow { Kind = QuotaWindowKind.Weekly, WindowMinutes = 10080, UsedPercent = 37, ResetsAt = DateTimeOffset.Now.AddDays(4) };
        quota.Windows.Add(five); quota.Windows.Add(weekly);
        var result = new QuotaReadResult { Snapshot = quota, IsCodexRunning = true };
        var settings = new AppSettings { Language = "zh", AppearanceMode = "dark", ShowFiveHourQuota = false };
        using (var widget = new TaskbarWidgetForm())
        using (var before = widget.RenderPreview(result, settings))
        {
            Assert(before.Width == 200 && before.Height == 38, "countdown preserves the existing weekly strip dimensions");
            five.ResetsAt = five.ResetsAt.Value.AddHours(1);
            using (var after = widget.RenderPreview(result, settings))
                Assert(!PixelsDiffer(before, after, new Rectangle(0, 0, before.Width, before.Height)), "hidden five-hour reset never appears in the weekly strip");
            weekly.ResetsAt = weekly.ResetsAt.Value.AddDays(1);
            using (var after = widget.RenderPreview(result, settings))
                Assert(PixelsDiffer(before, after, new Rectangle(143, 20, 49, 12)), "weekly countdown is drawn directly below its balance");
            settings.ShowFiveHourQuota = true;
            using (var dual = widget.RenderPreview(result, settings))
            {
                Assert(dual.Width == 252 && dual.Height == 38, "dual countdown preserves the existing two-period strip dimensions");
                five.ResetsAt = five.ResetsAt.Value.AddHours(-2);
                using (var changed = widget.RenderPreview(result, settings))
                {
                    Assert(PixelsDiffer(dual, changed, new Rectangle(28, 20, 44, 12)), "five-hour reset has its own visible countdown");
                    Assert(!PixelsDiffer(dual, changed, new Rectangle(200, 20, 44, 12)), "five-hour reset cannot change the weekly countdown");
                }
            }
        }
    }

    private static bool PixelsDiffer(Bitmap first, Bitmap second, Rectangle bounds)
    {
        for (var y = bounds.Top; y < bounds.Bottom; y++)
            for (var x = bounds.Left; x < bounds.Right; x++)
                if (first.GetPixel(x, y) != second.GetPixel(x, y)) return true;
        return false;
    }

    private static List<object> ActivityBuckets(QuotaPopupForm form, DateTimeOffset now)
    {
        return ((IEnumerable)RawActivityBuckets(form, now)).Cast<object>().ToList();
    }

    private static object RawActivityBuckets(QuotaPopupForm form, DateTimeOffset now)
    {
        var method = typeof(QuotaPopupForm).GetMethod("BuildActivityBuckets", BindingFlags.NonPublic | BindingFlags.Instance,
            null, new[] { typeof(DateTimeOffset) }, null);
        return method.Invoke(form, new object[] { now });
    }

    private static string QuotaSummary(QuotaPopupForm form, DateTimeOffset now)
    {
        return (string)typeof(QuotaPopupForm).GetMethod("ActivityQuotaSummary", BindingFlags.NonPublic | BindingFlags.Instance)
            .Invoke(form, new object[] { RawActivityBuckets(form, now), false });
    }

    private static long BucketTokens(List<object> buckets)
    {
        return buckets.Sum(bucket => (long)bucket.GetType().GetProperty("Tokens").GetValue(bucket, null));
    }

    private static string CardTitle(QuotaPopupForm form, QuotaWindow window, bool secondary)
    {
        return (string)typeof(QuotaPopupForm).GetMethod("QuotaCardTitle", BindingFlags.NonPublic | BindingFlags.Instance)
            .Invoke(form, new object[] { window, secondary });
    }

    private static void VerifyFiveHourActivity()
    {
        var now = DateTimeOffset.Parse("2026-10-10T12:00:00+08:00");
        var quota = new QuotaSnapshot();
        var weekly = new QuotaWindow { Kind = QuotaWindowKind.Weekly, WindowMinutes = 10080, UsedPercent = 31, ResetsAt = now.AddDays(4) };
        quota.Windows.Add(weekly);
        var result = new QuotaReadResult { Snapshot = quota, ActivityIsRequestBased = true, ActivityStatus = "partial" };
        result.ActivityTimeline.Add(new TokenActivitySample { CapturedAt = now.AddHours(-6), Tokens = 9000 });
        result.ActivityTimeline.Add(new TokenActivitySample { CapturedAt = now.AddHours(-5), Tokens = 100 });
        result.ActivityTimeline.Add(new TokenActivitySample { CapturedAt = now.AddHours(-4), Tokens = 200 });
        result.ActivityTimeline.Add(new TokenActivitySample { CapturedAt = now.AddMinutes(-1), Tokens = 300 });
        result.ActivityTimeline.Add(new TokenActivitySample { CapturedAt = now.AddMinutes(1), Tokens = 8000 });
        result.QuotaHistory.Add(new QuotaUsageSample { CapturedAt = now.AddHours(-4), WindowMinutes = 10080, RemainingPercent = 80 });
        result.QuotaHistory.Add(new QuotaUsageSample { CapturedAt = now.AddHours(-1), WindowMinutes = 10080, RemainingPercent = 69 });
        using (var form = new QuotaPopupForm())
        {
            form.ApplySettings(new AppSettings { Language = "zh", GlassEnabled = false, ActivityRange = ActivityRange.Hours5 });
            form.ApplyResult(result);
            var buckets = ActivityBuckets(form, now);
            Assert(buckets.Count == 30 && BucketTokens(buckets) == 600, "weekly-only quota still displays local five-hour requests and excludes old/future records");
            Assert(CardTitle(form, quota.FiveHourWindow, false) == "5 小时" && CardTitle(form, quota.DisplayWeeklyWindow, true) == "每周", "weekly-only response produces distinct quota card titles");
            Assert(!(bool)typeof(QuotaPopupForm).GetField("_quotaHistoryAvailable", BindingFlags.NonPublic | BindingFlags.Instance).GetValue(form), "weekly consumption is not displayed as five-hour consumption");
            Assert(result.ActivityStatus == "partial", "partial activity stays partial");

            result.Snapshot = null;
            Assert(BucketTokens(ActivityBuckets(form, now)) == 600, "local activity survives unavailable account quota");
            result.Snapshot = quota;
            var fiveHour = new QuotaWindow { Kind = QuotaWindowKind.FiveHour, WindowMinutes = 300 };
            quota.Windows.Add(fiveHour);
            Assert(BucketTokens(ActivityBuckets(form, now)) == 600, "missing reset time falls back to the last five hours");
            fiveHour.ResetsAt = now.AddHours(-1);
            Assert(BucketTokens(ActivityBuckets(form, now)) == 600, "expired quota cycle does not hide recent activity");
            fiveHour.ResetsAt = now.AddHours(6);
            Assert(BucketTokens(ActivityBuckets(form, now)) == 600, "invalid future reset does not hide recent activity");
            fiveHour.ResetsAt = now.AddHours(2);
            Assert(BucketTokens(ActivityBuckets(form, now)) == 300, "valid quota cycle keeps its original start and excludes future requests");

            quota.Windows.Remove(fiveHour);
            result.ActivityIsRequestBased = false;
            result.ActivityTimeline.Clear();
            var sourceStart = now.AddHours(-8);
            result.ActivityTimeline.Add(new TokenActivitySample { SourceKey = "old", SourceStartedAt = sourceStart, CapturedAt = now.AddHours(-6), Tokens = 1000 });
            result.ActivityTimeline.Add(new TokenActivitySample { SourceKey = "old", SourceStartedAt = sourceStart, CapturedAt = now.AddHours(-4), Tokens = 1100 });
            result.ActivityTimeline.Add(new TokenActivitySample { SourceKey = "old", SourceStartedAt = sourceStart, CapturedAt = now.AddHours(-1), Tokens = 50 });
            result.ActivityTimeline.Add(new TokenActivitySample { SourceKey = "unbased", SourceStartedAt = sourceStart, CapturedAt = now.AddHours(-1), Tokens = 7000 });
            Assert(BucketTokens(ActivityBuckets(form, now)) == 150, "rolling legacy activity keeps baseline subtraction and counter reset handling");
            form.ApplyResult(null);
            Assert(BucketTokens(ActivityBuckets(form, now)) == 0, "startup without a result produces an empty chart safely");
        }
    }

    private static void VerifyQuotaVisibilitySetting()
    {
        var previousRoot = Environment.GetEnvironmentVariable("SESSION_USAGE_STATE_DIR");
        var folder = Path.Combine(AppDomain.CurrentDomain.BaseDirectory, "visibility-settings-" + Guid.NewGuid().ToString("N"));
        var settingsPath = Path.Combine(folder, "settings.ini");
        Directory.CreateDirectory(folder);
        Environment.SetEnvironmentVariable("SESSION_USAGE_STATE_DIR", folder);
        try
        {
            File.WriteAllText(settingsPath, "WidgetVisible=False\nGlassOpacityPercent=55\nWidgetScalePercent=110\n");
            var saved = AppSettings.Load();
            Assert(saved.TaskStatusEnabled && saved.ShowFiveHourQuota && !saved.WidgetVisible && saved.GlassOpacityPercent == 55, "legacy settings enable task status and preserve existing preferences");
            saved.ShowFiveHourQuota = false;
            saved.TaskStatusEnabled = false;
            saved.Save();
            saved = AppSettings.Load();
            Assert(!saved.TaskStatusEnabled && !saved.ShowFiveHourQuota && saved.WidgetScalePercent == 110 && saved.GlassOpacityPercent == 55, "quota and status preferences survive reload without changing other settings");
            saved.ShowFiveHourQuota = true;
            saved.Save();
            Assert(AppSettings.Load().ShowFiveHourQuota, "five-hour display can be enabled again and persisted");

            var now = DateTimeOffset.Now;
            var quota = new QuotaSnapshot();
            quota.Windows.Add(new QuotaWindow { Kind = QuotaWindowKind.FiveHour, WindowMinutes = 300, UsedPercent = 25, ResetsAt = now.AddHours(1) });
            quota.Windows.Add(new QuotaWindow { Kind = QuotaWindowKind.Weekly, WindowMinutes = 10080, UsedPercent = 31, ResetsAt = now.AddDays(4) });
            var result = new QuotaReadResult { Snapshot = quota, IsCodexRunning = true, ActivityIsRequestBased = true };
            result.ActivityTimeline.Add(new TokenActivitySample { CapturedAt = now.AddMinutes(-10), Tokens = 1234 });
            var settings = new AppSettings { Language = "zh", GlassEnabled = false, ActivityRange = ActivityRange.Hours5 };
            using (var form = new QuotaPopupForm())
            using (var widget = new TaskbarWidgetForm())
            {
                form.ApplySettings(settings);
                form.ApplyResult(result);
                form.ShowPreviewPage(1);
                var changed = 0;
                form.SettingsChanged += delegate { changed++; settings.Save(); };
                var bounds = (Rectangle)typeof(QuotaPopupForm).GetField("_fiveHourQuotaToggleBounds", BindingFlags.NonPublic | BindingFlags.Instance).GetValue(form);
                var click = typeof(QuotaPopupForm).GetMethod("OnMouseUp", BindingFlags.NonPublic | BindingFlags.Instance);
                var mouse = new MouseEventArgs(MouseButtons.Left, 1, bounds.Left + 5, bounds.Top + 5, 0);
                click.Invoke(form, new object[] { mouse });
                Assert(!settings.ShowFiveHourQuota && !AppSettings.Load().ShowFiveHourQuota && changed == 1, "settings-page toggle raises the save event and hides the quota");
                Assert(BucketTokens(ActivityBuckets(form, now)) == 1234 && quota.FiveHourWindow.RemainingPercent == 75, "hiding quota leaves five-hour tokens and account data intact");
                int hiddenWidth;
                using (var bitmap = widget.RenderPreview(result, settings)) hiddenWidth = bitmap.Width;
                typeof(TaskbarWidgetForm).GetMethod("UpdateQuotaDescription", BindingFlags.NonPublic | BindingFlags.Instance).Invoke(widget, null);
                Assert(!widget.AccessibleDescription.Contains("5h") && widget.AccessibleDescription.Contains("每周 69%"), "hidden five-hour quota is absent from taskbar description while weekly balance remains");
                click.Invoke(form, new object[] { mouse });
                Assert(settings.ShowFiveHourQuota && AppSettings.Load().ShowFiveHourQuota && changed == 2, "settings-page toggle restores and saves the quota");
                using (var bitmap = widget.RenderPreview(result, settings))
                    Assert(bitmap.Width > hiddenWidth, "reenabling five-hour quota restores the dual taskbar width");
                typeof(TaskbarWidgetForm).GetMethod("UpdateQuotaDescription", BindingFlags.NonPublic | BindingFlags.Instance).Invoke(widget, null);
                Assert(widget.AccessibleDescription.Contains("5h 75%") && widget.AccessibleDescription.Contains("每周 69%"), "reenabling quota restores both taskbar balances");
                settings.ShowFiveHourQuota = false;
                quota.ReserveWindow = new QuotaWindow { Kind = QuotaWindowKind.Weekly, Key = "gpt-reserve:10080", UsedPercent = 40, WindowMinutes = 10080 };
                using (var bitmap = widget.RenderPreview(result, settings))
                    Assert(bitmap.Width == hiddenWidth, "reserve quota uses the single taskbar width when five-hour display is hidden");
                typeof(TaskbarWidgetForm).GetMethod("UpdateQuotaDescription", BindingFlags.NonPublic | BindingFlags.Instance).Invoke(widget, null);
                Assert(widget.AccessibleDescription.Contains("备用 60%") && !widget.AccessibleDescription.Contains("5h"), "reserve balance remains visible without five-hour balance");
                quota.ReserveWindow = null;
                quota.Windows.RemoveAt(1);
                using (var bitmap = widget.RenderPreview(result, settings)) { }
                typeof(TaskbarWidgetForm).GetMethod("UpdateQuotaDescription", BindingFlags.NonPublic | BindingFlags.Instance).Invoke(widget, null);
                Assert(widget.AccessibleDescription.Contains("每周 --") && !widget.AccessibleDescription.Contains("75%"), "unavailable weekly quota never borrows a hidden five-hour balance");
                settings.ShowFiveHourQuota = true;
                form.ShowPreviewPage(2);
                bounds = (Rectangle)typeof(QuotaPopupForm).GetField("_quotaLayoutToggleBounds", BindingFlags.NonPublic | BindingFlags.Instance).GetValue(form);
                click.Invoke(form, new object[] { new MouseEventArgs(MouseButtons.Left, 1, bounds.Left + 5, bounds.Top + 5, 0) });
                Assert(!settings.QuotaPrimaryEmphasis && settings.ShowFiveHourQuota, "relocated quota emphasis toggle remains independent of quota visibility");
                settings.ShowFiveHourQuota = false;
                settings.Reset();
                Assert(settings.ShowFiveHourQuota, "reset defaults restores five-hour visibility");
            }
        }
        finally
        {
            Environment.SetEnvironmentVariable("SESSION_USAGE_STATE_DIR", previousRoot);
            File.Delete(settingsPath);
            Directory.Delete(folder);
        }
    }

    private static void VerifyWeeklyActivityConsumption()
    {
        var now = DateTimeOffset.Parse("2026-10-10T12:00:00+08:00");
        var reset = now.AddDays(4);
        var quota = new QuotaSnapshot();
        var fiveHour = new QuotaWindow { Kind = QuotaWindowKind.FiveHour, WindowMinutes = 300, Key = "codex:300", ResetsAt = now.AddHours(1) };
        quota.Windows.Add(fiveHour);
        quota.Windows.Add(new QuotaWindow { Kind = QuotaWindowKind.Weekly, WindowMinutes = 10080, Key = "codex:10080", ResetsAt = reset });
        var result = new QuotaReadResult { Snapshot = quota, ActivityIsRequestBased = true };
        var offsets = new[] { -25d, -23d, -6d, -4d, -1d, -1d / 6d, 1d / 60d };
        var remaining = new[] { 90, 85, 80, 79, 75, 74, 1 };
        for (var index = 0; index < offsets.Length; index++)
            result.QuotaHistory.Add(new QuotaUsageSample { CapturedAt = now.AddHours(offsets[index]), WindowKey = "codex:10080",
                WindowMinutes = 10080, RemainingPercent = remaining[index], ResetsAt = reset });
        result.QuotaHistory.Add(new QuotaUsageSample { CapturedAt = now.AddHours(-6), WindowKey = "codex:300", WindowMinutes = 300, RemainingPercent = 90 });
        result.QuotaHistory.Add(new QuotaUsageSample { CapturedAt = now.AddHours(-4), WindowKey = "codex:300", WindowMinutes = 300, RemainingPercent = 80 });
        result.QuotaHistory.Add(new QuotaUsageSample { CapturedAt = now.AddHours(-1), WindowKey = "codex:300", WindowMinutes = 300, RemainingPercent = 70 });
        result.QuotaHistory.Add(new QuotaUsageSample { CapturedAt = now.AddMinutes(-10), WindowKey = "codex:300", WindowMinutes = 300, RemainingPercent = 69 });
        result.QuotaHistory.Add(new QuotaUsageSample { CapturedAt = now.AddHours(-4), WindowKey = "gpt-reserve:10080", WindowMinutes = 10080, RemainingPercent = 100 });
        result.QuotaHistory.Add(new QuotaUsageSample { CapturedAt = now.AddHours(-1), WindowKey = "gpt-reserve:10080", WindowMinutes = 10080, RemainingPercent = 2 });
        using (var form = new QuotaPopupForm())
        {
            form.ApplySettings(new AppSettings { Language = "zh", GlassEnabled = false, ActivityRange = ActivityRange.Hours5 });
            form.ApplyResult(result);
            Assert(QuotaSummary(form, now) == "额度消耗 5h 11% · 周 5%", "five-hour activity uses the range-start baseline for independent short and weekly consumption without reserve or future samples");
            form.ApplySettings(new AppSettings { Language = "zh", GlassEnabled = false, ActivityRange = ActivityRange.Hours24 });
            Assert(QuotaSummary(form, now) == "周额度消耗 11%", "24-hour summary shows weekly consumption only and excludes pre-range and five-hour deltas");
            var hourly = ActivityBuckets(form, now);
            Assert(hourly.Sum(bucket => (double)bucket.GetType().GetProperty("QuotaUsedPercent").GetValue(bucket, null)) == 11,
                "24-hour hover buckets also use weekly consumption");
            form.ApplySettings(new AppSettings { Language = "en", GlassEnabled = false, ActivityRange = ActivityRange.Hours24 });
            Assert(QuotaSummary(form, now) == "Weekly quota used 11%", "English 24-hour summary names weekly quota");
            quota.Windows.Remove(fiveHour);
            form.ApplySettings(new AppSettings { Language = "zh", GlassEnabled = false, ActivityRange = ActivityRange.Hours5, ShowFiveHourQuota = false });
            Assert(QuotaSummary(form, now) == "周额度消耗 5%", "weekly five-hour consumption remains available when short quota is absent or hidden");

            result.QuotaHistory.Clear();
            result.QuotaHistory.Add(new QuotaUsageSample { CapturedAt = now.AddHours(-1), WindowKey = "codex:10080", WindowMinutes = 10080, RemainingPercent = 74, ResetsAt = reset });
            Assert(QuotaSummary(form, now) == "周额度消耗 --", "one snapshot does not fabricate consumption or zero");
            result.QuotaHistory.Add(new QuotaUsageSample { CapturedAt = now.AddMinutes(-10), WindowKey = "codex:10080", WindowMinutes = 10080, RemainingPercent = 74, ResetsAt = reset });
            Assert(QuotaSummary(form, now) == "周额度消耗 0%", "unchanged recorded weekly balance legitimately yields zero consumption");
            result.QuotaHistory.Clear();
            result.QuotaHistory.Add(new QuotaUsageSample { CapturedAt = now.AddHours(-5), WindowKey = "codex:10080", WindowMinutes = 10080, RemainingPercent = 50, ResetsAt = now.AddHours(-3) });
            result.QuotaHistory.Add(new QuotaUsageSample { CapturedAt = now.AddHours(-4), WindowKey = "codex:10080", WindowMinutes = 10080, RemainingPercent = 20, ResetsAt = now.AddHours(-3) });
            result.QuotaHistory.Add(new QuotaUsageSample { CapturedAt = now.AddHours(-2), WindowKey = "codex:10080", WindowMinutes = 10080, RemainingPercent = 90, ResetsAt = now.AddHours(-3).AddDays(7) });
            result.QuotaHistory.Add(new QuotaUsageSample { CapturedAt = now.AddHours(-1), WindowKey = "codex:10080", WindowMinutes = 10080, RemainingPercent = 80, ResetsAt = now.AddHours(-3).AddDays(7) });
            Assert(QuotaSummary(form, now) == "周额度消耗 50%", "weekly reset retains consumption on both sides without subtracting a replenishment");
            result.QuotaHistory.Clear();
            Assert(QuotaSummary(form, now) == "周额度消耗 --", "missing weekly history remains explicitly unavailable");
            result.QuotaHistory.Add(new QuotaUsageSample { CapturedAt = now.AddDays(-1), WindowKey = "codex:10080", WindowMinutes = 10080, RemainingPercent = 100, ResetsAt = reset });
            result.QuotaHistory.Add(new QuotaUsageSample { CapturedAt = now.AddHours(-4), WindowKey = "codex:10080", WindowMinutes = 10080, RemainingPercent = 50, ResetsAt = reset });
            result.QuotaHistory.Add(new QuotaUsageSample { CapturedAt = now.AddHours(-1), WindowKey = "codex:10080", WindowMinutes = 10080, RemainingPercent = 40, ResetsAt = reset });
            Assert(QuotaSummary(form, now) == "周额度消耗 10%", "an old baseline cannot pull out-of-range consumption into the selected period");
            result.QuotaHistory.Add(new QuotaUsageSample { CapturedAt = now, WindowKey = "codex:10080", WindowMinutes = 10080, RemainingPercent = 35, ResetsAt = reset });
            Assert(QuotaSummary(form, now) == "周额度消耗 15%", "a closing snapshot at now is included in the last five-hour bucket");
        }
    }
}
