# 标书解析工作台（一期骨架）

独立的工程与技术服务招标文件解析服务。当前可运行纵向切片覆盖文件上传、DOCX/PDF/TXT/Markdown及嵌套 ZIP 文件包解析、项目字段与评分表候选提取、持久化任务和审核 API、人工修正留痕及原文证据定位。

## 运行

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -e ".[dev]"
Copy-Item .env.example .env
python -m uvicorn bidreader.app:app --app-dir src --host 127.0.0.1 --port 8000
```

浏览器访问 <http://127.0.0.1:8000>；API 文档位于 <http://127.0.0.1:8000/docs>。默认使用本机 SQLite 与 `data/` 目录存储；设置 `DATABASE_URL` 可改用 SQLAlchemy 支持的 PostgreSQL URL（需安装 `.[postgres]`）。

首期主要接口包括 `POST /api/v1/tenders` 上传并创建解析任务、`GET /api/v1/runs/{run_id}` 查询任务、`GET /api/v1/tenders/{tender_id}/analysis` 读取结构化结果、`PATCH /api/v1/runs/{run_id}/fields/{field_name}` 和 `PATCH /api/v1/runs/{run_id}/criteria/{criterion_id}` 留存审核修正、`POST /api/v1/runs/{run_id}/confirm` 最终确认，以及 `POST /api/v1/runs/{run_id}/retry` 从持久化检查点重试失败任务。

如要启用 OpenAI 兼容模型的评分条目辅助分类，配置 `LLM_BASE_URL`、`LLM_API_KEY` 和 `LLM_MODEL`。系统只应用可在候选原文中验证的类别判断，失败时保留规则结果。原文证据仍须人工确认。

项目名称和项目编号候选使用了 [Inupedia/tender-extract](https://github.com/Inupedia/tender-extract) 的 MIT 许可增强规则和抽取引擎，并映射回本系统解析出的原文块。复用文件、上游 revision 和适配范围见 [第三方来源说明](src/bidreader/vendor/tender_extract/NOTICE.md)；上游 MIT License 随 vendored 代码保留。评分项抽取当前仍使用本项目的候选规则，后续将依据本地金标准评估决定接入哪些上游方法。

扫描 PDF 默认尝试本机 OCR。安装 OCR 适配包：

```powershell
python -m pip install -e ".[ocr]"
```

PaddleOCR 3.x 还需要按操作系统/CPU/GPU环境安装匹配的 PaddlePaddle 推理框架，详见 [PaddleOCR 官方安装文档](https://github.com/PaddlePaddle/PaddleOCR/blob/main/docs/version3.x/installation.md)。设置 `LOCAL_OCR_ENABLED=false` 可关闭扫描页 OCR；没有 OCR 运行环境时，扫描页会保留为待复核状态，不会被静默当作已解析页面。OCR 框坐标映射回 PDF 页面坐标，并且扫描页证据会标为 OCR 来源。

## 当前边界

- 支持 DOCX、可提取文本的 PDF、XLSX、TXT、Markdown 和嵌套 ZIP 文件包。XLSX 按工作表行解析，合并单元格上下文会展开到关联行，证据记录工作表与单元格范围。ZIP 解析在内存/临时文件中逐级检查条目数、解压体积、压缩比和嵌套深度；旧版 `.doc` 仍进入待复核账本，暂不自动解析。
- 扫描 PDF 通过可选 PaddleOCR 3.x 本地适配执行页面 OCR，若 OCR 依赖未安装、推理失败或页面无文字则进入人工复核。MinerU 尚未接入。网页现有证据列表展示引用页码/区域数据，但 PDF 页面渲染和交互式框选高亮尚未落地。DOCX 当前提供段落/表格块定位，页码依赖未来固定渲染器建立映射。
- DOCX 原生评分表按表格边界和评分列提取；普通正文中的“评分”提及不会单独成为评分项。包含多个分标评分表的文件包会尝试利用包件名称与表内适用范围筛选候选，但仍需业务人员确认适用分标。复杂表头、跨页表、补遗合并、公式计算和附件关系仍须实现与标注验证。

- 任务检查点、进度和结果持久化在 SQL 数据库；当前执行器为同进程工作线程，适合单机试验，不保证多副本并发或分布式任务语义。
- 服务默认绑定回环地址。公网或局域网部署前必须补用户认证、访问控制、CSRF/限流策略、病毒扫描、PostgreSQL迁移、对象存储和独立任务队列。
- 不应用“总分必须为100”一类假设规则；后续校验只在招标文件明示总分及加总语义时执行，并把差异标为人工复核。

## 开源实现参考

XLSX 工作表/单元格证据锚点和文件处理账本的完整性门借鉴了 [bid-agent-vscode](https://github.com/fanfanyuyang/bid-agent-vscode) 公开方案中的逐对象清点与原生多格式解析设计；候选的结构化评分结果和人工复核状态参考 [BidPilot-AI](https://github.com/Router0824/BidPilot-AI) 的证据完整性、来源命中与冲突需复核思路。当前 XLSX 解析由本项目基于 openpyxl 实现，未复制这些仓库的源文件。

## 测试

```powershell
python -m pytest
```

## 样本基线评测

还没有用户标注的真实招标文件，因此目前没有对外宣称识别准确率。黄金样本格式、双人标注约定和逐文件评测命令见 [evaluation/README.md](evaluation/README.md)；评测前要求标注完整覆盖并校验原件 SHA-256，避免将不完整标注或不同文件版本算成有效指标。
