using System;
using System.Collections.Generic;
using System.Drawing;
using System.Drawing.Drawing2D;
using System.Globalization;
using System.Linq;

namespace CodexQuotaTray
{
    // The combined companion's own quota presentation; readers and settings stay shared.
    internal sealed partial class QuotaPopupForm
    {
        private static readonly Rectangle OverviewChart = new Rectangle(28, 361, 364, 68);
        private const int HoverSessionEntry = 51;

        private void DrawQuotaOverview(Graphics graphics)
        {
            DrawText(graphics, T("账号额度", "ACCOUNT LIMITS"), 8,
                new RectangleF(24, 19, 215, 18), _palette.Muted, FontStyle.Regular);
            DrawText(graphics, T("额度概览", "Quota overview"), 19,
                new RectangleF(24, 38, 310, 36), _palette.Text, FontStyle.Bold);
            var plan = GetPlanLabel();
            var offline = _result != null && !_result.IsCodexRunning;
            DrawText(graphics, (plan ?? "") + (offline ? (string.IsNullOrEmpty(plan) ? "" : " · ") + T("Codex 未启动", "Codex offline") : ""), 8,
                new RectangleF(24, 74, 372, 18), offline ? _palette.Warning : _palette.Muted, FontStyle.Regular);
            DrawRefreshIcon(graphics, new PointF(383, 37));

            var primary = GetMainWindow();
            var weekly = _result == null || _result.Snapshot == null ? null : _result.Snapshot.DisplayWeeklyWindow;
            if (weekly == primary) weekly = null;
            if (_settings.ShowFiveHourQuota)
            {
                var leftWidth = _settings.QuotaPrimaryEmphasis ? 204 : 180;
                DrawQuotaCard(graphics, primary, new Rectangle(24, 100, leftWidth, 174), false);
                DrawQuotaCard(graphics, weekly, new Rectangle(36 + leftWidth, 100, 360 - leftWidth, 174), true);
            }
            else DrawQuotaCard(graphics, weekly, new Rectangle(24, 100, 372, 174), true);
            DrawOverviewActivity(graphics);
            DrawCombinedEntries(graphics);
            DrawOverviewFooter(graphics);
            if (_hoverHint == HoverRefresh)
                DrawContextHint(graphics, new RectangleF(228, 60, 168, 28), T("刷新账号额度", "Refresh account limits"));
        }

        private string QuotaCardTitle(QuotaWindow window, bool secondary)
        {
            if (window != null && !string.IsNullOrWhiteSpace(window.Key) &&
                window.Key.StartsWith("gpt-reserve:", StringComparison.OrdinalIgnoreCase))
                return T("备用额度", "Reserve quota");
            if (window == null) return secondary ? T("每周", "Weekly") : T("5 小时", "5 hours");
            if (window.Kind == QuotaWindowKind.Weekly || window.WindowMinutes == 10080) return T("每周", "Weekly");
            if (window.Kind == QuotaWindowKind.FiveHour || window.WindowMinutes == 300) return T("5 小时", "5 hours");
            return GetWindowName(window) ?? T("额度", "Quota");
        }

        private void DrawQuotaCard(Graphics graphics, QuotaWindow window, Rectangle bounds, bool secondary)
        {
            var remaining = window == null ? 0 : window.RemainingPercent;
            var color = window == null ? _palette.Faint : _palette.QuotaColor(remaining);
            if (secondary && remaining > 20 && window != null) color = _palette.Secondary;
            DrawContentSurface(graphics, bounds, 13);
            var x = bounds.Left + 14;
            var width = bounds.Width - 28;
            using (var accent = new SolidBrush(color)) graphics.FillEllipse(accent, x, bounds.Top + 16, 5, 5);
            DrawText(graphics, QuotaCardTitle(window, secondary), 9,
                new RectangleF(x + 12, bounds.Top + 9, width - 12, 22), _palette.Text, FontStyle.Bold);
            DrawOverviewNumber(graphics, window == null ? "--" : remaining + "%", bounds.Width < 170 ? 27 : 32,
                new RectangleF(x - 1, bounds.Top + 35, width + 2, 53), window == null ? _palette.Faint : _palette.Text);
            var time = window == null ? -1 : window.TimeRemainingPercent;
            var fast = window != null && time >= 0 && QuotaUsageMath.IsConsumptionFast(remaining, time);
            DrawText(graphics, fast ? T("消耗偏快", "Above pace") : T("额度剩余", "Quota left"), 7.8f,
                new RectangleF(x, bounds.Top + 89, width, 19), fast ? _palette.Warning : _palette.Muted, FontStyle.Regular);
            DrawProgress(graphics, new Rectangle(x, bounds.Top + 113, width, 4), remaining, color);
            DrawText(graphics, T("时间余量 ", "Time left ") + (time < 0 ? "--" : time + "%"), 7.4f,
                new RectangleF(x, bounds.Top + 119, width, 18), _palette.Muted, FontStyle.Regular);
            var countdown = window != null && window.ResetsAt.HasValue
                ? T("重置 ", "Reset in ") + FormatCountdown(window.ResetsAt.Value - DateTimeOffset.Now)
                : T("暂无重置时间", "Reset time unavailable");
            DrawText(graphics, countdown, 7.5f,
                new RectangleF(x, bounds.Top + 138, width, 18), _palette.Text, FontStyle.Regular);
            var reset = window != null && window.ResetsAt.HasValue
                ? window.ResetsAt.Value.ToLocalTime().ToString(_settings.IsEnglish ? "MMM d · HH:mm" : "M月d日 · HH:mm", CultureInfo.CurrentCulture)
                : (window == null ? T("暂无该周期额度", "Quota unavailable") : "");
            DrawText(graphics, reset, 7,
                new RectangleF(x, bounds.Top + 155, width, 16), _palette.Muted, FontStyle.Regular);
        }

        private void DrawContentSurface(Graphics graphics, Rectangle bounds, int radius)
        {
            if (_glassActive)
            {
                using (var fill = new LinearGradientBrush(bounds,
                    Color.FromArgb(_palette.IsLight ? 65 : 30, ThemePalette.Mix(Color.White, _palette.Primary, .35)),
                    Color.FromArgb(_palette.IsLight ? 24 : 14, _palette.Secondary), 65f))
                    graphics.FillRoundedRectangle(fill, bounds, radius);
                using (var edge = new Pen(Color.FromArgb(_palette.IsLight ? 110 : 62, _palette.Primary)))
                    graphics.DrawRoundedRectangle(edge, bounds, radius);
            }
            else
            {
                using (var fill = new SolidBrush(_palette.Surface)) graphics.FillRoundedRectangle(fill, bounds, radius);
                using (var edge = new Pen(Color.FromArgb(150, _palette.Divider))) graphics.DrawRoundedRectangle(edge, bounds, radius);
            }
        }

        private void DrawOverviewNumber(Graphics graphics, string text, float size, RectangleF bounds, Color color)
        {
            var scaledSize = size * Math.Min(1.08f, _settings.FontScalePercent / 100f);
            if (_glassActive)
            {
                _layeredTextItems.Add(new DirectWriteTextRenderer.TextItem
                {
                    Text = text, Size = scaledSize, Bounds = bounds, Color = color,
                    Style = FontStyle.Regular, Alignment = StringAlignment.Near, FontFamily = "Bahnschrift"
                });
                return;
            }
            using (var font = new Font("Bahnschrift", scaledSize * 96f / 72f, FontStyle.Regular, GraphicsUnit.Pixel))
            using (var brush = new SolidBrush(color))
            using (var format = new StringFormat { LineAlignment = StringAlignment.Center, Trimming = StringTrimming.EllipsisCharacter })
                graphics.DrawString(text, font, brush, bounds, format);
        }

        private void DrawOverviewActivity(Graphics graphics)
        {
            var buckets = BuildActivityBuckets();
            _currentBuckets = buckets;
            EnsureHoverWeights(buckets.Count);
            DrawText(graphics, T("用量走势", "Usage activity"), 10,
                new RectangleF(24, 287, 188, 26), _palette.Text, FontStyle.Bold);
            DrawText(graphics, FormatTokenCount(buckets.Sum(item => item.Tokens)) + " tokens", 10,
                new RectangleF(215, 287, 181, 26), _palette.Text, FontStyle.Bold, StringAlignment.Far);
            DrawOverviewRange(graphics, _range5Bounds, ActivityRange.Hours5, T("5小时", "5h"), HoverRange5);
            DrawOverviewRange(graphics, _range24Bounds, ActivityRange.Hours24, T("24小时", "24h"), HoverRange24);
            DrawOverviewRange(graphics, _range7Bounds, ActivityRange.Days7, T("7天", "7d"), HoverRange7);
            DrawOverviewRange(graphics, _range30Bounds, ActivityRange.Days30, T("30天", "30d"), HoverRange30);
            DrawActivityTrace(graphics, buckets);
            var change = ActivityQuotaSummary(buckets);
            DrawText(graphics, change, 7.6f, new RectangleF(24, 439, 233, 22), _palette.Muted, FontStyle.Regular);
            if (_result != null && _result.ActivityStatus == "partial")
                DrawText(graphics, T("部分活动记录", "Partial activity"), 7.6f,
                    new RectangleF(262, 439, 134, 22), _palette.Warning, FontStyle.Regular, StringAlignment.Far);
        }

        private void DrawOverviewRange(Graphics graphics, Rectangle bounds, ActivityRange range, string text, int hover)
        {
            var selected = _settings.ActivityRange == range;
            var hovered = _hoverHint == hover;
            if (selected || hovered)
                using (var brush = new SolidBrush(Color.FromArgb(selected ? 30 : 14, _palette.Primary)))
                    graphics.FillRoundedRectangle(brush, bounds, 6);
            DrawText(graphics, text, 8, bounds, selected ? _palette.Text : _palette.Muted,
                selected ? FontStyle.Bold : FontStyle.Regular, StringAlignment.Center);
            if (selected)
                using (var pen = new Pen(_palette.Primary, 2)) graphics.DrawLine(pen, bounds.Left + 13, bounds.Bottom, bounds.Right - 13, bounds.Bottom);
        }

        private void DrawActivityTrace(Graphics graphics, IList<ActivityBucket> values)
        {
            var bounds = OverviewChart;
            using (var rule = new Pen(Color.FromArgb(80, _palette.Divider)))
                for (var i = 0; i <= 2; i++) graphics.DrawLine(rule, bounds.Left, bounds.Top + i * bounds.Height / 2, bounds.Right, bounds.Top + i * bounds.Height / 2);
            if (values.Count == 0 || values.All(item => item.Tokens <= 0))
            {
                DrawText(graphics, _result != null && _result.ActivityStatus == "pending"
                    ? T("活动记录更新中", "Activity is updating") : T("此时段暂无用量", "No activity in this period"),
                    9, bounds, _palette.Muted, FontStyle.Regular, StringAlignment.Center);
                return;
            }
            var max = Math.Max(1L, values.Max(item => item.Tokens));
            var points = values.Select((item, index) => new PointF(
                bounds.Left + index * bounds.Width / (float)Math.Max(1, values.Count - 1),
                bounds.Bottom - 4 - (bounds.Height - 12) * item.Tokens / (float)max)).ToArray();
            using (var area = new GraphicsPath())
            using (var fill = new LinearGradientBrush(bounds, Color.FromArgb(42, _palette.Primary), Color.FromArgb(2, _palette.Primary), 90f))
            using (var line = new Pen(_palette.Primary, 1.8f) { LineJoin = LineJoin.Round })
            {
                area.AddLines(points);
                area.AddLine(points[points.Length - 1], new PointF(bounds.Right, bounds.Bottom));
                area.AddLine(new PointF(bounds.Right, bounds.Bottom), new PointF(bounds.Left, bounds.Bottom));
                area.CloseFigure();
                graphics.FillPath(fill, area);
                graphics.DrawLines(line, points);
            }
            if (_hoverIndex >= 0 && _hoverIndex < points.Length)
            {
                var point = points[_hoverIndex];
                using (var cursor = new Pen(Color.FromArgb(140, _palette.Muted)) { DashStyle = DashStyle.Dot })
                    graphics.DrawLine(cursor, point.X, bounds.Top, point.X, bounds.Bottom);
                using (var dot = new SolidBrush(_palette.Primary)) graphics.FillEllipse(dot, point.X - 3, point.Y - 3, 6, 6);
                DrawActivityTooltip(graphics, values, bounds, 0, bounds.Width / (float)Math.Max(1, values.Count - 1));
            }
        }

        private void DrawOverviewFooter(Graphics graphics)
        {
            var live = _result != null && _result.Snapshot != null && _result.IsLiveQuota;
            using (var dot = new SolidBrush(live ? _palette.Success : _palette.Faint)) graphics.FillEllipse(dot, 24, 560, 5, 5);
            var stamp = _result != null && _result.Snapshot != null
                ? (live ? T("实时同步", "Live") : T("缓存额度", "Cached quota")) + " · " + _result.Snapshot.CapturedAt.ToLocalTime().ToString("HH:mm")
                : T("等待额度更新", "Waiting for quota");
            DrawText(graphics, stamp, 8, new RectangleF(38, 548, 267, 30), _palette.Muted, FontStyle.Regular);
            DrawText(graphics, T("退出", "Quit"), 9, _exitBounds, InteractiveTextColor(HoverExit, _palette.Muted), FontStyle.Regular, StringAlignment.Far);
        }
    }
}
