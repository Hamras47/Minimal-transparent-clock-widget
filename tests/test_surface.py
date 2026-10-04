"""The face renderer: every font and colour on offer must draw, at every size."""

import unittest

import surface


class SurfaceTests(unittest.TestCase):
    def test_every_font_and_colour_renders(self):
        for font in surface.FONTS:
            for colour in surface.COLOURS:
                picture = surface.render(
                    "15:07", "Saturday, 26 September 2026", "light", 300, 120, 1.5,
                    hover=True, font=font, colour=colour,
                )
                self.assertEqual(len(picture.pixels), 300 * 120 * 4, (font, colour))

    def test_a_hidden_date_still_renders(self):
        picture = surface.render("15:07:04", "", "dark", 200, 80, 1.0, font="mono")
        self.assertGreater(picture.hit[2], picture.hit[0])

    def test_wider_means_bigger_text(self):
        small = surface.fit_size("15:07", "Saturday, 26 September 2026", 150, 1.0)
        huge = surface.fit_size("15:07", "Saturday, 26 September 2026", 620, 1.0)
        self.assertGreater(huge[1], small[1] * 2)

    def test_an_unknown_choice_falls_back(self):
        picture = surface.render("1:00 pm", "x", "light", 200, 80, font="nope", colour="nope")
        self.assertEqual(picture.width, 200)


if __name__ == "__main__":
    unittest.main()
