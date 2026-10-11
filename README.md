# freecad-sheetmetal-drawing Skill

使用 FreeCAD 将 STEP/STP 钣金总成生成同一图幅的六面正投影可编辑 DXF，并完成视图尺寸、压铆柱分类、图幅与统一比例、表格标题栏、图线简化及交付验收。入口是 [SKILL.md](SKILL.md)，Agent 会根据任务阶段按需读取 `references/` 中的专项规则，并在出图后调用 `scripts/` 中的独立审计工具。

本 Skill 面向带压铆件的钣金安装工程图，不用于推断 STEP 中不存在的材料、公差、螺纹等级、折弯展开或加工工艺参数。

## 工具链

| 工具/组件 | 用途 | 要求与边界 |
| --- | --- | --- |
| Codex + 本 Skill | 读取项目输入，选择专项规则，组织出图、回读和验收 | Skill 提供规则、样例和审计工具，不用历史图纸替代当前 STEP 数据 |
| FreeCAD / `FreeCADCmd` | 导入并重算 STEP，读取装配与实体几何，取得标准视向，执行 TechDraw HLR 正投影 | 几何处理脚本应在能够导入 `FreeCAD`、`Part` 和 `TechDraw` 的 FreeCAD Python 环境运行；当前 HLR 分组规则按 FreeCAD 1.1 验证，版本变化后须重新验证 `projectEx()` 返回结构 |
| Python 3 | 运行项目出图辅助程序和 Skill 的独立审计脚本 | 普通 DXF 后处理可使用系统 Python；调用 FreeCAD API 的程序使用 FreeCAD 自带 Python 或 `FreeCADCmd`，不能假定系统 Python 已安装 FreeCAD 模块 |
| `ezdxf` | 创建、编辑和重新读取 DXF；检查图层、线型、线宽、字体、实体、包围框、重合及文件结构 | 出图环境和审计环境应使用彼此兼容的版本；最终验收必须重新读取已关闭的最终 DXF，不能复用生成器内存中的对象或自报结论 |
| Matplotlib + `ezdxf.addons.drawing` | 从最终 DXF 生成高分辨率回读预览 | 无界面运行使用非交互后端（如 `Agg`）；预览只用于审图证据，不替代可编辑 DXF，也不能代替目标 CAD 测试 |
| 工程字体 | 显示中文、数字和 `⌀/R/±/°` 等工程符号 | 项目或加工厂字体优先；本项目 Windows 默认使用可解析的 `simfang.ttf`，禁止静默字体替换 |
| 目标 CAD 阅读器 | 验证最终 DXF 的实际兼容性和交互性能 | 使用加工厂指定或项目常用软件完成打开、缩放、平移、图层切换、打印预览和保存副本测试；未测试时状态保持“待目标阅读器验证” |

项目可以使用自己的 FreeCAD 出图脚本完成模型识别、投影、标注和 DXF 组装；这些生成脚本不固定在 Skill 内。Skill 自带的 `scripts/` 只负责最终 DXF 的文字审计、结构与性能审计、预览检查记录和项目验收汇总，不能替代出图程序。

开始任务前至少确认以下工具可调用；命令名不在 `PATH` 时使用实际可执行文件的绝对路径：

```powershell
FreeCADCmd --version
python --version
python -c "import ezdxf, matplotlib; print(ezdxf.__version__, matplotlib.__version__)"
```

每次出图记录实际使用的 FreeCAD、Python、`ezdxf`、DXF 导出器和目标 CAD 版本。缺少 FreeCAD 时不能从 STEP 可靠生成投影；缺少 `ezdxf` 时不能运行本 Skill 的独立 DXF 审计；缺少目标 CAD 验证时只能交付为待验收状态。

## 从 STEP 工程中使用

Skill 目录结构如下，实际存放位置不限：

```text
freecad-sheetmetal-drawing/
├─ SKILL.md
├─ README.md
├─ references/
│  ├─ drawing-workflow.md
│  ├─ view-basis-selection.md
│  ├─ stud-workflow.md
│  ├─ projection-complexity.md
│  ├─ feature-dimensions.md
│  ├─ sheet-fit-evaluation.md
│  ├─ notes-tables-titleblock.md
│  ├─ dxf-delivery.md
│  └─ project-acceptance.md
├─ scripts/
│  ├─ dxf_text_audit.py
│  ├─ dxf_performance_audit.py
│  ├─ record_projection_review.py
│  └─ project_acceptance.py
└─ assets/
   └─ 工程图样例文件
```

使用绝对路径指定 Skill 时不需要先安装。推荐按以下步骤使用：

1. 在包含待处理 STEP/STP 的工程目录中打开 Codex，并确认 Agent 可以访问 FreeCAD、源模型、输出目录和本 Skill。
2. 在指令中写明实际 `SKILL.md` 路径、源模型及输出目录。Agent 会按任务阶段读取需要的专题文档，无需手工列出全部 `references`。
3. 第一次接手模型时，先进行只读盘点：确认模型单位、实体数量、钣金本体、压铆件、整体包络、默认前视图和缺失项目输入。
4. 确认方向后再执行六视图投影、尺寸标注、压铆分类、图幅评估、表格排版和 DXF 导出。
5. 出图后必须回读最终 DXF，依次完成文字审计、图线和性能审计、预览检查以及项目验收汇总。
6. 查看 Agent 报告的最终 DXF、检查记录、失败项、待确认输入和目标 CAD 验证状态。自动门禁通过不等于工程批准或投产放行。

以下指令中的 `<SKILL.md 的实际路径>`、`<源模型>` 和 `<输出目录>` 是占位符，使用时替换为真实路径。第一次检查可以发送：

```text
请读取并遵循 <SKILL.md 的实际路径>，按需读取它引用的参考文档。

检查 <源模型> 是否具备生成钣金六视图压铆标注图的条件；盘点钣金本体、压铆件、方向、尺寸来源和缺失项目输入。本次只检查，不生成或修改文件。
```

需要正式出图时，推荐按照下面的结构说明任务：

```text
请读取并遵循 <SKILL.md 的实际路径>，按需读取它引用的参考文档。

源模型：<STEP/STP 文件>
输出目录：<输出目录>
图纸前视图：<FRONT/REAR/LEFT/RIGHT/TOP/BOTTOM；未指定时使用 FreeCAD 默认 FRONT>
图幅与比例：<指定图幅/方向/比例，或允许自动选择>
项目输入：<图号、版本、材料、表面处理、公差、数量、公司、项目模板；未知项写待确认>
目标 CAD/DXF：<接收软件与版本；未知时写待验证>
完成要求：生成同幅六视图可编辑 DXF，标注尺寸并分类压铆柱，完成回读、审计、预览和项目验收记录。
```

只填写当前项目已经确认或确实需要限制的信息，不要从旧图、文件名或样例图纸推断未知参数。

| 任务 | 示例指令 |
| --- | --- |
| 模型盘点 | 按这个 Skill 检查当前 STEP 的本体、压铆件、单位、方向和出图条件，本次只审查。 |
| 默认方向出图 | 按这个 Skill 生成六视图 DXF；未指定前视图，使用 FreeCAD 默认 FRONT。 |
| 重定义前视图 | 按这个 Skill 将 FreeCAD REAR 定义为图纸 FRONT，并从该方向重新推导其余五个视图。 |
| 指定图幅 | 按这个 Skill 在 A1 横向图幅中选择可读的最大统一比例，六视图不得单独缩放。 |
| 压铆标注 | 识别压铆柱的内径、外径和不含板厚的高度，按 `M<内径>*D<外径>-H<高度>` 分类并标注。 |
| DXF 审查 | 按这个 Skill 审查现有最终 DXF 的文字压线、图线重合、碎片数量、隐藏线节奏和阅读器性能，不修改源文件。 |

## 前视图和六视图布局

`front_source` 只接受 `FRONT`、`REAR`、`LEFT`、`RIGHT`、`TOP`、`BOTTOM`。未提供时使用 FreeCAD 默认 `FRONT`，不能根据最大平面或当前相机方向猜测。

图纸固定采用第三角法布局：FRONT 居中、TOP 在上、BOTTOM 在下、LEFT 在左、RIGHT 在右、REAR 位于 RIGHT 外侧。六个视图位于同一图幅、使用同一比例，并按共同投影基准对齐。

## 输出与验收

默认输出文件名为 `<前缀>_六视图压铆标注.dxf`。完整交付记录包括：

- 最终可编辑 DXF；
- 图幅与统一比例评估；
- 工程图检查记录；
- 最终 DXF 文字审计；
- DXF 图线与性能审计；
- 项目验收汇总记录；
- 用于验收的最终 DXF 回读预览。

`scripts/` 中的工具用于独立审查最终结果，不承担通用 STEP→DXF 生成。项目可以保留自己的 FreeCAD 出图脚本，但最终文件必须通过下列检查：

| 脚本 | 作用 |
| --- | --- |
| `dxf_text_audit.py` | 检查字体解析、真实字形框、文字越格/压线、安全净距和标题栏合并区域。 |
| `dxf_performance_audit.py` | 检查实体与顶点数量、重复图线、可见/隐藏线重合、线型线宽、非法坐标和卡死风险。 |
| `record_projection_review.py` | 将可见轮廓断裂、内部特征缺失和隐藏线异常的人工检查结果与预览哈希绑定。 |
| `project_acceptance.py` | 汇总模型、方向、六视图、尺寸、压铆、图幅、文字、性能、人工审图和目标 CAD 状态。 |

独立审计需要 Python 和 `ezdxf`；没有安装在当前环境时，可使用脚本的 `--vendor-path` 指向兼容依赖目录。中文图纸还需要出图端和接收端均可解析的项目字体；本项目未另行指定时使用 Windows `simfang.ttf`。最终 DXF 仍须在项目指定或加工厂常用 CAD 中完成打开、缩放、平移、图层切换、打印预览和保存副本测试。

## 安装为可发现的 Skill

复制整个 `freecad-sheetmetal-drawing` 文件夹，保留 `SKILL.md`、`references/`、`scripts/` 和需要使用的 `assets/`：

- 默认个人目录：`%USERPROFILE%\.codex\skills\freecad-sheetmetal-drawing`
- 设置了 `CODEX_HOME` 时：`%CODEX_HOME%\skills\freecad-sheetmetal-drawing`

安装后可在 Codex 中选择 `freecad-sheetmetal-drawing`，或调用：

```text
$freecad-sheetmetal-drawing
请检查当前 STEP，并生成同一图幅的六视图压铆标注 DXF。未指定前视图，使用 FreeCAD 默认 FRONT；缺失项目参数保留为待确认。
```

如果新安装的 Skill 尚未显示，请重新启动当前 Codex 会话后再检查可用 Skill 列表。

## 文件导航

- [SKILL.md](SKILL.md)：适用范围、输入、执行入口和不可违反的核心约束。
- [完整出图流程](references/drawing-workflow.md)：从模型盘点到最终验收的主流程。
- [前视图选择](references/view-basis-selection.md)：重定义图纸 FRONT 及其余五视图的推导规则。
- [压铆柱工作流](references/stud-workflow.md)：实例识别、分面、规格分类、颜色和位号。
- [投影与复杂度控制](references/projection-complexity.md)：HLR 分组、可见优先、去碎片和内部轮廓保持。
- [特征尺寸清单](references/feature-dimensions.md)：六视图尺寸、孔槽、折边和定位标注要求。
- [图幅与比例评估](references/sheet-fit-evaluation.md)：动态图幅及最高可行统一比例的计算和验收。
- [表格与标题栏](references/notes-tables-titleblock.md)：技术要求、类别表、版本履历和联合标题栏。
- [DXF 交付规则](references/dxf-delivery.md)：图层、线型、线宽、导出、回读和目标 CAD 验证。
- [项目验收闭环](references/project-acceptance.md)：自动、人工、项目输入和工程批准门禁。
- `assets/`：用于版式和视觉关系参考的历史样例；其中尺寸、图号、材料和人员信息不能作为新项目数据源。

## 使用边界

- 源 STEP、用户确认输入和批准模板优先于样例与默认规则。
- 不从旧工程图、旧脚本或样例复制固定尺寸、孔位、图号、材料、版本或公司信息。
- 不把透明投影视图或全边实线当作工程图；输出采用可见实线、隐藏虚线和中心线。
- 不把所有 HLR 边无差别离散为折线；直线、圆弧和圆优先保留为解析 CAD 实体。
- 不生成折弯展开、工艺孔、螺纹等级、公差或表面处理，除非存在可靠项目输入。
- 自动检查通过但人工审图、项目输入或目标 CAD 测试未完成时，只能标记“待验收”。
