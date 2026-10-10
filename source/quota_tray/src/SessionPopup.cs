using System;
using System.Drawing;
using System.Linq;
using System.Windows.Forms;

namespace CodexQuotaTray
{
    internal sealed partial class QuotaPopupForm
    {
        private SessionUsageSnapshot _session = CompanionBridge.Unavailable(null);
        private bool _showSession;
        private bool _sessionThreads;
        private int _sessionOffset;
        private readonly Rectangle _sessionEntryBounds = new Rectangle(20, 468, 220, 60);
        private readonly Rectangle _sessionModelBounds = new Rectangle(24, 172, 174, 28);
        private readonly Rectangle _sessionThreadBounds = new Rectangle(220, 172, 176, 28);
        public void ApplySession(SessionUsageSnapshot value)
        {
            if (_session.ThreadId != value.ThreadId) _sessionOffset = 0;
            _session = value;
            Invalidate();
        }
        public void ShowSession()
        { _showSettings = _showDisplaySettings = false; _showSession = true; _sessionOffset = 0; Invalidate(); }

        private void DrawCombinedEntries(Graphics graphics)
        {
            using (var fill = new SolidBrush(Color.FromArgb(_hoverHint == HoverSessionEntry ? 28 : 12, _palette.Primary)))
            using (var edge = new Pen(Color.FromArgb(95, _palette.Divider)))
            {
                graphics.FillRoundedRectangle(fill, _sessionEntryBounds, 10);
                graphics.DrawRoundedRectangle(edge, _sessionEntryBounds, 10);
                graphics.DrawRoundedRectangle(edge, _settingsBounds, 10);
            }
            if (_hoverHint == HoverSettingsEntry)
                using (var fill = new SolidBrush(Color.FromArgb(20, _palette.Primary)))
                    graphics.FillRoundedRectangle(fill, _settingsBounds, 10);
            DrawText(graphics, T("会话用量", "Session usage"), 9.5f,
                new RectangleF(34, 474, 179, 25), _palette.Text, FontStyle.Bold);
            DrawText(graphics, T("模型 · Fast · 智能体", "Models · Fast · Agents"), 7.6f,
                new RectangleF(34, 499, 186, 21), _palette.Muted, FontStyle.Regular);
            DrawText(graphics, "›", 15, new RectangleF(219, 482, 15, 28), _palette.Muted, FontStyle.Regular);
            DrawText(graphics, T("设置", "Settings"), 9.5f,
                new RectangleF(276, 482, 114, 28), _palette.Muted, FontStyle.Regular, StringAlignment.Center);
        }
        private void DrawSessionPage(Graphics graphics)
        {
            DrawBackIcon(graphics);
            DrawText(graphics, T("会话用量", "Session usage"), 14, new RectangleF(60, 18, 300, 28), _palette.Text, FontStyle.Bold);
            DrawText(graphics, _session.ThreadId == null ? T("等待确认当前会话", "Waiting for current session") :
                T("当前会话 · ", "Current session · ") + _session.ThreadId.Substring(0, Math.Min(8, _session.ThreadId.Length)),
                8.5f, new RectangleF(60, 47, 310, 22), _palette.Muted, FontStyle.Regular);
            DrawDivider(graphics, 82);
            DrawText(graphics, _session.Total.HasValue ? FormatTokenCount(_session.Total.Value) : "--", 30,
                new RectangleF(24, 92, 278, 51), _palette.Text, FontStyle.Bold);
            DrawText(graphics, "tokens", 10, new RectangleF(296, 116, 100, 25), _palette.Muted, FontStyle.Regular, StringAlignment.Far);
            DrawText(graphics, _session.Status == "partial" ? T("统计不完整 · 可查看已确认明细", "Partial · confirmed records below") :
                _session.Status == "complete" ? T("主会话 + 子智能体", "Main session + subagents") :
                T("请打开本地会话，连接后自动更新", "Open a local conversation to connect"),
                8.2f, new RectangleF(24, 145, 372, 22), _palette.Muted, FontStyle.Regular);
            DrawSessionTab(graphics, _sessionModelBounds, T("模型与配置", "Models and settings"), !_sessionThreads);
            DrawSessionTab(graphics, _sessionThreadBounds, T("智能体", "Agents"), _sessionThreads);
            var rows = _sessionThreads ? _session.Threads : _session.Models;
            _sessionOffset = Math.Max(0, Math.Min(_sessionOffset, Math.Max(0, rows.Count - 3)));
            if (rows.Count == 0)
                DrawText(graphics, T("暂无用量记录", "No usage records yet"), 11, new RectangleF(24, 236, 372, 170), _palette.Muted, FontStyle.Regular, StringAlignment.Center);
            var y = 210;
            foreach (var row in rows.Skip(_sessionOffset).Take(3))
            {
                DrawContentSurface(graphics, new Rectangle(24, y, 372, 84), 9);
                var rowName = row.Name == "unattributed" ? T("未归属模型", "Unattributed model") :
                    row.Name == "主会话" ? T("主会话", "Main session") : row.Name == "子智能体" ? T("子智能体", "Subagent") : row.Name;
                DrawText(graphics, rowName, 10, new RectangleF(36, y + 5, 252, 24), _palette.Text, FontStyle.Bold);
                DrawText(graphics, FormatTokenCount(row.Total), 11, new RectangleF(286, y + 5, 98, 24), _palette.Primary, FontStyle.Bold, StringAlignment.Far);
                var configuration = _settings.IsEnglish ? row.Configuration.Replace("普通", "Standard").Replace("未记录", "Unknown") : row.Configuration;
                DrawText(graphics, configuration, 8, new RectangleF(36, y + 28, 348, 19), _palette.Muted, FontStyle.Regular);
                var line = _sessionThreads ? T("独立请求用量 · ", "Own request usage · ") + row.Total.ToString("N0") :
                    T("输入 ", "In ") + FormatTokenCount(row.Input) + T("  输出 ", "  Out ") + FormatTokenCount(row.Output) +
                    T("  缓存 ", "  Cache ") + FormatTokenCount(row.Cached) + T("  推理 ", "  Reason ") + FormatTokenCount(row.Reasoning);
                DrawText(graphics, line, 7.6f, new RectangleF(36, y + 48, 348, 18), _palette.Text, FontStyle.Regular);
                if (!_sessionThreads)
                    DrawText(graphics, T("主会话 ", "Main ") + FormatTokenCount(row.Main) + T(" · 子智能体 ", " · Agents ") + FormatTokenCount(row.Children),
                        7.6f, new RectangleF(36, y + 65, 348, 17), _palette.Muted, FontStyle.Regular);
                y += 90;
            }
            DrawText(graphics, rows.Count > 3 ? T("滚轮翻页 · ", "Scroll · ") + (_sessionOffset + 1) + "–" + Math.Min(rows.Count, _sessionOffset + 3) + "/" + rows.Count :
                T("Fast 为日志记录的请求设置", "Fast is the recorded request setting"), 8,
                new RectangleF(24, 484, 372, 22), _palette.Muted, FontStyle.Regular);
            DrawText(graphics, _session.Status == "partial" ? T("部分缺口无法归属，已保留在总量中", "Unattributed gaps remain in the total") :
                T("缓存包含于输入，推理包含于输出", "Cache is included in input; reasoning in output"), 7.8f,
                new RectangleF(24, 511, 372, 22), _palette.Muted, FontStyle.Regular);
            DrawDivider(graphics, 536);
            DrawText(graphics, T("账号额度  ←", "←  Account quota"), 8.5f, new RectangleF(24, 549, 260, 30), _palette.Primary, FontStyle.Regular);
            DrawText(graphics, T("退出", "Quit"), 10, _exitBounds, _palette.Text, FontStyle.Regular, StringAlignment.Far);
        }
        private void DrawSessionTab(Graphics graphics, Rectangle bounds, string title, bool selected)
        {
            if (selected) using (var brush = new SolidBrush(Color.FromArgb(30, _palette.Primary))) graphics.FillRoundedRectangle(brush, bounds, 7);
            DrawText(graphics, title, 9, bounds, selected ? _palette.Primary : _palette.Muted, FontStyle.Regular, StringAlignment.Center);
        }
        private void HandleSessionClick(Point point)
        {
            if (_backBounds.Contains(point) || new Rectangle(24, 549, 260, 30).Contains(point)) _showSession = false;
            else if (_sessionModelBounds.Contains(point)) { _sessionThreads = false; _sessionOffset = 0; }
            else if (_sessionThreadBounds.Contains(point)) { _sessionThreads = true; _sessionOffset = 0; }
            Invalidate();
        }
        protected override void OnMouseWheel(MouseEventArgs e)
        {
            base.OnMouseWheel(e);
            if (!_showSession) return;
            _sessionOffset = Math.Max(0, _sessionOffset + (e.Delta < 0 ? 1 : -1));
            Invalidate();
        }
    }
}
