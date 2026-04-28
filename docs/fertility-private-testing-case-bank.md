# 私有历史检测案例库处理说明

本文档说明如何从 `WES类项目数据统计25年截止12月.xlsx` 构建 `yk-FERTA` 私有历史检测案例库。

## 1. 输入数据特点

输入文件：

- `database/WES类项目数据统计25年截止12月.xlsx`
- 使用 sheet：`合并`

源数据特点：

- 行粒度是“变异一行”。
- 同一项目的基础信息只在部分行填写，后续变异行大量为空。
- 缺少 HPO。
- 缺少最终临床诊断。
- 有临床信息、检测结论、变异信息、变异评级、相关疾病、咨询建议等。

## 2. 输出文件

当前生成两个表：

- `database/fertility_private_testing_cases_2025.csv`
- `database/fertility_private_testing_variants_2025.csv`
- `database/fertility_private_testing_cases_2025.stats.json`

生成脚本：

- `scripts/build_private_testing_case_bank.py`

## 3. 病例表设计

病例表是一项目一行，保留 DeepRare 兼容字段：

- `_id`
- `case_report`
- `diagnosis`
- `Orpha_name`
- `Orpha_id`
- `age`
- `gender`
- `embedding`

其中：

- `diagnosis` 不是最终临床诊断，而是兼容检索系统的案例标签。
- `diagnosis_status` 固定为 `no_final_diagnosis`。
- `Orpha_name / Orpha_id` 当前为空。
- `embedding` 当前为空，后续生成。

扩展字段包括：

- `project_id`
- `clinical_info`
- `spouse_clinical_info`
- `test_project`
- `report_status`
- `reported_genes`
- `phenotype_relevant_genes`
- `variant_summary`
- `variant_interpretation_summary`
- `proband_conclusion`
- `spouse_conclusion`
- `fmr1_result`
- `y_microdeletion_result`
- `variant_count`
- `phenotype_relevant_variant_count`
- `carrier_variant_count`
- `cnv_count`
- `retrieval_tags`
- `data_quality`

## 4. 当前统计

源数据：

- 源行数：`47,716`
- 项目数：`13,616`
- 变异明细行：`43,703`

病例表：

- 总病例数：`13,616`
- positive：`6,589`
- negative：`6,974`
- unknown：`53`

数据质量：

- high：`10,127`
- medium：`3,297`
- low：`192`

主要检索标签：

- `carrier_variant`: `7,155`
- `phenotype_relevant_variant`: `946`
- `male_factor`: `956`
- `infertility`: `856`
- `recurrent_pregnancy_loss`: `332`
- `cnv`: `161`
- `poi`: `89`
- `dsd`: `53`
- `molar_pregnancy`: `38`

## 5. 重要限制

这不是确诊病例库，而是：

**私有历史检测案例库 / 表型-基因检测发现库。**

因此后续推理输出中不能写：

> 相似病例提示该患者诊断为某病。

更准确的表达应为：

> 相似历史检测案例中，曾在相近临床背景下检出某些表型相关基因/变异。

## 6. 脱敏状态

当前处理：

- 未输出 `受检人姓名`。
- 未输出 `配偶姓名`。
- 未输出 `医院送检编码`。
- 已使用受检人/配偶姓名对自由文本做精确替换。

仍需注意：

- 临床自由文本中可能包含亲属姓名或其他非结构化身份信息。
- 在进入正式服务或共享前，应增加更严格的中文医疗文本脱敏流程。

## 7. 后续建议

短期建议：

- 优先使用 `phenotype_relevant_variant_count > 0` 或 `retrieval_tags` 非空的病例生成 embedding。
- 对 `carrier_variant` 且无不孕不育临床信息的病例降低检索权重。
- 检索展示时明确标注为“历史检测案例”，而不是“确诊病例”。

中期建议：

- 用现有 RAG-HPO 服务为 `clinical_info + spouse_clinical_info` 补 HPO。
- 建立 `reported_genes -> fertility disease/process` 的本地映射。
- 对病例生成更规范的 `case_report_for_retrieval`。
- 对变异建立结构化 JSON 或关系表，支持按基因/ACMG/遗传模式过滤。

## 8. HPO 补充

补充来源：

- `database/GDT项目临床信息.xlsx`

补充输出：

- `database/fertility_private_testing_cases_2025.with_hpo.csv`
- `database/fertility_private_testing_cases_2025.with_hpo.stats.json`
- 生成脚本：`scripts/enrich_private_case_bank_with_hpo.py`

匹配方式：

- 使用 `送检单编号` 对齐 `project_id`。
- 从 `受检人HPO表型术语` 和 `配偶HPO表型术语` 中解析 `HP:xxxxxxx`。
- 仅把可解析到 `HP:` 编码的内容写入结构化 HPO 字段。
- 原始 HPO 文本保留在 `proband_hpo_raw` 和 `spouse_hpo_raw`，便于审计。

当前统计：

- 私有病例库项目数：`13,616`
- GDT 临床信息表项目数：`16,370`
- 可按项目号匹配：`13,615`
- 成功解析到 HPO 的病例：`3,636`
- 解析出的 HPO term 总数：`13,981`

新增字段：

- `hpo_labels`
- `hpo_term_details`
- `proband_hpo_raw`
- `spouse_hpo_raw`
- `gdt_clinical_info`
- `gdt_proband_clinical_info`
- `gdt_spouse_clinical_info`
- `hpo_term_count`

注意：

- 部分 `HPO表型术语` 单元格实际填写的是自由文本或 `/`，这些不会被写入 `hpo_terms`。
- `case_report` 中的 `HPO terms:` 行已同步替换为解析后的 HPO 编码和中文标签。

## 9. 本地相似案例检索索引

当前已基于公共病例库和私有历史检测案例库构建一个最小本地 RAG 检索索引。

索引输入：

- 公共病例库：`database/fertility_public_cases_rds.csv`
- 私有历史检测案例库：`database/fertility_private_testing_cases_2025.with_hpo.csv`

索引输出：

- 量化向量：`database/fertility_case_vector_index.npz`
- 向量元数据：`database/fertility_case_vector_metadata.csv`
- 本地向量器：`database/fertility_case_vectorizer.joblib`
- 统计文件：`database/fertility_case_vector_index.stats.json`
- 构建脚本：`scripts/build_fertility_case_vector_index.py`

实现方式：

- 使用本地 `TF-IDF char ngram + SVD` 生成 dense embedding。
- 向量归一化后以 `float16` 保存，作为轻量量化索引。
- 在线检索时先做向量召回，再结合 HPO、疾病/生殖标签、基因、表型相关变异数等规则做 rerank。

当前索引统计：

- 总案例数：`15,212`
- 公共病例：`1,596`
- 私有检测案例：`13,616`
- 向量维度：`256`
- 存储类型：`float16`

证据角色：

- 公共病例返回为 `diagnosis_reference`，可作为相似诊断病例参考。
- 私有检测案例返回为 `testing_finding_reference`，只作为历史检测发现参考，不能当作确诊病例。

重建命令：

```bash
/home/zhangly/micromamba/envs/yk-ferta-dev/bin/python scripts/build_fertility_case_vector_index.py
```
