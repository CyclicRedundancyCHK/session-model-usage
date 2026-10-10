using System;
using System.Drawing;
using System.Runtime.InteropServices;
using System.Windows.Forms;

namespace CodexQuotaTray
{
    // No activation is needed: inspect only button edges while a popup is visible.
    internal sealed class PopupClickWatcher : IDisposable
    {
        [DllImport("user32.dll")] private static extern short GetAsyncKeyState(int key);
        [DllImport("user32.dll")] private static extern IntPtr WindowFromPoint(Point point);
        [DllImport("user32.dll")] private static extern IntPtr GetAncestor(IntPtr window, uint flags);
        private readonly Timer _timer = new Timer { Interval = 16 };
        private readonly Func<Point, IntPtr, bool> _inside;
        private readonly Action _outside;
        private int _buttons;
        internal PopupClickWatcher(Func<Point, IntPtr, bool> inside, Action outside)
        {
            _inside = inside; _outside = outside;
            _timer.Tick += delegate { var point = Cursor.Position; Sample(Buttons(), point, GetAncestor(WindowFromPoint(point), 2)); };
        }
        private static int Buttons()
        {
            return ((GetAsyncKeyState(1) & 0x8000) != 0 ? 1 : 0) |
                ((GetAsyncKeyState(2) & 0x8000) != 0 ? 2 : 0) |
                ((GetAsyncKeyState(4) & 0x8000) != 0 ? 4 : 0);
        }
        internal static IntPtr Root(Control control) { return GetAncestor(control.Handle, 2); }
        internal void Start() { _buttons = Buttons(); _timer.Start(); }
        internal void Stop() { _timer.Stop(); }
        internal void Sample(int buttons, Point point, IntPtr root)
        {
            var pressed = buttons & ~_buttons;
            _buttons = buttons;
            if (pressed != 0 && !_inside(point, root)) _outside();
        }
        public void Dispose() { _timer.Stop(); _timer.Dispose(); }
    }
}
