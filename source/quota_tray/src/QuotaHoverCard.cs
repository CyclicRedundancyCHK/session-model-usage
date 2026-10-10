using System;
using System.Drawing;
using System.Drawing.Drawing2D;
using System.Drawing.Text;
using System.Windows.Forms;

namespace CodexQuotaTray
{
    internal sealed class QuotaHoverCard : Form
    {
        private AppSettings _settings = new AppSettings();
        private ThemePalette _palette;
        private float _dpi = 1f;
        private float DrawingScale { get { return _dpi * _settings.PopupScalePercent / 100f; } }
        private string _title = "", _balance = "", _body = "", _hint = "";
        private int _titleHeight, _balanceHeight, _bodyHeight, _hintHeight;
        private bool _glass, _rounded;
        private float Radius { get { return _rounded ? 8 * _dpi : 18 * DrawingScale; } }

        internal QuotaHoverCard()
        {
            AutoScaleMode = AutoScaleMode.None;
            FormBorderStyle = FormBorderStyle.None;
            ShowInTaskbar = false;
            StartPosition = FormStartPosition.Manual;
            DoubleBuffered = true;
            _palette = ThemePalette.FromSettings(_settings);
        }

        protected override bool ShowWithoutActivation { get { return true; } }
        protected override CreateParams CreateParams
        {
            get { var p = base.CreateParams; p.ExStyle |= 0x08000080; return p; }
        }

        private Font TextFont(float size, FontStyle style)
        {
            return new Font("Microsoft YaHei UI", size * DrawingScale * _settings.FontScalePercent / 100f, style, GraphicsUnit.Pixel);
        }

        internal void Apply(string description, AppSettings settings, float dpi)
        {
            _settings = settings; _dpi = dpi;
            _palette = ThemePalette.FromSettings(settings);
            BackColor = _palette.Background;
            var lines = (description ?? "").Split(new[] { "\r\n", "\n", "\r" }, StringSplitOptions.None);
            var split = lines[0].IndexOf(" · ", StringComparison.Ordinal);
            _title = split < 0 ? lines[0] : lines[0].Substring(0, split);
            _balance = split < 0 ? "" : lines[0].Substring(split + 3);
            var body = "";
            _hint = "";
            for (var i = 1; i < lines.Length; i++)
            {
                if (i == lines.Length - 1 && (lines[i].StartsWith("点击", StringComparison.Ordinal) || lines[i].StartsWith("Click", StringComparison.Ordinal))) _hint = lines[i];
                else body += (body.Length == 0 ? "" : Environment.NewLine) + lines[i].Replace(" · Err ", Environment.NewLine + "Err ");
            }
            _body = body;
            var width = (int)Math.Round(320 * DrawingScale);
            using (var graphics = CreateGraphics())
            using (var title = TextFont(12, FontStyle.Bold))
            using (var balance = TextFont(16, FontStyle.Bold))
            using (var text = TextFont(12, FontStyle.Regular))
            using (var hint = TextFont(11, FontStyle.Regular))
            {
                var content = width - 32 * DrawingScale;
                _titleHeight = (int)Math.Ceiling(graphics.MeasureString(_title, title, (int)content).Height);
                _balanceHeight = string.IsNullOrEmpty(_balance) ? 0 : (int)Math.Ceiling(graphics.MeasureString(_balance, balance, (int)content).Height);
                _bodyHeight = string.IsNullOrEmpty(_body) ? 0 : (int)Math.Ceiling(graphics.MeasureString(_body, text, (int)content).Height);
                _hintHeight = string.IsNullOrEmpty(_hint) ? 0 : (int)Math.Ceiling(graphics.MeasureString(_hint, hint, (int)content).Height);
            }
            Size = new Size(width, (int)Math.Ceiling(28 * DrawingScale + _titleHeight + _balanceHeight + (_balanceHeight > 0 ? 8 * DrawingScale : 0) + _bodyHeight + (_bodyHeight > 0 ? 8 * DrawingScale : 0) +
                (_hintHeight > 0 ? 20 * DrawingScale + _hintHeight : 0)));
            AccessibleName = _title;
            AccessibleDescription = description;
            ApplyRegion();
            Invalidate();
        }

        private void ApplyRegion()
        {
            var previous = Region;
            if (_rounded) Region = null;
            else using (var path = GraphicsExtensions.CreateRoundedPath(ClientRectangle, Radius)) Region = new Region(path);
            if (previous != null && previous != Region) previous.Dispose();
        }

        internal void ShowAbove(Control owner)
        {
            _rounded = AcrylicEffect.ConfigureRoundedCorners(Handle);
            // Fall back to a rounded solid surface if DWM cannot round blur.
            _glass = _rounded && _settings.GlassEnabled && AcrylicEffect.Apply(Handle, true);
            if (!_glass) AcrylicEffect.Apply(Handle, false);
            ApplyRegion();
            var anchor = owner.RectangleToScreen(owner.ClientRectangle);
            var work = Screen.FromRectangle(anchor).WorkingArea;
            Location = new Point(Math.Max(work.Left, Math.Min(work.Right - Width, anchor.Left + (anchor.Width - Width) / 2)),
                Math.Max(work.Top, Math.Min(work.Bottom - Height, anchor.Top - Height - (int)Math.Round(8 * _dpi))));
            Show(owner);
        }

        private void PaintCard(Graphics g)
        {
            g.SmoothingMode = SmoothingMode.AntiAlias;
            g.TextRenderingHint = TextRenderingHint.AntiAliasGridFit;
            g.CompositingMode = CompositingMode.SourceCopy;
            g.Clear(Color.Transparent);
            g.CompositingMode = CompositingMode.SourceOver;
            var inset = .5f * _dpi;
            var bounds = new RectangleF(inset, inset, Width - 2 * inset, Height - 2 * inset);
            using (var path = GraphicsExtensions.CreateRoundedPath(bounds, Radius))
            using (var brush = new SolidBrush(Color.FromArgb(_glass ? (int)Math.Round(_settings.GlassOpacityPercent * 2.55) : 255, _palette.Background)))
            using (var edge = new Pen(Color.FromArgb(110, _palette.Divider), _dpi))
            { g.FillPath(brush, path); g.DrawPath(edge, path); }
            var x = 16 * DrawingScale; var y = 14 * DrawingScale; var width = Width - 2 * x;
            using (var title = TextFont(12, FontStyle.Bold))
            using (var balance = TextFont(16, FontStyle.Bold))
            using (var text = TextFont(12, FontStyle.Regular))
            using (var hint = TextFont(11, FontStyle.Regular))
            using (var foreground = new SolidBrush(_palette.Text))
            using (var muted = new SolidBrush(_palette.Muted))
            using (var accent = new SolidBrush(_palette.Primary))
            {
                g.DrawString(_title, title, foreground, new RectangleF(x, y, width, _titleHeight + 2));
                y += _titleHeight;
                if (_balanceHeight > 0)
                {
                    y += 8 * DrawingScale;
                    g.DrawString(_balance, balance, accent, new RectangleF(x, y, width, _balanceHeight + 2));
                    y += _balanceHeight;
                }
                if (_bodyHeight > 0) y += 8 * DrawingScale;
                g.DrawString(_body, text, muted, new RectangleF(x, y, width, _bodyHeight + 2));
                y += _bodyHeight;
                if (_hintHeight > 0)
                {
                    y += 10 * DrawingScale;
                    using (var pen = new Pen(_palette.Divider, _dpi)) g.DrawLine(pen, x, y, Width - x, y);
                    y += 10 * DrawingScale;
                    g.DrawString(_hint, hint, muted, new RectangleF(x, y, width, _hintHeight + 2));
                }
            }
        }

        protected override void OnPaint(PaintEventArgs e) { PaintCard(e.Graphics); }

        internal Bitmap RenderPreview(string description, AppSettings settings, float dpi)
        {
            _glass = _rounded = false;
            Apply(description, settings, dpi);
            var bitmap = new Bitmap(Width, Height);
            using (var g = Graphics.FromImage(bitmap)) PaintCard(g);
            return bitmap;
        }
    }
}
