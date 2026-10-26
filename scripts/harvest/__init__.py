#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""批量取数：国家级全文 + 省市目录。

和 `scripts/catalog/`（按主题查清单）分开：那边是「用户问一件事，我现查」，
这边是「先把地基铺满」。两者共用 `data/catalog.db` 与 `CatalogItem` 模型。
"""
