import unittest

from dockerutil import container_id_from_hostname, foreign_ids, foreign_named, same_container


class ContainerIdTests(unittest.TestCase):
    def test_accepts_a_hex_hostname_only(self):
        self.assertIsNone(container_id_from_hostname(None))
        self.assertIsNone(container_id_from_hostname(""))
        self.assertIsNone(container_id_from_hostname("local-host"))
        self.assertIsNone(container_id_from_hostname("a" * 11))
        self.assertIsNone(container_id_from_hostname("a" * 65))
        self.assertIsNone(container_id_from_hostname("g" * 12))
        self.assertEqual(container_id_from_hostname("A" * 12), "a" * 12)
        self.assertEqual(container_id_from_hostname("  " + "b" * 64 + "\n"), "b" * 64)

    def test_same_container_matches_a_short_or_full_id(self):
        own = "a" * 12
        full = own + "b" * 52
        self.assertFalse(same_container(full, None))
        self.assertFalse(same_container("", own))
        self.assertFalse(same_container("c" * 64, own))
        self.assertTrue(same_container(own, own))
        self.assertTrue(same_container(full, own))
        self.assertTrue(same_container(own, full))

    def test_foreign_ids_omit_this_container(self):
        own = "a" * 12
        full = own + "b" * 52
        other = "c" * 64
        text = f"\n{full}\n{other}\n{own}\n\n"
        self.assertEqual(foreign_ids(text, own=own), [other])
        self.assertEqual(foreign_ids(text, own=None), [full, other, own])

    def test_foreign_named_omits_this_container_and_keeps_the_name(self):
        own = "d" * 12
        full = own + "e" * 52
        text = f"{full} rookrunner-{own}\n{'f' * 64} rookrunner-kept\nno-name\n"
        self.assertEqual(foreign_named(text, own=own), ["rookrunner-kept"])
        self.assertEqual(
            foreign_named(text, own=None),
            [f"rookrunner-{own}", "rookrunner-kept"],
        )
