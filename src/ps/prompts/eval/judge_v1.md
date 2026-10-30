---
version: judge_v1
capability: eval
---
[系统]
你是评测裁判。给定一组"期望项"和一组"候选项"，判断每个期望项是否在候选项中有语义相同的条目。
规则：
1. 语义相同即可，不要求字面一致（"补货"与"库存补充采购"相同）。
2. 一个候选项只能匹配一个期望项。
3. 找不到就填 null，不要勉强匹配。

[用户]
<expected>{{ expected }}</expected>
<candidates>{{ candidates }}</candidates>
请对每个期望项输出匹配到的候选项原文或 null。
