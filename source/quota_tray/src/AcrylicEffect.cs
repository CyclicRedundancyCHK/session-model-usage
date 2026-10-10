using System;
using System.Runtime.InteropServices;

namespace CodexQuotaTray
{
    internal static class AcrylicEffect
    {
        [StructLayout(LayoutKind.Sequential)]
        private struct AccentPolicy
        {
            public int State;
            public int Flags;
            public int GradientColor;
            public int AnimationId;
        }

        [StructLayout(LayoutKind.Sequential)]
        private struct WindowCompositionAttributeData
        {
            public int Attribute;
            public IntPtr Data;
            public int Size;
        }

        [DllImport("user32.dll")]
        private static extern int SetWindowCompositionAttribute(IntPtr window, ref WindowCompositionAttributeData data);

        [DllImport("dwmapi.dll")]
        private static extern int DwmSetWindowAttribute(IntPtr window, int attribute, ref int value, int size);

        [StructLayout(LayoutKind.Sequential)]
        private struct Margins
        {
            public int Left, Right, Top, Bottom;
        }

        [DllImport("dwmapi.dll")]
        private static extern int DwmExtendFrameIntoClientArea(IntPtr window, ref Margins margins);

        internal static bool Apply(IntPtr window, bool enabled)
        {
            if (window == IntPtr.Zero) return false;
            var rounded = ConfigureRoundedCorners(window);
            // A successful blur call with no native corner support would
            // restore a rectangular backdrop outside the fallback border.
            var blurEnabled = enabled && rounded;
            // The canvas owns tint and opacity. Acrylic adds its own opaque/noisy
            // tint, which can hide the desktop even at the minimum slider value.
            try
            {
                var extent = blurEnabled ? -1 : 0;
                var margins = new Margins { Left = extent, Right = extent, Top = extent, Bottom = extent };
                DwmExtendFrameIntoClientArea(window, ref margins);
                const int systemBackdropType = 38;
                var noBackdrop = 1;
                DwmSetWindowAttribute(window, systemBackdropType, ref noBackdrop, sizeof(int));
            }
            catch { }

            var policy = new AccentPolicy
            {
                State = blurEnabled ? 3 : 0,
                Flags = 0,
                GradientColor = 0,
                AnimationId = 0
            };
            var size = Marshal.SizeOf(typeof(AccentPolicy));
            var pointer = Marshal.AllocHGlobal(size);
            try
            {
                Marshal.StructureToPtr(policy, pointer, false);
                var data = new WindowCompositionAttributeData { Attribute = 19, Data = pointer, Size = size };
                if (SetWindowCompositionAttribute(window, ref data) != 0) return !enabled || blurEnabled;
            }
            catch
            {
            }
            finally
            {
                Marshal.FreeHGlobal(pointer);
            }

            // Without blur support the form uses its readable solid fallback.
            return false;
        }

        internal static bool ConfigureRoundedCorners(IntPtr window)
        {
            if (window == IntPtr.Zero) return false;
            try
            {
                const int windowCornerPreference = 33;
                const int round = 2;
                var preference = round;
                var rounded = DwmSetWindowAttribute(window, windowCornerPreference, ref preference, sizeof(int)) == 0;
                const int borderColor = 34;
                var noBorder = unchecked((int)0xfffffffe);
                DwmSetWindowAttribute(window, borderColor, ref noBorder, sizeof(int));
                return rounded;
            }
            catch
            {
                return false;
            }
        }

    }
}
