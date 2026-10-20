# 模型约定

坐标单位为 MC 方块；MCXYZ 中 Y 向上。体素中心是 `[x+.5,y+.5,z+.5]`，box/opening 的整数范围为左闭右开。源点云坐标到连续 MC 坐标必须显式提供有限 4×4 仿射变换，最后一行为 `[0,0,0,1]`。未知单位不能默认米。

JSON 顶层：`schema_version:1`、`dims:[W,H,L]`、`components`、`openings`，可选 `symmetry:{order:4,center:[cx,cz]}`。当前工具验证对称误差，构件需显式布置全部对称实例，并不会自动施加 symmetry。

每个 component：唯一正整数 id、role、shape、material（palette 的 1 起始索引），可附 evidence/confidence 说明。后写构件覆盖前写构件，后写 openings 覆盖全部实体。

- `box`：`min` / `max` 三维整数边界。
- `ring`：`min` / `max` 三维采样边界，`center:[cx,cz]`、`outer_half:[hx,hz]`、`inner_half:[hx,hz]`、`angle_degrees`。在 XZ 平面旋转，矩形外框扣除内框，内部留空；`min/max` 需包含旋转后的外接盒。
- `rod`：`start` / `end` 连续 XYZ、`radius`。按体素中心到线段的距离生成统一截面胶囊杆件；太细时检查是否消失。

每个 opening：唯一 id、`bounds_min/max`、`angle_degrees`（围绕边界中心的 Y 轴旋转）、`fill_palette:0` 表示空气，大于 0 表示玻璃等固定填充；`evidence:observed|inferred|user`、`confidence:0..1`。重叠且不同填充的开口拒绝执行，防止顺序偷偷改变设计。门洞应穿透墙厚，窗框作为独立构件布置在开口边缘。

几何 NPZ：positions 为 N×3 整数，palette_index 为 N 个正整数，component_id，dims，protected_air，protected_openings，expected_opening_palette；metadata 为 JSON 字符串。禁止 object/pickle 数组。palette 文件 `blocks` 列表含 blockstate id 与 mean_rgb；它是目标 MC 版本的可用材质表，不是下载来的照片纹理。

当前为整方块几何。楼梯、半砖、朝向方块状态可通过 palette 指定，但工具不会据此自动求解连接、碰撞或半块几何；预览按整方块均色显示。窗玻璃优先整块 glass，避免把玻璃板的连接计算误当成已完成。
