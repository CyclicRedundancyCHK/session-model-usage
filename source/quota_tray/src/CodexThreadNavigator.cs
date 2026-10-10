using System;
using System.ComponentModel;
using System.Diagnostics;
using System.Linq;
using System.Collections.Generic;
using System.Windows.Forms;
using System.Globalization;

namespace CodexQuotaTray
{
    internal sealed class RecentThreadSnapshot
    {
        internal bool Available;
        internal DateTimeOffset CapturedAt;
        internal readonly List<TaskNavigationTarget> Targets = new List<TaskNavigationTarget>();
        internal readonly Dictionary<string, object> Titles = new Dictionary<string, object>();
    }

    internal static class CodexThreadNavigator
    {
        internal static string ThreadUri(string threadId)
        {
            Guid id;
            return Guid.TryParseExact(threadId, "D", out id) ? "codex://threads/" + id.ToString("D") + "?hostId=local" : null;
        }

        internal static TaskNavigationTarget[] Targets(TaskStatusSnapshot snapshot)
        {
            return snapshot == null || snapshot.State == CodexTaskState.None ? new TaskNavigationTarget[0] :
                snapshot.Targets.Where(t => t.State == snapshot.State && ThreadUri(t.ThreadId) != null).ToArray();
        }

        internal static Dictionary<string, object> ReadTitles(string runId)
        {
            var data = CompanionBridge.Read(CompanionPaths.FilePath("task-titles.json"));
            var age = CompanionBridge.Now - CompanionBridge.Number(CompanionBridge.Get(data, "captured_at"));
            return CompanionBridge.Text(CompanionBridge.Get(data, "run_id")) == runId && age >= 0 && age < 30
                ? CompanionBridge.Dictionary(CompanionBridge.Get(data, "titles")) : null;
        }

        internal static RecentThreadSnapshot ReadRecent(string runId)
        {
            var result = new RecentThreadSnapshot();
            var data = CompanionBridge.Read(CompanionPaths.FilePath("recent-threads.json"));
            var age = CompanionBridge.Now - CompanionBridge.Number(CompanionBridge.Get(data, "captured_at"));
            if (CompanionBridge.Text(CompanionBridge.Get(data, "run_id")) != runId || age < 0 || age >= 30 ||
                CompanionBridge.Text(CompanionBridge.Get(data, "status")) != "complete") return result;
            if (!(CompanionBridge.Get(data, "threads") is object[])) return result;
            result.Available = true;
            result.CapturedAt = new DateTimeOffset(1970, 1, 1, 0, 0, 0, TimeSpan.Zero)
                .AddSeconds(CompanionBridge.Number(CompanionBridge.Get(data, "captured_at")));
            foreach (var row in CompanionBridge.Rows(CompanionBridge.Get(data, "threads")).Take(128))
            {
                var id = CompanionBridge.Text(CompanionBridge.Get(row, "thread_id"));
                if (ThreadUri(id) == null || result.Titles.ContainsKey(id)) continue;
                result.Targets.Add(new TaskNavigationTarget { ThreadId = id });
                result.Titles[id] = CompanionBridge.Text(CompanionBridge.Get(row, "title"));
                if (result.Targets.Count == 6) break;
            }
            if (result.Targets.Count == 0 && ((object[])CompanionBridge.Get(data, "threads")).Length > 0)
                result.Available = false;
            return result;
        }

        internal static ThemedContextMenu RecentMenu(RecentThreadSnapshot recent, bool english, Action<TaskNavigationTarget> open, TaskStatusSnapshot status = null, AppSettings settings = null)
        {
            var menu = new ThemedContextMenu(settings ?? new AppSettings());
            menu.Items.Add(new ToolStripMenuItem(english ? "Recent chats" : "最近会话") { Enabled = false, Tag = "heading" });
            menu.Items.Add(new ToolStripSeparator());
            foreach (var target in recent.Targets)
            {
                var task = status == null ? null : status.Targets.FirstOrDefault(t => t.ThreadId == target.ThreadId);
                var selected = new TaskNavigationTarget { ThreadId = target.ThreadId,
                    State = task == null ? CodexTaskState.None : task.State, Updated = task == null ? default(DateTimeOffset) : task.Updated };
                var title = DisplayTitle(CompanionBridge.Text(CompanionBridge.Get(recent.Titles, target.ThreadId)), english);
                var item = new ToolStripMenuItem(MenuLabel(selected, recent.Titles, english)) { AutoToolTip = false,
                    ToolTipText = title, AccessibleName = title, Tag = selected, ShowShortcutKeys = false };
                item.Click += delegate { open(selected); };
                menu.Items.Add(item);
            }
            if (recent.Targets.Count == 0)
                menu.Items.Add(new ToolStripMenuItem(recent.Available
                    ? (english ? "No recent chats" : "暂无最近会话")
                    : (english ? "Chat list unavailable; try again shortly" : "暂时无法读取会话，请稍后重试")) { Enabled = false });
            menu.ApplyTheme();
            return menu;
        }

        internal static string DisplayTitle(string title, bool english = false)
        {
            title = string.IsNullOrWhiteSpace(title) ? "" : title.Replace("\r", " ").Replace("\n", " ").Replace("\t", " ").Trim();
            return title.Length == 0 ? (english ? "Untitled" : "未命名会话") : title;
        }

        internal static string MenuLabel(TaskNavigationTarget target, Dictionary<string, object> titles, bool english = false)
        {
            var title = DisplayTitle(CompanionBridge.Text(CompanionBridge.Get(titles, target.ThreadId)), english);
            var characters = StringInfo.ParseCombiningCharacters(title);
            // The ellipsis takes the final slot, keeping at most 12 visible characters.
            return characters.Length > 12 ? title.Substring(0, characters[11]) + "…" : title;
        }

        internal static bool Open(TaskNavigationTarget target, Func<string, bool> launch = null)
        {
            var uri = target == null ? null : ThreadUri(target.ThreadId);
            if (uri == null) return false;
            try
            {
                if (launch != null) return launch(uri);
                Process.Start(new ProcessStartInfo(uri) { UseShellExecute = true });
                return true;
            }
            catch (Win32Exception) { return false; }
            catch (InvalidOperationException) { return false; }
        }
    }
}
