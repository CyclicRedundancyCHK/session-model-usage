import tempfile
import unittest
from pathlib import Path
from session_model_usage.appearance import palette, preferences, rgb

class AppearanceTests(unittest.TestCase):
    def test_themes_change_surfaces_and_accent_in_both_appearances(self):
        with tempfile.TemporaryDirectory() as name:
            folder = Path(name)
            for mode in ('dark', 'light'):
                colors = []
                for theme in ('mono', 'glacier', 'rose', 'champagne'):
                    (folder / 'settings.ini').write_text(f'Theme={theme}\nAppearance={mode}\n')
                    colors.append(palette(mode != 'dark', folder))
                self.assertEqual(len({c['surface'] for c in colors}), 4)
                self.assertEqual(len({c['card'] for c in colors}), 4)
                self.assertEqual(len({c['accent'] for c in colors}), 4)
                self.assertTrue(all(c['dark'] == (mode == 'dark') for c in colors))

    def test_custom_argb_and_system_appearance(self):
        with tempfile.TemporaryDirectory() as name:
            folder = Path(name)
            (folder/'settings.ini').write_text('Theme=custom\nAppearance=system\nCustomPrimary=-65536\nCustomSecondary=-16776961\n')
            self.assertEqual(palette(True, folder)['accent'], '#FF0000')
            self.assertEqual(palette(True, folder)['secondary'], '#0000FF')
            self.assertFalse(palette(False, folder)['dark'])

    def test_light_accent_labels_have_readable_contrast(self):
        with tempfile.TemporaryDirectory() as name:
            folder = Path(name)
            (folder/'settings.ini').write_text('Theme=custom\nAppearance=light\nCustomPrimary=-1\nCustomSecondary=-256\n')
            c = palette(False, folder)
            def lum(color):
                parts = [x/255 for x in rgb(color)]
                return sum((x/12.92 if x <= .04045 else ((x+.055)/1.055)**2.4)*w for x,w in zip(parts, (.2126,.7152,.0722)))
            self.assertGreaterEqual((lum(c['card'])+.05)/(lum(c['accent'])+.05), 4.5)

    def test_invalid_and_oversized_settings_fall_back_without_reading_more(self):
        with tempfile.TemporaryDirectory() as name:
            folder = Path(name)
            (folder/'settings.ini').write_text('Theme=custom\nCustomPrimary=invalid\nCustomSecondary=99999999999999\n')
            self.assertEqual(palette(True, folder)['accent'], '#43C2DD')
            (folder/'settings.ini').write_bytes(b'X'*65537)
            self.assertEqual(preferences(folder), {})

if __name__ == '__main__': unittest.main()
