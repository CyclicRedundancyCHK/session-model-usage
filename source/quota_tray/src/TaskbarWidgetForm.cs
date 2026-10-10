using System;
using System.Drawing;
using System.Drawing.Drawing2D;
using System.Globalization;
using System.Runtime.InteropServices;
using System.Threading;
using System.Windows.Forms;
using System.Windows.Automation;

namespace CodexQuotaTray
{
    internal sealed class TaskbarWidgetForm : Form
    {
        private const int SingleBaseWidth = 100;
        private const int DualBaseWidth = 160;
        private const int BaseHeight = 38;
        private const int StatusWidth = 92;
        private TaskStatusSnapshot _taskStatus = new TaskStatusSnapshot();
        internal event EventHandler QuotaClicked;
        internal event EventHandler TaskStatusClicked;
        private readonly System.Windows.Forms.Timer _pulseTimer = new System.Windows.Forms.Timer { Interval = 60 };
        private readonly System.Diagnostics.Stopwatch _pulseClock = System.Diagnostics.Stopwatch.StartNew();
        private readonly System.Windows.Forms.Timer _countdownTimer = new System.Windows.Forms.Timer { Interval = 15000 };

        [DllImport("user32.dll")]
        private static extern bool SystemParametersInfo(uint action, uint parameter, out bool value, uint flags);

        [StructLayout(LayoutKind.Sequential)]
        private struct NativeRect
        {
            public int Left;
            public int Top;
            public int Right;
            public int Bottom;
        }

        [DllImport("user32.dll", CharSet = CharSet.Auto)]
        private static extern IntPtr FindWindow(string className, string windowName);

        [DllImport("user32.dll", CharSet = CharSet.Auto)]
        private static extern IntPtr FindWindowEx(IntPtr parent, IntPtr childAfter, string className, string windowName);

        [DllImport("user32.dll")]
        private static extern bool GetWindowRect(IntPtr window, out NativeRect bounds);

        [DllImport("user32.dll")]
        private static extern IntPtr GetWindow(IntPtr window, uint command);

        [DllImport("user32.dll", SetLastError = true)]
        private static extern int SetWindowLong(IntPtr window, int index, int value);

        [DllImport("user32.dll", SetLastError = true)]
        private static extern int GetWindowLong(IntPtr window, int index);

        [DllImport("user32.dll", EntryPoint = "SetWindowLongPtr", SetLastError = true)]
        private static extern IntPtr SetWindowLongPtr64(IntPtr window, int index, IntPtr value);

        [DllImport("user32.dll")]
        private static extern bool SetWindowPos(
            IntPtr window,
            IntPtr insertAfter,
            int x,
            int y,
            int width,
            int height,
            uint flags);

        private int _fiveHourRemainingPercent;
        private int _fiveHourTimeRemainingPercent = -1;
        private int _weeklyRemainingPercent;
        private DateTimeOffset? _fiveHourResetsAt;
        private bool _hasWeeklyWindow;
        private bool _hasPrimaryWindow;
        private bool _isReserve;
        private string _primaryLabel = "5h";
        private DateTimeOffset? _weeklyResetsAt;
        private readonly QuotaHoverCard _quotaTip = new QuotaHoverCard();
        private readonly System.Windows.Forms.Timer _hoverTimer = new System.Windows.Forms.Timer { Interval = 450 };
        private bool _popupOpen;
        private bool _active;
        private float _dpiScale = 1f;
        private AppSettings _settings = new AppSettings();
        private ThemePalette _palette;
        private IntPtr _taskbarHandle;
        private bool _hovered;
        private NativeRect _refreshedTaskButtonBounds;
        private NativeRect _refreshedNotificationBounds;
        private IntPtr _refreshedTaskbarHandle;
        private bool _hasRefreshedTaskButtonBounds;
        private bool _hasRefreshedNotificationBounds;
        private int _dynamicAnchorRefreshRunning;

        public TaskbarWidgetForm()
        {
            AutoScaleMode = AutoScaleMode.None;
            _palette = ThemePalette.FromSettings(_settings);
            ClientSize = new Size(SingleBaseWidth, BaseHeight);
            FormBorderStyle = FormBorderStyle.None;
            ShowInTaskbar = false;
            StartPosition = FormStartPosition.Manual;
            TopMost = false;
            DoubleBuffered = true;
            BackColor = _palette.Background;
            Cursor = Cursors.Hand;
            _pulseTimer.Tick += delegate { Invalidate(); };
            _countdownTimer.Tick += delegate { UpdateQuotaDescription(); Invalidate(); };
            _hoverTimer.Tick += delegate
            {
                _hoverTimer.Stop();
                if (_hovered && Visible && !_popupOpen)
                {
                    UpdateQuotaDescription();
                    _quotaTip.Apply(AccessibleDescription, _settings, _dpiScale);
                    _quotaTip.ShowAbove(this);
                }
            };
            SetRoundedRegion();
        }

        protected override bool ShowWithoutActivation
        {
            get { return true; }
        }

        protected override CreateParams CreateParams
        {
            get
            {
                const int WsExToolWindow = 0x00000080;
                const int WsExNoActivate = 0x08000000;
                var parameters = base.CreateParams;
                parameters.ExStyle |= WsExToolWindow | WsExNoActivate;
                return parameters;
            }
        }

        public void ApplyResult(QuotaReadResult result)
        {
            SetQuotaValues(result);
            UpdateCountdown();
            UpdateQuotaDescription();
            ApplyScaledSize();
            Reposition();
            Invalidate();
        }

        private void SetQuotaValues(QuotaReadResult result)
        {
            _active = result != null && result.IsCodexRunning && result.Snapshot != null;
            var primary = result == null || result.Snapshot == null ? null : result.Snapshot.FiveHourWindow;
            var weekly = result == null || result.Snapshot == null ? null : result.Snapshot.DisplayWeeklyWindow;
            _fiveHourRemainingPercent = primary == null ? 0 : primary.RemainingPercent;
            _fiveHourTimeRemainingPercent = primary == null ? -1 : primary.TimeRemainingPercent;
            _weeklyRemainingPercent = weekly == null ? 0 : weekly.RemainingPercent;
            _fiveHourResetsAt = primary == null ? null : primary.ResetsAt;
            _hasWeeklyWindow = weekly != null && weekly != primary;
            _hasPrimaryWindow = primary != null;
            _weeklyResetsAt = weekly == null ? null : weekly.ResetsAt;
            _isReserve = weekly != null && weekly.Key != null && weekly.Key.StartsWith("gpt-reserve:", StringComparison.OrdinalIgnoreCase);
            _primaryLabel = primary != null && (primary.Kind == QuotaWindowKind.Weekly || primary.WindowMinutes == 10080) ? "W" :
                primary != null && primary.WindowMinutes >= 1440 ? (primary.WindowMinutes / 1440d).ToString("0.#", CultureInfo.InvariantCulture) + "d" :
                primary != null && primary.WindowMinutes > 0 && primary.WindowMinutes != 300
                    ? (primary.WindowMinutes / 60d).ToString("0.#", CultureInfo.InvariantCulture) + "h" : "5h";
        }

        public void ApplySettings(AppSettings settings)
        {
            _settings = settings ?? new AppSettings();
            _palette = ThemePalette.FromSettings(_settings);
            BackColor = _palette.Background;
            UpdatePulse();
            UpdateCountdown();
            UpdateQuotaDescription();
            ApplyScaledSize();
            Reposition();
            Invalidate();
        }

        public void SetVisible(bool visible)
        {
            if (visible)
            {
                EnsureTaskbarAttachment();
                if (!Visible) Show();
                EnsureOnTop();
            }
            else
            {
                Hide();
            }
            UpdatePulse();
            UpdateCountdown();
        }

        private void UpdateCountdown()
        {
            _countdownTimer.Enabled = Visible && _active &&
                ((_settings.ShowFiveHourQuota && _hasPrimaryWindow && _fiveHourResetsAt.HasValue) ||
                 (_hasWeeklyWindow && _weeklyResetsAt.HasValue));
        }

        internal void ApplyTaskStatus(TaskStatusSnapshot status)
        {
            _taskStatus = status ?? new TaskStatusSnapshot();
            UpdatePulse(); UpdateQuotaDescription(); Invalidate();
        }

        private void UpdatePulse()
        {
            bool animated;
            var enabled = !SystemParametersInfo(0x1042, 0, out animated, 0) || animated;
            _pulseTimer.Enabled = Visible && _settings.TaskStatusEnabled && _taskStatus.State == CodexTaskState.Working && enabled;
        }

        public void EnsureOnTop()
        {
            if (!EnsureTaskbarAttachment()) return;
            if (!Visible) Show();
            Reposition(true);
        }

        public void RefreshDynamicAnchor()
        {
            if (Interlocked.Exchange(ref _dynamicAnchorRefreshRunning, 1) != 0) return;
            var thread = new Thread(new ThreadStart(delegate
            {
                try
                {
                    var taskbar = FindWindow("Shell_TrayWnd", null);
                    NativeRect taskbarBounds;
                    if (taskbar == IntPtr.Zero || !GetWindowRect(taskbar, out taskbarBounds)) return;
                    var horizontal = taskbarBounds.Right - taskbarBounds.Left >= taskbarBounds.Bottom - taskbarBounds.Top;
                    NativeRect refreshedTaskButtons;
                    NativeRect refreshedNotifications;
                    var hasTaskButtons = TryGetTaskButtonBounds(taskbar, taskbarBounds, horizontal, out refreshedTaskButtons);
                    var hasNotifications = TryGetVisibleNotificationBounds(taskbar, taskbarBounds, horizontal, out refreshedNotifications);
                    if (!hasTaskButtons && !hasNotifications) return;
                    if (IsDisposed || !IsHandleCreated) return;
                    BeginInvoke(new Action(delegate
                    {
                        _refreshedTaskbarHandle = taskbar;
                        if (hasTaskButtons)
                        {
                            _refreshedTaskButtonBounds = refreshedTaskButtons;
                            _hasRefreshedTaskButtonBounds = true;
                        }
                        if (hasNotifications)
                        {
                            _refreshedNotificationBounds = refreshedNotifications;
                            _hasRefreshedNotificationBounds = true;
                        }
                        Reposition(true);
                    }));
                }
                catch (InvalidOperationException) { }
                finally { Interlocked.Exchange(ref _dynamicAnchorRefreshRunning, 0); }
            }));
            thread.IsBackground = true;
            thread.SetApartmentState(ApartmentState.MTA);
            thread.Start();
        }

        public Rectangle GetAnchorBounds()
        {
            NativeRect bounds;
            if (IsHandleCreated && GetWindowRect(Handle, out bounds))
            {
                return Rectangle.FromLTRB(bounds.Left, bounds.Top, bounds.Right, bounds.Bottom);
            }

            return new Rectangle(Left, Top, Width, Height);
        }

        public void Reposition(bool forceZOrder = false)
        {
            var taskbar = FindWindow("Shell_TrayWnd", null);
            NativeRect taskbarBounds;
            if (taskbar == IntPtr.Zero || !GetWindowRect(taskbar, out taskbarBounds))
            {
                return;
            }

            if (!EnsureTaskbarAttachment()) return;

            var taskbarWidth = taskbarBounds.Right - taskbarBounds.Left;
            var taskbarHeight = taskbarBounds.Bottom - taskbarBounds.Top;
            var tray = FindWindowEx(taskbar, IntPtr.Zero, "TrayNotifyWnd", null);
            var hasTray = _hasRefreshedNotificationBounds && _refreshedTaskbarHandle == taskbar &&
                IntersectsTaskbar(_refreshedNotificationBounds, taskbarBounds);
            var trayBounds = hasTray ? _refreshedNotificationBounds : new NativeRect();
            if (!hasTray) hasTray = tray != IntPtr.Zero && GetWindowRect(tray, out trayBounds);
            NativeRect visibleNotificationBounds;
            if (!hasTray &&
                TryGetVisibleNotificationBounds(taskbar, taskbarBounds, taskbarWidth >= taskbarHeight, out visibleNotificationBounds))
            {
                trayBounds = visibleNotificationBounds;
                hasTray = true;
            }
            NativeRect taskButtonBounds;
            var hasTaskButtons = _hasRefreshedTaskButtonBounds && _refreshedTaskbarHandle == taskbar &&
                IntersectsTaskbar(_refreshedTaskButtonBounds, taskbarBounds);
            taskButtonBounds = hasTaskButtons ? _refreshedTaskButtonBounds : new NativeRect();
            if (!hasTaskButtons)
                hasTaskButtons = TryGetTaskButtonBounds(taskbar, taskbarBounds, taskbarWidth >= taskbarHeight, out taskButtonBounds);

            int x;
            int y;
            if (taskbarWidth >= taskbarHeight)
            {
                var taskButtonX = hasTaskButtons ? taskButtonBounds.Left - Width - 8 : int.MinValue;
                x = taskButtonX >= taskbarBounds.Left + 4
                    ? CalculateLeadingPlacement(taskbarBounds.Left, taskbarBounds.Right, Width, taskButtonBounds.Left, 8)
                    : (hasTray ? trayBounds.Left - Width - 8 : taskbarBounds.Right - Width - 190);
                y = taskbarBounds.Top + Math.Max(0, (taskbarHeight - Height) / 2) + Math.Max(1, (int)Math.Round(_dpiScale));
                y = Math.Min(y, taskbarBounds.Bottom - Height - 1);
                x = Math.Max(taskbarBounds.Left + 4, Math.Min(x, taskbarBounds.Right - Width - 4));
            }
            else
            {
                x = taskbarBounds.Left + Math.Max(0, (taskbarWidth - Width) / 2);
                var taskButtonY = hasTaskButtons ? taskButtonBounds.Top - Height - 8 : int.MinValue;
                y = taskButtonY >= taskbarBounds.Top + 4
                    ? CalculateLeadingPlacement(taskbarBounds.Top, taskbarBounds.Bottom, Height, taskButtonBounds.Top, 8)
                    : (hasTray ? trayBounds.Top - Height - 8 : taskbarBounds.Bottom - Height - 190);
                y = Math.Max(taskbarBounds.Top + 4, Math.Min(y, taskbarBounds.Bottom - Height - 4));
            }

            NativeRect currentBounds;
            const int extendedStyle = -20;
            const int topMostStyle = 0x00000008;
            var correctlyPlaced = GetWindowRect(Handle, out currentBounds) &&
                currentBounds.Left == x && currentBounds.Top == y &&
                currentBounds.Right - currentBounds.Left == Width &&
                currentBounds.Bottom - currentBounds.Top == Height;
            var alreadyTopMost = (GetWindowLong(Handle, extendedStyle) & topMostStyle) != 0;
            if (correctlyPlaced && alreadyTopMost && !forceZOrder) return;

            const uint noActivate = 0x0010;
            const uint showWindow = 0x0040;
            var topMost = new IntPtr(-1);
            SetWindowPos(
                Handle,
                topMost,
                x,
                y,
                Width,
                Height,
                noActivate | showWindow);
        }

        internal static int CalculateLeadingPlacement(int taskbarStart, int taskbarEnd, int widgetExtent, int leadingEdge, int gap)
        {
            return Math.Max(taskbarStart + 4, Math.Min(leadingEdge - widgetExtent - gap, taskbarEnd - widgetExtent - 4));
        }

        private static bool TryGetTaskButtonBounds(
            IntPtr taskbar,
            NativeRect taskbarBounds,
            bool horizontal,
            out NativeRect result)
        {
            result = new NativeRect();
            try
            {
                var root = FindTaskbarAutomationRoot(taskbar);
                if (root == null) return false;

                var buttons = root.FindAll(
                    TreeScope.Descendants,
                    new PropertyCondition(AutomationElement.ControlTypeProperty, ControlType.Button));
                var found = false;
                var leadingEdge = int.MaxValue;
                for (var index = 0; index < buttons.Count; index++)
                {
                    var current = buttons[index].Current;
                    if (current.IsOffscreen || !IsTaskButton(current.AutomationId)) continue;
                    var bounds = current.BoundingRectangle;
                    if (bounds.IsEmpty || bounds.Width < 4 || bounds.Height < 4 || bounds.Width > 240 || bounds.Height > 240) continue;
                    var candidate = ToNativeRect(bounds);
                    if (!IntersectsTaskbar(candidate, taskbarBounds)) continue;

                    var edge = horizontal ? candidate.Left : candidate.Top;
                    if (edge >= leadingEdge) continue;
                    leadingEdge = edge;
                    result = candidate;
                    found = true;
                }
                return found;
            }
            catch (ElementNotAvailableException) { return false; }
            catch (InvalidOperationException) { return false; }
            catch (COMException) { return false; }
        }

        private static bool TryGetVisibleNotificationBounds(
            IntPtr taskbar,
            NativeRect taskbarBounds,
            bool horizontal,
            out NativeRect result)
        {
            result = new NativeRect();
            try
            {
                var root = FindTaskbarAutomationRoot(taskbar);
                if (root == null) return false;

                var buttons = root.FindAll(
                    TreeScope.Descendants,
                    new OrCondition(
                        new PropertyCondition(AutomationElement.AutomationIdProperty, "SystemTrayIcon"),
                        new PropertyCondition(AutomationElement.AutomationIdProperty, "NotifyItemIcon")));
                var found = false;
                var leadingEdge = int.MaxValue;
                for (var index = 0; index < buttons.Count; index++)
                {
                    var current = buttons[index].Current;
                    if (current.IsOffscreen) continue;
                    var bounds = current.BoundingRectangle;
                    if (bounds.IsEmpty || bounds.Width < 4 || bounds.Height < 4 || bounds.Width > 200 || bounds.Height > 200) continue;
                    var candidate = ToNativeRect(bounds);
                    if (!IntersectsTaskbar(candidate, taskbarBounds)) continue;

                    var edge = horizontal ? candidate.Left : candidate.Top;
                    if (edge >= leadingEdge) continue;
                    leadingEdge = edge;
                    result = candidate;
                    found = true;
                }
                return found;
            }
            catch (ElementNotAvailableException) { return false; }
            catch (InvalidOperationException) { return false; }
            catch (COMException) { return false; }
        }

        private static AutomationElement FindTaskbarAutomationRoot(IntPtr taskbar)
        {
            var root = AutomationElement.RootElement.FindFirst(
                TreeScope.Children,
                new PropertyCondition(AutomationElement.ClassNameProperty, "Shell_TrayWnd"));
            return root ?? AutomationElement.FromHandle(taskbar);
        }

        private static bool IsTaskButton(string automationId)
        {
            return string.Equals(automationId, "StartButton", StringComparison.Ordinal) ||
                string.Equals(automationId, "SearchButton", StringComparison.Ordinal) ||
                string.Equals(automationId, "TaskViewButton", StringComparison.Ordinal) ||
                string.Equals(automationId, "WidgetsButton", StringComparison.Ordinal) ||
                (!string.IsNullOrEmpty(automationId) && automationId.StartsWith("Appid:", StringComparison.Ordinal));
        }

        private static NativeRect ToNativeRect(System.Windows.Rect bounds)
        {
            return new NativeRect
            {
                Left = (int)Math.Round(bounds.Left),
                Top = (int)Math.Round(bounds.Top),
                Right = (int)Math.Round(bounds.Right),
                Bottom = (int)Math.Round(bounds.Bottom)
            };
        }

        private static bool IntersectsTaskbar(NativeRect candidate, NativeRect taskbar)
        {
            return candidate.Right > taskbar.Left && candidate.Left < taskbar.Right &&
                candidate.Bottom > taskbar.Top && candidate.Top < taskbar.Bottom;
        }

        private bool EnsureTaskbarAttachment()
        {
            const int ownerIndex = -8;
            const uint ownerWindow = 4;

            var taskbar = FindWindow("Shell_TrayWnd", null);
            if (taskbar == IntPtr.Zero) return false;

            var handle = Handle;
            if (GetWindow(handle, ownerWindow) != taskbar)
            {
                SetWindowLongPtr(handle, ownerIndex, taskbar);
                _taskbarHandle = taskbar;
                _dpiScale = GraphicsExtensions.GetDpiScale(handle);
                ApplyScaledSize();
            }
            else _taskbarHandle = taskbar;

            return GetWindow(handle, ownerWindow) == _taskbarHandle;
        }

        private static IntPtr SetWindowLongPtr(IntPtr window, int index, IntPtr value)
        {
            if (IntPtr.Size == 8) return SetWindowLongPtr64(window, index, value);
            return new IntPtr(SetWindowLong(window, index, value.ToInt32()));
        }

        protected override void OnResize(EventArgs e)
        {
            base.OnResize(e);
            SetRoundedRegion();
        }

        protected override void OnMouseEnter(EventArgs e)
        {
            base.OnMouseEnter(e);
            UpdateQuotaDescription();
            _hovered = true;
            if (!_popupOpen) _hoverTimer.Start();
            Invalidate();
        }

        protected override void OnMouseLeave(EventArgs e)
        {
            base.OnMouseLeave(e);
            _hovered = false;
            HideHoverDetails();
            Invalidate();
        }

        internal Rectangle TaskStatusBounds
        {
            get
            {
                var scale = GetDrawingScale();
                return Rectangle.Round(new RectangleF((GetBaseWidth() - 84) / 2f * scale, 4 * scale, 84 * scale, 24 * scale));
            }
        }

        protected override void OnMouseClick(MouseEventArgs e)
        {
            base.OnMouseClick(e);
            if (e.Button != MouseButtons.Left) return;
            var handler = _settings.TaskStatusEnabled && TaskStatusBounds.Contains(e.Location)
                ? TaskStatusClicked : QuotaClicked;
            if (handler != null) handler(this, EventArgs.Empty);
        }

        protected override void OnMouseDown(MouseEventArgs e)
        {
            HideHoverDetails();
            base.OnMouseDown(e);
        }

        protected override void OnVisibleChanged(EventArgs e)
        {
            if (!Visible && _hoverTimer != null) HideHoverDetails();
            base.OnVisibleChanged(e);
            if (_countdownTimer != null) UpdateCountdown();
        }

        private void HideHoverDetails() { _hoverTimer.Stop(); _quotaTip.Hide(); }
        internal void SetPopupOpen(bool open) { _popupOpen = open; if (open) HideHoverDetails(); }

        protected override void OnHandleCreated(EventArgs e)
        {
            base.OnHandleCreated(e);
            _dpiScale = GraphicsExtensions.GetDpiScale(Handle);
            ApplyScaledSize();
            if (_settings.WidgetVisible)
                BeginInvoke(new Action(delegate { EnsureOnTop(); }));
        }

        protected override void WndProc(ref Message message)
        {
            const int DpiChanged = 0x02E0;
            if (message.Msg == DpiChanged)
            {
                var newDpi = message.WParam.ToInt64() & 0xFFFF;
                base.WndProc(ref message);
                _dpiScale = Math.Max(1f, newDpi / 96f);
                ApplyScaledSize();
                BeginInvoke(new Action(delegate { Reposition(); }));
                Invalidate();
                return;
            }
            base.WndProc(ref message);
        }

        protected override void OnPaint(PaintEventArgs e)
        {
            base.OnPaint(e);
            DrawQuotaStrip(e.Graphics);
        }

        private void DrawQuotaStrip(Graphics graphics, double? previewPhase = null)
        {
            graphics.SmoothingMode = SmoothingMode.AntiAlias;
            graphics.TextRenderingHint = System.Drawing.Text.TextRenderingHint.ClearTypeGridFit;
            graphics.ScaleTransform(GetDrawingScale(), GetDrawingScale());
            var baseWidth = GetBaseWidth();
            var backgroundColor = _hovered ? Blend(_palette.Background, _palette.Primary, 0.07f) : _palette.Background;
            using (var background = new SolidBrush(backgroundColor))
            using (var border = new Pen(_hovered ? _palette.Primary : Color.FromArgb(145, _palette.Divider)))
            {
                graphics.FillRoundedRectangle(background, new Rectangle(0, 0, baseWidth - 1, BaseHeight - 1), 8);
                graphics.DrawRoundedRectangle(border, new Rectangle(0, 0, baseWidth - 1, BaseHeight - 1), 8);
            }
            var dual = _settings.ShowFiveHourQuota && _hasWeeklyWindow;
            var columnWidth = dual ? DualBaseWidth / 2 : baseWidth;
            var now = DateTimeOffset.Now;
            if (_settings.ShowFiveHourQuota)
                DrawStripColumn(graphics, 8, columnWidth - 16, _primaryLabel == "W" && !_settings.IsEnglish ? "周" : _primaryLabel, _fiveHourRemainingPercent,
                    _active && _hasPrimaryWindow, false, _fiveHourResetsAt, now);
            else
                DrawStripColumn(graphics, 8, columnWidth - 16,
                    _isReserve ? "R" : (_settings.IsEnglish ? "W" : "周"), _weeklyRemainingPercent, _active && _hasWeeklyWindow, true, _weeklyResetsAt, now);
            if (dual)
            {
                using (var separator = new Pen(Color.FromArgb(110, _palette.Divider))) graphics.DrawLine(separator, columnWidth, 8, columnWidth, 30);
                if (_settings.TaskStatusEnabled)
                    using (var separator = new Pen(Color.FromArgb(110, _palette.Divider))) graphics.DrawLine(separator, columnWidth + StatusWidth, 8, columnWidth + StatusWidth, 30);
                DrawStripColumn(graphics, columnWidth + (_settings.TaskStatusEnabled ? StatusWidth : 0) + 8, columnWidth - 16,
                    _isReserve ? "R" : (_settings.IsEnglish ? "W" : "周"), _weeklyRemainingPercent, _active, true, _weeklyResetsAt, now);
            }
            if (_settings.TaskStatusEnabled) DrawTaskStatus(graphics, baseWidth, previewPhase);
        }

        private void DrawTaskStatus(Graphics graphics, int baseWidth, double? previewPhase)
        {
            var state = _taskStatus.State;
            var color = StatusColor(state, _palette.IsLight);
            var phase = previewPhase ?? (_pulseTimer.Enabled ? .5 + .5 * Math.Sin(_pulseClock.Elapsed.TotalSeconds * Math.PI) : 1);
            var intensity = state == CodexTaskState.Working ? .45 + .55 * phase : 1;
            var bounds = new Rectangle((baseWidth - 84) / 2, 4, 84, 24);
            using (var fill = new SolidBrush(Color.FromArgb((int)(18 + intensity * 15), color)))
                graphics.FillRoundedRectangle(fill, bounds, 6);
            var dot = new RectangleF(bounds.Left + 6, 13, 5, 5);
            using (var glow = new SolidBrush(Color.FromArgb((int)(intensity * 32), color)))
                if (state == CodexTaskState.Working) graphics.FillEllipse(glow, dot.X - 3, dot.Y - 3, 11, 11);
            using (var brush = new SolidBrush(ThemePalette.Mix(_palette.Muted, color, intensity))) graphics.FillEllipse(brush, dot);
            using (var font = new Font("Segoe UI", 11.6f * Math.Min(1.1f, _settings.FontScalePercent / 100f), FontStyle.Bold, GraphicsUnit.Pixel))
            using (var brush = new SolidBrush(color))
            using (var format = new StringFormat { Alignment = StringAlignment.Center, LineAlignment = StringAlignment.Center })
                graphics.DrawString(_taskStatus.Label, font, brush, new RectangleF(bounds.Left + 12, bounds.Top, bounds.Width - 14, bounds.Height), format);
        }

        internal static Color StatusColor(CodexTaskState state, bool light)
        {
            switch (state)
            {
                case CodexTaskState.Finish: return ColorTranslator.FromHtml(light ? "#187A46" : "#59D98E");
                case CodexTaskState.Ask: return ColorTranslator.FromHtml(light ? "#985400" : "#FFB451");
                case CodexTaskState.Err: return ColorTranslator.FromHtml(light ? "#C52E35" : "#FF6D76");
                case CodexTaskState.Working: return ColorTranslator.FromHtml(light ? "#1768C4" : "#62ADFF");
                default: return ColorTranslator.FromHtml(light ? "#596879" : "#A7B6C7");
            }
        }

        private void DrawStripColumn(Graphics graphics, int x, int width, string label, int remaining, bool available, bool secondary,
            DateTimeOffset? resetsAt, DateTimeOffset now)
        {
            var accent = !available ? _palette.Faint : remaining <= 20 ? _palette.QuotaColor(remaining)
                : secondary ? _palette.Secondary : _palette.Primary;
            var value = available ? remaining + "%" : "--";
            var fontScale = _settings.FontScalePercent / 100f;
            var valueBounds = new RectangleF(x + 20, 1, width - 20, 21);
            if (_settings.TaskStatusEnabled && !(_settings.ShowFiveHourQuota && _hasWeeklyWindow))
                valueBounds = new RectangleF(GetBaseWidth() - 57, 1, 49, 21);
            var resetBounds = new RectangleF(valueBounds.X, 20, valueBounds.Width, 12);
            var reset = available ? FormatResetCountdown(resetsAt, now, _settings.IsEnglish) : "--";
            using (var labelFont = new Font("Segoe UI", 9.4f * fontScale, FontStyle.Regular, GraphicsUnit.Pixel))
            using (var labelBrush = new SolidBrush(_palette.Muted))
            using (var valueBrush = new SolidBrush(available ? _palette.Text : _palette.Muted))
            using (var format = new StringFormat { Alignment = StringAlignment.Far, LineAlignment = StringAlignment.Center })
            {
                graphics.DrawString(label, labelFont, labelBrush, new RectangleF(x, 7, 22, 18));
                var size = (_settings.ShowFiveHourQuota && _hasWeeklyWindow ? 17f : 19f) * fontScale;
                // Fit 100% and the largest supported font without clipping the tiny taskbar surface.
                using (var measured = new Font("Bahnschrift", size, FontStyle.Regular, GraphicsUnit.Pixel))
                {
                    var measuredWidth = graphics.MeasureString(value, measured).Width;
                    size *= Math.Min(1f, valueBounds.Width / Math.Max(1f, measuredWidth));
                }
                using (var valueFont = new Font("Bahnschrift", size, FontStyle.Regular, GraphicsUnit.Pixel))
                    graphics.DrawString(value, valueFont, valueBrush, valueBounds, format);
                var resetSize = 9.2f * fontScale;
                using (var measured = new Font("Segoe UI", resetSize, FontStyle.Regular, GraphicsUnit.Pixel))
                    resetSize *= Math.Min(1f, resetBounds.Width / Math.Max(1f, graphics.MeasureString(reset, measured).Width));
                using (var resetFont = new Font("Segoe UI", resetSize, FontStyle.Regular, GraphicsUnit.Pixel))
                    graphics.DrawString(reset, resetFont, labelBrush, resetBounds, format);
            }
            using (var track = new Pen(_palette.Track, 2f))
            using (var fill = new Pen(accent, 2f))
            {
                track.StartCap = track.EndCap = fill.StartCap = fill.EndCap = LineCap.Round;
                graphics.DrawLine(track, x, 34, x + width, 34);
                if (available && remaining > 0) graphics.DrawLine(fill, x, 34, x + width * remaining / 100f, 34);
            }
        }

        private void UpdateQuotaDescription()
        {
            var title = _settings.IsEnglish ? "Account quota remaining" : "账号额度剩余";
            var primary = _active && _hasPrimaryWindow ? _fiveHourRemainingPercent + "%" : "--";
            var reset = FormatResetHours(_fiveHourResetsAt, DateTimeOffset.Now);
            var description = title;
            if (_settings.ShowFiveHourQuota) description += " · " + _primaryLabel + " " + primary;
            if (_hasWeeklyWindow || !_settings.ShowFiveHourQuota)
                description += " · " + (_isReserve ? (_settings.IsEnglish ? "Reserve" : "备用") : (_settings.IsEnglish ? "Weekly" : "每周")) + " " + (_active && _hasWeeklyWindow ? _weeklyRemainingPercent + "%" : "--");
            if (_settings.ShowFiveHourQuota)
                description += Environment.NewLine + (_settings.IsEnglish ? "Short window resets in " : "短周期重置倒计时 ") + reset;
            if (_hasWeeklyWindow)
                description += (_settings.ShowFiveHourQuota ? " · " : Environment.NewLine) + (_settings.IsEnglish ? "Long window " : "长周期 ") + FormatResetHours(_weeklyResetsAt, DateTimeOffset.Now);
            if (_settings.ShowFiveHourQuota && _fiveHourTimeRemainingPercent >= 0)
                description += Environment.NewLine + (_settings.IsEnglish ? "Short window time remaining " : "短周期时间余量 ") + _fiveHourTimeRemainingPercent + "%";
            if (!_active) description += Environment.NewLine + (_settings.IsEnglish ? "Codex is offline or quota is unavailable" : "Codex 未启动或额度尚未获取");
            if (_settings.TaskStatusEnabled)
                description += Environment.NewLine + _taskStatus.Label + " · Working " + _taskStatus.WorkingCount + " · Ask " + _taskStatus.AskCount +
                    " · Err " + _taskStatus.ErrorCount + " · Finish " + _taskStatus.FinishCount +
                    Environment.NewLine + (_settings.IsEnglish ? "Click the Codex status area for the six most recent chats; click balance for quota"
                        : "点击 Codex 状态区域查看最近 6 个会话；点击余额查看额度");
            AccessibleName = title;
            AccessibleDescription = description;
            if (_quotaTip.Visible) _quotaTip.Apply(description, _settings, _dpiScale);
        }

        internal Bitmap RenderPreview(QuotaReadResult result, AppSettings settings, float dpiScale = 1f)
        {
            _dpiScale = dpiScale;
            _settings = settings;
            _palette = ThemePalette.FromSettings(settings);
            SetQuotaValues(result);
            var size = GetScaledSize();
            var bitmap = new Bitmap(size.Width, size.Height);
            using (var graphics = Graphics.FromImage(bitmap))
            {
                graphics.Clear(_palette.Background);
                DrawQuotaStrip(graphics);
            }
            return bitmap;
        }

        internal Bitmap RenderStatusPreview(QuotaReadResult result, AppSettings settings, TaskStatusSnapshot status, double phase)
        {
            _taskStatus = status; _settings = settings; _palette = ThemePalette.FromSettings(settings); SetQuotaValues(result);
            var size = GetScaledSize(); var bitmap = new Bitmap(size.Width, size.Height);
            using (var graphics = Graphics.FromImage(bitmap))
            {
                graphics.Clear(_palette.Background); DrawQuotaStrip(graphics, phase);
            }
            return bitmap;
        }

        protected override void Dispose(bool disposing)
        {
            if (disposing) { _pulseTimer.Stop(); _pulseTimer.Dispose(); _countdownTimer.Stop(); _countdownTimer.Dispose(); _hoverTimer.Stop(); _hoverTimer.Dispose(); _quotaTip.Dispose(); }
            base.Dispose(disposing);
        }

        internal static string FormatResetHours(DateTimeOffset? resetsAt, DateTimeOffset now)
        {
            if (!resetsAt.HasValue) return "--H";
            var hours = Math.Max(0d, (resetsAt.Value - now).TotalHours);
            return hours.ToString("0.#", CultureInfo.InvariantCulture) + "H";
        }

        internal static string FormatResetCountdown(DateTimeOffset? resetsAt, DateTimeOffset now, bool english)
        {
            if (!resetsAt.HasValue) return "--";
            var remaining = resetsAt.Value - now;
            if (remaining <= TimeSpan.Zero) return english ? "due" : "待刷新";
            var minutes = (long)Math.Ceiling(remaining.TotalMinutes);
            if (minutes >= 1440)
                return (minutes / 1440).ToString(CultureInfo.InvariantCulture) + "d" + ((minutes % 1440) / 60).ToString(CultureInfo.InvariantCulture) + "h";
            if (minutes >= 60)
                return (minutes / 60).ToString(CultureInfo.InvariantCulture) + "h" + (minutes % 60).ToString(CultureInfo.InvariantCulture) + "m";
            return minutes.ToString(CultureInfo.InvariantCulture) + "m";
        }

        private void SetRoundedRegion()
        {
            using (var path = GraphicsExtensions.CreateRoundedPath(
                new RectangleF(0, 0, Width, Height),
                Math.Max(8f, 8f * GetDrawingScale())))
            {
                Region = new Region(path);
            }
        }

        private static Color Blend(Color from, Color to, float amount)
        {
            amount = Math.Max(0f, Math.Min(1f, amount));
            return Color.FromArgb(
                (int)Math.Round(from.A + (to.A - from.A) * amount),
                (int)Math.Round(from.R + (to.R - from.R) * amount),
                (int)Math.Round(from.G + (to.G - from.G) * amount),
                (int)Math.Round(from.B + (to.B - from.B) * amount));
        }

        private float GetDrawingScale()
        {
            return _dpiScale * _settings.WidgetScalePercent / 100f;
        }

        private Size GetScaledSize()
        {
            return GraphicsExtensions.ScaleSize(new Size(GetBaseWidth(), BaseHeight), GetDrawingScale());
        }

        private int GetBaseWidth()
        {
            var dual = _settings.ShowFiveHourQuota && _hasWeeklyWindow;
            return (dual ? DualBaseWidth : SingleBaseWidth) + (_settings.TaskStatusEnabled ? (dual ? StatusWidth : 100) : 0);
        }

        private void ApplyScaledSize()
        {
            if (!IsHandleCreated) return;
            Size = GetScaledSize();
            SetRoundedRegion();
        }
    }
}
