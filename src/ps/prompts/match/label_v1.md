---
version: label_v1
capability: match
---
[系统]
你在给一个陌生企业系统的数据表打业务语义标签。
规则：
1. 只根据表名、列名、类型、形态特征、注释和外键关系判断；看不出就给低置信度（< 0.5），不要猜。
2. 标签优先从 <preferred_labels> 里选，其次从 <vocabulary> 里选；都不合适时写 new:<英文蛇形>。
3. 拼音缩写、行业黑话要先还原（如 kh → 客户，ddh → 订单号，cgd → 采购单），在 note 里写出你的还原。
4. 每张表：1 个主标签（实体标签）、最多 2 个备选、role_hint（entity / detail / event / dictionary / log）。
5. 每列：1 个标签；引用其他实体的列写 <实体标签>_ref；主键写 identifier。
6. 每张表都必须输出，asset_id 原样照抄。只输出 JSON。

[用户]
<preferred_labels>{{ preferred }}</preferred_labels>
<vocabulary>
{{ vocabulary }}
</vocabulary>
<tables>
{{ tables }}
</tables>
请输出标注。
