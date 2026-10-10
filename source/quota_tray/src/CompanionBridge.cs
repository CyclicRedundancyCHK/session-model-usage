using System;
using System.Collections;
using System.Collections.Generic;
using System.Diagnostics;
using System.Globalization;
using System.IO;
using System.Linq;
using System.Text;
using System.Web.Script.Serialization;

namespace CodexQuotaTray
{
    internal static class CompanionPaths
    {
        internal static string Root
        {
            get { return Environment.GetEnvironmentVariable("SESSION_USAGE_STATE_DIR") ??
                Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData), "CodexSessionUsage"); }
        }
        internal static string FilePath(string name) { return Path.Combine(Root, name); }
        internal static string MigratedPath(string name)
        {
            var target = FilePath(name);
            if (!File.Exists(target))
            {
                var prior = Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData), "CodexMeter", name);
                try
                {
                    if (File.Exists(prior)) { Directory.CreateDirectory(Root); File.Copy(prior, target, false); }
                }
                catch { }
            }
            return target;
        }
    }

    internal sealed class SessionUsageRow
    {
        public string Name;
        public string Configuration;
        public long Total;
        public long Input;
        public long Cached;
        public long Output;
        public long Reasoning;
        public long Main;
        public long Children;
    }

    internal sealed class SessionUsageSnapshot
    {
        public string ThreadId;
        public string Status;
        public string Message;
        public string Warning;
        public long? Total;
        public readonly List<SessionUsageRow> Models = new List<SessionUsageRow>();
        public readonly List<SessionUsageRow> Threads = new List<SessionUsageRow>();
    }

    internal static class CompanionBridge
    {
        internal static Dictionary<string, object> Dictionary(object value) { return value as Dictionary<string, object>; }
        internal static object Get(Dictionary<string, object> value, string key)
        { object found; return value != null && value.TryGetValue(key, out found) ? found : null; }
        internal static string Text(object value) { return value as string; }
        internal static double Number(object value)
        {
            double parsed;
            return value != null && double.TryParse(Convert.ToString(value, CultureInfo.InvariantCulture),
                NumberStyles.Float, CultureInfo.InvariantCulture, out parsed) && !double.IsNaN(parsed) && !double.IsInfinity(parsed) ? parsed : 0;
        }
        internal static long Tokens(Dictionary<string, object> value, string key)
        {
            long parsed;
            return long.TryParse(Convert.ToString(Get(value, key), CultureInfo.InvariantCulture),
                NumberStyles.Integer, CultureInfo.InvariantCulture, out parsed) && parsed >= 0 ? parsed : 0;
        }
        internal static IEnumerable<Dictionary<string, object>> Rows(object value)
        { var rows = value as IEnumerable; return rows == null ? Enumerable.Empty<Dictionary<string, object>>() : rows.Cast<object>().Select(Dictionary).Where(row => row != null); }
        internal static Dictionary<string, object> Parse(string json)
        {
            try { return new JavaScriptSerializer { MaxJsonLength = 64 * 1024 * 1024 }.DeserializeObject(json) as Dictionary<string, object>; }
            catch { return null; }
        }
        internal static Dictionary<string, object> Read(string path)
        {
            try
            {
                if (!File.Exists(path) || new FileInfo(path).Length > 64 * 1024 * 1024) return null;
                using (var stream = new FileStream(path, FileMode.Open, FileAccess.Read, FileShare.ReadWrite | FileShare.Delete))
                using (var reader = new StreamReader(stream, Encoding.UTF8)) return Parse(reader.ReadToEnd());
            }
            catch { return null; }
        }
        internal static double Now { get { return (DateTimeOffset.UtcNow - new DateTimeOffset(1970, 1, 1, 0, 0, 0, TimeSpan.Zero)).TotalSeconds; } }

        internal static bool? DesktopRunning()
        {
            var state = Read(CompanionPaths.FilePath("runtime.json"));
            if (state == null) return null; // Standalone read-only test harness.
            if (Now - Number(Get(state, "heartbeat")) > 10) return false;
            try
            {
                using (var app = Process.GetProcessById((int)Number(Get(state, "app_pid"))))
                {
                    var created = (app.StartTime.ToUniversalTime() - new DateTime(1970, 1, 1)).TotalSeconds;
                    return !app.HasExited && Math.Abs(created - Number(Get(state, "app_created"))) < 0.1;
                }
            }
            catch { return false; }
        }

        internal static SessionUsageSnapshot ReadSession(string runId)
        {
            var state = Read(CompanionPaths.FilePath("runtime.json"));
            var attachment = Text(Get(state, "attachment_id"));
            // Attachment names are generated hex identifiers, never arbitrary paths.
            if (attachment == null || !System.Text.RegularExpressions.Regex.IsMatch(attachment, "^[a-f0-9]{32}$"))
                return Unavailable(Text(Get(state, "message")));
            return ParseSession(state, Read(CompanionPaths.FilePath("usage-" + attachment + ".json")), runId, Now);
        }

        internal static SessionUsageSnapshot Unavailable(string message)
        { return new SessionUsageSnapshot { Status = "pending", Message = message ?? "等待确认当前本地会话" }; }

        internal static SessionUsageSnapshot ParseSession(Dictionary<string, object> state,
            Dictionary<string, object> envelope, string runId, double now)
        {
            var thread = Text(Get(state, "thread_id"));
            var usage = Dictionary(Get(envelope, "usage"));
            var heartbeat = Number(Get(state, "heartbeat"));
            var captured = Number(Get(envelope, "captured_at"));
            if (thread == null || runId != Text(Get(state, "run_id")) || runId != Text(Get(envelope, "run_id")) ||
                Text(Get(state, "attachment_id")) != Text(Get(envelope, "attachment_id")) ||
                thread != Text(Get(usage, "thread_id")) || now - heartbeat > 10 || heartbeat > now + 2 ||
                now - captured > 15 || captured > now + 2)
                return Unavailable(Text(Get(state, "message")));
            var snapshot = new SessionUsageSnapshot { ThreadId = thread, Status = Text(Get(usage, "status")),
                Message = Text(Get(state, "message")) };
            var totals = Dictionary(Get(usage, "totals"));
            if (totals != null) snapshot.Total = Tokens(totals, "total_tokens");
            foreach (var model in Rows(Get(usage, "models")))
            {
                var configurations = Rows(Get(model, "configurations")).ToList();
                if (configurations.Count == 0) configurations.Add(model);
                foreach (var config in configurations)
                {
                    var effort = Text(Get(config, "reasoning_effort")) ?? "未记录";
                    var fast = Get(config, "fast_mode");
                    var speed = fast is bool ? ((bool)fast ? "Fast" : "普通") : (Text(Get(config, "service_tier")) ?? "未记录");
                    var row = UsageRow(config);
                    row.Name = Text(Get(model, "model")) ?? "未归属模型";
                    row.Configuration = effort + " · " + speed;
                    snapshot.Models.Add(row);
                }
            }
            foreach (var threadRow in Rows(Get(usage, "threads")))
            {
                var row = UsageRow(threadRow);
                row.Name = Text(Get(threadRow, "role")) == "main" ? "主会话" : (Text(Get(threadRow, "agent_path")) ?? "子智能体");
                row.Configuration = Text(Get(threadRow, "status")) ?? "pending";
                snapshot.Threads.Add(row);
            }
            var warnings = Get(usage, "warnings") as IEnumerable;
            if (warnings != null) snapshot.Warning = string.Join("；", warnings.Cast<object>().OfType<string>().Take(2));
            return snapshot;
        }

        private static SessionUsageRow UsageRow(Dictionary<string, object> row)
        {
            var totals = Dictionary(Get(row, "totals"));
            return new SessionUsageRow { Total = Tokens(totals, "total_tokens"), Input = Tokens(totals, "input_tokens"),
                Cached = Tokens(totals, "cached_input_tokens"), Output = Tokens(totals, "output_tokens"),
                Reasoning = Tokens(totals, "reasoning_output_tokens"),
                Main = Tokens(Dictionary(Get(row, "main")), "total_tokens"),
                Children = Tokens(Dictionary(Get(row, "subagents")), "total_tokens") };
        }

        internal static void ApplyActivity(QuotaReadResult result)
        {
            result.Activity = new List<TokenActivitySample>();
            result.ActivityTimeline = new List<TokenActivitySample>();
            result.ActivityIsRequestBased = true;
            var data = Read(CompanionPaths.FilePath("activity.json"));
            var captured = Number(Get(data, "captured_at"));
            result.ActivityStatus = captured > Now - 660 && captured <= Now + 2 ? Text(Get(data, "status")) : "pending";
            if (result.ActivityStatus == "pending") return;
            foreach (var row in Rows(Get(data, "samples")))
            {
                DateTimeOffset stamp;
                if (!DateTimeOffset.TryParse(Text(Get(row, "timestamp")), CultureInfo.InvariantCulture, DateTimeStyles.None, out stamp)) continue;
                result.Activity.Add(new TokenActivitySample { CapturedAt = stamp, Tokens = Tokens(row, "tokens") });
            }
            result.ActivityTimeline = result.Activity;
        }

        internal static void Command(string runId, string action)
        {
            if (action != "stop" && action != "retry") return;
            Directory.CreateDirectory(CompanionPaths.FilePath("commands"));
            var path = CompanionPaths.FilePath(Path.Combine("commands", Guid.NewGuid().ToString("N") + ".json"));
            var temp = path + ".tmp";
            File.WriteAllText(temp, new JavaScriptSerializer().Serialize(new { run_id = runId, action = action }), new UTF8Encoding(false));
            File.Move(temp, path);
        }

        internal static void PublishFrontend(string runId, QuotaReadResult quota, bool widgetVisible, bool popupVisible, TaskStatusSnapshot taskStatus, bool taskStatusEnabled)
        {
            var path = CompanionPaths.FilePath("frontend.json");
            var temp = path + ".tmp";
            try
            {
                Directory.CreateDirectory(CompanionPaths.Root);
                using (var process = Process.GetCurrentProcess())
                {
                    var snapshot = quota == null ? null : quota.Snapshot;
                    var five = snapshot == null ? null : snapshot.FiveHourWindow;
                    var weekly = snapshot == null ? null : snapshot.DisplayWeeklyWindow;
                    var value = new { run_id = runId, pid = process.Id,
                        created = (process.StartTime.ToUniversalTime() - new DateTime(1970, 1, 1)).TotalSeconds,
                        heartbeat = Now, widget_visible = widgetVisible, popup_visible = popupVisible,
                        quota_live = quota != null && quota.IsLiveQuota, codex_running = quota != null && quota.IsCodexRunning,
                        five_hour_remaining = five == null ? (int?)null : five.RemainingPercent,
                        weekly_remaining = weekly == null ? (int?)null : weekly.RemainingPercent,
                        activity_status = quota == null ? "pending" : quota.ActivityStatus,
                        task_status_enabled = taskStatusEnabled, task_status = taskStatus.Label,
                        working_count = taskStatus.WorkingCount, ask_count = taskStatus.AskCount,
                        error_count = taskStatus.ErrorCount, finish_count = taskStatus.FinishCount,
                        task_thread_ids = taskStatus.Targets.Take(128).Select(t => t.ThreadId).ToArray(),
                        task_source_available = taskStatus.SourceAvailable };
                    File.WriteAllText(temp, new JavaScriptSerializer().Serialize(value), new UTF8Encoding(false));
                }
                if (File.Exists(path)) File.Replace(temp, path, null); else File.Move(temp, path);
            }
            catch { try { if (File.Exists(temp)) File.Delete(temp); } catch { } }
        }
    }
}
