#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""运行当前仓库的安全与备份回归测试。"""

import sys
import unittest


def main():
    suite = unittest.defaultTestLoader.discover('tests', pattern='test_*.py')
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    return 0 if result.wasSuccessful() else 1


if __name__ == '__main__':
    sys.exit(main())
