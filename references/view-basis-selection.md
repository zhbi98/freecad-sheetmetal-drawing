# 选择 FreeCAD 标准方向作为图纸前视图

本流程允许用户把 FreeCAD 的任一标准视图方向重新定义为图纸 `FRONT`，例如“以 FreeCAD `REAR` 作为图纸 `FRONT`”。用户未说明前视图时，自动采用 FreeCAD/3D 模型默认 `FRONT`，即 `FREECAD_FRONT → DRAWING_FRONT`。其他五个视图必须从解析后的源方向统一推导，不能继续沿用原模型的旧 TOP/FRONT 名称，也不能逐个手工猜方向。

## 1. 选择输入

1. 参数 `front_source` 只接受 `FRONT`、`REAR`、`LEFT`、`RIGHT`、`TOP`、`BOTTOM` 六个 FreeCAD 标准方向之一。用户明确提供时按输入解析并记录 `front_source_basis=user_selected`；未提供时解析为 `FRONT` 并记录 `front_source_basis=default_model_front`。界面或交付记录必须同时显示“FreeCAD 源方向”和“图纸目标方向”，例如 `FREECAD_FRONT → DRAWING_FRONT` 或 `FREECAD_REAR → DRAWING_FRONT`。
2. 在 FreeCAD 正交相机模式下调用对应标准视图方法：`viewFront()`、`viewRear()`、`viewLeft()`、`viewRight()`、`viewTop()` 或 `viewBottom()`，随后读取活动三维视图的相机旋转 `q = getCameraOrientation()`。不得仅依据 STEP 全局轴名称自行模拟 FreeCAD 的屏幕朝上方向。
3. 若没有用户输入，直接调用 FreeCAD 标准 `viewFront()`，读取该标准相机旋转并继续正式出图，不等待人工选择。可生成带方向名称的预览用于内部校核，但不能把当前随机相机、最大平面、包围框最宽面或算法猜测的“最佳面”替代默认 `FRONT`。已有项目明确规定其他源方向时采用项目输入并注明依据。

## 2. 建立新的绘图坐标系

从所选 FreeCAD 标准相机旋转取得三个互相正交的方向：

- `+X_d = q · (1, 0, 0)`：所选前视图在纸面上的向右方向；
- `+Z_d = q · (0, 1, 0)`：所选前视图在纸面上的向上方向；
- `+Y_d = q · (0, 0, 1)`：所选前视图的观察侧/TechDraw 投影方向。

归一化并复核三轴正交性、右手性和旋转矩阵行列式；若 FreeCAD API 返回的相机约定与当前 TechDraw 导出器不同，以“相同相机旋转生成的 TechDraw 预览”校正符号，并把校正写入记录，不能只改视图标签。

新六视图由该绘图坐标系唯一确定：

- DRAWING_FRONT：观察侧 `+Y_d`；
- DRAWING_REAR：观察侧 `-Y_d`；
- DRAWING_RIGHT：观察侧 `+X_d`；
- DRAWING_LEFT：观察侧 `-X_d`；
- DRAWING_TOP：观察侧 `+Z_d`；
- DRAWING_BOTTOM：观察侧 `-Z_d`。

例如选择 `FREECAD_REAR → DRAWING_FRONT` 后，图纸 FRONT 必须显示 FreeCAD REAR 的相机方向和屏幕朝上方向；其余视图相对这个新 FRONT 旋转推导。图纸标签表达新的六视图含义，不得把 FreeCAD 原名称继续标在视图标题上造成混淆。

## 3. 建立每视图唯一二维变换

1. 为六个 DRAWING 视图分别建立 `T_model_to_view` 和 `T_view_to_sheet`。前者包含观察方向、纸面向右/向上基向量以及 FreeCAD/TechDraw 实际返回的二维轴序、旋转和符号；后者只包含统一比例及该视图在图幅中的平移。每个变换具有唯一 `view_transform_id`。
2. 禁止根据全局 XYZ 直接硬编码 `FRONT=(x,z)`、`LEFT=(y,z)` 等坐标公式。`TechDraw.projectEx()` 可以按视向采用不同局部二维轴序；即使外轮廓看似正确，另写的位号或尺寸坐标也可能旋转、镜像或落入相邻空白区。
3. 在正式标注前，用同一次投影接口处理三个不共线且可辨识的模型参考点/短线，求解或验证 `T_model_to_view`。也可在安装孔锚点处投影不进入最终图层的小型三维十字/圆环，从返回几何直接取得二维中心。不得用生成器自行假设的映射作为校准真值。
4. HLR 几何、普通孔中心、压铆锚点、尺寸落点、视图边界和局部/剖视定位必须调用同一个变换对象；禁止各模块维护独立坐标字典。图幅比例或视图中心改变时，只更新 `T_view_to_sheet` 并重算全部关联实体。
5. 每个视图保存至少三个校准点的三维坐标、实际投影二维坐标、变换计算值、残差和结论。最大纸面残差不得超过项目位置容差；未指定时为 `0.10 mm`。改变 FreeCAD 版本、投影接口、`front_source`、统一比例或视图排布后，六个视图全部重新校准。

## 4. 预览确认与重算范围

1. 在正式标注前生成新的 FRONT、TOP、RIGHT 三个校核预览，显示 FreeCAD 坐标三轴、图纸 `X_d/Y_d/Z_d` 方向和至少三个不共线特征。确认解析后的源面位于 FRONT 近侧、画面朝上正确、左右未镜像；三者确定后再生成其相反视图。默认 `FRONT` 情况不要求用户再次选择，但仍须完成此内部方向校核。
2. 用户更改 `front_source` 后，必须从模型重新计算全部六视图、六个二维变换、压铆柱柱身所在侧映射、位号纸面坐标、视图对齐、图幅评估和表格六面数量。禁止只交换视图标题、移动旧投影或复用旧位号坐标。
3. 六视图完成后仍按固定第三角法版式排放：TOP 在 FRONT 上方、BOTTOM 在下方、LEFT 在左、RIGHT 在右、REAR 在 RIGHT 外侧。这里的名称均指重定义后的 DRAWING 方向。

## 5. 记录与验收

保存源 STEP、FreeCAD 版本、解析后的 `front_source`、`front_source_basis`、调用的标准视图方法、相机四元数 `q`、`X_d/Y_d/Z_d`、源坐标到绘图坐标的变换矩阵、六个 `view_transform_id`、校准点/残差及预览校核结果。验收时至少检查：

- DRAWING_FRONT 与所选 FreeCAD 标准相机的几何、朝上方向和左右关系一致；
- 三对相反视图分别互为正确反向投影，没有通过镜像图元伪造；
- 六视图名称、压铆柱所属面、检查记录中的六面数量和投影基准均已按新坐标系重算；图面三列分类表不显示六面数量；
- 每个压铆锚点使用的 `view_transform_id` 与其目标视图一致，回读位置位于目标视图边界内并与安装孔中心投影重合；
- DXF 回读后视图方向与生成前预览一致。

**通过条件：** 解析后的 `front_source` 及其用户指定/默认来源可追溯；未指定时确为 `FRONT` 且调用 `viewFront()`；新绘图坐标系正交且方向已由预览核对；其余五视图均由同一坐标基推导并具有校准通过的唯一二维变换；不存在只换标签、独立硬编码坐标、左右镜像、位号漂移或压铆柱仍按旧坐标系归面的情况。
