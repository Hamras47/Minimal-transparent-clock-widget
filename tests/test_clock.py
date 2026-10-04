"""The card's two lines, with no widget involved.

These run without a window, a clock or a desktop, which is the point of keeping the
wording in clock.py.
"""

import unittest
from datetime import datetime, timedelta

import clock


class DateTests(unittest.TestCase):
    def test_the_format_that_was_asked_for(self):
        self.assertEqual(
            clock.date_text(datetime(2026, 9, 26)), "Saturday, 26 September 2026"
        )

    def test_no_leading_zero_on_the_day(self):
        self.assertEqual(
            clock.date_text(datetime(2026, 9, 5)), "Saturday, 5 September 2026"
        )
        self.assertEqual(clock.date_text(datetime(2027, 1, 1)), "Friday, 1 January 2027")

    def test_the_last_day_of_the_year(self):
        self.assertEqual(
            clock.date_text(datetime(2026, 12, 31)), "Thursday, 31 December 2026"
        )

    def test_a_leap_day(self):
        self.assertEqual(
            clock.date_text(datetime(2028, 2, 29)), "Tuesday, 29 February 2028"
        )

    def test_the_day_is_never_padded(self):
        for day in range(1, 10):
            text = clock.date_text(datetime(2026, 9, day))
            self.assertNotIn("0", text.split()[1], text)

    def test_all_twelve_month_names_are_used(self):
        seen = {clock.date_text(datetime(2026, month, 15)).split()[2] for month in range(1, 13)}
        self.assertEqual(seen, set(clock.MONTHS))

    def test_all_seven_weekday_names_are_used(self):
        seen = {
            clock.date_text(datetime(2026, 3, day)).split(",")[0] for day in range(2, 9)
        }
        self.assertEqual(seen, set(clock.DAYS))

    def test_the_year_is_always_four_digits(self):
        self.assertTrue(clock.date_text(datetime(901, 6, 1)).endswith(" 901"))
        self.assertTrue(clock.date_text(datetime(2026, 6, 1)).endswith(" 2026"))


class TimeTests(unittest.TestCase):
    def test_twenty_four_hour_pads_the_hour(self):
        self.assertEqual(clock.time_text(datetime(2026, 9, 26, 9, 7), True), "09:07")
        self.assertEqual(clock.time_text(datetime(2026, 9, 26, 15, 7), True), "15:07")

    def test_twenty_four_hour_edges(self):
        self.assertEqual(clock.time_text(datetime(2026, 9, 26, 0, 0), True), "00:00")
        self.assertEqual(clock.time_text(datetime(2026, 9, 26, 23, 59), True), "23:59")

    def test_twelve_hour_has_no_leading_zero_and_a_lowercase_meridiem(self):
        self.assertEqual(clock.time_text(datetime(2026, 9, 26, 9, 7), False), "9:07 am")
        self.assertEqual(clock.time_text(datetime(2026, 9, 26, 15, 7), False), "3:07 pm")

    def test_midnight_and_noon_are_twelve_not_zero(self):
        self.assertEqual(clock.time_text(datetime(2026, 9, 26, 0, 0), False), "12:00 am")
        self.assertEqual(clock.time_text(datetime(2026, 9, 26, 0, 5), False), "12:05 am")
        self.assertEqual(clock.time_text(datetime(2026, 9, 26, 12, 0), False), "12:00 pm")

    def test_the_minute_before_noon_and_midnight(self):
        self.assertEqual(clock.time_text(datetime(2026, 9, 26, 11, 59), False), "11:59 am")
        self.assertEqual(clock.time_text(datetime(2026, 9, 26, 23, 59), False), "11:59 pm")

    def test_every_hour_of_the_day_in_both_faces(self):
        seen_am_pm = set()
        for hour in range(24):
            moment = datetime(2026, 9, 26, hour, 30)
            twenty_four = clock.time_text(moment, True)
            twelve = clock.time_text(moment, False)
            self.assertEqual(twenty_four, f"{hour:02d}:30")
            self.assertTrue(twelve.endswith((" am", " pm")), twelve)
            seen_am_pm.add(twelve.split()[1])
        self.assertEqual(seen_am_pm, {"am", "pm"})

    def test_the_hour_never_reads_zero_in_the_twelve_hour_face(self):
        for hour in range(24):
            text = clock.time_text(datetime(2026, 9, 26, hour, 30), False)
            self.assertNotEqual(text.split(":")[0], "0", text)


class FaceTests(unittest.TestCase):
    def test_face_is_the_two_lines(self):
        moment = datetime(2026, 9, 26, 15, 7)
        self.assertEqual(clock.face(moment, True), ("15:07", "Saturday, 26 September 2026"))
        self.assertEqual(clock.face(moment, False), ("3:07 pm", "Saturday, 26 September 2026"))

    def test_the_date_does_not_change_with_the_time_format(self):
        moment = datetime(2026, 9, 26, 15, 7)
        self.assertEqual(clock.face(moment, True)[1], clock.face(moment, False)[1])

    def test_seconds_in_both_faces(self):
        moment = datetime(2026, 9, 26, 15, 7, 4)
        self.assertEqual(clock.face(moment, True, seconds=True)[0], "15:07:04")
        self.assertEqual(clock.face(moment, False, seconds=True)[0], "3:07:04 pm")

    def test_a_hidden_date_is_an_empty_line(self):
        moment = datetime(2026, 9, 26, 15, 7)
        self.assertEqual(clock.face(moment, True, show_date=False), ("15:07", ""))

    def test_the_tooltip_carries_both_lines(self):
        tooltip = clock.tip(datetime(2026, 9, 26, 15, 7), True)
        self.assertEqual(tooltip, "15:07  ·  Saturday, 26 September 2026")


class WaitTests(unittest.TestCase):
    def test_on_the_minute_it_waits_a_whole_minute(self):
        moment = datetime(2026, 9, 26, 10, 30, 0, 0)
        self.assertAlmostEqual(clock.wait_for_next_change(moment), 60.0, places=6)

    def test_mid_minute_it_waits_the_rest(self):
        moment = datetime(2026, 9, 26, 10, 30, 42, 250000)
        self.assertAlmostEqual(clock.wait_for_next_change(moment), 17.75, places=6)

    def test_a_hair_before_the_boundary_still_waits_something(self):
        moment = datetime(2026, 9, 26, 10, 30, 59, 999999)
        self.assertGreaterEqual(clock.wait_for_next_change(moment), clock.MIN_WAIT)

    def test_the_delay_lands_on_the_next_minute_boundary(self):
        for moment in (
            datetime(2026, 9, 26, 10, 30, 0, 0),
            datetime(2026, 9, 26, 10, 30, 42, 250000),
            datetime(2026, 9, 26, 23, 59, 59, 500000),
            datetime(2026, 9, 26, 10, 30, 59, 999999),
        ):
            remaining = clock.wait_for_next_change(moment)
            after = moment + timedelta(seconds=remaining)
            boundary = moment.replace(second=0, microsecond=0) + timedelta(minutes=1)
            # Never early (the face would still be the old one) ...
            self.assertGreaterEqual(after, boundary)
            # ... and never more than the clamp late, so a wake-up just before the
            # boundary cannot turn into a busy loop.
            self.assertLess((after - boundary).total_seconds(), clock.MIN_WAIT + 1e-9)
            if 60 - moment.second - moment.microsecond / 1_000_000 > clock.MIN_WAIT:
                self.assertEqual(after, boundary)
                self.assertNotEqual(clock.face(after)[0], clock.face(moment)[0])

    def test_the_twelve_hour_face_changes_on_the_same_boundary(self):
        moment = datetime(2026, 9, 26, 12, 59, 59, 900000)
        after = moment + timedelta(seconds=clock.wait_for_next_change(moment))
        self.assertEqual(clock.face(after, False)[0], "1:00 pm")


if __name__ == "__main__":
    unittest.main()
