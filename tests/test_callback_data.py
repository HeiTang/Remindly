from __future__ import annotations

import unittest

from remindly.reminders.callback_data import (
    CallbackDataError,
    parse_reminder_callback,
    reminder_callback,
)


class CallbackDataTest(unittest.TestCase):
    def test_time_callback_keeps_clock_value_intact(self) -> None:
        encoded = reminder_callback("time", "draft_abc", "09:00")
        decoded = parse_reminder_callback(encoded)

        self.assertEqual("reminder", decoded.namespace)
        self.assertEqual("time", decoded.action)
        self.assertEqual("draft_abc", decoded.target_id)
        self.assertEqual("09:00", decoded.value)

    def test_action_without_value(self) -> None:
        decoded = parse_reminder_callback(reminder_callback("list"))

        self.assertEqual("list", decoded.action)
        self.assertEqual("_", decoded.target_id)
        self.assertIsNone(decoded.value)

    def test_rejects_wrong_namespace(self) -> None:
        with self.assertRaises(CallbackDataError):
            parse_reminder_callback("other:list:_")

    def test_rejects_colon_in_target_id(self) -> None:
        with self.assertRaises(CallbackDataError):
            reminder_callback("view", "bad:id")


if __name__ == "__main__":
    unittest.main()
