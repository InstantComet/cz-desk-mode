"""Offline tests: Sym-layer translation, Shift handling and the give-back hotkey."""
import importlib.util
import os
import unittest

spec = importlib.util.spec_from_file_location('dm', os.path.join(os.path.dirname(__file__), 'cz-desk-mode.py'))
dm = importlib.util.module_from_spec(spec)
spec.loader.exec_module(dm)


class FakeVirtual:
    def __init__(self):
        self.out, self.down = [], set()

    def emit(self, code, value):
        self.out.append((code, value))
        (self.down.add if value else self.down.discard)(code)


def handover(events):
    h = dm.Handover.__new__(dm.Handover)
    h.keyboard, h.virtual, h.held, h.shifted = None, FakeVirtual(), set(), {}
    queue = list(events)
    dm.key_events = lambda fd: [queue.pop(0)] if queue else []
    back = False
    while queue:
        back = h.pump() or back
    return h.virtual.out, back


class Translate(unittest.TestCase):
    def test_hash_is_shift_3(self):
        out, _ = handover([(39, 1), (39, 0)])
        self.assertEqual(out, [(42, 1), (4, 1), (4, 0), (42, 0)])

    def test_semicolon_no_shift(self):
        out, _ = handover([(79, 1), (79, 0)])
        self.assertEqual(out, [(39, 1), (39, 0)])

    def test_letters_pass_through_and_repeat_dropped(self):
        out, _ = handover([(30, 1), (30, 2), (30, 0)])
        self.assertEqual(out, [(30, 1), (30, 0)])

    def test_held_shift_not_released_by_symbol(self):
        out, _ = handover([(42, 1), (26, 1), (26, 0), (42, 0)])   # Shift held, then !
        self.assertEqual(out, [(42, 1), (2, 1), (2, 0), (42, 0)])

    def test_ctrl_alt_esc_gives_back_without_forwarding_esc(self):
        out, back = handover([(29, 1), (56, 1), (1, 1)])
        self.assertTrue(back)
        self.assertNotIn((1, 1), out)

    def test_plain_esc_forwarded(self):
        out, back = handover([(1, 1), (1, 0)])
        self.assertFalse(back)
        self.assertEqual(out, [(1, 1), (1, 0)])

    def test_every_sym_target_is_standard(self):
        for code, (target, shift) in dm.SYM.items():
            self.assertIn(target, (2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 26, 27, 39, 40, 41, 43, 51, 52, 53))


if __name__ == '__main__':
    unittest.main()
