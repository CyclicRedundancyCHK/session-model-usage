using System;
using System.Diagnostics;
using System.Drawing;
using System.IO;
using System.Linq;
using System.Windows.Forms;

namespace CodexQuotaTray
{
    internal sealed class TrayApplicationContext : ApplicationContext
    {
        private const int RefreshIntervalMilliseconds = 20000;
        private const int CompletionFlashIntervalMilliseconds = 600;
        private readonly SessionQuotaReader _reader;
        private readonly CodexCompletionMonitor _completionMonitor;
        private readonly CodexTaskMonitor _taskMonitor = new CodexTaskMonitor();
        private TaskStatusSnapshot _taskStatus = new TaskStatusSnapshot();
        private int _taskPollBusy;
        private readonly Timer _taskTimer;
        private readonly NotifyIcon _notifyIcon;
        private readonly QuotaPopupForm _popup;
        private readonly TaskbarWidgetForm _widget;
        private readonly Timer _refreshTimer;
        private readonly Timer _widgetGuardTimer;
        private readonly Timer _notificationTimer;
        private readonly Timer _completionFlashTimer;
        private readonly ToolStripMenuItem _showMenuItem;
        private readonly ToolStripMenuItem _refreshMenuItem;
        private readonly ToolStripMenuItem _exitMenuItem;
        private readonly AppSettings _settings;
        private Icon _currentIcon;
        private Rectangle _lastTrayAnchor;
        private QuotaReadResult _lastResult;
        private bool _lastSystemLightAppearance;
        private string _lastSystemLanguage;
        private bool _updateBusy;
        private bool _completionMonitorEnabled;
        private bool _hasCompletionUnread;
        private bool _completionFlashPhase;
        private int _completionUnreadCount;
        private int _refreshBusy;
        private readonly string _runId;
        private readonly Timer _sessionTimer;
        private readonly ToolStripMenuItem _sessionMenuItem;
        private readonly ToolStripMenuItem _retryMenuItem;
        private ContextMenuStrip _taskMenu;
        private readonly ThemedContextMenu _contextMenu;

        public TrayApplicationContext(string runId)
        {
            _runId = runId;
            _settings = AppSettings.Load();
            _lastSystemLightAppearance = ThemePalette.SystemUsesLightAppearance();
            _lastSystemLanguage = AppSettings.SystemLanguageName();
            _reader = new SessionQuotaReader();
            _completionMonitor = new CodexCompletionMonitor();

            _popup = new QuotaPopupForm();
            _popup.ApplySettings(_settings);
            _popup.RefreshRequested += delegate { RefreshInBackground(); };
            _popup.ExitRequested += delegate { RequestExit(); };
            _popup.SettingsChanged += delegate { ApplySettingsChange(); };
            _popup.UpdateCheckRequested += delegate { CheckForUpdates(); };

            _widget = new TaskbarWidgetForm();
            _popup.VisibleChanged += delegate { UpdateHoverSuppression(); };
            _widget.ApplySettings(_settings);
            _lastTrayAnchor = new Rectangle(Cursor.Position.X - 1, Cursor.Position.Y - 1, 3, 3);
            _widget.QuotaClicked += delegate { AcknowledgeCompletionReminder(); TogglePopup(_widget.GetAnchorBounds()); };
            _widget.TaskStatusClicked += delegate { ShowRecentChats(); };

            var menu = _contextMenu = new ThemedContextMenu(_settings, true);
            menu.VisibleChanged += delegate { UpdateHoverSuppression(); };
            menu.Opening += delegate { if (_taskMenu != null) _taskMenu.Close(); _popup.Hide(); };
            _showMenuItem = new ToolStripMenuItem();
            _showMenuItem.Click += delegate { TogglePopup(_lastTrayAnchor); };
            _refreshMenuItem = new ToolStripMenuItem();
            _refreshMenuItem.Click += delegate { RefreshInBackground(); };
            _exitMenuItem = new ToolStripMenuItem { Tag = "danger" };
            _exitMenuItem.Click += delegate { RequestExit(); };
            _sessionMenuItem = new ToolStripMenuItem();
            _sessionMenuItem.Click += delegate { _popup.ShowCenteredAbove(_lastTrayAnchor); _popup.ShowSession(); };
            _retryMenuItem = new ToolStripMenuItem();
            _retryMenuItem.Click += delegate { CompanionBridge.Command(_runId, "retry"); };
            menu.Items.Add(_showMenuItem);
            menu.Items.Add(_sessionMenuItem);
            menu.Items.Add(_retryMenuItem);
            menu.Items.Add(_refreshMenuItem);
            menu.Items.Add(new ToolStripSeparator());
            menu.Items.Add(_exitMenuItem);
            _widget.ContextMenuStrip = menu;

            var palette = ThemePalette.FromSettings(_settings);
            _currentIcon = RingIconFactory.Create(0, false, palette);
            _notifyIcon = new NotifyIcon
            {
                Icon = _currentIcon,
                Visible = true,
                ContextMenuStrip = menu
            };
            _notifyIcon.MouseClick += NotifyIconOnMouseClick;

            _refreshTimer = new Timer { Interval = RefreshIntervalMilliseconds };
            _refreshTimer.Tick += delegate { RefreshInBackground(); };
            _refreshTimer.Start();

            _notificationTimer = new Timer { Interval = CodexCompletionMonitor.IdleIntervalMilliseconds };
            _notificationTimer.Tick += delegate { PollCompletionNotifications(); };
            _taskTimer = new Timer { Interval = 2000 };
            _taskTimer.Tick += delegate { PollTaskStatus(); };
            _taskTimer.Start();

            _completionFlashTimer = new Timer { Interval = CompletionFlashIntervalMilliseconds };
            _completionFlashTimer.Tick += delegate
            {
                _completionFlashPhase = !_completionFlashPhase;
                ApplyVisualResult(_lastResult);
            };

            _widgetGuardTimer = new Timer { Interval = 1000 };
            _widgetGuardTimer.Tick += delegate
            {
                if (_settings.WidgetVisible) _widget.EnsureOnTop();
                var systemLight = ThemePalette.SystemUsesLightAppearance();
                if (string.Equals(_settings.AppearanceMode, "system", StringComparison.OrdinalIgnoreCase) &&
                    systemLight != _lastSystemLightAppearance)
                {
                    _lastSystemLightAppearance = systemLight;
                    ApplySettingsVisuals();
                }
                var systemLanguage = AppSettings.SystemLanguageName();
                if (string.Equals(_settings.Language, "system", StringComparison.OrdinalIgnoreCase) &&
                    !string.Equals(systemLanguage, _lastSystemLanguage, StringComparison.OrdinalIgnoreCase))
                {
                    _lastSystemLanguage = systemLanguage;
                    ApplySettingsVisuals();
                }
            };
            _widgetGuardTimer.Start();

            UpdateLanguage();
            ApplyCompletionReminderSetting();
            var popupHandle = _popup.Handle;
            var widgetHandle = _widget.Handle;
            _sessionTimer = new Timer { Interval = 1000 };
            _sessionTimer.Tick += delegate
            {
                _popup.ApplySession(CompanionBridge.ReadSession(_runId));
                CompanionBridge.PublishFrontend(_runId, _lastResult, _widget.Visible, _popup.Visible, _taskStatus, _settings.TaskStatusEnabled);
            };
            _sessionTimer.Start();
            RefreshInBackground();
            _widget.SetVisible(_settings.WidgetVisible);
            PollTaskStatus();
        }

        private void PollTaskStatus()
        {
            if (System.Threading.Interlocked.CompareExchange(ref _taskPollBusy, 1, 0) != 0) return;
            var enabled = _settings.TaskStatusEnabled;
            System.Threading.ThreadPool.QueueUserWorkItem(delegate
            {
                TaskStatusSnapshot status;
                try
                {
                    var runtime = CompanionBridge.Read(CompanionPaths.FilePath("runtime.json"));
                    var pid = (int)CompanionBridge.Number(CompanionBridge.Get(runtime, "app_pid"));
                    var started = new DateTimeOffset(1970, 1, 1, 0, 0, 0, TimeSpan.Zero).AddSeconds(CompanionBridge.Number(CompanionBridge.Get(runtime, "app_created")));
                    status = _taskMonitor.Poll(enabled && CompanionBridge.DesktopRunning() == true, pid, started);
                }
                catch { status = new TaskStatusSnapshot(); }
                try
                {
                    if (_widget.IsDisposed || !_widget.IsHandleCreated) return;
                    _widget.BeginInvoke(new Action(delegate
                    {
                        _taskStatus = _settings.TaskStatusEnabled ? _taskMonitor.Snapshot() : new TaskStatusSnapshot();
                        _taskStatus.SourceAvailable = status.SourceAvailable;
                        _widget.ApplyTaskStatus(_taskStatus);
                    }));
                }
                catch (InvalidOperationException) { }
                finally { System.Threading.Interlocked.Exchange(ref _taskPollBusy, 0); }
            });
        }

        private void ApplySettingsChange()
        {
            try { _settings.Save(); }
            catch { }

            _lastSystemLightAppearance = ThemePalette.SystemUsesLightAppearance();
            _lastSystemLanguage = AppSettings.SystemLanguageName();
            ApplySettingsVisuals();
            ApplyCompletionReminderSetting();
            PollTaskStatus();
        }

        private void ApplySettingsVisuals()
        {
            _popup.ApplySettings(_settings);
            _widget.ApplySettings(_settings);
            _widget.SetVisible(_settings.WidgetVisible);
            UpdateLanguage();
            _contextMenu.ApplySettings(_settings);
            if (_taskMenu != null) ((ThemedContextMenu)_taskMenu).ApplySettings(_settings);
            ApplyVisualResult(_lastResult);
        }

        private void UpdateLanguage()
        {
            _showMenuItem.Text = T("账号额度", "Account quota");
            _sessionMenuItem.Text = T("当前会话用量", "Current session usage");
            _retryMenuItem.Text = T("恢复会话连接", "Reconnect session");
            _refreshMenuItem.Text = T("立即刷新", "Refresh now");
            _exitMenuItem.Text = T("退出", "Quit");
            if (_contextMenu != null) _contextMenu.ApplyTheme();
        }

        private void NotifyIconOnMouseClick(object sender, MouseEventArgs e)
        {
            _lastTrayAnchor = TrayIconAnchorResolver.Resolve(Cursor.Position);
            if (e.Button == MouseButtons.Left)
            {
                AcknowledgeCompletionReminder();
                TogglePopup(_lastTrayAnchor);
            }
        }

        private void TogglePopup(Rectangle anchorBounds)
        {
            if (_popup.Visible) _popup.Hide();
            else
            {
                _popup.ShowCenteredAbove(anchorBounds);
                RefreshInBackground();
            }
        }

        private void ShowRecentChats()
        {
            if (_taskMenu != null && _taskMenu.Visible) { _taskMenu.Close(); return; }
            _contextMenu.Close();
            _popup.Hide();
            if (_taskMenu != null) _taskMenu.Dispose();
            _taskMenu = CodexThreadNavigator.RecentMenu(CodexThreadNavigator.ReadRecent(_runId), _settings.IsEnglish, OpenTask, _taskStatus, _settings);
            ((ThemedContextMenu)_taskMenu).WatchOutsideClicks(_widget, () => _widget.TaskStatusBounds);
            _taskMenu.VisibleChanged += delegate { UpdateHoverSuppression(); };
            _taskMenu.Show(_widget, new Point(_widget.TaskStatusBounds.Left, 0), ToolStripDropDownDirection.AboveRight);
        }

        private void UpdateHoverSuppression()
        {
            _widget.SetPopupOpen(_popup.Visible || (_taskMenu != null && _taskMenu.Visible) || (_contextMenu != null && _contextMenu.Visible));
        }

        private void OpenTask(TaskNavigationTarget target)
        {
            if (!CodexThreadNavigator.Open(target))
            {
                MessageBox.Show(T("无法打开 Codex 会话。请检查 Codex 是否已安装后重试。", "Unable to open the Codex chat. Check that Codex is installed and try again."),
                    T("会话跳转", "Open chat"), MessageBoxButtons.OK, MessageBoxIcon.Information);
                return;
            }
            _taskMonitor.Acknowledge(target);
            var sourceAvailable = _taskStatus.SourceAvailable;
            _taskStatus = _taskMonitor.Snapshot();
            _taskStatus.SourceAvailable = sourceAvailable;
            _widget.ApplyTaskStatus(_taskStatus);
        }

        private void RefreshNow()
        {
            ApplyReadResult(_reader.ReadLatest());
        }

        private void RefreshInBackground()
        {
            if (System.Threading.Interlocked.CompareExchange(ref _refreshBusy, 1, 0) != 0) return;
            System.Threading.ThreadPool.QueueUserWorkItem(delegate
            {
                var result = _reader.ReadLatest();
                try
                {
                    if (_widget.IsDisposed || !_widget.IsHandleCreated)
                    {
                        System.Threading.Interlocked.Exchange(ref _refreshBusy, 0);
                        return;
                    }
                    _widget.BeginInvoke(new Action(delegate
                    {
                        System.Threading.Interlocked.Exchange(ref _refreshBusy, 0);
                        ApplyReadResult(result);
                    }));
                }
                catch
                {
                    System.Threading.Interlocked.Exchange(ref _refreshBusy, 0);
                }
            });
        }

        private void ApplyReadResult(QuotaReadResult result)
        {
            _lastResult = result;
            _popup.ApplyResult(_lastResult);
            _widget.ApplyResult(_lastResult);
            _widget.RefreshDynamicAnchor();
            ApplyVisualResult(_lastResult);
        }

        private void ApplyVisualResult(QuotaReadResult result)
        {
            if (result == null) return;
            var palette = ThemePalette.FromSettings(_settings);
            var active = result.IsCodexRunning && result.Snapshot != null;
            var window = result.Snapshot == null
                ? null
                : (!_settings.ShowFiveHourQuota ? result.Snapshot.DisplayWeeklyWindow : result.Snapshot.IsReserveActive
                    ? result.Snapshot.ReserveWindow
                    : result.Snapshot.Windows.OrderBy(item => item.RemainingPercent).FirstOrDefault());
            var remaining = window == null ? 0 : window.RemainingPercent;
            ReplaceIcon(RingIconFactory.Create(remaining, active && window != null, palette, _hasCompletionUnread && _completionFlashPhase));

            if (!result.IsCodexRunning)
                SetTooltip(T("Codex 未运行 · 不会自动启动 Codex", "Codex is not running · It will not be started"));
            else if (result.Snapshot == null)
                SetTooltip(T("Codex 已运行 · 等待额度快照", "Codex is running · Waiting for quota data"));
            else
            {
                var fiveHour = result.Snapshot.FiveHourWindow;
                var weekly = result.Snapshot.DisplayWeeklyWindow;
                var tooltip = _settings.ShowFiveHourQuota ? (fiveHour == null ? "5H --" : "5H " + fiveHour.RemainingPercent + "%") : "";
                if (weekly != null && weekly != fiveHour || !_settings.ShowFiveHourQuota)
                    tooltip += (tooltip.Length == 0 ? "" : " · ") + (result.Snapshot.IsReserveActive ? "Reserve " : "7D ") +
                        (weekly == null ? "--" : weekly.RemainingPercent + "%");
                if (_hasCompletionUnread) tooltip += T(" · 新任务 ", " · New ") + _completionUnreadCount;
                SetTooltip(tooltip);
            }
        }

        private void ApplyCompletionReminderSetting()
        {
            if (_settings.CompletionReminderEnabled == _completionMonitorEnabled) return;
            _completionMonitorEnabled = _settings.CompletionReminderEnabled;
            if (_completionMonitorEnabled)
            {
                _completionMonitor.Enable();
                _notificationTimer.Interval = CodexCompletionMonitor.IdleIntervalMilliseconds;
                _notificationTimer.Start();
            }
            else
            {
                _notificationTimer.Stop();
                _completionMonitor.Disable();
                StopCompletionReminder();
            }
        }

        private void PollCompletionNotifications()
        {
            if (!_completionMonitorEnabled) return;
            var result = _completionMonitor.Poll();
            _completionUnreadCount = result.UnreadCount;
            if (result.HasUnread)
            {
                _hasCompletionUnread = true;
                _notificationTimer.Interval = CodexCompletionMonitor.PollInterval(true);
                if (!_completionFlashTimer.Enabled)
                {
                    _completionFlashPhase = true;
                    _completionFlashTimer.Start();
                }
            }
            else
            {
                StopCompletionReminder();
                _notificationTimer.Interval = CodexCompletionMonitor.PollInterval(false);
            }
            if (result.StateChanged) ApplyVisualResult(_lastResult);
        }

        private void AcknowledgeCompletionReminder()
        {
            // Quota/tray access only silences the optional flash. Task reminders
            // are acknowledged individually when their confirmed chat is opened.
            if (!_hasCompletionUnread) return;
            _completionMonitor.AcknowledgeAll();
            StopCompletionReminder();
            _notificationTimer.Interval = CodexCompletionMonitor.PollInterval(false);
            ApplyVisualResult(_lastResult);
        }

        private void StopCompletionReminder()
        {
            _hasCompletionUnread = false;
            _completionUnreadCount = 0;
            _completionFlashPhase = false;
            _completionFlashTimer.Stop();
        }

        private void RequestExit()
        {
            try { CompanionBridge.Command(_runId, "stop"); } catch { }
            ExitThread();
        }

        private void CheckForUpdates()
        {
            if (_updateBusy) return;
            _updateBusy = true;
            _popup.SetUpdateStatus("正在检查整合版更新…", "Checking combined app updates…", true);
            System.Threading.ThreadPool.QueueUserWorkItem(delegate
            {
                var result = CombinedUpdateService.Check();
                InvokePopup(delegate
                {
                    _updateBusy = false;
                    _popup.SetUpdateStatus(result.Chinese, result.English, false);
                    if (result.Newer && MessageBox.Show(T("发现整合版更新，打开发布页下载安装包？", "A combined app update is available. Open the release page?"),
                        T("整合版更新", "Combined app update"), MessageBoxButtons.YesNo) == DialogResult.Yes)
                        Process.Start(new ProcessStartInfo(result.Url) { UseShellExecute = true });
                });
            });
        }

        private void InvokePopup(Action action)
        {
            try
            {
                if (!_popup.IsDisposed && _popup.IsHandleCreated) _popup.BeginInvoke(action);
            }
            catch
            {
            }
        }

        private string T(string chinese, string english) { return _settings.IsEnglish ? english : chinese; }

        private void ReplaceIcon(Icon icon)
        {
            var previous = _currentIcon;
            _currentIcon = icon;
            _notifyIcon.Icon = icon;
            if (previous != null) previous.Dispose();
        }

        private void SetTooltip(string value)
        {
            _notifyIcon.Text = value.Length <= 63 ? value : value.Substring(0, 63);
        }

        protected override void ExitThreadCore()
        {
            if (_taskMenu != null) _taskMenu.Dispose();
            _sessionTimer.Stop();
            _sessionTimer.Dispose();
            _taskTimer.Stop();
            _taskTimer.Dispose();
            _refreshTimer.Stop();
            _refreshTimer.Dispose();
            _widgetGuardTimer.Stop();
            _widgetGuardTimer.Dispose();
            _notificationTimer.Stop();
            _notificationTimer.Dispose();
            _completionFlashTimer.Stop();
            _completionFlashTimer.Dispose();
            _notifyIcon.Visible = false;
            _notifyIcon.Dispose();
            _contextMenu.Dispose();
            _popup.Dispose();
            _widget.Dispose();
            if (_currentIcon != null) _currentIcon.Dispose();
            base.ExitThreadCore();
        }
    }
}
