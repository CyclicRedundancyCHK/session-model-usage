using System;
using System.Collections.Generic;
using System.Globalization;
using System.IO;
using System.Linq;
using System.Text;
using System.Text.RegularExpressions;

namespace CodexQuotaTray
{
    internal enum CodexTaskState { None, Finish, Working, Ask, Err }

    internal sealed class TaskNavigationTarget
    {
        public string ThreadId;
        public CodexTaskState State;
        public DateTimeOffset Updated;
    }

    internal sealed class TaskStatusSnapshot
    {
        public CodexTaskState State;
        public int WorkingCount, AskCount, ErrorCount, FinishCount;
        public bool SourceAvailable;
        public readonly List<TaskNavigationTarget> Targets = new List<TaskNavigationTarget>();
        public string Label { get { return State == CodexTaskState.None ? "Codex" : State.ToString(); } }
    }

    // Observe lifecycle metadata only. Tool failures, reasoning text and UI errors are not task failures.
    internal sealed class CodexTaskMonitor
    {
        private sealed class TaskEntry
        {
            public CodexTaskState State;
            public DateTimeOffset Updated;
            public string Turn;
            public bool Running;
            public bool Unread;
            public bool Completed;
            public string LastTerminalTurn;
            public readonly HashSet<string> Questions = new HashSet<string>();
        }
        private sealed class ObservedLine
        {
            public string SessionId, Line;
            public DateTimeOffset Stamp;
        }
        private readonly List<ObservedLine> _observed = new List<ObservedLine>();
        private const int TailLimit = 4 * 1024 * 1024;
        private readonly string _logsRoot, _sessionsRoot;
        private readonly Dictionary<string, long> _positions = new Dictionary<string, long>();
        private readonly Dictionary<string, string> _sessionIds = new Dictionary<string, string>();
        private readonly Dictionary<string, TaskEntry> _tasks = new Dictionary<string, TaskEntry>();
        private int _hostPid;
        private bool _bootstrapping;
        private DateTimeOffset _hostStarted;
        private DateTimeOffset _startedObserving;
        private DateTimeOffset _nextCatalog;
        private List<string> _sessionFiles = new List<string>();
        private readonly Dictionary<string, string> _localPaths = new Dictionary<string, string>(StringComparer.OrdinalIgnoreCase);

        internal CodexTaskMonitor() : this(ResolveLogsRoot(), Path.Combine(
            Environment.GetEnvironmentVariable("CODEX_HOME") ?? Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.UserProfile), ".codex"), "sessions")) { }
        internal CodexTaskMonitor(string logsRoot, string sessionsRoot) { _logsRoot = logsRoot; _sessionsRoot = sessionsRoot; }

        internal TaskStatusSnapshot Poll(bool running, int hostPid, DateTimeOffset hostStarted)
        {
            lock (this)
            {
                if (!running || hostPid != _hostPid)
                {
                    _tasks.Clear(); _positions.Clear(); _sessionIds.Clear(); _sessionFiles.Clear();
                    _localPaths.Clear();
                    _observed.Clear();
                    _nextCatalog = DateTimeOffset.MinValue; _bootstrapping = true;
                    _hostPid = running ? hostPid : 0; _hostStarted = hostStarted;
                    _startedObserving = DateTimeOffset.UtcNow;
                }
                if (!running) return new TaskStatusSnapshot();
                var available = false;
                try
                {
                    if (!string.IsNullOrEmpty(_sessionsRoot) && Directory.Exists(_sessionsRoot) && DateTimeOffset.UtcNow >= _nextCatalog)
                    {
                        var files = Directory.EnumerateFiles(_sessionsRoot, "*.jsonl", SearchOption.AllDirectories).Select(p => new FileInfo(p)).ToList();
                        _localPaths.Clear();
                        foreach (var file in files)
                        {
                            var name = Path.GetFileNameWithoutExtension(file.Name);
                            if (name.Length < 36) continue;
                            var candidate = name.Substring(name.Length - 36); Guid parsed;
                            if (Guid.TryParse(candidate, out parsed)) _localPaths[candidate] = file.FullName;
                        }
                        _sessionFiles = files.Where(f => f.LastWriteTimeUtc >= DateTime.UtcNow.AddDays(-2))
                            .OrderByDescending(f => f.LastWriteTimeUtc).Take(32).Select(f => f.FullName).ToList();
                        _nextCatalog = DateTimeOffset.UtcNow.AddSeconds(10);
                    }
                    if (!string.IsNullOrEmpty(_logsRoot) && Directory.Exists(_logsRoot))
                    {
                        var files = Directory.EnumerateFiles(_logsRoot, "*.log", SearchOption.AllDirectories)
                            .Where(p => Path.GetFileName(p).Contains("-" + hostPid + "-"))
                            .Select(p => new FileInfo(p)).OrderByDescending(f => f.LastWriteTimeUtc).Take(12)
                            .OrderBy(f => f.LastWriteTimeUtc).ToList();
                        foreach (var file in files) available |= ReadFile(file.FullName, null);
                    }
                    if (!string.IsNullOrEmpty(_sessionsRoot) && Directory.Exists(_sessionsRoot))
                    {
                        var activePaths = _tasks.Where(p => p.Value.State == CodexTaskState.Working || p.Value.State == CodexTaskState.Ask)
                            .Select(p => p.Key).Concat(_observed.Where(p => p.SessionId == null).Select(p => Value(p.Line, "conversationId=")))
                            .Where(id => id != null && _localPaths.ContainsKey(id)).Select(id => _localPaths[id]);
                        foreach (var path in _sessionFiles.Concat(activePaths).Distinct().ToList())
                        {
                            string id;
                            if (!_sessionIds.TryGetValue(path, out id))
                            {
                                id = ReadSessionId(path);
                                if (id != null) _sessionIds[path] = id;
                            }
                            if (id != null) available |= ReadFile(path, id);
                        }
                    }
                }
                catch (IOException) { }
                catch (UnauthorizedAccessException) { }
                // Merge sources by event time: a later desktop completion must not hide an earlier user reply.
                foreach (var row in _observed.OrderBy(p => p.Stamp))
                    if (row.SessionId == null) ProcessDesktopLine(row.Line); else ProcessSessionLine(row.SessionId, row.Line);
                _observed.Clear();
                _bootstrapping = false;
                // Bound old idle entries; live work and pending questions remain until a lifecycle transition.
                foreach (var id in _tasks.Where(p => p.Value.State == CodexTaskState.None && p.Value.Updated < DateTimeOffset.UtcNow.AddDays(-1)).Select(p => p.Key).ToList()) _tasks.Remove(id);
                var snapshot = Snapshot(); snapshot.SourceAvailable = available; return snapshot;
            }
        }

        internal void Acknowledge()
        {
            lock (this)
                foreach (var task in _tasks.Values)
                    if (task.State == CodexTaskState.Finish || task.State == CodexTaskState.Err)
                    { task.Unread = false; task.State = task.Running ? CodexTaskState.Working : CodexTaskState.None; }
        }

        internal void Acknowledge(TaskNavigationTarget target)
        {
            if (target == null) return;
            lock (this)
            {
                TaskEntry task;
                // Do not consume a newer completion while an older selection is open.
                if (_tasks.TryGetValue(target.ThreadId, out task) && task.State == target.State && task.Updated == target.Updated &&
                    (task.State == CodexTaskState.Finish || task.State == CodexTaskState.Err))
                { task.Unread = false; task.State = task.Running ? CodexTaskState.Working : CodexTaskState.None; }
            }
        }

        internal void ReconcileRecent(RecentThreadSnapshot recent)
        {
            if (recent == null || !recent.Available) return;
            var age = DateTimeOffset.UtcNow - recent.CapturedAt;
            if (age < TimeSpan.Zero || age >= TimeSpan.FromSeconds(30)) return;
            var visible = new HashSet<string>(recent.Targets.Select(t => t.ThreadId), StringComparer.OrdinalIgnoreCase);
            lock (this)
                foreach (var pair in _tasks)
                    if (pair.Value.State == CodexTaskState.Finish && pair.Value.Unread &&
                        pair.Value.Updated <= recent.CapturedAt && !visible.Contains(pair.Key))
                    {
                        pair.Value.Unread = false;
                        pair.Value.State = CodexTaskState.None;
                    }
        }

        internal TaskStatusSnapshot Snapshot()
        {
            lock (this)
            {
            var result = new TaskStatusSnapshot();
            foreach (var task in _tasks.Values)
                switch (task.State)
                {
                    case CodexTaskState.Working: result.WorkingCount++; break;
                    case CodexTaskState.Ask: result.AskCount++; break;
                    case CodexTaskState.Err: if (task.Unread) result.ErrorCount++; break;
                    case CodexTaskState.Finish: if (task.Unread) result.FinishCount++; break;
                }
            result.State = result.ErrorCount > 0 ? CodexTaskState.Err : result.AskCount > 0 ? CodexTaskState.Ask :
                result.FinishCount > 0 ? CodexTaskState.Finish : result.WorkingCount > 0 ? CodexTaskState.Working : CodexTaskState.None;
            result.Targets.AddRange(_tasks.Where(p => p.Value.State != CodexTaskState.None &&
                (p.Value.State == CodexTaskState.Working || p.Value.State == CodexTaskState.Ask || p.Value.Unread))
                .OrderBy(p => Priority(p.Value.State)).ThenByDescending(p => p.Value.Updated).ThenBy(p => p.Key, StringComparer.Ordinal)
                .Select(p => new TaskNavigationTarget { ThreadId = p.Key, State = p.Value.State, Updated = p.Value.Updated }));
            return result;
            }
        }

        internal static int Priority(CodexTaskState state)
        {
            switch (state)
            {
                case CodexTaskState.Err: return 0;
                case CodexTaskState.Ask: return 1;
                case CodexTaskState.Finish: return 2;
                case CodexTaskState.Working: return 4;
                default: return int.MaxValue;
            }
        }

        internal void ProcessDesktopLine(string line)
        {
            DateTimeOffset stamp;
            var space = line == null ? -1 : line.IndexOf(' ');
            if (space < 0 || !DateTimeOffset.TryParse(line.Substring(0, space), CultureInfo.InvariantCulture, DateTimeStyles.AssumeUniversal, out stamp)) return;
            var id = Value(line, "conversationId=");
            if (id == null) return;
            // Side generations also emit turn/start. Only explicitly confirmed local sessions are user work.
            if (_sessionsRoot != null)
            {
                string path, confirmed;
                if (!_localPaths.TryGetValue(id, out path)) return;
                if (!_sessionIds.TryGetValue(path, out confirmed))
                { confirmed = ReadSessionId(path); if (confirmed != null) _sessionIds[path] = confirmed; }
                if (!string.Equals(id, confirmed, StringComparison.OrdinalIgnoreCase)) return;
            }
            if (line.Contains("[desktop-notifications] received question") || line.Contains("[desktop-notifications] received approval") ||
                line.Contains("[desktop-notifications] suppressed approval") || line.Contains("[desktop-notifications] show permission") ||
                (line.Contains("[desktop-notifications] show notification") && Value(line, "kind=") == "permission"))
                Update(id, CodexTaskState.Ask, stamp, null, Value(line, "requestId="));
            else if (line.Contains("[desktop-notifications] received turn-complete") || line.Contains("[desktop-notifications] show turn-complete"))
                Update(id, CodexTaskState.Finish, stamp, Value(line, "turnId="), null);
            else if (line.Contains("[AppServerConnection] response_routed") && Value(line, "method=") == "turn/start")
            {
                var error = Value(line, "errorCode=");
                Update(id, error == null || error == "null" ? CodexTaskState.Working : CodexTaskState.Err, stamp, null, null);
            }
            else if (line.Contains("Received turn/started") || line.Contains("Reasoning summary turn-start config resolved"))
                Update(id, CodexTaskState.Working, stamp, null, null);
            else if (line.Contains("thread_stream_view_activity_changed") && Value(line, "active=") == "true" && Value(line, "resumeState=") == "resumed")
            {
                TaskEntry task;
                if (_tasks.TryGetValue(id, out task) && task.State == CodexTaskState.Finish && stamp >= task.Updated)
                { task.State = CodexTaskState.None; task.Unread = false; }
            }
        }

        internal void ProcessSessionLine(string id, string line)
        {
            // Avoid deserializing tool output and conversation bodies unless their envelope is relevant.
            if (line.IndexOf("\"event_msg\"", StringComparison.Ordinal) < 0 && line.IndexOf("\"turn_context\"", StringComparison.Ordinal) < 0 &&
                line.IndexOf("\"function_call\"", StringComparison.Ordinal) < 0 && line.IndexOf("\"function_call_output\"", StringComparison.Ordinal) < 0 &&
                !(line.IndexOf("\"message\"", StringComparison.Ordinal) >= 0 && Regex.IsMatch(line, "\"role\"\\s*:\\s*\"user\""))) return;
            var row = CompanionBridge.Parse(line); var payload = CompanionBridge.Dictionary(CompanionBridge.Get(row, "payload"));
            DateTimeOffset stamp;
            if (payload == null || !DateTimeOffset.TryParse(CompanionBridge.Text(CompanionBridge.Get(row, "timestamp")), out stamp)) return;
            var type = CompanionBridge.Text(CompanionBridge.Get(payload, "type"));
            var turn = CompanionBridge.Text(CompanionBridge.Get(payload, "turn_id"));
            if (CompanionBridge.Text(CompanionBridge.Get(row, "type")) == "turn_context" || type == "task_started")
                Update(id, CodexTaskState.Working, stamp, turn, null);
            else if (type == "task_complete" || type == "task_completed")
                Update(id, CompanionBridge.Text(CompanionBridge.Get(payload, "status")) == "failed" ? CodexTaskState.Err : CodexTaskState.Finish, stamp, turn, null);
            else if (type == "turn_aborted") Update(id, CodexTaskState.None, stamp, turn, null);
            else if (type == "turn_failed" || type == "task_failed" || (type == "error" &&
                (object.Equals(CompanionBridge.Get(payload, "will_retry"), false) || object.Equals(CompanionBridge.Get(payload, "willRetry"), false))))
                Update(id, CodexTaskState.Err, stamp, turn, null);
            else if (type == "exec_approval_request" || type == "apply_patch_approval_request" || type == "request_user_input" || type == "mcp_elicitation_request")
                Update(id, CodexTaskState.Ask, stamp, turn, CompanionBridge.Text(CompanionBridge.Get(payload, "call_id")));
            else if (type == "function_call")
            {
                var name = CompanionBridge.Text(CompanionBridge.Get(payload, "name")) ?? "";
                if (name.EndsWith("request_user_input", StringComparison.Ordinal) || name.EndsWith("request_user_input_async", StringComparison.Ordinal))
                    Update(id, CodexTaskState.Ask, stamp, null, CompanionBridge.Text(CompanionBridge.Get(payload, "call_id")));
            }
            else if (type == "message" && CompanionBridge.Text(CompanionBridge.Get(payload, "role")) == "user") Resume(id, stamp, null);
            else if (type == "function_call_output")
            {
                var output = CompanionBridge.Parse(CompanionBridge.Text(CompanionBridge.Get(payload, "output")) ?? "");
                var answers = CompanionBridge.Get(output, "answers");
                if (answers is Dictionary<string, object> || answers is object[])
                    Resume(id, stamp, CompanionBridge.Text(CompanionBridge.Get(payload, "call_id")));
            }
        }

        private void Resume(string id, DateTimeOffset stamp, string question)
        {
            TaskEntry task;
            if (_tasks.TryGetValue(id, out task) && task.State == CodexTaskState.Ask && stamp >= task.Updated &&
                (question == null || task.Questions.Contains(question)))
            {
                if (question == null) task.Questions.Clear(); else task.Questions.Remove(question);
                task.Updated = stamp;
                if (task.Questions.Count == 0) task.State = task.Running ? CodexTaskState.Working :
                    task.Completed && task.Unread ? CodexTaskState.Finish : CodexTaskState.None;
            }
        }

        private void Update(string id, CodexTaskState state, DateTimeOffset stamp, string turn, string question)
        {
            if (string.IsNullOrEmpty(id) || id.Length > 160 || stamp < _hostStarted || stamp > DateTimeOffset.UtcNow.AddMinutes(2)) return;
            TaskEntry task;
            if (!_tasks.TryGetValue(id, out task)) { task = new TaskEntry(); _tasks[id] = task; }
            if (stamp < task.Updated) return;
            if (state != CodexTaskState.Working && state != CodexTaskState.Ask && turn != null && task.Turn != null && task.Running && turn != task.Turn) return;
            if (state == CodexTaskState.Working)
            {
                // Metadata for the same running turn must not consume a pending question.
                if (task.State == CodexTaskState.Ask && (turn == null || turn == task.Turn)) return;
                task.Running = true; task.Questions.Clear(); task.Unread = false; task.Completed = false;
                if (turn == null) task.Turn = null;
            }
            else if (state == CodexTaskState.Ask)
            { if (question != null) task.Questions.Add(question); }
            else
            {
                if (state == CodexTaskState.Finish && turn != null && turn == task.LastTerminalTurn) return;
                var changed = task.State != state || (turn != null && turn != task.Turn) || (state == CodexTaskState.Finish && !task.Completed);
                task.Running = false;
                // A historical question call is not evidence of a currently pending request.
                // Completion, cancellation and failure all close questions from this turn.
                task.Questions.Clear();
                task.Completed = state == CodexTaskState.Finish;
                if (changed) task.Unread = !_bootstrapping && stamp >= _startedObserving;
                if (turn != null) task.LastTerminalTurn = turn;
            }
            task.State = state; task.Updated = stamp; if (turn != null) task.Turn = turn;
        }

        private bool ReadFile(string path, string sessionId)
        {
            try
            {
                using (var stream = new FileStream(path, FileMode.Open, FileAccess.Read, FileShare.ReadWrite | FileShare.Delete))
                {
                    long position; var known = _positions.TryGetValue(path, out position);
                    if (known && position == stream.Length) return true;
                    if (known && position > stream.Length && sessionId != null) _tasks.Remove(sessionId);
                    if (!known || position > stream.Length) position = Math.Max(0, stream.Length - TailLimit);
                    if (stream.Length - position > TailLimit) position = stream.Length - TailLimit;
                    var skipped = position > 0 && (!known || !_positions.ContainsKey(path) || stream.Length - position >= TailLimit);
                    stream.Seek(position, SeekOrigin.Begin);
                    var data = new byte[(int)Math.Min(TailLimit, stream.Length - position)];
                    var read = 0; int count; while (read < data.Length && (count = stream.Read(data, read, data.Length - read)) > 0) read += count;
                    if (read == 0) return true;
                    var last = Array.LastIndexOf(data, (byte)10, read - 1, read);
                    if (last < 0) return true; // Keep a partial final line for the next poll.
                    var text = Encoding.UTF8.GetString(data, 0, last + 1); var lines = text.Split('\n');
                    for (var i = skipped ? 1 : 0; i < lines.Length; i++)
                        if (lines[i].Length <= 2 * 1024 * 1024)
                            QueueLine(sessionId, lines[i].TrimStart('\uFEFF'));
                    _positions[path] = position + last + 1; return true;
                }
            }
            catch (IOException) { return false; }
            catch (UnauthorizedAccessException) { return false; }
        }

        private void QueueLine(string sessionId, string line)
        {
            DateTimeOffset stamp;
            if (sessionId == null)
            {
                if (!line.Contains("conversationId=") || !(line.Contains("[desktop-notifications]") || line.Contains("response_routed") ||
                    line.Contains("turn/started") || line.Contains("turn-start config resolved") || line.Contains("thread_stream_view_activity_changed"))) return;
                var space = line.IndexOf(' ');
                if (space < 0 || !DateTimeOffset.TryParse(line.Substring(0, space), out stamp)) return;
            }
            else
            {
                if (!(line.Contains("\"task_started\"") || line.Contains("\"turn_context\"") || line.Contains("\"task_complete\"") ||
                    line.Contains("\"task_completed\"") || line.Contains("\"turn_aborted\"") || line.Contains("\"turn_failed\"") ||
                    line.Contains("\"task_failed\"") || line.Contains("\"error\"") || line.Contains("\"function_call\"") ||
                    line.Contains("\"function_call_output\"") || line.Contains("\"request_user_input\"") || line.Contains("\"exec_approval_request\"") ||
                    line.Contains("\"apply_patch_approval_request\"") || line.Contains("\"mcp_elicitation_request\"") ||
                    Regex.IsMatch(line, "\"role\"\\s*:\\s*\"user\""))) return;
                var match = Regex.Match(line, "\"timestamp\"\\s*:\\s*\"([^\"]{1,48})\"");
                if (!match.Success || !DateTimeOffset.TryParse(match.Groups[1].Value, out stamp)) return;
            }
            _observed.Add(new ObservedLine { SessionId = sessionId, Line = line, Stamp = stamp });
        }

        private static string ReadSessionId(string path)
        {
            try
            {
                using (var stream = new FileStream(path, FileMode.Open, FileAccess.Read, FileShare.ReadWrite | FileShare.Delete))
                {
                    var buffer = new byte[Math.Min(65536, (int)Math.Min(stream.Length, 65536))]; var length = stream.Read(buffer, 0, buffer.Length);
                    var newline = Array.IndexOf(buffer, (byte)10, 0, length); if (newline < 0) return null;
                    var header = CompanionBridge.Parse(Encoding.UTF8.GetString(buffer, 0, newline).TrimStart('\uFEFF'));
                    if (CompanionBridge.Text(CompanionBridge.Get(header, "type")) != "session_meta") return null;
                    return CompanionBridge.Text(CompanionBridge.Get(CompanionBridge.Dictionary(CompanionBridge.Get(header, "payload")), "id"));
                }
            }
            catch (IOException) { return null; }
            catch (UnauthorizedAccessException) { return null; }
        }
        private static string Value(string line, string key)
        { var at = line.IndexOf(key, StringComparison.Ordinal); if (at < 0) return null; at += key.Length; var end = line.IndexOf(' ', at); return (end < 0 ? line.Substring(at) : line.Substring(at, end - at)).Trim(); }
        private static string ResolveLogsRoot()
        {
            var packages = Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData), "Packages");
            try { var package = Directory.EnumerateDirectories(packages, "OpenAI.Codex_*").FirstOrDefault(); return package == null ? null : Path.Combine(package, "LocalCache", "Local", "Codex", "Logs"); }
            catch (IOException) { return null; } catch (UnauthorizedAccessException) { return null; }
        }
    }
}
