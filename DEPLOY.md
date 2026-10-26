# 部署

目标形态很简单：**一台机器、一个 Python 进程、一层反向代理。**

不用 Docker。整个项目零第三方依赖，全是标准库，装 Docker 只是多一层需要维护的东西，
换不来什么。这一条是刻意的：从一台机器搬到另一台，只要 `python app/server.py` 能跑。

---

## 一、机器

| 项 | 要求 | 说明 |
|---|---|---|
| CPU | 1 核够 | 瓶颈在调模型（16 秒）和 SQLite 全表扫，不在 CPU |
| 内存 | 4 GB | `kb.db` 1.9 GB，SQLite 页缓存给 512 MB，剩下留给 Python |
| 磁盘 | 10 GB | 见下面的盘算 |
| Python | 3.10 以上 | 用到了 `X \| None` 写法，3.9 不行 |

**盘算**：`kb.db` 1.9 GB + 正文 392 MB + 目录 67 MB + 埋点库 ≈ 2.4 GB。
重建索引时会写一个新文件再原子替换，峰值要 5 GB。所以留 10 GB，不留满。

---

## 二、传代码

```bash
git clone <repo> policy-verify-engine     # 或者 rsync 整个目录
cd policy-verify-engine
```

代码不含数据，几百 KB。

---

## 三、传数据（这一步最费时间）

三个文件，都是 SQLite 或纯文本：

```
data/kb.db          1.9 GB   全文索引，最重要的
data/catalog.db      67 MB   目录 + 台账 + 正文落盘记录
data/fulltext/      392 MB   每个文件的正文 txt，重新建索引要用
```

**先收缩再传，不然会传一个大几百 MB 的空洞。** SQLite 删过的行不会还给磁盘：

```bash
# 本地执行，不要在生产机上跑
sqlite3 data/kb.db "PRAGMA wal_checkpoint(TRUNCATE); VACUUM;"
sqlite3 data/catalog.db "PRAGMA wal_checkpoint(TRUNCATE); VACUUM;"
rm -f data/*.db-wal data/*.db-shm
tar -cJf pv-data.tar.xz data/kb.db data/catalog.db data/fulltext
```

`tar -J` 压缩比看数据，一般能压到 600—800 MB。传完解压：

```bash
tar -xJf pv-data.tar.xz        # 解到项目根目录，保持 data/ 结构
```

`app.db`（埋点库）不用传，第一次启动会自动建表。

> **为什么不用 `rsync --partial` 断点续传**：一个 700 MB 的包在宿舍网络上传十来分钟，
> 断了重来很烦。`rsync` 支持断点，`scp` 不支持。超过十分钟的传输一律用 rsync。

---

## 四、装密钥

`.env` 放**项目根目录**（跟 `.env.example` 同一个位置）就行，代码会按顺序找三处：
项目根 → 上一级 → 上两级，谁先存在用谁。推荐第一个，删项目的时候一起删掉，不留尾巴。

```bash
# 项目在 /srv/policy-verify-engine
cp .env.example .env          # .env 就在项目根
vim .env                      # 填一家模型的 key，设一个 PV_TOKEN
chmod 600 .env
```

> 这个「往上找三层」是 2026-10-26 改的。原来 `core.ENV_FILE` 写死 `ROOT.parent.parent`，
> 本地开发时是 `ai-pm/.env`（对的），搬到 `/srv/policy-verify-engine` 就变成 `/.env`（错了）；
> 而 `DEPLOY.md` 又写着「放上一级」。三处说法互相打架，症状是**启动一切正常、
> 一调模型就报「没有可用的模型通道」**。现在按候选找，并且 `PV_TOKEN` / `PV_PORT` /
> `PV_BIND` / `QW_RAW_ONLY` 也会读 `.env` —— 之前只认系统环境变量，
> 写在 `.env` 里的 `PV_TOKEN` 等于没设，公网裸奔而界面上看不出来。

三家模型只要有一家的 key 能用就能跑，换着试的顺序在 `core.pick_providers` 里。

**权限**：`chmod 600 .env`。里面有 API key，不能让同机器其他用户读到。

---

## 五、先自检，再起服务

```bash
python scripts/selftest.py
```

30 项断言，两秒跑完，不联网不调模型。全过才往下走。

它能抓到的最典型问题：数据传了但索引没传（docs 表空）、台账漏了某个地域、
`blocks` 和 `docs` 对不上。这些不报错，只会让检索少给几条——而少给几条的后果
是「用户以为没有」，这是本项目最不能出的错。

---

## 六、起服务

### 直接跑（先验证）

```bash
PV_TOKEN=$(openssl rand -hex 24) python app/server.py
```

启动会打印真实的库规模、地域分布、待恢复的中断任务数：

```
有据 · 政策依据梳理
  模型：dashscope/qwen3-max → deepseek/deepseek-chat → ark/doubao-seed
  数据：全文 33,525 份 / 295,000 块 · 目录 67,541 条 · 1,493 个地域
  分布：国家 18,608 · 上海市 8,903 · 福建省 2,838 · 江苏省 1,585 · 南京市 1,581
  联网：1 个站标记为不可达（直接返回原因，不硬等超时）
  任务：2 个上次未完成的任务已标为中断
  打开：http://127.0.0.1:8848
```

### 常驻（systemd）

```ini
# /etc/systemd/system/pv.service
[Unit]
Description=政策依据核验
After=network.target

[Service]
Type=simple
User=pv
WorkingDirectory=/srv/policy-verify-engine
Environment=PV_PORT=8848
Environment=PV_BIND=127.0.0.1
Environment=PV_TOKEN=换成你自己的长随机串
ExecStart=/usr/bin/python3 app/server.py
Restart=always
RestartSec=3

[Install]
WantedBy=multi-user.target
```

```bash
systemctl daemon-reload && systemctl enable --now pv
journalctl -u pv -f          # 看日志
```

`PV_BIND` 保持 `127.0.0.1`。改成 `0.0.0.0` 会绕过反向代理上的 HTTPS 和限流。

**重启不丢任务的机制**：异步任务落在 `app.db` 的 `job` 表，不是进程内存。重启后
已完成的报告还能查（结果留一天），正在跑的会被标成 `interrupted` 并返回明确错误，
轮询方不会无限转圈。这一条在断电、`systemctl restart`、部署新版本时都会用到。

---

## 七、反向代理与 HTTPS

用 Caddy。nginx 要多装 certbot 和多一条 cron 续期，Caddy 三行就完事：

```
# /etc/caddy/Caddyfile
pv.example.com {
    reverse_proxy 127.0.0.1:8848 {
        flush_interval -1
    }
}
```

`flush_interval -1` 必须加。不加的话 Caddy 会缓冲响应，SSE（`/api/plan` 等四个流式接口）
在浏览器里表现为「一直转圈，最后一次性全部出来」——功能没坏，但用户看到的是卡死 20 秒。

Caddy 自动签证书、自动续期，不用管。

---

## 八、验证上线

```bash
# 1. 健康检查（不需要 token）
curl https://pv.example.com/v1/health
# {"ok": true, "t": ..., "providers": ["dashscope", "deepseek", "ark"]}

# 2. 无 token 应该被拒
curl -i https://pv.example.com/v1/plan -d '{"q":"测试"}'
# HTTP/1.1 401

# 3. 有 token 提交异步任务
curl https://pv.example.com/v1/jobs \
  -H "X-PV-Token: $PV_TOKEN" -H "Content-Type: application/json" \
  -d '{"op":"plan","q":"南京老门西历史文化街区更新，建筑高度能不能调整"}'
# {"ok": true, "job_id": "j..."}

# 4. 轮询
curl https://pv.example.com/v1/jobs/j... -H "X-PV-Token: $PV_TOKEN"
# state: running → done，result.plan.tasks 里 8—10 个方向

# 5. 浏览器打开首页，走完四步
```

第 5 步不能省。接口通不代表界面通——前端有独立的埋点上报和 SSE 解析，
只有真的走一遍四步才能确认。

---

## 九、回滚

三个开关都是环境变量，改完 `systemctl restart pv` 生效，不发版：

| 开关 | 作用 | 什么时候用 |
|---|---|---|
| `QW_DISABLE_WEB=1` | 关联网，只查库 | 政府站大面积挂、或者被反爬盯上 |
| `QW_RAW_ONLY=1` | 撰写不调模型，只排原文 | 报告出现编造、或者模型集体限流 |
| 删掉 `PV_TOKEN` | 服务变成无鉴权 | **不要这么干**，公网裸奔几个小时就会被扫到 |

要退回上一版代码：`git checkout <上一版 tag>` 然后重启。数据和代码是分开的，
回滚代码不用动数据。

---

## 十、已知的部署坑

按踩到的顺序排。

**1. `.env` 路径。** 代码按「项目根 → 上一级 → 上两级」找，第一个存在的生效。
如果明明写了 key 却说「没有可用的模型通道」，先跑
`python -c "import sys;sys.path.insert(0,'app');import core;print(core.ENV_FILE)"`
看它到底读的哪个文件——比猜快。

**2. 忘了 `wal_checkpoint`。** `kb.db-wal` 可能比 `kb.db` 还大。只传 `.db` 不传 `-wal`，
传上去的库会缺最近的写入——看起来正常，查某些文件就是没有。**传输前一律 checkpoint 并删 wal/shm。**
（`scripts/make_release.py` 会自动做这一步，手工传数据时才需要自己记得。）

**3. 索引重建要留双倍空间。** `build_index.py` 写新文件再替换，1.9 GB 的库要 4 GB 空闲。

**4. 中文路径。** Windows 上开发的脚本用 `utf-8` 显式读写，Linux 上没问题。
反过来从 Linux 拷到 Windows 时，`.bat` 里的 `chcp 65001` 不能删。

**5. 时间。** 台账的 `expired` / `pending` 判定依赖 `date.today()`。服务器时区必须是
`Asia/Shanghai`，UTC 机器上会在每天 8 点前判错一天——影响很小，但会让自检里
「expired 的都真的过期了」偶发失败。
