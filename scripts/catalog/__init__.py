#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""目录层：回答「这个方向有哪些文件」，只存字段不存全文。

对外只需要三个入口：

    from catalog import crawl
    crawl.run(region="鹰潭", topic="城市更新")       # 抓一次
    crawl.load_cached(region="鹰潭", topic="城市更新")  # 读缓存
    from catalog.store import Store                   # 直接查库
"""
from .model import CatalogItem, TOPICS, STATUS_STALE, fold, parse_date  # noqa: F401
from .store import Store, DB_PATH                                       # noqa: F401

__all__ = ["CatalogItem", "TOPICS", "STATUS_STALE", "Store", "DB_PATH",
           "fold", "parse_date"]
