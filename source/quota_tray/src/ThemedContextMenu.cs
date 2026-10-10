using System;
using System.ComponentModel;
using System.Drawing;
using System.Drawing.Drawing2D;
using System.Drawing.Text;
using System.Linq;
using System.Windows.Forms;

namespace CodexQuotaTray
{
    internal sealed class ThemedContextMenu : ContextMenuStrip
    {
        private AppSettings _settings;
        private readonly bool _actions;
        private Font _bodyFont;
        private float _dpiScale = 1f;
        private bool _preview;
        private bool _updatingRegion;
        private Size _regionSize;
        private float _regionRadius;
        private bool _regionNative;
        private PopupClickWatcher _dismissal;
        internal ThemePalette Palette { get; private set; }
        internal bool GlassActive { get; private set; }
        internal bool NativeRoundedCorners { get; private set; }
        internal float DrawingScale { get { return _dpiScale * _settings.PopupScalePercent / 100f; } }
        internal float CornerRadius { get { return NativeRoundedCorners ? 8f * _dpiScale : 18f * DrawingScale; } }

        internal ThemedContextMenu(AppSettings settings, bool actions = false)
        {
            _settings = settings;
            _actions = actions;
            AutoSize = false;
            // Modal menu auto-close can eat the taskbar click, or reopen on its
            // release. Let the original button toggle and watch outside clicks.
            AutoClose = false;
            ShowImageMargin = ShowCheckMargin = false;
            ShowItemToolTips = true;
            DropShadowEnabled = true;
            CanOverflow = false;
            LayoutStyle = ToolStripLayoutStyle.VerticalStackWithOverflow;
            Renderer = new ThemedMenuRenderer(this);
            ApplyTheme();
            if (_actions) WatchOutsideClicks(null, null);
        }

        internal void WatchOutsideClicks(Control anchor, Func<Rectangle> anchorBounds)
        {
            if (_dismissal != null) _dismissal.Dispose();
            _dismissal = new PopupClickWatcher((point, root) =>
                root == Handle || (anchor != null && root == PopupClickWatcher.Root(anchor) && anchor.RectangleToScreen(anchorBounds()).Contains(point) &&
                anchor.RectangleToScreen(anchor.ClientRectangle).Contains(point)), Close);
        }

        protected override CreateParams CreateParams
        {
            get { var p = base.CreateParams; p.ExStyle |= 0x08000000; return p; }
        }

        protected override void WndProc(ref Message message)
        {
            if (message.Msg == 0x0021) { message.Result = new IntPtr(3); return; } // MA_NOACTIVATE
            base.WndProc(ref message);
        }

        protected override void OnVisibleChanged(EventArgs e)
        {
            base.OnVisibleChanged(e);
            if (_dismissal == null) return;
            if (Visible) _dismissal.Start(); else _dismissal.Stop();
        }

        protected override void OnItemClicked(ToolStripItemClickedEventArgs e)
        {
            if (_actions && e.ClickedItem.Enabled) Close();
            base.OnItemClicked(e);
            if (e.ClickedItem.Enabled && e.ClickedItem.Tag is TaskNavigationTarget) Close();
        }

        internal void ApplySettings(AppSettings settings)
        {
            _settings = settings ?? new AppSettings();
            if (IsHandleCreated) ApplyMaterial();
            ApplyTheme();
        }

        internal void ApplyTheme()
        {
            Palette = ThemePalette.FromSettings(_settings);
            BackColor = Palette.Background;
            ForeColor = Palette.Text;
            var fontSize = 12f * DrawingScale * _settings.FontScalePercent / 100f;
            if (_bodyFont == null || Math.Abs(_bodyFont.Size - fontSize) > .001f)
            {
                var previous = _bodyFont;
                _bodyFont = new Font("Microsoft YaHei UI", fontSize, FontStyle.Regular, GraphicsUnit.Pixel);
                Font = _bodyFont;
                if (previous != null) previous.Dispose();
            }
            var scale = DrawingScale;
            Padding = new Padding((int)Math.Round(8 * scale));
            var width = (int)Math.Round((_actions ? 216 : 244) * scale);
            var height = Padding.Vertical;
            foreach (ToolStripItem item in Items)
            {
                item.AutoSize = false;
                item.Margin = Padding.Empty;
                item.Size = new Size(width - Padding.Horizontal, (int)Math.Round(
                    (item is ToolStripSeparator ? 8 : Equals(item.Tag, "heading") ? 28 : _actions ? 34 : 38) * scale));
                height += item.Height;
            }
            Size = new Size(width, height);
            PerformLayout();
            UpdateRegion();
            Invalidate();
        }

        private void ApplyMaterial()
        {
            NativeRoundedCorners = !_preview && AcrylicEffect.ConfigureRoundedCorners(Handle);
            GlassActive = !_preview && _settings.GlassEnabled && AcrylicEffect.Apply(Handle, true);
            if (!GlassActive) AcrylicEffect.Apply(Handle, false);
            UpdateRegion();
        }

        protected override void OnHandleCreated(EventArgs e)
        {
            base.OnHandleCreated(e);
            if (!_preview)
            {
                _dpiScale = GraphicsExtensions.GetDpiScale(Handle);
                ApplyMaterial();
            }
        }

        protected override void OnOpening(CancelEventArgs e)
        {
            _dpiScale = GraphicsExtensions.GetDpiScale(Handle);
            ApplyMaterial();
            ApplyTheme();
            base.OnOpening(e);
        }

        protected override void OnSizeChanged(EventArgs e)
        {
            base.OnSizeChanged(e);
            if (_settings != null) UpdateRegion();
        }

        private void UpdateRegion()
        {
            if (_updatingRegion || Width <= 0 || Height <= 0 ||
                (_regionSize == Size && _regionRadius == CornerRadius && _regionNative == NativeRoundedCorners)) return;
            _updatingRegion = true;
            try
            {
                var previous = Region;
                if (NativeRoundedCorners) Region = null;
                else
                    using (var path = GraphicsExtensions.CreateRoundedPath(new RectangleF(0, 0, Width, Height), CornerRadius))
                        Region = new Region(path);
                if (previous != null && previous != Region) previous.Dispose();
                _regionSize = Size; _regionRadius = CornerRadius; _regionNative = NativeRoundedCorners;
            }
            finally { _updatingRegion = false; }
        }

        internal Bitmap RenderPreview(float dpiScale)
        {
            _preview = true;
            _dpiScale = dpiScale;
            GlassActive = NativeRoundedCorners = false;
            // Complete native menu/font initialization before allocating the paint target.
            var handle = Handle;
            ApplyTheme();
            var bitmap = new Bitmap(Width, Height);
            // Hidden native drop-downs may omit their children in DrawToBitmap.
            // Use the same renderer explicitly so every preview includes all rows.
            using (var graphics = Graphics.FromImage(bitmap))
            {
                Renderer.DrawToolStripBackground(new ToolStripRenderEventArgs(graphics, this));
                foreach (ToolStripItem item in Items)
                {
                    var saved = graphics.Save();
                    graphics.TranslateTransform(item.Bounds.Left, item.Bounds.Top);
                    if (item is ToolStripSeparator)
                        Renderer.DrawSeparator(new ToolStripSeparatorRenderEventArgs(graphics, (ToolStripSeparator)item, false));
                    else
                    {
                        Renderer.DrawMenuItemBackground(new ToolStripItemRenderEventArgs(graphics, item));
                        Renderer.DrawItemText(new ToolStripItemTextRenderEventArgs(graphics, item, item.Text, new Rectangle(Point.Empty, item.Size),
                            ForeColor, Font, TextFormatFlags.Default));
                    }
                    graphics.Restore(saved);
                }
                Renderer.DrawToolStripBorder(new ToolStripRenderEventArgs(graphics, this));
            }
            return bitmap;
        }

        protected override void Dispose(bool disposing)
        {
            if (disposing && _dismissal != null) { _dismissal.Dispose(); _dismissal = null; }
            base.Dispose(disposing);
            if (disposing && _bodyFont != null) { _bodyFont.Dispose(); _bodyFont = null; }
        }

        private sealed class ThemedMenuRenderer : ToolStripRenderer
        {
            private readonly ThemedContextMenu _menu;
            internal ThemedMenuRenderer(ThemedContextMenu menu) { _menu = menu; }

            protected override void OnRenderToolStripBackground(ToolStripRenderEventArgs e)
            {
                var graphics = e.Graphics;
                graphics.SmoothingMode = SmoothingMode.AntiAlias;
                if (_menu.GlassActive)
                {
                    graphics.CompositingMode = CompositingMode.SourceCopy;
                    graphics.Clear(Color.Transparent);
                    graphics.CompositingMode = CompositingMode.SourceOver;
                }
                var alpha = _menu.GlassActive ? (int)Math.Round(Math.Max(5, Math.Min(95, _menu._settings.GlassOpacityPercent)) * 2.55) : 255;
                using (var path = GraphicsExtensions.CreateRoundedPath(e.ToolStrip.ClientRectangle, _menu.CornerRadius))
                using (var brush = new SolidBrush(Color.FromArgb(alpha, _menu.Palette.Background))) graphics.FillPath(brush, path);
            }

            protected override void OnRenderToolStripBorder(ToolStripRenderEventArgs e)
            {
                var inset = .5f * _menu._dpiScale;
                var bounds = new RectangleF(inset, inset, e.ToolStrip.Width - 2 * inset, e.ToolStrip.Height - 2 * inset);
                using (var path = GraphicsExtensions.CreateRoundedPath(bounds, Math.Max(0, _menu.CornerRadius - inset)))
                using (var pen = new Pen(Color.FromArgb(110, _menu.Palette.Divider), _menu._dpiScale)) e.Graphics.DrawPath(pen, path);
            }

            protected override void OnRenderMenuItemBackground(ToolStripItemRenderEventArgs e)
            {
                if (!(e.Item.Tag is TaskNavigationTarget) && !(_menu._actions && e.Item.Enabled)) return;
                var scale = _menu.DrawingScale;
                var bounds = new RectangleF(8 * scale - e.Item.Bounds.Left, 2 * scale, _menu.Width - 16 * scale, e.Item.Height - 4 * scale);
                var accent = Equals(e.Item.Tag, "danger") ? _menu.Palette.Danger : _menu.Palette.Primary;
                var color = e.Item.Selected ? ThemePalette.Mix(_menu.Palette.Surface, accent, _menu.Palette.IsLight ? .07 : .12) : _menu.Palette.Surface;
                using (var path = GraphicsExtensions.CreateRoundedPath(bounds, 8 * scale))
                using (var brush = new SolidBrush(Color.FromArgb(_menu.GlassActive ? 200 : 255, color))) e.Graphics.FillPath(brush, path);
                var target = e.Item.Tag as TaskNavigationTarget;
                if (target == null) return;
                var dot = target.State == CodexTaskState.None ? _menu.Palette.Faint : TaskbarWidgetForm.StatusColor(target.State, _menu.Palette.IsLight);
                using (var brush = new SolidBrush(dot)) e.Graphics.FillEllipse(brush, 18 * scale - e.Item.Bounds.Left, (e.Item.Height - 5 * scale) / 2f, 5 * scale, 5 * scale);
            }

            protected override void OnRenderItemText(ToolStripItemTextRenderEventArgs e)
            {
                var scale = _menu.DrawingScale;
                var heading = Equals(e.Item.Tag, "heading");
                var row = e.Item.Tag is TaskNavigationTarget;
                var left = (row ? 32 : 16) * scale - e.Item.Bounds.Left;
                var bounds = new RectangleF(left, 0, _menu.Width - e.Item.Bounds.Left - left - 16 * scale, e.Item.Height);
                e.Graphics.TextRenderingHint = TextRenderingHint.AntiAliasGridFit;
                using (var format = new StringFormat { LineAlignment = StringAlignment.Center, FormatFlags = StringFormatFlags.NoWrap,
                    Trimming = StringTrimming.EllipsisCharacter, HotkeyPrefix = HotkeyPrefix.None })
                using (var brush = new SolidBrush(!e.Item.Enabled ? _menu.Palette.Muted : Equals(e.Item.Tag, "danger") ? _menu.Palette.Danger : _menu.Palette.Text))
                using (var font = heading ? new Font(_menu.Font, FontStyle.Bold) : new Font(_menu.Font, FontStyle.Regular))
                    e.Graphics.DrawString(e.Text, font, brush, bounds, format);
                if (heading)
                {
                    var count = _menu.Items.Cast<ToolStripItem>().Count(item => item.Tag is TaskNavigationTarget);
                    var countBounds = new RectangleF(_menu.Width - 36 * scale - e.Item.Bounds.Left, 0, 20 * scale, e.Item.Height);
                    using (var brush = new SolidBrush(_menu.Palette.Faint))
                    using (var format = new StringFormat { Alignment = StringAlignment.Far, LineAlignment = StringAlignment.Center })
                        e.Graphics.DrawString(count.ToString(), _menu.Font, brush, countBounds, format);
                }
            }

            protected override void OnRenderSeparator(ToolStripSeparatorRenderEventArgs e)
            {
                using (var pen = new Pen(Color.FromArgb(120, _menu.Palette.Divider)))
                    e.Graphics.DrawLine(pen, 16 * _menu.DrawingScale - e.Item.Bounds.Left, e.Item.Height / 2f,
                        _menu.Width - 16 * _menu.DrawingScale - e.Item.Bounds.Left, e.Item.Height / 2f);
            }
        }
    }
}
