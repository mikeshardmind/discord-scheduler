"""
This Source Code Form is subject to the terms of the Mozilla Public
License, v. 2.0. If a copy of the MPL was not distributed with this
file, You can obtain one at http://mozilla.org/MPL/2.0/.

Copyright (C) 2023 Michael Hall <https://github.com/mikeshardmind>
"""

from __future__ import annotations

import pathlib
import unittest
import uuid

import apsw

import scheduler

# may split to multiple files if needed, but... meh


class PathlibTesting(unittest.TestCase):
    # see: https://github.com/mikeshardmind/discord-scheduler/issues/1

    # Testing something which does filesystem access, as a "unit test", yeah yeah...

    def test_pathlib_handling(self) -> None:
        """Not adding setup and teardown for this"""
        path = pathlib.Path.cwd() / uuid.uuid4().hex / "test.file"
        resolved_path = path.resolve()
        self.assertFalse(path.exists(), "path exists somehow")
        with self.assertRaises(FileNotFoundError):
            path.resolve(strict=True)

        new_path = scheduler.scheduler.resolve_path_with_links(path)
        self.assertEqual(new_path, resolved_path, f"unexpected result {new_path=} {resolved_path=}")
        parent = path.parent
        path.unlink()
        parent.rmdir()


class DBSetup(unittest.TestCase):
    # https://github.com/mikeshardmind/discord-scheduler/issues/2

    def test_db_setup(self) -> None:
        conn = apsw.Connection(":memory:")
        self.assertIsInstance(scheduler.scheduler._setup_db(conn), set)  # pyright: ignore[reportPrivateUsage]
        conn.close()
