---
name: preserve-building-openings
description: 在建筑点云转 MC、墙体补全或规则建模中识别并保护门窗、连廊、架空层和平台中孔，防止几何修复或材质映射封住开口。
---

# 建筑开口保护

当任务重点是楼房门窗、洞口或通行空间时使用。MCP 为 `building-reconstruction`；具体输入输出见交付包 `C:/BuildingWorkspace/reconstruction-kit/README.md`。

建立楼层与立面规则后，把实体构件与 openings 分开记录。墙体生成完成后再扣洞，空气 `fill_palette:0`；需要玻璃时明确指定对应 palette 索引并锁定。门窗来源记录 evidence/confidence。点云缺失可能由玻璃、树木遮挡或光照导致，不能据此自动判定窗位。

固定窗格模数、间距、窗台高度和墙厚；窗框放在开口边缘。连廊、架空层、挑空和塔身孔也作为负空间。开口必须穿透墙厚，且缩放后至少留下有效体素。新增支撑不能穿过开口。

`build_geometry` 后调用 `validate_build`；`map_materials` 后再次验证 protected_air 无占据、protected_openings 的逐格材质保持 expected_opening_palette。连接性只作诊断，不能为了单连通而封门窗或抹去合法悬挑。交付时分别报告空气格、玻璃格、推断窗位和未验证通行性。
