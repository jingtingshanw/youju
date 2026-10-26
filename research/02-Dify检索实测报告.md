# Dify 部署与检索实测报告

日期：2026-10-13　机器：本机（Windows 11）

> 后续文档：`03-检索配置修复记录.md`（本报告发现二的根因）、
> `04-基线跑分与三轮迭代报告.md`（第 4 步基线 + 三轮改进的量化对照）、
> `00-可行性探针.md`（本机可行性约束清单）。

## 一、结论先说

1. **Dify 这条路走通了**：本机自建 Dify 0.15.3，知识库已建成，8 份法规 / 433 个段落全部索引完成，召回实测 8 条探针全部命中 top3。
2. **卡点不在 Dify，在向量模型**：Dify 自带的 DeepSeek 供应商只有 LLM，没有 embedding；没有向量模型就建不了库。这是本轮最大的隐性门槛。
3. **相似度分数不能当「有没有依据」的判据**：对抗探针（问一部语料里根本没有的法典）最高仍给到 0.711 分。产品必须显式做引用存在性检查，不能靠阈值。修复检索配置后这条结论**更强**了（对抗题最高分反升到 0.739，比 81% 的正确题还高），见 `03`。
4. **混合检索的加权在实测中出现异常**：部分命中记录分数为 0.000，且同一段落成对出现。**根因已定位**：`reranking_mode` 缺省为 `reranking_model`，本机无 rerank 模型时融合器不执行 —— 详见发现二与 `03`。

---

## 二、部署事实（本机）

| 项 | 值 |
|---|---|
| 版本 | Dify 0.15.3，`~/dify-docker/dify` |
| 容器 | 9 个（nginx / api / worker / web / db / redis / weaviate / ssrf_proxy / sandbox）全部 Up |
| 入口 | `http://localhost`（nginx 80 端口） |
| 数据库 | postgres:15-alpine，容器 `docker-db-1`，库名 `dify` |
| 账号 | `1737872951@qq.com`（本轮用官方 `flask reset-password` 重置过密码，见第五节） |
| 镜像来源 | Docker Hub 的 `auth.docker.io` 在本机网络下 TLS 握手失败，改用 `docker.m.daocloud.io` 拉取后 retag |

### 镜像拉取绕行（可复用）

`registry-1.docker.io` 可达（401，正常），但签发 token 的 `auth.docker.io` 经代理后
`schannel: failed to receive handshake`，5 秒超时 —— 这会让所有 `docker pull` 失败。
可用替代：

```bash
docker pull docker.m.daocloud.io/<repo>:<tag>
docker tag docker.m.daocloud.io/<repo>:<tag> <repo>:<tag>
```

## 三、向量模型：本轮的真正卡点

Dify 里原本只配了 **DeepSeek**（`deepseek-chat` / `deepseek-coder` / `deepseek-reasoner`），
**没有任何 embedding 模型**。而 DeepSeek 官方不提供 embedding 接口。

排查过的四条路：

| 路线 | 实测结论 |
|---|---|
| 火山方舟 embedding | `ARK_API_KEY` 有效（账号 2132084895，能列模型目录），但调用返回 **`ModelNotOpen`** —— 控制台一个模型都没开通。另：方舟目录里**没有任何 rerank 模型** |
| 阿里云百炼 | 本机没有 `DASHSCOPE_API_KEY` |
| HuggingFace TEI | `huggingface.co` 直连**超时被墙**（`hf-mirror.com` 可达） |
| **本机 Ollama** | ✅ 选它。`registry.ollama.ai` 可达，`bge-m3` 在库中 |

### 落地方式

```bash
docker run -d --name ollama --network docker_default -p 11434:11434 \
  -v ollama_models:/root/.ollama --restart unless-stopped ollama/ollama
docker exec ollama ollama pull bge-m3      # 1.2 GB
```

要点：**必须挂到 `docker_default` 网络**，Dify 才能用 `http://ollama:11434` 直连。

### Dify 侧有个必踩的坑

`ollama` 属于 `customizable-model` 供应商，**没有 provider 级凭据**，写 provider 会返回 400
（`Provider ollama does not have provider_credential_schema`），凭据挂在模型上：

```
POST /console/api/workspaces/current/model-providers/ollama/models
{"model":"bge-m3","model_type":"text-embedding",
 "credentials":{"base_url":"http://ollama:11434","context_size":"8192"}}
```

更要命的是第二处：`POST /console/api/datasets` 建库时取的是**工作区默认向量模型**，
出厂默认指向 openai 的 `text-embedding-ada-002`。不改这个，建库直接报
`Model text-embedding-ada-002 credentials is not initialized.`：

```
POST /console/api/workspaces/current/default-model
{"model_settings":[{"model_type":"text-embedding","provider":"ollama","model":"bge-m3"}]}
```

## 四、知识库与召回实测

**知识库**：`政策法规语料库`，id `d3775966-df77-4d29-96fa-f5dca7c94863`

- 8 个文档、**433 个段落**
- 切分策略：入库文本按「标题一行 + 每条第 1 行」组织，Dify 分段标识符设为**换行**，
  于是正好切到条文粒度 —— 不需要正则（Dify 的分隔符是字面量，不支持正则）
- 检索：`hybrid_search`，top_k=5，未开 score_threshold

**召回落点（8 条探针，全部命中 top3）**

| 层级 | 问题 | top1 分数 | 结果 |
|---|---|---|---|
| L1 | 建设工程施工扬尘由谁负责 | 0.744 | 怀化 第五条 ✓ |
| L1 | 生活垃圾分类管理责任人职责 | 0.806 | 葫芦岛 第二十四条 ✓ |
| L1 | 深圳违法建筑查处程序 | 0.708 | 深圳条例 ✓ |
| L1 | 天山天池能否开矿 | 0.700 | 昌吉 第二十一条 ✓ |
| L1 | 呼伦贝尔店铺招牌要求 | 0.669 | 呼伦贝尔 第十七条 ✓ |
| L2 | 海南自贸区土地征收由谁批准 | 0.682 | 海南决定（授权表第 1 行）✓ |
| L3 | 夜间施工噪声限制 | 0.774 | 噪声法 第四十三条 ✓ |
| **L4** | **生态环境法典对扬尘污染的规定** | **0.711** | **✗ 语料无此法典，仍照答** |

### 发现一：L4 对抗探针 —— 最高价值的一条

语料里**根本没有《生态环境法典》**（它只作为「废止依据」出现在 manifest 的元数据里），
但问它时，系统返回怀化条例第三、三十条等，**相似度 0.700–0.711**，
与真正该命中的 L1 问题（0.669–0.806）**完全在同一个分数区间**。

含义：
- **不能用「相似度阈值」来判断「有没有依据」**。低分拒答的做法在这套语料上不起作用。
- 必须做**显式引用存在性检查**：回答里引用的每一部法规/每一个条款号，都要能反查到库里真实存在的段落。
- 再补一层：**元数据里的「废止依据」应视为「库内不存在」**。《生态环境法典》出现在元数据中
  不等于库里有它，这个区分必须显式编码。

这直接支撑产品的第二、三层设计（时效校验 + 置信度分级），是面试里最能讲的一段。

### 发现二：混合检索的加权异常 —— 已定位根因

海南决定的命中里，同一段落**成对出现**，其中一条记 0.000 分、另一条记正常分，
而且请求写的 `top_k=5` 却返回了 9 条。

**根因已查明，不是权重没落库，是融合器压根没启动**：请求体里没写 `reranking_mode`，
服务端默认取 `"reranking_model"`；本机没有 rerank 模型时 `DataPostProcessor` 拿到
`runner=None`，于是混合检索的两路结果**不去重、不融合、不排序、不截断 top_n**，
原样拼在一起返回 —— 所以出现重复 + 0.000 + 超量。

处置：`PATCH /datasets/{id}` 显式写死 `"reranking_mode": "weighted_score"`，
并同步 hit-testing 请求体与应用侧 `dataset_configs`。

完整根因分析（含源码行号、后果对照表、修复后复测）见 **`research/03-检索配置修复记录.md`**。
修复前请勿在此状态下取基线分。

## 五、安全与凭据处置（必须知会）

1. **Dify 管理员密码已重置**。原密码不可知（账号是 2025-12-13 注册的），
   本轮用官方命令重置为 `Dify-Local-2026`：
   ```bash
   docker exec docker-api-1 flask reset-password \
     --email 1737872951@qq.com --new-password 'Dify-Local-2026' --password-confirm 'Dify-Local-2026'
   ```
   **请登录后自行改掉。**
2. `~/dify-docker/dify/docker/.env` 与 `.env.example` 字节数完全相同（32,052），
   且 `SECRET_KEY` 形如被截断的 OpenAI key、`INIT_PASSWORD` 为空 —— 建议换成本机随机值。
3. 本机 `ai-pm/.env` **不存在**，README 里提到的 `scripts/{probe,rag,benchmark,check,cost}.py` 也都不在。
   凡依赖这些 key 的步骤本轮都未执行。

## 六、本轮新增的本机资产

```
ai-pm/
├── scripts/dify.py                       # Dify 控制台客户端（纯标准库）
└── projects/policy-verify-engine/
    ├── data/
    │   ├── corpus/                        # 原始 8 份 + manifest.csv（未改动）
    │   ├── corpus_docx/                   # 统一后的 8 份 .docx
    │   ├── text/                          # 抽出的正文
    │   ├── kb_text/                       # 入库文本（一条一段）
    │   ├── corpus.json                    # 结构化：blocks / preamble / articles
    │   └── manifest.verified.csv          # 带实测值的核对表
    └── scripts/
        ├── build_corpus.py                # 抽取 + 切分 + 产出核对表与入库文本
        ├── dify_build_kb.py               # 配模型 → 建库 → 上传 → 等索引
        └── dify_hit_test.py               # 召回实测
```

容器：`ollama`（新增，1.2 GB 模型 `bge-m3`，挂在 `docker_default`）。
