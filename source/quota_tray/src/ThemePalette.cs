using System;
using System.Collections.Generic;
using System.Drawing;
using Microsoft.Win32;

namespace CodexQuotaTray
{
    internal sealed class ThemePalette
    {
        public string Id { get; private set; }
        public string ChineseName { get; private set; }
        public string EnglishName { get; private set; }
        public bool IsLight { get; private set; }
        public Color Background { get; private set; }
        public Color Surface { get; private set; }
        public Color Text { get; private set; }
        public Color Muted { get; private set; }
        public Color Faint { get; private set; }
        public Color Divider { get; private set; }
        public Color Track { get; private set; }
        public Color Primary { get; private set; }
        public Color Secondary { get; private set; }
        public Color Success { get; private set; }
        public Color Warning { get; private set; }
        public Color Danger { get; private set; }

        public string Name(bool english)
        {
            return english ? EnglishName : ChineseName;
        }

        public Color QuotaColor(int remainingPercent)
        {
            if (remainingPercent <= 10) return Danger;
            if (remainingPercent <= 20) return Warning;
            return Primary;
        }

        public static IList<ThemePalette> Presets()
        {
            return new[]
            {
                Create("mono", "黑白", "Black & white", "#171717", "#242424", "#F5F5F5", "#A3A3A3", "#56CF97", "#F4A449", "#FF625C"),
                Create("glacier", "冰川蓝", "Glacier", "#18202E", "#222E40", "#90B8F8", "#7EDDDD", "#73CDB4", "#E5BD80", "#EE8994"),
                Create("rose", "玫瑰粉", "Rose", "#2A1E27", "#382933", "#EBA1B4", "#BCABEC", "#8BC5B0", "#E3C184", "#ED8591"),
                Create("champagne", "香槟金", "Champagne", "#27231B", "#342F24", "#E3C48D", "#A6C3AE", "#91C4A8", "#E8B875", "#E79587")
            };
        }

        public static string NormalizeId(string id)
        {
            // Keep the same selected slot when loading the four previous presets.
            switch ((id ?? "").ToLowerInvariant())
            {
                case "cyan": return "mono";
                case "aurora": return "glacier";
                case "emerald": return "rose";
                case "sunset": return "champagne";
                default: return (id ?? "").ToLowerInvariant();
            }
        }

        public static bool IsKnownTheme(string id)
        {
            id = NormalizeId(id);
            if (string.Equals(id, "custom", StringComparison.OrdinalIgnoreCase)) return true;
            foreach (var theme in Presets())
            {
                if (string.Equals(theme.Id, id, StringComparison.OrdinalIgnoreCase)) return true;
            }
            return false;
        }

        public static ThemePalette FromSettings(AppSettings settings)
        {
            ThemePalette selected = null;
            var themeId = NormalizeId(settings.ThemeId);
            if (string.Equals(settings.ThemeId, "custom", StringComparison.OrdinalIgnoreCase))
            {
                selected = Create("custom", "自定义", "Custom", "#1A1F25", "#232A32",
                    ColorToHex(settings.CustomPrimary), ColorToHex(settings.CustomSecondary),
                    "#56CF97", "#F4A449", "#FF625C");
            }
            else
            {
                foreach (var theme in Presets())
                {
                    if (!string.Equals(theme.Id, themeId, StringComparison.OrdinalIgnoreCase)) continue;
                    selected = theme;
                    break;
                }
            }

            if (selected == null) selected = Presets()[0];
            var useLight = string.Equals(settings.AppearanceMode, "light", StringComparison.OrdinalIgnoreCase) ||
                (string.Equals(settings.AppearanceMode, "system", StringComparison.OrdinalIgnoreCase) && SystemUsesLightAppearance());
            return WithAppearance(selected, useLight);
        }

        public static bool SystemUsesLightAppearance()
        {
            return ReadWindowsThemeValue("AppsUseLightTheme");
        }

        public static bool SystemUsesLightTaskbar()
        {
            return ReadWindowsThemeValue("SystemUsesLightTheme");
        }

        private static bool ReadWindowsThemeValue(string valueName)
        {
            try
            {
                using (var key = Registry.CurrentUser.OpenSubKey(@"Software\Microsoft\Windows\CurrentVersion\Themes\Personalize"))
                {
                    var value = key == null ? null : key.GetValue(valueName);
                    return value != null && Convert.ToInt32(value) != 0;
                }
            }
            catch
            {
                return false;
            }
        }

        internal static ThemePalette WithAppearance(ThemePalette source, bool light)
        {
            var monochrome = source.Id == "mono";
            return new ThemePalette
            {
                Id = source.Id,
                ChineseName = source.ChineseName,
                EnglishName = source.EnglishName,
                IsLight = light,
                Background = monochrome ? Parse(light ? "#FAFAFA" : "#171717") : Mix(light ? Parse("#F4F7FA") : source.Background, source.Primary, light ? .09 : .18),
                Surface = monochrome ? Parse(light ? "#FFFFFF" : "#242424") : Mix(light ? Parse("#FFFFFF") : source.Surface, source.Primary, light ? .045 : .11),
                Text = monochrome ? Parse(light ? "#171717" : "#F5F5F5") : (light ? Parse("#18212B") : source.Text),
                Muted = monochrome ? Parse(light ? "#5C5C5C" : "#B0B0B0") : (light ? Parse("#5D6A78") : source.Muted),
                Faint = monochrome ? Parse(light ? "#8A8A8A" : "#808080") : (light ? Parse("#8A96A3") : source.Faint),
                Divider = monochrome ? Parse(light ? "#D9D9D9" : "#3D3D3D") : Mix(light ? Parse("#D6DEE6") : source.Divider, source.Primary, .22),
                Track = monochrome ? Parse(light ? "#E3E3E3" : "#484848") : Mix(light ? Parse("#DCE3EA") : source.Track, source.Secondary, .16),
                Primary = monochrome ? Parse(light ? "#171717" : "#F5F5F5") : (light ? ReadableAccent(source.Primary) : source.Primary),
                Secondary = monochrome ? Parse(light ? "#626262" : "#A3A3A3") : (light ? ReadableAccent(source.Secondary) : source.Secondary),
                Success = source.Success,
                Warning = source.Warning,
                Danger = source.Danger
            };
        }

        internal static Color Mix(Color from, Color to, double amount)
        {
            return Color.FromArgb((int)Math.Round(from.R + (to.R - from.R) * amount),
                (int)Math.Round(from.G + (to.G - from.G) * amount),
                (int)Math.Round(from.B + (to.B - from.B) * amount));
        }

        private static Color ReadableAccent(Color color)
        {
            // Colored labels on the light surfaces need more than the bright swatch color.
            while (Luminance(color) > .14) color = Mix(color, Color.Black, .06);
            return color;
        }

        private static double Luminance(Color color)
        {
            Func<byte, double> channel = value => value / 255d <= .04045 ? value / 255d / 12.92 : Math.Pow((value / 255d + .055) / 1.055, 2.4);
            return .2126 * channel(color.R) + .7152 * channel(color.G) + .0722 * channel(color.B);
        }

        private static ThemePalette Create(
            string id,
            string chineseName,
            string englishName,
            string background,
            string surface,
            string primary,
            string secondary,
            string success,
            string warning,
            string danger)
        {
            return new ThemePalette
            {
                Id = id,
                ChineseName = chineseName,
                EnglishName = englishName,
                IsLight = false,
                Background = Parse(background),
                Surface = Parse(surface),
                Text = Parse("#F2F5F8"),
                Muted = Parse("#A5AFBA"),
                Faint = Parse("#717B86"),
                Divider = Parse("#39424B"),
                Track = Parse("#414A54"),
                Primary = Parse(primary),
                Secondary = Parse(secondary),
                Success = Parse(success),
                Warning = Parse(warning),
                Danger = Parse(danger)
            };
        }

        private static Color Parse(string value)
        {
            return ColorTranslator.FromHtml(value);
        }

        private static string ColorToHex(Color color)
        {
            return "#" + color.R.ToString("X2") + color.G.ToString("X2") + color.B.ToString("X2");
        }
    }
}
