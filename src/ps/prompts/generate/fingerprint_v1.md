---
version: fingerprint_v1
capability: match
---
[系统]
你要为一个业务流程或业务问题写出"它在数据库里应该留下的痕迹"（数据指纹）。

规则：
1. 只能使用这些 kind：
   - entity_table：某类业务对象的主表（semantic 为实体标签）
   - event_table：一类发生记录的明细表（semantic 为实体标签；needs_columns 列出必须有的字段标签；需要历史时写 min_history_days）
   - column_semantic：某对象上必须有的一类字段（on 为实体标签，semantic 为字段标签）
   - fk_chain：对象之间的引用链（path 为实体标签列表，按引用方向）
   - status_column：流程状态字段（on 为实体标签）
   - event_time：流程发生时间字段（on 为实体标签）
2. semantic / on / path / needs_columns 里的标签必须从 <vocabulary> 中选；都不合适才写 new:<英文蛇形>。不要猜具体表名。
3. 3–8 条；缺了它流程就不可能存在、问题就不可能回答的条目标 must=true（通常 1–2 条）；weight 取 1–3。
4. 如果给了 <answer_plan>，指纹必须逐项覆盖它的 inputs。
5. 每条给一个短 id（f1、f2…）。只输出 JSON。

[用户]
<target>{{ target }}</target>
<answer_plan>{{ answer_plan }}</answer_plan>
<vocabulary>
{{ vocabulary }}
</vocabulary>
请输出数据指纹。
