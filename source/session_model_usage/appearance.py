"""Shared interface colors from the native settings, with bounded local reads."""
from pathlib import Path
from .platform_win import state_directory

PRESETS = {
    'mono': ('#F5F5F5', '#A3A3A3', '#171717', '#242424'),
    'glacier': ('#90B8F8', '#7EDDDD', '#18202E', '#222E40'),
    'rose': ('#EBA1B4', '#BCABEC', '#2A1E27', '#382933'),
    'champagne': ('#E3C48D', '#A6C3AE', '#27231B', '#342F24'),
}
LEGACY_PRESETS = {'cyan': 'mono', 'aurora': 'glacier', 'emerald': 'rose', 'sunset': 'champagne'}

def preferences(folder: Path | None = None) -> dict:
    try:
        with ((folder or state_directory()) / 'settings.ini').open('rb') as stream:
            raw = stream.read(65537)
        if len(raw) > 65536: return {}
        return {k.strip().casefold(): v.strip() for line in raw.decode('utf-8-sig').splitlines()
                for k, sep, v in [line.partition('=')] if sep}
    except (OSError, UnicodeError): return {}

def rgb(color: str) -> tuple[int, int, int]:
    return tuple(int(color[i:i+2], 16) for i in (1, 3, 5))

def mix(first: str, second: str, ratio: float) -> str:
    return '#' + ''.join(f'{round(a+(b-a)*ratio):02X}' for a,b in zip(rgb(first),rgb(second)))

def palette(dark: bool, folder: Path | None = None) -> dict:
    values = preferences(folder)
    mode = values.get('appearance', 'system')
    if mode in ('light', 'dark'): dark = mode == 'dark'
    theme = values.get('theme', 'mono').casefold()
    theme = LEGACY_PRESETS.get(theme, theme)
    primary, secondary, surface, card = PRESETS.get(theme, PRESETS['mono'])
    if theme == 'mono' or (theme not in PRESETS and theme != 'custom'):
        return {'dark': dark, 'surface': '#171717' if dark else '#FAFAFA',
                'card': '#242424' if dark else '#FFFFFF',
                'text': '#F5F5F5' if dark else '#171717', 'muted': '#B0B0B0' if dark else '#5C5C5C',
                'edge': '#3D3D3D' if dark else '#D9D9D9', 'segment': '#484848' if dark else '#E3E3E3',
                'accent': primary if dark else '#171717', 'secondary': secondary if dark else '#626262'}
    if theme == 'custom':
        primary, secondary = '#43C2DD', '#41AEEB'
        surface, card = '#1A1F25', '#232A32'
        def custom(key, fallback):
            try:
                value = int(values[key])
                if -(2**31) <= value < 2**32: return f'#{value & 0xFFFFFF:06X}'
            except (KeyError, ValueError): pass
            return fallback
        primary, secondary = custom('customprimary', primary), custom('customsecondary', secondary)
    if not dark:
        def readable(color):
            def luminance(c):
                channels = [v/255 for v in rgb(c)]
                channels = [v/12.92 if v<=.04045 else ((v+.055)/1.055)**2.4 for v in channels]
                return sum(v*w for v,w in zip(channels, (.2126,.7152,.0722)))
            while luminance(color) > .14: color = mix(color, '#000000', .06)
            return color
        accent, second = readable(primary), readable(secondary)
    else: accent, second = primary, secondary
    return {'dark': dark, 'surface': mix(surface if dark else '#F4F7FA', primary, .18 if dark else .09),
            'card': mix(card if dark else '#FFFFFF', primary, .11 if dark else .045),
            'text': '#F2F5F8' if dark else '#18212B', 'muted': '#A5AFBA' if dark else '#5D6A78',
            'edge': mix('#39424B' if dark else '#D6DEE6', primary, .22),
            'segment': mix('#414A54' if dark else '#DCE3EA', secondary, .16),
            'accent': accent, 'secondary': second}
